"""Bounded worker-side text output, with oversized cells saved as UTF-8 files.

These files are ordinary, mutable worker artifacts, not trusted host evidence.
Only bounded notices cross IPC. No host component opens a supplied pathname.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import stat
import tempfile
import threading

OUTPUT_CHAR_LIMIT = 8000


class CellOutput:
    """One cell's stdout/stderr/displays, in their observed capture order.

    Retain at most ``limit`` text characters, including already-delivered prefixes.
    On overflow, stream that prefix and subsequent text to one private /tmp file.
    Count LF-delimited lines, including a nonempty final unterminated line.
    A late subprocess may append after cell completion; report its final counts at
    pipe EOF. Both cell completion and pipe EOF are needed to close the artifact.
    """

    def __init__(self, cell_id, send, *, limit=OUTPUT_CHAR_LIMIT):
        self.cell_id, self.send = cell_id, send
        self.limit = limit
        self._lock = threading.RLock()
        self._runs = []  # [stream, text, delivered character offset]
        self.chars = self.newlines = self.saved_bytes = 0
        self._ends_newline = False
        self.path = None
        self._fd = None
        self._spilled = False
        self._storage_error = None
        self._cell_finished = self._streams_closed = False
        self._reported = None
        self._sha256 = hashlib.sha256()
        self._retained_duplicate = None

    @property
    def lines(self):
        return self.newlines + bool(self.chars and not self._ends_newline)

    def _fail_storage(self, reason):
        if self._storage_error is None:
            self._storage_error = reason
        self._close_file()

    def _close_file(self):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                os.close(fd)
            except OSError as exc:
                if self._storage_error is None:
                    self._storage_error = f"close failed ({type(exc).__name__}, errno={exc.errno})"

    @staticmethod
    def _same_file(actual, expected):
        return stat.S_ISREG(actual.st_mode) and (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns) == (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
        )

    @staticmethod
    def _unlink_same(path, expected):
        # Best effort: mutable worker paths are not an integrity/security boundary.
        # Do not knowingly delete a replacement created by another writer.
        try:
            actual = os.lstat(path)
            if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                os.unlink(path)
                return True
        except FileNotFoundError:
            return True
        except OSError:
            pass
        return False

    def _verified_existing(self, path):
        """Return a stable, private, matching artifact; a hash filename is not proof."""
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size != self.saved_bytes
            ):
                return None
            digest = hashlib.sha256()
            remaining = self.saved_bytes
            while remaining:
                block = os.read(fd, min(65536, remaining))
                if not block:
                    return None
                digest.update(block)
                remaining -= len(block)
            if os.read(fd, 1) or digest.digest() != self._sha256.digest():
                return None
            after = os.fstat(fd)
            published = os.lstat(path)
            # Include ctime, ownership and permissions: content/metadata can
            # change during hashing, and the pathname may be swapped altogether.
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_uid", "st_mode")
            signature = lambda info: tuple(getattr(info, field) for field in fields)
            if signature(before) != signature(after) or signature(after) != signature(published):
                return None
            return after
        except OSError:
            return None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass  # Verification must never turn an optional reuse into a crash.

    def _finish_file(self):
        if self._fd is None:
            return  # Completion/publication is idempotent.
        try:
            captured = os.fstat(self._fd)
        except OSError as exc:
            self._fail_storage(f"file status failed ({type(exc).__name__}, errno={exc.errno})")
            return
        self._close_file()
        if self._storage_error is not None:
            return  # Incomplete artifacts keep their provisional filenames.
        if captured.st_uid != os.getuid() or stat.S_IMODE(captured.st_mode) != 0o600:
            return  # Do not publish a nonprivate artifact under a canonical name.
        original = self.path
        try:
            if captured.st_size != self.saved_bytes or not self._same_file(os.lstat(original), captured):
                raise FileNotFoundError("spool pathname changed")
        except OSError:
            self.path = None
            self._storage_error = "spool pathname changed or became unavailable before publication"
            return
        # Canonical names deduplicate completed contents. link is no-clobber,
        # including when an existing destination is a symlink or corrupt artifact.
        directory = os.path.dirname(original)
        named = os.path.join(directory, f"py-output-{self._sha256.hexdigest()[:16]}.txt")
        try:
            os.link(original, named, follow_symlinks=False)
        except FileExistsError:
            if self._verified_existing(named) is None:
                return  # Do not trust, overwrite or reuse an unsafe collision.
            self.path = named
            if not self._unlink_same(original, captured):
                self._retained_duplicate = original
            return
        except OSError:
            return  # Naming is optional; the original file remains readable.
        try:
            linked = os.lstat(named)
            if not self._same_file(linked, captured):
                # The source was replaced between its stat and link. Never report
                # another inode as the hashed captured data.
                self._unlink_same(named, linked)
                self.path = None
                self._storage_error = "spool pathname changed during publication"
                return
        except OSError:
            return  # Could not verify the alias; keep the provisional name.
        self.path = named
        if not self._unlink_same(original, captured):
            self._retained_duplicate = original
        # Provisional names expire at publication, even if already announced.
        # Existing open handles still work; future reads use the final path.

    def _write(self, text):
        if self._storage_error is not None:
            return
        # Honor actual inherited OS file limits without imposing one ourselves.
        # Recheck because ordinary cell code may change its own soft limit.
        soft, _ = resource.getrlimit(resource.RLIMIT_FSIZE)
        # Avoid encoding a whole, potentially enormous rendered display at once.
        for start in range(0, len(text), 4096):
            data = text[start : start + 4096].encode("utf-8", "replace")
            remaining = len(data) if soft == resource.RLIM_INFINITY else max(0, soft - self.saved_bytes)
            clipped = len(data) > remaining
            if clipped:
                # Never leave a partial UTF-8 sequence at the end of the file.
                data = data[:remaining].decode("utf-8", "ignore").encode("utf-8")
            try:
                while data:
                    written = os.write(self._fd, data)
                    if written <= 0:
                        raise OSError("spool write made no progress")
                    self._sha256.update(data[:written])
                    self.saved_bytes += written
                    data = data[written:]
            except OSError as exc:
                self._fail_storage(f"write failed ({type(exc).__name__}, errno={exc.errno})")
                return
            if clipped:
                self._fail_storage(f"inherited OS file size limit {soft} reached")
                return

    def _spill(self):
        self._spilled = True
        try:
            self._fd, self.path = tempfile.mkstemp(prefix="py-output-", suffix=".txt", dir="/tmp")
            os.fchmod(self._fd, 0o600)
        except OSError as exc:
            self._fail_storage(f"file creation failed ({type(exc).__name__}, errno={exc.errno})")
        for _, text, _ in self._runs:
            self._write(text)
        self._runs.clear()

    def append(self, stream, text):
        if not text:
            return
        with self._lock:
            self.chars += len(text)
            self.newlines += text.count("\n")
            self._ends_newline = text.endswith("\n")
            if not self._spilled and self.chars > self.limit:
                self._spill()
            if self._spilled:
                self._write(text)
                # If overflow occurs only after cell_end, announce its path once.
                # Further late fragments update the file silently until pipe EOF.
                if self._cell_finished and self._reported is None:
                    self._report()
            elif self._runs and stream in ("stdout", "stderr") and self._runs[-1][0] == stream:
                self._runs[-1][1] += text
            else:
                self._runs.append([stream, text, 0])

    def _emit(self, stream, text):
        # Send in modest chunks without imposing a logical output-size cap.
        for start in range(0, len(text), 4096):
            self.send({
                "v": 1,
                "type": "output",
                "cell_id": self.cell_id,
                "stream": stream,
                "text": text[start : start + 4096],
            })

    def _report(self):
        complete = self._cell_finished and self._streams_closed
        state = (self.chars, complete, self._storage_error)
        if self._reported == state:
            return
        self._reported = state
        progress = "" if complete else "; counts so far, more output may follow"
        message = f"Output too long to display here (size={self.chars} chars, lines={self.lines}{progress}). "
        if self._storage_error is None:
            message += f"Saved to {self.path}. "
        else:
            message += f"Full output NOT saved: {self._storage_error}. "
            if self.path is not None:
                message += f"Partial output ({self.saved_bytes} UTF-8 bytes) saved to {self.path}. "
        if complete and self._retained_duplicate is not None:
            message += f"Provisional path {self._retained_duplicate} could not be removed (cleanup unavailable). "
        if self.path is not None:
            message += (
                f"Read this UTF-8 file in chunks smaller than {self.limit} characters "
                "(for example, f.read(4000)); larger output will be saved again. "
            )
        if not complete:
            message += (
                "This path is provisional; use the final reported path after capture completes "
                "(content-hashed when publication succeeds). "
                "Final counts will be reported when the cell and its output streams finish. "
            )
        self._emit("stdout", message.rstrip() + "\n")

    def flush(self):
        """Preserve order before say/history control events, without repeat notices."""
        with self._lock:
            if self._spilled:
                if self._reported is None:
                    self._report()
            else:
                for run in self._runs:
                    stream, text, delivered = run
                    self._emit(stream, text[delivered:])
                    run[2] = len(text)

    def finish_cell(self):
        with self._lock:
            self._cell_finished = True
            if self._streams_closed:
                self._finish_file()
            if self._spilled:
                self._report()
            else:
                self.flush()

    def streams_closed(self):
        with self._lock:
            if self._streams_closed:
                return
            self._streams_closed = True
            if self._cell_finished:
                self._finish_file()
                if self._spilled:
                    self._report()
                else:
                    self.flush()


def spool_say(cell_id, content):
    """Keep small typed replies; turn an oversized reply into a final file notice.

    Structured content is encoded incrementally as JSON. This is separate from
    the cell's stdout/stderr/display capture, so a staged final reply never points
    to a provisional filename that will disappear before it is published.
    """
    if isinstance(content, str):
        if len(content) <= OUTPUT_CHAR_LIMIT:
            return content
        chunks = (content,)
    else:
        chunks = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(content)
    notices = []
    output = CellOutput(cell_id, lambda frame: notices.append(frame["text"]))
    for chunk in chunks:
        output.append("display", chunk)
    output.streams_closed()
    output.finish_cell()
    return "".join(notices) if output.chars > OUTPUT_CHAR_LIMIT else content
