"""Persistent IPython subprocess. Production launch is exclusively via Sandbox.

This entry point intentionally has no sandbox bypass switch. The supervisor
owns interruption and validates transport. OS resource limits are inherited,
never replaced with application-imposed execution or session quotas.
"""

from __future__ import annotations

from pathlib import Path

# -I excludes the script's directory and ambient Python paths. This one pinned
# runtime parent is protected by Sandbox; never add the workspace or load config.
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ast
import builtins
import codecs
from collections import deque
import ctypes
import io
import itertools
import json
import os
import resource
import selectors
import threading
import traceback

from py_agent.bridge import Bridge, CellYield, no_input
from py_agent.output import CellOutput
from py_agent.protocol import (
    HOST_TYPES,
    MAX_NAMESPACE_BYTES,
    MAX_NAMESPACE_NAME_CHARS,
    MAX_NAMESPACE_ROWS,
    MAX_NAMESPACE_TYPE_CHARS,
    ProtocolError,
    read_frame_sync,
    write_frame_sync,
)

MAX_NAMESPACE_SCAN = 512
_NAMESPACE_HELPERS = frozenset({
    "say",
    "wait",
    "history",
    "ask_rw_approval",
    "memories",
    "In",
    "Out",
    "get_ipython",
    "exit",
    "quit",
    "open",
})
_TYPE_NAME = type.__dict__["__name__"]


def kernel_metadata(namespace):
    """Read only builtin metadata, not values, repr/len hooks or metaclass properties.

    A hostile/rebound namespace or a mutation during iteration is unknown. Even
    an enormous namespace has bounded scan, retained rows and serialized size.
    """
    unknown = {"memories_count": None, "namespace_summary": None}
    if type(namespace) is not dict:
        return unknown
    count = None
    summary = {"variables": [], "truncated": False}
    try:
        for index, (name, value) in enumerate(itertools.islice(dict.items(namespace), MAX_NAMESPACE_SCAN + 1)):
            if index == MAX_NAMESPACE_SCAN:
                summary["truncated"] = True
                break
            # Exact strings avoid calling custom key hashing/equality/formatting.
            if type(name) is not str:
                summary["truncated"] = True
                continue
            if name == "memories":
                count = list.__len__(value) if type(value) is list else None
            if not name or name.startswith("_") or name in _NAMESPACE_HELPERS:
                continue
            if len(summary["variables"]) >= MAX_NAMESPACE_ROWS:
                summary["truncated"] = True
                continue
            # Invoke type's builtin descriptor directly: type.__getattribute__
            # could still invoke a custom metaclass's __name__ property.
            type_name = _TYPE_NAME.__get__(type(value), type) or "<unnamed>"
            if len(name) > MAX_NAMESPACE_NAME_CHARS or len(type_name) > MAX_NAMESPACE_TYPE_CHARS:
                summary["truncated"] = True
            row = {"name": name[:MAX_NAMESPACE_NAME_CHARS], "type": type_name[:MAX_NAMESPACE_TYPE_CHARS]}
            summary["variables"].append(row)
            if len(json.dumps(summary, ensure_ascii=True, separators=(",", ":")).encode("ascii")) > MAX_NAMESPACE_BYTES:
                summary["variables"].pop()
                summary["truncated"] = True
        return {"memories_count": count, "namespace_summary": summary}
    except (RuntimeError, TypeError):
        return unknown  # e.g. unsupported concurrent namespace mutation


