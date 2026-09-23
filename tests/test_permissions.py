from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from py_agent.permissions import PermissionError, PermissionManager, PermissionStore, PermissionStoreError


async def _pending_id(events, task):
    await asyncio.sleep(0)
    assert not task.done()
    return events[-1]["content"]["request_id"]


async def test_network_scope_lifetimes_and_project_isolation(tmp_path):
    host = tmp_path / "host"
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    events = []
    store = PermissionStore(host)
    manager = PermissionManager(store, first_workspace, emit=events.append)

    once = asyncio.create_task(manager.request_network("Example.COM", 443))
    manager.resolve(await _pending_id(events, once), allow=True, scope="once")
    assert await once

    repeated = asyncio.create_task(manager.request_network("example.com", 443))
    request_id = await _pending_id(events, repeated)
    manager.resolve(request_id, allow=True, scope="session")
    assert await repeated
    assert await manager.request_network("example.com", 443)

    project = asyncio.create_task(manager.request_network("project.example", 8443))
    manager.resolve(await _pending_id(events, project), allow=True, scope="project")
    assert await project
    global_request = asyncio.create_task(manager.request_network("global.example", 443))
    manager.resolve(await _pending_id(events, global_request), allow=True, scope="global")
    assert await global_request
    manager.close()

    same_project = PermissionManager(store, first_workspace)
    assert await same_project.request_network("project.example", 8443)
    assert await same_project.request_network("global.example", 443)
    same_project.close()

    other_project = PermissionManager(store, second_workspace)
    blocked = asyncio.create_task(other_project.request_network("project.example", 8443))
    await asyncio.sleep(0)
    assert not blocked.done()
    other_project.resolve(other_project.pending[0]["request_id"], allow=False)
    assert not await blocked
    assert await other_project.request_network("global.example", 443)
    other_project.close()


