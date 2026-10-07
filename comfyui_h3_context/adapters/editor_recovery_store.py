"""Opt-in private editable recovery; closed snapshots precede atomic manifest acknowledgement."""

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
from typing import Any

from ..core.durable_workspace_state import DurableStateError, require_owner
from ..core.editor_recovery import (
    MAX_RECOVERY_BYTES,
    MAX_RECOVERY_REVISION,
    RECOVERY_SCHEMA,
    EditorRecoveryError,
    decode_recovery_snapshot,
    recovery_json,
    require_project,
)
from ..core.private_storage_layout import MIB, private_subtree_path
from ..core.project_document import closed, integer, project_bytes, rows
from ..core.retained_assets import require_asset_id
from ..core.safe_paths import (
    UnsafePathError,
    ensure_directory,
    validate_directory,
    validate_regular_file,
)
from .durable_state_store import _delete, _exists, _read, _replace_manifest
from .managed_artifact_scopes import _acquire_lock, _create_or_admit, _remove_empty_directory
from .media_runtime_resolution import _link_free_existing_prefix
from .segment_artifact_store import (
    ArtifactStoreError,
    _directory_identity,
    _identity,
    _validated_directories,
    _windows_native_path,
    _write_new_file,
)

MANIFEST_SCHEMA = "h3.context.project_recovery_manifest.v1"
MAX_MANIFEST_BYTES = 32768
CLOSED_RETENTION_MS = 7 * 86400 * 1000
_SNAPSHOT = re.compile(r"revision-([0-9]{16})-([0-9a-f]{64})\.json\Z")
_FAMILY = ".h3-project-recovery-v1"
_CATALOG = ".catalog.lock"
_OWNER = ".owner.lock"
_MANIFEST = "manifest.json"
_NEXT = "manifest.next.json"
_INTENT = "intent.json"
_FAMILY_BYTES = project_bytes({"schema": "h3.context.project_recovery_family.v1"})
_CATALOG_BYTES = project_bytes({"schema": "h3.context.project_recovery_catalog_lock.v1"})


@dataclass(frozen=True, slots=True)
class RecoveryStoreLimits:
    max_owner_records: int = 16
    max_global_records: int = 64
    max_total_bytes: int = 640 * MIB

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.max_owner_records, 16),
            (self.max_global_records, 64),
            (self.max_total_bytes, 640 * MIB),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise EditorRecoveryError("quota_bytes")


def _owner_bytes(owner: str) -> bytes:
    return project_bytes({"schema": "h3.context.project_recovery_owner_lock.v1", "owner_id": owner})


def _references(value: object) -> tuple[str, ...]:
    identifiers = tuple(require_asset_id(key) for key in rows(value, 128))
    if tuple(sorted(set(identifiers))) != identifiers:
        raise EditorRecoveryError()
    return identifiers


def _manifest_value(value: object, owner: str) -> dict[str, Any]:
    wire = closed(value, {"schema", "owner_id", "revision", "enabled", "include_video", "records"})
    if wire["schema"] != MANIFEST_SCHEMA:
        raise EditorRecoveryError("version_unsupported")
    if wire["owner_id"] != owner:
        raise EditorRecoveryError("owner_mismatch")
    integer(wire["revision"], 1, MAX_RECOVERY_REVISION)
    if type(wire["enabled"]) is not bool or type(wire["include_video"]) is not bool:
        raise EditorRecoveryError()
    identifiers = set()
    for item in rows(wire["records"], 16):
        row = closed(
            item,
            {
                "project_id",
                "revision",
                "saved_at_ms",
                "closed_at_ms",
                "latest",
                "previous",
                "retained_ids",
            },
        )
        project = require_project(row["project_id"])
        if project in identifiers:
            raise EditorRecoveryError()
        identifiers.add(project)
        revision = integer(row["revision"], 1, wire["revision"])
        integer(row["saved_at_ms"], 1, 4_102_444_800_000)
        if row["closed_at_ms"] is not None:
            integer(row["closed_at_ms"], row["saved_at_ms"], 4_102_444_800_000)
        for index, name in enumerate((row["latest"], row["previous"])):
            if index and name is None:
                continue
            match = _SNAPSHOT.fullmatch(name) if type(name) is str else None
            if (
                match is None
                or not 1 <= int(match[1]) <= revision
                or (not index and int(match[1]) != revision)
                or (index and int(match[1]) >= revision)
            ):
                raise EditorRecoveryError()
        _references(row["retained_ids"])
    return wire


