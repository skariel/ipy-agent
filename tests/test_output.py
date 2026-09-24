"""Worker-only spill mechanics; no sandbox or arbitrary code execution."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

import py_agent.output as module
from py_agent.output import CellOutput
from py_agent.protocol import encode_frame


@pytest.fixture()
def sink(tmp_path, monkeypatch):
    # Keep fixtures local, while checking that production explicitly uses /tmp.
    real_mkstemp = tempfile.mkstemp

    def create(**kwargs):
        assert kwargs == {"prefix": "py-output-", "suffix": ".txt", "dir": "/tmp"}
        return real_mkstemp(**(kwargs | {"dir": tmp_path}))

    monkeypatch.setattr(module, "tempfile", SimpleNamespace(mkstemp=create))
    events = []

    def send(frame):
        encode_frame(frame)
        events.append(frame)

    output = CellOutput("a1:c000001", send)
    yield output, events
    output.streams_closed()
    output.finish_cell()


def complete(output):
    output.streams_closed()
    output.finish_cell()


def text(events):
    return "".join(event["text"] for event in events)


@pytest.mark.parametrize("size", [0, 1, 7999, 8000, 8001, 20000])
def test_exact_character_boundary(sink, size):
    output, events = sink
    value = "x" * size
    output.append("stdout", value)
    assert not events
    complete(output)
    assert output.chars == size
    assert output.lines == bool(size)
    if size <= 8000:
        assert text(events) == value
        assert output.path is None
    else:
        assert Path(output.path).read_text() == value
        assert f"size={size} chars, lines=1" in text(events)
        assert "counts so far" not in text(events)
        assert len(text(events)) < 600
    assert output._fd is None


def test_unicode_limit_is_characters_not_bytes_and_file_is_private(sink):
    output, events = sink
    value = "雪🐍\n" * 3000
    output.append("stdout", value[:8000])
    assert output.path is None
    output.append("stderr", value[8000:])
    complete(output)
    assert Path(output.path).read_text(encoding="utf-8") == value
    assert Path(output.path).stat().st_mode & 0o777 == 0o600
    assert output.saved_bytes == len(value.encode("utf-8"))
    assert output.chars == 9000
    assert output.lines == 3000
    assert "size=9000 chars, lines=3000" in text(events)
    assert "smaller than 8000 characters" in text(events)


@pytest.mark.parametrize(
    "value,lines", [("", 0), ("a", 1), ("\n", 1), ("a\n", 1), ("a\nb", 2), ("\n\n", 2), ("a\r\nb\n", 2)]
)
def test_line_count_includes_final_unterminated_line(sink, value, lines):
    output, _ = sink
    for char in value:
        output.append("stdout", char)
    assert output.lines == lines


def test_tiny_writes_coalesce_before_transport(sink):
    output, events = sink
    for _ in range(8000):
        output.append("stdout", "x")
    complete(output)
    assert text(events) == "x" * 8000
    assert len(events) == 2
    assert sum(len(encode_frame(event)) for event in events) < 9000


def test_control_boundaries_preserve_order_and_spilled_prefix(sink):
    output, events = sink
    output.append("stdout", "before\n")
    output.flush()
    events.append({"text": "CONTROL", "stream": "control"})
    output.append("stderr", "error\n")
    output.append("display", "display")
    output.append("stdout", "x" * 9000)
    output.flush()
    output.flush()  # No new notice for each say/history control event.
    output.append("stderr", "last\n")
    complete(output)
    assert events[0]["text"] == "before\n"
    assert events[1]["text"] == "CONTROL"
    assert len(events) == 4  # prefix, control, one progress notice, final notice
    assert Path(output.path).read_text() == "before\nerror\ndisplay" + "x" * 9000 + "last\n"
    assert output.chars == 9025
    assert output.lines == 3
    assert "size=9025 chars, lines=3" in events[-1]["text"]


def test_short_output_preserves_streams_and_display_boundaries(sink):
    output, events = sink
    for stream, value in [
        ("stdout", "a"),
        ("stdout", "b"),
        ("stderr", "c"),
        ("display", "d"),
        ("display", "e"),
        ("stdout", "f"),
    ]:
        output.append(stream, value)
    complete(output)
    assert [(event["stream"], event["text"]) for event in events] == [
        ("stdout", "ab"),
        ("stderr", "c"),
        ("display", "d"),
        ("display", "e"),
        ("stdout", "f"),
    ]


def test_large_incremental_stream_retains_no_full_text_in_memory(sink):
    output, events = sink
    block = "a" * 4096
    for _ in range(512):
        output.append("stdout", block)
        assert sum(len(run[1]) for run in output._runs) <= 8000
    complete(output)
    assert len(events) == 1
    assert output.chars == 2 * 1024 * 1024
    assert Path(output.path).stat().st_size == output.chars
    with Path(output.path).open(encoding="utf-8") as saved:
        assert saved.read(4000) == "a" * 4000


def test_late_small_output_emits_only_undelivered_text(sink):
    output, events = sink
    output.append("stdout", "early")
    output.finish_cell()
    output.append("stdout", "late")
    assert text(events) == "early"
    output.streams_closed()
    assert text(events) == "earlylate"
    assert [event["cell_id"] for event in events] == ["a1:c000001"] * 2


def test_late_overflow_saves_already_delivered_prefix_and_final_counts(sink):
    output, events = sink
    output.append("stdout", "early\n")
    output.finish_cell()
    output.append("stdout", "late\n" * 2000)
    assert "counts so far" in events[-1]["text"]
    fd = output._fd
    assert fd is not None
    before = len(events)
    for _ in range(100):
        output.append("stderr", "tail\n")
    assert len(events) == before
    output.streams_closed()
    assert output._fd is None
    with pytest.raises(OSError):
        os.fstat(fd)
    assert Path(output.path).read_text() == "early\n" + "late\n" * 2000 + "tail\n" * 100
    assert "size=10506 chars, lines=2101" in events[-1]["text"]
    assert "counts so far" not in events[-1]["text"]
    assert len(events) == 3


def test_early_pipe_eof_keeps_file_open_until_display_and_cell_end(sink):
    output, events = sink
    output.append("stdout", "x" * 9000)
    output.streams_closed()
    assert output._fd is not None
    output.append("display", "value")
    output.finish_cell()
    assert output._fd is None
    assert Path(output.path).read_text() == "x" * 9000 + "value"
    assert "size=9005 chars" in text(events)
    output.streams_closed()
    output.finish_cell()
    assert len(events) == 1


def test_inherited_file_limit_preserves_utf8_and_reports_incomplete_without_stopping(sink, monkeypatch):
    output, events = sink
    inherited_file_limit(monkeypatch, 7)
    output.append("stdout", "雪" * 9000)
    output.append("stderr", "\nmore\n")
    complete(output)
    assert Path(output.path).read_text(encoding="utf-8") == "雪雪"
    assert Path(output.path).stat().st_size == 6
    assert output.chars == 9006
    assert output.lines == 2
    assert "Full output NOT saved: inherited OS file size limit 7 reached" in text(events)
    assert "Partial output (6 UTF-8 bytes)" in text(events)
    assert output._fd is None


def test_creation_failure_drains_and_counts_without_throwing(sink, monkeypatch):
    output, events = sink

    def fail(**kwargs):
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr(module.tempfile, "mkstemp", fail)
    output.append("stdout", "x" * 9000)
    output.append("stderr", "\nlast")
    complete(output)
    assert output.chars == 9005
    assert output.lines == 2
    assert "Full output NOT saved: file creation failed" in text(events)
    assert output.path is None
    assert output._fd is None
    assert "Saved to" not in text(events)


def test_mid_write_failure_preserves_partial_prefix_and_closes_fd(sink, monkeypatch):
    output, events = sink
    real_write = os.write
    calls = []

    def write(fd, data):
        calls.append(fd)
        if len(calls) == 1:
            return real_write(fd, data)
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(module, "os", SimpleNamespace(**(vars(os) | {"write": write})))
    output.append("stdout", "prefix\n")
    output.append("stdout", "x" * 9000)
    output.append("stdout", "last\n")
    complete(output)
    assert Path(output.path).read_text() == "prefix\n"
    assert output._sha256.hexdigest() == hashlib.sha256(b"prefix\n").hexdigest()
    assert "Full output NOT saved: write failed" in text(events)
    assert "Partial output (7 UTF-8 bytes)" in text(events)
    assert output.chars == 9012
    assert output.lines == 2
    assert output._fd is None
    with pytest.raises(OSError):
        os.fstat(calls[0])


def test_spool_paths_are_unique_and_do_not_overwrite_previous_output(sink):
    output, events = sink
    output.append("stdout", "first" * 2000)
    complete(output)
    second = CellOutput("a1:c000002", events.append)
    second.append("stdout", "second" * 2000)
    complete(second)
    assert output.path != second.path
    assert Path(output.path).read_text() == "first" * 2000
    assert Path(second.path).read_text() == "second" * 2000


def test_secure_creation_does_not_follow_existing_symlink(sink, tmp_path, monkeypatch):
    output, _ = sink
    target = tmp_path / "must-not-overwrite"
    target.write_text("unchanged")
    (tmp_path / "py-output-occupied.txt").symlink_to(target)
    monkeypatch.setattr(tempfile, "_get_candidate_names", lambda: iter(["occupied", "fresh"]))
    output.append("stdout", "x" * 9000)
    complete(output)
    assert Path(output.path).name == f"py-output-{hashlib.sha256(b'x' * 9000).hexdigest()[:16]}.txt"
    assert not Path(output.path).is_symlink()
    assert target.read_text() == "unchanged"


def test_short_os_writes_are_retried_without_dropping_bytes(sink, monkeypatch):
    output, _ = sink
    output.limit = 1
    real_write = os.write
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            **(
                vars(os)
                | {
                    "write": lambda fd, data: real_write(fd, data[:1]),
                }
            )
        ),
    )
    output.append("stdout", "🐍雪\n")
    complete(output)
    assert Path(output.path).read_text(encoding="utf-8") == "🐍雪\n"
    assert output.saved_bytes == 8
    assert hashlib.sha256("🐍雪\n".encode()).hexdigest()[:16] in Path(output.path).name


def inherited_file_limit(monkeypatch, size):
    monkeypatch.setattr(
        module,
        "resource",
        SimpleNamespace(
            RLIMIT_FSIZE=module.resource.RLIMIT_FSIZE,
            RLIM_INFINITY=-1,
            getrlimit=lambda key: (size, size),
        ),
    )


def test_changed_inherited_file_limit_is_seen_at_write_time(sink, monkeypatch):
    output, events = sink
    inherited_file_limit(monkeypatch, 123)
    output.append("stdout", "x" * 9000)
    complete(output)
    assert Path(output.path).stat().st_size == 123
    assert "inherited OS file size limit 123 reached" in text(events)


def test_spool_over_old_64mib_cap_has_complete_hash_file_with_bounded_memory(sink):
    output, events = sink
    size = 65 * 1024 * 1024
    soft, _ = module.resource.getrlimit(module.resource.RLIMIT_FSIZE)
    if soft != module.resource.RLIM_INFINITY and soft < size:
        pytest.skip("Inherited OS file limit prevents >64MiB regression")
    block = "x" * 65536
    expected_hash = hashlib.sha256()
    for _ in range(size // len(block)):
        output.append("stdout", block)
        expected_hash.update(block.encode())
        assert sum(len(run[1]) for run in output._runs) <= 8000
    complete(output)
    path = Path(output.path)
    assert path.stat().st_size == output.chars == size
    assert path.name == f"py-output-{expected_hash.hexdigest()[:16]}.txt"
    assert len(events) == 1
    assert "Full output NOT saved" not in text(events)


def test_real_inherited_fsize_reports_incomplete_without_signal_exit(tmp_path):
    import subprocess
    import sys

    # Fixed trusted subprocess: constrain only this tiny spool exercise, never
    # the parent test runner or IPython's unrelated history database.
    source = """
