from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from py_agent.sandbox import Sandbox, SandboxUnavailable, _verify_runtime_tree


@pytest.fixture(autouse=True)
def host_tempdirs(request, tmp_path, monkeypatch):
    """Relocate mocked launcher's host scratch, never the real srt tests."""
    if request.node.get_closest_marker('sandbox'):
        yield {}
        return

    requested_paths = {}
    directories = []

    def temporary_directory(*, prefix, dir):
        if prefix == 'py-srt-':
            assert dir == '/tmp'  # bridge sockets must use short host paths
        else:
            assert prefix == 'py-srt-outside-' or prefix == 'x' * 100
        directory = tempfile.TemporaryDirectory(prefix=prefix, dir=tmp_path)
        directories.append(directory)
        requested_paths[Path(directory.name)] = Path(dir) / Path(directory.name).name
        return directory

    def fsencode(path):
        # _prepare checks sockaddr_un length. Represent relocated scratch as its
        # requested host pathname for this check; actual IO stays under tmp_path.
        path = os.fspath(path)
        if isinstance(path, str):
            for actual, requested in requested_paths.items():
                if Path(path).is_relative_to(actual):
                    path = str(requested / Path(path).relative_to(actual))
                    break
        return os.fsencode(path)

    monkeypatch.setattr('py_agent.sandbox.tempfile', SimpleNamespace(
        TemporaryDirectory=temporary_directory, mkstemp=tempfile.mkstemp,
    ))
    # Replace only the sandbox module's reference, not the process-wide os module.
    monkeypatch.setattr('py_agent.sandbox.os', SimpleNamespace(**(vars(os) | {'fsencode': fsencode})))
    try:
        yield requested_paths
    finally:
        for directory in reversed(directories):
            directory.cleanup()


def build(tmp_path, **kwargs):
    root = tmp_path / 'workspace'
    root.mkdir(exist_ok=True)
    return Sandbox(root, tmp_path / 'host', root / 'scratch', **kwargs)


def tools(monkeypatch):
    monkeypatch.setattr('py_agent.sandbox.platform.system', lambda: 'Linux')
    monkeypatch.setattr('py_agent.sandbox.shutil.which', lambda command: '/usr/bin/' + command)


def test_open_network_fails_closed_without_spawning(tmp_path, monkeypatch):
    sandbox = build(tmp_path)
    called = []
    monkeypatch.setattr('py_agent.sandbox.shutil.which', lambda command: called.append(command))
    with pytest.raises(SandboxUnavailable, match='open networking is unavailable'):
        asyncio.run(sandbox.start())
    assert not called
    assert sandbox.process is None
    assert not sandbox.host_dir.exists()


def test_proxy_config_is_explicit_private_and_no_weaker_isolation(tmp_path, monkeypatch):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy', allowed_domains=('example.com',))
    sandbox._prepare()
    config = json.loads(sandbox._settings.read_text())
    assert config['network'] == {
        'allowedDomains': ['example.com'], 'deniedDomains': [],
        'strictAllowlist': True, 'allowAllUnixSockets': False, 'allowLocalBinding': False,
    }
    assert config['filesystem']['allowWrite'] == [str(sandbox.workspace), '/tmp']
    assert sandbox.policy['write_roots'] == [str(sandbox.workspace), '/tmp']
    assert config['filesystem']['denyRead'] == []
    assert str(sandbox.host_dir) in config['filesystem']['denyWrite']
    assert config['enableWeakerNestedSandbox'] is False
    assert config['enableWeakerNetworkIsolation'] is False
    assert sandbox._settings.stat().st_mode & 0o777 == 0o600
    assert sandbox.scratch.stat().st_mode & 0o777 == 0o700
    settings = sandbox._settings
    asyncio.run(sandbox.close())
    assert not settings.exists()


