"""Bounded immutable recovery snapshots, retained owner leases and manifest CAS."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..core.durable_workspace_state import (
    MAX_OWNER_RECORDS,
    MAX_OWNER_REVISION,
    MAX_SNAPSHOT_BYTES,
    DurableStateError,
    StateRecord,
    StateSnapshot,
    decode_snapshot,
    encode_snapshot,
    json_bytes,
    require_owner,
    require_record_id,
    strict_json,
)
from ..core.private_storage_layout import MIB, private_subtree_path
from ..core.safe_paths import (
    UnsafePathError,
    ensure_directory,
    validate_directory,
    validate_regular_file,
)
from .managed_artifact_scopes import _acquire_lock, _create_or_admit
from .media_runtime_resolution import _link_free_existing_prefix
from .segment_artifact_store import (
    ArtifactStoreError,
    _directory_identity,
    _identity,
    _msvcrt_get_osfhandle,
    _msvcrt_open_osfhandle,
    _read_regular_bytes,
    _validated_directories,
    _windows_delete_exact,
    _windows_dll,
    _windows_last_error,
    _windows_native_path,
    _write_new_file,
)

MANIFEST_SCHEMA = "h3.context.workspace_state_manifest.v1"
CLOSED_RETENTION_MS = 7 * 86400 * 1000
MAX_MANIFEST_BYTES = 8192
_SNAPSHOT_NAME = re.compile(r"revision-([0-9]{16})-([0-9a-f]{64})\.json\Z")
_FAMILY_MARKER = ".h3-workspace-state-v1"
_CATALOG_LOCK = ".catalog.lock"
_OWNER_LOCK = ".owner.lock"
_MANIFEST = "manifest.json"
_PENDING = "manifest.next.json"
_FAMILY_BYTES = json_bytes({"schema": "h3.context.workspace_state_family.v1"})
_CATALOG_BYTES = json_bytes({"schema": "h3.context.workspace_state_catalog_lock.v1"})


@dataclass(frozen=True, slots=True)
class StateStoreLimits:
    max_owner_records: int = MAX_OWNER_RECORDS
    max_global_records: int = 256
    max_owners: int = 16
    max_total_bytes: int = 16 * MIB

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.max_owner_records, 64),
            (self.max_global_records, 256),
            (self.max_owners, 16),
            (self.max_total_bytes, 16 * MIB),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise DurableStateError("limits_invalid")


@dataclass(frozen=True, slots=True)
class SnapshotReference:
    file_name: str
    revision: int
    record_ids: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "file_name": self.file_name,
            "revision": self.revision,
            "record_ids": list(self.record_ids),
        }


@dataclass(frozen=True, slots=True)
class StateManifest:
    owner_id: str
    revision: int = 0
    enabled: bool = False
    latest: SnapshotReference | None = None
    previous: SnapshotReference | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": MANIFEST_SCHEMA,
            "owner_id": self.owner_id,
            "revision": self.revision,
            "enabled": self.enabled,
            "latest": None if self.latest is None else self.latest.to_wire(),
            "previous": None if self.previous is None else self.previous.to_wire(),
        }


def _revision(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_OWNER_REVISION:
        raise DurableStateError("revision_invalid")
    return value


def _reference(value: object) -> SnapshotReference | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"file_name", "revision", "record_ids"}:
        raise DurableStateError("manifest_invalid")
    name = value["file_name"]
    matched = _SNAPSHOT_NAME.fullmatch(name) if type(name) is str else None
    revision = _revision(value["revision"], minimum=1)
    ids = value["record_ids"]
    if (
        matched is None
        or int(matched.group(1)) != revision
        or type(ids) is not list
        or len(ids) > MAX_OWNER_RECORDS
    ):
        raise DurableStateError("manifest_invalid")
    identifiers = tuple(require_record_id(identifier) for identifier in ids)
    if tuple(sorted(set(identifiers))) != identifiers:
        raise DurableStateError("manifest_invalid")
    return SnapshotReference(name, revision, identifiers)


def _decode_manifest(data: bytes, owner: str) -> StateManifest:
    value = strict_json(data, maximum_bytes=MAX_MANIFEST_BYTES)
    if type(value) is not dict or set(value) != {
        "schema",
        "owner_id",
        "revision",
        "enabled",
        "latest",
        "previous",
    }:
        raise DurableStateError("manifest_invalid")
    if value["schema"] != MANIFEST_SCHEMA:
        raise DurableStateError("version_unsupported")
    if value["owner_id"] != owner:
        raise DurableStateError("owner_mismatch")
    revision = _revision(value["revision"], minimum=1)
    if type(value["enabled"]) is not bool:
        raise DurableStateError("manifest_invalid")
    latest, previous = _reference(value["latest"]), _reference(value["previous"])
    if (
        previous is not None
        and (latest is None or previous.revision >= latest.revision)
        or latest is not None
        and latest.revision > revision
    ):
        raise DurableStateError("manifest_invalid")
    return StateManifest(owner, revision, value["enabled"], latest, previous)


def _owner_bytes(owner: str) -> bytes:
    return json_bytes({"schema": "h3.context.workspace_state_owner_lock.v1", "owner_id": owner})


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _read(path: Path, maximum: int) -> tuple[bytes, os.stat_result]:
    before = validate_regular_file(path, maximum_bytes=maximum).lstat()
    if before.st_nlink != 1:
        raise DurableStateError("storage_unsafe")
    payload = _read_regular_bytes(path, maximum_bytes=maximum)
    if _identity(before) != _identity(path.lstat()):
        raise DurableStateError("storage_unsafe")
    return payload, before


def _delete(path: Path, expected: os.stat_result) -> None:
    with _validated_directories(path.parent):
        actual = validate_regular_file(path).lstat()
        if actual.st_nlink != 1 or _identity(actual) != _identity(expected):
            raise DurableStateError("storage_unsafe")
        if os.name == "nt":
            _windows_delete_exact(path, expected=expected)
        else:
            directory = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if _directory_identity(os.fstat(directory)) != _directory_identity(
                    path.parent.lstat()
                ):
                    raise DurableStateError("storage_unsafe")
                os.unlink(path.name, dir_fd=directory)
            finally:
                os.close(directory)


def _replace_manifest(
    source: Path, target: Path, *, maximum_bytes: int = MAX_MANIFEST_BYTES
) -> None:
    """Publish a closed single-link manifest without following the destination entry."""
    with _validated_directories(source.parent, target.parent):
        before = validate_regular_file(source, maximum_bytes=maximum_bytes).lstat()
        if source.parent != target.parent or before.st_nlink != 1:
            raise DurableStateError("storage_unsafe")
        if (
            _exists(target)
            and validate_regular_file(target, maximum_bytes=maximum_bytes).lstat().st_nlink != 1
        ):
            raise DurableStateError("storage_unsafe")
        if os.name != "nt":
            # This path is hermetically injectable; it is not the initial live-host qualification.
            os.replace(source, target)
            directory_descriptor = os.open(
                target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            return
        import ctypes
        from ctypes import wintypes

        kernel = _windows_dll("kernel32")
        create = kernel.CreateFileW
        create.argtypes = (
            ctypes.c_wchar_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        )
        create.restype = wintypes.HANDLE
        handle = create(
            _windows_native_path(source), 0x80000000 | 0x00010000, 1, None, 3, 0x00200080, None
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError(_windows_last_error(), "manifest_pin_failed")
        descriptor = None
        try:
            descriptor = _msvcrt_open_osfhandle(int(handle), os.O_RDONLY)
            opened = os.fstat(descriptor)
            if opened.st_nlink != 1 or _identity(opened) != _identity(before):
                raise DurableStateError("storage_unsafe")

            class RenameChoice(ctypes.Union):
                _fields_ = (("replace", ctypes.c_ubyte), ("flags", wintypes.DWORD))

            class RenameInfo(ctypes.Structure):
                _fields_ = (
                    ("choice", RenameChoice),
                    ("root", wintypes.HANDLE),
                    ("length", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 1),
                )

            name = str(target).encode("utf-16-le")
            size = ctypes.sizeof(RenameInfo) + len(name)
            buffer = ctypes.create_string_buffer(size)
            info = ctypes.cast(buffer, ctypes.POINTER(RenameInfo)).contents
            info.choice.replace, info.root, info.length = 1, None, len(name)
            ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.name.offset, name, len(name))
            setter = kernel.SetFileInformationByHandle
            setter.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            setter.restype = wintypes.BOOL
            # CRITICAL: replace through this exact pinned source handle, not os.replace(path).
            # A closed-source pathname race could publish an unrelated substituted manifest.
            if not setter(_msvcrt_get_osfhandle(descriptor), 3, buffer, size):
                raise OSError(_windows_last_error(), "manifest_replace_failed")
            if _identity(os.fstat(descriptor)) != _identity(target.lstat()):
                raise DurableStateError("storage_unsafe")
        finally:
            if descriptor is None:
                kernel.CloseHandle(wintypes.HANDLE(handle))
            else:
                os.close(descriptor)


class DurableStateStore:
    """Filesystem port only; callers must qualify the current server owner before use."""

    def __init__(
        self,
        private_root: Path,
        owner_id: str,
        *,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        limits: StateStoreLimits | None = None,
    ) -> None:
        self.owner_id = require_owner(owner_id)
        if type(private_root) not in (Path, type(Path())) or not private_root.is_absolute():
            raise DurableStateError("storage_unsafe")
        self.private_root = private_root
        self.root = private_subtree_path(private_root, "workspace-state") / "v1"
        self.owner_directory = self.root / self.owner_id
        self.limits = limits or StateStoreLimits()
        self.clock_ms = clock_ms
        self._lock = threading.RLock()
        self._lease: int | None = None
        self._owner_identity: tuple[int, int] | None = None

    def close(self) -> None:
        with self._lock:
            if self._lease is not None:
                os.close(self._lease)
                self._lease = None
                self._owner_identity = None

    def _check_owner_lease(self, identity: tuple[int, int]) -> None:
        if self._lease is None or self._owner_identity != identity:
            raise DurableStateError("storage_unsafe")
        path = self.owner_directory / _OWNER_LOCK
        before = validate_regular_file(path, maximum_bytes=1024).lstat()
        opened = os.fstat(self._lease)
        expected = _owner_bytes(self.owner_id)
        if (
            before.st_nlink != 1
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
            or opened.st_size != len(expected)
        ):
            raise DurableStateError("storage_unsafe")
        # CRITICAL: byte-zero ownership does not prevent writes to the rest of the file.
        # Recheck the closed payload through our retained FD; reopening byte zero fails on Windows.
        os.lseek(self._lease, 0, os.SEEK_SET)
        payload = os.read(self._lease, len(expected) + 1)
        if (
            payload != expected
            or _identity(os.fstat(self._lease)) != _identity(opened)
            or _identity(path.lstat()) != _identity(opened)
        ):
            raise DurableStateError("storage_unsafe")

    @contextmanager
    def _operation(self, *, create: bool = False) -> Iterator[bool]:
        with self._lock:
            guard = None
            try:
                if not _link_free_existing_prefix(self.root):
                    raise DurableStateError("storage_unsafe")
                if not _exists(self.root) and not create:
                    yield False
                    return
                if create:
                    if (
                        not _exists(self.root)
                        and len(_FAMILY_BYTES)
                        + len(_CATALOG_BYTES)
                        + len(_owner_bytes(self.owner_id))
                        > self.limits.max_total_bytes
                    ):
                        raise DurableStateError("quota_bytes")
                    ensure_directory(self.root)
                    _create_or_admit(self.root / _FAMILY_MARKER, _FAMILY_BYTES)
                    _create_or_admit(self.root / _CATALOG_LOCK, _CATALOG_BYTES)
                with _validated_directories(self.private_root, self.root.parent, self.root):
                    if (
                        _read_regular_bytes(self.root / _FAMILY_MARKER, maximum_bytes=1024)
                        != _FAMILY_BYTES
                    ):
                        raise DurableStateError("storage_unsafe")
                    try:
                        guard = _acquire_lock(self.root / _CATALOG_LOCK, _CATALOG_BYTES)
                    except ArtifactStoreError as error:
                        if str(error) == "scope_busy":
                            raise DurableStateError("catalog_busy") from error
                        raise
                    if not _exists(self.owner_directory):
                        if not create:
                            yield False
                            return
                        owners = self._owners()
                        if len(owners) >= self.limits.max_owners:
                            raise DurableStateError("quota_owners")
                        total, _ = self._inventory()
                        if total + len(_owner_bytes(self.owner_id)) > self.limits.max_total_bytes:
                            raise DurableStateError("quota_bytes")
                        ensure_directory(self.owner_directory)
                    with _validated_directories(self.owner_directory):
                        identity = _directory_identity(self.owner_directory.lstat())
                        if self._lease is None:
                            if not _exists(self.owner_directory / _OWNER_LOCK):
                                total, _ = self._inventory()
                                if (
                                    total + len(_owner_bytes(self.owner_id))
                                    > self.limits.max_total_bytes
                                ):
                                    raise DurableStateError("quota_bytes")
                            _create_or_admit(
                                self.owner_directory / _OWNER_LOCK, _owner_bytes(self.owner_id)
                            )
                            try:
                                self._lease = _acquire_lock(
                                    self.owner_directory / _OWNER_LOCK, _owner_bytes(self.owner_id)
                                )
                            except ArtifactStoreError as error:
                                if str(error) == "scope_busy":
                                    raise DurableStateError("owner_busy") from error
                                raise
                            self._owner_identity = identity
                        self._check_owner_lease(identity)
                        yield True
            except DurableStateError:
                raise
            except (UnsafePathError, ArtifactStoreError, OSError) as error:
                raise DurableStateError(
                    "storage_unsafe"
                    if isinstance(error, UnsafePathError) or str(error) == "unsafe_store_entry"
                    else "storage_unavailable"
                ) from error
            finally:
                if guard is not None:
                    os.close(guard)

    def _owners(self) -> tuple[Path, ...]:
        owners = []
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries, 1):
                if count > 18:
                    raise DurableStateError("quota_directory")
                if entry.name in {_FAMILY_MARKER, _CATALOG_LOCK}:
                    continue
                require_owner(entry.name)
                owner = validate_directory(self.root / entry.name)
                owners.append(owner)
        if len(owners) > self.limits.max_owners:
            raise DurableStateError("quota_owners")
        return tuple(owners)

    def _files(self, owner: Path) -> tuple[tuple[Path, os.stat_result], ...]:
        files = []
        snapshots = 0
        with os.scandir(owner) as entries:
            for count, entry in enumerate(entries, 1):
                if count > 6:
                    raise DurableStateError("quota_directory")
                if entry.name not in {_OWNER_LOCK, _MANIFEST, _PENDING}:
                    if _SNAPSHOT_NAME.fullmatch(entry.name) is None:
                        raise DurableStateError("storage_unverified")
                    snapshots += 1
                path = owner / entry.name
                # CRITICAL: catalog quota includes foreign leases without reopening their locked
                # byte zero. Apply each closed file envelope, not the larger snapshot allowance.
                maximum = (
                    1024
                    if entry.name == _OWNER_LOCK
                    else MAX_MANIFEST_BYTES
                    if entry.name in {_MANIFEST, _PENDING}
                    else MAX_SNAPSHOT_BYTES
                )
                metadata = validate_regular_file(path, maximum_bytes=maximum).lstat()
                if metadata.st_nlink != 1:
                    raise DurableStateError("storage_unsafe")
                files.append((path, metadata))
        if snapshots > 3:
            raise DurableStateError("quota_directory")
        return tuple(files)

    def _manifest(self, owner: Path | None = None) -> StateManifest:
        directory = self.owner_directory if owner is None else owner
        path = directory / _MANIFEST
        if not _exists(path):
            return StateManifest(require_owner(directory.name))
        return _decode_manifest(_read(path, MAX_MANIFEST_BYTES)[0], directory.name)

    def _snapshot(self, owner: Path, name: str) -> tuple[StateSnapshot, os.stat_result]:
        matched = _SNAPSHOT_NAME.fullmatch(name)
        if matched is None:
            raise DurableStateError("manifest_invalid")
        payload, metadata = _read(owner / name, MAX_SNAPSHOT_BYTES)
        snapshot = decode_snapshot(payload, expected_owner=owner.name)
        if snapshot.revision != int(matched.group(1)) or hashlib.sha256(
            payload
        ).hexdigest() != matched.group(2):
            raise DurableStateError("snapshot_corrupt")
        return snapshot, metadata

    def _load(self, manifest: StateManifest) -> StateSnapshot | None:
        if manifest.latest is None:
            return None
        for reference in (manifest.latest, manifest.previous):
            if reference is None:
                continue
            try:
                snapshot, _ = self._snapshot(self.owner_directory, reference.file_name)
                if tuple(row.record_id for row in snapshot.records) != reference.record_ids:
                    raise DurableStateError("snapshot_corrupt")
                return snapshot
            except DurableStateError as error:
                if str(error) in {"version_unsupported", "owner_mismatch", "storage_unsafe"}:
                    raise
            except (FileNotFoundError, ArtifactStoreError, UnsafePathError) as error:
                if isinstance(error, UnsafePathError) or str(error) == "unsafe_store_entry":
                    raise DurableStateError("storage_unsafe") from error
        raise DurableStateError("snapshot_unavailable")

    def read(self) -> tuple[StateManifest, StateSnapshot | None]:
        with self._operation() as admitted:
            if not admitted:
                return StateManifest(self.owner_id), None
            manifest = self._manifest()
            return manifest, self._load(manifest)

    def _inventory(self) -> tuple[int, dict[str, set[str]]]:
        files = [
            (self.root / name, (self.root / name).lstat())
            for name in (_FAMILY_MARKER, _CATALOG_LOCK)
        ]
        owners = self._owners()
        for owner in owners:
            files.extend(self._files(owner))
        total = sum(metadata.st_size for _, metadata in files)
        if total > self.limits.max_total_bytes:
            raise DurableStateError("quota_bytes")
        identifiers: dict[str, set[str]] = {owner.name: set() for owner in owners}
        # CRITICAL: count every immutable/staged/previous file, not just the manifest's latest.
        # Otherwise a failed commit or another owner could evade the global recovery envelope.
        for path, _ in files:
            if _SNAPSHOT_NAME.fullmatch(path.name):
                snapshot, _ = self._snapshot(path.parent, path.name)
                identifiers[path.parent.name].update(row.record_id for row in snapshot.records)
            elif path.name in {_MANIFEST, _PENDING}:
                manifest = _decode_manifest(_read(path, MAX_MANIFEST_BYTES)[0], path.parent.name)
                for reference in (manifest.latest, manifest.previous):
                    if reference is not None:
                        identifiers[path.parent.name].update(reference.record_ids)
        if (
            any(len(values) > self.limits.max_owner_records for values in identifiers.values())
            or sum(len(values) for values in identifiers.values()) > self.limits.max_global_records
        ):
            raise DurableStateError("quota_records")
        return total, identifiers

    def _clean_unreferenced(self, manifest: StateManifest) -> None:
        keep = {
            reference.file_name
            for reference in (manifest.latest, manifest.previous)
            if reference is not None
        }
        for path, metadata in self._files(self.owner_directory):
            if path.name == _PENDING:
                _decode_manifest(_read(path, MAX_MANIFEST_BYTES)[0], self.owner_id)
                _delete(path, metadata)
            elif _SNAPSHOT_NAME.fullmatch(path.name) and path.name not in keep:
                _, verified = self._snapshot(self.owner_directory, path.name)
                if _identity(verified) != _identity(metadata):
                    raise DurableStateError("storage_unsafe")
                _delete(path, verified)

    def _cas(self, manifest: StateManifest, expected: int) -> None:
        _revision(expected)
        if expected != manifest.revision:
            raise DurableStateError("revision_conflict")
        if manifest.revision >= MAX_OWNER_REVISION:
            raise DurableStateError("revision_exhausted")

    def _publish(
        self, manifest: StateManifest, *, extra_bytes: int = 0, new_ids: tuple[str, ...] = ()
    ) -> None:
        payload = json_bytes(manifest.to_wire())
        _decode_manifest(payload, self.owner_id)
        total, owners = self._inventory()
        proposed = owners.get(self.owner_id, set()) | set(new_ids)
        if (
            len(proposed) > self.limits.max_owner_records
            or sum(len(values) for name, values in owners.items() if name != self.owner_id)
            + len(proposed)
            > self.limits.max_global_records
        ):
            raise DurableStateError("quota_records")
        if total + len(payload) + extra_bytes > self.limits.max_total_bytes:
            raise DurableStateError("quota_bytes")
        pending = self.owner_directory / _PENDING
        _write_new_file(pending, payload)
        _replace_manifest(pending, self.owner_directory / _MANIFEST)

    def set_enabled(self, enabled: bool, *, expected_revision: int) -> StateManifest:
        if type(enabled) is not bool:
            raise DurableStateError("intent_invalid")
        with self._operation(create=enabled) as admitted:
            manifest = self._manifest() if admitted else StateManifest(self.owner_id)
            self._cas(manifest, expected_revision)
            if not admitted:
                return manifest
            self._clean_unreferenced(manifest)
            updated = StateManifest(
                self.owner_id, manifest.revision + 1, enabled, manifest.latest, manifest.previous
            )
            self._publish(updated)
            return updated

    def save(self, records: tuple[StateRecord, ...], *, expected_revision: int) -> StateSnapshot:
        with self._operation() as admitted:
            manifest = self._manifest() if admitted else StateManifest(self.owner_id)
            self._cas(manifest, expected_revision)
            if not admitted or not manifest.enabled:
                raise DurableStateError("recovery_disabled")
            snapshot = StateSnapshot(self.owner_id, manifest.revision + 1, self.clock_ms(), records)
            payload = encode_snapshot(snapshot)
            self._clean_unreferenced(manifest)
            # Reserve the candidate and pending manifest together before creating either file.
            digest = hashlib.sha256(payload).hexdigest()
            name = f"revision-{snapshot.revision:016d}-{digest}.json"
            reference = SnapshotReference(
                name,
                snapshot.revision,
                tuple(row.record_id for row in sorted(records, key=lambda row: row.record_id)),
            )
            old = self._load(manifest)
            previous = None
            if (
                old is not None
                and manifest.latest is not None
                and not any(
                    row.closed_at_ms is not None
                    and snapshot.saved_at_ms - row.closed_at_ms >= CLOSED_RETENTION_MS
                    for row in old.records
                )
            ):
                previous = (
                    manifest.latest
                    if old.revision == manifest.latest.revision
                    else manifest.previous
                )
            updated = StateManifest(self.owner_id, snapshot.revision, True, reference, previous)
            total, owners = self._inventory()
            union = owners[self.owner_id] | set(reference.record_ids)
            if (
                len(union) > self.limits.max_owner_records
                or sum(len(values) for owner, values in owners.items() if owner != self.owner_id)
                + len(union)
                > self.limits.max_global_records
            ):
                raise DurableStateError("quota_records")
            if (
                total + len(payload) + len(json_bytes(updated.to_wire()))
                > self.limits.max_total_bytes
            ):
                raise DurableStateError("quota_bytes")
            if (
                sum(
                    bool(_SNAPSHOT_NAME.fullmatch(path.name))
                    for path, _ in self._files(self.owner_directory)
                )
                >= 3
            ):
                raise DurableStateError("quota_directory")
            _write_new_file(self.owner_directory / name, payload)
            self._publish(updated)
            # The commit is already durable. A cleanup refusal never reports a failed save or
            # deletes the admitted last good revision; preserved extras are charged next time.
            try:
                self._clean_unreferenced(updated)
            except (DurableStateError, ArtifactStoreError, UnsafePathError, OSError):
                pass
            return decode_snapshot(payload, expected_owner=self.owner_id)

    def reset(self, *, expected_revision: int) -> StateManifest:
        with self._operation() as admitted:
            manifest = self._manifest() if admitted else StateManifest(self.owner_id)
            self._cas(manifest, expected_revision)
            if not admitted:
                return manifest
            self._clean_unreferenced(manifest)
            verified = []
            for reference in (manifest.latest, manifest.previous):
                if reference is not None:
                    _, metadata = self._snapshot(self.owner_directory, reference.file_name)
                    verified.append((self.owner_directory / reference.file_name, metadata))
            updated = StateManifest(self.owner_id, manifest.revision + 1, False)
            self._publish(updated)
            for path, metadata in verified:
                _delete(path, metadata)
            return updated