import json, resource, sys, tempfile
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import py_agent.output as module
real_mkstemp = tempfile.mkstemp
def create(**kwargs):
    assert kwargs["dir"] == "/tmp"
    return real_mkstemp(**(kwargs | {"dir": sys.argv[2]}))
module.tempfile = SimpleNamespace(mkstemp=create)
resource.setrlimit(resource.RLIMIT_FSIZE, (32, 32))
frames = []
output = module.CellOutput("a1:c1", frames.append)
output.append("stdout", "x" * 9000)
output.streams_closed()
output.finish_cell()
print(json.dumps({"path": output.path, "text": "".join(f["text"] for f in frames)}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", source, str(Path(module.__file__).resolve().parent.parent), str(tmp_path)],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    value = __import__("json").loads(result.stdout)
    assert Path(value["path"]).read_bytes() == b"x" * 32
    assert "Full output NOT saved: inherited OS file size limit 32 reached" in value["text"]


def hash_path(original, value):
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return original.with_name(f"py-output-{digest}.txt")


def test_completed_filename_hashes_exact_incremental_utf8_and_is_idempotent(sink):
    output, events = sink
    output.limit = 1
    pieces = ["snow 雪\n", "🐍" * 2000, "\nlast"]
    for piece in pieces:
        output.append("stdout", piece)
    original = Path(output.path)
    expected = hash_path(original, "".join(pieces))
    complete(output)
    assert Path(output.path) == expected
    assert expected.read_bytes() == "".join(pieces).encode("utf-8")
    assert expected.stat().st_mode & 0o777 == 0o600
    assert not original.exists()  # Never announced: remove the provisional name.
    assert f"Saved to {expected}" in text(events)
    assert len(events) == 1
    complete(output)
    complete(output)
    assert Path(output.path) == expected
    assert len(events) == 1
    assert list(expected.parent.iterdir()) == [expected]


def test_identical_contents_reuse_one_verified_canonical_file(sink):
    output, events = sink
    value = "same 雪🐍\n" * 3000
    output.append("stdout", value)
    complete(output)
    canonical = Path(output.path)
    inode = canonical.stat().st_ino
    for index in range(5):
        other = CellOutput(f"a1:c{index + 2:06d}", events.append)
        other.append("stdout", value)
        provisional = Path(other.path)
        complete(other)
        assert other.path == output.path
        assert canonical.stat().st_ino == inode
        assert not provisional.exists()
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    assert canonical.name == f"py-output-{digest}.txt"
    assert canonical.read_text() == value
    assert list(canonical.parent.iterdir()) == [canonical]


def test_early_duplicate_path_is_removed_and_content_deduplicated(sink):
    output, events = sink
    value = "repeat\n" * 2000
    output.append("stdout", value)
    complete(output)
    canonical = Path(output.path)
    other = CellOutput("a1:c000002", events.append)
    other.append("stdout", value)
    other.flush()
    provisional = Path(other.path)
    assert "This path is provisional" in events[-1]["text"]
    assert "use the final reported path" in events[-1]["text"]
    with provisional.open(encoding="utf-8") as reader:
        complete(other)
        assert reader.read() == value  # Unlink does not invalidate open handles.
    assert Path(other.path) == canonical
    assert not provisional.exists()
    assert canonical.read_text() == value
    assert list(canonical.parent.iterdir()) == [canonical]
    assert "cleanup unavailable" not in events[-1]["text"]
    assert f"Saved to {canonical}" in events[-1]["text"]


def test_duplicate_cleanup_failure_is_disclosed_without_clobbering(sink, monkeypatch):
    output, events = sink
    value = "same" * 3000
    output.append("stdout", value)
    complete(output)
    other = CellOutput("a1:c000002", events.append)
    other.append("stdout", value)
    provisional = Path(other.path)
    real_unlink = os.unlink

    def deny(path, *args, **kwargs):
        if path == str(provisional):
            raise PermissionError(errno.EACCES, "synthetic cleanup denial")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module, "os", SimpleNamespace(**(vars(os) | {"unlink": deny})))
    complete(other)
    assert other.path == output.path
    assert provisional.read_text() == Path(other.path).read_text() == value
    assert "cleanup unavailable" in events[-1]["text"]


def test_nonprivate_source_is_not_published_under_canonical_hash(sink):
    output, _ = sink
    value = "x" * 9000
    output.append("stdout", value)
    provisional = Path(output.path)
    provisional.chmod(0o644)
    complete(output)
    assert Path(output.path) == provisional
    assert not hash_path(provisional, value).exists()
    assert provisional.read_text() == value


def test_early_notice_path_expires_after_final_hash_publication(sink):
    output, events = sink
    value = "early\n" * 1500
    output.append("stdout", value)
    output.flush()
    early = Path(output.path)
    assert f"Saved to {early}" in events[-1]["text"]
    assert "This path is provisional" in events[-1]["text"]
    with early.open(encoding="utf-8") as reader:
        output.finish_cell()
        output.append("stderr", "late\n")
        output.streams_closed()
        assert reader.read() == value + "late\n"
    named = hash_path(early, value + "late\n")
    assert Path(output.path) == named
    assert not early.exists()
    assert named.read_text() == value + "late\n"
    assert list(named.parent.iterdir()) == [named]
    assert f"Saved to {named}" in events[-1]["text"]
    assert "counts so far" not in events[-1]["text"]


@pytest.mark.parametrize("symlink", [False, True])
def test_hash_destination_collision_never_overwrites_or_follows(sink, tmp_path, symlink):
    output, events = sink
    value = "x" * 9000
    output.append("stdout", value)
    original = Path(output.path)
    destination = hash_path(original, value)
    target = tmp_path / "untouched"
    target.write_text("other data")
    if symlink:
        destination.symlink_to(target)
    else:
        destination.write_text("collision")
    complete(output)
    assert Path(output.path) == original
    assert original.read_text() == value
    assert destination.is_symlink() == symlink
    assert destination.read_text() == ("other data" if symlink else "collision")
    assert target.read_text() == "other data"
    assert f"Saved to {original}" in text(events)
    assert "Full output NOT saved" not in text(events)


@pytest.mark.parametrize("kind", ["corrupt", "fifo", "symlink", "readable_by_others", "not_writable", "unreadable"])
def test_existing_canonical_requires_safe_private_matching_contents(sink, tmp_path, monkeypatch, kind):
    output, events = sink
    value = "x" * 9000
    output.append("stdout", value)
    original = Path(output.path)
    canonical = hash_path(original, value)
    if kind == "fifo":
        os.mkfifo(canonical, 0o600)
    elif kind == "symlink":
        target = tmp_path / "same-content-target"
        target.write_text(value)
        target.chmod(0o600)
        canonical.symlink_to(target)
    else:
        canonical.write_text("y" * 9000 if kind == "corrupt" else value)
        canonical.chmod(0o644 if kind == "readable_by_others" else 0o400 if kind == "not_writable" else 0o600)
    if kind == "unreadable":
        real_open = os.open

        def denied(path, flags, *args, **kwargs):
            if path == str(canonical):
                raise PermissionError(errno.EACCES, "synthetic denial")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(module, "os", SimpleNamespace(**(vars(os) | {"open": denied})))
    before = canonical.lstat()
    complete(output)
    assert Path(output.path) == original
    assert original.read_text() == value
    after = canonical.lstat()
    assert (after.st_ino, after.st_mode, after.st_size) == (before.st_ino, before.st_mode, before.st_size)
    assert f"Saved to {original}" in text(events)
    assert "Full output NOT saved" not in text(events)


def test_foreign_owned_canonical_is_not_reused(sink, monkeypatch):
    output, _ = sink
    value = "x" * 9000
    output.append("stdout", value)
    original = Path(output.path)
    canonical = hash_path(original, value)
    canonical.write_text(value)
    canonical.chmod(0o600)
    foreign_inode = canonical.stat().st_ino
    real_fstat = os.fstat

    def foreign(fd):
        info = real_fstat(fd)
        if info.st_ino == foreign_inode:
            return SimpleNamespace(
                **(
                    {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                    | {"st_uid": info.st_uid + 1}
                )
            )
        return info

    monkeypatch.setattr(module, "os", SimpleNamespace(**(vars(os) | {"fstat": foreign})))
    complete(output)
    assert Path(output.path) == original
    assert original.read_text() == canonical.read_text() == value


@pytest.mark.parametrize("change", ["contents", "path", "permissions"])
def test_canonical_change_during_validation_is_not_reused(sink, monkeypatch, change):
    output, _ = sink
    value = "x" * 9000
    output.append("stdout", value)
    original = Path(output.path)
    canonical = hash_path(original, value)
    canonical.write_text(value)
    canonical.chmod(0o600)
    inode = canonical.stat().st_ino
    real_read = os.read
    mutated = False

    def changing(fd, amount):
        nonlocal mutated
        block = real_read(fd, amount)
        if not mutated and os.fstat(fd).st_ino == inode:
            mutated = True
            if change == "contents":
                canonical.write_text("y" * 9000)
            elif change == "path":
                replacement = canonical.with_name("replacement")
                replacement.write_text(value)
                replacement.chmod(0o600)
                Path(replacement).replace(canonical)
            else:
                canonical.chmod(0o644)
        return block

    monkeypatch.setattr(module, "os", SimpleNamespace(**(vars(os) | {"read": changing})))
    complete(output)
    assert mutated
    assert Path(output.path) == original
    assert original.read_text() == value


def test_existing_artifact_verification_reads_bounded_chunks_and_closes_fd(sink, monkeypatch):
    output, events = sink
    value = "雪🐍\n" * 20000
    output.append("stdout", value)
    complete(output)
    other = CellOutput("a1:c000002", events.append)
    other.append("stdout", value)
    real_open, real_read, real_close = os.open, os.read, os.close
    opened, reads, closed = [], [], []

    def tracked_open(path, flags, *args, **kwargs):
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_NONBLOCK
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    def tracked_read(fd, size):
        reads.append(size)
        return real_read(fd, size)

    def tracked_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            **(
                vars(os)
                | {
                    "open": tracked_open,
                    "read": tracked_read,
                    "close": tracked_close,
                }
            )
        ),
    )
    complete(other)
    assert other.path == output.path
    assert len(opened) == 1
    assert opened[0] in closed
    assert max(reads) <= 65536
    assert len(reads) > 2