def test_runtime_and_venv_inside_workspace_are_protected(tmp_path, monkeypatch):
    tools(monkeypatch)
    root = tmp_path / 'workspace'
    root.mkdir()
    module = root / 'src' / 'py_agent' / 'sandbox.py'
    module.parent.mkdir(parents=True)
    module.write_text('')
    prefix = root / '.venv'
    prefix.mkdir()
    base = root / 'base-python'
    base.mkdir()
    monkeypatch.setattr('py_agent.sandbox.__file__', str(module))
    monkeypatch.setattr(sys, 'prefix', str(prefix))
    monkeypatch.setattr(sys, 'base_prefix', str(base))
    sandbox = Sandbox(root, root / 'control', root / 'scratch', network='proxy')
    sandbox._prepare()
    denied = json.loads(sandbox._settings.read_text())['filesystem']['denyWrite']
    assert str(module.parent.parent) in denied
    assert str(prefix) in denied
    assert str(base) in denied
    assert str(root / 'control') in denied
    asyncio.run(sandbox.close())


def test_runtime_outside_workspace_but_in_shared_tmp_stays_protected(tmp_path, monkeypatch):
    tools(monkeypatch)
    # Synthetic runtime lives in /tmp but outside this session's workspace.
    # No real files there are accessed by _prepare (runtime scan is separate).
    prefix = Path('/tmp/py-test-venv')
    source = Path('/tmp/py-test-source/src')
    monkeypatch.setattr(sys, 'prefix', str(prefix))
    monkeypatch.setattr('py_agent.sandbox.__file__', str(source / 'py_agent' / 'sandbox.py'))
    sandbox = build(tmp_path, network='proxy')
    try:
        sandbox._prepare()
        policy = json.loads(sandbox._settings.read_text())['filesystem']
        assert '/tmp' in policy['allowWrite']
        assert str(prefix) in policy['denyWrite']
        assert str(source) in policy['denyWrite']
        assert str(sandbox.host_dir) in policy['denyWrite']
        assert sandbox._launcher_tmp.name in policy['denyWrite']
    finally:
        asyncio.run(sandbox.close())


def test_environment_is_allowlist_not_secret_denylist(tmp_path, monkeypatch):
    tools(monkeypatch)
    for key in ('OPENAI_API_KEY', 'UNUSUAL_VENDOR_SECRET', 'SSH_AUTH_SOCK', 'DOCKER_HOST',
                'PYTHONPATH', 'PYTHONSTARTUP', 'NODE_OPTIONS', 'BASH_ENV', 'HTTPS_PROXY'):
        monkeypatch.setenv(key, 'must-not-inherit')
    sandbox = build(tmp_path, network='proxy')
    sandbox._prepare()
    assert all(value != 'must-not-inherit' for value in sandbox._env.values())
    assert sandbox._env['PATH'] == '/usr/bin:/bin'
    assert sandbox._env['HOME'] == str(sandbox.scratch / 'home')
    asyncio.run(sandbox.close())


def test_long_workspace_uses_short_host_sockets_without_granting_writes(tmp_path, monkeypatch, host_tempdirs):
    tools(monkeypatch)
    root = tmp_path / ('long-workspace-' + 'x' * 100)
    root.mkdir()
    scratch = root / '.py' / 'sessions' / ('a' * 32) / 'scratch'
    sandbox = Sandbox(root, tmp_path / 'host', scratch, network='proxy')
    sandbox._prepare()
    launcher_tmp = Path(sandbox._launcher_tmp.name)
    try:
        socket_name = 'claude-socks-' + '0' * 16 + '.sock'
        assert len(os.fsencode(str(scratch / 'tmp' / socket_name))) > 107
        requested = host_tempdirs[launcher_tmp]
        assert requested.parent == Path('/tmp')
        assert requested.name.startswith('py-srt-')
        assert len(os.fsencode(str(requested / socket_name))) <= 107
        assert launcher_tmp.stat().st_mode & 0o777 == 0o700
        assert sandbox._env['TMPDIR'] == str(launcher_tmp)
        assert sandbox._env['CLAUDE_CODE_TMPDIR'] == str(scratch / 'tmp')
        policy = json.loads(sandbox._settings.read_text())['filesystem']
        assert policy['allowWrite'] == [str(root), '/tmp']
        assert str(launcher_tmp) in policy['denyWrite']
    finally:
        asyncio.run(sandbox.close())
    assert not launcher_tmp.exists()


