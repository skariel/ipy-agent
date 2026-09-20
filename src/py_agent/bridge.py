"""Worker conveniences, never a security or authority boundary.

The trusted supervisor validates/correlates every frame independently. A worker
can redefine these functions or forge its own frames; that grants no host power.
"""
from __future__ import annotations

import threading
from typing import Callable

from py_agent.protocol import ProtocolError, encode_frame


class CellYield(BaseException):
    """End a cell without an error; deliberately not an Exception subtype."""


class BrokerError(RuntimeError):
    pass


class History:
    def __init__(self, bridge: Bridge):
        self._bridge = bridge

    def recent(self, n=10):
        return self._bridge.request("recent", {"n": n})

    def search(self, query, *, kind=None, limit=20):
        return self._bridge.request("search", {"query": query, "kind": kind, "limit": limit})

    def read(self, event_or_cell_id, *, offset=0, limit=8000):
        return self._bridge.request("read", {"event_or_cell_id": event_or_cell_id, "offset": offset, "limit": limit})


class Bridge:
    def __init__(self, send: Callable[[dict], None], receive: Callable[[], dict]):
        self._send = send
        self._receive = receive
        self._thread = threading.get_ident()
        self._counter = 0
        self.cell_id: str | None = None
        self.final_requested = False
        self.wait_requested = False
        self.history = History(self)

    def begin(self, cell_id: str) -> None:
        self.cell_id = cell_id
        self.final_requested = self.wait_requested = False

    def end(self) -> None:
        self.cell_id = None

    def _active(self) -> str:
        if threading.get_ident() != self._thread or self.cell_id is None:
            raise RuntimeError("say/wait/history are only supported in the active cell's main thread")
        return self.cell_id

    def say(self, content, *, final=False) -> None:
        frame = {"v": 1, "type": "say", "cell_id": self._active(), "content": content, "final": final}
        # Validate before marking control intent, so a caught invalid say does
        # not accidentally request completion. The host stages final content.
        encode_frame(frame)
        self._send(frame)
        self.final_requested |= final

    def wait(self) -> None:
        self._active()
        self.wait_requested = True
        raise CellYield()

    def request(self, method: str, args: dict):
        cell_id = self._active()
        self._counter += 1
        request_id = f"r{self._counter}"
        self._send({"v": 1, "type": "broker_request", "cell_id": cell_id,
                    "request_id": request_id, "method": method, "args": args})
        frame = self._receive()
        if (frame["type"] != "broker_response" or frame["cell_id"] != cell_id
                or frame["request_id"] != request_id):
            raise ProtocolError("Uncorrelated broker response")
        if "error" in frame:
            raise BrokerError(frame["error"])
        return frame["result"]


def no_input(*args, **kwargs):
    raise RuntimeError("input() is unavailable; use say(question); wait(). The next user message starts a new cell.")