class EditorRecoveryStore:
    """One qualified owner lease; no filesystem activity until an explicit operation."""

    def __init__(
        self,
        private_root: Path,
        owner_id: str,
        *,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        limits: RecoveryStoreLimits | None = None,
        reference_sync: Callable[[str, tuple[str, ...]], None] | None = None,
    ) -> None:
        self.owner_id = require_owner(owner_id)
        if not isinstance(private_root, Path) or not private_root.is_absolute():
            raise EditorRecoveryError("storage_unsafe")
        # IMPORTANT: carry the same lexical namespace through pathlib and direct Win32 calls.
        # Prefixing only CreateFileW leaves Python lstat/cleanup unable to see the created file.
        private_root = (
            Path(_windows_native_path(private_root, force=True))
            if os.name == "nt"
            else private_root
        )
        self.private_root = private_root
        self.root = private_subtree_path(private_root, "project-recovery") / "v1"
        self.owner_directory = self.root / self.owner_id
        self.clock_ms, self.limits = clock_ms, limits or RecoveryStoreLimits()
        self.reference_sync = reference_sync
        self._lock = threading.RLock()
        self._lease: int | None = None
        self._owner_identity: tuple[int, int] | None = None

    def close(self) -> None:
        with self._lock:
            if self._lease is not None:
                os.close(self._lease)
                self._lease, self._owner_identity = None, None

    @contextmanager
    def _operation(self, *, create: bool = False) -> Iterator[bool]:
        with self._lock:
            catalog = None
            try:
                if not _link_free_existing_prefix(self.root):
                    raise EditorRecoveryError("storage_unsafe")
                if not _exists(self.root) and not create:
                    yield False
                    return
                if create:
                    ensure_directory(self.root)
                    _create_or_admit(self.root / _FAMILY, _FAMILY_BYTES)
                    _create_or_admit(self.root / _CATALOG, _CATALOG_BYTES)
                with _validated_directories(self.private_root, self.root.parent, self.root):
                    if _read(self.root / _FAMILY, 1024)[0] != _FAMILY_BYTES:
                        raise EditorRecoveryError("storage_unsafe")
                    try:
                        catalog = _acquire_lock(self.root / _CATALOG, _CATALOG_BYTES)
                    except ArtifactStoreError as error:
                        if str(error) == "scope_busy":
                            raise EditorRecoveryError("catalog_busy") from None
                        raise
                    if not _exists(self.owner_directory):
                        if not create:
                            yield False
                            return
                        self._inventory()
                        ensure_directory(self.owner_directory)
                    with _validated_directories(self.owner_directory):
                        identity = _directory_identity(self.owner_directory.lstat())
                        path, expected = self.owner_directory / _OWNER, _owner_bytes(self.owner_id)
                        if self._lease is None:
                            _create_or_admit(path, expected)
                            try:
                                self._lease = _acquire_lock(path, expected)
                            except ArtifactStoreError as error:
                                if str(error) == "scope_busy":
                                    raise EditorRecoveryError("owner_busy") from None
                                raise
                            self._owner_identity = identity
                        before, opened = (
                            validate_regular_file(path, maximum_bytes=1024).lstat(),
                            os.fstat(self._lease),
                        )
                        if (
                            identity != self._owner_identity
                            or before.st_nlink != 1
                            or opened.st_nlink != 1
                            or _identity(before) != _identity(opened)
                        ):
                            raise EditorRecoveryError("storage_unsafe")
                        # CRITICAL: the byte-zero lease does not protect the rest of its payload.
                        os.lseek(self._lease, 0, os.SEEK_SET)
                        if os.read(self._lease, 1025) != expected or _identity(
                            path.lstat()
                        ) != _identity(opened):
                            raise EditorRecoveryError("storage_unsafe")
                        self._inventory()
                        self._reconcile()
                        yield True
            except EditorRecoveryError:
                raise
            except (UnsafePathError, ArtifactStoreError, DurableStateError, OSError) as error:
                unsafe = isinstance(error, UnsafePathError) or str(error) in {
                    "unsafe_store_entry",
                    "storage_unsafe",
                }
                raise EditorRecoveryError(
                    "storage_unsafe" if unsafe else "storage_unavailable"
                ) from None
            except (ValueError, TypeError, KeyError, RecursionError):
                raise EditorRecoveryError() from None
            finally:
                if catalog is not None:
                    os.close(catalog)

    def _default(self) -> dict[str, Any]:
        return {
            "schema": MANIFEST_SCHEMA,
            "owner_id": self.owner_id,
            "revision": 0,
            "enabled": False,
            "include_video": False,
            "records": [],
        }

    def _manifest(self) -> dict[str, Any]:
        path = self.owner_directory / _MANIFEST
        return (
            _manifest_value(recovery_json(_read(path, MAX_MANIFEST_BYTES)[0]), self.owner_id)
            if _exists(path)
            else self._default()
        )

    def _inventory(self) -> tuple[int, dict[str, set[str]]]:
        total, owners = 0, {}
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries, 1):
                if count > 66:
                    raise EditorRecoveryError("quota_directory")
                path = self.root / entry.name
                if entry.name in {_FAMILY, _CATALOG}:
                    metadata = validate_regular_file(path, maximum_bytes=1024).lstat()
                    if metadata.st_nlink != 1:
                        raise EditorRecoveryError("storage_unsafe")
                    total += metadata.st_size
                    continue
                owner = require_owner(entry.name)
                validate_directory(path)
                projects: set[str] = set()
                owners[owner] = projects
                with os.scandir(path) as children:
                    for index, child in enumerate(children, 1):
                        if index > 20:
                            raise EditorRecoveryError("quota_directory")
                        target = path / child.name
                        if child.name in {_OWNER, _MANIFEST, _NEXT, _INTENT}:
                            maximum = 1024 if child.name == _OWNER else MAX_MANIFEST_BYTES
                            metadata = validate_regular_file(target, maximum_bytes=maximum).lstat()
                            if metadata.st_nlink != 1:
                                raise EditorRecoveryError("storage_unsafe")
                            total += metadata.st_size
                            if child.name in {_MANIFEST, _NEXT}:
                                manifest = _manifest_value(
                                    recovery_json(_read(target, maximum)[0]), owner
                                )
                                projects.update(row["project_id"] for row in manifest["records"])
                            continue
                        project = require_project(child.name)
                        projects.add(project)
                        validate_directory(target)
                        with os.scandir(target) as snapshots:
                            for number, snapshot in enumerate(snapshots, 1):
                                if number > 3:
                                    raise EditorRecoveryError("quota_directory")
                                if _SNAPSHOT.fullmatch(snapshot.name) is None:
                                    raise EditorRecoveryError("storage_unverified")
                                metadata = validate_regular_file(
                                    target / snapshot.name, maximum_bytes=MAX_RECOVERY_BYTES
                                ).lstat()
                                if metadata.st_nlink != 1:
                                    raise EditorRecoveryError("storage_unsafe")
                                total += metadata.st_size
                if len(projects) > self.limits.max_owner_records:
                    raise EditorRecoveryError("quota_records")
        if sum(len(projects) for projects in owners.values()) > self.limits.max_global_records:
            raise EditorRecoveryError("quota_records")
        if total > self.limits.max_total_bytes:
            raise EditorRecoveryError("quota_bytes")
        return total, owners

    def _reserve(self, extra: int, project: str | None = None) -> None:
        total, owners = self._inventory()
        current = owners.get(self.owner_id, set()) | (set() if project is None else {project})
        if (
            len(current) > self.limits.max_owner_records
            or sum(len(ids) for owner, ids in owners.items() if owner != self.owner_id)
            + len(current)
            > self.limits.max_global_records
        ):
            raise EditorRecoveryError("quota_records")
        if total + extra > self.limits.max_total_bytes:
            raise EditorRecoveryError("quota_bytes")

    def _snapshot(self, project: str, name: str) -> dict[str, Any]:
        match = _SNAPSHOT.fullmatch(name)
        if match is None:
            raise EditorRecoveryError()
        if not _exists(self.owner_directory / project / name):
            raise EditorRecoveryError("snapshot_unavailable")
        with _validated_directories(self.owner_directory / project):
            payload = _read(self.owner_directory / project / name, MAX_RECOVERY_BYTES)[0]
            value = decode_recovery_snapshot(payload, expected_owner=self.owner_id)
            if (
                value["project_id"] != project
                or value["revision"] != int(match[1])
                or hashlib.sha256(payload).hexdigest() != match[2]
            ):
                raise EditorRecoveryError("snapshot_corrupt")
            return value

    def _load(self, row: dict[str, Any]) -> tuple[dict[str, Any], str]:
        for name in (row["latest"], row["previous"]):
            if name is None:
                continue
            try:
                return self._snapshot(row["project_id"], name), name
            except EditorRecoveryError as error:
                if error.code in {"version_unsupported", "owner_mismatch", "storage_unsafe"}:
                    raise
            except FileNotFoundError:
                pass
        raise EditorRecoveryError("snapshot_unavailable")

    def _publish(self, manifest: dict[str, Any]) -> None:
        payload = project_bytes(manifest, maximum_bytes=MAX_MANIFEST_BYTES)
        _manifest_value(recovery_json(payload), self.owner_id)
        _write_new_file(self.owner_directory / _NEXT, payload)
        _replace_manifest(
            self.owner_directory / _NEXT,
            self.owner_directory / _MANIFEST,
            maximum_bytes=MAX_MANIFEST_BYTES,
        )

    def _next(self, manifest: dict[str, Any]) -> dict[str, Any]:
        if manifest["revision"] >= MAX_RECOVERY_REVISION:
            raise EditorRecoveryError("revision_exhausted")
        return dict(manifest, revision=manifest["revision"] + 1)

    def _sync(self, project: str, ids: tuple[str, ...]) -> None:
        if self.reference_sync is None:
            if ids:
                raise EditorRecoveryError("media_unavailable")
        else:
            self.reference_sync(project, ids)

    def _intent(
        self,
        project: str,
        before: tuple[str, ...],
        after: tuple[str, ...],
        manifest: dict[str, Any],
    ) -> bytes:
        return project_bytes(
            {
                "schema": "h3.context.project_recovery_intent.v1",
                "owner_id": self.owner_id,
                "project_id": project,
                "before": list(before),
                "after": list(after),
                "manifest": manifest,
            },
            maximum_bytes=MAX_MANIFEST_BYTES,
        )

    def _reconcile(self) -> None:
        path = self.owner_directory / _INTENT
        if _exists(path):
            value = closed(
                recovery_json(_read(path, MAX_MANIFEST_BYTES)[0]),
                {"schema", "owner_id", "project_id", "before", "after", "manifest"},
            )
            if (
                value["schema"] != "h3.context.project_recovery_intent.v1"
                or value["owner_id"] != self.owner_id
            ):
                raise EditorRecoveryError("owner_mismatch")
            project = require_project(value["project_id"])
            intended = _manifest_value(value["manifest"], self.owner_id)
            current = self._manifest()
            if current["revision"] > intended["revision"]:
                raise EditorRecoveryError("storage_unverified")
            committed = current == intended
            self._sync(project, _references(value["after"] if committed else value["before"]))
            _delete(path, path.lstat())
        next_path = self.owner_directory / _NEXT
        if _exists(next_path):
            _manifest_value(recovery_json(_read(next_path, MAX_MANIFEST_BYTES)[0]), self.owner_id)
            _delete(next_path, next_path.lstat())

    def _cleanup(self, manifest: dict[str, Any]) -> None:
        references = {
            row["project_id"]: {row["latest"], row["previous"]} for row in manifest["records"]
        }
        for directory in self.owner_directory.iterdir():
            if directory.name in {_OWNER, _MANIFEST, _NEXT, _INTENT}:
                continue
            project = require_project(directory.name)
            validate_directory(directory)
            for path in directory.iterdir():
                if path.name not in references.get(project, set()):
                    # SECURITY: never clean arbitrary or corrupt bytes on a recognized-looking path.
                    self._snapshot(project, path.name)
                    _delete(path, path.lstat())
            if project not in references and not any(directory.iterdir()):
                before = _directory_identity(directory.lstat())
                with _validated_directories(self.owner_directory):
                    _remove_empty_directory(
                        directory, before, _directory_identity(self.owner_directory.lstat())
                    )

    def inventory(self) -> dict[str, Any]:
        with self._operation() as admitted:
            manifest = self._manifest() if admitted else self._default()
            total = self._inventory()[0] if admitted else 0
            return {
                "enabled": manifest["enabled"],
                "include_video": manifest["include_video"],
                "revision": manifest["revision"],
                "charged_bytes": total,
                "records": [
                    {
                        key: row[key]
                        for key in ("project_id", "revision", "saved_at_ms", "closed_at_ms")
                    }
                    for row in manifest["records"]
                ],
            }

    def settings(
        self, enabled: bool, include_video: bool, *, expected_revision: int
    ) -> dict[str, Any]:
        if type(enabled) is not bool or type(include_video) is not bool:
            raise EditorRecoveryError("command_invalid")
        with self._operation(create=enabled) as admitted:
            current = self._manifest() if admitted else self._default()
            if type(expected_revision) is not int or expected_revision != current["revision"]:
                raise EditorRecoveryError("revision_conflict")
            if admitted and (
                enabled != current["enabled"] or include_video != current["include_video"]
            ):
                self._cleanup(current)
                updated = dict(self._next(current), enabled=enabled, include_video=include_video)
                self._reserve(len(project_bytes(updated)))
                self._publish(updated)
        return self.inventory()

    def read(self, project_id: str) -> dict[str, Any]:
        project = require_project(project_id)
        with self._operation() as admitted:
            row = (
                next(
                    (row for row in self._manifest()["records"] if row["project_id"] == project),
                    None,
                )
                if admitted
                else None
            )
            if row is None:
                raise EditorRecoveryError("project_unavailable")
            return self._load(row)[0]

    def save(
        self, project_id: str, value: object, *, retained_ids: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        project = require_project(project_id)
        data = closed(value, {"document", "history"})
        ids = _references(list(retained_ids))
        with self._operation() as admitted:
            current = self._manifest() if admitted else self._default()
            if not admitted or not current["enabled"]:
                raise EditorRecoveryError("recovery_disabled")
            if ids and not current["include_video"]:
                raise EditorRecoveryError("media_unavailable")
            self._cleanup(current)
            updated = self._next(current)
            snapshot = {
                "schema": RECOVERY_SCHEMA,
                "owner_id": self.owner_id,
                "project_id": project,
                "revision": updated["revision"],
                "saved_at_ms": self.clock_ms(),
                **data,
            }
            payload = project_bytes(snapshot, maximum_bytes=MAX_RECOVERY_BYTES)
            decode_recovery_snapshot(payload, expected_owner=self.owner_id)
            referenced = {
                row["retained_id"]
                for row in snapshot["document"]["media"]
                if row["retained_id"] is not None
            }
            if referenced != set(ids):
                raise EditorRecoveryError("media_unavailable")
            name = (
                f"revision-{snapshot['revision']:016d}-{hashlib.sha256(payload).hexdigest()}.json"
            )
            prior = next((row for row in current["records"] if row["project_id"] == project), None)
            previous, prior_ids = None, ()
            if prior is not None:
                old, previous = self._load(prior)
                if previous != prior["latest"]:
                    raise EditorRecoveryError("snapshot_corrupt")
                prior_ids = tuple(prior["retained_ids"])
                ids = tuple(
                    sorted(
                        set(ids)
                        | {
                            row["retained_id"]
                            for row in old["document"]["media"]
                            if row["retained_id"] is not None
                        }
                    )
                )
            row = {
                "project_id": project,
                "revision": snapshot["revision"],
                "saved_at_ms": snapshot["saved_at_ms"],
                "closed_at_ms": None,
                "latest": name,
                "previous": previous,
                "retained_ids": list(ids),
            }
            updated["records"] = [
                item for item in current["records"] if item["project_id"] != project
            ] + [row]
            intent = self._intent(project, prior_ids, ids, updated)
            self._reserve(len(payload) + len(intent) + len(project_bytes(updated)), project)
            ensure_directory(self.owner_directory / project)
            # CRITICAL: recoverable intent precedes pins; manifest ack follows closed data.
            _write_new_file(self.owner_directory / _INTENT, intent)
            self._sync(project, ids)
            _write_new_file(self.owner_directory / project / name, payload)
            self._publish(updated)
            self._reconcile()
            self._cleanup(updated)
            return snapshot

    def close_record(self, project_id: str) -> None:
        project = require_project(project_id)
        with self._operation() as admitted:
            if not admitted:
                raise EditorRecoveryError("project_unavailable")
            current = self._manifest()
            updated = self._next(current)
            updated["records"] = [
                dict(row, closed_at_ms=max(row["saved_at_ms"], self.clock_ms()))
                if row["project_id"] == project
                else row
                for row in current["records"]
            ]
            self._reserve(len(project_bytes(updated)))
            self._publish(updated)

    def clear(self, project_id: str) -> None:
        project = require_project(project_id)
        with self._operation() as admitted:
            if not admitted:
                raise EditorRecoveryError("project_unavailable")
            current = self._manifest()
            row = next((row for row in current["records"] if row["project_id"] == project), None)
            if row is None:
                raise EditorRecoveryError("project_unavailable")
            for name in (row["latest"], row["previous"]):
                if name is not None:
                    self._snapshot(project, name)
            self._cleanup(current)
            updated = self._next(current)
            updated["records"] = [
                item for item in current["records"] if item["project_id"] != project
            ]
            intent = self._intent(project, tuple(row["retained_ids"]), (), updated)
            self._reserve(len(intent) + len(project_bytes(updated)))
            _write_new_file(self.owner_directory / _INTENT, intent)
            # CRITICAL: remove the durable document reference before releasing retained media pins.
            self._publish(updated)
            self._reconcile()
            self._cleanup(updated)

    def prune_closed(self, *, active_ids: set[str]) -> int:
        inventory = self.inventory()
        candidates = [
            row["project_id"]
            for row in inventory["records"]
            if row["project_id"] not in active_ids
            and row["closed_at_ms"] is not None
            and self.clock_ms() - row["closed_at_ms"] >= CLOSED_RETENTION_MS
        ]
        for project in candidates:
            self.clear(project)
        return len(candidates)
