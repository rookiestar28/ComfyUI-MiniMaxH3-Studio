"""Production-only volatile artifact scope ownership and bounded next-start cleanup."""

from __future__ import annotations

import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from ..core.canonical import canonical_bytes
from ..core.private_storage_layout import MANAGED_ARTIFACT_SUBTREE, MIB, private_subtree_path
from ..core.safe_paths import (
    UnsafePathError,
    ensure_directory,
    validate_directory,
    validate_regular_file,
)
from .media_runtime_resolution import (
    ComfyHostRootPort,
    HostRootError,
    _link_free_existing_prefix,
    _overlaps,
)
from .segment_artifact_store import (
    _ARTIFACT_FILE,
    _PARTIAL_FILE,
    _RECEIPT_FILE,
    _TEMP_FILE,
    SEGMENT_ARTIFACT_STORE_SCHEMA,
    SEGMENT_ARTIFACT_STORE_STATE_SCHEMA,
    ArtifactStoreError,
    ArtifactStorePolicy,
    PrivateSegmentArtifactStore,
    _directory_identity,
    _identity,
    _is_link_or_reparse,
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

_FAMILY_MARKER = ".h3-managed-artifact-scopes-v1"
_GUARD = ".cleanup.lock"
_OWNER = ".owner.lock"
_STORE_MARKER = ".h3-segment-artifact-store-v1"
_STATE = "store-state.json"
_SCOPE = re.compile(r"[A-Za-z0-9_-]{32}\Z")
_FAMILY = canonical_bytes({"schema": "h3.context.managed_artifact_scopes.v1"})
_GUARD_PAYLOAD = canonical_bytes({"schema": "h3.context.managed_artifact_cleanup_lock.v1"})
_POLICY = ArtifactStorePolicy(
    max_artifact_bytes=128 * MIB,
    max_total_bytes=1024 * MIB,
    max_entries=64,
    ttl_seconds=604800,
    max_concurrent_writes=2,
    max_recovery_entries=128,
)
_STORES_LOCK = threading.RLock()
# Process ownership deliberately outlives individual coordinator/store wrappers and GC.
_STORES: dict[str, tuple[PrivateSegmentArtifactStore, int, ArtifactStorePolicy]] = {}


@dataclass(frozen=True, slots=True)
class ScopeSweepLimits:
    max_entries: int = 64
    max_bytes: int = 1280 * MIB
    max_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            type(self.max_entries) is not int
            or not 1 <= self.max_entries <= 64
            or type(self.max_bytes) is not int
            or not 1 <= self.max_bytes <= 1280 * MIB
            or type(self.max_seconds) not in (float, int)
            or not 0 < self.max_seconds <= 5
        ):
            raise ArtifactStoreError("scope_limits")


_DEFAULT_SWEEP_LIMITS = ScopeSweepLimits()


@dataclass(slots=True)
class ScopeSweepReport:
    scanned_entries: int = 0
    removed_scopes: int = 0
    removed_files: int = 0
    retained_scopes: int = 0
    busy_scopes: int = 0
    work_bytes: int = 0
    stop_reason: str = "complete"


class _SweepStopped(Exception):
    pass


class _Budget:
    def __init__(
        self, limits: ScopeSweepLimits, clock: Callable[[], float], cancel: Callable[[], bool]
    ):
        self.limits, self.clock, self.cancel = limits, clock, cancel
        self.started = clock()
        self.report = ScopeSweepReport()

    def reserve(self, charge: int = 0) -> None:
        reason = (
            "cancelled"
            if self.cancel()
            else "time"
            if self.clock() - self.started >= self.limits.max_seconds
            else "bytes"
            if self.report.work_bytes + charge > self.limits.max_bytes
            else None
        )
        if reason is not None:
            self.report.stop_reason = reason
            raise _SweepStopped

    def check(self, charge: int = 0) -> None:
        self.reserve(charge)
        self.report.work_bytes += charge


