"""Private receipt-last store for M17-11 reconstruction outputs.

The store owns one explicit caller-supplied root.  Filesystem locators never enter
portable receipts or public inspection projections.  Immutable member files are
published first and the exact :class:`AVReconstructionReceipt` is published last as
the sole completion marker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from ..core.av_reconstruction import (
    AV_RECONSTRUCTION_RECEIPT_SCHEMA,
    MAX_AV_RECONSTRUCTION_RECEIPT_BYTES,
    AVOutputKind,
    AVReceiptOutput,
    AVReconstructionReceipt,
    decode_av_reconstruction_receipt,
)
from ..core.canonical import canonical_bytes, canonical_fingerprint
from ..core.errors import MediaProcessError
from ..core.safe_paths import (
    UnsafePathError,
    ensure_directory,
    validate_directory,
    validate_regular_file,
)
from .media_subprocess import OwnedOutputLease, create_output_lease
from .segment_artifact_store import (
    ArtifactStoreError,
    _directory_identity,
    _identity,
    _is_link_or_reparse,
    _is_windows_path_race_error,
)
from .segment_artifact_store import (
    _lstat_optional as _artifact_lstat_optional,
)
from .segment_artifact_store import (
    _read_regular_bytes as _artifact_read_regular_bytes,
)
from .segment_artifact_store import (
    _safe_unlink as _artifact_safe_unlink,
)
from .segment_artifact_store import (
    _validated_directories as _artifact_validated_directories,
)
from .segment_artifact_store import (
    _windows_delete_open_file as _artifact_windows_delete_open_file,
)
from .segment_artifact_store import (
    _windows_move_new_file as _artifact_windows_move_new_file,
)
from .segment_artifact_store import (
    _write_new_file as _artifact_write_new_file,
)

AV_RECONSTRUCTION_STORE_SCHEMA = "h3.context.av_reconstruction_store.v1"
AV_RECONSTRUCTION_STORE_STATE_SCHEMA = "h3.context.av_reconstruction_store_state.v1"
AV_RECONSTRUCTION_TRANSACTION_SCHEMA = "h3.context.av_reconstruction_transaction.v1"

_MARKER_NAME = ".h3-av-reconstruction-store-v1"
_STATE_NAME = "store-state.json"
_CHILD_NAMES = ("members", "receipts", "staging")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,127}\Z")
_MEMBER_FILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_-]{15,127})\.bin\Z")
_RECEIPT_FILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\.json\Z")
_TRANSACTION_DIR = re.compile(r"tx\.([0-9a-f]{64})\Z")
_STATE_TEMP_FILE = re.compile(r"state\.([0-9a-f]{32})\.tmp\Z")
_LEASE_DIR = re.compile(r"h3-media-[A-Za-z0-9_-]{1,128}\Z")
_LEASE_FILE = "artifact.bin"
_TRANSACTION_MARKER = "transaction.json"


class AVStoreError(RuntimeError):
    """Content-free private-store failure with one stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _translated_artifact_error(exc: ArtifactStoreError) -> AVStoreError:
    return AVStoreError(exc.code)


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return _artifact_lstat_optional(path)
    except ArtifactStoreError as exc:
        raise _translated_artifact_error(exc) from exc


def _read_regular_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    try:
        return _artifact_read_regular_bytes(path, maximum_bytes=maximum_bytes)
    except ArtifactStoreError as exc:
        raise _translated_artifact_error(exc) from exc


def _write_new_file(path: Path, payload: bytes) -> None:
    try:
        _artifact_write_new_file(path, payload)
    except ArtifactStoreError as exc:
        raise _translated_artifact_error(exc) from exc


def _safe_unlink(
    path: Path,
    *,
    missing_ok: bool = True,
    maximum_links: int = 1,
) -> bool:
    try:
        return _artifact_safe_unlink(
            path,
            missing_ok=missing_ok,
            maximum_links=maximum_links,
        )
    except ArtifactStoreError as exc:
        raise _translated_artifact_error(exc) from exc


@contextmanager
def _validated_directories(*directories: Path) -> Iterator[None]:
    try:
        with _artifact_validated_directories(*directories):
            yield
    except ArtifactStoreError as exc:
        raise _translated_artifact_error(exc) from exc


def _windows_move_new_file(source: Path, destination: Path) -> int:
    try:
        return _artifact_windows_move_new_file(source, destination)
    except ArtifactStoreError as exc:
        raise _translated_artifact_error(exc) from exc


def _windows_delete_open_file(descriptor: int) -> None:
    try:
        _artifact_windows_delete_open_file(descriptor)
    except ArtifactStoreError as exc:
        raise _translated_artifact_error(exc) from exc


class AVStoreInspectionStatus(str, Enum):
    COMPLETE = "complete"
    MISSING = "missing"
    INCOMPATIBLE = "incompatible"
    TAMPERED = "tampered"
    DISABLED = "disabled"
    UNSAFE = "unsafe"


