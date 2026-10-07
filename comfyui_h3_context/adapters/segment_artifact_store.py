"""Private, explicit-root storage for generated segment artifacts.

The adapter never discovers a ComfyUI root and never exposes filesystem locators through receipt
or result projections. Callers own the private root and pass it explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from ..core.canonical import canonical_bytes, canonical_fingerprint
from ..core.safe_paths import (
    UnsafePathError,
    ensure_directory,
    validate_directory,
    validate_regular_file,
)
from ..core.segment_artifacts import (
    MAX_SEGMENT_ARTIFACT_BYTES,
    MAX_SEGMENT_ARTIFACT_RECEIPT_BYTES,
    MAX_SEGMENT_ARTIFACT_TTL_MILLISECONDS,
    ArtifactLifecycleState,
    SegmentArtifactError,
    SegmentArtifactReceipt,
    complete_segment_artifact_receipt,
    decode_segment_artifact_receipt,
    fail_segment_artifact_receipt,
    redact_segment_artifact_fingerprint,
)

SEGMENT_ARTIFACT_STORE_SCHEMA = "h3.context.segment_artifact_store.v1"
SEGMENT_ARTIFACT_STORE_STATE_SCHEMA = "h3.context.segment_artifact_store_state.v1"
ARTIFACT_CAPACITY_RESERVATION_SCHEMA = "h3.context.artifact_capacity_reservation.v1"
MAX_SEGMENT_ARTIFACT_STORE_BYTES = 16 * 1024 * 1024 * 1024
MAX_SEGMENT_ARTIFACT_STORE_ENTRIES = 1_024
MAX_SEGMENT_ARTIFACT_STORE_WRITES = 8
MAX_SEGMENT_ARTIFACT_RECOVERY_ENTRIES = 4_096
MAX_ARTIFACT_CAPACITY_RESERVATION_SEGMENTS = 15

_MARKER_NAME = ".h3-segment-artifact-store-v1"
_STATE_NAME = "store-state.json"
_CHILD_NAMES = ("artifacts", "receipts", "staging")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ARTIFACT_FILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\.bin\Z")
_RECEIPT_FILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\.json\Z")
_PARTIAL_FILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\.partial\.json\Z")
_TEMP_FILE = re.compile(r"[A-Za-z0-9_.:-]{1,220}\.tmp\Z")
_SHA256_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(slots=True)
class _RootCoordination:
    policy: ArtifactStorePolicy
    lock: threading.RLock
    write_slots: threading.BoundedSemaphore
    enabled: bool = True
    reservations: dict[str, _ArtifactCapacityReservationState] = field(default_factory=dict)


_COORDINATORS_GUARD = threading.Lock()
_COORDINATORS: dict[str, _RootCoordination] = {}


class ArtifactStoreError(RuntimeError):
    """A content-free private-store failure with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ArtifactInspectionStatus(str, Enum):
    REUSABLE = "reusable"
    MISSING = "missing"
    INCOMPATIBLE = "incompatible"
    TAMPERED = "tampered"
    EXPIRED = "expired"
    DISABLED = "disabled"
    NOT_COMPLETE = "not_complete"
    UNSAFE = "unsafe"


@dataclass(frozen=True, slots=True)
class ArtifactStorePolicy:
    max_artifact_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 4 * 1024 * 1024 * 1024
    max_entries: int = 256
    ttl_seconds: int = 7 * 24 * 60 * 60
    max_concurrent_writes: int = 2
    max_recovery_entries: int = 1_024

    def __post_init__(self) -> None:
        _bounded_int(
            self.max_artifact_bytes,
            "policy_artifact_bytes",
            MAX_SEGMENT_ARTIFACT_BYTES,
        )
        _bounded_int(
            self.max_total_bytes,
            "policy_total_bytes",
            MAX_SEGMENT_ARTIFACT_STORE_BYTES,
        )
        if self.max_total_bytes < self.max_artifact_bytes:
            raise ArtifactStoreError("policy_total_below_artifact")
        _bounded_int(self.max_entries, "policy_entries", MAX_SEGMENT_ARTIFACT_STORE_ENTRIES)
        _bounded_int(
            self.ttl_seconds,
            "policy_ttl",
            MAX_SEGMENT_ARTIFACT_TTL_MILLISECONDS // 1_000,
        )
        _bounded_int(
            self.max_concurrent_writes,
            "policy_concurrent_writes",
            MAX_SEGMENT_ARTIFACT_STORE_WRITES,
        )
        _bounded_int(
            self.max_recovery_entries,
            "policy_recovery_entries",
            MAX_SEGMENT_ARTIFACT_RECOVERY_ENTRIES,
        )
        if self.max_recovery_entries < self.max_entries * 2:
            raise ArtifactStoreError("policy_recovery_below_inventory")


@dataclass(frozen=True, slots=True)
class ArtifactInspection:
    artifact_id: str
    status: ArtifactInspectionStatus
    receipt_fingerprint: str

    def to_public_dict(self) -> dict[str, str]:
        token = redact_segment_artifact_fingerprint(
            self.receipt_fingerprint,
            domain="receipt",
        )
        if token is None:  # pragma: no cover - receipt fingerprints are required
            raise ArtifactStoreError("receipt_fingerprint_missing")
        return {
            "artifact_id": self.artifact_id,
            "status": self.status.value,
            "receipt_token": token,
        }


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactIdentity:
    """What one full inspection observed of a reusable artifact's two files.

    It carries no locator and proves nothing by itself: it only lets `identity_current` answer
    whether the files a full inspection verified are still the files on disk.
    """

    _receipt_fingerprint: str
    _expires_at_ms: int
    _receipt: tuple[int, int, int, int]
    _artifact: tuple[int, int, int, int]

    def __repr__(self) -> str:
        return "<ArtifactIdentity opaque>"


@dataclass(frozen=True, slots=True)
class ArtifactRecoveryReport:
    expired_removed: int = 0
    orphans_removed: int = 0
    invalid_removed: int = 0
    staging_removed: int = 0
    scanned_entries: int = 0

    def to_public_dict(self) -> dict[str, int]:
        return {
            "expired_removed": self.expired_removed,
            "orphans_removed": self.orphans_removed,
            "invalid_removed": self.invalid_removed,
            "staging_removed": self.staging_removed,
            "scanned_entries": self.scanned_entries,
        }


@dataclass(frozen=True, slots=True)
class ArtifactStoreDispositionReport:
    enabled: bool
    retained: bool
    purged_entries: int


@dataclass(frozen=True, slots=True)
class ArtifactStoreMigrationReport:
    status: str
    from_schema: str
    to_schema: str


def _bounded_int(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ArtifactStoreError(field)
    return value


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        getattr(metadata, "st_mtime_ns", 0),
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(path.is_symlink() or stat.S_ISLNK(metadata.st_mode) or attributes & reparse_flag)


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactStoreError("store_entry_unavailable") from exc


def _is_windows_path_race_error(exc: OSError) -> bool:
    return os.name == "nt" and getattr(exc, "winerror", None) in {5, 32, 33}


def _require_identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ArtifactStoreError(field)
    return value


@contextmanager
def _validated_directories(*directories: Path) -> Iterator[None]:
    """Fail closed if any admitted directory identity changes during an operation."""

    unique = tuple(dict.fromkeys(directories))
    pinned: dict[Path, int] = {}
    try:
        identities: dict[Path, tuple[int, int]] = {}
        for directory in unique:
            admitted = validate_directory(directory)
            metadata = admitted.lstat()
            identity = _directory_identity(metadata)
            identities[directory] = identity
            if os.name == "nt":
                # CRITICAL: omitting FILE_SHARE_DELETE pins the admitted directory
                # against rename/delete until validation and publication both finish.
                descriptor = _windows_pin_directory(admitted)
                pinned[directory] = descriptor
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or _is_link_or_reparse(admitted, opened)
                    or _directory_identity(opened) != identity
                ):
                    raise ArtifactStoreError("unsafe_store_entry")
        yield
        for directory, identity in identities.items():
            current = validate_directory(directory).lstat()
            if _directory_identity(current) != identity:
                raise ArtifactStoreError("unsafe_store_entry")
            pinned_descriptor = pinned.get(directory)
            if pinned_descriptor is not None:
                opened = os.fstat(pinned_descriptor)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or _is_link_or_reparse(directory, opened)
                    or _directory_identity(opened) != identity
                ):
                    raise ArtifactStoreError("unsafe_store_entry")
    except (UnsafePathError, OSError) as exc:
        raise ArtifactStoreError("unsafe_store_entry") from exc
    finally:
        for descriptor in reversed(tuple(pinned.values())):
            os.close(descriptor)


def _read_regular_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    with _validated_directories(path.parent):
        return _read_regular_bytes_pinned(path, maximum_bytes=maximum_bytes)


