"""Python session standard library.

``llm`` is host-backed: no credentials or provider clients enter the kernel.
Other existing helpers remain injected globals (say/preview/read_output/collapse).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
import io
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from PIL.Image import Image as PillowImage

    from .images import ImageAttachment

from .inspection import source, test_summary

_ACTIVE_LLM: ContextVar[Callable[[dict[str, Any]], str] | None] = ContextVar("py_active_llm", default=None)
MAX_PROMPT_CHARS = 65536
MAX_RESULT_CHARS = 65536
MAX_CALLS_PER_CELL = 8
MAX_RPC_IMAGE_BYTES = 512000
DEFAULT_SYSTEM = (
    "You are a helpful assistant called from a Python program. "
    "Answer the supplied task directly as text. Do not assume access to an agent "
    "conversation or Python namespace. Your response is data, never executed."
)


def llm(prompt: str, *, system: str = DEFAULT_SYSTEM, images: Sequence[ImageAttachment | bytes | PillowImage] = (), max_tokens: int = 2048) -> str:
    """Call the session's current model/effort with a fresh, independent context.

    Returns text, never executes it. Images accept Pillow images, encoded raster
    bytes, or ImageAttachment. Calls are separately billable and journaled.
    Only usable in the active cell's main thread; eight calls per cell.
    """
    callback = _ACTIVE_LLM.get()
    if callback is None:
        raise RuntimeError("llm() requires an active py execution with host model support")
    return callback(_payload(prompt, system=system, images=images, max_tokens=max_tokens))


def _payload(prompt: str, *, system: str = DEFAULT_SYSTEM, images: Sequence[ImageAttachment | bytes | PillowImage] = (), max_tokens: int = 2048) -> dict[str, Any]:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError("llm prompt must be nonempty text, at most 65536 characters")
    if not isinstance(system, str) or not system.strip() or len(system) > MAX_PROMPT_CHARS:
        raise ValueError("llm system must be nonempty text, at most 65536 characters")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 16384:
        raise ValueError("llm max_tokens must be an integer from 1 to 16384")
    from .images import MAX_IMAGES, ImageAttachment, normalize
    if not isinstance(images, (tuple, list)) or len(images) > MAX_IMAGES:
        raise ValueError("llm images must be a list/tuple of at most four rasters")
    records = []
    size = 0
    for value in images:
        if isinstance(value, ImageAttachment):
            image = value
        elif isinstance(value, bytes):
            image = normalize("image/png", value)
        else:
            from PIL import Image
            if not isinstance(value, Image.Image):
                raise TypeError("llm images require Pillow images, raster bytes, or ImageAttachment")
            if value.width * value.height > 16_000_000:
                raise ValueError("llm image exceeds pixel limit")
            buffer = io.BytesIO()
            value.save(buffer, format="PNG")
            image = normalize("image/png", buffer.getvalue())
        size += len(image.data)
        if size > MAX_RPC_IMAGE_BYTES:
            raise ValueError("llm images exceed the 512000-byte combined RPC limit")
        records.append(image.record())
    return {"prompt": prompt, "system": system, "images": records, "max_tokens": max_tokens}


def validate_payload(payload: object) -> dict[str, Any]:
    """Validate again on the trusted host boundary."""
    from .images import MAX_IMAGES, ImageAttachment
    if not isinstance(payload, dict) or set(payload) != {"prompt", "system", "images", "max_tokens"}:
        raise ValueError("Invalid llm request")
    records = payload["images"]
    if not isinstance(records, list) or len(records) > MAX_IMAGES:
        raise ValueError("Invalid llm images")
    images = [ImageAttachment.from_record(record) for record in records]
    return _payload(payload["prompt"], system=payload["system"], images=images,
                    max_tokens=payload["max_tokens"])


# Documented capabilities must be registered in the actual worker namespace.
HELPERS = {
    "say": ("say(text, final=False)", "Communicate with the user; final=True finishes."),
    "preview": ("preview(value, label=None)", "Bounded output inspection; retain originals in variables."),
    "read_output": ("read_output(index, start=0, limit=4000)", "Read an archived output excerpt."),
    "collapse": ("collapse(start_id, end_id, summary)", "Standalone context management; use literal boundary IDs."),
    "llm": ("llm(prompt, *, system=DEFAULT_SYSTEM, images=(), max_tokens=2048) -> str",
            "Host-backed call to the current model/effort using a fresh conversation; returns data, never executes."),
    "source": ("source(path, start=1, end=None, *, limit=6000) -> str",
               "Bounded numbered source excerpt; reads incrementally, never executes."),
    "test_summary": ("test_summary(result, *, limit=4000) -> dict",
                     "Summarize a retained text-mode subprocess result without rerunning tests."),
}


def install_helpers(namespace: dict[str, object], implementations: Mapping[str, Callable[..., object]]) -> None:
    if set(implementations) != set(HELPERS) or not all(callable(fn) for fn in implementations.values()):
        raise RuntimeError("Session helper registry does not match runtime implementations")
    namespace.update(implementations)


def helper_prompt() -> str:
    lines = ["## Python session standard library",
             "", "These registered functions are available as Python globals; no import is required:"]
    lines.extend(f"- {signature}: {description}" for signature, description in HELPERS.values())
    return "\n".join(lines)




def _standalone_collapse(*args: object, **kwargs: object) -> NoReturn:
    raise RuntimeError("collapse requires a standalone literal call handled by the coordinator")


def runtime_helpers(*, say: Callable[..., object], preview: Callable[..., object], read_output: Callable[..., object], llm: Callable[..., object]) -> dict[str, Callable[..., object]]:
    """Actual worker bindings, validated against the documented registry."""
    return {"say": say, "preview": preview, "read_output": read_output,
            "collapse": _standalone_collapse, "llm": llm,
            "source": source, "test_summary": test_summary}
