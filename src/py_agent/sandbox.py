"""Fail-closed Linux srt launcher.

The installed srt CLI requires a domain-filtered network configuration.  It
cannot express transparent open networking.  ``network='open'`` therefore
fails explicitly; ``network='proxy'`` is an opt-in to a different policy.

Only inherited stdin/stdout/stderr cross the launcher boundary.  The worker
must reserve its transport descriptors before redirecting cell standard IO.
This module does not execute generated source. Workers inherit OS resource
limits; the application does not impose execution or session resource quotas.
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import platform
import shutil
import signal
import stat
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Literal


class SandboxUnavailable(RuntimeError):
    """Required isolation or the requested network policy is unavailable."""


# Trusted, fixed startup probe. No model-provided text enters this program.
_PROBE = r'''
import errno, json, os, pathlib, socket, sys
scratch, protected, outside, launcher_tmp = map(pathlib.Path, sys.argv[1:])
checks = {}
p = scratch / 'probe-write'
p.write_text('inside')
checks['inside_write'] = p.read_text() == 'inside'
p.unlink()
checks['read_protected'] = protected.read_text() == 'protected'
def denied(path):
    try:
        with path.open('w') as f:
            f.write('ESCAPED')
    except OSError as e:
        return e.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
    return False
checks['protected_write_denied'] = denied(protected)
checks['outside_write_denied'] = denied(outside)
checks['launcher_tmp_write_denied'] = denied(launcher_tmp / 'worker-must-not-write')
import tempfile
with tempfile.TemporaryDirectory(prefix='py-write-probe-', dir='/tmp') as directory:
    temporary = pathlib.Path(directory) / 'read-write'
    temporary.write_text('tmp is writable')
    checks['tmp_read_write'] = temporary.read_text() == 'tmp is writable'
checks['worker_tmp_in_workspace'] = (pathlib.Path(tempfile.gettempdir()) == scratch / 'tmp'
    and all(os.environ.get(k) == str(scratch / 'tmp') for k in ('TMPDIR', 'TMP', 'TEMP')))
link = scratch / 'probe-symlink'
try:
    link.symlink_to(outside)
    checks['symlink_write_denied'] = denied(link)
finally:
    link.unlink(missing_ok=True)
p.write_text('inside')
try:
    os.replace(p, outside)
    checks['rename_denied'] = False
except OSError as e:
    checks['rename_denied'] = e.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.EXDEV)
finally:
    p.unlink(missing_ok=True)
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.close()
    checks['unix_socket_denied'] = False
except OSError as e:
    checks['unix_socket_denied'] = e.errno in (errno.EACCES, errno.EPERM)
checks['stdio'] = sys.stdin.buffer.readline() == b'py-srt-stdio-probe\n'
checks['no_credentials'] = not any(k in os.environ for k in (
    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'SSH_AUTH_SOCK', 'DOCKER_HOST',
    'PYTHONPATH', 'PYTHONSTARTUP', 'NODE_OPTIONS', 'BASH_ENV', 'ENV'))
print(json.dumps(checks), flush=True)
'''


def _absolute_no_symlinks(path: Path) -> Path:
    """Reject path redirection instead of silently resolving protected paths."""
    result = Path(os.path.abspath(path))
    for part in (result, *result.parents):
        if part.is_symlink():
            raise SandboxUnavailable(f"Sandbox path must not contain symlinks: {part}")
    return result


def _private_directory(path: Path) -> None:
    _absolute_no_symlinks(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SandboxUnavailable(f"Directory must be owned by the current user: {path}")
    if info.st_mode & 0o077:
        raise SandboxUnavailable(f"Directory must be private (chmod 700): {path}")


def _verify_runtime_tree(roots: set[Path], protected: set[Path], *, max_entries: int = 100000) -> None:
    """Reject inode aliases that pathname read-only mounts cannot protect.

    Symlinks may only resolve into already protected runtime paths. Their targets
    are scanned as well, including targets outside the originally walked roots.
    This is a bounded startup check, not protection against concurrent trusted
    host processes replacing the installation during launch.
    """
    pending = list(roots)
    seen: set[tuple[int, int]] = set()
    count = 0
    while pending:
        path = pending.pop()
        count += 1
        if count > max_entries:
            raise SandboxUnavailable("Trusted runtime verification exceeded its entry limit; use a smaller dedicated runtime")
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                if not any(target.is_relative_to(root) for root in protected):
                    raise SandboxUnavailable(f"Trusted runtime symlink escapes protected paths: {path} -> {target}")
                pending.append(target)
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise SandboxUnavailable(
                    f"Trusted runtime has a hardlinked file: {path}. Read-only path mounts cannot protect writable aliases; "
                    "reinstall into a dedicated runtime using uv --link-mode copy."
                )
            key = (info.st_dev, info.st_ino)
            if key in seen:
                continue
            seen.add(key)
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        pending.append(Path(entry.path))
                        if len(pending) + count > max_entries:
                            raise SandboxUnavailable("Trusted runtime verification exceeded its entry limit; use a smaller dedicated runtime")
            elif not stat.S_ISREG(info.st_mode):
                raise SandboxUnavailable(f"Unsupported file type in trusted runtime: {path}")
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, SandboxUnavailable):
                raise
            raise SandboxUnavailable(f"Cannot verify trusted runtime path {path}: {exc}") from exc


class Sandbox:
    """One persistent sandboxed worker; instances are not reusable after close.

    ``allowed_domains`` applies only to explicitly selected proxy networking.
    An empty tuple denies all network destinations. DNS, UDP and transparent
    localhost access are NOT supplied by this proxy mode.

    ``start()`` first runs a fixed disposable isolation/stdio probe. Failure
    raises ``SandboxUnavailable`` before any worker/model source is run.
    ``interrupt()`` destroys the kernel (partial effects remain), as does
    ``close()``. There is deliberately no unsafe/unsandboxed fallback.
    """

    def __init__(
        self,
        workspace: Path,
        host_dir: Path,
        scratch: Path,
        *,
        network: Literal['open', 'proxy'] = 'open',
        allowed_domains: tuple[str, ...] = (),
        startup_timeout: float = 20.0,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise SandboxUnavailable("Workspace must be an existing directory")
        self.write_roots = tuple(dict.fromkeys((self.workspace, Path('/tmp'))))
        self.host_dir = _absolute_no_symlinks(host_dir)
        self.scratch = _absolute_no_symlinks(scratch)
        if not self.scratch.is_relative_to(self.workspace):
            raise SandboxUnavailable("Scratch must be inside the anchored workspace")
        if self.scratch == self.workspace:
            raise SandboxUnavailable("Use a private scratch subdirectory, not the workspace itself")
        if self.scratch.is_relative_to(self.host_dir) or self.host_dir.is_relative_to(self.scratch):
            raise SandboxUnavailable("Host control files and writable scratch must be disjoint")
        if network not in ('open', 'proxy'):
            raise ValueError("network must be 'open' or 'proxy'")
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        if isinstance(allowed_domains, (str, bytes)) or not all(isinstance(d, str) and d and '\x00' not in d for d in allowed_domains):
            raise ValueError("allowed_domains must contain nonempty domain strings")
        self.network = network
        self.allowed_domains = tuple(allowed_domains)
        self.startup_timeout = startup_timeout
        self.process: asyncio.subprocess.Process | None = None
        self._probe_process: asyncio.subprocess.Process | None = None
        self.preflight_result: dict[str, bool] | None = None
        self._settings: Path | None = None
        self._launcher_tmp: tempfile.TemporaryDirectory | None = None
        self._closed = False
        self._starting = False
        self._srt: str | None = None
        self._env: dict[str, str] | None = None
        self._runtime_protected: set[Path] = set()
        self._runtime_scan_roots: set[Path] = set()

    @property
    def policy(self) -> dict:
        return {
            'write_roots': [str(root) for root in self.write_roots],
            'reads': 'all normally readable files (not confidentiality isolation)',
            'network': self.network,
            'allowed_domains': list(self.allowed_domains),
            'network_limitations': 'proxy only: no transparent DNS/UDP/localhost' if self.network == 'proxy' else 'unsupported by current srt CLI',
            'broad_root_warning': self.workspace in (Path('/'), Path.home().resolve()),
            'mandatory_srt_write_protections': True,
            'interrupt': 'kills launcher group; descendant cleanup relies on srt PID namespaces and remains integration-unverified; no rollback or replay',
            'resource_limits': 'inherited OS limits only; no application execution/session quotas',
        }

    def _prepare(self) -> None:
        if platform.system() != 'Linux':
            raise SandboxUnavailable("Only Linux srt isolation is implemented")
        if self.network == 'open':
            raise SandboxUnavailable(
                "Requested open networking is unavailable through the supported srt CLI: "
                "its schema requires allowedDomains and always isolates the network namespace. "
                "No weaker isolation was enabled. Explicitly choose network='proxy' and "
                "allowed_domains after accepting that DNS/UDP/localhost are not transparent."
            )
        for command in ('srt', 'bwrap', 'socat', 'rg', 'node'):
            if not shutil.which(command):
                raise SandboxUnavailable(f"Required sandbox executable not found: {command}")
        self._srt = str(Path(shutil.which('srt')).resolve())
        _private_directory(self.host_dir)
        _private_directory(self.scratch)
        for name in ('home', 'tmp', 'ipython', 'cache'):
            _private_directory(self.scratch / name)
        # srt creates Unix-domain bridge sockets beneath Node's os.tmpdir().
        # Session/workspace paths readily exceed Linux's 107-byte pathname limit.
        # This is host-only control-plane scratch, NOT a worker write permission.
        self._launcher_tmp = tempfile.TemporaryDirectory(prefix='py-srt-', dir='/tmp')
        launcher_tmp = _absolute_no_symlinks(Path(self._launcher_tmp.name))
        _private_directory(launcher_tmp)
        if len(os.fsencode(str(launcher_tmp / ('claude-socks-' + '0' * 16 + '.sock')))) > 107:
            raise SandboxUnavailable("Host bridge socket directory exceeds Linux's Unix-socket path limit")
        # No inherited PATH, Python/Node startup configuration, proxy credentials,
        # API keys, SSH agent, Docker endpoint, or unrelated file descriptors.
        self._env = {
            'PATH': '/usr/bin:/bin',
            'HOME': str(self.scratch / 'home'),
            'TMPDIR': str(launcher_tmp),
            'TMP': str(launcher_tmp),
            'TEMP': str(launcher_tmp),
            'CLAUDE_CODE_TMPDIR': str(self.scratch / 'tmp'),
            'IPYTHONDIR': str(self.scratch / 'ipython'),
            'XDG_CACHE_HOME': str(self.scratch / 'cache'),
            'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8',
            'TERM': 'dumb',
            'PAGER': '/bin/cat',
            'GIT_PAGER': '/bin/cat',
        }
        # An editable install trusts the entire import root (src), not just its
        # package: a sibling sitecustomize.py can run during the next startup.
        # Base interpreter libraries remain dependencies even inside a venv.
        source_root = Path(__file__).resolve().parent.parent
        prefixes = {Path(sys.prefix).absolute(), Path(sys.base_prefix).absolute()}
        executable_paths = {Path(shutil.which(command)).resolve() for command in ('srt', 'bwrap', 'socat', 'rg', 'node')}
        executable_paths.add(Path(sys.executable).resolve())
        srt_root = Path(self._srt).parent.parent
        self._runtime_protected = {
            source_root, *prefixes, *(p.resolve() for p in prefixes),
            srt_root, *executable_paths, *(p.parent for p in executable_paths),
        }
        # Never recursively scan /usr or /usr/local merely because they are the
        # system prefix. Inspect actual Python library/import paths and binaries.
        self._runtime_scan_roots = {source_root, srt_root, *executable_paths}
        for name in ('stdlib', 'platstdlib', 'purelib', 'platlib'):
            path = sysconfig.get_path(name)
            if path:
                library = Path(path).absolute()
                self._runtime_scan_roots.add(library)
                self._runtime_protected.update({library, library.resolve()})
        if sys.prefix != sys.base_prefix:
            self._runtime_scan_roots.add(Path(sys.prefix).absolute())
        protected = self._runtime_protected | {self.host_dir}
        denied = sorted(str(p) for p in protected
                        if any(p.is_relative_to(root) for root in self.write_roots))
        for host_path in (self.host_dir, launcher_tmp):
            if str(host_path) not in denied:
                denied.append(str(host_path))
        settings = {
            'network': {
                'allowedDomains': list(self.allowed_domains),
                'deniedDomains': [],
                'strictAllowlist': True,
                'allowAllUnixSockets': False,
                'allowLocalBinding': False,
            },
            'filesystem': {
                'denyRead': [],
                'allowWrite': [str(root) for root in self.write_roots],
                'denyWrite': denied,
            },
            'enableWeakerNestedSandbox': False,
            'enableWeakerNetworkIsolation': False,
            'allowAppleEvents': False,
        }
        fd, name = tempfile.mkstemp(prefix='srt-', suffix='.json', dir=self.host_dir)
        self._settings = Path(name)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(settings, stream)
            stream.flush()
            os.fsync(stream.fileno())

    def _verify_runtime(self) -> None:
        """Required before any preflight or worker process is spawned."""
        if not self._runtime_scan_roots:
            raise SandboxUnavailable("Runtime protection has not been prepared")
        if self._runtime_scan_roots.intersection({Path('/'), Path('/usr'), Path('/usr/local')}):
            raise SandboxUnavailable("Cannot bound this runtime layout safely; use a dedicated Python/srt installation rather than scanning a system prefix")
        _verify_runtime_tree(self._runtime_scan_roots, self._runtime_protected)

    async def _spawn(self, argv: list[str]) -> asyncio.subprocess.Process:
        assert self._srt and self._settings and self._env
        return await asyncio.create_subprocess_exec(
            self._srt, '--settings', str(self._settings), '--',
            '/usr/bin/env', *(f'{key}={self.scratch / "tmp"}' for key in ('TMPDIR', 'TMP', 'TEMP')), *argv,
            cwd=self.workspace, env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            close_fds=True, start_new_session=True,
            limit=1024 * 1024,
        )

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        # srt itself does not forward signals to all descendants. Kill its whole
        # original session group. bwrap uses --die-with-parent and PID namespaces;
        # killing its launching shell kills the namespace even if cells setsid().
        # This relies on the intended srt/bwrap topology, not process-group
        # membership of arbitrary grandchildren. Actual detached-child cleanup
        # remains unverified until the opt-in integration test passes on a host.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise SandboxUnavailable("Sandbox process tree did not terminate promptly") from exc

    async def preflight(self) -> dict[str, bool]:
        """Run trusted disposable confinement and standard-descriptor probes."""
        if self._closed:
            raise SandboxUnavailable("Sandbox is closed")
        if self._settings is None:
            self._prepare()
        self._verify_runtime()
        # Protected sentinel deliberately remains readable but must not be writable.
        fd, name = tempfile.mkstemp(prefix='protected-probe-', dir=self.host_dir)
        protected = Path(name)
        with os.fdopen(fd, 'w') as stream:
            stream.write('protected')
        external: tempfile.TemporaryDirectory | None = None
        try:
            # /tmp is writable. Probe outside both write roots when possible;
            # for broad roots, use the explicitly protected host sentinel instead.
            outside_root = self.host_dir.parent
            outside = protected
            if not any(outside_root.is_relative_to(root) for root in self.write_roots):
                try:
                    external = tempfile.TemporaryDirectory(prefix='py-srt-outside-', dir=outside_root)
                except OSError as exc:
                    # An owned host directory can have a root-owned/read-only
                    # parent (e.g. /var/lib). Its sentinel still tests protection.
                    if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                        raise
                else:
                    outside = Path(external.name) / 'sentinel'
                    outside.write_text('outside')
            process = await self._spawn([
                sys.executable, '-I', '-c', _PROBE,
                str(self.scratch), str(protected), str(outside), self._launcher_tmp.name,
            ])
            self._probe_process = process
            if self._closed:
                await self._terminate(process)
                raise SandboxUnavailable("Sandbox closed during preflight")
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(b'py-srt-stdio-probe\n'), self.startup_timeout
                )
            except BaseException:
                await self._terminate(process)
                raise
            if process.returncode != 0:
                detail = stderr.decode('utf-8', 'replace')[-2000:]
                raise SandboxUnavailable(f"srt isolation preflight failed (exit {process.returncode}): {detail}")
            try:
                result = json.loads(stdout)
            except (ValueError, UnicodeError) as exc:
                raise SandboxUnavailable("srt did not preserve a clean stdout transport") from exc
            expected = {
                'inside_write', 'read_protected', 'protected_write_denied',
                'outside_write_denied', 'symlink_write_denied', 'rename_denied',
                'unix_socket_denied', 'stdio', 'no_credentials',
                'launcher_tmp_write_denied', 'worker_tmp_in_workspace', 'tmp_read_write',
            }
            if not isinstance(result, dict) or set(result) != expected or any(v is not True for v in result.values()):
                raise SandboxUnavailable(f"Sandbox enforcement probe failed: {result!r}")
            if protected.read_text() != 'protected':
                raise SandboxUnavailable("Sandbox modified its protected control-plane sentinel")
            self.preflight_result = result
            return result.copy()
        except asyncio.TimeoutError as exc:
            raise SandboxUnavailable("srt isolation preflight timed out") from exc
        finally:
            self._probe_process = None
            protected.unlink(missing_ok=True)
            if external is not None:
                external.cleanup()

    async def start(self) -> asyncio.subprocess.Process:
        if self._closed or self._starting or self.process is not None:
            raise SandboxUnavailable("Sandbox already started or closed")
        self._starting = True
        try:
            await self.preflight()
            if self._closed:
                raise SandboxUnavailable("Sandbox closed during startup")
            worker = Path(__file__).resolve().with_name('worker.py')
            if not worker.is_file():
                raise SandboxUnavailable(f"Worker bootstrap is missing: {worker}")
            self.process = await self._spawn([sys.executable, '-I', str(worker)])
            if self._closed:
                await self._terminate(self.process)
                raise SandboxUnavailable("Sandbox closed during startup")
            # Readiness and framed IPC validation belong to the supervisor. Do not
            # consume even one worker byte here: it is part of the protocol.
            return self.process
        except BaseException:
            await self.close()
            raise
        finally:
            self._starting = False

    async def interrupt(self) -> None:
        """Terminate the kernel; never replay a potentially partial cell."""
        await self.close()

    async def close(self) -> None:
        self._closed = True
        if self._probe_process is not None:
            await self._terminate(self._probe_process)
        if self.process is not None:
            await self._terminate(self.process)
            self.process = None
        if self._settings is not None:
            self._settings.unlink(missing_ok=True)
            self._settings = None
        if self._launcher_tmp is not None:
            self._launcher_tmp.cleanup()
            self._launcher_tmp = None

    async def __aenter__(self) -> 'Sandbox':
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