def _read_regular_bytes_pinned(path: Path, *, maximum_bytes: int) -> bytes:
    try:
        admitted = validate_regular_file(path, maximum_bytes=maximum_bytes)
        before = admitted.lstat()
    except UnsafePathError as exc:
        raise ArtifactStoreError("unsafe_store_entry") from exc
    if before.st_nlink != 1:
        raise ArtifactStoreError("unsafe_store_entry")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = (
            _windows_open_existing_file(admitted) if os.name == "nt" else os.open(admitted, flags)
        )
    except OSError as exc:
        raise ArtifactStoreError("store_entry_unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
        ):
            raise ArtifactStoreError("unsafe_store_entry")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after_handle = os.fstat(descriptor)
        after_path = admitted.lstat()
        if (
            len(payload) > maximum_bytes
            or _is_link_or_reparse(admitted, after_path)
            or after_path.st_nlink != 1
            or _identity(after_handle) != _identity(opened)
            or _identity(after_path) != _identity(opened)
        ):
            raise ArtifactStoreError("unsafe_store_entry")
    except OSError as exc:
        raise ArtifactStoreError("store_entry_unavailable") from exc
    finally:
        os.close(descriptor)
    return payload


def _write_new_file(path: Path, payload: bytes) -> None:
    with _validated_directories(path.parent):
        _write_new_file_pinned(path, payload)