def resolve_managed_artifact_root(host: ComfyHostRootPort, scope: str) -> Path:
    if type(scope) is not str or _SCOPE.fullmatch(scope) is None:
        raise ArtifactStoreError("scope_identity")
    try:
        private = host.private_root()
        served = host.served_roots()
        # CRITICAL: served temp is never a private fallback; ComfyUI serves and clears it.
        if any(_overlaps(private, root) for root in served) or not _link_free_existing_prefix(
            private
        ):
            raise ArtifactStoreError("unsafe_private_root")
        selected = private_subtree_path(private, MANAGED_ARTIFACT_SUBTREE) / scope
        if not _link_free_existing_prefix(selected):
            raise ArtifactStoreError("unsafe_private_root")
        return selected
    except HostRootError as exc:
        raise ArtifactStoreError("private_root_unavailable") from exc


def _owner_payload(scope: str) -> bytes:
    return canonical_bytes({"schema": "h3.context.managed_artifact_owner.v1", "scope": scope})


def _windows_open_lock(path: Path) -> int:
    import ctypes

    kernel = _windows_dll("kernel32")
    create = kernel.CreateFileW
    create.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create.restype = ctypes.c_void_p
    handle = create(
        _windows_native_path(path), 0x80000000 | 0x40000000, 0x1 | 0x2, None, 3, 0x00200080, None
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError(_windows_last_error(), "scope_lock_unavailable")
    try:
        return _msvcrt_open_osfhandle(int(handle), os.O_RDWR | getattr(os, "O_BINARY", 0))
    except BaseException:
        kernel.CloseHandle(ctypes.c_void_p(handle))
        raise


def _acquire_lock(path: Path, expected: bytes, budget: _Budget | None = None) -> int:
    if budget is not None:
        budget.check()
    admitted = validate_regular_file(path, maximum_bytes=1024)
    before = admitted.lstat()
    if before.st_nlink != 1:
        raise ArtifactStoreError("unsafe_scope_lock")
    descriptor = (
        _windows_open_lock(admitted)
        if os.name == "nt"
        else os.open(admitted, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
        ):
            raise ArtifactStoreError("unsafe_scope_lock")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                # IMPORTANT: lazy native access preserves byte locking on Windows while POSIX
                # stubs omit the API; never replace either branch with a no-op or blocking lock.
                locking = cast(Callable[[int, int, int], None], getattr(msvcrt, "locking"))  # noqa: B009
                locking(descriptor, int(getattr(msvcrt, "LK_NBLCK")), 1)  # noqa: B009
            else:
                import fcntl

                flock = cast(Callable[[int, int], None], getattr(fcntl, "flock"))  # noqa: B009
                exclusive = int(getattr(fcntl, "LOCK_EX"))  # noqa: B009
                nonblocking = int(getattr(fcntl, "LOCK_NB"))  # noqa: B009
                flock(descriptor, exclusive | nonblocking)
        except OSError as exc:
            raise ArtifactStoreError("scope_busy") from exc
        # IMPORTANT: read through the lock owner; reopening byte zero fails on Windows.
        os.lseek(descriptor, 0, os.SEEK_SET)
        if budget is not None:
            budget.reserve(opened.st_size + 1)
        payload = os.read(descriptor, opened.st_size + 1)
        if budget is not None:
            budget.report.work_bytes += len(payload)
            budget.check()
        if (
            payload != expected
            or _identity(os.fstat(descriptor)) != _identity(opened)
            or _identity(admitted.lstat()) != _identity(opened)
        ):
            raise ArtifactStoreError("unsafe_scope_lock")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_metadata(path: Path, budget: _Budget | None = None) -> tuple[bytes, os.stat_result]:
    if budget is not None:
        budget.check()
    before = validate_regular_file(path, maximum_bytes=1024).lstat()
    if budget is not None:
        budget.reserve(before.st_size + 1)
    payload = _read_regular_bytes(path, maximum_bytes=max(before.st_size, 1))
    if budget is not None:
        budget.report.work_bytes += len(payload)
        budget.check()
    if _identity(path.lstat()) != _identity(before):
        raise ArtifactStoreError("scope_identity_changed")
    return payload, before


def _create_or_admit(path: Path, payload: bytes) -> None:
    if path.exists():
        return
    try:
        _write_new_file(path, payload)
    except ArtifactStoreError as exc:
        # IMPORTANT: exclusive-create races are wrapped by the shared writer. Admit
        # only a completed exact schema below, never partial IO or arbitrary failures.
        cause = exc.__cause__
        if not isinstance(cause, FileExistsError) and getattr(cause, "winerror", None) not in {
            80,
            183,
        }:
            raise


@contextmanager
def _namespace(parent: Path, budget: _Budget | None = None) -> Iterator[None]:
    if budget is not None:
        budget.check()
    existed = parent.exists()
    admitted = ensure_directory(parent)
    with _validated_directories(admitted):
        marker, guard = admitted / _FAMILY_MARKER, admitted / _GUARD
        if not marker.exists():
            if existed:
                with os.scandir(admitted) as entries:
                    if next(entries, None) is not None:
                        raise ArtifactStoreError("foreign_scope_namespace")
            _create_or_admit(marker, _FAMILY)
        if _read_metadata(marker, budget)[0] != _FAMILY:
            raise ArtifactStoreError("foreign_scope_namespace")
        _create_or_admit(guard, _GUARD_PAYLOAD)
        descriptor = _acquire_lock(guard, _GUARD_PAYLOAD, budget)
        try:
            yield
        finally:
            os.close(descriptor)


def _entries(directory: Path, maximum: int, budget: _Budget) -> tuple[Path, ...]:
    result: list[Path] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            budget.check()
            if len(result) >= maximum:
                raise ArtifactStoreError("scope_inventory_limit")
            result.append(directory / entry.name)
    return tuple(result)


def _inventory(
    scope: Path, budget: _Budget, owner: os.stat_result
) -> tuple[
    list[tuple[Path, os.stat_result]],
    list[tuple[Path, tuple[int, int]]],
    dict[Path, os.stat_result],
]:
    allowed = {_OWNER, _STORE_MARKER, _STATE, "artifacts", "receipts", "staging"}
    roots = _entries(scope, len(allowed), budget)
    if any(path.name not in allowed for path in roots):
        raise ArtifactStoreError("foreign_scope_entry")
    marker, marker_identity = _read_metadata(scope / _STORE_MARKER, budget)
    if marker != canonical_bytes(
        {"schema": SEGMENT_ARTIFACT_STORE_SCHEMA, "policy": asdict(_POLICY)}
    ):
        raise ArtifactStoreError("foreign_scope_policy")
    files, directories = [], []
    metadata = {scope / _STORE_MARKER: marker_identity, scope / _OWNER: owner}
    if (scope / _STATE).exists():
        state, state_identity = _read_metadata(scope / _STATE, budget)
        metadata[scope / _STATE] = state_identity
        if state not in (
            canonical_bytes({"schema": SEGMENT_ARTIFACT_STORE_STATE_SCHEMA, "enabled": True}),
            canonical_bytes({"schema": SEGMENT_ARTIFACT_STORE_STATE_SCHEMA, "enabled": False}),
        ):
            raise ArtifactStoreError("foreign_scope_state")
    sources = (
        ("artifacts", (_ARTIFACT_FILE,)),
        ("receipts", (_RECEIPT_FILE,)),
        ("staging", (_PARTIAL_FILE, _TEMP_FILE)),
    )
    remaining = _POLICY.max_recovery_entries
    for name, patterns in sources:
        path = scope / name
        if not path.exists():
            continue
        budget.check()
        directory = validate_directory(path)
        directories.append((directory, _directory_identity(directory.lstat())))
        children = _entries(directory, remaining, budget)
        remaining -= len(children)
        for child in children:
            budget.check()
            if not any(pattern.fullmatch(child.name) for pattern in patterns):
                raise ArtifactStoreError("foreign_scope_entry")
            bound = _POLICY.max_artifact_bytes if name != "receipts" else 64 * 1024
            observed = validate_regular_file(child, maximum_bytes=bound).lstat()
            if observed.st_nlink != 1:
                raise ArtifactStoreError("unsafe_scope_entry")
            files.append((child, observed))
    return files, directories, metadata


def _unlink_exact(path: Path, expected: os.stat_result, parent_identity: tuple[int, int]) -> None:
    if _directory_identity(validate_directory(path.parent).lstat()) != parent_identity:
        raise ArtifactStoreError("scope_identity_changed")
    if _identity(validate_regular_file(path).lstat()) != _identity(expected):
        raise ArtifactStoreError("scope_identity_changed")
    if os.name == "nt":
        _windows_delete_exact(path, expected=expected)
    else:
        # Windows stubs omit these flags; do not weaken no-follow on the POSIX path.
        flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY")) | int(getattr(os, "O_NOFOLLOW"))  # noqa: B009
        descriptor = os.open(path.parent, flags)
        try:
            if (
                _directory_identity(os.fstat(descriptor)) != parent_identity
                or _directory_identity(validate_directory(path.parent).lstat()) != parent_identity
                or _identity(os.stat(path.name, dir_fd=descriptor, follow_symlinks=False))
                != _identity(expected)
            ):
                raise ArtifactStoreError("scope_identity_changed")
            os.unlink(path.name, dir_fd=descriptor)
        finally:
            os.close(descriptor)


def _remove_empty_directory(
    path: Path, expected: tuple[int, int], parent_identity: tuple[int, int]
) -> None:
    if _directory_identity(validate_directory(path.parent).lstat()) != parent_identity:
        raise ArtifactStoreError("scope_identity_changed")
    if _directory_identity(validate_directory(path).lstat()) != expected:
        raise ArtifactStoreError("scope_identity_changed")
    if os.name != "nt":
        flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY")) | int(getattr(os, "O_NOFOLLOW"))  # noqa: B009
        directory_descriptor = os.open(path.parent, flags)
        try:
            if (
                _directory_identity(os.fstat(directory_descriptor)) != parent_identity
                or _directory_identity(validate_directory(path.parent).lstat()) != parent_identity
                or _directory_identity(
                    os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
                )
                != expected
            ):
                raise ArtifactStoreError("scope_identity_changed")
            os.rmdir(path.name, dir_fd=directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return
    import ctypes
    from ctypes import wintypes

    kernel = _windows_dll("kernel32")
    create = kernel.CreateFileW
    create.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create.restype = ctypes.c_void_p
    # CRITICAL: the namespace guard and parent pin cover the child-pin reopen gap.
    # Delete only this exact ordinary empty directory; never follow or rmtree a junction.
    handle = create(
        _windows_native_path(path),
        0x00010000 | 0x80,
        0x1 | 0x2,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise ArtifactStoreError("scope_cleanup_failed")
    descriptor: int | None = None
    try:
        descriptor = _msvcrt_open_osfhandle(int(handle), os.O_RDONLY)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _is_link_or_reparse(path, opened)
            or _directory_identity(opened) != expected
            or _directory_identity(validate_directory(path).lstat()) != expected
        ):
            raise ArtifactStoreError("scope_identity_changed")

        class Disposition(ctypes.Structure):
            _fields_ = [("delete_file", ctypes.c_ubyte)]

        set_info = kernel.SetFileInformationByHandle
        set_info.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        set_info.restype = wintypes.BOOL
        info = Disposition(1)
        if not set_info(
            ctypes.c_void_p(_msvcrt_get_osfhandle(descriptor)),
            4,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ArtifactStoreError("scope_cleanup_failed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        else:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    if path.exists():
        raise ArtifactStoreError("scope_cleanup_failed")


def _reap(scope: Path, budget: _Budget) -> bool:
    budget.check()
    admitted = validate_directory(scope)
    identity = _directory_identity(admitted.lstat())
    parent_identity = _directory_identity(validate_directory(admitted.parent).lstat())
    descriptor = _acquire_lock(admitted / _OWNER, _owner_payload(admitted.name), budget)
    try:
        with _validated_directories(admitted):
            if _directory_identity(admitted.lstat()) != identity:
                raise ArtifactStoreError("scope_identity_changed")
            files, directories, metadata_files = _inventory(admitted, budget, os.fstat(descriptor))
            for path, observed in metadata_files.items():
                budget.check()
                if _identity(validate_regular_file(path).lstat()) != _identity(observed):
                    raise ArtifactStoreError("scope_identity_changed")
            directory_identities = dict(directories)
            # CRITICAL: inventory and dead-owner lock must precede EVERY data unlink.
            # Keep the namespace guard until ownership metadata and scope finalization finish.
            for path, metadata in files:
                budget.check(metadata.st_size)
                with _validated_directories(path.parent):
                    _unlink_exact(path, metadata, directory_identities[path.parent])
                budget.report.removed_files += 1
            for path, directory_identity in directories:
                budget.check()
                _remove_empty_directory(path, directory_identity, identity)
            state = admitted / _STATE
            if state in metadata_files:
                budget.check(metadata_files[state].st_size)
                _unlink_exact(state, metadata_files[state], identity)
            owner, marker = admitted / _OWNER, admitted / _STORE_MARKER
            # Reserve both final metadata unlinks before dropping the owner lock so
            # a byte-limit stop still leaves a restart-inspectable owned scope.
            budget.check(metadata_files[owner].st_size + metadata_files[marker].st_size)
            # Complete data deletion before releasing the owner byte lock. Legitimate
            # contenders still cannot enter because the namespace guard remains held.
            os.close(descriptor)
            descriptor = -1
            _unlink_exact(owner, metadata_files[owner], identity)
            _unlink_exact(marker, metadata_files[marker], identity)
        budget.check()
        _remove_empty_directory(admitted, identity, parent_identity)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sweep(parent: Path, budget: _Budget) -> ScopeSweepReport:
    try:
        with os.scandir(parent) as entries:
            while True:
                budget.check()
                if budget.report.scanned_entries >= budget.limits.max_entries:
                    budget.report.stop_reason = "entries"
                    break
                try:
                    entry = next(entries)
                except StopIteration:
                    break
                budget.report.scanned_entries += 1
                if entry.name in {_FAMILY_MARKER, _GUARD}:
                    continue
                if _SCOPE.fullmatch(entry.name) is None:
                    budget.report.retained_scopes += 1
                    continue
                try:
                    if _reap(parent / entry.name, budget):
                        budget.report.removed_scopes += 1
                except ArtifactStoreError as exc:
                    budget.report.retained_scopes += 1
                    if exc.code == "scope_busy":
                        budget.report.busy_scopes += 1
                except (OSError, UnsafePathError):
                    budget.report.retained_scopes += 1
    except _SweepStopped:
        pass
    return budget.report


def sweep_managed_artifact_scopes(
    parent: Path,
    *,
    limits: ScopeSweepLimits = _DEFAULT_SWEEP_LIMITS,
    clock: Callable[[], float] = time.monotonic,
    should_cancel: Callable[[], bool] = lambda: False,
) -> ScopeSweepReport:
    """Explicit bounded cleanup; acquiring the namespace guard is not optional."""
    budget = _Budget(limits, clock, should_cancel)
    try:
        with _namespace(parent, budget):
            return _sweep(parent, budget)
    except _SweepStopped:
        return budget.report
    except (OSError, UnsafePathError) as exc:
        raise ArtifactStoreError("scope_namespace_unavailable") from exc


def open_managed_artifact_store(
    root: Path, *, policy: ArtifactStorePolicy, clock_ms: Callable[[], int]
) -> PrivateSegmentArtifactStore:
    """First-use production constructor. Never resumes another process's store."""
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or _SCOPE.fullmatch(root.name) is None
        or root.parent.name != MANAGED_ARTIFACT_SUBTREE
        or policy != _POLICY
    ):
        raise ArtifactStoreError("scope_configuration")
    key = os.path.normcase(str(root))
    with _STORES_LOCK:
        cached = _STORES.get(key)
        if cached is not None:
            if cached[2] != policy:
                raise ArtifactStoreError("store_policy_mismatch")
            return cached[0]
        descriptor = None
        try:
            budget = _Budget(ScopeSweepLimits(), time.monotonic, lambda: False)
            with _namespace(root.parent, budget):
                _sweep(root.parent, budget)
                # IMPORTANT: never adopt an existing root, even when its name looks random.
                # A marker-less foreign or failed-initialization scope is not a writable store.
                root.mkdir(exist_ok=False)
                validate_directory(root)
                with _validated_directories(root.parent, root):
                    _write_new_file(root / _OWNER, _owner_payload(root.name))
                    descriptor = _acquire_lock(root / _OWNER, _owner_payload(root.name))
                    store = PrivateSegmentArtifactStore(
                        root, policy=policy, clock_ms=clock_ms, owner_lock=descriptor
                    )
                    _STORES[key] = (store, descriptor, policy)
                    descriptor = None
                    return store
        except (_SweepStopped, OSError, UnsafePathError) as exc:
            raise ArtifactStoreError("scope_store_unavailable") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