def test_hash_publication_failure_keeps_original_readable(sink, monkeypatch):
    output, events = sink
    value = "x" * 9000
    output.append("stdout", value)
    original = Path(output.path)
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError(errno.EPERM, "hardlinks unavailable")

    monkeypatch.setattr(module, "os", SimpleNamespace(**(vars(os) | {"link": fail})))
    complete(output)
    complete(output)
    assert len(calls) == 1
    assert Path(output.path) == original
    assert original.read_text() == value
    assert "Full output NOT saved" not in text(events)
    assert output._fd is None


def test_close_failure_does_not_publish_hash_name(sink, monkeypatch):
    output, events = sink
    output.append("stdout", "x" * 9000)
    original, fd = Path(output.path), output._fd
    real_close = os.close

    def fail_close(number):
        real_close(number)
        if number == fd:
            raise OSError(errno.EIO, "synthetic close failure")

    monkeypatch.setattr(module, "os", SimpleNamespace(**(vars(os) | {"close": fail_close})))
    complete(output)
    assert Path(output.path) == original
    assert original.read_text() == "x" * 9000
    assert "Full output NOT saved: close failed" in text(events)
    assert len(list(original.parent.iterdir())) == 1


def test_incomplete_output_keeps_provisional_name_without_full_content_hash(sink, monkeypatch):
    output, events = sink
    inherited_file_limit(monkeypatch, 3)
    output.append("stdout", "x" * 9000)
    provisional = Path(output.path)
    complete(output)
    assert Path(output.path) == provisional
    assert provisional.read_bytes() == b"xxx"
    assert hashlib.sha256(b"xxx").hexdigest() not in provisional.name
    assert "Full output NOT saved" in text(events)