class Transport:
    def __init__(self):
        # Duplicates are close-on-exec: subprocesses get cell streams, not IPC.
        self.reader = os.fdopen(os.dup(0), "rb")
        self.writer = os.fdopen(os.dup(1), "wb")
        self.lock = threading.Lock()
        with Path(os.devnull).open("r+b", buffering=0) as null:
            for fd in (0, 1, 2):
                os.dup2(null.fileno(), fd)
        sys.stdin = io.TextIOWrapper(io.FileIO(0, "r", closefd=False), encoding="utf-8")
        sys.stdout = io.TextIOWrapper(
            io.FileIO(1, "w", closefd=False), encoding="utf-8", errors="backslashreplace", write_through=True
        )
        sys.stderr = io.TextIOWrapper(
            io.FileIO(2, "w", closefd=False), encoding="utf-8", errors="backslashreplace", write_through=True
        )
        sys.__stdin__, sys.__stdout__, sys.__stderr__ = sys.stdin, sys.stdout, sys.stderr
        # Native extensions may use libc stdio rather than os.write. Make the
        # standard C streams unbuffered so bytes cannot spill into a later
        # cell's reassigned descriptors. Linux libc exports both symbols.
        libc = ctypes.CDLL(None)
        libc.setvbuf.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
        libc.setvbuf.restype = ctypes.c_int
        for name in ("stdout", "stderr"):
            if libc.setvbuf(ctypes.c_void_p.in_dll(libc, name), None, 2, 0) != 0:
                raise RuntimeError("Cannot disable native standard-stream buffering")

    def receive(self):
        return read_frame_sync(self.reader, allowed_types=HOST_TYPES)

    def send(self, frame):
        with self.lock:
            write_frame_sync(self.writer, frame)


class Capture:
    """Cell-owned descriptor pipes; inherited subprocess output keeps its ID.

    A barrier drains available bytes before control/display events. A background
    subprocess retaining a pipe can outlive cell_end; its later events retain the
    original cell_id. Unmanaged Python threads doing raw os.write on fd 1/2 across
    cells cannot be reliably attributed and are outside the supported task model.
    """

    def __init__(self, cell_id, send):
        self.cell_id, self.send = cell_id, send
        self.output = CellOutput(cell_id, send)
        self.selector = selectors.DefaultSelector()
        self.decoders = {}
        self.pending = deque()
        self.guard = threading.Lock()
        self.finished = threading.Event()
        self.failure = None
        self.wake_read, self.wake_write = os.pipe()
        self.selector.register(self.wake_read, selectors.EVENT_READ, None)
        for number, stream in ((1, "stdout"), (2, "stderr")):
            read, write = os.pipe()
            os.set_blocking(read, False)
            self.selector.register(read, selectors.EVENT_READ, stream)
            self.decoders[read] = codecs.getincrementaldecoder("utf-8")("replace")
            os.dup2(write, number)
            os.close(write)
        self.thread = threading.Thread(target=self._pump, name=f"capture-{cell_id}", daemon=True)
        self.thread.start()

    def _drain(self, key):
        # A continuously writing descendant must not starve stderr, barriers or
        # cell completion. More than the pipe's buffered capacity is drained per
        # turn; still-readable streams are selected again on the next turn.
        for _ in range(64):
            try:
                data = os.read(key.fd, 4096)
            except BlockingIOError:
                return
            text = self.decoders[key.fd].decode(data, final=not data)
            if text:
                self.output.append(key.data, text)
            if not data:
                self.selector.unregister(key.fd)
                os.close(key.fd)
                del self.decoders[key.fd]
                if not self.decoders:
                    self.output.streams_closed()
                return

    def _pump(self):
        try:
            while self.decoders:
                for key, _ in self.selector.select():
                    if key.data is not None:
                        # Another selected entry may have been drained at a
                        # barrier earlier in this iteration.
                        if key.fd in self.decoders:
                            self._drain(key)
                    else:
                        os.read(self.wake_read, 4096)
                        for item in list(self.selector.get_map().values()):
                            if item.data is not None:
                                self._drain(item)
                        with self.guard:
                            while self.pending:
                                self.pending.popleft().set()
        except BaseException as exc:
            self.failure = exc
        finally:
            try:
                self.output.streams_closed()
            except BaseException as exc:
                self.failure = self.failure or exc
            with self.guard:
                self.finished.set()
                while self.pending:
                    self.pending.popleft().set()
            for key in list(self.selector.get_map().values()):
                os.close(key.fd)
            self.selector.close()
            # Synchronize wake writes against descriptor close/reuse.
            with self.guard:
                os.close(self.wake_write)

    def barrier(self):
        marker = threading.Event()
        with self.guard:
            if not self.finished.is_set():
                self.pending.append(marker)
                os.write(self.wake_write, b".")
            else:
                marker.set()
        # A long-running flush is not a failed cell. The supervisor can still
        # interrupt by terminating this worker and its sandbox process tree.
        marker.wait()
        if self.failure is not None:
            raise RuntimeError("Output transport failed") from self.failure

    def detach(self):
        sys.stdout.flush()
        sys.stderr.flush()
        with Path(os.devnull).open("wb", buffering=0) as null:
            os.dup2(null.fileno(), 1)
            os.dup2(null.fileno(), 2)
        self.barrier()
        self.output.finish_cell()


