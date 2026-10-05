"""Private durable checkpoint store for joint AV latent artifacts (M20-04).

One bounded private store rooted only at an explicit caller-owned directory, mirroring the
accepted ``PrivateSegmentArtifactStore`` safety model for the M20-04 checkpoint family: same
same-root policy/lock/quota coordination, same atomic no-clobber publication, same bounded
aggregate-before-mutation recovery, same Windows junction/reparse/TOCTOU defenses -- imported
from ``segment_artifact_store`` rather than reimplemented, per the accepted
``av_reconstruction_store`` precedent.  Error surface deliberately stays
:class:`ArtifactStoreError`: this store is an artifact store composed from the same admitted
mechanics, and a second error vocabulary for identical failures would only cost callers a
translation table.

What is latent-specific is exact extent truth: a checkpoint payload is the descriptor's two raw
dense domains concatenated video-then-audio, so ``commit`` refuses any payload whose byte count
is not exactly ``descriptor.payload_byte_length``, before hashing and before quota.  A payload
that does not match its descriptor is not an oversized write, it is a wrong object.

Filesystem locators never enter receipts, projections, errors or logs.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.canonical import canonical_bytes
from ..core.joint_av_latent import (
    MAX_JOINT_AV_LATENT_RECEIPT_BYTES,
    JointAVLatentError,
    JointAVLatentReceipt,
    complete_joint_av_latent_receipt,
    decode_joint_av_latent_receipt,
    fail_joint_av_latent_receipt,
)
from ..core.safe_paths import (
    UnsafePathError,
    ensure_directory,
    validate_directory,
    validate_regular_file,
)
from ..core.segment_artifacts import ArtifactLifecycleState
from .segment_artifact_store import (
    ArtifactInspection,
    ArtifactInspectionStatus,
    ArtifactRecoveryReport,
    ArtifactStoreDispositionReport,
    ArtifactStoreError,
    ArtifactStoreMigrationReport,
    ArtifactStorePolicy,
    _copy_regular_file_into_new,
    _decode_json_object,
    _directory_identity,
    _identity,
    _is_link_or_reparse,
    _is_windows_path_race_error,
    _lstat_optional,
    _read_regular_bytes,
    _require_identifier,
    _safe_unlink,
    _safe_unlink_pinned,
    _validated_directories,
    _windows_delete_open_file,
    _windows_move_new_file,
    _write_new_file,
)

JOINT_AV_LATENT_STORE_SCHEMA = "h3.context.joint_av_latent_store.v1"
JOINT_AV_LATENT_STORE_STATE_SCHEMA = "h3.context.joint_av_latent_store_state.v1"

_MARKER_NAME = ".h3-joint-av-latent-store-v1"
_STATE_NAME = "store-state.json"
_CHILD_NAMES = ("latents", "receipts", "staging")
_LATENT_FILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\.bin\Z")
_RECEIPT_FILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\.json\Z")
_PARTIAL_FILE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\.partial\.json\Z")
_TEMP_FILE = re.compile(r"[A-Za-z0-9_.:-]{1,220}\.tmp\Z")


@dataclass(slots=True)
class _RootCoordination:
    policy: ArtifactStorePolicy
    lock: threading.RLock
    write_slots: threading.BoundedSemaphore
    enabled: bool = field(default=True)


#: Keyed by normalized absolute root; the latent family shares nothing with the segment or AV
#: coordinators on purpose -- a root is one store kind, and a policy agreement in one family
#: must never satisfy a different family's marker.
_COORDINATORS: dict[str, _RootCoordination] = {}
_COORDINATORS_GUARD = threading.Lock()


class PrivateJointAVLatentStore:
    """One bounded private latent checkpoint store at an explicit caller-owned root."""

    def __init__(
        self,
        root: Path,
        *,
        policy: ArtifactStorePolicy | None = None,
        clock_ms: Callable[[], int],
    ) -> None:
        if not isinstance(root, Path):
            raise ArtifactStoreError("unsafe_store_root")
        self._policy = ArtifactStorePolicy() if policy is None else policy
        if type(self._policy) is not ArtifactStorePolicy or not callable(clock_ms):
            raise ArtifactStoreError("store_configuration")
        self._clock_ms = clock_ms
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
                    maximum=len(_CHILD_NAMES) + 2,
                )
        except (UnsafePathError, OSError, ArtifactStoreError) as exc:
            raise ArtifactStoreError("unsafe_store_root") from exc
        self._root = admitted_root
        self._marker = self._root / _MARKER_NAME
        self._state = self._root / _STATE_NAME
        marker_metadata = _lstat_optional(self._marker)
        if marker_metadata is None:
            if initial_entries:
                raise ArtifactStoreError("foreign_store_root")
            self._atomic_publish_new(
                self._marker,
                self._marker_payload(),
                parent=self._root,
            )
        self._validate_marker()
        allowed_root_names = {_MARKER_NAME, _STATE_NAME, *_CHILD_NAMES}
        try:
            if any(entry.name not in allowed_root_names for entry in self._root.iterdir()):
                raise ArtifactStoreError("foreign_store_root")
            for name in _CHILD_NAMES:
                ensure_directory(self._root / name)
        except (UnsafePathError, OSError) as exc:
            raise ArtifactStoreError("unsafe_store_root") from exc
        self._latents = self._root / "latents"
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
                "schema": JOINT_AV_LATENT_STORE_SCHEMA,
                "policy": self._policy_wire(),
            }
        )

    def _validate_marker(self) -> None:
        payload = _read_regular_bytes(self._marker, maximum_bytes=1_024)
        value = _decode_json_object(payload, error_code="unsupported_store_schema")
        if value != {
            "schema": JOINT_AV_LATENT_STORE_SCHEMA,
            "policy": self._policy_wire(),
        }:
            raise ArtifactStoreError("unsupported_store_schema")

    @staticmethod
    def _state_payload(*, enabled: bool) -> bytes:
        return canonical_bytes(
            {
                "schema": JOINT_AV_LATENT_STORE_STATE_SCHEMA,
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
            or value["schema"] != JOINT_AV_LATENT_STORE_STATE_SCHEMA
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

    # The publication and scan primitives below carry the accepted
    # ``PrivateSegmentArtifactStore`` implementations verbatim; they are methods there, not
    # module functions, so the composition boundary is this copy rather than an import.

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

    def _latent_path(self, artifact_id: str) -> Path:
        return self._latents / f"{_require_identifier(artifact_id, 'artifact_id')}.bin"

    def _receipt_path(self, artifact_id: str) -> Path:
        return self._receipts / f"{_require_identifier(artifact_id, 'artifact_id')}.json"

    def _partial_path(self, artifact_id: str) -> Path:
        return self._staging / f"{_require_identifier(artifact_id, 'artifact_id')}.partial.json"

    def _require_enabled(self) -> None:
        if not self._coordination.enabled:
            raise ArtifactStoreError("store_disabled")

    def begin(self, receipt: JointAVLatentReceipt) -> JointAVLatentReceipt:
        if type(receipt) is not JointAVLatentReceipt:
            raise ArtifactStoreError("receipt_type")
        if receipt.state is not ArtifactLifecycleState.PARTIAL:
            raise ArtifactStoreError("receipt_not_partial")
        if receipt.descriptor.payload_byte_length > self._policy.max_artifact_bytes:
            raise ArtifactStoreError("artifact_too_large")
        with self._lock:
            self._require_enabled()
            now = self._now_ms()
            if receipt.created_at_ms > now:
                raise ArtifactStoreError("receipt_created_in_future")
            if receipt.expires_at_ms > now + self._policy.ttl_seconds * 1_000:
                raise ArtifactStoreError("receipt_ttl_exceeds_policy")
            if receipt.expires_at_ms <= now:
                raise ArtifactStoreError("receipt_expired")
            paths = (
                self._partial_path(receipt.artifact_id),
                self._receipt_path(receipt.artifact_id),
                self._latent_path(receipt.artifact_id),
            )
            if any(_lstat_optional(path) is not None for path in paths):
                raise ArtifactStoreError("duplicate_artifact")
            lifecycle_count, _ = self._inventory()
            if lifecycle_count >= self._policy.max_entries:
                raise ArtifactStoreError("entry_quota_exceeded")
            self._atomic_publish_new(
                paths[0],
                receipt.to_wire_bytes(),
                parent=self._staging,
            )
            return receipt

    def _load_partial(self, expected: JointAVLatentReceipt) -> JointAVLatentReceipt:
        try:
            stored = decode_joint_av_latent_receipt(
                _read_regular_bytes(
                    self._partial_path(expected.artifact_id),
                    maximum_bytes=MAX_JOINT_AV_LATENT_RECEIPT_BYTES,
                )
            )
        except (ArtifactStoreError, JointAVLatentError) as exc:
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
                receipt = decode_joint_av_latent_receipt(
                    _read_regular_bytes(
                        path,
                        maximum_bytes=MAX_JOINT_AV_LATENT_RECEIPT_BYTES,
                    )
                )
            except (ArtifactStoreError, JointAVLatentError) as exc:
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

    def commit(self, receipt: JointAVLatentReceipt, payload: bytes) -> JointAVLatentReceipt:
        if type(receipt) is not JointAVLatentReceipt:
            raise ArtifactStoreError("receipt_type")
        if type(payload) is not bytes or not payload:
            raise ArtifactStoreError("artifact_payload")
        # Latent-specific admission: the payload IS the descriptor's two raw domains, so any
        # other byte count is a wrong object, refused before hashing and before quota.
        if len(payload) != receipt.descriptor.payload_byte_length:
            raise ArtifactStoreError("payload_extent_mismatch")
        if len(payload) > self._policy.max_artifact_bytes:
            raise ArtifactStoreError("artifact_too_large")
        if not self._write_slots.acquire(blocking=False):
            raise ArtifactStoreError("write_concurrency_limit")
        try:
            with self._lock:
                self._require_enabled()
                partial = self._load_partial(receipt)
                if partial.expires_at_ms <= self._now_ms():
                    raise ArtifactStoreError("receipt_expired")
                _, total = self._inventory()
                if total + len(payload) > self._policy.max_total_bytes:
                    raise ArtifactStoreError("byte_quota_exceeded")
                output_fingerprint = "sha256:" + hashlib.sha256(payload).hexdigest()
                try:
                    completed = complete_joint_av_latent_receipt(
                        partial,
                        output_fingerprint=output_fingerprint,
                        byte_length=len(payload),
                    )
                except JointAVLatentError as exc:
                    raise ArtifactStoreError("payload_extent_mismatch") from exc
                latent_path = self._latent_path(receipt.artifact_id)
                receipt_path = self._receipt_path(receipt.artifact_id)
                self._atomic_publish_new(latent_path, payload, parent=self._staging)
                try:
                    self._atomic_publish_new(
                        receipt_path,
                        completed.to_wire_bytes(),
                        parent=self._staging,
                    )
                except ArtifactStoreError:
                    _safe_unlink(latent_path)
                    raise
                _safe_unlink(self._partial_path(receipt.artifact_id), missing_ok=False)
                return completed
        finally:
            self._write_slots.release()

    def fail(
        self,
        receipt: JointAVLatentReceipt,
        *,
        failure_code: str,
    ) -> JointAVLatentReceipt:
        with self._lock:
            self._require_enabled()
            partial = self._load_partial(receipt)
            failed = fail_joint_av_latent_receipt(partial, failure_code=failure_code)
            self._atomic_publish_new(
                self._receipt_path(receipt.artifact_id),
                failed.to_wire_bytes(),
                parent=self._staging,
            )
            _safe_unlink(self._partial_path(receipt.artifact_id), missing_ok=False)
            return failed

    @staticmethod
    def _inspection(
        receipt: JointAVLatentReceipt,
        status: ArtifactInspectionStatus,
    ) -> ArtifactInspection:
        return ArtifactInspection(receipt.artifact_id, status, receipt.fingerprint)

    def inspect(self, expected: JointAVLatentReceipt) -> ArtifactInspection:
        if type(expected) is not JointAVLatentReceipt:
            raise ArtifactStoreError("receipt_type")
        with self._lock:
            if not self._coordination.enabled:
                return self._inspection(expected, ArtifactInspectionStatus.DISABLED)
            if expected.state is not ArtifactLifecycleState.COMPLETE:
                return self._inspection(expected, ArtifactInspectionStatus.NOT_COMPLETE)
            receipt_path = self._receipt_path(expected.artifact_id)
            if _lstat_optional(receipt_path) is None:
                return self._inspection(expected, ArtifactInspectionStatus.MISSING)
            try:
                actual = decode_joint_av_latent_receipt(
                    _read_regular_bytes(
                        receipt_path,
                        maximum_bytes=MAX_JOINT_AV_LATENT_RECEIPT_BYTES,
                    )
                )
            except ArtifactStoreError as exc:
                status = (
                    ArtifactInspectionStatus.UNSAFE
                    if exc.code == "unsafe_store_entry"
                    else ArtifactInspectionStatus.TAMPERED
                )
                return self._inspection(expected, status)
            except JointAVLatentError:
                return self._inspection(expected, ArtifactInspectionStatus.TAMPERED)
            if actual.fingerprint != expected.fingerprint:
                return self._inspection(expected, ArtifactInspectionStatus.INCOMPATIBLE)
            if actual.expires_at_ms <= self._now_ms():
                return self._inspection(expected, ArtifactInspectionStatus.EXPIRED)
            latent_path = self._latent_path(expected.artifact_id)
            if _lstat_optional(latent_path) is None:
                return self._inspection(expected, ArtifactInspectionStatus.MISSING)
            try:
                payload = _read_regular_bytes(
                    latent_path,
                    maximum_bytes=self._policy.max_artifact_bytes,
                )
            except ArtifactStoreError as exc:
                status = (
                    ArtifactInspectionStatus.UNSAFE
                    if exc.code == "unsafe_store_entry"
                    else ArtifactInspectionStatus.TAMPERED
                )
                return self._inspection(expected, status)
            fingerprint = "sha256:" + hashlib.sha256(payload).hexdigest()
            if (
                len(payload) != actual.byte_length
                or len(payload) != actual.descriptor.payload_byte_length
                or fingerprint != actual.output_fingerprint
            ):
                return self._inspection(expected, ArtifactInspectionStatus.TAMPERED)
            return self._inspection(expected, ArtifactInspectionStatus.REUSABLE)

    def read(self, expected: JointAVLatentReceipt) -> bytes:
        inspection = self.inspect(expected)
        if inspection.status is not ArtifactInspectionStatus.REUSABLE:
            raise ArtifactStoreError("artifact_not_reusable")
        payload = _read_regular_bytes(
            self._latent_path(expected.artifact_id),
            maximum_bytes=self._policy.max_artifact_bytes,
        )
        if (
            len(payload) != expected.byte_length
            or "sha256:" + hashlib.sha256(payload).hexdigest() != expected.output_fingerprint
        ):
            raise ArtifactStoreError("artifact_not_reusable")
        return payload

    def read_domains(self, expected: JointAVLatentReceipt) -> tuple[bytes, bytes]:
        """Return the exact (video, audio) domain byte runs of one reusable checkpoint.

        The split point is descriptor arithmetic, not parsing: the payload has no header, so
        the descriptor that survived receipt validation is the only split authority.
        """

        payload = self.read(expected)
        split = expected.descriptor.video.byte_length
        return payload[:split], payload[split:]

    def stream_latent_into(
        self,
        expected: JointAVLatentReceipt,
        destination: Path,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[int, str]:
        """Stream one exact reusable checkpoint into a caller-owned new file.

        The M20-08 bounded-chunk primitive: the payload never exists in memory beyond one
        chunk, the destination is created exclusively, and a length or fingerprint divergence
        removes the partial copy and fails closed.
        """

        if not isinstance(destination, Path) or not destination.is_absolute():
            raise ArtifactStoreError("store_configuration")
        inspection = self.inspect(expected)
        if inspection.status is not ArtifactInspectionStatus.REUSABLE:
            raise ArtifactStoreError("artifact_not_reusable")
        length, fingerprint = _copy_regular_file_into_new(
            self._latent_path(expected.artifact_id),
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
            latent_entries = self._bounded_entries(self._latents, maximum=remaining)
            for path in staging_entries:
                if (
                    _PARTIAL_FILE.fullmatch(path.name) is None
                    and _TEMP_FILE.fullmatch(path.name) is None
                ):
                    raise ArtifactStoreError("unsafe_recovery_entry")
            for path in receipt_entries:
                if _RECEIPT_FILE.fullmatch(path.name) is None:
                    raise ArtifactStoreError("unsafe_recovery_entry")
            for path in latent_entries:
                if _LATENT_FILE.fullmatch(path.name) is None:
                    raise ArtifactStoreError("unsafe_recovery_entry")
            for path in staging_entries:
                scanned += 1
                if (
                    _PARTIAL_FILE.fullmatch(path.name) is None
                    and _TEMP_FILE.fullmatch(path.name) is None
                ):  # pragma: no cover - admitted by the aggregate pre-scan above
                    raise ArtifactStoreError("unsafe_recovery_entry")
                _safe_unlink(path, missing_ok=False)
                staging += 1

            known_receipts: set[str] = set()
            for path in receipt_entries:
                scanned += 1
                match = _RECEIPT_FILE.fullmatch(path.name)
                if match is None:  # pragma: no cover - admitted above
                    raise ArtifactStoreError("unsafe_recovery_entry")
                artifact_id = match.group(1)
                try:
                    receipt = decode_joint_av_latent_receipt(
                        _read_regular_bytes(
                            path,
                            maximum_bytes=MAX_JOINT_AV_LATENT_RECEIPT_BYTES,
                        )
                    )
                except (ArtifactStoreError, JointAVLatentError):
                    _safe_unlink(path, missing_ok=False)
                    latent_path = self._latent_path(artifact_id)
                    if _lstat_optional(latent_path) is not None:
                        _safe_unlink(latent_path, missing_ok=False)
                    invalid += 1
                    continue
                if receipt.artifact_id != artifact_id:
                    _safe_unlink(path, missing_ok=False)
                    invalid += 1
                    continue
                if receipt.expires_at_ms <= now:
                    _safe_unlink(path, missing_ok=False)
                    latent_path = self._latent_path(artifact_id)
                    if _lstat_optional(latent_path) is not None:
                        _safe_unlink(latent_path, missing_ok=False)
                    expired += 1
                    continue
                if receipt.state is ArtifactLifecycleState.COMPLETE:
                    latent_path = self._latent_path(artifact_id)
                    if _lstat_optional(latent_path) is None:
                        _safe_unlink(path, missing_ok=False)
                        orphans += 1
                        continue
                    inspection = self.inspect(receipt)
                    if inspection.status is not ArtifactInspectionStatus.REUSABLE:
                        _safe_unlink(path, missing_ok=False)
                        _safe_unlink(latent_path, missing_ok=False)
                        invalid += 1
                        continue
                known_receipts.add(artifact_id)

            for path in latent_entries:
                scanned += 1
                match = _LATENT_FILE.fullmatch(path.name)
                if match is None:  # pragma: no cover - admitted above
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
                (self._latents, (_LATENT_FILE,)),
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
                from_schema=JOINT_AV_LATENT_STORE_SCHEMA,
                to_schema=JOINT_AV_LATENT_STORE_SCHEMA,
            )


__all__ = [
    "JOINT_AV_LATENT_STORE_SCHEMA",
    "JOINT_AV_LATENT_STORE_STATE_SCHEMA",
    "PrivateJointAVLatentStore",
]
