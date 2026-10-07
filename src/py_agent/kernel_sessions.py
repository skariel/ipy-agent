"""Private lifecycle records for independently managed Jupyter kernels.

A connection file contains a bearer key for an execution-capable kernel. Session
files are therefore kept in an owned mode-0700 directory and are never printed
by this module. This is lifecycle hygiene, not a same-user security boundary.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import errno
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

CONNECTION_FILE_PLACEHOLDER = "{connection_file}"

DEFAULT_SESSION_ROOT = Path.home() / ".py" / "kernel-sessions"
_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_METADATA_NAME = "session.json"
_CONNECTION_NAME = "connection.json"
_LOG_NAME = "kernel.log"
_METADATA_VERSION = 1


class KernelSessionError(RuntimeError):
    """Invalid, unavailable, or unsafe managed-kernel lifecycle operation."""


class UnsupportedKernelSessions(KernelSessionError):
    """The current platform or optional runtime cannot manage kernel sessions."""


@dataclass(frozen=True)
class KernelSession:
    session_id: str
    pid: int
    created_at: float
    connection_file: Path
    log_file: Path
    provider: str | None
    model: str | None
    running: bool
    connection_ready: bool = True


@dataclass(frozen=True)
class _Record:
    session_id: str
    pid: int
    process_start: str
    created_at: float
    connection_file: Path
    log_file: Path
    provider: str | None
    model: str | None

    def session(self, *, running: bool, connection_ready: bool = True) -> KernelSession:
        return KernelSession(
            self.session_id, self.pid, self.created_at, self.connection_file,
            self.log_file, self.provider, self.model, running, connection_ready,
        )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _check_no_symlink_components(path: Path) -> None:
    path = _absolute(path)
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise KernelSessionError(f"Managed kernel path contains a symlink: {component}")


def _ensure_private_directory(path: Path) -> Path:
    path = _absolute(path)
    _check_no_symlink_components(path)
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise KernelSessionError(f"Managed kernel directory must be owned and private (mode 0700): {path}")
    return path


def _private_regular_file(path: Path, *, maximum_size: int = 65536) -> os.stat_result:
    _check_no_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise KernelSessionError(f"Managed kernel file is missing: {path.name}") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
        or info.st_size > maximum_size
    ):
        raise KernelSessionError(f"Managed kernel file must be a private owned regular file: {path.name}")
    return info


def _read_private_text(path: Path, *, maximum_size: int = 65536) -> str:
    """Read a bounded private file without following a replacement symlink."""
    _check_no_symlink_components(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise KernelSessionError(f"Managed kernel file is missing: {path.name}") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise KernelSessionError(f"Managed kernel file is a symlink: {path.name}") from exc
        raise KernelSessionError(f"Could not safely open managed kernel file: {path.name}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > maximum_size
        ):
            raise KernelSessionError(f"Managed kernel file must be a private owned regular file: {path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            contents = stream.read(maximum_size + 1)
    finally:
        os.close(descriptor)
    if len(contents) > maximum_size:
        raise KernelSessionError(f"Managed kernel file exceeds {maximum_size} bytes: {path.name}")
    try:
        return contents.decode("utf-8")
    except UnicodeError as exc:
        raise KernelSessionError(f"Managed kernel file is not valid UTF-8: {path.name}") from exc


def _require_linux_process_identity() -> None:
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        raise UnsupportedKernelSessions("Managed kernel sessions currently require Linux /proc process identity")


@dataclass(frozen=True)
class _ProcInfo:
    pid: int
    uid: int
    state: str
    parent_pid: int
    process_group: int
    session_id: int
    start_ticks: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class _PinnedWorker:
    process: _ProcInfo
    pidfd: int


def _proc_stat(
    pid: int, *, strict: bool = False
) -> tuple[str, int, int, int, str] | None:
    proc = Path("/proc") / str(pid)
    try:
        raw_stat = (proc / "stat").read_text(encoding="ascii")
        # The comm field is parenthesized and may itself contain spaces or ')'.
        fields = raw_stat.rsplit(")", 1)[1].split()
        return fields[0], int(fields[1]), int(fields[2]), int(fields[3]), fields[19]
    except PermissionError as exc:
        if strict:
            raise KernelSessionError(f"Could not verify Linux process {pid} ownership") from exc
        return None
    except (FileNotFoundError, ProcessLookupError, IndexError, ValueError, OSError):
        return None


def _proc_info(
    pid: int, *, read_command: bool = True, strict: bool = False
) -> _ProcInfo | None:
    """Read an owned Linux process identity, rejecting PID reuse during inspection."""
    _require_linux_process_identity()
    proc = Path("/proc") / str(pid)
    try:
        uid = proc.stat().st_uid
        if uid != os.getuid():
            return None
        before = _proc_stat(pid, strict=strict)
        if before is None:
            return None
        raw_command = (proc / "cmdline").read_bytes() if read_command else b""
        after = _proc_stat(pid, strict=strict)
    except PermissionError as exc:
        if strict:
            raise KernelSessionError(f"Could not verify Linux process {pid} ownership") from exc
        return None
    except (FileNotFoundError, ProcessLookupError, OSError):
        return None
    if after is None or before[4] != after[4]:
        return None
    command = tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in raw_command.rstrip(b"\x00").split(b"\x00")
    ) if raw_command else ()
    state, parent_pid, process_group, session_id, start_ticks = after
    return _ProcInfo(
        pid, uid, state, parent_pid, process_group, session_id, start_ticks, command,
    )


def _iter_proc_infos(
    *, read_command: bool = True, strict: bool = False
) -> tuple[_ProcInfo, ...]:
    _require_linux_process_identity()
    try:
        processes = tuple(Path("/proc").iterdir())
    except OSError as exc:
        raise KernelSessionError("Could not inspect Linux /proc process ownership") from exc
    result = []
    for entry in processes:
        if entry.name.isdigit():
            info = _proc_info(int(entry.name), read_command=read_command, strict=strict)
            if info is not None:
                result.append(info)
    return tuple(result)


def _process_identity(pid: int) -> tuple[str, str] | None:
    """Return Linux process start ticks and command line, or None if absent."""
    info = _proc_info(pid, strict=True)
    if info is None:
        return None
    return info.start_ticks, " ".join(info.command)


def _process_matches(record: _Record) -> bool:
    identity = _process_identity(record.pid)
    if identity is None:
        return False
    start_ticks, command = identity
    expected_connection = str(record.connection_file)
    return (
        start_ticks == record.process_start
        and "py_agent.jupyter_kernel" in command
        and expected_connection in command
    )


def _record_path(root: Path, session_id: str) -> Path:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise KernelSessionError("Kernel session ID must be a 32-character lowercase hexadecimal ID")
    return root / session_id


def _recover_incomplete_session(root: Path, session_id: str) -> _Record | None:
    """Recover a process launched just before its initial metadata was committed."""
    _require_linux_process_identity()
    directory = _record_path(root, session_id)
    _check_no_symlink_components(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise KernelSessionError(f"Kernel session directory is not private and owned: {session_id}")
    for child in directory.iterdir():
        child_info = child.lstat()
        if (
            not stat.S_ISREG(child_info.st_mode)
            or child_info.st_uid != os.getuid()
            or child_info.st_mode & 0o077
            or child_info.st_nlink != 1
        ):
            raise KernelSessionError(f"Refusing to recover an unexpected kernel session entry: {child.name}")

    connection_file = directory / _CONNECTION_NAME
    matches = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        pid = int(proc.name)
        identity = _process_identity(pid)
        if identity is None:
            continue
        start, command = identity
        if "py_agent.jupyter_kernel" not in command or str(connection_file) not in command:
            continue
        try:
            if os.getpgid(pid) == pid:
                matches.append((pid, start))
        except ProcessLookupError:
            continue
    if len(matches) > 1:
        raise KernelSessionError(f"Multiple processes match incomplete kernel session {session_id}")
    if not matches:
        _remove_session_directory(root, session_id)
        return None
    pid, start = matches[0]
    record = _Record(
        session_id, pid, start, info.st_mtime, connection_file, directory / _LOG_NAME,
        None, None,
    )
    _write_record(root, record)
    return record


def _load_record(root: Path, session_id: str) -> _Record:
    directory = _record_path(root, session_id)
    _check_no_symlink_components(directory)
    try:
        directory_info = directory.lstat()
    except FileNotFoundError:
        raise KernelSessionError(f"Unknown managed kernel session: {session_id}") from None
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != os.getuid()
        or directory_info.st_mode & 0o077
    ):
        raise KernelSessionError(f"Kernel session directory is not private and owned: {session_id}")
    metadata = directory / _METADATA_NAME
    _private_regular_file(metadata)
    try:
        value = json.loads(_read_private_text(metadata))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KernelSessionError(f"Kernel session metadata is invalid: {session_id}") from exc
    keys = {
        "version", "session_id", "pid", "process_start", "created_at",
        "connection_file", "log_file", "provider", "model",
    }
    if not isinstance(value, dict) or set(value) != keys or value.get("version") != _METADATA_VERSION:
        raise KernelSessionError(f"Kernel session metadata has an unsupported format: {session_id}")
    if (
        value["session_id"] != session_id
        or type(value["pid"]) is not int
        or value["pid"] <= 1
        or not isinstance(value["process_start"], str)
        or not value["process_start"].isdigit()
        or type(value["created_at"]) not in (int, float)
        or not 0 <= value["created_at"] <= 4_102_444_800
        or not isinstance(value["connection_file"], str)
        or not isinstance(value["log_file"], str)
        or (value["provider"] is not None and not isinstance(value["provider"], str))
        or (value["model"] is not None and not isinstance(value["model"], str))
    ):
        raise KernelSessionError(f"Kernel session metadata contains invalid fields: {session_id}")
    connection_file = Path(value["connection_file"])
    log_file = Path(value["log_file"])
    if connection_file != directory / _CONNECTION_NAME or log_file != directory / _LOG_NAME:
        raise KernelSessionError(f"Kernel session metadata points outside its private directory: {session_id}")
    return _Record(
        session_id, value["pid"], value["process_start"], float(value["created_at"]),
        connection_file, log_file, value["provider"], value["model"],
    )


def _load_or_recover_record(root: Path, session_id: str) -> _Record | None:
    directory = _record_path(root, session_id)
    _check_no_symlink_components(directory)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        raise KernelSessionError(f"Unknown managed kernel session: {session_id}") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise KernelSessionError(f"Kernel session directory is not private and owned: {session_id}")
    metadata = directory / _METADATA_NAME
    try:
        metadata.lstat()
    except FileNotFoundError:
        return _recover_incomplete_session(root, session_id)
    return _load_record(root, session_id)


def _write_record(root: Path, record: _Record) -> None:
    directory = _record_path(root, record.session_id)
    payload = {
        "version": _METADATA_VERSION,
        "session_id": record.session_id,
        "pid": record.pid,
        "process_start": record.process_start,
        "created_at": record.created_at,
        "connection_file": str(record.connection_file),
        "log_file": str(record.log_file),
        "provider": record.provider,
        "model": record.model,
    }
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / _METADATA_NAME)
        os.chmod(directory / _METADATA_NAME, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _remove_session_directory(root: Path, session_id: str) -> None:
    directory = _record_path(root, session_id)
    _check_no_symlink_components(directory)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise KernelSessionError(f"Refusing to clean a non-private kernel session directory: {session_id}")
    for child in directory.iterdir():
        child_info = child.lstat()
        if (
            not stat.S_ISREG(child_info.st_mode)
            or child_info.st_uid != os.getuid()
            or child_info.st_nlink != 1
        ):
            raise KernelSessionError(f"Refusing to clean an unexpected kernel session entry: {child.name}")
        child.unlink()
    directory.rmdir()


def _read_connection_file(path: Path) -> dict[str, Any]:
    _private_regular_file(path)
    try:
        value = json.loads(_read_private_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KernelSessionError("Kernel connection file is invalid") from exc
    if not isinstance(value, dict) or not isinstance(value.get("key"), str) or not value["key"]:
        raise KernelSessionError("Kernel connection file has no private authentication key")
    return value


def _wait_for_connection_file(
    process: subprocess.Popen[bytes],
    path: Path,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise KernelSessionError(f"Kernel process exited during startup (status {process.returncode})")
        try:
            _read_connection_file(path)
            return
        except KernelSessionError:
            time.sleep(0.05)
    raise KernelSessionError("Timed out waiting for the private Jupyter connection file")


def _wait_for_kernel_ready(connection_file: Path, timeout: float) -> None:
    try:
        from jupyter_client.blocking.client import BlockingKernelClient
    except ImportError as exc:
        raise UnsupportedKernelSessions(
            "Managed Jupyter kernels require the optional 'jupyter_client' package; install py-agent[jupyter]"
        ) from exc
    client = BlockingKernelClient(connection_file=str(connection_file))
    client.load_connection_file()
    client.start_channels()
    try:
        client.wait_for_ready(timeout=timeout)
    finally:
        client.stop_channels()


def _terminate_spawned(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def start_session(
    command: Sequence[str],
    *,
    root: Path = DEFAULT_SESSION_ROOT,
    cwd: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    wait_timeout: float = 30.0,
    wait_ready: Callable[[Path, float], None] | None = None,
) -> KernelSession:
    """Start a detached kernel, keeping its connection key in private storage."""
    _require_linux_process_identity()
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or not command:
        raise ValueError("Kernel command must be a nonempty argument sequence")
    if any(not isinstance(item, str) or "\x00" in item for item in command):
        raise ValueError("Kernel command arguments must be NUL-free text")
    if (
        type(wait_timeout) not in (int, float)
        or not math.isfinite(wait_timeout)
        or wait_timeout <= 0
    ):
        raise ValueError("Kernel startup timeout must be finite and positive")
    if provider is not None and not isinstance(provider, str):
        raise TypeError("provider must be text or None")
    if model is not None and not isinstance(model, str):
        raise TypeError("model must be text or None")

    private_root = _ensure_private_directory(Path(root))
    session_id = uuid.uuid4().hex
    directory = _ensure_private_directory(private_root / session_id)
    connection_file = directory / _CONNECTION_NAME
    log_file = directory / _LOG_NAME
    command_arguments = [
        str(connection_file) if item == CONNECTION_FILE_PLACEHOLDER else item
        for item in command
    ]
    if CONNECTION_FILE_PLACEHOLDER not in command:
        _remove_session_directory(private_root, session_id)
        raise ValueError("Kernel command must include the connection-file placeholder")
    log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    process: subprocess.Popen[bytes] | None = None
    try:
        with os.fdopen(log_fd, "wb", closefd=True) as log:
            process = subprocess.Popen(
                command_arguments,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=os.fspath(cwd) if cwd is not None else None,
                close_fds=True,
                start_new_session=True,
                umask=0o077,
            )
        deadline = time.monotonic() + float(wait_timeout)
        identity = None
        while time.monotonic() < deadline:
            identity = _process_identity(process.pid)
            if identity is not None:
                break
            if process.poll() is not None:
                raise KernelSessionError(f"Kernel process exited during startup (status {process.returncode})")
            time.sleep(0.02)
        if identity is None:
            raise KernelSessionError("Could not establish a safe kernel process identity")
        _wait_for_connection_file(process, connection_file, max(0.05, deadline - time.monotonic()))
        checker = wait_ready or _wait_for_kernel_ready
        checker(connection_file, max(0.05, deadline - time.monotonic()))
        # Validate and re-check after the child has completed setup.
        _read_connection_file(connection_file)
        if process.poll() is not None:
            raise KernelSessionError(f"Kernel process exited during startup (status {process.returncode})")
        final_identity = _process_identity(process.pid)
        if final_identity is None or final_identity[0] != identity[0]:
            raise KernelSessionError("Kernel process identity changed during startup")
        record = _Record(
            session_id, process.pid, identity[0], time.time(), connection_file, log_file,
            provider, model,
        )
        _write_record(private_root, record)
        return record.session(running=True)
    except BaseException:
        if process is not None:
            _terminate_spawned(process)
        _remove_session_directory(private_root, session_id)
        raise


def list_sessions(
    *,
    root: Path = DEFAULT_SESSION_ROOT,
    cleanup_stale: bool = True,
) -> tuple[KernelSession, ...]:
    """List only owned sessions; dead or PID-reused records are safely removed."""
    root = _absolute(Path(root))
    _check_no_symlink_components(root)
    if not root.exists():
        return ()
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise KernelSessionError(f"Managed kernel directory must be owned and private (mode 0700): {root}")
    sessions = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            raise KernelSessionError(f"Unexpected entry in private kernel directory: {directory.name}")
        if not _SESSION_ID.fullmatch(directory.name):
            raise KernelSessionError(f"Unexpected entry in private kernel directory: {directory.name}")
        record = _load_or_recover_record(root, directory.name)
        if record is None:
            continue
        if not _process_matches(record):
            if cleanup_stale:
                _remove_session_directory(root, record.session_id)
            continue
        # Validate a completed connection file but never expose its key. A
        # recovered process may still be in startup before writing the file.
        connection_ready = record.connection_file.is_file()
        if connection_ready:
            _read_connection_file(record.connection_file)
        sessions.append(record.session(running=True, connection_ready=connection_ready))
    return tuple(sessions)


def get_session(session_id: str, *, root: Path = DEFAULT_SESSION_ROOT) -> KernelSession:
    """Return a live owned session without exposing connection-file contents."""
    root = _absolute(Path(root))
    _check_no_symlink_components(root)
    if not root.exists():
        raise KernelSessionError(f"Unknown managed kernel session: {session_id}")
    record = _load_or_recover_record(root, session_id)
    if record is None:
        raise KernelSessionError(f"Kernel session is stale and has been cleaned up: {session_id}")
    if not _process_matches(record):
        _remove_session_directory(root, session_id)
        raise KernelSessionError(f"Kernel session is stale and has been cleaned up: {session_id}")
    if not record.connection_file.is_file():
        raise KernelSessionError(f"Kernel session is still starting: {session_id}")
    _read_connection_file(record.connection_file)
    return record.session(running=True)


def _send_shutdown_request(record: _Record, timeout: float) -> bool:
    try:
        from jupyter_client.blocking.client import BlockingKernelClient
    except ImportError:
        return False
    client = BlockingKernelClient(connection_file=str(record.connection_file))
    try:
        client.load_connection_file()
        client.start_channels()
        client.wait_for_ready(timeout=min(timeout, 5.0))
        request = client.session.msg("shutdown_request", content={"restart": False})
        client.control_channel.send(request)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            reply = client.get_control_msg(timeout=remaining)
            if reply.get("parent_header", {}).get("msg_id") == request["header"]["msg_id"]:
                return True
        return False
    except Exception:
        return False
    finally:
        try:
            client.stop_channels()
        except Exception:
            pass


def _wait_for_exit(record: _Record, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_matches(record):
            return True
        time.sleep(0.05)
    return not _process_matches(record)


def _signal_owned_process(record: _Record, sig: int) -> bool:
    if not _process_matches(record):
        return False
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        raise UnsupportedKernelSessions("Safe managed-kernel stop requires Linux pidfd signal support")
    try:
        descriptor = pidfd_open(record.pid, 0)
    except ProcessLookupError:
        return False
    try:
        # The pidfd pins a process identity, and this second check makes sure it
        # is still the PID/start-time/command recorded by this session.
        if not _process_matches(record):
            return False
        try:
            pidfd_send_signal(descriptor, sig)
        except ProcessLookupError:
            return False
        return True
    finally:
        os.close(descriptor)


_LOCAL_WORKER_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from py_agent.local_worker import main; main()"
)


def _matches_local_worker_bootstrap(process: _ProcInfo, runtime_parent: str) -> bool:
    command = process.command
    return (
        len(command) == 5
        and bool(command[0])
        and command[1] == "-I"
        and command[2] == "-c"
        and command[3] == _LOCAL_WORKER_BOOTSTRAP
        and command[4] == runtime_parent
    )


def _identify_executor_worker(record: _Record) -> _ProcInfo:
    """Find the kernel's direct child only if it runs our pinned worker bootstrap."""
    _require_linux_process_identity()
    runtime_parent = str(Path(__file__).resolve().parent.parent)
    worker_module = Path(runtime_parent) / "py_agent" / "local_worker.py"
    if not worker_module.is_file():
        raise KernelSessionError("The installed py_agent.local_worker bootstrap is unavailable")

    matches = [
        process for process in _iter_proc_infos(strict=True)
        if process.uid == os.getuid()
        and process.parent_pid == record.pid
        and _matches_local_worker_bootstrap(process, runtime_parent)
    ]
    if len(matches) != 1:
        raise KernelSessionError(
            "Could not uniquely verify the kernel's direct py_agent.local_worker child"
        )
    worker = matches[0]
    if worker.process_group != worker.pid or worker.session_id != worker.pid:
        raise KernelSessionError("The verified executor worker is not in its own process session")
    return worker