def _shell(bridge, emit_display):
    from IPython.core.inputtransformer2 import HelpEnd, _help_end_re
    from IPython.core.interactiveshell import InteractiveShell
    from IPython.core.profiledir import ProfileDir
    from traitlets.config import Config

    profile_root = Path(os.environ["IPYTHONDIR"])
    profile_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = ProfileDir.create_profile_dir(str(profile_root / "profile_py_agent"))
    config = Config()
    config.InteractiveShell.colors = "nocolor"
    config.InteractiveShell.separate_in = ""
    config.InteractiveShell.separate_out = ""
    config.InteractiveShell.separate_out2 = ""
    config.HistoryManager.hist_file = str(profile_root / "history.sqlite")
    config.HistoryManager.db_cache_size = 0
    shell = InteractiveShell.instance(
        config=config,
        ipython_dir=str(profile_root),
        profile_dir=profile,
        user_ns={
            "say": bridge.say,
            "wait": bridge.wait,
            "history": bridge.history,
            "ask_rw_approval": bridge.ask_rw_approval,
            "memories": [],
        },
    )
    # IPython's suffix-help transformer searches the END of a line and can
    # silently discard preceding prose: "Hi! How can I help?" becomes help?.
    # Require the entire help expression to match. Preserve explicit len?,
    # obj.attr??, x[0]? and %magic? rather than disabling IPython syntax.
    # This deliberately uses the grammar from our pinned IPython dependency.

    class StrictHelpEnd(HelpEnd):
        def transform(self, lines):
            piece = "".join(lines[self.start_line : self.q_line + 1])
            if _help_end_re.fullmatch(piece[self.start_col :].strip()) is None:
                raise SyntaxError(
                    "Ambiguous IPython help syntax: emit one raw code cell; use say(...) for user-facing text"
                )
            return super().transform(lines)

    transformers = shell.input_transformer_manager.token_transformers
    transformers[:] = [StrictHelpEnd if cls is HelpEnd else cls for cls in transformers]
    # No InteractiveShellApp/profile startup or extension loading occurs.
    shell.set_custom_exc((CellYield,), lambda *args, **kwargs: [])
    shell.display_formatter.active_types = ["text/plain", "text/markdown"]
    shell.displayhook.write_output_prompt = lambda: None

    def publish_display(data, **kwargs):
        if "text/markdown" in data:
            emit_display(data["text/markdown"], "markdown")
        else:
            emit_display(data.get("text/plain", ""), "display")

    shell.displayhook.write_format_data = lambda data, md_dict=None: publish_display(data)
    shell.display_pub.publish = publish_display

    def disabled_debugger(*args, **kwargs):
        raise RuntimeError("Interactive debugging is unavailable; inspect state in another cell")

    shell.debugger = disabled_debugger
    shell.show_in_pager = lambda data, *args, **kwargs: emit_display(
        data.get("text/plain", "") if isinstance(data, dict) else str(data)
    )
    shell.register_magic_function(disabled_debugger, "line", "debug")
    shell.register_magic_function(disabled_debugger, "line", "pdb")
    shell.ask_exit = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("Use say(..., final=True) or wait(); the supervisor owns process lifetime")
    )
    builtins.input = no_input
    builtins.breakpoint = disabled_debugger
    # pdb and IPython's debugger eventually ask for input; deny it explicitly
    # rather than repeatedly consuming EOF or waiting on terminal state.
    import pdb

    pdb.set_trace = disabled_debugger
    return shell