def test_host_tempdirs_are_local_without_changing_stdlib(tmp_path, host_tempdirs):
    import py_agent.sandbox as module

    assert module.tempfile is not tempfile
    assert module.os is not os
    with tempfile.TemporaryDirectory(dir=tmp_path) as ordinary:
        assert Path(ordinary) not in host_tempdirs
    with module.tempfile.TemporaryDirectory(prefix='py-srt-', dir='/tmp') as relocated:
        actual = Path(relocated)
        assert actual.parent == tmp_path
        assert actual.stat().st_mode & 0o777 == 0o700
        expected = host_tempdirs[actual] / 'socket'
        assert module.os.fsencode(actual / 'socket') == os.fsencode(expected)
        assert os.fsencode(actual / 'socket') != os.fsencode(expected)
    assert not actual.exists()
    assert module.os.fsencode(tmp_path / 'unrelated') == os.fsencode(tmp_path / 'unrelated')


def test_oversized_host_bridge_name_still_fails_closed(tmp_path, monkeypatch):
    import py_agent.sandbox as module

    tools(monkeypatch)
    temporary_directory = module.tempfile.TemporaryDirectory
    monkeypatch.setattr(module.tempfile, 'TemporaryDirectory',
                        lambda **kwargs: temporary_directory(**(kwargs | {'prefix': 'x' * 100})))
    sandbox = build(tmp_path, network='proxy')
    try:
        with pytest.raises(SandboxUnavailable, match='socket path limit'):
            sandbox._prepare()
    finally:
        asyncio.run(sandbox.close())


def test_worker_tmp_override_runs_inside_sandbox(tmp_path, monkeypatch):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy')
    sandbox._prepare()
    calls = []
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    try:
        asyncio.run(sandbox._spawn([sys.executable, '-I', '/trusted/worker.py']))
        args, kwargs = calls[0]
        command = args[args.index('--') + 1:]
        assert command == ('/usr/bin/env', *(f'{k}={sandbox.scratch / "tmp"}' for k in ('TMPDIR', 'TMP', 'TEMP')),
                           sys.executable, '-I', '/trusted/worker.py')
        assert kwargs['env']['TMPDIR'] != str(sandbox.scratch / 'tmp')
        assert kwargs['env']['TMPDIR'] == sandbox._launcher_tmp.name
    finally:
        asyncio.run(sandbox.close())


def test_failed_start_removes_host_bridge_directory(tmp_path, monkeypatch):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy')
    sandbox._prepare()
    launcher_tmp = Path(sandbox._launcher_tmp.name)
    def fail_verification():
        raise SandboxUnavailable('deliberate probe failure')
    monkeypatch.setattr(sandbox, '_verify_runtime', fail_verification)
    with pytest.raises(SandboxUnavailable, match='deliberate probe failure'):
        asyncio.run(sandbox.start())
    assert not launcher_tmp.exists()