@pytest.mark.parametrize("replacement", ["missing", "file", "symlink"])
def test_source_replacement_before_publication_is_not_hash_labelled(sink, tmp_path, replacement):
    output, events = sink
    output.append("stdout", "x" * 9000)
    original = Path(output.path)
    expected = hash_path(original, "x" * 9000)
    original.unlink()
    if replacement == "file":
        original.write_text("replacement")
    elif replacement == "symlink":
        target = tmp_path / "target"
        target.write_text("replacement")
        original.symlink_to(target)
    complete(output)
    assert not expected.exists()
    assert output.path is None
    assert "Full output NOT saved: spool pathname changed" in text(events)
    assert "Saved to" not in text(events)
    if replacement != "missing":
        assert original.read_text() == "replacement"


@pytest.mark.parametrize("symlink", [False, True])
def test_source_swap_during_link_is_detected_without_touching_replacement(sink, tmp_path, monkeypatch, symlink):
    output, events = sink
    value = "x" * 9000
    output.append("stdout", value)
    original = Path(output.path)
    destination = hash_path(original, value)
    target = tmp_path / "replacement"
    target.write_text("other data")
    real_link = os.link

    def swap_then_link(source, named, **kwargs):
        assert kwargs == {"follow_symlinks": False}
        original.unlink()
        if symlink:
            original.symlink_to(target)
        else:
            real_link(target, original)
        real_link(source, named, **kwargs)

    monkeypatch.setattr(module, "os", SimpleNamespace(**(vars(os) | {"link": swap_then_link})))
    complete(output)
    assert output.path is None
    assert not destination.exists()
    assert not destination.is_symlink()
    assert original.read_text() == target.read_text() == "other data"
    assert "Full output NOT saved: spool pathname changed during publication" in text(events)
    assert "Saved to" not in text(events)
