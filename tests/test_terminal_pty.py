"""Real terminal-descriptor exit tests with a fixed stub, no model/worker code."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux PTY/signal integration")

SCRIPT = r"""
import asyncio, sys
from pathlib import Path
from py_agent.cli import _run_bounded
from py_agent.terminal import Terminal
class Stub:
    on_event = None
    submissions = 0
    def submit(self, text):
        self.submissions += 1
        return f"a1:e{self.submissions}"
    def status(self):
        return {"model": "test", "state": sys.argv[1], "queued": 0}
    async def interrupt(self):
        await asyncio.Event().wait()  # stays stuck until exit cancels it
    async def close(self):
        Path(sys.argv[2]).write_text("closed")
async def main():
    terminal = Terminal(Stub(), multiline=sys.argv[3] == "multiline")
    async def fragments():
        while not terminal.session.app.is_running:
            await asyncio.sleep(0)
        terminal.on_event({"kind": "source", "cell_id": "a1:c0001", "content": "hidden_generated_code()"})
        for text in ("f", " ", "file.py", "\n"):
            terminal.on_event({"kind": "output", "cell_id": "a1:c0001", "stream": "stdout", "content": text})
            await asyncio.sleep(0.01)
        terminal.on_event({"kind": "cell_end", "cell_id": "a1:c0001", "content": {"status": "success"}})
    emitter = asyncio.create_task(fragments()) if sys.argv[3] == "fragments" else None
    try:
        await terminal.run()
    finally:
        if emitter:
            emitter.cancel()
            await asyncio.gather(emitter, return_exceptions=True)
try:
    _run_bounded(main())
except asyncio.CancelledError:
    sys.exit(143)
"""


@pytest.mark.parametrize(
    "state,gesture,mode",
    [
        ("IDLE", b"\x03", "normal"),
        ("FAILED", b"\x03", "normal"),
        ("EXECUTING", b"\x03\x03", "normal"),
        ("GENERATING", b"\x04", "normal"),
        ("EXECUTING", b"/quit\r", "multiline"),
        ("EXECUTING", None, "normal"),  # SIGTERM, not a terminal key
        ("IDLE", b"\x04", "fragments"),  # arbitrary pipe chunks render as one line
        ("IDLE", b"/quit\r", "numbering"),  # messages advance prompts; commands/blank do not
    ],
)
def test_real_tty_exit_restores_shell_and_calls_cleanup(tmp_path, state, gesture, mode):
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    before = termios.tcgetattr(slave)
    marker = tmp_path / "closed"
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", SCRIPT, state, str(marker), mode],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TERM": "xterm-256color"},
        )
        data = bytearray()
        deadline = time.monotonic() + 5
        # Printing temporarily enters cooked mode. Wait for the prompt redraw
        # after the output before sending a raw-mode exit key.

        def ready():
            if mode != "fragments":
                return b"In [1]:" in data
            position = data.find(b"f file.py")
            return position >= 0 and b"In [1]:" in data[position + len(b"f file.py") :]

        while not ready():
            remaining = deadline - time.monotonic()
            assert remaining > 0, repr(bytes(data))
            if select.select([master], [], [], remaining)[0]:
                part = os.read(master, 8192)
                data.extend(part)
                if b"\x1b[6n" in part:
                    os.write(master, b"\x1b[1;1R")
            assert process.poll() is None, repr(bytes(data))
        if mode == "fragments":
            assert bytes(data).count(b"stdout [1]:") == 1
            assert b"hidden_generated_code" not in data
        if mode == "numbering":
            os.write(master, b"\r/help\rfirst request\r/trace\rsecond request\r")
            deadline = time.monotonic() + 5
            while b"In [3]:" not in data:
                remaining = deadline - time.monotonic()
                assert remaining > 0, repr(bytes(data))
                if select.select([master], [], [], remaining)[0]:
                    part = os.read(master, 8192)
                    data.extend(part)
                    if b"\x1b[6n" in part:
                        os.write(master, b"\x1b[1;1R")
                assert process.poll() is None, repr(bytes(data))
            assert b"In [2]:" in data
            assert b"In [4]:" not in data
        if gesture is None:
            process.send_signal(signal.SIGTERM)
        else:
            os.write(master, gesture)
        result = process.wait(timeout=5)
        assert result == (143 if gesture is None else 0), repr(bytes(data))
        assert marker.read_text() == "closed"
        assert termios.tcgetattr(slave) == before
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        os.close(master)
        os.close(slave)
