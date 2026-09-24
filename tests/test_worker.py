"""Unsandboxed RUNTIME unit tests with fixed trusted snippets only.

This fixture is deliberately test-local. It is NOT an execution backend, does
not accept model output, and proves no filesystem/network isolation. Production
uses Sandbox.start(); its independent integration tests remain required.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import re
import resource
import signal
import sys
import threading

import pytest

from py_agent.protocol import WORKER_TYPES, read_frame, write_frame

WORKER = Path(__file__).resolve().parents[1] / "src/py_agent/worker.py"

# Fixed test-only bootstrap. Production still explicitly writes to /tmp; only
# this module reference is replaced, never the shared stdlib tempfile module.
WORKER_BOOTSTRAP = r"""
import runpy, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
worker, artifacts = map(Path, sys.argv[1:])
sys.path.insert(0, str(worker.parents[1]))
import py_agent.output as output

def local_mkstemp(**kwargs):
    assert kwargs["dir"] == "/tmp"
    return tempfile.mkstemp(**(kwargs | {"dir": artifacts}))

output.tempfile = SimpleNamespace(mkstemp=local_mkstemp)
runpy.run_path(str(worker), run_name="__main__")
"""


class Runtime:
    def __init__(self, process):
        self.process = process
        self.counter = 0
        self.events = []

    async def recv(self, timeout=8):
        try:
            frame = await asyncio.wait_for(read_frame(self.process.stdout, allowed_types=WORKER_TYPES), timeout)
        except EOFError as exc:
            diagnostic = b""
            with suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 1)
                diagnostic = await asyncio.wait_for(self.process.stderr.read(4096), 1)
            raise EOFError(
                f"Worker transport closed (exit={self.process.returncode}); stderr: {diagnostic.decode('utf-8', 'replace')}"
            ) from exc
        self.events.append(frame)
        return frame

    async def dispatch(self, source, cell_id=None):
        self.counter += 1
        cell_id = cell_id or f"a1:c{self.counter:04d}"
        await write_frame(self.process.stdin, {"v": 1, "type": "execute", "cell_id": cell_id, "source": source})
        return cell_id

    async def execute(self, source, broker=None):
        cell_id = await self.dispatch(source)
        events = []
        while True:
            frame = await self.recv()
            if frame["cell_id"] != cell_id:
                continue  # asynchronous previous-cell output remains in .events
            events.append(frame)
            if frame["type"] == "broker_request":
                assert broker is not None
                response = broker(frame)
                await write_frame(
                    self.process.stdin,
                    {
                        "v": 1,
                        "type": "broker_response",
                        "cell_id": cell_id,
                        "request_id": frame["request_id"],
                        **response,
                    },
                )
            if frame["type"] == "cell_end":
                return events

    async def stop(self):
        if self.process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        await self.process.wait()


@pytest.fixture()
async def runtime(tmp_path):
    # Poison locations that InteractiveShellApp or ambient startup could read.
    startup = tmp_path / "profile" / "profile_py_agent" / "startup"
    startup.mkdir(parents=True)
    (startup / "00-untrusted.py").write_text("raise AssertionError('startup must not run')")
    (tmp_path / "profile" / "profile_py_agent" / "ipython_config.py").write_text(
        "raise AssertionError('profile config must not run')"
    )
    (tmp_path / "sitecustomize.py").write_text("raise AssertionError('ambient import must not run')")
    env = {
        "HOME": str(tmp_path),
        "IPYTHONDIR": str(tmp_path / "profile"),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "PAGER": "/bin/cat",
    }
    artifacts = tmp_path / "output-artifacts"
    artifacts.mkdir(mode=0o700)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        WORKER_BOOTSTRAP,
        str(WORKER),
        str(artifacts),
        cwd=tmp_path,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    rt = Runtime(process)
    rt.workspace = tmp_path
    try:
        ready = await rt.recv()
        assert ready["type"] == "ready"
        yield rt
    finally:
        await rt.stop()
        # Artifacts belong to pytest's per-test directory. Never unlink paths
        # parsed from notices: shared /tmp may contain identical user output.


def text(events, stream=None):
    return "".join(
        event["text"] for event in events if event["type"] == "output" and (stream is None or event["stream"] == stream)
    )


def end(events):
    assert events[-1]["type"] == "cell_end"
    return events[-1]


async def test_persistent_assignments_functions_and_display(runtime):
    first = await runtime.execute("a = 2\ndef sumit(x, y):\n    return x + y\nprint(a)")
    second = await runtime.execute("sumit(a, 10)")
    assert text(first) == "2\n"
    assert text(second, "display") == "12"
    assert end(first)["execution_count"] == 1
    assert end(second)["execution_count"] == 2
    assert end(second)["status"] == "success"


async def test_success_without_output_and_ipython_history(runtime):
    result = await runtime.execute("number = 123")
    assert len(result) == 1
    assert end(result)["status"] == "success"
    history = await runtime.execute("%history -n")
    assert "number = 123" in text(history)
    names = await runtime.execute("%whos")
    assert "number" in text(names)
    assert "123" in text(names)


@pytest.mark.parametrize("source", ["```python\nx = 99\n```", "Here is some Python code:\nx=99", "x = ("])
async def test_format_errors_never_extract_source(runtime, source):
    result = await runtime.execute(source)
    assert end(result)["status"] == "error"
    assert "SyntaxError" in end(result)["error"]
    check = await runtime.execute("print('x' in globals())")
    assert text(check) == "False\n"


async def test_exception_keeps_partial_effects_and_final_is_not_success(runtime):
    result = await runtime.execute("changed = 17\nsay('not done', final=True)\nraise ValueError('later failure')")
    assert any(event["type"] == "say" and event["final"] for event in result)
    assert end(result)["status"] == "error"
    assert "ValueError: later failure" in text(result)
    assert text(await runtime.execute("print(changed)")) == "17\n"
    # The supervisor, not this worker, stages/discards the final say above.


async def test_wait_unwinds_without_traceback_and_does_not_resume(runtime):
    result = await runtime.execute("before = 1\ntry:\n    wait()\nfinally:\n    cleanup = 2\nafter = 3")
    assert end(result)["status"] == "wait"
    assert text(result) == ""
    assert text(await runtime.execute("print(before, cleanup, 'after' in globals())")) == "1 2 False\n"


@pytest.mark.parametrize(
    "source",
    [
        "side_effect = 1\nsay('done', final=True)\nwait()",
        "try:\n    wait()\nfinally:\n    say('done', final=True)",
    ],
)
async def test_conflicting_control_requests(runtime, source):
    result = await runtime.execute(source)
    assert end(result)["status"] == "invalid_control"
    assert "preceding effects remain" in end(result)["error"]


async def test_error_during_wait_finally_is_not_wait(runtime):
    result = await runtime.execute("try:\n    wait()\nfinally:\n    raise RuntimeError('cleanup failed')")
    assert end(result)["status"] == "error"
    assert "cleanup failed" in end(result)["error"]


async def test_progress_final_json_and_ordinary_output_order(runtime):
    result = await runtime.execute(
        "print('first')\nsay({'progress': 1})\nprint('second')\nsay({'answer': 42}, final=True)"
    )
    significant = [event for event in result if event["type"] != "cell_end"]
    assert significant[0]["type"] == "output"
    assert significant[0]["text"] == "first\n"
    assert significant[1]["type"] == "say"
    assert significant[1]["content"] == {"progress": 1}
    assert significant[2]["type"] == "output"
    assert significant[2]["text"] == "second\n"
    assert significant[3]["final"] is True
    assert end(result)["status"] == "success"


async def test_native_subprocess_stderr_and_control_separation(runtime):
    result = await runtime.execute(
        "import os, subprocess\nos.write(1, b'{\"v\":1,\"type\":\"ready\",\"kernel_pid\":999}\\n')\nos.write(2, b'native error\\n')\nsubprocess.run(['/bin/sh', '-c', 'printf subprocess; printf suberror >&2'])\nNone"
    )
    assert '{"v":1,"type":"ready","kernel_pid":999}' in text(result, "stdout")
    assert "subprocess" in text(result, "stdout")
    assert "native error" in text(result, "stderr")
    assert "suberror" in text(result, "stderr")
    assert all(event["type"] != "ready" for event in result)
    assert end(result)["status"] == "success"


async def test_libc_output_does_not_leak_into_the_next_cell(runtime):
    first = await runtime.execute("import ctypes\nlibc = ctypes.CDLL(None)\n_ = libc.printf(b'native-without-newline')")
    assert text(first, "stdout") == "native-without-newline"
    second = await runtime.execute("print('next-cell')")
    assert text(second, "stdout") == "next-cell\n"


async def test_input_and_subprocess_stdin_cannot_consume_commands(runtime):
    result = await runtime.execute("input('answer?')")
    assert end(result)["status"] == "error"
    assert "say(question); wait()" in text(result)
    result = await runtime.execute(
        "import subprocess, sys\nprint(repr(sys.stdin.read()))\np = subprocess.run(['/bin/cat'], capture_output=True)\nprint(repr(p.stdout))"
    )
    assert text(result) == "''\nb''\n"
    assert end(result)["status"] == "success"


async def test_broker_requests_and_results(runtime):
    requests = []

    def broker(frame):
        requests.append(frame)
        return {"result": {"method": frame["method"], "original": "saved value"}}

    result = await runtime.execute(
        "print(history.recent(3))\nprint(history.search('assertion', kind='error', limit=2))\nprint(history.read('a1:c0042', offset=4, limit=100))",
        broker=broker,
    )
    assert [frame["method"] for frame in requests] == ["recent", "search", "read"]
    assert len({frame["request_id"] for frame in requests}) == 3
    assert requests[2]["args"] == {"event_or_cell_id": "a1:c0042", "offset": 4, "limit": 100}
    assert "saved value" in text(result)
    assert end(result)["status"] == "success"
    failure = await runtime.execute("history.read('a1:missing')", broker=lambda _: {"error": "unknown evidence"})
    assert end(failure)["status"] == "error"
    assert "unknown evidence" in text(failure)


async def test_brokered_write_approval_and_capability_operations(runtime):
    requests = []

    def broker(frame):
        requests.append(frame)
        if frame["method"] == "rw_request":
            return {"result": "cap-123"}
        return {"result": None}

    result = await runtime.execute(
        "fs = ask_rw_approval('/outside/project', operations=('create', 'modify', 'rename', 'delete'), reason='migration')\n"
        "fs.write_text('a.txt', 'hello')\n"
        "fs.mkdir('generated')\n"
        "fs.rename('a.txt', 'b.txt')\n"
        "fs.remove('b.txt')",
        broker=broker,
    )
    assert [request["method"] for request in requests] == [
        "rw_request",
        "rw_write_text",
        "rw_mkdir",
        "rw_rename",
        "rw_remove",
    ]
    assert requests[0]["args"] == {
        "path": "/outside/project",
        "recursive": True,
        "operations": ["create", "modify", "rename", "delete"],
        "reason": "migration",
    }
    assert requests[1]["args"] == {
        "capability_id": "cap-123",
        "path": "a.txt",
        "content": "hello",
    }
    assert end(result)["status"] == "success"

    denied = await runtime.execute(
        "ask_rw_approval('/denied')",
        broker=lambda _: {"result": None},
    )
    assert end(denied)["status"] == "error"
    assert "Write permission denied" in text(denied)


async def test_rw_helper_is_not_reported_as_user_kernel_state(runtime):
    result = await runtime.execute("print(callable(ask_rw_approval))")
    assert text(result) == "True\n"
    assert all(row["name"] != "ask_rw_approval" for row in end(result)["namespace_summary"]["variables"])


async def test_uncorrelated_broker_response_rejected(runtime):
    cell_id = await runtime.dispatch("history.recent()")
    frame = await runtime.recv()
    assert frame["type"] == "broker_request"
    await write_frame(
        runtime.process.stdin,
        {"v": 1, "type": "broker_response", "cell_id": cell_id, "request_id": "wrong", "result": []},
    )
    events = []
    while True:
        frame = await runtime.recv()
        events.append(frame)
        if frame["type"] == "cell_end":
            break
    assert end(events)["status"] == "error"
    assert "Uncorrelated broker response" in text(events)


async def test_duplicate_dispatch_fails_without_replay(runtime):
    first = await runtime.execute("from pathlib import Path\nPath('once').write_text('1')")
    cell_id = end(first)["cell_id"]
    await runtime.dispatch("Path('once').write_text('2')", cell_id=cell_id)
    with pytest.raises(EOFError):
        await runtime.recv()
    assert await asyncio.wait_for(runtime.process.wait(), 5) == 70
    # Source is never replayed; inspect fixed fixture artifact outside worker.
    assert (runtime.workspace / "once").read_text() == "1"


async def test_render_once_text_only_and_ipython_default_expression_cache(runtime):
    result = await runtime.execute(
        "class Counted:\n    calls = 0\n    def __repr__(self):\n        Counted.calls += 1\n        return 'rendered-value'\n    def _repr_html_(self):\n        raise AssertionError('rich formatting must be off')\nobj = Counted()\nobj"
    )
    assert text(result, "display") == "rendered-value"
    assert (
        text(
            await runtime.execute(
                "print(Counted.calls, get_ipython().cache_size == type(get_ipython()).cache_size.default_value)"
            )
        )
        == "1 True\n"
    )
    assert end(result)["status"] == "success"


async def test_markdown_display_preserves_markdown_mime(runtime):
    result = await runtime.execute(
        "from IPython.display import Markdown, display\ndisplay(Markdown('| A | B |\\n|---|---|\\n| 1 | 2 |'))"
    )
    markdown = [frame for frame in result if frame.get("stream") == "markdown"]
    assert "".join(frame["text"] for frame in markdown) == "| A | B |\n|---|---|\n| 1 | 2 |"
    assert end(result)["status"] == "success"


async def test_display_function_and_async_cell(runtime):
    result = await runtime.execute(
        "from IPython.display import display\ndisplay({'key': 'value'})\nimport asyncio\nawait asyncio.sleep(0)\n42"
    )
    assert "{'key': 'value'}" in text(result, "display")
    assert "42" in text(result, "display")
    assert end(result)["status"] == "success"


@pytest.mark.parametrize("source", ["%debug", "%pdb on", "breakpoint()", "import pdb; pdb.set_trace()"])
async def test_debuggers_fail_clearly(runtime, source):
    result = await runtime.execute(source)
    assert end(result)["status"] == "error"
    assert "Interactive debugging is unavailable" in text(result)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("len?", "number of items"),
        ("str.upper??", "uppercase"),
        ("values = [len]\nvalues[0]?", "number of items"),
        ("%pwd?", "working directory"),
        ("?len", "number of items"),
    ],
)
async def test_pager_introspection_is_noninteractive(runtime, source, expected):
    result = await runtime.execute(source)
    assert end(result)["status"] == "success"
    assert expected in text(result)


@pytest.mark.parametrize(
    "source",
    [
        "Hi! How can I help?",
        "What would you like help with?",
        "This is prose before len??",
        "if True:\n    What would you like help with?",
    ],
)
async def test_question_prose_is_not_successful_ipython_help(runtime, source):
    result = await runtime.execute("effect = 'must not execute'\n" + source)
    assert end(result)["status"] == "error"
    assert "SyntaxError" in text(result)
    assert "Object `with` not found" not in text(result)
    assert "Signature:" not in text(result)
    assert text(await runtime.execute("print('effect' in globals())")) == "False\n"


async def test_questions_inside_python_strings_are_preserved(runtime):
    result = await runtime.execute('say("Hi! How can I help?", final=True)')
    assert end(result)["status"] == "success"
    assert [e["content"] for e in result if e["type"] == "say"] == ["Hi! How can I help?"]


async def test_background_subprocess_output_retains_origin(runtime):
    first = await runtime.execute(
        "import subprocess\np = subprocess.Popen(['/bin/sh', '-c', 'sleep 0.3; printf late-from-first'])"
    )
    first_id = end(first)["cell_id"]
    second = await runtime.execute("import time\ntime.sleep(0.6)\nprint('second')")
    second_id = end(second)["cell_id"]
    late = [event for event in runtime.events if event.get("type") == "output" and "late-from-first" in event["text"]]
    assert late
    assert all(event["cell_id"] == first_id for event in late)
    assert first_id != second_id
    assert text(second) == "second\n"


def spool_path(events):
    matches = re.findall(r"[Ss]aved to (/[^\n]*?/py-output-[\w-]+\.txt)", text(events))
    assert matches, text(events)
    return Path(matches[-1])  # Provisional paths expire; use the latest notice.


async def test_worker_spools_are_test_local_without_patching_stdlib(runtime):
    checks = await runtime.execute(
        "import py_agent.output as output_module\nimport tempfile\n"
        "print(output_module.tempfile is tempfile)\n"
        "print(tempfile.mkstemp.__module__)\n"
        "print(hasattr(tempfile, 'TemporaryDirectory'))"
    )
    assert text(checks) == "False\ntempfile\nTrue\n"
    first = spool_path(await runtime.execute("print('isolated' * 1100, end='')"))
    second = spool_path(await runtime.execute("print('isolated' * 1100, end='')"))
    assert first == second
    assert first.parent == runtime.workspace / "output-artifacts"
    assert list(first.parent.iterdir()) == [first]
    assert first.read_text() == "isolated" * 1100


async def test_invalid_utf8_and_large_display_are_spooled(runtime):
    result = await runtime.execute("import os\nos.write(1, b'\\xffnative\\n')\n'x' * 20000")
    assert "size=20010 chars, lines=2" in text(result)
    assert spool_path(result).read_text(encoding="utf-8") == "�native\n'" + "x" * 20000 + "'"
    assert spool_path(result).parent == runtime.workspace / "output-artifacts"
    assert len(text(result)) < 2000  # includes the potentially long pytest path
    assert end(result)["status"] == "success"


def test_capture_hot_pipe_does_not_starve_control_barriers(monkeypatch):
    from types import SimpleNamespace

    from py_agent import worker

    reads = []

    def read(fd, size):
        reads.append(fd)
        if len(reads) > 1000:
            raise BlockingIOError
        return b"x" * size

    monkeypatch.setattr(worker, "os", SimpleNamespace(**(vars(os) | {"read": read})))
    capture = worker.Capture.__new__(worker.Capture)
    capture.decoders = {123: SimpleNamespace(decode=lambda data, final: data.decode())}
    capture.output = SimpleNamespace(append=lambda stream, value: None)
    capture._drain(SimpleNamespace(fd=123, data="stdout"))
    assert len(reads) == 64  # return to selector/control even though the pipe stays hot


async def test_output_flood_can_be_stopped_by_host(runtime):
    # Announce the artifact before the infinite flood, then interrupt without
    # expecting an endless series of output frames (those now stay in the file).
    await runtime.dispatch(
        "import os\n_ = os.write(1, b'x' * 9000)\nsay('flood starting')\nwhile True:\n    os.write(1, b'x' * 4096)"
    )
    frame = await runtime.recv()
    assert frame["type"] == "output"
    assert "Output too long" in frame["text"]
    assert (await runtime.recv())["type"] == "say"
    await asyncio.sleep(0.05)
    await runtime.stop()
    assert runtime.process.returncode is not None


@pytest.mark.parametrize("size", [8000, 8001])
async def test_worker_character_limit_includes_unicode_without_print_newline(runtime, size):
    result = await runtime.execute(f"import os\n_ = os.write(1, ('雪' * {size}).encode('utf-8'))")
    if size == 8000:
        assert text(result) == "雪" * 8000
    else:
        assert f"size={size} chars, lines=1" in text(result)
        assert spool_path(result).read_text(encoding="utf-8") == "雪" * size
    assert end(result)["status"] == "success"


async def test_megabyte_subprocess_output_keeps_kernel_and_supports_chunk_reads(runtime):
    result = await runtime.execute(
        "import subprocess, sys\nfrom pathlib import Path\ncompleted_once = 0\nsubprocess.run([sys.executable, '-c', \"import os; [os.write(1, b'x' * 4096) for _ in range(512)]\"], check=True)\ncompleted_once += 1"
    )
    path = spool_path(result)
    assert path.stat().st_size == 2 * 1024 * 1024
    assert path.stat().st_mode & 0o777 == 0o600
    assert f"size={2 * 1024 * 1024} chars, lines=1" in text(result)
    assert len(result) == 2
    assert end(result)["status"] == "success"
    chunk = await runtime.execute(
        f"with Path({str(path)!r}).open(encoding='utf-8') as saved:\n    saved.seek(10000)\n    print(saved.read(4000))\nprint(completed_once)"
    )
    assert text(chunk) == "x" * 4000 + "\n1\n"
    assert end(chunk)["status"] == "success"
    blocked_again = await runtime.execute(f"print(Path({str(path)!r}).read_text())")
    assert spool_path(blocked_again).stat().st_size == path.stat().st_size + 1
    assert len(text(blocked_again)) < 2000
    assert text(await runtime.execute("print(completed_once)")) == "1\n"


async def test_stdout_stderr_and_display_share_one_cell_budget(runtime):
    result = await runtime.execute("import os\n_ = os.write(1, b'a' * 3000)\n_ = os.write(2, b'b' * 3000)\n'c' * 3000")
    saved = spool_path(result).read_text()
    assert len(saved) == 9002
    assert saved.count("a") == saved.count("b") == saved.count("c") == 3000
    assert saved.endswith("'" + "c" * 3000 + "'")
    assert end(result)["status"] == "success"


async def test_spool_creation_failure_does_not_kill_kernel(runtime):
    result = await runtime.execute(
        "import py_agent.output as output_module\nfrom types import SimpleNamespace\noriginal_tempfile = output_module.tempfile\ndef fail_spool(**kwargs):\n    raise OSError(28, 'synthetic disk full')\noutput_module.tempfile = SimpleNamespace(mkstemp=fail_spool)\nprint('x' * 9000)"
    )
    assert "Full output NOT saved: file creation failed" in text(result)
    assert "size=9001 chars, lines=1" in text(result)
    assert end(result)["status"] == "success"
    assert (
        text(await runtime.execute("output_module.tempfile = original_tempfile\nprint('still alive')"))
        == "still alive\n"
    )


async def test_inherited_file_limit_reports_partial_output_without_losing_kernel(runtime):
    # Model an inherited file limit only for spooling; real-OS behavior is covered
    # independently without constraining IPython's own SQLite history file.
    await runtime.execute(
        "import py_agent.output as output_module\nfrom types import SimpleNamespace\noutput_module.resource = SimpleNamespace(RLIMIT_FSIZE=1, RLIM_INFINITY=-1, getrlimit=lambda _: (32, 32))"
    )
    result = await runtime.execute("print('z' * 9000)")
    assert "Full output NOT saved: inherited OS file size limit 32 reached" in text(result)
    assert spool_path(result).read_text() == "z" * 32
    assert "size=9001 chars, lines=1" in text(result)
    assert end(result)["status"] == "success"
    assert text(await runtime.execute("print('still alive')")) == "still alive\n"


async def test_large_traceback_spools_without_losing_cell_error_or_state(runtime):
    result = await runtime.execute("kept_after_failure = 42\nraise ValueError('z' * 12000)")
    assert end(result)["status"] == "error"
    assert "ValueError" in spool_path(result).read_text()
    assert len(text(result)) < 2000
    assert text(await runtime.execute("print(kept_after_failure)")) == "42\n"


async def test_large_display_is_rendered_once(runtime):
    result = await runtime.execute(
        "class Large:\n    calls = 0\n    def __repr__(self):\n        Large.calls += 1\n        return 'r' * 10000\nlarge = Large()\nlarge"
    )
    assert spool_path(result).read_text() == "r" * 10000
    assert text(await runtime.execute("print(Large.calls)")) == "1\n"


async def test_late_subprocess_overflow_keeps_old_cell_origin(runtime):
    first = await runtime.execute(
        "import subprocess, sys\np = subprocess.Popen([sys.executable, '-c', \"import time,os; time.sleep(.2); os.write(1, b'l' * 20000)\"])\nprint('early')"
    )
    first_id = end(first)["cell_id"]
    assert text(first) == "early\n"
    second = await runtime.execute("import time\ntime.sleep(.6)\nprint('second')")
    notices = [e for e in runtime.events if e.get("type") == "output" and e["text"].startswith("Output too long")]
    assert notices
    assert all(e["cell_id"] == first_id for e in notices)
    assert len(notices) <= 2
    assert "size=20006 chars, lines=2" in notices[-1]["text"]
    assert "counts so far" not in notices[-1]["text"]
    assert spool_path(notices).read_text() == "early\n" + "l" * 20000
    if len(notices) > 1:
        assert "This path is provisional" in notices[0]["text"]
        assert not spool_path(notices[:1]).exists()
    assert text(second) == "second\n"


async def test_infinite_loop_host_interrupt_and_worker_exit(runtime):
    await runtime.dispatch("while True:\n    pass")
    with pytest.raises(asyncio.TimeoutError):
        await runtime.recv(timeout=0.2)
    await runtime.stop()
    assert runtime.process.returncode == -signal.SIGKILL


async def test_explicit_worker_crash_never_claims_success(runtime):
    await runtime.dispatch("import os\nos._exit(7)")
    with pytest.raises(EOFError):
        await runtime.recv()
    assert await runtime.process.wait() == 7


async def test_unrelated_same_uid_threads_do_not_prevent_capture(runtime):
    # Linux RLIMIT_NPROC counts these unrelated HOST threads against the worker.
    # The old fixed limit of 64 allowed ready, then killed the first cell with 70.
    stop = threading.Event()
    threads = []
    try:
        for _ in range(80):
            thread = threading.Thread(target=stop.wait, daemon=True)
            try:
                thread.start()
            except RuntimeError as exc:
                if str(exc) != "can't start new thread":
                    raise
                pytest.skip(
                    f"Host cannot create 80 stress threads (started {len(threads)}); "
                    f"inherited RLIMIT_AS={resource.getrlimit(resource.RLIMIT_AS)}, "
                    f"RLIMIT_NPROC={resource.getrlimit(resource.RLIMIT_NPROC)}"
                )
            threads.append(thread)
        result = await runtime.execute("print('capture still works')")
        assert end(result)["status"] == "success"
        assert text(result) == "capture still works\n"
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)


async def test_uid_process_limit_is_inherited_not_replaced(runtime):
    expected = resource.getrlimit(resource.RLIMIT_NPROC)
    result = await runtime.execute("import resource\nprint(resource.getrlimit(resource.RLIMIT_NPROC))")
    assert text(result) == f"{expected}\n"


async def test_capture_start_failure_reports_original_fatal_diagnostic(runtime):
    # Force only the next cell's capture thread to fail, not this active capture.
    result = await runtime.execute(
        'import threading\ndef no_threads(*args, **kwargs):\n    raise RuntimeError("can\'t start new thread")\nthreading.Thread.start = no_threads'
    )
    assert end(result)["status"] == "success"
    await runtime.dispatch("print('must not execute')")
    with pytest.raises(EOFError, match="can't start new thread") as failure:
        await runtime.recv()
    assert "RLIMIT_NPROC" in str(failure.value)
    assert runtime.process.returncode == 70


async def test_cleanup_tolerates_process_exiting_before_killpg(monkeypatch):
    class Exited:
        returncode = None  # asyncio has not yet reaped the already-exited child
        pid = 123

        async def wait(self):
            self.returncode = 70
            return 70

    def gone(*args):
        raise ProcessLookupError

    process = Exited()
    monkeypatch.setattr(os, "killpg", gone)
    await Runtime(process).stop()
    assert process.returncode == 70


@pytest.mark.parametrize("structured", [False, True])
async def test_large_say_spools_to_final_hash_path_and_keeps_typed_small_replies(runtime, structured):
    expression = "{'value': '雪' * 10000}" if structured else "'雪' * 10000"
    result = await runtime.execute(f"say({expression}, final=True)")
    replies = [event for event in result if event["type"] == "say"]
    assert len(replies) == 1
    assert replies[0]["final"] is True
    notice = replies[0]["content"]
    assert "Output too long to display here" in notice
    assert "provisional" not in notice
    path = Path(re.search(r"Saved to (.*?\.txt)\.", notice)[1])
    assert path.parent == runtime.workspace / "output-artifacts"
    expected = '{"value":"' + "雪" * 10000 + '"}' if structured else "雪" * 10000
    assert path.read_text(encoding="utf-8") == expected
    assert len(path.stem.removeprefix("py-output-")) == 16
    assert end(result)["status"] == "success"
    again = await runtime.execute("say({'answer': 42}, final=True)")
    assert [event["content"] for event in again if event["type"] == "say"] == [{"answer": 42}]
    assert end(again)["status"] == "success"


async def test_large_say_final_still_reports_later_cell_failure(runtime):
    result = await runtime.execute("say('x' * 20000, final=True)\nraise ValueError('later failure')")
    reply = next(event for event in result if event["type"] == "say")
    assert reply["final"] is True
    assert "Output too long" in reply["content"]
    assert end(result)["status"] == "error"  # supervisor must discard the staged final


async def test_invalid_say_control_does_not_create_artifacts(runtime):
    result = await runtime.execute("say('x' * 20000, final=1)")
    assert end(result)["status"] == "error"
    assert not any(event["type"] == "say" for event in result)
    assert not list((runtime.workspace / "output-artifacts").iterdir())


async def test_ordinary_file_can_exceed_old_64mib_ceiling(runtime):
    size = 64 * 1024 * 1024 + 1
    soft, _ = resource.getrlimit(resource.RLIMIT_FSIZE)
    if soft != resource.RLIM_INFINITY and soft < size:
        pytest.skip("Inherited OS file limit prevents >64MiB regression")
    result = await runtime.execute(
        f"from pathlib import Path\nwith open('large-file', 'wb') as f:\n    _ = f.truncate({size})\nprint(Path('large-file').stat().st_size)"
    )
    assert text(result) == f"{size}\n"
    assert end(result)["status"] == "success"


def test_runner_has_no_old_cell_or_active_capture_quota(monkeypatch):
    from types import SimpleNamespace

    from py_agent import worker

    class Capture:
        def __init__(self, *args):
            self.finished = threading.Event()

        def detach(self):
            pass

    monkeypatch.setattr(worker, "Capture", Capture)
    frames = []
    runner = worker.Runner.__new__(worker.Runner)
    runner.transport = SimpleNamespace(send=frames.append)
    runner.capture = None
    runner.captures = [Capture() for _ in range(17)]
    runner.dispatched = {f"a1:prior{index}" for index in range(10000)}
    runner.bridge = worker.Bridge(frames.append, lambda: None)
    runner.shell = SimpleNamespace(
        execution_count=10001,
        user_ns={"memories": []},
        transform_cell=lambda source: source,
        run_cell=lambda *args, **kwargs: SimpleNamespace(error_before_exec=None, error_in_exec=None),
    )
    frame = {"v": 1, "type": "execute", "cell_id": "a1:next", "source": "pass"}
    runner.execute(frame)
    assert len(runner.dispatched) == 10001
    assert len(runner.captures) == 18
    assert frames[-1]["status"] == "success"
    with pytest.raises(worker.ProtocolError, match="Duplicate cell dispatch"):
        runner.execute(frame)


def test_capture_barrier_waits_without_an_execution_deadline(monkeypatch):
    from collections import deque
    from types import SimpleNamespace

    from py_agent import worker

    class Marker:
        def wait(self, timeout=None):
            assert timeout is None
            return True

    capture = worker.Capture.__new__(worker.Capture)
    capture.guard = threading.Lock()
    capture.finished = threading.Event()
    capture.pending = deque()
    capture.failure = None
    capture.wake_write = 123
    monkeypatch.setattr(worker, "threading", SimpleNamespace(Event=Marker))
    monkeypatch.setattr(worker, "os", SimpleNamespace(write=lambda fd, data: 1))
    capture.barrier()
    assert len(capture.pending) == 1


async def test_no_ambient_startup_and_all_os_resource_limits_are_inherited(runtime):
    names = sorted(name for name in dir(resource) if name.startswith("RLIMIT_"))
    expected = {name: resource.getrlimit(getattr(resource, name)) for name in names}
    result = await runtime.execute(
        "import resource, os\nprint(os.environ.get('PYTHONPATH'))\nprint({name: resource.getrlimit(getattr(resource, name)) for name in sorted(dir(resource)) if name.startswith('RLIMIT_')})"
    )
    assert text(result) == f"None\n{expected}\n"