def _pin_executor_worker(record: _Record, worker: _ProcInfo) -> _PinnedWorker:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        raise UnsupportedKernelSessions("Safe managed-kernel stop requires Linux pidfd signal support")
    try:
        descriptor = pidfd_open(worker.pid, 0)
    except OSError as exc:
        raise KernelSessionError("Could not safely pin the verified executor worker") from exc
    runtime_parent = str(Path(__file__).resolve().parent.parent)
    current = _proc_info(worker.pid, strict=True)
    if (
        current is None
        or not _same_proc_instance(worker, current)
        or current.parent_pid != record.pid
        or current.process_group != worker.pid
        or current.session_id != worker.pid
        or not _matches_local_worker_bootstrap(current, runtime_parent)
    ):
        os.close(descriptor)
        raise KernelSessionError("Executor worker identity changed before forced stop")
    return _PinnedWorker(worker, descriptor)


def _same_proc_instance(expected: _ProcInfo, current: _ProcInfo) -> bool:
    # Command and parent can legitimately change (exec/reparenting); start ticks
    # plus the dedicated worker process group pin the process we inspected.
    return (
        expected.pid == current.pid
        and expected.uid == current.uid
        and expected.start_ticks == current.start_ticks
        and expected.process_group == current.process_group
        and expected.session_id == current.session_id
    )


