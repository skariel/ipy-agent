"""Trusted permission persistence and brokered host filesystem operations.

The worker is untrusted. Permission identifiers are correlation handles, never
proof of authority; every operation is checked against host-owned capability
state. Filesystem approvals do not change the worker's mount namespace.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path, PurePath
import re
import stat
from typing import Any, Literal
import uuid

Scope = Literal["once", "session", "project", "global"]
Operation = Literal["create", "modify", "delete", "rename"]
SCOPES = frozenset({"once", "session", "project", "global"})
OPERATIONS = frozenset({"create", "modify", "delete", "rename"})
_MAX_STORE_BYTES = 1_048_576
_MAX_TEXT_BYTES = 1_048_576
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")


class PermissionError(RuntimeError):
    """A permission request, decision, or operation was rejected."""


class PermissionStoreError(PermissionError):
    """Persistent permission state is unsafe or malformed."""


@dataclass(frozen=True)
class Grant:
    id: str
    kind: Literal["network", "filesystem"]
    scope: Literal["project", "global"]
    project: str | None
    resource: dict[str, Any]

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "scope": self.scope,
            "project": self.project,
            "resource": self.resource,
        }


@dataclass
class _Pending:
    id: str
    kind: Literal["network", "filesystem"]
    resource: dict[str, Any]
    reason: str
    future: asyncio.Future[Scope | None]


@dataclass
class _Capability:
    id: str
    root: Path
    recursive: bool
    operations: frozenset[str]
    scope: Scope
    fd: int
    remaining: int | None


class PermissionStore:
    """Small locked JSON store beneath a private, sandbox-protected host root."""

    def __init__(self, host_root: str | Path):
        self.root = Path(os.path.abspath(host_root))
        self._prepare_root()
        self.path = self.root / "permissions.json"
        self.lock_path = self.root / "permissions.lock"

    def _prepare_root(self) -> None:
        missing: list[Path] = []
        probe = self.root
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        for path in (probe, *probe.parents):
            if path.is_symlink():
                raise PermissionStoreError(f"permission store path contains a symlink: {path}")
        for path in reversed(missing):
            path.mkdir(mode=0o700)
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionStoreError("permission store directory must be owned and private (mode 0700)")

    @staticmethod
    def _validate_file(fd: int, label: str) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise PermissionStoreError(f"{label} must be an owned regular file with one link")
        if info.st_mode & 0o077:
            raise PermissionStoreError(f"{label} must have mode 0600")

    def _lock(self) -> int:
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            self._validate_file(fd, "permission lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _read_locked(self) -> list[Grant]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        try:
            self._validate_file(fd, "permission store")
            info = os.fstat(fd)
            if info.st_size > _MAX_STORE_BYTES:
                raise PermissionStoreError("permission store exceeds size limit")
            data = os.read(fd, _MAX_STORE_BYTES + 1)
        finally:
            os.close(fd)
        try:
            value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise PermissionStoreError("permission store is malformed") from exc
        if not isinstance(value, dict) or set(value) != {"version", "grants"} or value["version"] != 1:
            raise PermissionStoreError("unsupported permission store schema")
        if not isinstance(value["grants"], list) or len(value["grants"]) > 10_000:
            raise PermissionStoreError("invalid permission grants")
        return [_parse_grant(record) for record in value["grants"]]

    def load(self) -> list[Grant]:
        lock = self._lock()
        try:
            return self._read_locked()
        finally:
            os.close(lock)

    def add(self, grant: Grant) -> None:
        _validate_grant(grant)
        lock = self._lock()
        try:
            grants = self._read_locked()
            if any(existing.id == grant.id for existing in grants):
                raise PermissionStoreError("duplicate permission ID")
            grants.append(grant)
            self._write_locked(grants)
        finally:
            os.close(lock)

    def remove(self, grant_id: str) -> bool:
        lock = self._lock()
        try:
            grants = self._read_locked()
            retained = [grant for grant in grants if grant.id != grant_id]
            if len(retained) == len(grants):
                return False
            self._write_locked(retained)
            return True
        finally:
            os.close(lock)

    def _write_locked(self, grants: list[Grant]) -> None:
        payload = json.dumps(
            {"version": 1, "grants": [grant.record() for grant in grants]},
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > _MAX_STORE_BYTES:
            raise PermissionStoreError("permission store exceeds size limit")
        temporary = self.root / f".permissions-{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            remaining = payload
            while remaining:
                remaining = remaining[os.write(fd, remaining) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class PermissionManager:
    """Own interactive decisions, grants, and brokered filesystem capabilities."""

    def __init__(
        self,
        store: PermissionStore,
        workspace: str | Path,
        *,
        protected_paths: Iterable[str | Path] = (),
        emit: Callable[[dict[str, Any]], None] | None = None,
        timeout: float = 600.0,
    ):
        self.store = store
        self.workspace = str(Path(workspace).resolve(strict=True))
        self.protected = tuple(Path(path).resolve(strict=False) for path in protected_paths)
        self.emit = emit or (lambda event: None)
        if timeout <= 0:
            raise ValueError("permission timeout must be positive")
        self.timeout = timeout
        self._persisted = store.load()
        self._session: list[tuple[str, dict[str, Any]]] = []
        self._pending: dict[str, _Pending] = {}
        self._capabilities: dict[str, _Capability] = {}
        self._counter = 0
        self._closed = False

    @property
    def pending(self) -> list[dict[str, Any]]:
        return [self._pending_record(request) for request in self._pending.values()]

    @property
    def grants(self) -> list[dict[str, Any]]:
        return [grant.record() for grant in self._persisted]

    async def request_network(self, host: str, port: int | None = None, *, reason: str = "") -> bool:
        resource = _network_resource(host, port)
        if self._matches("network", resource):
            return True
        request = self._new_pending("network", resource, reason)
        return await self._decision(request) is not None

    async def request_filesystem(
        self,
        path: str | Path,
        *,
        recursive: bool = True,
        operations: Iterable[Operation] = ("create", "modify"),
        reason: str = "",
    ) -> str | None:
        resource = self._filesystem_resource(path, recursive, operations)
        scope: Scope | None
        if self._matches("filesystem", resource):
            scope = "session"
        else:
            request = self._new_pending("filesystem", resource, reason)
            scope = await self._decision(request)
        if scope is None:
            return None
        return self._create_capability(resource, scope)

    async def _decision(self, request: _Pending) -> Scope | None:
        try:
            return await asyncio.wait_for(asyncio.shield(request.future), self.timeout)
        except TimeoutError:
            if self._pending.pop(request.id, None) is request:
                if not request.future.done():
                    request.future.set_result(None)
                self.emit({
                    "kind": "permission_decision",
                    "content": {"request_id": request.id, "allow": False, "scope": None, "timeout": True},
                })
            return None

    def resolve(self, request_id: str, *, allow: bool, scope: Scope = "once") -> None:
        if scope not in SCOPES:
            raise PermissionError("invalid permission scope")
        try:
            request = self._pending.pop(request_id)
        except KeyError as exc:
            raise PermissionError("unknown or resolved permission request") from exc
        if request.future.done():
            raise PermissionError("permission request is already resolved")
        selected: Scope | None = None
        if allow:
            if scope == "session":
                self._session.append((request.kind, request.resource))
            elif scope in {"project", "global"}:
                persistent_scope = scope
                grant = Grant(
                    id=f"p-{uuid.uuid4().hex}",
                    kind=request.kind,
                    scope=persistent_scope,
                    project=self.workspace if scope == "project" else None,
                    resource=request.resource,
                )
                try:
                    self.store.add(grant)  # Persist before releasing authority.
                except BaseException:
                    request.future.set_result(None)
                    raise
                self._persisted.append(grant)
            selected = scope
        decision = {
            "kind": "permission_decision",
            "content": {"request_id": request.id, "allow": allow, "scope": scope if allow else None},
        }
        self.emit(decision)
        request.future.set_result(selected)

    def revoke(self, grant_id: str) -> bool:
        removed = self.store.remove(grant_id)
        if removed:
            self._persisted = [grant for grant in self._persisted if grant.id != grant_id]
        return removed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for request in self._pending.values():
            if not request.future.done():
                request.future.set_result(None)
        self._pending.clear()
        for capability in self._capabilities.values():
            os.close(capability.fd)
        self._capabilities.clear()

    def write_text(self, capability_id: str, relative_path: str, content: str) -> None:
        if not isinstance(content, str) or len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise PermissionError("write content must be bounded text")
        capability = self._capability(capability_id)
        parent, name = self._parent_fd(capability, relative_path)
        temporary: str | None = None
        try:
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                operation = "create"
                target = name
            else:
                operation = "modify"
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PermissionError("write target must be a regular one-link file")
                temporary = f".py-agent-{uuid.uuid4().hex}.tmp"
                target = temporary
            self._require(capability, operation)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                data = content.encode("utf-8")
                while data:
                    written = os.write(fd, data)
                    data = data[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            if temporary is not None:
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                temporary = None
            os.fsync(parent)
        finally:
            if temporary is not None:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)
            os.close(parent)
        self._used(capability, operation, relative_path)

    def mkdir(self, capability_id: str, relative_path: str) -> None:
        capability = self._capability(capability_id)
        self._require(capability, "create")
        parent, name = self._parent_fd(capability, relative_path)
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        finally:
            os.close(parent)
        self._used(capability, "create", relative_path)

    def rename(self, capability_id: str, source: str, destination: str) -> None:
        capability = self._capability(capability_id)
        self._require(capability, "rename")
        source_parent, source_name = self._parent_fd(capability, source)
        destination_parent, destination_name = self._parent_fd(capability, destination)
        try:
            try:
                os.stat(destination_name, dir_fd=destination_parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PermissionError("rename destination already exists")
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
        finally:
            os.close(source_parent)
            os.close(destination_parent)
        self._used(capability, "rename", f"{source} -> {destination}")

    def remove(self, capability_id: str, relative_path: str) -> None:
        capability = self._capability(capability_id)
        self._require(capability, "delete")
        parent, name = self._parent_fd(capability, relative_path)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                os.rmdir(name, dir_fd=parent)
            else:
                os.unlink(name, dir_fd=parent)
        finally:
            os.close(parent)
        self._used(capability, "delete", relative_path)

    def _new_pending(self, kind: Literal["network", "filesystem"], resource: dict[str, Any], reason: str) -> _Pending:
        if self._closed:
            raise PermissionError("permission manager is closed")
        if not isinstance(reason, str) or len(reason) > 1000 or "\x00" in reason:
            raise PermissionError("permission reason must be bounded text")
        self._counter += 1
        request_id = f"perm-{self._counter}"
        request = _Pending(request_id, kind, resource, reason, asyncio.get_running_loop().create_future())
        self._pending[request_id] = request
        self.emit({"kind": "permission_request", "content": self._pending_record(request)})
        return request

    @staticmethod
    def _pending_record(request: _Pending) -> dict[str, Any]:
        return {
            "request_id": request.id,
            "permission_kind": request.kind,
            "resource": request.resource,
            "reason": request.reason,
        }

    def _filesystem_resource(
        self,
        path: str | Path,
        recursive: bool,
        operations: Iterable[Operation],
    ) -> dict[str, Any]:
        if type(recursive) is not bool:
            raise PermissionError("recursive must be boolean")
        root = Path(path).resolve(strict=True)
        if not root.is_dir():
            raise PermissionError("filesystem approval root must be an existing directory")
        requested = tuple(sorted(set(operations)))
        if not requested or any(operation not in OPERATIONS for operation in requested):
            raise PermissionError("invalid filesystem operations")
        if any(root.is_relative_to(protected) or protected.is_relative_to(root) for protected in self.protected):
            raise PermissionError("filesystem approval intersects a permanently protected path")
        return {"path": str(root), "recursive": recursive, "operations": list(requested)}

    def _matches(self, kind: str, resource: dict[str, Any]) -> bool:
        if any(
            grant_kind == kind and _resource_covers(kind, grant_resource, resource)
            for grant_kind, grant_resource in self._session
        ):
            return True
        return any(
            grant.kind == kind
            and (grant.scope == "global" or grant.project == self.workspace)
            and _resource_covers(kind, grant.resource, resource)
            for grant in self._persisted
        )

    def _create_capability(self, resource: dict[str, Any], scope: Scope) -> str:
        root = Path(resource["path"])
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        capability_id = f"cap-{uuid.uuid4().hex}"
        self._capabilities[capability_id] = _Capability(
            capability_id,
            root,
            resource["recursive"],
            frozenset(resource["operations"]),
            scope,
            fd,
            1 if scope == "once" else None,
        )
        return capability_id

    def _capability(self, capability_id: str) -> _Capability:
        if self._closed:
            raise PermissionError("permission manager is closed")
        try:
            return self._capabilities[capability_id]
        except KeyError as exc:
            raise PermissionError("unknown or expired filesystem capability") from exc

    @staticmethod
    def _require(capability: _Capability, operation: str) -> None:
        if operation not in capability.operations:
            raise PermissionError(f"filesystem capability does not allow {operation}")

    def _parent_fd(self, capability: _Capability, relative_path: str) -> tuple[int, str]:
        parts = _relative_parts(relative_path)
        if not capability.recursive and len(parts) != 1:
            raise PermissionError("non-recursive capability only covers direct children")
        current = os.dup(capability.fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = next_fd
            return current, parts[-1]
        except BaseException:
            os.close(current)
            raise

    def _used(self, capability: _Capability, operation: str, path: str) -> None:
        self.emit({
            "kind": "permission_operation",
            "content": {"capability_id": capability.id, "operation": operation, "path": path},
        })
        if capability.remaining is not None:
            capability.remaining -= 1
            if capability.remaining <= 0:
                self._capabilities.pop(capability.id, None)
                os.close(capability.fd)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _network_resource(host: str, port: int | None) -> dict[str, Any]:
    if not isinstance(host, str):
        raise PermissionError("invalid network host")
    try:
        canonical = str(ipaddress.ip_address(host))
    except ValueError:
        if not _HOST.fullmatch(host) or ".." in host or host.endswith("."):
            raise PermissionError("invalid network host") from None
        canonical = host.lower()
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise PermissionError("invalid network port")
    return {"host": canonical, "port": port}


def _relative_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise PermissionError("relative path must be nonempty text")
    pure = PurePath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PermissionError("path must be a normalized relative path")
    return pure.parts


def _resource_covers(kind: str, granted: dict[str, Any], requested: dict[str, Any]) -> bool:
    if kind == "network":
        return granted == requested
    granted_path = Path(granted["path"])
    requested_path = Path(requested["path"])
    path_covered = requested_path == granted_path or (
        bool(granted["recursive"]) and requested_path.is_relative_to(granted_path)
    )
    return path_covered and set(requested["operations"]) <= set(granted["operations"])


def _parse_grant(value: Any) -> Grant:
    if not isinstance(value, dict) or set(value) != {"id", "kind", "scope", "project", "resource"}:
        raise PermissionStoreError("invalid permission grant record")
    grant = Grant(value["id"], value["kind"], value["scope"], value["project"], value["resource"])
    _validate_grant(grant)
    return grant


def _validate_grant(grant: Grant) -> None:
    if not isinstance(grant.id, str) or not re.fullmatch(r"p-[a-f0-9]{32}", grant.id):
        raise PermissionStoreError("invalid persistent permission ID")
    if grant.kind not in {"network", "filesystem"} or grant.scope not in {"project", "global"}:
        raise PermissionStoreError("invalid persistent permission kind or scope")
    if grant.scope == "project":
        if not isinstance(grant.project, str) or not Path(grant.project).is_absolute():
            raise PermissionStoreError("project permission needs an absolute workspace")
    elif grant.project is not None:
        raise PermissionStoreError("global permission must not name a project")
    if not isinstance(grant.resource, dict):
        raise PermissionStoreError("invalid permission resource")
    try:
        if grant.kind == "network":
            if set(grant.resource) != {"host", "port"}:
                raise PermissionError("invalid network resource")
            if _network_resource(grant.resource["host"], grant.resource["port"]) != grant.resource:
                raise PermissionError("network resource is not canonical")
        else:
            if set(grant.resource) != {"path", "recursive", "operations"}:
                raise PermissionError("invalid filesystem resource")
            path = grant.resource["path"]
            operations = grant.resource["operations"]
            if (
                not isinstance(path, str)
                or not Path(path).is_absolute()
                or type(grant.resource["recursive"]) is not bool
                or not isinstance(operations, list)
                or not operations
                or operations != sorted(set(operations))
                or any(operation not in OPERATIONS for operation in operations)
            ):
                raise PermissionError("invalid filesystem resource")
    except PermissionError as exc:
        raise PermissionStoreError(str(exc)) from exc