def _write_new_file_pinned(path: Path, payload: bytes) -> None:
    try:
        parent = validate_directory(path.parent)
        parent_before = parent.lstat()
    except (UnsafePathError, OSError) as exc:
        raise ArtifactStoreError("unsafe_store_entry") from exc
    if path.parent != parent or path.name in {"", ".", ".."}:
        raise ArtifactStoreError("unsafe_store_entry")
    try:
        descriptor = (
            _windows_open_new_file(path)
            if os.name == "nt"
            else os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
        )
    except OSError as exc:
        if _is_windows_path_race_error(exc):
            raise ArtifactStoreError("unsafe_store_entry") from exc
        raise ArtifactStoreError("store_write_failed") from exc
    created_identity: tuple[int, int, int, int] | None = None
    failure: ArtifactStoreError | None = None
    try:
        parent_opened = validate_directory(parent).lstat()
        opened_handle = os.fstat(descriptor)
        opened_path = validate_regular_file(path, maximum_bytes=1).lstat()
        created_identity = _identity(opened_handle)
        if (
            _directory_identity(parent_before) != _directory_identity(parent_opened)
            or not stat.S_ISREG(opened_handle.st_mode)
            or opened_handle.st_nlink != 1
            or _identity(opened_path) != created_identity
        ):
            raise ArtifactStoreError("unsafe_store_entry")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ArtifactStoreError("store_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        parent_after = validate_directory(parent).lstat()
        created = validate_regular_file(path, maximum_bytes=max(len(payload), 1)).lstat()
        after_handle = os.fstat(descriptor)
        if (
            _directory_identity(parent_before) != _directory_identity(parent_after)
            or created.st_nlink != 1
            or _identity(created) != _identity(after_handle)
        ):
            raise ArtifactStoreError("unsafe_store_entry")
    except UnsafePathError as exc:
        failure = ArtifactStoreError("unsafe_store_entry")
        failure.__cause__ = exc
    except OSError as exc:
        failure = ArtifactStoreError(
            "unsafe_store_entry" if _is_windows_path_race_error(exc) else "store_write_failed"
        )
        failure.__cause__ = exc
    except ArtifactStoreError as exc:
        failure = exc
    finally:
        os.close(descriptor)
    if failure is not None:
        current = _lstat_optional(path)
        if (
            created_identity is not None
            and current is not None
            and _identity(current) == created_identity
        ):
            try:
                _safe_unlink_pinned(path, missing_ok=True, maximum_links=1)
            except ArtifactStoreError as cleanup_exc:
                raise ArtifactStoreError("store_cleanup_failed") from cleanup_exc
        raise failure


_STREAM_CHUNK_BYTES = 1024 * 1024


def _copy_regular_file_into_new(
    source: Path,
    destination: Path,
    *,
    maximum_bytes: int,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[int, str]:
    with _validated_directories(source.parent, destination.parent):
        return _copy_regular_file_into_new_pinned(
            source,
            destination,
            maximum_bytes=maximum_bytes,
            should_cancel=should_cancel,
        )


def _copy_regular_file_into_new_pinned(
    source: Path,
    destination: Path,
    *,
    maximum_bytes: int,
    should_cancel: Callable[[], bool] | None,
) -> tuple[int, str]:
    """Stream one pinned regular file into one freshly created file, hashing en route.

    M20-08: the incremental-transport primitive. Read-side admission mirrors
    ``_read_regular_bytes_pinned`` and write-side admission mirrors ``_write_new_file_pinned``;
    the payload itself only ever exists one bounded chunk at a time.
    """

    try:
        admitted = validate_regular_file(source, maximum_bytes=maximum_bytes)
        before = admitted.lstat()
    except UnsafePathError as exc:
        raise ArtifactStoreError("unsafe_store_entry") from exc
    if before.st_nlink != 1:
        raise ArtifactStoreError("unsafe_store_entry")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        reader = (
            _windows_open_existing_file(admitted) if os.name == "nt" else os.open(admitted, flags)
        )
    except OSError as exc:
        raise ArtifactStoreError("store_entry_unavailable") from exc
    try:
        opened = os.fstat(reader)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
        ):
            raise ArtifactStoreError("unsafe_store_entry")
        try:
            parent = validate_directory(destination.parent)
            parent_before = parent.lstat()
        except (UnsafePathError, OSError) as exc:
            raise ArtifactStoreError("unsafe_store_entry") from exc
        if destination.parent != parent or destination.name in {"", ".", ".."}:
            raise ArtifactStoreError("unsafe_store_entry")
        try:
            writer = (
                _windows_open_new_file(destination)
                if os.name == "nt"
                else os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            )
        except OSError as exc:
            if _is_windows_path_race_error(exc):
                raise ArtifactStoreError("unsafe_store_entry") from exc
            raise ArtifactStoreError("store_write_failed") from exc
        created_identity: tuple[int, int, int, int] | None = None
        failure: ArtifactStoreError | None = None
        digest = hashlib.sha256()
        total = 0
        try:
            parent_opened = validate_directory(parent).lstat()
            writer_handle = os.fstat(writer)
            writer_path = validate_regular_file(destination, maximum_bytes=1).lstat()
            created_identity = _identity(writer_handle)
            if (
                _directory_identity(parent_before) != _directory_identity(parent_opened)
                or not stat.S_ISREG(writer_handle.st_mode)
                or writer_handle.st_nlink != 1
                or _identity(writer_path) != created_identity
            ):
                raise ArtifactStoreError("unsafe_store_entry")
            remaining = maximum_bytes + 1
            while remaining > 0:
                if should_cancel is not None and should_cancel():
                    raise ArtifactStoreError("stream_cancelled")
                chunk = os.read(reader, min(_STREAM_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise ArtifactStoreError("unsafe_store_entry")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(writer, view)
                    if written <= 0:
                        raise ArtifactStoreError("store_write_failed")
                    view = view[written:]
                remaining -= len(chunk)
            os.fsync(writer)
            reader_after = os.fstat(reader)
            source_after = admitted.lstat()
            if (
                _is_link_or_reparse(admitted, source_after)
                or source_after.st_nlink != 1
                or _identity(reader_after) != _identity(opened)
                or _identity(source_after) != _identity(opened)
            ):
                raise ArtifactStoreError("unsafe_store_entry")
            parent_after = validate_directory(parent).lstat()
            created = validate_regular_file(destination, maximum_bytes=max(total, 1)).lstat()
            writer_after = os.fstat(writer)
            if (
                _directory_identity(parent_before) != _directory_identity(parent_after)
                or created.st_nlink != 1
                or _identity(created) != _identity(writer_after)
            ):
                raise ArtifactStoreError("unsafe_store_entry")
        except UnsafePathError as exc:
            failure = ArtifactStoreError("unsafe_store_entry")
            failure.__cause__ = exc
        except OSError as exc:
            failure = ArtifactStoreError(
                "unsafe_store_entry" if _is_windows_path_race_error(exc) else "store_write_failed"
            )
            failure.__cause__ = exc
        except ArtifactStoreError as exc:
            failure = exc
        finally:
            os.close(writer)
        if failure is not None:
            current = _lstat_optional(destination)
            if (
                created_identity is not None
                and current is not None
                and _identity(current) == created_identity
            ):
                try:
                    _safe_unlink_pinned(destination, missing_ok=True, maximum_links=1)
                except ArtifactStoreError as cleanup_exc:
                    raise ArtifactStoreError("store_cleanup_failed") from cleanup_exc
            raise failure
        return total, "sha256:" + digest.hexdigest()
    finally:
        os.close(reader)


def _windows_dll(name: str) -> Any:
    import ctypes

    # CRITICAL: Linux Python stubs omit Windows-only ctypes symbols; callers are reached only
    # from the explicit Windows storage paths below.
    win_dll = getattr(ctypes, "WinDLL")  # noqa: B009
    return win_dll(name, use_last_error=True)


def _windows_last_error() -> int:
    import ctypes

    return int(getattr(ctypes, "get_last_error")())  # noqa: B009


def _msvcrt_open_osfhandle(handle: int, flags: int) -> int:
    import msvcrt

    return int(getattr(msvcrt, "open_osfhandle")(handle, flags))  # noqa: B009


def _msvcrt_get_osfhandle(descriptor: int) -> int:
    import msvcrt

    return int(getattr(msvcrt, "get_osfhandle")(descriptor))  # noqa: B009


def _windows_native_path(path: Path, *, force: bool = False) -> str:
    value = str(path)
    if (not force and len(value) < 248) or value.startswith("\\\\?\\"):
        return value
    if not path.is_absolute() or ".." in path.parts:
        raise ArtifactStoreError("unsafe_store_entry")
    # CRITICAL: Python's long-path support does not extend to direct CreateFileW calls.
    # Prefix only an already admitted absolute path; never resolve across reparse components.
    return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value


def _windows_pin_directory(path: Path) -> int:
    import ctypes

    kernel32 = _windows_dll("kernel32")
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        _windows_native_path(path),
        0x80000000,
        0x00000001 | 0x00000002,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value or handle is None:
        raise OSError(_windows_last_error(), "CreateFileW directory pin failed")
    try:
        return _msvcrt_open_osfhandle(int(handle), os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def _windows_open_new_file(path: Path) -> int:
    import ctypes

    kernel32 = _windows_dll("kernel32")
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        _windows_native_path(path),
        0x40000000,
        0x00000001 | 0x00000002,
        None,
        1,
        0x00000080 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value or handle is None:
        raise OSError(_windows_last_error(), "CreateFileW failed")
    try:
        return _msvcrt_open_osfhandle(
            int(handle),
            os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
    except OSError:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def _windows_open_existing_file(path: Path) -> int:
    import ctypes

    kernel32 = _windows_dll("kernel32")
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        _windows_native_path(path),
        0x80000000,
        0x00000001 | 0x00000002,
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value or handle is None:
        raise OSError(_windows_last_error(), "CreateFileW failed")
    try:
        return _msvcrt_open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except OSError:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def _windows_move_new_file(source: Path, destination: Path) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_dll("kernel32")
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        _windows_native_path(source),
        0x80000000 | 0x00010000,
        0x00000001,
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value or handle is None:
        raise OSError(_windows_last_error(), "CreateFileW publication pin failed")
    descriptor: int | None = None
    try:
        descriptor = _msvcrt_open_osfhandle(int(handle), os.O_RDONLY)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ArtifactStoreError("unsafe_store_entry")

        class FileRenameInfo(ctypes.Structure):
            _fields_ = (
                ("replace_if_exists", wintypes.DWORD),
                ("root_directory", wintypes.HANDLE),
                ("file_name_length", wintypes.DWORD),
                ("file_name", wintypes.WCHAR * 1),
            )

        encoded_name = str(destination).encode("utf-16-le")
        buffer_size = ctypes.sizeof(FileRenameInfo) + len(encoded_name)
        buffer = ctypes.create_string_buffer(buffer_size)
        information = ctypes.cast(buffer, ctypes.POINTER(FileRenameInfo)).contents
        information.replace_if_exists = 0
        information.root_directory = None
        information.file_name_length = len(encoded_name)
        ctypes.memmove(
            ctypes.addressof(buffer) + FileRenameInfo.file_name.offset,
            encoded_name,
            len(encoded_name),
        )
        set_information = kernel32.SetFileInformationByHandle
        set_information.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        set_information.restype = wintypes.BOOL
        if not set_information(
            ctypes.c_void_p(_msvcrt_get_osfhandle(descriptor)),
            3,
            buffer,
            buffer_size,
        ):
            error = _windows_last_error()
            if error in {80, 183} or _lstat_optional(destination) is not None:
                raise FileExistsError(error, "FileRenameInfo destination exists", str(destination))
            raise OSError(error, "SetFileInformationByHandle rename failed", str(source))
        return descriptor
    except BaseException:
        if descriptor is None:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        else:
            os.close(descriptor)
        raise


def _windows_delete_open_file(descriptor: int) -> None:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    kernel32 = _windows_dll("kernel32")
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_information.restype = wintypes.BOOL
    disposition = FileDispositionInfo(True)
    if not set_information(
        ctypes.c_void_p(_msvcrt_get_osfhandle(descriptor)),
        4,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ArtifactStoreError("store_cleanup_failed")


def _safe_unlink(
    path: Path,
    *,
    missing_ok: bool = True,
    maximum_links: int = 1,
) -> bool:
    with _validated_directories(path.parent):
        return _safe_unlink_pinned(
            path,
            missing_ok=missing_ok,
            maximum_links=maximum_links,
        )


def _safe_unlink_pinned(
    path: Path,
    *,
    missing_ok: bool,
    maximum_links: int,
) -> bool:
    try:
        validate_directory(path.parent)
    except UnsafePathError as exc:
        raise ArtifactStoreError("unsafe_store_entry") from exc
    metadata = _lstat_optional(path)
    if metadata is None:
        if missing_ok:
            return False
        raise ArtifactStoreError("store_entry_missing")
    if (
        _is_link_or_reparse(path, metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or not 1 <= metadata.st_nlink <= maximum_links
    ):
        raise ArtifactStoreError("unsafe_store_entry")
    try:
        admitted = validate_regular_file(path)
    except UnsafePathError as exc:
        raise ArtifactStoreError("unsafe_store_entry") from exc
    if _identity(admitted.lstat()) != _identity(metadata):
        raise ArtifactStoreError("unsafe_store_entry")
    if os.name == "nt":
        _windows_delete_exact(admitted, expected=metadata)
        return True
    try:
        path.unlink()
    except OSError as exc:
        raise ArtifactStoreError("store_cleanup_failed") from exc
    return True


def _windows_delete_exact(path: Path, *, expected: os.stat_result) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_dll("kernel32")
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        _windows_native_path(path),
        0x00010000 | 0x00000080,
        0x00000001 | 0x00000002,
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value or handle is None:
        raise ArtifactStoreError("store_cleanup_failed")
    descriptor: int | None = None
    try:
        descriptor = _msvcrt_open_osfhandle(int(handle), os.O_RDONLY)
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(expected):
            raise ArtifactStoreError("unsafe_store_entry")

        class FileDispositionInfo(ctypes.Structure):
            _fields_ = [("delete_file", wintypes.BOOL)]

        disposition = FileDispositionInfo(True)
        set_information = kernel32.SetFileInformationByHandle
        set_information.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        set_information.restype = wintypes.BOOL
        if not set_information(
            ctypes.c_void_p(_msvcrt_get_osfhandle(descriptor)),
            4,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise ArtifactStoreError("store_cleanup_failed")
    finally:
        if descriptor is None:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        else:
            os.close(descriptor)


def _decode_json_object(payload: bytes, *, error_code: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ArtifactStoreError(error_code)
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactStoreError(error_code) from exc
    if type(value) is not dict:
        raise ArtifactStoreError(error_code)
    return value


@dataclass(frozen=True, slots=True)
class ArtifactCapacityReservationV1:
    """One content-free capability over atomically reserved artifact-store capacity."""

    reservation_id: str
    parent_sequence_id: str
    parent_authorization_fingerprint: str
    segment_count: int
    per_artifact_max_bytes: int
    reserved_entries: int
    reserved_bytes: int
    expires_at_ms: int
    consumed_entries: int = 0
    consumed_bytes: int = 0
    released: bool = False
    token_fingerprint: str | None = None
    schema: str = ARTIFACT_CAPACITY_RESERVATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ARTIFACT_CAPACITY_RESERVATION_SCHEMA:
            raise ArtifactStoreError("unsupported_capacity_reservation_schema")
        _require_identifier(self.reservation_id, "reservation_id")
        _require_identifier(self.parent_sequence_id, "parent_sequence_id")
        if _SHA256_FINGERPRINT.fullmatch(self.parent_authorization_fingerprint) is None:
            raise ArtifactStoreError("capacity_parent_fingerprint")
        if (
            type(self.segment_count) is not int
            or not 1 <= self.segment_count <= MAX_ARTIFACT_CAPACITY_RESERVATION_SEGMENTS
        ):
            raise ArtifactStoreError("capacity_segment_count")
        _bounded_int(
            self.per_artifact_max_bytes,
            "capacity_artifact_bytes",
            MAX_SEGMENT_ARTIFACT_BYTES,
        )
        if self.reserved_entries != self.segment_count:
            raise ArtifactStoreError("capacity_reserved_entries")
        if self.reserved_bytes != self.per_artifact_max_bytes * self.segment_count:
            raise ArtifactStoreError("capacity_reserved_bytes")
        if type(self.expires_at_ms) is not int or not 1 <= self.expires_at_ms <= 9_999_999_999_999:
            raise ArtifactStoreError("capacity_expiry")
        if (
            type(self.consumed_entries) is not int
            or not 0 <= self.consumed_entries <= self.reserved_entries
        ):
            raise ArtifactStoreError("capacity_consumed_entries")
        if (
            type(self.consumed_bytes) is not int
            or not 0 <= self.consumed_bytes <= self.reserved_bytes
        ):
            raise ArtifactStoreError("capacity_consumed_bytes")
        if type(self.released) is not bool:
            raise ArtifactStoreError("capacity_released")
        expected = canonical_fingerprint(self._token_wire())
        if self.token_fingerprint is None:
            object.__setattr__(self, "token_fingerprint", expected)
        elif self.token_fingerprint != expected:
            raise ArtifactStoreError("capacity_reservation_fingerprint")

    def _token_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "reservation_id": self.reservation_id,
            "parent_sequence_id": self.parent_sequence_id,
            "parent_authorization_fingerprint": self.parent_authorization_fingerprint,
            "segment_count": self.segment_count,
            "per_artifact_max_bytes": self.per_artifact_max_bytes,
            "reserved_entries": self.reserved_entries,
            "reserved_bytes": self.reserved_bytes,
            "expires_at_ms": self.expires_at_ms,
        }

    @property
    def fingerprint(self) -> str:
        if self.token_fingerprint is None:  # pragma: no cover - initialized above
            raise ArtifactStoreError("capacity_reservation_fingerprint")
        return self.token_fingerprint

    def to_wire(self) -> dict[str, object]:
        value = self._token_wire()
        value.update(
            {
                "consumed_entries": self.consumed_entries,
                "consumed_bytes": self.consumed_bytes,
                "released": self.released,
                "token_fingerprint": self.fingerprint,
            }
        )
        return value


@dataclass(slots=True)
class _ArtifactCapacityReservationState:
    token: ArtifactCapacityReservationV1
    consumed_entries: int = 0
    consumed_bytes: int = 0
    released: bool = False
    begun_artifact_ids: set[str] = field(default_factory=set)
    committed_artifact_ids: set[str] = field(default_factory=set)

    def snapshot(self) -> ArtifactCapacityReservationV1:
        return replace(
            self.token,
            consumed_entries=self.consumed_entries,
            consumed_bytes=self.consumed_bytes,
            released=self.released,
        )


class PrivateSegmentArtifactStore:
    """One bounded private store rooted only at an explicit caller-owned directory."""

    def __init__(
        self,
        root: Path,
        *,
        policy: ArtifactStorePolicy | None = None,
        clock_ms: Callable[[], int],
        owner_lock: int | None = None,
    ) -> None:
        if not isinstance(root, Path):
            raise ArtifactStoreError("unsafe_store_root")
        self._policy = ArtifactStorePolicy() if policy is None else policy
        if type(self._policy) is not ArtifactStorePolicy or not callable(clock_ms):
            raise ArtifactStoreError("store_configuration")
        self._clock_ms = clock_ms
        self._owner_lock = owner_lock
        if owner_lock is not None:
            try:
                if type(owner_lock) is not int or owner_lock < 0:
                    raise ArtifactStoreError("unsafe_store_owner")
                owner = validate_regular_file(root / ".owner.lock", maximum_bytes=1_024).lstat()
                opened = os.fstat(owner_lock)
                # CRITICAL: only the production lease's exact open single-link file extends
                # the closed root layout; merely finding an owner filename grants nothing.
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or owner.st_nlink != 1
                    or _identity(opened) != _identity(owner)
                ):
                    raise ArtifactStoreError("unsafe_store_owner")
            except (UnsafePathError, OSError) as exc:
                raise ArtifactStoreError("unsafe_store_owner") from exc
        coordination_key = os.path.normcase(str(root.absolute()))
        with _COORDINATORS_GUARD:
            coordination = _COORDINATORS.get(coordination_key)
            if coordination is None:
                coordination = _RootCoordination(
                    policy=self._policy,
                    lock=threading.RLock(),
                    write_slots=threading.BoundedSemaphore(self._policy.max_concurrent_writes),
                )
                _COORDINATORS[coordination_key] = coordination
            elif coordination.policy != self._policy:
                raise ArtifactStoreError("store_policy_mismatch")
        self._coordination = coordination
        self._lock = coordination.lock
        self._write_slots = coordination.write_slots
        with self._lock:
            self._initialize(root)

    def _initialize(self, root: Path) -> None:
        try:
            metadata = _lstat_optional(root)
            if metadata is None:
                admitted_root = ensure_directory(root)
                initial_entries: tuple[Path, ...] = ()
            else:
                admitted_root = validate_directory(root)
                initial_entries = self._bounded_entries(
                    admitted_root,
                    maximum=len(_CHILD_NAMES) + 2 + int(self._owner_lock is not None),
                )
        except (UnsafePathError, OSError, ArtifactStoreError) as exc:
            raise ArtifactStoreError("unsafe_store_root") from exc
        self._root = admitted_root
        self._marker = self._root / _MARKER_NAME
        self._state = self._root / _STATE_NAME
        marker_metadata = _lstat_optional(self._marker)
        if marker_metadata is None:
            permitted_initial = {".owner.lock"} if self._owner_lock is not None else set()
            if any(entry.name not in permitted_initial for entry in initial_entries):
                raise ArtifactStoreError("foreign_store_root")
            self._atomic_publish_new(
                self._marker,
                self._marker_payload(),
                parent=self._root,
            )
        self._validate_marker()
        allowed_root_names = {_MARKER_NAME, _STATE_NAME, *_CHILD_NAMES}
        if self._owner_lock is not None:
            allowed_root_names.add(".owner.lock")
        try:
            if any(entry.name not in allowed_root_names for entry in self._root.iterdir()):
                raise ArtifactStoreError("foreign_store_root")
            for name in _CHILD_NAMES:
                ensure_directory(self._root / name)
        except (UnsafePathError, OSError) as exc:
            raise ArtifactStoreError("unsafe_store_root") from exc
        self._artifacts = self._root / "artifacts"
        self._receipts = self._root / "receipts"
        self._staging = self._root / "staging"
        if _lstat_optional(self._state) is None:
            self._atomic_publish_new(
                self._state,
                self._state_payload(enabled=True),
                parent=self._root,
            )
        self._coordination.enabled = self._read_state()

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or value <= 0:
            raise ArtifactStoreError("store_clock")
        return value

    def _policy_wire(self) -> dict[str, int]:
        return {
            "max_artifact_bytes": self._policy.max_artifact_bytes,
            "max_total_bytes": self._policy.max_total_bytes,
            "max_entries": self._policy.max_entries,
            "ttl_seconds": self._policy.ttl_seconds,
            "max_concurrent_writes": self._policy.max_concurrent_writes,
            "max_recovery_entries": self._policy.max_recovery_entries,
        }

    def _marker_payload(self) -> bytes:
        return canonical_bytes(
            {
                "schema": SEGMENT_ARTIFACT_STORE_SCHEMA,
                "policy": self._policy_wire(),
            }
        )

    def _validate_marker(self) -> None:
        payload = _read_regular_bytes(self._marker, maximum_bytes=1_024)
        value = _decode_json_object(payload, error_code="unsupported_store_schema")
        if value != {
            "schema": SEGMENT_ARTIFACT_STORE_SCHEMA,
            "policy": self._policy_wire(),
        }:
            raise ArtifactStoreError("unsupported_store_schema")

    @staticmethod
    def _state_payload(*, enabled: bool) -> bytes:
        return canonical_bytes(
            {
                "schema": SEGMENT_ARTIFACT_STORE_STATE_SCHEMA,
                "enabled": enabled,
            }
        )

    def _read_state(self) -> bool:
        value = _decode_json_object(
            _read_regular_bytes(self._state, maximum_bytes=1_024),
            error_code="invalid_store_state",
        )
        if (
            set(value) != {"schema", "enabled"}
            or value["schema"] != SEGMENT_ARTIFACT_STORE_STATE_SCHEMA
            or type(value["enabled"]) is not bool
        ):
            raise ArtifactStoreError("invalid_store_state")
        return bool(value["enabled"])

    def _persist_enabled(self, enabled: bool) -> None:
        temporary = self._staging / f"state.{uuid.uuid4().hex}.tmp"
        _write_new_file(temporary, self._state_payload(enabled=enabled))
        try:
            validate_regular_file(self._state, maximum_bytes=1_024)
            # CRITICAL: replace only this validated store-owned state file; never accept a path.
            os.replace(temporary, self._state)
        except (OSError, UnsafePathError) as exc:
            _safe_unlink(temporary, maximum_links=2)
            raise ArtifactStoreError("store_state_write_failed") from exc
        self._coordination.enabled = enabled

    def _atomic_publish_new(self, final: Path, payload: bytes, *, parent: Path) -> None:
        with _validated_directories(self._root, parent, final.parent):
            self._atomic_publish_new_pinned(final, payload, parent=parent)

    def _atomic_publish_new_pinned(
        self,
        final: Path,
        payload: bytes,
        *,
        parent: Path,
    ) -> None:
        try:
            admitted_parent = validate_directory(parent)
            admitted_final_parent = validate_directory(final.parent)
            parent_identity = _directory_identity(admitted_parent.lstat())
            final_parent_identity = _directory_identity(admitted_final_parent.lstat())
        except UnsafePathError as exc:
            raise ArtifactStoreError("unsafe_store_entry") from exc
        if admitted_parent != parent or admitted_final_parent != final.parent:
            raise ArtifactStoreError("unsafe_store_entry")
        metadata = _lstat_optional(final)
        if metadata is not None:
            if _is_link_or_reparse(final, metadata):
                raise ArtifactStoreError("unsafe_store_entry")
            raise ArtifactStoreError("duplicate_artifact")
        temporary = parent / f"publish.{uuid.uuid4().hex}.tmp"
        published_identity: tuple[int, int, int, int] | None = None
        published_descriptor: int | None = None
        try:
            _write_new_file(temporary, payload)
            temporary_identity = _identity(temporary.lstat())
            if (
                _directory_identity(validate_directory(parent).lstat()) != parent_identity
                or _directory_identity(validate_directory(final.parent).lstat())
                != final_parent_identity
            ):
                raise ArtifactStoreError("unsafe_store_entry")
            if _lstat_optional(final) is not None:
                raise ArtifactStoreError("duplicate_artifact")
            if os.name == "nt":
                # CRITICAL: rename the open no-write/no-delete-share source handle
                # and retain it until the final path and identity are verified.
                published_descriptor = _windows_move_new_file(temporary, final)
                published_handle = os.fstat(published_descriptor)
                if _identity(published_handle) != temporary_identity:
                    raise ArtifactStoreError("unsafe_store_entry")
            else:
                # Hard-link publication is atomic and fails instead of clobbering
                # an existing target.
                os.link(temporary, final, follow_symlinks=False)
            published_identity = _identity(final.lstat())
        except FileExistsError as exc:
            _safe_unlink(temporary, maximum_links=2)
            raise ArtifactStoreError("duplicate_artifact") from exc
        except (OSError, UnsafePathError) as exc:
            _safe_unlink(temporary, maximum_links=2)
            if isinstance(exc, OSError) and _is_windows_path_race_error(exc):
                raise ArtifactStoreError("unsafe_store_entry") from exc
            raise ArtifactStoreError("store_publish_failed") from exc
        except ArtifactStoreError:
            _safe_unlink(temporary, maximum_links=2)
            raise
        try:
            admitted_final = validate_regular_file(
                final,
                maximum_bytes=max(len(payload), 1),
            )
            if (
                _directory_identity(validate_directory(parent).lstat()) != parent_identity
                or _directory_identity(validate_directory(final.parent).lstat())
                != final_parent_identity
                or _identity(admitted_final.lstat()) != published_identity
                or (
                    published_descriptor is not None
                    and _identity(os.fstat(published_descriptor)) != published_identity
                )
            ):
                raise ArtifactStoreError("unsafe_store_entry")
        except (ArtifactStoreError, UnsafePathError) as exc:
            # CRITICAL: roll back only the exact inode this call created, even if a
            # directory component was replaced after publication.
            if published_descriptor is not None:
                _windows_delete_open_file(published_descriptor)
            else:
                current = _lstat_optional(final)
                if current is not None and _identity(current) == published_identity:
                    try:
                        final.unlink()
                    except OSError:
                        pass
            _safe_unlink(temporary, maximum_links=2)
            raise ArtifactStoreError("store_publish_failed") from exc
        finally:
            if published_descriptor is not None:
                os.close(published_descriptor)
        if _lstat_optional(temporary) is not None:
            _safe_unlink(temporary, maximum_links=2)

    def _artifact_path(self, artifact_id: str) -> Path:
        return self._artifacts / f"{_require_identifier(artifact_id, 'artifact_id')}.bin"

    def _receipt_path(self, artifact_id: str) -> Path:
        return self._receipts / f"{_require_identifier(artifact_id, 'artifact_id')}.json"

    def _partial_path(self, artifact_id: str) -> Path:
        return self._staging / f"{_require_identifier(artifact_id, 'artifact_id')}.partial.json"

    def _require_enabled(self) -> None:
        if not self._coordination.enabled:
            raise ArtifactStoreError("store_disabled")

    def _expire_capacity_reservations(self, now_ms: int) -> None:
        for state in self._coordination.reservations.values():
            if not state.released and state.token.expires_at_ms <= now_ms:
                state.released = True

    def _active_reserved_capacity(self) -> tuple[int, int]:
        entries = 0
        total_bytes = 0
        for state in self._coordination.reservations.values():
            if state.released:
                continue
            entries += state.token.reserved_entries - state.consumed_entries
            total_bytes += state.token.reserved_bytes - state.consumed_bytes
        return entries, total_bytes

    def _capacity_state(
        self,
        reservation: ArtifactCapacityReservationV1,
    ) -> _ArtifactCapacityReservationState:
        if type(reservation) is not ArtifactCapacityReservationV1:
            raise ArtifactStoreError("capacity_reservation_type")
        state = self._coordination.reservations.get(reservation.reservation_id)
        if state is None or state.token.fingerprint != reservation.fingerprint:
            raise ArtifactStoreError("capacity_reservation_unknown")
        return state

    def reserve_capacity(
        self,
        *,
        reservation_id: str,
        parent_sequence_id: str,
        parent_authorization_fingerprint: str,
        segment_count: int,
        expires_at_ms: int,
    ) -> ArtifactCapacityReservationV1:
        if (
            type(segment_count) is not int
            or not 1 <= segment_count <= MAX_ARTIFACT_CAPACITY_RESERVATION_SEGMENTS
        ):
            raise ArtifactStoreError("capacity_segment_count")
        per_artifact_max_bytes = min(
            self._policy.max_artifact_bytes,
            self._policy.max_total_bytes // segment_count,
        )
        reservation = ArtifactCapacityReservationV1(
            reservation_id=reservation_id,
            parent_sequence_id=parent_sequence_id,
            parent_authorization_fingerprint=parent_authorization_fingerprint,
            segment_count=segment_count,
            per_artifact_max_bytes=per_artifact_max_bytes,
            reserved_entries=segment_count,
            reserved_bytes=per_artifact_max_bytes * segment_count,
            expires_at_ms=expires_at_ms,
        )
        with self._lock:
            self._require_enabled()
            now = self._now_ms()
            if reservation.expires_at_ms <= now:
                raise ArtifactStoreError("capacity_reservation_expired")
            if reservation.expires_at_ms > now + self._policy.ttl_seconds * 1_000:
                raise ArtifactStoreError("capacity_reservation_ttl")
            self._expire_capacity_reservations(now)
            existing = self._coordination.reservations.get(reservation.reservation_id)
            if existing is not None:
                if existing.token.fingerprint != reservation.fingerprint:
                    raise ArtifactStoreError("capacity_reservation_conflict")
                return existing.snapshot()
            lifecycle_count, total = self._inventory()
            reserved_entries, reserved_bytes = self._active_reserved_capacity()
            if (
                lifecycle_count + reserved_entries + reservation.reserved_entries
                > self._policy.max_entries
            ):
                raise ArtifactStoreError("capacity_entries_unavailable")
            if total + reserved_bytes + reservation.reserved_bytes > self._policy.max_total_bytes:
                raise ArtifactStoreError("capacity_bytes_unavailable")
            state = _ArtifactCapacityReservationState(token=reservation)
            self._coordination.reservations[reservation.reservation_id] = state
            return state.snapshot()

    def read_capacity_reservation(
        self,
        reservation: ArtifactCapacityReservationV1,
    ) -> ArtifactCapacityReservationV1:
        with self._lock:
            now = self._now_ms()
            self._expire_capacity_reservations(now)
            return self._capacity_state(reservation).snapshot()

    def release_capacity_reservation(
        self,
        reservation: ArtifactCapacityReservationV1,
    ) -> ArtifactCapacityReservationV1:
        with self._lock:
            state = self._capacity_state(reservation)
            state.released = True
            return state.snapshot()

    def consume_reused_capacity(
        self,
        reservation: ArtifactCapacityReservationV1,
        receipt: SegmentArtifactReceipt,
    ) -> ArtifactCapacityReservationV1:
        """Bind one already-published exact artifact to an owned sequence reservation."""

        if type(receipt) is not SegmentArtifactReceipt:
            raise ArtifactStoreError("receipt_type")
        with self._lock:
            self._require_enabled()
            now = self._now_ms()
            self._expire_capacity_reservations(now)
            state = self._capacity_state(reservation)
            if state.released:
                code = (
                    "capacity_reservation_expired"
                    if state.token.expires_at_ms <= now
                    else "capacity_reservation_released"
                )
                raise ArtifactStoreError(code)
            if state.consumed_entries >= state.token.reserved_entries:
                raise ArtifactStoreError("capacity_entries_exhausted")
            if (
                receipt.artifact_id in state.begun_artifact_ids
                or receipt.artifact_id in state.committed_artifact_ids
            ):
                raise ArtifactStoreError("capacity_artifact_duplicate")
            if (
                receipt.byte_length > state.token.per_artifact_max_bytes
                or state.consumed_bytes + receipt.byte_length > state.token.reserved_bytes
            ):
                raise ArtifactStoreError("artifact_exceeds_reservation")
            # CRITICAL: inspect under the same root coordination lock that consumes the token.
            # Splitting these lets another owner replace/remove the retained artifact between the
            # exact-currentness proof and the capacity mutation.
            inspection = self.inspect(
                receipt,
                maximum_bytes=state.token.per_artifact_max_bytes,
            )
            if inspection.status is not ArtifactInspectionStatus.REUSABLE:
                raise ArtifactStoreError("artifact_not_reusable")
            state.consumed_entries += 1
            state.consumed_bytes += receipt.byte_length
            state.committed_artifact_ids.add(receipt.artifact_id)
            return state.snapshot()

    def begin(
        self,
        receipt: SegmentArtifactReceipt,
        *,
        capacity_reservation: ArtifactCapacityReservationV1 | None = None,
    ) -> SegmentArtifactReceipt:
        if type(receipt) is not SegmentArtifactReceipt:
            raise ArtifactStoreError("receipt_type")
        if receipt.state is not ArtifactLifecycleState.PARTIAL:
            raise ArtifactStoreError("receipt_not_partial")
        with self._lock:
            self._require_enabled()
            now = self._now_ms()
            self._expire_capacity_reservations(now)
            capacity_state = (
                None if capacity_reservation is None else self._capacity_state(capacity_reservation)
            )
            if capacity_state is not None:
                if capacity_state.released:
                    code = (
                        "capacity_reservation_expired"
                        if capacity_state.token.expires_at_ms <= now
                        else "capacity_reservation_released"
                    )
                    raise ArtifactStoreError(code)
                if capacity_state.consumed_entries >= capacity_state.token.reserved_entries:
                    raise ArtifactStoreError("capacity_entries_exhausted")
                if (
                    receipt.artifact_id in capacity_state.begun_artifact_ids
                    or receipt.artifact_id in capacity_state.committed_artifact_ids
                ):
                    raise ArtifactStoreError("capacity_artifact_duplicate")
            if receipt.created_at_ms > now:
                raise ArtifactStoreError("receipt_created_in_future")
            if receipt.expires_at_ms > now + self._policy.ttl_seconds * 1_000:
                raise ArtifactStoreError("receipt_ttl_exceeds_policy")
            if receipt.expires_at_ms <= now:
                raise ArtifactStoreError("receipt_expired")
            paths = (
                self._partial_path(receipt.artifact_id),
                self._receipt_path(receipt.artifact_id),
                self._artifact_path(receipt.artifact_id),
            )
            if any(_lstat_optional(path) is not None for path in paths):
                raise ArtifactStoreError("duplicate_artifact")
            lifecycle_count, _ = self._inventory()
            reserved_entries, _ = self._active_reserved_capacity()
            if capacity_state is None:
                if lifecycle_count >= self._policy.max_entries:
                    raise ArtifactStoreError("entry_quota_exceeded")
                if lifecycle_count + reserved_entries >= self._policy.max_entries:
                    raise ArtifactStoreError("entry_quota_reserved")
            self._atomic_publish_new(
                paths[0],
                receipt.to_wire_bytes(),
                parent=self._staging,
            )
            if capacity_state is not None:
                capacity_state.consumed_entries += 1
                capacity_state.begun_artifact_ids.add(receipt.artifact_id)
            return receipt

    def _load_partial(self, expected: SegmentArtifactReceipt) -> SegmentArtifactReceipt:
        try:
            stored = decode_segment_artifact_receipt(
                _read_regular_bytes(
                    self._partial_path(expected.artifact_id),
                    maximum_bytes=MAX_SEGMENT_ARTIFACT_RECEIPT_BYTES,
                )
            )
        except (ArtifactStoreError, SegmentArtifactError) as exc:
            raise ArtifactStoreError("partial_receipt_invalid") from exc
        if (
            stored.state is not ArtifactLifecycleState.PARTIAL
            or stored.fingerprint != expected.fingerprint
        ):
            raise ArtifactStoreError("partial_receipt_mismatch")
        return stored

    def _inventory(self) -> tuple[int, int]:
        receipt_entries = self._bounded_entries(
            self._receipts,
            maximum=self._policy.max_recovery_entries,
        )
        partial_entries = self._bounded_entries(
            self._staging,
            maximum=self._policy.max_recovery_entries - len(receipt_entries),
        )
        lifecycle_ids: set[str] = set()
        total = 0
        for path in receipt_entries:
            match = _RECEIPT_FILE.fullmatch(path.name)
            if match is None:
                raise ArtifactStoreError("store_inventory_invalid")
            try:
                receipt = decode_segment_artifact_receipt(
                    _read_regular_bytes(path, maximum_bytes=MAX_SEGMENT_ARTIFACT_RECEIPT_BYTES)
                )
            except (ArtifactStoreError, SegmentArtifactError) as exc:
                raise ArtifactStoreError("store_inventory_invalid") from exc
            if receipt.artifact_id != match.group(1):
                raise ArtifactStoreError("store_inventory_invalid")
            lifecycle_ids.add(receipt.artifact_id)
            if receipt.state is ArtifactLifecycleState.COMPLETE:
                total += receipt.byte_length
        for path in partial_entries:
            match = _PARTIAL_FILE.fullmatch(path.name)
            if match is None:
                if _TEMP_FILE.fullmatch(path.name) is not None:
                    continue
                raise ArtifactStoreError("store_inventory_invalid")
            lifecycle_ids.add(match.group(1))
        return len(lifecycle_ids), total

    def commit(
        self,
        receipt: SegmentArtifactReceipt,
        payload: bytes,
        *,
        capacity_reservation: ArtifactCapacityReservationV1 | None = None,
    ) -> SegmentArtifactReceipt:
        if type(receipt) is not SegmentArtifactReceipt:
            raise ArtifactStoreError("receipt_type")
        if type(payload) is not bytes or not payload:
            raise ArtifactStoreError("artifact_payload")
        if len(payload) > self._policy.max_artifact_bytes:
            raise ArtifactStoreError("artifact_too_large")
        if not self._write_slots.acquire(blocking=False):
            raise ArtifactStoreError("write_concurrency_limit")
        try:
            with self._lock:
                self._require_enabled()
                now = self._now_ms()
                self._expire_capacity_reservations(now)
                capacity_state = (
                    None
                    if capacity_reservation is None
                    else self._capacity_state(capacity_reservation)
                )
                if capacity_state is not None:
                    if capacity_state.released:
                        code = (
                            "capacity_reservation_expired"
                            if capacity_state.token.expires_at_ms <= now
                            else "capacity_reservation_released"
                        )
                        raise ArtifactStoreError(code)
                    if receipt.artifact_id not in capacity_state.begun_artifact_ids:
                        raise ArtifactStoreError("capacity_artifact_not_begun")
                    if len(payload) > capacity_state.token.per_artifact_max_bytes:
                        raise ArtifactStoreError("artifact_exceeds_reservation")
                    if (
                        len(payload)
                        > capacity_state.token.reserved_bytes - capacity_state.consumed_bytes
                    ):
                        raise ArtifactStoreError("capacity_bytes_exhausted")
                partial = self._load_partial(receipt)
                if partial.expires_at_ms <= now:
                    raise ArtifactStoreError("receipt_expired")
                _, total = self._inventory()
                _, reserved_bytes = self._active_reserved_capacity()
                if capacity_state is None:
                    if total + len(payload) > self._policy.max_total_bytes:
                        raise ArtifactStoreError("byte_quota_exceeded")
                    if total + reserved_bytes + len(payload) > self._policy.max_total_bytes:
                        raise ArtifactStoreError("byte_quota_reserved")
                elif total + reserved_bytes > self._policy.max_total_bytes:
                    raise ArtifactStoreError("capacity_invariant")
                output_fingerprint = "sha256:" + hashlib.sha256(payload).hexdigest()
                completed = complete_segment_artifact_receipt(
                    partial,
                    output_fingerprint=output_fingerprint,
                    byte_length=len(payload),
                )
                artifact_path = self._artifact_path(receipt.artifact_id)
                receipt_path = self._receipt_path(receipt.artifact_id)
                self._atomic_publish_new(artifact_path, payload, parent=self._staging)
                try:
                    self._atomic_publish_new(
                        receipt_path,
                        completed.to_wire_bytes(),
                        parent=self._staging,
                    )
                except ArtifactStoreError:
                    _safe_unlink(artifact_path)
                    raise
                _safe_unlink(self._partial_path(receipt.artifact_id), missing_ok=False)
                if capacity_state is not None:
                    capacity_state.consumed_bytes += len(payload)
                    capacity_state.begun_artifact_ids.remove(receipt.artifact_id)
                    capacity_state.committed_artifact_ids.add(receipt.artifact_id)
                return completed
        finally:
            self._write_slots.release()

    def fail(self, receipt: SegmentArtifactReceipt, *, failure_code: str) -> SegmentArtifactReceipt:
        with self._lock:
            self._require_enabled()
            partial = self._load_partial(receipt)
            failed = fail_segment_artifact_receipt(partial, failure_code=failure_code)
            self._atomic_publish_new(
                self._receipt_path(receipt.artifact_id),
                failed.to_wire_bytes(),
                parent=self._staging,
            )
            _safe_unlink(self._partial_path(receipt.artifact_id), missing_ok=False)
            return failed

    @staticmethod
    def _inspection(
        receipt: SegmentArtifactReceipt,
        status: ArtifactInspectionStatus,
    ) -> ArtifactInspection:
        return ArtifactInspection(receipt.artifact_id, status, receipt.fingerprint)

    def inspect(
        self,
        expected: SegmentArtifactReceipt,
        *,
        maximum_bytes: int | None = None,
    ) -> ArtifactInspection:
        if type(expected) is not SegmentArtifactReceipt:
            raise ArtifactStoreError("receipt_type")
        if maximum_bytes is not None and (
            type(maximum_bytes) is not int
            or not 1 <= maximum_bytes <= self._policy.max_artifact_bytes
        ):
            raise ArtifactStoreError("read_limit")
        read_limit = self._policy.max_artifact_bytes if maximum_bytes is None else maximum_bytes
        if expected.byte_length > read_limit:
            raise ArtifactStoreError("read_limit")
        with self._lock:
            if not self._coordination.enabled:
                return self._inspection(expected, ArtifactInspectionStatus.DISABLED)
            if expected.state is not ArtifactLifecycleState.COMPLETE:
                return self._inspection(expected, ArtifactInspectionStatus.NOT_COMPLETE)
            receipt_path = self._receipt_path(expected.artifact_id)
            if _lstat_optional(receipt_path) is None:
                return self._inspection(expected, ArtifactInspectionStatus.MISSING)
            try:
                actual = decode_segment_artifact_receipt(
                    _read_regular_bytes(
                        receipt_path,
                        maximum_bytes=MAX_SEGMENT_ARTIFACT_RECEIPT_BYTES,
                    )
                )
            except ArtifactStoreError as exc:
                status = (
                    ArtifactInspectionStatus.UNSAFE
                    if exc.code == "unsafe_store_entry"
                    else ArtifactInspectionStatus.TAMPERED
                )
                return self._inspection(expected, status)
            except SegmentArtifactError:
                return self._inspection(expected, ArtifactInspectionStatus.TAMPERED)
            if actual.fingerprint != expected.fingerprint:
                return self._inspection(expected, ArtifactInspectionStatus.INCOMPATIBLE)
            if actual.expires_at_ms <= self._now_ms():
                return self._inspection(expected, ArtifactInspectionStatus.EXPIRED)
            artifact_path = self._artifact_path(expected.artifact_id)
            if _lstat_optional(artifact_path) is None:
                return self._inspection(expected, ArtifactInspectionStatus.MISSING)
            try:
                payload = _read_regular_bytes(
                    artifact_path,
                    maximum_bytes=read_limit,
                )
            except ArtifactStoreError as exc:
                status = (
                    ArtifactInspectionStatus.UNSAFE
                    if exc.code == "unsafe_store_entry"
                    else ArtifactInspectionStatus.TAMPERED
                )
                return self._inspection(expected, status)
            fingerprint = "sha256:" + hashlib.sha256(payload).hexdigest()
            if len(payload) != actual.byte_length or fingerprint != actual.output_fingerprint:
                return self._inspection(expected, ArtifactInspectionStatus.TAMPERED)
            return self._inspection(expected, ArtifactInspectionStatus.REUSABLE)

    @staticmethod
    def _plain_file_identity(path: Path) -> tuple[int, int, int, int] | None:
        metadata = _lstat_optional(path)
        if (
            metadata is None
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _is_link_or_reparse(path, metadata)
        ):
            return None
        return _identity(metadata)

    def inspect_with_identity(
        self,
        expected: SegmentArtifactReceipt,
        *,
        maximum_bytes: int | None = None,
    ) -> tuple[ArtifactInspection, ArtifactIdentity | None]:
        """Inspect fully and, when reusable, return the identity of the files that were verified.

        The identity is returned only when both files look the same before and after the verified
        read, so it never describes bytes this call did not hash.
        """

        if type(expected) is not SegmentArtifactReceipt:
            raise ArtifactStoreError("receipt_type")
        with self._lock:
            receipt_path = self._receipt_path(expected.artifact_id)
            artifact_path = self._artifact_path(expected.artifact_id)
            before = (
                self._plain_file_identity(receipt_path),
                self._plain_file_identity(artifact_path),
            )
            inspection = self.inspect(expected, maximum_bytes=maximum_bytes)
            if inspection.status is not ArtifactInspectionStatus.REUSABLE:
                return inspection, None
            receipt, artifact = (
                self._plain_file_identity(receipt_path),
                self._plain_file_identity(artifact_path),
            )
            if receipt is None or artifact is None or (receipt, artifact) != before:
                return inspection, None
            return inspection, ArtifactIdentity(
                expected.fingerprint, expected.expires_at_ms, receipt, artifact
            )

    def identity_current(
        self, expected: SegmentArtifactReceipt, identity: ArtifactIdentity
    ) -> bool:
        """Whether the files a full inspection verified are still in place; reads no content.

        CRITICAL: this is the cheap tier only. It cannot see a same-size replacement whose
        timestamp was restored, so it must never be the proof behind new authority: every
        operation that grants or renews media authority still runs a full inspection.
        """

        if type(expected) is not SegmentArtifactReceipt or type(identity) is not ArtifactIdentity:
            return False
        try:
            with self._lock:
                return (
                    self._coordination.enabled
                    and expected.state is ArtifactLifecycleState.COMPLETE
                    and identity._receipt_fingerprint == expected.fingerprint
                    and identity._expires_at_ms > self._now_ms()
                    and self._plain_file_identity(self._receipt_path(expected.artifact_id))
                    == identity._receipt
                    and self._plain_file_identity(self._artifact_path(expected.artifact_id))
                    == identity._artifact
                )
        except (ArtifactStoreError, OSError):
            return False

    def read(
        self,
        expected: SegmentArtifactReceipt,
        *,
        maximum_bytes: int | None = None,
    ) -> bytes:
        inspection = self.inspect(expected, maximum_bytes=maximum_bytes)
        if inspection.status is not ArtifactInspectionStatus.REUSABLE:
            raise ArtifactStoreError("artifact_not_reusable")
        read_limit = self._policy.max_artifact_bytes if maximum_bytes is None else maximum_bytes
        payload = _read_regular_bytes(
            self._artifact_path(expected.artifact_id),
            maximum_bytes=read_limit,
        )
        if (
            len(payload) != expected.byte_length
            or "sha256:" + hashlib.sha256(payload).hexdigest() != expected.output_fingerprint
        ):
            raise ArtifactStoreError("artifact_not_reusable")
        return payload

    def stream_artifact_into(
        self,
        expected: SegmentArtifactReceipt,
        destination: Path,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[int, str]:
        """Stream one exact reusable artifact into a caller-owned new file.

        M20-08: the incremental alternative to ``read`` — the payload never exists in memory
        beyond one bounded chunk, the destination is created exclusively (no clobber), and a
        length or fingerprint divergence removes the partial copy and fails closed. The
        filesystem locator of the stored artifact never leaves this store.
        """

        if not isinstance(destination, Path) or not destination.is_absolute():
            raise ArtifactStoreError("store_configuration")
        inspection = self.inspect(expected)
        if inspection.status is not ArtifactInspectionStatus.REUSABLE:
            raise ArtifactStoreError("artifact_not_reusable")
        length, fingerprint = _copy_regular_file_into_new(
            self._artifact_path(expected.artifact_id),
            destination,
            maximum_bytes=self._policy.max_artifact_bytes,
            should_cancel=should_cancel,
        )
        if length != expected.byte_length or fingerprint != expected.output_fingerprint:
            try:
                _safe_unlink_pinned(destination, missing_ok=True, maximum_links=1)
            except ArtifactStoreError:
                pass
            raise ArtifactStoreError("artifact_not_reusable")
        return length, fingerprint

    def _bounded_entries(self, directory: Path, *, maximum: int) -> tuple[Path, ...]:
        if maximum < 0:
            raise ArtifactStoreError("recovery_scan_limit")
        try:
            admitted = validate_directory(directory)
            before = admitted.lstat()
            entries: list[Path] = []
            with os.scandir(admitted) as iterator:
                for entry in iterator:
                    entries.append(admitted / entry.name)
                    if len(entries) > maximum:
                        raise ArtifactStoreError("recovery_scan_limit")
            after = validate_directory(admitted).lstat()
        except (UnsafePathError, OSError) as exc:
            raise ArtifactStoreError("unsafe_store_entry") from exc
        if _directory_identity(before) != _directory_identity(after):
            raise ArtifactStoreError("unsafe_store_entry")
        return tuple(entries)

    def recover(self) -> ArtifactRecoveryReport:
        with self._lock:
            scanned = expired = orphans = invalid = staging = 0
            now = self._now_ms()
            # IMPORTANT: admit the aggregate inventory before deleting any entry.
            staging_entries = self._bounded_entries(
                self._staging,
                maximum=self._policy.max_recovery_entries,
            )
            remaining = self._policy.max_recovery_entries - len(staging_entries)
            receipt_entries = self._bounded_entries(self._receipts, maximum=remaining)
            remaining -= len(receipt_entries)
            artifact_entries = self._bounded_entries(self._artifacts, maximum=remaining)
            for path in staging_entries:
                if (
                    _PARTIAL_FILE.fullmatch(path.name) is None
                    and _TEMP_FILE.fullmatch(path.name) is None
                ):
                    raise ArtifactStoreError("unsafe_recovery_entry")
            for path in receipt_entries:
                if _RECEIPT_FILE.fullmatch(path.name) is None:
                    raise ArtifactStoreError("unsafe_recovery_entry")
            for path in artifact_entries:
                if _ARTIFACT_FILE.fullmatch(path.name) is None:
                    raise ArtifactStoreError("unsafe_recovery_entry")
            for path in staging_entries:
                scanned += 1
                if (
                    _PARTIAL_FILE.fullmatch(path.name) is None
                    and _TEMP_FILE.fullmatch(path.name) is None
                ):
                    raise ArtifactStoreError("unsafe_recovery_entry")
                _safe_unlink(path, missing_ok=False)
                staging += 1

            known_receipts: set[str] = set()
            for path in receipt_entries:
                scanned += 1
                match = _RECEIPT_FILE.fullmatch(path.name)
                if match is None:
                    raise ArtifactStoreError("unsafe_recovery_entry")
                artifact_id = match.group(1)
                try:
                    receipt = decode_segment_artifact_receipt(
                        _read_regular_bytes(
                            path,
                            maximum_bytes=MAX_SEGMENT_ARTIFACT_RECEIPT_BYTES,
                        )
                    )
                except (ArtifactStoreError, SegmentArtifactError):
                    _safe_unlink(path, missing_ok=False)
                    artifact_path = self._artifact_path(artifact_id)
                    if _lstat_optional(artifact_path) is not None:
                        _safe_unlink(artifact_path, missing_ok=False)
                    invalid += 1
                    continue
                if receipt.artifact_id != artifact_id:
                    _safe_unlink(path, missing_ok=False)
                    invalid += 1
                    continue
                if receipt.expires_at_ms <= now:
                    _safe_unlink(path, missing_ok=False)
                    artifact_path = self._artifact_path(artifact_id)
                    if _lstat_optional(artifact_path) is not None:
                        _safe_unlink(artifact_path, missing_ok=False)
                    expired += 1
                    continue
                if receipt.state is ArtifactLifecycleState.COMPLETE:
                    artifact_path = self._artifact_path(artifact_id)
                    if _lstat_optional(artifact_path) is None:
                        _safe_unlink(path, missing_ok=False)
                        orphans += 1
                        continue
                    inspection = self.inspect(receipt)
                    if inspection.status is not ArtifactInspectionStatus.REUSABLE:
                        _safe_unlink(path, missing_ok=False)
                        _safe_unlink(artifact_path, missing_ok=False)
                        invalid += 1
                        continue
                known_receipts.add(artifact_id)

            for path in artifact_entries:
                scanned += 1
                match = _ARTIFACT_FILE.fullmatch(path.name)
                if match is None:
                    raise ArtifactStoreError("unsafe_recovery_entry")
                if match.group(1) not in known_receipts and _lstat_optional(path) is not None:
                    _safe_unlink(path, missing_ok=False)
                    orphans += 1
            return ArtifactRecoveryReport(
                expired_removed=expired,
                orphans_removed=orphans,
                invalid_removed=invalid,
                staging_removed=staging,
                scanned_entries=scanned,
            )

    def disable(self, *, purge: bool) -> ArtifactStoreDispositionReport:
        if type(purge) is not bool:
            raise ArtifactStoreError("store_disposition")
        with self._lock:
            self._persist_enabled(False)
            if not purge:
                return ArtifactStoreDispositionReport(False, True, 0)
            ids: set[str] = set()
            admitted: list[tuple[Path, tuple[re.Pattern[str], ...]]] = []
            remaining = self._policy.max_recovery_entries
            purge_sources: tuple[tuple[Path, tuple[re.Pattern[str], ...]], ...] = (
                (self._artifacts, (_ARTIFACT_FILE,)),
                (self._receipts, (_RECEIPT_FILE,)),
                (self._staging, (_PARTIAL_FILE, _TEMP_FILE)),
            )
            for directory, patterns in purge_sources:
                entries = self._bounded_entries(directory, maximum=remaining)
                remaining -= len(entries)
                admitted.extend((path, patterns) for path in entries)
            # IMPORTANT: validate the full aggregate before the first purge mutation.
            for path, patterns in admitted:
                matched = False
                for pattern in patterns:
                    match = pattern.fullmatch(path.name)
                    if match is not None:
                        matched = True
                        if match.lastindex:
                            ids.add(match.group(1))
                        break
                if not matched:
                    raise ArtifactStoreError("unsafe_recovery_entry")
            for path, _ in admitted:
                _safe_unlink(path, missing_ok=False)
            return ArtifactStoreDispositionReport(False, False, len(ids))

    def migrate(self) -> ArtifactStoreMigrationReport:
        with self._lock:
            self._validate_marker()
            return ArtifactStoreMigrationReport(
                status="already_current",
                from_schema=SEGMENT_ARTIFACT_STORE_SCHEMA,
                to_schema=SEGMENT_ARTIFACT_STORE_SCHEMA,
            )


__all__ = [
    "ARTIFACT_CAPACITY_RESERVATION_SCHEMA",
    "ArtifactCapacityReservationV1",
    "ArtifactIdentity",
    "ArtifactInspection",
    "ArtifactInspectionStatus",
    "ArtifactRecoveryReport",
    "ArtifactStoreDispositionReport",
    "ArtifactStoreError",
    "ArtifactStoreMigrationReport",
    "ArtifactStorePolicy",
    "MAX_ARTIFACT_CAPACITY_RESERVATION_SEGMENTS",
    "MAX_SEGMENT_ARTIFACT_RECOVERY_ENTRIES",
    "MAX_SEGMENT_ARTIFACT_STORE_BYTES",
    "MAX_SEGMENT_ARTIFACT_STORE_ENTRIES",
    "MAX_SEGMENT_ARTIFACT_STORE_WRITES",
    "PrivateSegmentArtifactStore",
    "SEGMENT_ARTIFACT_STORE_SCHEMA",
    "SEGMENT_ARTIFACT_STORE_STATE_SCHEMA",
]
