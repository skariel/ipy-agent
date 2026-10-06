"""Synthetic raster output reaches model input; no live providers."""
from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from py_agent.codex import CodexProvider
from py_agent.contracts import ContextSnapshot, ExecutionOutput, ExecutionRequest, ExecutionResult, ModelRequest, Origin
from py_agent.coordinator_observations import ModelObservations
from py_agent.images import MAX_EDGE, MAX_IMAGES, ImageAttachment, content_with_images, normalize
from py_agent.local_executor import LocalExecutor
from py_agent.local_worker import _safe_mime_bundle
from py_agent.production_services import ProductionContextAdapter, ProductionProviderAdapter
from py_agent.provider import LitelmProvider, ProviderError, _validate_messages
from py_agent.session_journal import SQLiteSessionJournal


def png(size=(32, 24), color="red", metadata=False):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    if metadata:
        info.add_text("private-note", "secret metadata")
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def attachment():
    return normalize("image/png", png())


def packer():
    from py_agent.builtin_services import BuiltinPlugin
    from py_agent.coordinator import Coordinator
    from py_agent.plugins import PluginRuntime
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake", interpreter="basic", executor="local")
    return ModelObservations(coordinator)


def origin():
    return Origin("session", "request", "terminal", 1, "generation", "execution")


def test_normalization_resizes_strips_metadata_and_validates_records():
    image = normalize("image/png", png((2000, 1000), metadata=True))
    assert image.width == MAX_EDGE
    assert image.height == MAX_EDGE // 2
    assert b"secret metadata" not in image.data
    assert image == ImageAttachment.from_record(image.record())
    assert base64.b64encode(image.data).decode() not in repr(image)
    bad = image.record()
    bad["sha256"] = "tampered"
    with pytest.raises(ValueError):
        ImageAttachment.from_record(bad)


def test_transparent_images_use_white_background_and_animation_uses_first_frame():
    buffer = io.BytesIO()
    Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(buffer, "PNG")
    normalized = normalize("image/png", buffer.getvalue())
    assert Image.open(io.BytesIO(normalized.data)).getpixel((0, 0)) == (255, 255, 255)
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, "GIF", save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")], duration=10)
    normalized = normalize("image/gif", buffer.getvalue())
    assert Image.open(io.BytesIO(normalized.data)).getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize("mime,value", [
    ("image/png", "not base64!"), ("image/png", b"not an image"),
    ("image/svg+xml", b"<svg/>"), ("image/png", b"x" * 8_000_001),
])
def test_invalid_images_are_rejected(mime, value):
    with pytest.raises(ValueError):
        normalize(mime, value)


def test_pixel_limit_rejects_before_full_decode():
    with pytest.raises(ValueError, match="pixel"):
        normalize("image/png", png((4100, 4100)))