class Runner:
    def __init__(self, transport):
        self.transport = transport
        self.capture = None
        self.captures = []
        self.bridge = Bridge(self.send_control, transport.receive)
        self.shell = _shell(self.bridge, self.display)
        self.dispatched = set()

    def send_control(self, frame):
        if self.capture:
            self.capture.barrier()
            self.capture.output.flush()
        self.transport.send(frame)

    def display(self, text, stream="display"):
        if text and self.bridge.cell_id:
            # Render once. Drain prior native writes before appending the display
            # to the same cell-wide character budget and optional spill file.
            self.capture.barrier()
            self.capture.output.append(stream, text)

    def execute(self, frame):
        cell_id, source = frame["cell_id"], frame["source"]
        if cell_id in self.dispatched:
            raise ProtocolError("Duplicate cell dispatch rejected; never replay source")
        self.dispatched.add(cell_id)
        self.captures = [capture for capture in self.captures if not capture.finished.is_set()]
        self.bridge.begin(cell_id)
        capture = self.capture = Capture(cell_id, self.transport.send)
        self.captures.append(capture)
        status, error = "success", None
        count = self.shell.execution_count
        try:
            if source.lstrip().startswith("```"):
                raise SyntaxError("Emit one raw IPython cell, without Markdown fences")
            transformed = self.shell.transform_cell(source)
            # IPython transforms magics/shell escapes before Python syntax
            # checking. No extraction/repair of prose or partial source.
            compile(transformed, "<agent-cell-format-check>", "exec", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            self.shell.call_pdb = False
            result = self.shell.run_cell(source, store_history=True)
            failure = result.error_before_exec or result.error_in_exec
            if failure is not None and not isinstance(failure, CellYield):
                status = "error"
                error = f"{type(failure).__name__}: {str(failure)[:2000]}"
            elif self.bridge.wait_requested:
                status = "wait"
            if self.bridge.final_requested and self.bridge.wait_requested:
                status = "invalid_control"
                error = "say(final=True) and wait() cannot be combined; preceding effects remain"
        except BaseException as exc:
            status = "error"
            error = f"{type(exc).__name__}: {str(exc)[:2000]}"
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        finally:
            capture.detach()
            self.capture = None
            self.bridge.end()
        end = {
            "v": 1,
            "type": "cell_end",
            "cell_id": cell_id,
            "status": status,
            "execution_count": count,
            **kernel_metadata(self.shell.user_ns),
        }
        if error is not None:
            end["error"] = error
        self.transport.send(end)


def main() -> int:
    # Keep a bounded fatal-diagnostic path even after cell fd 2 is redirected.
    # This is still untrusted worker output, never authoritative control traffic.
    diagnostic_fd = os.dup(2)
    try:
        transport = Transport()
        runner = Runner(transport)
        transport.send({"v": 1, "type": "ready", "kernel_pid": os.getpid()})
        while True:
            try:
                frame = transport.receive()
            except EOFError:
                return 0
            if frame["type"] != "execute":
                raise ProtocolError("Unsolicited broker response")
            runner.execute(frame)
    except BaseException as exc:
        # Fatal/invalid transport has no well-defined correlated cell to end.
        # Do not hide infrastructure failures behind a generic transport EOF.
        try:
            message = f"py worker fatal: {type(exc).__name__}: {str(exc)[:2000]}"
            if isinstance(exc, RuntimeError) and "start new thread" in str(exc):
                message += (
                    "; unable to start an output/runtime thread. Check inherited "
                    "RLIMIT_NPROC, cgroup pids.max and memory limits; "
                    f"RLIMIT_NPROC={resource.getrlimit(resource.RLIMIT_NPROC)}"
                )
            os.write(diagnostic_fd, (message + "\n").encode("utf-8", "backslashreplace")[:4096])
        except BaseException:
            pass  # The process still exits even if the diagnostic pipe is gone.
        return 70
    finally:
        os.close(diagnostic_fd)


if __name__ == "__main__":
    # Bypass IPython atexit hooks and abandoned background Python threads. The
    # sandbox supervisor owns process-tree cleanup, including on normal EOF.
    os._exit(main())