def _positive(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise AVStoreError(field)
    return value


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise AVStoreError(field)
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise AVStoreError(field)
    return value


def _handle(value: object, field: str) -> str:
    if type(value) is not str or _HANDLE.fullmatch(value) is None:
        raise AVStoreError(field)
    return value


@dataclass(frozen=True, slots=True)
class AVStorePolicy:
    max_member_bytes: int
    max_total_bytes: int
    max_transactions: int
    max_members_per_transaction: int
    max_recovery_entries: int
    max_concurrent_writes: int
    transaction_ttl_ms: int

    def __post_init__(self) -> None:
        _positive(self.max_member_bytes, "policy_member_bytes", 4 * 1024 * 1024 * 1024)
        _positive(self.max_total_bytes, "policy_total_bytes", 16 * 1024 * 1024 * 1024)
        if self.max_total_bytes < self.max_member_bytes:
            raise AVStoreError("policy_total_below_member")
        _positive(self.max_transactions, "policy_transactions", 64)
        _positive(self.max_members_per_transaction, "policy_members", 65)
        _positive(self.max_recovery_entries, "policy_recovery_entries", 4_096)
        _positive(self.max_concurrent_writes, "policy_concurrent_writes", 8)
        _positive(self.transaction_ttl_ms, "policy_transaction_ttl", 86_400_000)
        minimum_inventory = self.max_transactions * (self.max_members_per_transaction + 1) + 1
        if self.max_recovery_entries < minimum_inventory:
            raise AVStoreError("policy_recovery_below_inventory")

    def to_wire(self) -> dict[str, int]:
        return {
            "max_member_bytes": self.max_member_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_transactions": self.max_transactions,
            "max_members_per_transaction": self.max_members_per_transaction,
            "max_recovery_entries": self.max_recovery_entries,
            "max_concurrent_writes": self.max_concurrent_writes,
            "transaction_ttl_ms": self.transaction_ttl_ms,
        }


@dataclass(frozen=True, slots=True)
class AVStoreTransaction:
    transaction_id: str
    plan_fingerprint: str
    approval_fingerprint: str
    created_at_ms: int
    expires_at_ms: int
    transaction_fingerprint: str | None = None
    schema: str = AV_RECONSTRUCTION_TRANSACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != AV_RECONSTRUCTION_TRANSACTION_SCHEMA:
            raise AVStoreError("unsupported_transaction_schema")
        _identifier(self.transaction_id, "transaction_id")
        _fingerprint(self.plan_fingerprint, "transaction_plan")
        _fingerprint(self.approval_fingerprint, "transaction_approval")
        _positive(self.created_at_ms, "transaction_created_at", 9_999_999_999_999)
        _positive(self.expires_at_ms, "transaction_expires_at", 9_999_999_999_999)
        if self.expires_at_ms <= self.created_at_ms:
            raise AVStoreError("transaction_expiry")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.transaction_fingerprint is None:
            object.__setattr__(self, "transaction_fingerprint", expected)
        elif self.transaction_fingerprint != expected:
            raise AVStoreError("transaction_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.transaction_fingerprint is None:  # pragma: no cover
            raise AVStoreError("transaction_fingerprint_uninitialized")
        return self.transaction_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction_id": self.transaction_id,
            "plan_fingerprint": self.plan_fingerprint,
            "approval_fingerprint": self.approval_fingerprint,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["transaction_fingerprint"] = self.fingerprint
        return value


@dataclass(frozen=True, slots=True)
class AVStoreInspection:
    transaction_id: str
    status: AVStoreInspectionStatus
    receipt_fingerprint: str

    def __post_init__(self) -> None:
        _identifier(self.transaction_id, "inspection_transaction_id")
        if type(self.status) is not AVStoreInspectionStatus:
            raise AVStoreError("inspection_status")
        _fingerprint(self.receipt_fingerprint, "inspection_receipt")

    def to_public_dict(self) -> dict[str, str]:
        token = hashlib.sha256(
            f"av-receipt:{self.receipt_fingerprint}".encode("ascii")
        ).hexdigest()[:16]
        return {
            "transaction_id": self.transaction_id,
            "status": self.status.value,
            "receipt_token": f"redacted:{token}",
        }


@dataclass(frozen=True, slots=True)
class AVSelectedOutputInspection:
    """Exact internal selected-member fact; never serialized or exposed to the browser."""

    handle: str
    byte_length: int
    content_fingerprint: str

    def __post_init__(self) -> None:
        _handle(self.handle, "selected_output_handle")
        _positive(self.byte_length, "selected_output_bytes", 4 * 1024 * 1024 * 1024)
        _fingerprint(self.content_fingerprint, "selected_output_fingerprint")


@dataclass(frozen=True, slots=True)
class AVStoreRecoveryReport:
    staging_removed: int
    orphan_members_removed: int
    invalid_receipts_removed: int
    scanned_entries: int

    def to_public_dict(self) -> dict[str, int]:
        return {
            "staging_removed": self.staging_removed,
            "orphan_members_removed": self.orphan_members_removed,
            "invalid_receipts_removed": self.invalid_receipts_removed,
            "scanned_entries": self.scanned_entries,
        }


class AVStoreReceipt(Protocol):
    """Structural surface every storable receipt contract must provide."""

    @property
    def transaction_id(self) -> str: ...

    @property
    def plan_fingerprint(self) -> str: ...

    @property
    def approval_fingerprint(self) -> str: ...

    @property
    def outputs(self) -> tuple[AVReceiptOutput, ...]: ...

    @property
    def completed_at_ms(self) -> int: ...

    @property
    def fingerprint(self) -> str: ...

    def to_wire_bytes(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class AVStoreReceiptCodec:
    """Injected receipt contract for one store root.

    The default codec is the exact M17-11 v1 receipt; the M20-09 seam path
    instantiates the same store mechanics over its own root with the seam
    receipt codec.  Two stores over one root must agree on the codec.
    """

    schema_id: str
    receipt_type: type
    decode: Callable[[bytes], object]
    max_receipt_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.schema_id) is not str
            or not self.schema_id
            or len(self.schema_id) > 128
            or type(self.receipt_type) is not type
            or not callable(self.decode)
            or type(self.max_receipt_bytes) is not int
            or not 1 <= self.max_receipt_bytes <= 16 * 1024 * 1024
        ):
            raise AVStoreError("receipt_codec_configuration")


def _default_receipt_codec() -> AVStoreReceiptCodec:
    return AVStoreReceiptCodec(
        schema_id=AV_RECONSTRUCTION_RECEIPT_SCHEMA,
        receipt_type=AVReconstructionReceipt,
        decode=decode_av_reconstruction_receipt,
        max_receipt_bytes=MAX_AV_RECONSTRUCTION_RECEIPT_BYTES,
    )


@dataclass(frozen=True, slots=True)
class _AVStoreExecutionClaim:
    """Opaque process-local authority for one admitted execution context."""

    transaction_fingerprint: str
    completed_receipt: AVStoreReceipt | None


@dataclass(slots=True)
class _RootCoordination:
    policy: AVStorePolicy
    lock: threading.RLock
    write_slots: threading.BoundedSemaphore
    receipt_schema: str
    enabled: bool = True
    active_execution_transactions: dict[str, _AVStoreExecutionClaim] = field(default_factory=dict)


_COORDINATORS_GUARD = threading.Lock()
_COORDINATORS: dict[str, _RootCoordination] = {}


def _decode_object(payload: bytes, *, code: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AVStoreError(code)
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except AVStoreError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError) as exc:
        raise AVStoreError(code) from exc
    if type(value) is not dict:
        raise AVStoreError(code)
    return value


def _hash_regular_file(path: Path, *, maximum_bytes: int) -> tuple[int, str]:
    """Hash one regular single-link file while pinning its identity and parent."""

    with _validated_directories(path.parent):
        try:
            admitted = validate_regular_file(path, maximum_bytes=maximum_bytes)
            before = admitted.lstat()
        except (UnsafePathError, OSError) as exc:
            raise AVStoreError("unsafe_store_entry") from exc
        if before.st_nlink != 1 or _is_link_or_reparse(admitted, before):
            raise AVStoreError("unsafe_store_entry")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(admitted, flags)
        except OSError as exc:
            raise AVStoreError("store_entry_unavailable") from exc
        digest = hashlib.sha256()
        total = 0
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
                raise AVStoreError("unsafe_store_entry")
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise AVStoreError("member_too_large")
                digest.update(chunk)
            after_handle = os.fstat(descriptor)
            after_path = admitted.lstat()
            if (
                after_path.st_nlink != 1
                or _is_link_or_reparse(admitted, after_path)
                or _identity(after_handle) != _identity(opened)
                or _identity(after_path) != _identity(opened)
            ):
                raise AVStoreError("unsafe_store_entry")
        except OSError as exc:
            raise AVStoreError("store_entry_unavailable") from exc
        finally:
            os.close(descriptor)
        return total, "sha256:" + digest.hexdigest()


class PrivateAVReconstructionStore:
    """One bounded M17-11 store with receipt-last visibility and exact retry."""

    def __init__(
        self,
        root: Path,
        *,
        policy: AVStorePolicy,
        clock_ms: Callable[[], int],
        receipt_codec: AVStoreReceiptCodec | None = None,
    ) -> None:
        if (
            not isinstance(root, Path)
            or type(policy) is not AVStorePolicy
            or not callable(clock_ms)
            or not (receipt_codec is None or type(receipt_codec) is AVStoreReceiptCodec)
        ):
            raise AVStoreError("store_configuration")
        self._policy = policy
        self._clock_ms = clock_ms
        self._codec = receipt_codec if receipt_codec is not None else _default_receipt_codec()
        key = os.path.normcase(str(root.absolute()))
        with _COORDINATORS_GUARD:
            coordination = _COORDINATORS.get(key)
            if coordination is None:
                coordination = _RootCoordination(
                    policy=policy,
                    lock=threading.RLock(),
                    write_slots=threading.BoundedSemaphore(policy.max_concurrent_writes),
                    receipt_schema=self._codec.schema_id,
                )
                _COORDINATORS[key] = coordination
            elif coordination.policy != policy:
                raise AVStoreError("store_policy_mismatch")
            elif coordination.receipt_schema != self._codec.schema_id:
                raise AVStoreError("store_codec_mismatch")
        self._coordination = coordination
        self._lock = coordination.lock
        self._write_slots = coordination.write_slots
        with self._lock:
            self._initialize(root)

    def _initialize(self, root: Path) -> None:
        try:
            metadata = _lstat_optional(root)
            if metadata is None:
                admitted = ensure_directory(root)
                initial_names: tuple[str, ...] = ()
            else:
                admitted = validate_directory(root)
                initial_names = tuple(entry.name for entry in admitted.iterdir())
        except (UnsafePathError, OSError) as exc:
            raise AVStoreError("unsafe_store_root") from exc
        self._root = admitted
        self._marker = admitted / _MARKER_NAME
        self._state = admitted / _STATE_NAME
        if _lstat_optional(self._marker) is None:
            if initial_names:
                raise AVStoreError("foreign_store_root")
            _write_new_file(self._marker, self._marker_payload())
        self._validate_marker()
        allowed = {_MARKER_NAME, _STATE_NAME, *_CHILD_NAMES}
        if any(entry.name not in allowed for entry in admitted.iterdir()):
            raise AVStoreError("foreign_store_root")
        try:
            for name in _CHILD_NAMES:
                ensure_directory(admitted / name)
        except (UnsafePathError, OSError) as exc:
            raise AVStoreError("unsafe_store_root") from exc
        self._members = admitted / "members"
        self._receipts = admitted / "receipts"
        self._staging = admitted / "staging"
        if _lstat_optional(self._state) is None:
            _write_new_file(self._state, self._state_payload(enabled=True))
        self._coordination.enabled = self._read_state()

    @property
    def active_execution_count(self) -> int:
        """Execution transactions currently claimed on this root (all store instances)."""

        with self._lock:
            return len(self._coordination.active_execution_transactions)

    @property
    def receipt_schema(self) -> str:
        """The schema identifier of the receipt codec this store was built with."""

        return self._codec.schema_id

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or value <= 0:
            raise AVStoreError("store_clock")
        return value

    def _marker_payload(self) -> bytes:
        value: dict[str, object] = {
            "schema": AV_RECONSTRUCTION_STORE_SCHEMA,
            "policy": self._policy.to_wire(),
        }
        # COMPATIBILITY: an absent receipt_schema is the exact legacy v1 codec;
        # only additive codecs extend the persisted marker.
        if self._codec.schema_id != AV_RECONSTRUCTION_RECEIPT_SCHEMA:
            value["receipt_schema"] = self._codec.schema_id
        return canonical_bytes(value)

    @staticmethod
    def _state_payload(*, enabled: bool) -> bytes:
        return canonical_bytes({"schema": AV_RECONSTRUCTION_STORE_STATE_SCHEMA, "enabled": enabled})

    def _validate_marker(self) -> None:
        value = _decode_object(
            _read_regular_bytes(self._marker, maximum_bytes=4096),
            code="unsupported_store_schema",
        )
        legacy = {
            "schema": AV_RECONSTRUCTION_STORE_SCHEMA,
            "policy": self._policy.to_wire(),
        }
        expected = {
            **legacy,
            "receipt_schema": self._codec.schema_id,
        }
        if value == legacy:
            if self._codec.schema_id == AV_RECONSTRUCTION_RECEIPT_SCHEMA:
                return
            raise AVStoreError("store_codec_mismatch")
        if value == expected:
            return
        # SECURITY: a persisted root permanently pins its receipt codec. Reopening
        # with a different codec must refuse loudly here (the process-local
        # coordinator guard does not survive a restart); reaching recovery instead
        # would delete the other codec's committed receipts as undecodable.
        if (
            set(value) == set(expected)
            and value.get("schema") == legacy["schema"]
            and value.get("policy") == legacy["policy"]
        ):
            raise AVStoreError("store_codec_mismatch")
        raise AVStoreError("unsupported_store_schema")

    def _read_state(self) -> bool:
        value = _decode_object(
            _read_regular_bytes(self._state, maximum_bytes=1024),
            code="invalid_store_state",
        )
        if (
            set(value) != {"schema", "enabled"}
            or value["schema"] != AV_RECONSTRUCTION_STORE_STATE_SCHEMA
            or type(value["enabled"]) is not bool
        ):
            raise AVStoreError("invalid_store_state")
        return bool(value["enabled"])

    def _persist_enabled(self, enabled: bool) -> None:
        temporary = self._staging / f"state.{uuid.uuid4().hex}.tmp"
        try:
            with _validated_directories(self._root, self._staging):
                _write_new_file(temporary, self._state_payload(enabled=enabled))
                validate_regular_file(self._state, maximum_bytes=1024)
                validate_regular_file(temporary, maximum_bytes=1024)
                # CRITICAL: replace only the validated store-owned state file.
                os.replace(temporary, self._state)
                validate_regular_file(self._state, maximum_bytes=1024)
        except (OSError, UnsafePathError) as exc:
            try:
                _safe_unlink(temporary, maximum_links=1)
            except (OSError, RuntimeError):
                pass
            raise AVStoreError("store_state_write_failed") from exc
        self._coordination.enabled = enabled

    def _require_enabled(self) -> None:
        if not self._coordination.enabled:
            raise AVStoreError("store_disabled")

    def _require_execution_claim(
        self,
        transaction: AVStoreTransaction,
        claim: _AVStoreExecutionClaim | None,
    ) -> None:
        active = self._coordination.active_execution_transactions.get(transaction.fingerprint)
        if active is None:
            if claim is not None:
                raise AVStoreError("execution_claim_inactive")
            return
        # SECURITY: only the exact process-local capability yielded by claim_execution may
        # mutate an actively owned transaction; equal caller-constructed values are insufficient.
        if active is not claim:
            raise AVStoreError("transaction_in_progress")

    def _transaction_path(self, transaction: AVStoreTransaction) -> Path:
        return self._staging / f"tx.{transaction.fingerprint.removeprefix('sha256:')}"

    def _receipt_path(self, transaction_id: str) -> Path:
        return self._receipts / f"{_identifier(transaction_id, 'transaction_id')}.json"

    def _member_path(self, handle: str) -> Path:
        return self._members / f"{_handle(handle, 'output_handle')}.bin"

    def _transaction_marker(self, transaction: AVStoreTransaction) -> Path:
        return self._transaction_path(transaction) / _TRANSACTION_MARKER

    def _load_transaction(self, expected: AVStoreTransaction) -> AVStoreTransaction:
        path = self._transaction_marker(expected)
        try:
            value = _decode_object(
                _read_regular_bytes(path, maximum_bytes=4096),
                code="transaction_invalid",
            )
            transaction = AVStoreTransaction(
                transaction_id=cast(str, value["transaction_id"]),
                plan_fingerprint=cast(str, value["plan_fingerprint"]),
                approval_fingerprint=cast(str, value["approval_fingerprint"]),
                created_at_ms=cast(int, value["created_at_ms"]),
                expires_at_ms=cast(int, value["expires_at_ms"]),
                transaction_fingerprint=cast(str, value["transaction_fingerprint"]),
                schema=cast(str, value["schema"]),
            )
        except (KeyError, TypeError, AVStoreError) as exc:
            raise AVStoreError("transaction_invalid") from exc
        if transaction.fingerprint != expected.fingerprint:
            raise AVStoreError("transaction_identity_mismatch")
        return transaction

    def begin(
        self,
        *,
        transaction_id: str,
        plan_fingerprint: str,
        approval_fingerprint: str,
        expires_at_ms: int,
    ) -> AVStoreTransaction:
        with self._lock:
            self._require_enabled()
            now = self._now_ms()
            transaction = AVStoreTransaction(
                transaction_id=transaction_id,
                plan_fingerprint=plan_fingerprint,
                approval_fingerprint=approval_fingerprint,
                created_at_ms=now,
                expires_at_ms=expires_at_ms,
            )
            if expires_at_ms > now + self._policy.transaction_ttl_ms:
                raise AVStoreError("transaction_ttl_exceeded")
            receipt_path = self._receipt_path(transaction_id)
            if _lstat_optional(receipt_path) is not None:
                existing = self._load_receipt(transaction_id)
                if existing is None:  # pragma: no cover - protected by the same-root lock
                    raise AVStoreError("identity_conflict")
                if (
                    existing.plan_fingerprint != transaction.plan_fingerprint
                    or existing.approval_fingerprint != transaction.approval_fingerprint
                ):
                    raise AVStoreError("identity_conflict")
                return transaction
            receipt_entries = self._bounded_entries(
                self._receipts,
                maximum=self._policy.max_recovery_entries,
            )
            remaining = self._policy.max_recovery_entries - len(receipt_entries)
            staging_entries = self._bounded_entries(self._staging, maximum=remaining)
            for entry in receipt_entries:
                entry_metadata = _lstat_optional(entry)
                if (
                    _RECEIPT_FILE.fullmatch(entry.name) is None
                    or entry_metadata is None
                    or entry_metadata.st_nlink != 1
                    or _is_link_or_reparse(entry, entry_metadata)
                    or not stat.S_ISREG(entry_metadata.st_mode)
                ):
                    raise AVStoreError("store_inventory_invalid")
            active_transactions = 0
            matching_active: AVStoreTransaction | None = None
            for entry in staging_entries:
                entry_metadata = _lstat_optional(entry)
                if (
                    _TRANSACTION_DIR.fullmatch(entry.name) is None
                    or entry_metadata is None
                    or _is_link_or_reparse(entry, entry_metadata)
                    or not stat.S_ISDIR(entry_metadata.st_mode)
                ):
                    raise AVStoreError("store_inventory_invalid")
                try:
                    value = _decode_object(
                        _read_regular_bytes(entry / _TRANSACTION_MARKER, maximum_bytes=4096),
                        code="transaction_invalid",
                    )
                    active = AVStoreTransaction(
                        transaction_id=cast(str, value["transaction_id"]),
                        plan_fingerprint=cast(str, value["plan_fingerprint"]),
                        approval_fingerprint=cast(str, value["approval_fingerprint"]),
                        created_at_ms=cast(int, value["created_at_ms"]),
                        expires_at_ms=cast(int, value["expires_at_ms"]),
                        transaction_fingerprint=cast(str, value["transaction_fingerprint"]),
                        schema=cast(str, value["schema"]),
                    )
                except (KeyError, TypeError, AVStoreError) as exc:
                    raise AVStoreError("store_inventory_invalid") from exc
                match = _TRANSACTION_DIR.fullmatch(entry.name)
                if match is None or match.group(1) != active.fingerprint.removeprefix("sha256:"):
                    raise AVStoreError("store_inventory_invalid")
                if active.transaction_id == transaction.transaction_id:
                    if (
                        matching_active is not None
                        or active.plan_fingerprint != transaction.plan_fingerprint
                        or active.approval_fingerprint != transaction.approval_fingerprint
                        or active.expires_at_ms != transaction.expires_at_ms
                    ):
                        raise AVStoreError("identity_conflict")
                    matching_active = active
                active_transactions += 1
            if matching_active is not None:
                return matching_active
            if len(receipt_entries) + active_transactions >= self._policy.max_transactions:
                raise AVStoreError("transaction_quota_exceeded")
            path = self._transaction_path(transaction)
            with _validated_directories(self._root, self._staging):
                try:
                    path.mkdir(mode=0o700, exist_ok=False)
                    validate_directory(path)
                    _write_new_file(
                        path / _TRANSACTION_MARKER, canonical_bytes(transaction.to_wire())
                    )
                except (OSError, UnsafePathError) as exc:
                    raise AVStoreError("transaction_begin_failed") from exc
            return transaction

    def allocate_output(
        self,
        transaction: AVStoreTransaction,
        *,
        handle: str,
        kind: AVOutputKind,
        claim: _AVStoreExecutionClaim | None = None,
    ) -> OwnedOutputLease:
        if type(transaction) is not AVStoreTransaction or type(kind) is not AVOutputKind:
            raise AVStoreError("output_allocation")
        _handle(handle, "output_handle")
        with self._lock:
            self._require_enabled()
            self._require_execution_claim(transaction, claim)
            stored = self._load_transaction(transaction)
            if stored.expires_at_ms <= self._now_ms():
                raise AVStoreError("transaction_expired")
            if _lstat_optional(self._member_path(handle)) is not None:
                raise AVStoreError("identity_conflict")
            try:
                entries = self._bounded_entries(
                    self._transaction_path(transaction),
                    maximum=self._policy.max_members_per_transaction + 1,
                )
            except AVStoreError as exc:
                if exc.code == "recovery_scan_limit":
                    raise AVStoreError("member_count_limit") from exc
                raise
            lease_count = 0
            for entry in entries:
                if entry.name == _TRANSACTION_MARKER:
                    continue
                metadata = _lstat_optional(entry)
                if (
                    _LEASE_DIR.fullmatch(entry.name) is None
                    or metadata is None
                    or _is_link_or_reparse(entry, metadata)
                    or not stat.S_ISDIR(metadata.st_mode)
                ):
                    raise AVStoreError("transaction_invalid")
                lease_count += 1
            if lease_count >= self._policy.max_members_per_transaction:
                raise AVStoreError("member_count_limit")
            try:
                return create_output_lease(self._transaction_path(transaction), suffix=".bin")
            except MediaProcessError as exc:
                raise AVStoreError("output_allocation_failed") from exc

    def _bounded_entries(self, directory: Path, *, maximum: int) -> tuple[Path, ...]:
        if maximum < 0:
            raise AVStoreError("recovery_scan_limit")
        try:
            admitted = validate_directory(directory)
            before = admitted.lstat()
            entries: list[Path] = []
            with os.scandir(admitted) as iterator:
                for entry in iterator:
                    entries.append(admitted / entry.name)
                    if len(entries) > maximum:
                        raise AVStoreError("recovery_scan_limit")
            after = validate_directory(admitted).lstat()
        except (UnsafePathError, OSError) as exc:
            raise AVStoreError("unsafe_store_entry") from exc
        if _directory_identity(before) != _directory_identity(after):
            raise AVStoreError("unsafe_store_entry")
        return tuple(entries)

    def _inventory(self) -> tuple[int, int, int]:
        receipt_entries = self._bounded_entries(
            self._receipts, maximum=self._policy.max_recovery_entries
        )
        remaining = self._policy.max_recovery_entries - len(receipt_entries)
        member_entries = self._bounded_entries(self._members, maximum=remaining)
        transactions: set[str] = set()
        total_bytes = 0
        for receipt_path in receipt_entries:
            match = _RECEIPT_FILE.fullmatch(receipt_path.name)
            if match is None:
                raise AVStoreError("store_inventory_invalid")
            try:
                receipt = self._decode_receipt(
                    _read_regular_bytes(
                        receipt_path,
                        maximum_bytes=self._codec.max_receipt_bytes,
                    )
                )
            except (AVStoreError, ValueError) as exc:
                raise AVStoreError("store_inventory_invalid") from exc
            if receipt.transaction_id != match.group(1):
                raise AVStoreError("store_inventory_invalid")
            transactions.add(receipt.transaction_id)
        for member_path in member_entries:
            if _MEMBER_FILE.fullmatch(member_path.name) is None:
                raise AVStoreError("store_inventory_invalid")
            try:
                admitted = validate_regular_file(
                    member_path,
                    maximum_bytes=self._policy.max_member_bytes,
                )
                metadata = admitted.lstat()
            except (UnsafePathError, OSError) as exc:
                raise AVStoreError("store_inventory_invalid") from exc
            if metadata.st_nlink != 1 or _is_link_or_reparse(admitted, metadata):
                raise AVStoreError("store_inventory_invalid")
            total_bytes += metadata.st_size
        return len(transactions), len(member_entries), total_bytes

    def _publish_member(self, source: Path, final: Path) -> None:
        """Atomically publish one verified lease file without caller-path support."""

        with _validated_directories(self._root, source.parent, final.parent):
            try:
                admitted_source = validate_regular_file(
                    source, maximum_bytes=self._policy.max_member_bytes
                )
                source_before = admitted_source.lstat()
                if source_before.st_nlink != 1 or _is_link_or_reparse(source, source_before):
                    raise AVStoreError("unsafe_store_entry")
                if _lstat_optional(final) is not None:
                    raise AVStoreError("identity_conflict")
                if os.name == "nt":
                    # CRITICAL: retain the no-delete-share source handle across the exact move.
                    descriptor = _windows_move_new_file(source, final)
                    try:
                        if _identity(os.fstat(descriptor)) != _identity(source_before):
                            raise AVStoreError("unsafe_store_entry")
                    except AVStoreError:
                        _windows_delete_open_file(descriptor)
                        raise
                    finally:
                        os.close(descriptor)
                else:
                    os.link(source, final, follow_symlinks=False)
                    _safe_unlink(source, missing_ok=False, maximum_links=2)
                final_metadata = validate_regular_file(
                    final, maximum_bytes=self._policy.max_member_bytes
                ).lstat()
                if final_metadata.st_nlink != 1 or _identity(final_metadata) != _identity(
                    source_before
                ):
                    raise AVStoreError("unsafe_store_entry")
            except FileExistsError as exc:
                raise AVStoreError("identity_conflict") from exc
            except AVStoreError:
                raise
            except (UnsafePathError, OSError) as exc:
                if isinstance(exc, OSError) and _is_windows_path_race_error(exc):
                    raise AVStoreError("unsafe_store_entry") from exc
                raise AVStoreError("publication_failed") from exc

    def _publish_receipt(self, receipt: AVStoreReceipt) -> None:
        with _validated_directories(self._root, self._receipts):
            try:
                _write_new_file(
                    self._receipt_path(receipt.transaction_id),
                    receipt.to_wire_bytes(),
                )
            except AVStoreError:
                raise
            except Exception as exc:
                raise AVStoreError("publication_failed") from exc

    def _decode_receipt(self, payload: bytes) -> AVStoreReceipt:
        # The codec may raise its own ValueError-family contract error; every
        # decode failure maps to the same content-free receipt_invalid code.
        decoded = self._codec.decode(payload)
        if type(decoded) is not self._codec.receipt_type:
            raise AVStoreError("receipt_invalid")
        return cast(AVStoreReceipt, decoded)

    def _load_receipt(self, transaction_id: str) -> AVStoreReceipt | None:
        path = self._receipt_path(transaction_id)
        if _lstat_optional(path) is None:
            return None
        try:
            receipt = self._decode_receipt(
                _read_regular_bytes(path, maximum_bytes=self._codec.max_receipt_bytes)
            )
        except AVStoreError as exc:
            if exc.code in {"unsafe_store_entry", "store_entry_unavailable"}:
                raise
            raise AVStoreError("receipt_invalid") from exc
        except ValueError as exc:
            raise AVStoreError("receipt_invalid") from exc
        if receipt.transaction_id != transaction_id:
            raise AVStoreError("receipt_invalid")
        return receipt

    def _cleanup_transaction(self, transaction: AVStoreTransaction) -> None:
        path = self._transaction_path(transaction)
        if _lstat_optional(path) is None:
            return
        entries = self._bounded_entries(path, maximum=self._policy.max_members_per_transaction + 1)
        for entry in entries:
            if entry.name == _TRANSACTION_MARKER:
                _safe_unlink(entry, missing_ok=False)
                continue
            if not entry.is_dir() or _LEASE_DIR.fullmatch(entry.name) is None:
                raise AVStoreError("cleanup_failed")
            lease_entries = self._bounded_entries(entry, maximum=1)
            if lease_entries:
                if len(lease_entries) != 1 or lease_entries[0].name != _LEASE_FILE:
                    raise AVStoreError("cleanup_failed")
                _safe_unlink(lease_entries[0], missing_ok=False)
            try:
                entry.rmdir()
            except OSError as exc:
                raise AVStoreError("cleanup_failed") from exc
        try:
            path.rmdir()
        except OSError as exc:
            raise AVStoreError("cleanup_failed") from exc

    def load_complete(
        self,
        *,
        transaction_id: str,
        plan_fingerprint: str,
        approval_fingerprint: str,
    ) -> AVStoreReceipt | None:
        """Return only an exact, fully verified completed transaction."""

        _identifier(transaction_id, "transaction_id")
        _fingerprint(plan_fingerprint, "transaction_plan")
        _fingerprint(approval_fingerprint, "transaction_approval")
        with self._lock:
            self._require_enabled()
            receipt = self._load_receipt(transaction_id)
            if receipt is None:
                return None
            if (
                receipt.plan_fingerprint != plan_fingerprint
                or receipt.approval_fingerprint != approval_fingerprint
            ):
                raise AVStoreError("identity_conflict")
            if self.inspect(receipt).status is not AVStoreInspectionStatus.COMPLETE:
                raise AVStoreError("reconstruction_not_complete")
            return receipt

    def abort(
        self,
        transaction: AVStoreTransaction,
        *,
        claim: _AVStoreExecutionClaim | None = None,
    ) -> None:
        """Remove only the exact active transaction after an integration failure."""

        if type(transaction) is not AVStoreTransaction:
            raise AVStoreError("transaction_type")
        with self._lock:
            self._require_enabled()
            self._require_execution_claim(transaction, claim)
            if _lstat_optional(self._transaction_path(transaction)) is None:
                return
            self._load_transaction(transaction)
            try:
                self._cleanup_transaction(transaction)
            except (AVStoreError, ArtifactStoreError) as exc:
                # CRITICAL: an unprovable transaction cleanup closes the whole store.
                try:
                    self._persist_enabled(False)
                except AVStoreError:
                    self._coordination.enabled = False
                raise AVStoreError("cleanup_failed") from exc

    @contextmanager
    def claim_execution(
        self,
        transaction: AVStoreTransaction,
    ) -> Iterator[_AVStoreExecutionClaim]:
        """Own one exact transaction across allocation, media use and commit."""

        if type(transaction) is not AVStoreTransaction:
            raise AVStoreError("transaction_type")
        claim: _AVStoreExecutionClaim
        completed: AVStoreReceipt | None = None
        with self._lock:
            self._require_enabled()
            receipt = self._load_receipt(transaction.transaction_id)
            if receipt is not None:
                if (
                    receipt.plan_fingerprint != transaction.plan_fingerprint
                    or receipt.approval_fingerprint != transaction.approval_fingerprint
                ):
                    raise AVStoreError("identity_conflict")
                if self.inspect(receipt).status is not AVStoreInspectionStatus.COMPLETE:
                    raise AVStoreError("reconstruction_not_complete")
                completed = receipt
                claim = _AVStoreExecutionClaim(
                    transaction_fingerprint=transaction.fingerprint,
                    completed_receipt=completed,
                )
            else:
                stored = self._load_transaction(transaction)
                key = stored.fingerprint
                if key in self._coordination.active_execution_transactions:
                    raise AVStoreError("transaction_in_progress")
                claim = _AVStoreExecutionClaim(
                    transaction_fingerprint=key,
                    completed_receipt=None,
                )
                self._coordination.active_execution_transactions[key] = claim
        try:
            yield claim
        finally:
            if completed is None:
                with self._lock:
                    active = self._coordination.active_execution_transactions.get(
                        transaction.fingerprint
                    )
                    if active is claim:
                        self._coordination.active_execution_transactions.pop(
                            transaction.fingerprint, None
                        )

    def commit(
        self,
        transaction: AVStoreTransaction,
        receipt: AVStoreReceipt,
        *,
        output_leases: tuple[tuple[str, OwnedOutputLease], ...],
        claim: _AVStoreExecutionClaim | None = None,
    ) -> AVStoreReceipt:
        if (
            type(transaction) is not AVStoreTransaction
            or type(receipt) is not self._codec.receipt_type
        ):
            raise AVStoreError("commit_type")
        if type(output_leases) is not tuple:
            raise AVStoreError("output_leases")
        if not self._write_slots.acquire(blocking=False):
            raise AVStoreError("write_concurrency_limit")
        try:
            with self._lock:
                self._require_enabled()
                self._require_execution_claim(transaction, claim)
                existing = self._load_receipt(transaction.transaction_id)
                if existing is not None:
                    if (
                        transaction.transaction_id != existing.transaction_id
                        or transaction.plan_fingerprint != existing.plan_fingerprint
                        or transaction.approval_fingerprint != existing.approval_fingerprint
                        or existing.fingerprint != receipt.fingerprint
                    ):
                        raise AVStoreError("identity_conflict")
                    if self.inspect(existing).status is not AVStoreInspectionStatus.COMPLETE:
                        raise AVStoreError("identity_conflict")
                    return existing
                stored = self._load_transaction(transaction)
                now = self._now_ms()
                if stored.expires_at_ms <= now:
                    raise AVStoreError("transaction_expired")
                if not (
                    receipt.transaction_id == stored.transaction_id
                    and receipt.plan_fingerprint == stored.plan_fingerprint
                    and receipt.approval_fingerprint == stored.approval_fingerprint
                    and stored.created_at_ms <= receipt.completed_at_ms <= now
                ):
                    raise AVStoreError("transaction_identity_mismatch")
                if len(receipt.outputs) > self._policy.max_members_per_transaction:
                    raise AVStoreError("member_count_limit")
                pairs = dict(output_leases)
                if len(pairs) != len(output_leases) or set(pairs) != {
                    item.handle for item in receipt.outputs
                }:
                    raise AVStoreError("output_lease_identity_mismatch")
                expected_by_handle = {item.handle: item for item in receipt.outputs}
                total_new = 0
                for handle, lease in output_leases:
                    _handle(handle, "output_handle")
                    if not isinstance(lease, OwnedOutputLease):
                        raise AVStoreError("output_lease_type")
                    try:
                        if lease.root.resolve(strict=True) != self._transaction_path(
                            transaction
                        ).resolve(strict=True):
                            raise AVStoreError("output_lease_root_mismatch")
                    except (OSError, RuntimeError) as exc:
                        raise AVStoreError("output_lease_root_mismatch") from exc
                    state, size = lease.inspect_artifact(self._policy.max_member_bytes)
                    if state != "ok":
                        raise AVStoreError("media_output_invalid")
                    hashed_size, content_fingerprint = _hash_regular_file(
                        lease.path, maximum_bytes=self._policy.max_member_bytes
                    )
                    expected = expected_by_handle[handle]
                    if (
                        size != hashed_size
                        or hashed_size != expected.byte_length
                        or content_fingerprint != expected.content_fingerprint
                    ):
                        raise AVStoreError("media_output_invalid")
                    total_new += size
                transaction_count, member_count, total_bytes = self._inventory()
                if transaction_count >= self._policy.max_transactions:
                    raise AVStoreError("transaction_quota_exceeded")
                if member_count + len(receipt.outputs) > (
                    self._policy.max_transactions * self._policy.max_members_per_transaction
                ):
                    raise AVStoreError("member_quota_exceeded")
                if total_bytes + total_new > self._policy.max_total_bytes:
                    raise AVStoreError("byte_quota_exceeded")
                published: list[Path] = []
                try:
                    for output in receipt.outputs:
                        lease = pairs[output.handle]
                        final = self._member_path(output.handle)
                        self._publish_member(lease.path, final)
                        published.append(final)
                        try:
                            lease.release()
                        except (MediaProcessError, OSError, RuntimeError) as exc:
                            raise AVStoreError("cleanup_failed") from exc
                    # Receipt publication is deliberately the final visibility operation.
                    self._publish_receipt(receipt)
                except Exception as primary_exc:
                    cleanup_failed = (
                        isinstance(primary_exc, AVStoreError)
                        and primary_exc.code == "cleanup_failed"
                    ) or isinstance(primary_exc, ArtifactStoreError)
                    for final in reversed(published):
                        if _lstat_optional(final) is None:
                            continue
                        try:
                            _safe_unlink(final, missing_ok=False)
                        except Exception:
                            cleanup_failed = True
                    if cleanup_failed:
                        try:
                            self._persist_enabled(False)
                        except AVStoreError:
                            self._coordination.enabled = False
                        raise AVStoreError("cleanup_failed") from None
                    raise
                try:
                    self._cleanup_transaction(transaction)
                except (AVStoreError, ArtifactStoreError) as exc:
                    try:
                        self._persist_enabled(False)
                    except AVStoreError:
                        self._coordination.enabled = False
                    raise AVStoreError("cleanup_failed") from exc
                return receipt
        finally:
            self._write_slots.release()

    def retire_unreferenced(
        self,
        *,
        keep_transaction_ids: frozenset[str],
        completed_before_ms: int,
    ) -> int:
        """Remove complete reconstructions that no live owner references any more.

        IMPORTANT (B-M2522-STORE-01): a committed receipt counts against `max_transactions` and
        the byte quota for the life of the root, which outlives every process-local owner, so
        without retirement the store refuses all work once the quota fills. The caller passes
        the complete set of transactions its live owners can still reach, taken at
        `completed_before_ms`. A receipt completed at or after that instant is never retired, so a
        concurrent publication that committed after the snapshot keeps its output. The receipt
        goes first: an unlinked receipt is unreachable at once, and a member that cannot be
        removed now (for example, still open for reading) is left as an orphan for `recover`.
        """

        if (
            type(keep_transaction_ids) is not frozenset
            or not all(type(item) is str for item in keep_transaction_ids)
            or type(completed_before_ms) is not int
            or completed_before_ms <= 0
        ):
            raise AVStoreError("retirement_invalid")
        with self._lock:
            self._require_enabled()
            retired = 0
            for path in self._bounded_entries(
                self._receipts, maximum=self._policy.max_recovery_entries
            ):
                match = _RECEIPT_FILE.fullmatch(path.name)
                if match is None:
                    raise AVStoreError("store_inventory_invalid")
                transaction_id = match.group(1)
                if transaction_id in keep_transaction_ids:
                    continue
                try:
                    receipt = self._load_receipt(transaction_id)
                except AVStoreError as exc:
                    if exc.code != "receipt_invalid":
                        raise
                    # An invalid receipt is recovery's to remove, not retirement's.
                    continue
                if receipt is None or receipt.completed_at_ms >= completed_before_ms:
                    continue
                _safe_unlink(path, missing_ok=False)
                retired += 1
                for output in receipt.outputs:
                    try:
                        _safe_unlink(self._member_path(output.handle), missing_ok=True)
                    except (AVStoreError, OSError):
                        continue
            return retired

    def recover(self) -> AVStoreRecoveryReport:
        """Remove only bounded, verified M17-11 crash state and unreachable members."""

        with self._lock:
            if self._coordination.active_execution_transactions:
                raise AVStoreError("transaction_in_progress")
            now = self._now_ms()
            staging_entries = self._bounded_entries(
                self._staging,
                maximum=self._policy.max_recovery_entries,
            )
            remaining = self._policy.max_recovery_entries - len(staging_entries)
            receipt_entries = self._bounded_entries(self._receipts, maximum=remaining)
            remaining -= len(receipt_entries)
            member_entries = self._bounded_entries(self._members, maximum=remaining)

            # IMPORTANT: admit the complete aggregate inventory before the first deletion.
            admitted_transactions: list[AVStoreTransaction] = []
            admitted_state_temps: list[Path] = []
            for path in staging_entries:
                metadata = _lstat_optional(path)
                if _STATE_TEMP_FILE.fullmatch(path.name) is not None:
                    if (
                        metadata is None
                        or metadata.st_nlink != 1
                        or _is_link_or_reparse(path, metadata)
                        or not stat.S_ISREG(metadata.st_mode)
                    ):
                        raise AVStoreError("unsafe_recovery_entry")
                    try:
                        value = _decode_object(
                            _read_regular_bytes(path, maximum_bytes=1024),
                            code="invalid_store_state",
                        )
                    except AVStoreError as exc:
                        raise AVStoreError("unsafe_recovery_entry") from exc
                    if (
                        set(value) != {"schema", "enabled"}
                        or value["schema"] != AV_RECONSTRUCTION_STORE_STATE_SCHEMA
                        or type(value["enabled"]) is not bool
                    ):
                        raise AVStoreError("unsafe_recovery_entry")
                    admitted_state_temps.append(path)
                    continue
                if (
                    _TRANSACTION_DIR.fullmatch(path.name) is None
                    or metadata is None
                    or _is_link_or_reparse(path, metadata)
                    or not stat.S_ISDIR(metadata.st_mode)
                ):
                    raise AVStoreError("unsafe_recovery_entry")
                try:
                    value = _decode_object(
                        _read_regular_bytes(path / _TRANSACTION_MARKER, maximum_bytes=4096),
                        code="transaction_invalid",
                    )
                    transaction = AVStoreTransaction(
                        transaction_id=cast(str, value["transaction_id"]),
                        plan_fingerprint=cast(str, value["plan_fingerprint"]),
                        approval_fingerprint=cast(str, value["approval_fingerprint"]),
                        created_at_ms=cast(int, value["created_at_ms"]),
                        expires_at_ms=cast(int, value["expires_at_ms"]),
                        transaction_fingerprint=cast(str, value["transaction_fingerprint"]),
                        schema=cast(str, value["schema"]),
                    )
                except (KeyError, TypeError, AVStoreError) as exc:
                    raise AVStoreError("unsafe_recovery_entry") from exc
                match = _TRANSACTION_DIR.fullmatch(path.name)
                if match is None or match.group(1) != transaction.fingerprint.removeprefix(
                    "sha256:"
                ):
                    raise AVStoreError("unsafe_recovery_entry")
                # Admission includes nested lease shapes so cleanup cannot discover a foreign
                # entry after unrelated receipts or members have already been removed.
                entries = self._bounded_entries(
                    path,
                    maximum=self._policy.max_members_per_transaction + 1,
                )
                for entry in entries:
                    if entry.name == _TRANSACTION_MARKER:
                        continue
                    entry_metadata = _lstat_optional(entry)
                    if (
                        _LEASE_DIR.fullmatch(entry.name) is None
                        or entry_metadata is None
                        or _is_link_or_reparse(entry, entry_metadata)
                        or not stat.S_ISDIR(entry_metadata.st_mode)
                    ):
                        raise AVStoreError("unsafe_recovery_entry")
                    lease_entries = self._bounded_entries(entry, maximum=1)
                    if lease_entries and (
                        len(lease_entries) != 1 or lease_entries[0].name != _LEASE_FILE
                    ):
                        raise AVStoreError("unsafe_recovery_entry")
                    if lease_entries:
                        lease_metadata = _lstat_optional(lease_entries[0])
                        if (
                            lease_metadata is None
                            or lease_metadata.st_nlink != 1
                            or _is_link_or_reparse(lease_entries[0], lease_metadata)
                            or not stat.S_ISREG(lease_metadata.st_mode)
                        ):
                            raise AVStoreError("unsafe_recovery_entry")
                admitted_transactions.append(transaction)
            for path, pattern in (
                *((path, _RECEIPT_FILE) for path in receipt_entries),
                *((path, _MEMBER_FILE) for path in member_entries),
            ):
                metadata = _lstat_optional(path)
                if (
                    pattern.fullmatch(path.name) is None
                    or metadata is None
                    or metadata.st_nlink != 1
                    or _is_link_or_reparse(path, metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                ):
                    raise AVStoreError("unsafe_recovery_entry")

            known_handles: set[str] = set()
            known_transaction_ids: set[str] = set()
            invalid_receipts = 0
            for path in receipt_entries:
                match = _RECEIPT_FILE.fullmatch(path.name)
                if match is None:  # pragma: no cover - admitted above
                    raise AVStoreError("unsafe_recovery_entry")
                try:
                    receipt = self._decode_receipt(
                        _read_regular_bytes(
                            path,
                            maximum_bytes=self._codec.max_receipt_bytes,
                        )
                    )
                    if receipt.transaction_id != match.group(1):
                        raise AVStoreError("receipt_invalid")
                    for output in receipt.outputs:
                        size, content_fingerprint = _hash_regular_file(
                            self._member_path(output.handle),
                            maximum_bytes=self._policy.max_member_bytes,
                        )
                        if (
                            size != output.byte_length
                            or content_fingerprint != output.content_fingerprint
                        ):
                            raise AVStoreError("receipt_invalid")
                except (AVStoreError, ValueError):
                    _safe_unlink(path, missing_ok=False)
                    invalid_receipts += 1
                    continue
                known_handles.update(output.handle for output in receipt.outputs)
                known_transaction_ids.add(receipt.transaction_id)

            orphan_members = 0
            for path in member_entries:
                match = _MEMBER_FILE.fullmatch(path.name)
                if match is None:  # pragma: no cover - admitted above
                    raise AVStoreError("unsafe_recovery_entry")
                if match.group(1) not in known_handles and _lstat_optional(path) is not None:
                    _safe_unlink(path, missing_ok=False)
                    orphan_members += 1

            staging_removed = 0
            for transaction in admitted_transactions:
                if (
                    transaction.expires_at_ms <= now
                    or transaction.transaction_id in known_transaction_ids
                ):
                    self._cleanup_transaction(transaction)
                    staging_removed += 1
            for path in admitted_state_temps:
                _safe_unlink(path, missing_ok=False)

            report = AVStoreRecoveryReport(
                staging_removed=staging_removed,
                orphan_members_removed=orphan_members,
                invalid_receipts_removed=invalid_receipts,
                scanned_entries=len(staging_entries) + len(receipt_entries) + len(member_entries),
            )
            self._persist_enabled(True)
            return report

    @staticmethod
    def _inspection(
        receipt: AVStoreReceipt,
        status: AVStoreInspectionStatus,
    ) -> AVStoreInspection:
        return AVStoreInspection(receipt.transaction_id, status, receipt.fingerprint)

    def inspect(self, expected: AVStoreReceipt) -> AVStoreInspection:
        if type(expected) is not self._codec.receipt_type:
            raise AVStoreError("receipt_type")
        with self._lock:
            if not self._coordination.enabled:
                return self._inspection(expected, AVStoreInspectionStatus.DISABLED)
            try:
                actual = self._load_receipt(expected.transaction_id)
            except AVStoreError as exc:
                status = (
                    AVStoreInspectionStatus.UNSAFE
                    if exc.code == "unsafe_store_entry"
                    else AVStoreInspectionStatus.TAMPERED
                )
                return self._inspection(expected, status)
            if actual is None:
                return self._inspection(expected, AVStoreInspectionStatus.MISSING)
            if actual.fingerprint != expected.fingerprint:
                return self._inspection(expected, AVStoreInspectionStatus.INCOMPATIBLE)
            for output in actual.outputs:
                path = self._member_path(output.handle)
                if _lstat_optional(path) is None:
                    return self._inspection(expected, AVStoreInspectionStatus.MISSING)
                try:
                    size, content_fingerprint = _hash_regular_file(
                        path, maximum_bytes=self._policy.max_member_bytes
                    )
                except AVStoreError as exc:
                    status = (
                        AVStoreInspectionStatus.UNSAFE
                        if exc.code == "unsafe_store_entry"
                        else AVStoreInspectionStatus.TAMPERED
                    )
                    return self._inspection(expected, status)
                if size != output.byte_length or content_fingerprint != output.content_fingerprint:
                    return self._inspection(expected, AVStoreInspectionStatus.TAMPERED)
            return self._inspection(expected, AVStoreInspectionStatus.COMPLETE)

    def read_output(
        self,
        expected: AVStoreReceipt,
        handle: str,
        *,
        maximum_bytes: int,
    ) -> bytes:
        _handle(handle, "output_handle")
        _positive(maximum_bytes, "read_maximum_bytes", self._policy.max_member_bytes)
        if self.inspect(expected).status is not AVStoreInspectionStatus.COMPLETE:
            raise AVStoreError("reconstruction_not_complete")
        output = next((item for item in expected.outputs if item.handle == handle), None)
        if output is None or output.byte_length > maximum_bytes:
            raise AVStoreError("read_limit")
        payload = _read_regular_bytes(self._member_path(handle), maximum_bytes=maximum_bytes)
        if len(payload) != output.byte_length or "sha256:" + hashlib.sha256(
            payload
        ).hexdigest() != (output.content_fingerprint):
            raise AVStoreError("reconstruction_not_complete")
        return payload

    @staticmethod
    def _preview_cancelled(cancellation: object | None) -> bool:
        if cancellation is None:
            return False
        probe = getattr(cancellation, "is_cancelled", None)
        if not callable(probe):
            raise AVStoreError("preview_cancellation")
        try:
            value = probe()
        except Exception as exc:
            raise AVStoreError("preview_cancellation") from exc
        if type(value) is not bool:
            raise AVStoreError("preview_cancellation")
        return value

    @staticmethod
    def _preview_remaining(deadline: float, clock: Callable[[], float]) -> float:
        if type(deadline) is not float or not callable(clock):
            raise AVStoreError("preview_deadline")
        try:
            remaining = deadline - clock()
        except Exception as exc:
            raise AVStoreError("preview_deadline") from exc
        if remaining <= 0:
            raise AVStoreError("preview_deadline")
        return remaining

    def _selected_output(
        self,
        expected: AVStoreReceipt,
        handle: str,
        *,
        maximum_bytes: int,
    ) -> tuple[AVStoreReceipt, AVReceiptOutput, Path]:
        if type(expected) is not self._codec.receipt_type:
            raise AVStoreError("receipt_type")
        _handle(handle, "output_handle")
        _positive(maximum_bytes, "preview_maximum_bytes", 4 * 1024 * 1024 * 1024)
        effective_maximum = min(maximum_bytes, self._policy.max_member_bytes)
        if not self._coordination.enabled:
            raise AVStoreError("reconstruction_not_complete")
        actual = self._load_receipt(expected.transaction_id)
        if actual is None or actual.fingerprint != expected.fingerprint:
            raise AVStoreError("reconstruction_not_complete")
        output = next((item for item in actual.outputs if item.handle == handle), None)
        if output is None or output.byte_length > effective_maximum:
            raise AVStoreError("read_limit")
        return actual, output, self._member_path(handle)

    def _stream_selected_output(
        self,
        expected: AVStoreReceipt,
        handle: str,
        *,
        maximum_bytes: int,
        deadline: float,
        clock: Callable[[], float],
        cancellation: object | None,
        destination: OwnedOutputLease | None,
    ) -> AVSelectedOutputInspection:
        remaining = self._preview_remaining(deadline, clock)
        if not self._lock.acquire(timeout=remaining):
            raise AVStoreError("preview_deadline")
        target: BinaryIO | None = None
        admitted: Path | None = None
        before: os.stat_result | None = None
        try:
            if self._preview_cancelled(cancellation):
                raise AVStoreError("cancelled")
            _, output, path = self._selected_output(
                expected,
                handle,
                maximum_bytes=maximum_bytes,
            )
            with _validated_directories(path.parent):
                try:
                    admitted = validate_regular_file(path, maximum_bytes=maximum_bytes)
                    before = admitted.lstat()
                except (UnsafePathError, OSError) as exc:
                    raise AVStoreError("unsafe_store_entry") from exc
                if before.st_nlink != 1 or _is_link_or_reparse(admitted, before):
                    raise AVStoreError("unsafe_store_entry")
                if destination is not None:
                    try:
                        destination.validate_for_spawn()
                        # SECURITY: retain Path-bound opens after the exact lease/path validation.
                        target = destination.path.open("xb", buffering=0)
                    except (OSError, MediaProcessError) as exc:
                        raise AVStoreError("preview_copy_unavailable") from exc
                digest = hashlib.sha256()
                total = 0
                buffer = bytearray(1024 * 1024)
                try:
                    with admitted.open("rb", buffering=0) as source:
                        opened = os.fstat(source.fileno())
                        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(
                            before
                        ):
                            raise AVStoreError("unsafe_store_entry")
                        while True:
                            self._preview_remaining(deadline, clock)
                            if self._preview_cancelled(cancellation):
                                raise AVStoreError("cancelled")
                            count = source.readinto(buffer)
                            if count is None:
                                raise AVStoreError("store_entry_unavailable")
                            if count == 0:
                                break
                            total += count
                            if total > maximum_bytes:
                                raise AVStoreError("member_too_large")
                            view = memoryview(buffer)[:count]
                            digest.update(view)
                            if target is not None:
                                written = 0
                                while written < count:
                                    self._preview_remaining(deadline, clock)
                                    if self._preview_cancelled(cancellation):
                                        raise AVStoreError("cancelled")
                                    amount = target.write(view[written:])
                                    if amount is None or amount <= 0:
                                        raise AVStoreError("preview_copy_unavailable")
                                    written += amount
                        after_handle = os.fstat(source.fileno())
                    after_path = admitted.lstat()
                    if (
                        after_path.st_nlink != 1
                        or _is_link_or_reparse(admitted, after_path)
                        or _identity(after_handle) != _identity(opened)
                        or _identity(after_path) != _identity(opened)
                    ):
                        raise AVStoreError("unsafe_store_entry")
                except OSError as exc:
                    raise AVStoreError("store_entry_unavailable") from exc
                finally:
                    if target is not None:
                        target.close()
                        target = None
            fingerprint = "sha256:" + digest.hexdigest()
            if total != output.byte_length or fingerprint != output.content_fingerprint:
                raise AVStoreError("reconstruction_not_complete")
            return AVSelectedOutputInspection(handle, total, fingerprint)
        except Exception:
            if target is not None:
                target.close()
            if destination is not None:
                try:
                    destination.release()
                except Exception as exc:
                    raise AVStoreError("cleanup_failed") from exc
            raise
        finally:
            self._lock.release()

    def verify_selected_output(
        self,
        expected: AVStoreReceipt,
        handle: str,
        *,
        maximum_bytes: int,
        deadline: float,
        cancellation: object | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> AVSelectedOutputInspection:
        """Hash only one exact persisted member under the caller's absolute deadline."""

        return self._stream_selected_output(
            expected,
            handle,
            maximum_bytes=maximum_bytes,
            deadline=deadline,
            clock=clock,
            cancellation=cancellation,
            destination=None,
        )

    def copy_selected_output(
        self,
        expected: AVStoreReceipt,
        handle: str,
        destination: OwnedOutputLease,
        *,
        maximum_bytes: int,
        deadline: float,
        cancellation: object | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> AVSelectedOutputInspection:
        """Copy only one exact member into an exclusive caller-owned lease."""

        if type(destination) is not OwnedOutputLease:
            raise AVStoreError("preview_copy_unavailable")
        return self._stream_selected_output(
            expected,
            handle,
            maximum_bytes=maximum_bytes,
            deadline=deadline,
            clock=clock,
            cancellation=cancellation,
            destination=destination,
        )


__all__ = [
    "AV_RECONSTRUCTION_STORE_SCHEMA",
    "AV_RECONSTRUCTION_STORE_STATE_SCHEMA",
    "AV_RECONSTRUCTION_TRANSACTION_SCHEMA",
    "AVStoreError",
    "AVStoreInspection",
    "AVStoreInspectionStatus",
    "AVSelectedOutputInspection",
    "AVStorePolicy",
    "AVStoreReceipt",
    "AVStoreReceiptCodec",
    "AVStoreRecoveryReport",
    "AVStoreTransaction",
    "PrivateAVReconstructionStore",
]