async def test_permission_waits_until_decided_without_timeout(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = PermissionManager(PermissionStore(tmp_path / "host"), workspace)
    request = asyncio.create_task(manager.request_network("waiting.example", 443))
    await asyncio.sleep(0.02)
    assert not request.done()
    manager.resolve(manager.pending[0]["request_id"], allow=False)
    assert not await request
    manager.close()


async def test_denial_close_and_persist_before_release(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = PermissionManager(PermissionStore(tmp_path / "host"), workspace)
    denied = asyncio.create_task(manager.request_network("denied.example", 443))
    await asyncio.sleep(0)
    manager.resolve(manager.pending[0]["request_id"], allow=False)
    assert not await denied

    waiting = asyncio.create_task(manager.request_network("waiting.example", 443))
    await asyncio.sleep(0)
    manager.close()
    assert not await waiting

    manager = PermissionManager(PermissionStore(tmp_path / "other-host"), workspace)
    request = asyncio.create_task(manager.request_network("failure.example", 443))
    await asyncio.sleep(0)

    def fail(_grant):
        raise OSError("disk full")

    monkeypatch.setattr(manager.store, "add", fail)
    with pytest.raises(OSError, match="disk full"):
        manager.resolve(manager.pending[0]["request_id"], allow=True, scope="global")
    assert not await request
    manager.close()


async def test_brokered_filesystem_operations_and_once_expiry(tmp_path):
    workspace = tmp_path / "workspace"
    target = tmp_path / "target"
    workspace.mkdir()
    target.mkdir()
    events = []
    manager = PermissionManager(PermissionStore(tmp_path / "host"), workspace, emit=events.append)
    request = asyncio.create_task(
        manager.request_filesystem(
            target,
            operations=("create", "modify", "delete", "rename"),
            reason="test operations",
        )
    )
    manager.resolve(await _pending_id(events, request), allow=True, scope="session")
    capability = await request
    assert capability is not None

    manager.mkdir(capability, "nested")
    manager.write_text(capability, "nested/a.txt", "one")
    manager.write_text(capability, "nested/a.txt", "two")
    manager.rename(capability, "nested/a.txt", "nested/b.txt")
    assert (target / "nested" / "b.txt").read_text() == "two"
    manager.remove(capability, "nested/b.txt")
    manager.remove(capability, "nested")
    assert any(event["kind"] == "permission_operation" for event in events)

    once = asyncio.create_task(manager.request_filesystem(target, operations=("create",)))
    # Session's broader grant auto-satisfies this request, so use a fresh manager
    # to verify the one-operation capability lifetime.
    manager.close()
    manager = PermissionManager(PermissionStore(tmp_path / "other-host"), workspace)
    once.cancel()
    request = asyncio.create_task(manager.request_filesystem(target, operations=("create",)))
    await asyncio.sleep(0)
    manager.resolve(manager.pending[0]["request_id"], allow=True, scope="once")
    capability = await request
    manager.write_text(capability, "first.txt", "first")
    with pytest.raises(PermissionError, match="expired"):
        manager.write_text(capability, "second.txt", "second")
    manager.close()


async def test_filesystem_permissions_are_scoped_and_symlink_safe(tmp_path):
    workspace = tmp_path / "workspace"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    protected = tmp_path / "protected"
    for path in (workspace, target, outside, protected):
        path.mkdir()
    (target / "escape").symlink_to(outside, target_is_directory=True)
    manager = PermissionManager(
        PermissionStore(tmp_path / "host"),
        workspace,
        protected_paths=(protected,),
    )

    with pytest.raises(PermissionError, match="protected"):
        await manager.request_filesystem(tmp_path, operations=("create",))
    with pytest.raises(PermissionError, match="protected"):
        await manager.request_filesystem(protected, operations=("create",))

    request = asyncio.create_task(manager.request_filesystem(target, operations=("create",)))
    await asyncio.sleep(0)
    manager.resolve(manager.pending[0]["request_id"], allow=True, scope="session")
    capability = await request
    with pytest.raises((NotADirectoryError, OSError)):
        manager.write_text(capability, "escape/pwned", "no")
    assert not (outside / "pwned").exists()
    with pytest.raises(PermissionError, match="normalized relative"):
        manager.write_text(capability, "../outside/pwned", "no")
    with pytest.raises(PermissionError, match="does not allow modify"):
        (target / "existing").write_text("old")
        manager.write_text(capability, "existing", "new")
    manager.close()


def test_store_security_schema_modes_and_revocation(tmp_path):
    root = tmp_path / "host"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = PermissionStore(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    manager = PermissionManager(store, workspace)

    async def persist():
        task = asyncio.create_task(manager.request_network("saved.example", 443))
        await asyncio.sleep(0)
        manager.resolve(manager.pending[0]["request_id"], allow=True, scope="global")
        assert await task

    asyncio.run(persist())
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    payload = json.loads(store.path.read_text())
    grant_id = payload["grants"][0]["id"]
    assert manager.revoke(grant_id)
    assert not manager.revoke(grant_id)
    manager.close()

    store.path.write_text('{"version":1,"grants":[],"extra":true}')
    os.chmod(store.path, 0o600)
    with pytest.raises(PermissionStoreError, match="schema"):
        store.load()


def test_store_rejects_public_hardlinked_and_symlink_state(tmp_path):
    root = tmp_path / "host"
    store = PermissionStore(root)
    store.path.write_text('{"version":1,"grants":[]}')
    os.chmod(store.path, 0o644)
    with pytest.raises(PermissionStoreError, match="0600"):
        store.load()

    store.path.unlink()
    original = tmp_path / "original"
    original.write_text('{"version":1,"grants":[]}')
    os.chmod(original, 0o600)
    os.link(original, store.path)
    with pytest.raises(PermissionStoreError, match="one link"):
        store.load()

    store.path.unlink()
    store.path.symlink_to(original)
    with pytest.raises(OSError):
        store.load()
