from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys

import pytest

from py_agent import kernel_sessions


def _record(root: Path, session_id: str, *, pid: int = 2) -> kernel_sessions._Record:
    directory = root / session_id
    directory.mkdir(mode=0o700)
    connection = directory / kernel_sessions._CONNECTION_NAME
    connection.write_text('{"key":"private-test-key"}', encoding="utf-8")
    connection.chmod(0o600)
    log = directory / kernel_sessions._LOG_NAME
    log.write_text("", encoding="utf-8")
    log.chmod(0o600)
    return kernel_sessions._Record(
        session_id=session_id,
        pid=pid,
        process_start="1",
        created_at=1.0,
        connection_file=connection,
        log_file=log,
        provider="fake",
        model=None,
    )


def test_metadata_is_private_and_does_not_store_connection_key(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    session_id = "a" * 32
    record = _record(root, session_id)

    kernel_sessions._write_record(root, record)
    metadata_path = root / session_id / kernel_sessions._METADATA_NAME
    loaded = kernel_sessions._load_record(root, session_id)

    assert metadata_path.stat().st_mode & 0o777 == 0o600
    assert loaded.connection_file == record.connection_file
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "private-test-key" not in json.dumps(metadata)
    assert metadata["provider"] == "fake"


def test_get_session_cleans_stale_owned_record(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    session_id = "b" * 32
    kernel_sessions._write_record(root, _record(root, session_id, pid=2))

    with pytest.raises(kernel_sessions.KernelSessionError, match="stale"):
        kernel_sessions.get_session(session_id, root=root)

    assert not (root / session_id).exists()


def test_list_cleans_dead_sessions_but_rejects_unexpected_root_entries(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    session_id = "c" * 32
    kernel_sessions._write_record(root, _record(root, session_id, pid=2))

    assert kernel_sessions.list_sessions(root=root) == ()
    assert list(root.iterdir()) == []

    (root / "not-a-session").write_text("unexpected", encoding="utf-8")
    with pytest.raises(kernel_sessions.KernelSessionError, match="Unexpected entry"):
        kernel_sessions.list_sessions(root=root)


def test_incomplete_startup_record_is_cleaned_when_no_matching_kernel_exists(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    directory = root / ("e" * 32)
    directory.mkdir(mode=0o700)
    log = directory / kernel_sessions._LOG_NAME
    log.write_text("", encoding="utf-8")
    log.chmod(0o600)

    assert kernel_sessions.list_sessions(root=root) == ()
    assert not directory.exists()


def test_session_ids_cannot_escape_the_owned_directory(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)

    with pytest.raises(kernel_sessions.KernelSessionError, match="session ID"):
        kernel_sessions.get_session("../outside", root=root)


def test_start_requires_connection_placeholder_without_leaving_a_record(tmp_path):
    root = tmp_path / "sessions"

    with pytest.raises(ValueError, match="placeholder"):
        kernel_sessions.start_session([os.fspath(Path("/bin/true"))], root=root)

    assert root.exists()
    assert list(root.iterdir()) == []


def test_symlinked_connection_file_is_rejected(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    session_id = "d" * 32
    record = _record(root, session_id)
    target = root / "outside-connection"
    target.write_text('{"key":"not-private"}', encoding="utf-8")
    target.chmod(0o600)
    record.connection_file.unlink()
    record.connection_file.symlink_to(target)
    kernel_sessions._write_record(root, record)

    with pytest.raises(kernel_sessions.KernelSessionError, match="symlink"):
        kernel_sessions._read_connection_file(record.connection_file)


def _worker_process(pid: int, *, parent_pid: int, process_group: int | None = None):
    runtime_parent = str(Path(kernel_sessions.__file__).resolve().parent.parent)
    return kernel_sessions._ProcInfo(
        pid=pid,
        uid=os.getuid(),
        state="S",
        parent_pid=parent_pid,
        process_group=pid if process_group is None else process_group,
        session_id=pid if process_group is None else process_group,
        start_ticks=str(pid + 100),
        command=(
            sys.executable,
            "-I",
            "-c",
            kernel_sessions._LOCAL_WORKER_BOOTSTRAP,
            runtime_parent,
        ),
    )


def test_forced_stop_verifies_direct_installed_worker_before_signaling(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    session_id = "f" * 32
    record = _record(root, session_id, pid=4100)
    kernel_sessions._write_record(root, record)
    worker = _worker_process(4200, parent_pid=record.pid)
    unrelated = kernel_sessions._ProcInfo(
        pid=4300, uid=os.getuid(), state="S", parent_pid=record.pid,
        process_group=4300, session_id=4300, start_ticks="4301", command=("other-child",),
    )
    monkeypatch.setattr(kernel_sessions, "_iter_proc_infos", lambda **_kwargs: (worker, unrelated))

    assert kernel_sessions._identify_executor_worker(record) == worker

    wrong_bootstrap = kernel_sessions._ProcInfo(
        **(vars(worker) | {"command": (*worker.command[:3], "import arbitrary", *worker.command[4:])})
    )
    monkeypatch.setattr(kernel_sessions, "_iter_proc_infos", lambda **_kwargs: (wrong_bootstrap, unrelated))
    with pytest.raises(kernel_sessions.KernelSessionError, match="direct py_agent.local_worker"):
        kernel_sessions._identify_executor_worker(record)


def test_forced_stop_retains_record_and_does_not_signal_unverified_worker(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    session_id = "1" * 32
    record = _record(root, session_id, pid=5100)
    kernel_sessions._write_record(root, record)
    monkeypatch.setattr(kernel_sessions, "_process_matches", lambda _record: True)
    monkeypatch.setattr(kernel_sessions, "_send_shutdown_request", lambda *_args: False)
    monkeypatch.setattr(kernel_sessions, "_wait_for_exit", lambda *_args: False)

    def unverified(_record):
        raise kernel_sessions.KernelSessionError("wrong worker bootstrap")

    monkeypatch.setattr(kernel_sessions, "_identify_executor_worker", unverified)
    signaled = []
    monkeypatch.setattr(kernel_sessions, "_signal_executor_worker_group", lambda *args: signaled.append(args))
    monkeypatch.setattr(kernel_sessions, "_signal_owned_process", lambda *args: signaled.append(args))

    with pytest.raises(kernel_sessions.KernelSessionError, match="record was retained"):
        kernel_sessions.stop_session(session_id, root=root, shutdown_timeout=0.01)

    assert (root / session_id).is_dir()
    assert signaled == []


def test_forced_stop_signals_verified_worker_and_kernel(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    session_id = "3" * 32
    record = _record(root, session_id, pid=6100)
    kernel_sessions._write_record(root, record)
    worker = _worker_process(6200, parent_pid=record.pid)
    monkeypatch.setattr(kernel_sessions, "_process_matches", lambda _record: True)
    monkeypatch.setattr(kernel_sessions, "_send_shutdown_request", lambda *_args: False)
    monkeypatch.setattr(kernel_sessions, "_wait_for_exit", lambda *_args: False)
    monkeypatch.setattr(kernel_sessions, "_identify_executor_worker", lambda _record: worker)
    monkeypatch.setattr(
        kernel_sessions, "_pin_executor_worker",
        lambda _record, process: kernel_sessions._PinnedWorker(process, 987),
    )
    calls = []
    monkeypatch.setattr(
        kernel_sessions,
        "_signal_executor_worker_group",
        lambda process, sig, **_kwargs: calls.append(("worker", process.pid, sig)),
    )
    monkeypatch.setattr(
        kernel_sessions,
        "_signal_owned_process",
        lambda _record, sig: calls.append(("kernel", record.pid, sig)),
    )
    monkeypatch.setattr(kernel_sessions, "_wait_for_forced_exit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(kernel_sessions.os, "close", lambda _fd: None)

    assert kernel_sessions.stop_session(session_id, root=root, shutdown_timeout=0.01)
    assert calls == [
        ("worker", worker.pid, signal.SIGTERM),
        ("kernel", record.pid, signal.SIGTERM),
    ]
    assert not (root / session_id).exists()


def test_worker_session_escalation_includes_grandchildren_not_other_sessions(monkeypatch):
    worker = _worker_process(6100, parent_pid=6000)
    child = kernel_sessions._ProcInfo(
        pid=6101, uid=os.getuid(), state="S", parent_pid=worker.pid,
        process_group=worker.pid, session_id=worker.pid,
        start_ticks="6102", command=("child",),
    )
    grandchild = kernel_sessions._ProcInfo(
        pid=6102, uid=os.getuid(), state="S", parent_pid=child.pid,
        process_group=worker.pid + 50, session_id=worker.pid,
        start_ticks="6103", command=("grandchild",),
    )
    unrelated = kernel_sessions._ProcInfo(
        pid=6103, uid=os.getuid(), state="S", parent_pid=worker.parent_pid,
        process_group=worker.pid + 1, session_id=worker.pid + 1,
        start_ticks="6104", command=("unrelated",),
    )
    monkeypatch.setattr(
        kernel_sessions, "_iter_proc_infos", lambda **_kwargs: (worker, child, grandchild, unrelated)
    )
    sent = []

    def fake_pidfd_signal(process, sig, *, validator):
        assert validator(process)
        sent.append((process.pid, sig))
        return True

    monkeypatch.setattr(kernel_sessions, "_pidfd_signal_process", fake_pidfd_signal)
    kernel_sessions._signal_executor_worker_group(worker, signal.SIGKILL)

    assert sent == [
        (worker.pid, signal.SIGKILL),
        (child.pid, signal.SIGKILL),
        (grandchild.pid, signal.SIGKILL),
    ]


def test_pidfd_signal_rejects_a_reused_pid(monkeypatch):
    expected = _worker_process(7100, parent_pid=7000)
    reused = kernel_sessions._ProcInfo(
        **(vars(expected) | {"start_ticks": "a-different-process"})
    )
    signaled = []
    def fake_pidfd_open(*_args):
        return 123

    monkeypatch.setattr(kernel_sessions.os, "pidfd_open", fake_pidfd_open, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: signaled.append(args), raising=False)
    monkeypatch.setattr(kernel_sessions.os, "close", lambda _fd: None)
    monkeypatch.setattr(kernel_sessions, "_proc_info", lambda _pid, **_kwargs: reused)

    assert not kernel_sessions._pidfd_signal_process(expected, signal.SIGKILL, validator=lambda _p: True)
    assert signaled == []


def test_graceful_stop_does_not_require_worker_proc_discovery(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    session_id = "2" * 32
    kernel_sessions._write_record(root, _record(root, session_id, pid=8100))
    monkeypatch.setattr(kernel_sessions, "_process_matches", lambda _record: True)
    monkeypatch.setattr(kernel_sessions, "_send_shutdown_request", lambda *_args: True)
    monkeypatch.setattr(kernel_sessions, "_wait_for_exit", lambda *_args: True)
    monkeypatch.setattr(
        kernel_sessions,
        "_identify_executor_worker",
        lambda _record: pytest.fail("graceful stop must not inspect the executor worker"),
    )

    assert kernel_sessions.stop_session(session_id, root=root, shutdown_timeout=0.01)
    assert not (root / session_id).exists()