def test_context_image_role_alignment_and_remote_url_rejection():
    image = attachment()
    with pytest.raises(ValueError):
        ContextSnapshot(0, (("system", "policy"),), images=((0, image),))
    with pytest.raises(ValueError):
        ContextSnapshot(0, (("user", "task"),), images=((1, image),))
    with pytest.raises(ValueError):
        _validate_messages([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/private.png"}}]}], None)


def test_display_observation_stays_structured_through_context():
    image = attachment()
    data = {"image/png": image.record()["data"], "text/plain": "<PIL.Image>"}
    result = ExecutionResult(origin(), "success", output_events=tuple(
        ExecutionOutput("display", data) for _ in range(MAX_IMAGES + 1)))
    request = ExecutionRequest(origin(), "display(image)", "agent")
    policy = packer()
    packed = policy._packed_observation(request, result)
    assert len(packed["_images"]) == MAX_IMAGES
    assert "Additional image omitted" in json.dumps(packed)
    context = ProductionContextAdapter()
    context.add("user", "inspect image")
    context.commit_response("request", "display(image)", observation=packed)
    snapshot = context.snapshot()
    assert len(snapshot.images) == MAX_IMAGES
    assert image.record()["data"] not in str(snapshot.messages)
    messages = context.provider_messages(snapshot)
    observation = messages[-1]
    assert observation["role"] == "user"
    assert observation["content"][0]["type"] == "text"
    assert observation["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_context_bounds_old_images_with_explicit_omission():
    context = ProductionContextAdapter()
    for i in range(20):
        context.commit_response(str(i), "display(image)", observation={
            "output": "[Image attached]", "_images": [attachment().record()]})
    snapshot = context.snapshot()
    assert len(snapshot.images) == 16
    assert "Older image attachment omitted" in str(snapshot.messages)
    assert all(snapshot.messages[index][0] == "observation" for index, _ in snapshot.images)


def test_codex_maps_only_user_attachments_to_input_image():
    image = attachment()
    messages = [{"role": "system", "content": "Python only"},
                {"role": "assistant", "content": "display(image)", "phase": "commentary"},
                {"role": "user", "content": content_with_images("Image output", [image])}]
    body = CodexProvider("openai-codex/gpt-5.4").build_request(messages)
    assert body["input"][0]["content"][0]["type"] == "output_text"
    assert body["input"][1]["content"][1] == {
        "type": "input_image", "image_url": image.data_url(), "detail": "auto"}


@pytest.mark.asyncio
async def test_api_adapter_passes_inline_images_and_rejects_text_only(monkeypatch, tmp_path):
    import litelm
    calls = []
    async def complete(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"role": "assistant", "content": "pass"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(litelm, "acompletion", complete)
    messages = [{"role": "user", "content": content_with_images("inspect", [attachment()])}]
    await LitelmProvider("openai/gpt-4o", stream=False, auth_file=tmp_path / "absent.json").generate(messages)
    assert calls[0]["messages"][0]["content"][1]["type"] == "image_url"
    with pytest.raises(ProviderError, match="not silently discarded"):
        await LitelmProvider("deepseek/deepseek-chat", stream=False, auth_file=tmp_path / "absent.json").generate(messages)
    assert len(calls) == 1
    await LitelmProvider("anthropic/claude-sonnet-4", stream=False, auth_file=tmp_path / "absent.json").generate(messages)
    block = calls[-1]["messages"][0]["content"][1]
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_python_pillow_display_reaches_next_provider_turn():
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(ExecutionRequest(origin(),
            "from PIL import Image\nfrom IPython.display import display\n"
            "display(Image.new('RGB', (20, 10), 'blue'))", "agent"))
        assert result.status == "success"
        event = next(event for event in result.output_events if "image/png" in event.data)
        assert normalize("image/png", event.data["image/png"]).width == 20
        packed = packer()._packed_observation(
            ExecutionRequest(origin(), "display(image)", "agent"), result)
        context = ProductionContextAdapter()
        context.add("user", "inspect")
        context.commit_response("request", "display(image)", observation=packed)
        captured = []
        class Backend:
            model = "openai/gpt-4o"
            async def generate(self, messages, *, max_tokens=None):
                captured.append(messages)
                return SimpleNamespace(text="pass", successful=True, finish_reason="stop",
                                       rejection_reason=None, usage={})
        adapter = ProductionProviderAdapter(Backend(), context=context)
        await adapter.generate(ModelRequest(origin(), context.snapshot(), "openai/gpt-4o"))
        assert captured[0][-1]["content"][1]["type"] == "image_url"
    finally:
        await executor.close()


def test_journal_preserves_accepted_image_bytes_in_request(tmp_path):
    import sqlite3
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    journal = SQLiteSessionJournal(path / "history.sqlite")
    image = attachment()
    context = ContextSnapshot(0, (("user", "display result"),), images=((0, image),))
    journal.record_model_request(ModelRequest(origin(), context, "openai/gpt-4o"))
    journal.close()
    with sqlite3.connect(path / "history.sqlite") as db:
        schema = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert schema
        row = db.execute("SELECT payload FROM events WHERE kind='model_request'").fetchone()
    content = json.loads(row[0])
    saved = content["content"]["context"]["images"][0]
    assert saved["message_index"] == 0
    assert ImageAttachment.from_record(saved) == image


def test_worker_normalizes_valid_raster_and_reports_invalid():
    data, _ = _safe_mime_bundle({"image/png": png((2000, 1000)), "text/plain": "image"}, {})
    assert normalize("image/png", data["image/png"]).width == MAX_EDGE
    data, _ = _safe_mime_bundle({"image/png": b"not valid"}, {})
    assert "image/png" not in data
    assert "Image rejected" in data["text/plain"]


def test_clear_and_update_display_replace_old_rasters():
    from py_agent.images import observation_images
    red = normalize("image/png", png(color="red"))
    blue = normalize("image/png", png(color="blue"))
    records, _ = observation_images((
        ExecutionOutput("display", {"image/png": red.record()["data"]}, display_id="figure"),
        ExecutionOutput("update", {"image/png": blue.record()["data"]}, display_id="figure"),
    ))
    assert len(records) == 1
    assert ImageAttachment.from_record(records[0]).sha256 == blue.sha256
    records, _ = observation_images((
        ExecutionOutput("display", {"image/png": red.record()["data"]}),
        ExecutionOutput("clear", {}),
    ))
    assert not records


@pytest.mark.asyncio
async def test_collapse_archives_images_and_removes_them_from_active_context():
    from py_agent.context import Context, Limits
    context = Context(Limits())
    adapter = ProductionContextAdapter(context)
    start = adapter.add("user", "inspect")
    adapter.commit_response("request", "display(image)", observation={
        "output": "image", "_images": [attachment().record()]})
    end = adapter.add("user", "continue")
    before = adapter.snapshot()
    assert before.images
    saved = []
    async def store(value):
        saved.append(value)
        return 1
    await adapter.collapse(start.messages[0]["boundary_id"], end.messages[0]["boundary_id"],
                           "Saw a red image.", store)
    assert not adapter.snapshot().images
    archive = json.loads(saved[0])
    assert attachment().record()["data"] in json.dumps(archive)


def test_context_byte_budget_and_request_limit():
    from py_agent.images import MAX_CONTEXT_IMAGE_BYTES
    # Noise PNG stays within per-image limit but exceeds total over repeats.
    noise = Image.effect_noise((600, 600), 100).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    image = normalize("image/png", buffer.getvalue())
    context = ProductionContextAdapter()
    for i in range(20):
        context.commit_response(str(i), "display(image)", observation={
            "output": "image", "_images": [image.record()]})
    snapshot = context.snapshot()
    assert sum(len(item.data) for _, item in snapshot.images) <= MAX_CONTEXT_IMAGE_BYTES
    assert "Older image attachment omitted" in str(snapshot.messages)
    with pytest.raises(ValueError, match="byte limit"):
        ContextSnapshot(0, (("user", "image"),), images=tuple((0, image) for _ in range(16)))


@pytest.mark.asyncio
async def test_matplotlib_display_emits_image_mime():
    pytest.importorskip("matplotlib")
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(ExecutionRequest(origin(),
            "get_ipython().run_line_magic('matplotlib', 'inline')\n"
            "import matplotlib.pyplot as plt\nplt.plot([1, 2], [3, 4])\nplt.show()", "agent"))
        assert result.status == "success", result.error
        assert any("image/png" in output.data for output in result.output_events)
    finally:
        await executor.close()


@pytest.mark.parametrize("model", [
    "openrouter/openai/gpt-4o", "openrouter/anthropic/claude-sonnet-4",
    "gemini/gemini-2.5-pro", "openai/gpt-5.4",
])
def test_routed_vision_models_accept_attachments(model):
    from py_agent.images import require_vision
    require_vision(model, [{"role": "user", "content": content_with_images("inspect", [attachment()])}])


@pytest.mark.parametrize("model", [
    "openai-codex/gpt-6.1-sol", "openrouter/openai/gpt-6.1-sol",
    "custom/new-vision-model",
])
def test_new_model_names_do_not_block_images(model):
    from py_agent.images import require_vision
    messages = [{"role": "user", "content": content_with_images("inspect", [attachment()])}]
    require_vision(model, messages)
    if model.startswith("openai-codex/"):
        body = CodexProvider(model).build_request(messages)
        assert body["input"][0]["content"][1]["type"] == "input_image"
        assert body["input"][0]["content"][1]["image_url"] == attachment().data_url()


@pytest.mark.asyncio
async def test_sol_image_reaches_codex_endpoint(monkeypatch):
    import httpx

    from py_agent import codex
    monkeypatch.setattr(codex, "read_codex_credentials", lambda _: SimpleNamespace(
        access="synthetic-token", account_id="test-account", expires=9999999999999))
    captured = []
    def handle(request):
        captured.append(json.loads(request.content))
        event = {"type": "response.completed", "response": {
            "id": "mock", "status": "completed",
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "say('image received', final=True)"}]}]}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("data: " + json.dumps(event) + "\n\n").encode())
    provider = CodexProvider("openai-codex/gpt-6.1-sol", transport=httpx.MockTransport(handle))
    response = await provider.generate([
        {"role": "user", "content": content_with_images("Read the image", [attachment()])}])
    assert response.successful
    assert captured[0]["model"] == "gpt-6.1-sol"
    assert captured[0]["input"][0]["content"][1]["image_url"] == attachment().data_url()