def _pidfd_signal_process(
    expected: _ProcInfo,
    sig: int,
    *,
    validator: Callable[[_ProcInfo], bool],
) -> bool:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        raise UnsupportedKernelSessions("Safe managed-kernel stop requires Linux pidfd signal support")
    try:
        descriptor = pidfd_open(expected.pid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise KernelSessionError(f"Could not safely pin process {expected.pid} before signaling") from exc
    try:
        current = _proc_info(expected.pid, read_command=False, strict=True)
        if (
            current is None
            or current.state in ("Z", "X")
            or not _same_proc_instance(expected, current)
            or not validator(current)
        ):
            return False
        try:
            pidfd_send_signal(descriptor, sig)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise KernelSessionError(f"Could not safely signal verified process {expected.pid}") from exc
        return True
    finally:
        os.close(descriptor)


def _worker_session_processes(worker: _ProcInfo) -> tuple[_ProcInfo, ...]:
    return tuple(
        process for process in _iter_proc_infos(read_command=False, strict=True)
        if process.uid == worker.uid
        and process.session_id == worker.pid
        and process.state not in ("Z", "X")
    )


def _signal_executor_worker_group(
    worker: _ProcInfo,
    sig: int,
    *,
    worker_pidfd: int | None = None,
) -> None:
    """Signal the worker's isolated session, including descendants in other groups."""
    for process in _worker_session_processes(worker):
        if process.pid == worker.pid and worker_pidfd is not None:
            current = _proc_info(worker.pid, read_command=False, strict=True)
            if (
                current is None
                or current.state in ("Z", "X")
                or not _same_proc_instance(worker, current)
                or current.session_id != worker.pid
            ):
                continue
            try:
                signal.pidfd_send_signal(worker_pidfd, sig)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise KernelSessionError("Could not signal the pinned executor worker") from exc
            continue
        _pidfd_signal_process(
            process,
            sig,
            validator=lambda current: (
                current.uid == worker.uid
                and current.session_id == worker.pid
            ),
        )


def _wait_for_forced_exit(
    record: _Record,
    worker: _ProcInfo,
    timeout: float,
    sig: int,
    *,
    worker_pidfd: int | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        # Repeat signaling the isolated session to catch grandchildren forked
        # during TERM handling. Each process is targeted only via its pidfd.
        _signal_executor_worker_group(worker, sig, worker_pidfd=worker_pidfd)
        if not _process_matches(record) and not _worker_session_processes(worker):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def stop_session(
    session_id: str,
    *,
    root: Path = DEFAULT_SESSION_ROOT,
    shutdown_timeout: float = 5.0,
) -> bool:
    """Ask gracefully first; forced stop targets only verified kernel/worker identities."""
    if (
        type(shutdown_timeout) not in (int, float)
        or not math.isfinite(shutdown_timeout)
        or shutdown_timeout <= 0
    ):
        raise ValueError("Kernel shutdown timeout must be finite and positive")
    root = _absolute(Path(root))
    _check_no_symlink_components(root)
    if not root.exists():
        raise KernelSessionError(f"Unknown managed kernel session: {session_id}")
    record = _load_or_recover_record(root, session_id)
    if record is None:
        return False
    if not _process_matches(record):
        _remove_session_directory(root, session_id)
        return False

    _send_shutdown_request(record, float(shutdown_timeout))
    stopped = _wait_for_exit(record, float(shutdown_timeout))
    if not stopped:
        try:
            # Resolve and verify the child before signaling either process. The
            # LocalExecutor worker has a separate session/process group, so the
            # kernel's PID alone does not own its execution tree.
            worker = _identify_executor_worker(record)
            pinned_worker = _pin_executor_worker(record, worker)
            try:
                _signal_executor_worker_group(
                    worker, signal.SIGTERM, worker_pidfd=pinned_worker.pidfd
                )
                _signal_owned_process(record, signal.SIGTERM)
                stopped = _wait_for_forced_exit(
                    record, worker, 3.0, signal.SIGTERM,
                    worker_pidfd=pinned_worker.pidfd,
                )
                if not stopped:
                    _signal_executor_worker_group(
                        worker, signal.SIGKILL, worker_pidfd=pinned_worker.pidfd
                    )
                    _signal_owned_process(record, signal.SIGKILL)
                    stopped = _wait_for_forced_exit(
                        record, worker, 2.0, signal.SIGKILL,
                        worker_pidfd=pinned_worker.pidfd,
                    )
            finally:
                os.close(pinned_worker.pidfd)
        except KernelSessionError as exc:
            raise KernelSessionError(
                f"Could not safely stop kernel {session_id}; its private session record was retained: {exc}"
            ) from exc
    if not stopped:
        raise KernelSessionError(
            f"Kernel or executor worker for {session_id} did not stop; its private session record was retained"
        )
    _remove_session_directory(root, session_id)
    return True


def require_jupyter_runtime(*, console: bool = False) -> None:
    """Fail explicitly when optional Jupyter protocol/client dependencies are absent."""
    missing = []
    for module in ("ipykernel", "jupyter_client"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if console:
        try:
            __import__("jupyter_console")
        except ImportError:
            missing.append("jupyter-console")
    if missing:
        if "jupyter-console" in missing:
            raise UnsupportedKernelSessions(
                "Terminal attach requires the optional stock jupyter-console package; install it separately "
                "alongside py-agent[jupyter]"
            )
        raise UnsupportedKernelSessions(
            "Jupyter kernel management requires the optional 'ipykernel' and 'jupyter_client' packages; "
            "install py-agent[jupyter]"
        )


def kernel_argv() -> list[str]:
    """Pin the installed runtime, not a notebook's working-directory import."""
    runtime_parent = str(Path(__file__).resolve().parent.parent)
    bootstrap = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from py_agent.jupyter_kernel import main; raise SystemExit(main(sys.argv[2:]))"
    )
    return [sys.executable, "-I", "-c", bootstrap, runtime_parent]


def install_kernelspec(
    launch_arguments: Sequence[str],
    *,
    name: str = "py-agent",
    display_name: str = "py-agent",
) -> str:
    """Install a user kernelspec which launches this environment's adapter."""
    require_jupyter_runtime()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("Kernelspec name may contain only letters, digits, '.', '_' and '-'")
    if not isinstance(display_name, str) or not display_name.strip() or "\x00" in display_name:
        raise ValueError("Kernelspec display name must be nonempty text")
    if any(not isinstance(value, str) or "\x00" in value for value in launch_arguments):
        raise ValueError("Kernelspec arguments must be NUL-free text")
    try:
        from jupyter_client.kernelspec import KernelSpecManager
    except ImportError as exc:
        raise UnsupportedKernelSessions(
            "Kernelspec installation requires 'jupyter_client'; install py-agent[jupyter]"
        ) from exc

    argv = [*kernel_argv(), *launch_arguments, "-f", CONNECTION_FILE_PLACEHOLDER]
    with tempfile.TemporaryDirectory(prefix="py-agent-kernelspec-") as temporary:
        directory = Path(temporary)
        spec = {
            "argv": argv,
            "display_name": display_name,
            "language": "python",
            "interrupt_mode": "signal",
            "metadata": {"debugger": True},
        }
        (directory / "kernel.json").write_text(
            json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            return KernelSpecManager().install_kernel_spec(
                str(directory), kernel_name=name, user=True, replace=False,
            )
        except Exception as exc:
            raise KernelSessionError(f"Could not install user kernelspec {name!r}: {exc}") from exc