def test_path_boundaries(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    with pytest.raises(SandboxUnavailable, match='inside'):
        Sandbox(root, tmp_path / 'host', tmp_path / 'outside')
    with pytest.raises(SandboxUnavailable, match='disjoint'):
        Sandbox(root, root / 'scratch' / 'host', root / 'scratch')
    with pytest.raises(SandboxUnavailable, match='private scratch'):
        Sandbox(root, tmp_path / 'host', root)
    link = root / 'link'
    link.symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(SandboxUnavailable, match='symlinks'):
        Sandbox(root, tmp_path / 'host', link / 'scratch')


def test_insecure_host_directory_rejected(tmp_path, monkeypatch):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy')
    sandbox.host_dir.mkdir(mode=0o755)
    with pytest.raises(SandboxUnavailable, match='chmod 700'):
        sandbox._prepare()


def test_missing_prerequisite_fails_closed(tmp_path, monkeypatch):
    tools(monkeypatch)
    monkeypatch.setattr('py_agent.sandbox.shutil.which', lambda command: None if command == 'bwrap' else '/usr/bin/' + command)
    sandbox = build(tmp_path, network='proxy')
    with pytest.raises(SandboxUnavailable, match='bwrap'):
        asyncio.run(sandbox.start())
    assert sandbox.process is None


def test_spawn_uses_only_standard_pipes_and_isolated_python(tmp_path, monkeypatch):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy')
    sandbox._prepare()
    calls = []
    process = object()

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    assert asyncio.run(sandbox._spawn([sys.executable, '-I', '/trusted/worker.py'])) is process
    args, kwargs = calls[0]
    assert args[-3:] == (sys.executable, '-I', '/trusted/worker.py')
    assert '--settings' in args and '--' in args
    assert kwargs['stdin'] == kwargs['stdout'] == kwargs['stderr'] == asyncio.subprocess.PIPE
    assert kwargs['close_fds'] and kwargs['start_new_session']
    assert 'pass_fds' not in kwargs
    assert kwargs['cwd'] == sandbox.workspace
    asyncio.run(sandbox.close())


def result(**changes):
    checks = dict.fromkeys((
        'inside_write', 'read_protected', 'protected_write_denied',
        'outside_write_denied', 'symlink_write_denied', 'rename_denied',
        'unix_socket_denied', 'stdio', 'no_credentials',
        'launcher_tmp_write_denied', 'worker_tmp_in_workspace', 'tmp_read_write',
    ), True)
    checks.update(changes)
    return checks


@pytest.mark.parametrize('payload', [result(unix_socket_denied=False), result(outside_write_denied=False),
                                     result(tmp_read_write=False), {'stdio': True}, []])
def test_preflight_rejects_missing_or_failed_guarantees(tmp_path, monkeypatch, payload):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy')
    monkeypatch.setattr(sandbox, '_verify_runtime', lambda: None)  # synthetic launcher fixture

    async def communicate(data):
        assert data == b'py-srt-stdio-probe\n'
        return json.dumps(payload).encode(), b''

    async def spawn(argv):
        assert argv[1:3] == ['-I', '-c']
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(sandbox, '_spawn', spawn)
    with pytest.raises(SandboxUnavailable, match='probe failed'):
        asyncio.run(sandbox.preflight())
    assert sandbox.preflight_result is None
    asyncio.run(sandbox.close())


def test_preflight_success_requires_exact_true_values(tmp_path, monkeypatch):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy')
    monkeypatch.setattr(sandbox, '_verify_runtime', lambda: None)  # synthetic launcher fixture

    async def communicate(data):
        return json.dumps(result()).encode(), b''

    async def spawn(argv):
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(sandbox, '_spawn', spawn)
    assert asyncio.run(sandbox.preflight()) == result()
    assert not list(sandbox.host_dir.glob('protected-probe-*'))
    asyncio.run(sandbox.close())


def test_preflight_errors_never_start_worker(tmp_path, monkeypatch):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy')
    monkeypatch.setattr(sandbox, '_verify_runtime', lambda: None)  # synthetic launcher fixture
    calls = []

    async def communicate(data):
        return b'', b'namespace creation failed'

    async def spawn(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=1, communicate=communicate)

    monkeypatch.setattr(sandbox, '_spawn', spawn)
    with pytest.raises(SandboxUnavailable, match='namespace creation failed'):
        asyncio.run(sandbox.start())
    assert len(calls) == 1
    assert sandbox.process is None
    assert not list(sandbox.host_dir.glob('srt-*.json'))


def test_timeout_terminates_probe(tmp_path, monkeypatch):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy', startup_timeout=0.01)
    monkeypatch.setattr(sandbox, '_verify_runtime', lambda: None)  # synthetic launcher fixture
    terminated = []
    process = SimpleNamespace(returncode=None)

    async def communicate(data):
        await asyncio.sleep(10)

    process.communicate = communicate

    async def spawn(argv):
        return process

    async def terminate(proc):
        terminated.append(proc)

    monkeypatch.setattr(sandbox, '_spawn', spawn)
    monkeypatch.setattr(sandbox, '_terminate', terminate)
    with pytest.raises(SandboxUnavailable, match='timed out'):
        asyncio.run(sandbox.preflight())
    assert terminated == [process]
    asyncio.run(sandbox.close())


@pytest.mark.parametrize('error_number', [errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC])
def test_preflight_handles_nonwritable_host_parent(tmp_path, monkeypatch, error_number):
    import py_agent.sandbox as module

    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy')
    sandbox._prepare()
    actual_host = sandbox.host_dir
    # Model a private /var/lib/py-agent host root without writing real /var/lib.
    sandbox.host_dir = Path('/var/lib/py-test-host')
    monkeypatch.setattr(sandbox, '_verify_runtime', lambda: None)
    monkeypatch.setattr(module.tempfile, 'mkstemp',
                        lambda **kwargs: tempfile.mkstemp(**(kwargs | {'dir': actual_host})))
    attempts = []

    def unavailable(*, prefix, dir):
        attempts.append((prefix, dir))
        raise OSError(error_number, 'synthetic parent failure')

    async def spawn(argv):
        assert argv[-2] == argv[-3]  # outside probe falls back to protected sentinel
        assert Path(argv[-3]).read_text() == 'protected'
        async def communicate(data):
            return json.dumps(result()).encode(), b''
        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(module.tempfile, 'TemporaryDirectory', unavailable)
    monkeypatch.setattr(sandbox, '_spawn', spawn)
    try:
        if error_number == errno.ENOSPC:
            with pytest.raises(OSError) as caught:
                asyncio.run(sandbox.preflight())
            assert caught.value.errno == errno.ENOSPC
        else:
            assert all(asyncio.run(sandbox.preflight()).values())
        assert attempts == [('py-srt-outside-', Path('/var/lib'))]
        assert not list(actual_host.glob('protected-probe-*'))
    finally:
        asyncio.run(sandbox.close())


def test_interrupt_kills_process_group_not_just_worker_pid(tmp_path, monkeypatch):
    sandbox = build(tmp_path, network='proxy')
    signals = []
    waited = []

    async def wait():
        waited.append(True)
        return -9

    sandbox.process = SimpleNamespace(pid=98765, wait=wait)
    monkeypatch.setattr('py_agent.sandbox.os.killpg', lambda pid, sig: signals.append((pid, sig)))
    asyncio.run(sandbox.interrupt())
    assert signals == [(98765, signal.SIGKILL)]
    assert waited
    with pytest.raises(SandboxUnavailable, match='closed'):
        asyncio.run(sandbox.start())


def test_start_returns_unconsumed_worker_transport_and_rejects_reuse(tmp_path, monkeypatch):
    sandbox = build(tmp_path, network='proxy')
    calls = []
    process = SimpleNamespace(pid=98765)

    async def preflight():
        return result()

    async def spawn(argv):
        calls.append(argv)
        return process

    async def terminate(proc):
        assert proc is process

    monkeypatch.setattr(sandbox, 'preflight', preflight)
    monkeypatch.setattr(sandbox, '_spawn', spawn)
    monkeypatch.setattr(sandbox, '_terminate', terminate)
    monkeypatch.setattr(Path, 'is_file', lambda path: path.name == 'worker.py')

    async def run():
        assert await sandbox.start() is process
        assert sandbox.process is process
        assert calls[0][:2] == [sys.executable, '-I']
        assert Path(calls[0][2]).name == 'worker.py'
        with pytest.raises(SandboxUnavailable, match='already started'):
            await sandbox.start()
        await sandbox.close()
        await sandbox.close()  # idempotent: do not signal an old/reused PID
        assert sandbox.process is None

    asyncio.run(run())


def test_runtime_verification_rejects_hardlink_alias_outside_protected_tree(tmp_path):
    protected = tmp_path / 'runtime'
    protected.mkdir()
    module = protected / 'module.py'
    module.write_text('trusted = True')
    cache = tmp_path / 'writable-cache'
    cache.mkdir()
    alias = cache / 'module.py'
    os.link(module, alias)
    assert module.stat().st_ino == alias.stat().st_ino
    with pytest.raises(SandboxUnavailable, match='hardlinked file.*link-mode copy'):
        _verify_runtime_tree({protected}, {protected})
    # Breaking the alias via an actual copy removes the unsafe shared inode.
    content = alias.read_bytes()
    alias.unlink()
    alias.write_bytes(content)
    _verify_runtime_tree({protected}, {protected})


def test_runtime_verification_follows_only_protected_symlink_targets(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    module = runtime / 'module.py'
    module.write_text('trusted = True')
    (runtime / 'internal.py').symlink_to(module)
    _verify_runtime_tree({runtime}, {runtime})
    other = tmp_path / 'other'
    other.mkdir()
    other_module = other / 'external.py'
    other_module.write_text('trusted = True')
    external = runtime / 'external.py'
    external.symlink_to(other_module)
    with pytest.raises(SandboxUnavailable, match='symlink escapes protected paths'):
        _verify_runtime_tree({runtime}, {runtime})
    _verify_runtime_tree({runtime}, {runtime, other})
    # A separately protected target still needs scanning for inode aliases.
    os.link(other_module, tmp_path / 'alias.py')
    with pytest.raises(SandboxUnavailable, match='hardlinked file'):
        _verify_runtime_tree({runtime}, {runtime, other})


def test_runtime_verification_is_bounded_and_fails_on_missing_or_special_files(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    for index in range(4):
        (runtime / str(index)).write_text('fixed')
    with pytest.raises(SandboxUnavailable, match='entry limit'):
        _verify_runtime_tree({runtime}, {runtime}, max_entries=3)
    with pytest.raises(SandboxUnavailable, match='Cannot verify trusted runtime path'):
        _verify_runtime_tree({runtime / 'missing'}, {runtime})
    os.mkfifo(runtime / 'fifo')
    with pytest.raises(SandboxUnavailable, match='Unsupported file type'):
        _verify_runtime_tree({runtime}, {runtime})


def test_runtime_verification_cannot_be_skipped_before_preflight_spawn(tmp_path, monkeypatch):
    tools(monkeypatch)
    sandbox = build(tmp_path, network='proxy')
    sandbox._prepare()
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    module = runtime / 'module.py'
    module.write_text('fixed')
    os.link(module, tmp_path / 'writable-alias.py')
    sandbox._runtime_scan_roots = {runtime}
    sandbox._runtime_protected = {runtime}
    calls = []

    async def spawn(argv):
        calls.append(argv)
        raise AssertionError('Unsafe runtime reached spawn')

    monkeypatch.setattr(sandbox, '_spawn', spawn)
    with pytest.raises(SandboxUnavailable, match='hardlinked file'):
        asyncio.run(sandbox.start())
    assert calls == []
    assert sandbox.process is None
    assert not list(sandbox.host_dir.glob('srt-*.json'))


def test_runtime_layout_protects_srt_and_executable_dirs_without_scanning_system_prefix(tmp_path, monkeypatch):
    tools(monkeypatch)
    root = tmp_path / 'workspace'
    root.mkdir()
    package = root / 'node_modules' / 'sandbox-runtime'
    (package / 'dist').mkdir(parents=True)
    cli = package / 'dist' / 'cli.js'
    cli.write_text('// fixed fixture')
    bins = root / 'bin'
    bins.mkdir()
    for command in ('bwrap', 'socat', 'rg', 'node'):
        (bins / command).write_text('fixed fixture')
    monkeypatch.setattr('py_agent.sandbox.shutil.which', lambda command: str(cli if command == 'srt' else bins / command))
    monkeypatch.setattr(sys, 'prefix', '/usr')
    monkeypatch.setattr(sys, 'base_prefix', '/usr')
    sandbox = Sandbox(root, root / 'control', root / 'scratch', network='proxy')
    sandbox._prepare()
    denied = json.loads(sandbox._settings.read_text())['filesystem']['denyWrite']
    assert str(package) in denied
    assert str(bins) in denied
    assert Path('/usr') not in sandbox._runtime_scan_roots
    assert Path('/usr') in sandbox._runtime_protected
    asyncio.run(sandbox.close())


def test_runtime_verification_rejects_accidentally_broad_scan_root(tmp_path):
    sandbox = build(tmp_path, network='proxy')
    sandbox._runtime_scan_roots = {Path('/usr')}
    sandbox._runtime_protected = {Path('/usr')}
    with pytest.raises(SandboxUnavailable, match='rather than scanning a system prefix'):
        sandbox._verify_runtime()


def test_cleanup_policy_does_not_claim_verified_descendant_enforcement(tmp_path):
    sandbox = build(tmp_path)
    assert 'integration-unverified' in sandbox.policy['interrupt']


@pytest.mark.sandbox
@pytest.mark.skipif(os.environ.get('PY_AGENT_SANDBOX_TESTS') != '1', reason='opt in with PY_AGENT_SANDBOX_TESTS=1')
def test_real_srt_preflight_and_transport():
    # Linux sockaddr_un is short; pytest's default nested temporary paths can
    # exceed it before srt even reaches bubblewrap.
    directory = tempfile.TemporaryDirectory(prefix='py-srt-', dir='/tmp')
    sandbox = build(Path(directory.name), network='proxy')

    async def run():
        try:
            try:
                checks = await sandbox.preflight()
            except SandboxUnavailable as exc:
                if 'srt isolation preflight failed (exit' not in str(exc):
                    raise
                pytest.skip(f'Required host isolation unavailable: {exc}')
            assert all(checks.values())
        finally:
            await sandbox.close()
            directory.cleanup()

    asyncio.run(run())


@pytest.mark.sandbox
@pytest.mark.skipif(os.environ.get('PY_AGENT_SANDBOX_TESTS') != '1', reason='opt in with PY_AGENT_SANDBOX_TESTS=1')
def test_real_namespace_cleanup_includes_setsid_descendant():
    directory = tempfile.TemporaryDirectory(prefix='py-srt-', dir='/tmp')
    sandbox = build(Path(directory.name), network='proxy')

    async def run():
        try:
            try:
                await sandbox.preflight()
            except SandboxUnavailable as exc:
                if 'srt isolation preflight failed (exit' not in str(exc):
                    raise
                pytest.skip(f'Required host isolation unavailable: {exc}')
            # Trusted test fixture, never model-generated code. A detached process
            # repeatedly changes a heartbeat; close must stop it despite setsid.
            heartbeat = sandbox.scratch / 'heartbeat'
            child = "import pathlib,time; p=pathlib.Path(__import__('sys').argv[1]);\nwhile True: p.write_text(str(time.monotonic_ns())); time.sleep(.02)"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],sys.argv[2]],start_new_session=True); print('ready',flush=True); time.sleep(60)"
            sandbox.process = await sandbox._spawn([sys.executable, '-I', '-c', parent, child, str(heartbeat)])
            assert await asyncio.wait_for(sandbox.process.stdout.readline(), 20) == b'ready\n'
            for _ in range(100):
                if heartbeat.exists():
                    break
                await asyncio.sleep(.02)
            assert heartbeat.exists()
            await sandbox.close()
            await asyncio.sleep(.2)
            value = heartbeat.read_text()
            await asyncio.sleep(.2)
            assert heartbeat.read_text() == value
        finally:
            await sandbox.close()
            directory.cleanup()

    asyncio.run(run())
