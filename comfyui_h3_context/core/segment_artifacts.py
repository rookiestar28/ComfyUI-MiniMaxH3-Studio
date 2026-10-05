"""Content-free identity for private generated segment artifacts.

This pure module owns receipt truth only. Filesystem paths, payload bytes, cleanup and host storage
remain behind an explicit adapter boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum

from .canonical import canonical_bytes, canonical_fingerprint
from .pipeline_transaction import PipelineTransaction, PipelineTransactionState
from .segment_workspace import SegmentContextManifest

SEGMENT_ARTIFACT_RECEIPT_SCHEMA = "h3.context.segment_artifact_receipt.v1"
SEGMENT_ARTIFACT_PUBLIC_SCHEMA = "h3.context.segment_artifact_public.v1"
MAX_SEGMENT_ARTIFACT_RECEIPT_BYTES = 32_768
MAX_SEGMENT_ARTIFACT_DIMENSIONS = 8
MAX_SEGMENT_ARTIFACT_DIMENSION = 1_000_000
MAX_SEGMENT_ARTIFACT_TTL_MILLISECONDS = 30 * 24 * 60 * 60 * 1_000
MAX_SEGMENT_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SegmentArtifactError(ValueError):
    """Raised when a segment artifact receipt cannot preserve exact identity."""


class ArtifactKind(str, Enum):
    SEGMENT_OUTPUT = "segment_output"


class ArtifactLifecycleState(str, Enum):
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise SegmentArtifactError(f"bounded_identifier:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise SegmentArtifactError(f"sha256_fingerprint:{field}")
    return value


def _optional_fingerprint(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, field)


def _positive_integer(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise SegmentArtifactError(f"positive_integer:{field}")
    return value


def redact_segment_artifact_fingerprint(value: str | None, *, domain: str) -> str | None:
    """Return a bounded, domain-separated token without exposing a private fingerprint."""

    if value is None:
        return None
    _fingerprint(value, domain)
    _identifier(domain, "redaction_domain")
    digest = hashlib.sha256(f"{domain}:{value}".encode("ascii")).hexdigest()
    return f"redacted:{digest[:16]}"


@dataclass(frozen=True, slots=True)
class SegmentArtifactReceipt:
    """One immutable locator-free artifact lifecycle receipt."""

    artifact_id: str
    state: ArtifactLifecycleState
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    segment_id: str
    manifest_fingerprint: str
    producer_fingerprint: str
    transaction_fingerprint: str
    graph_fingerprint: str
    native_binding_fingerprint: str
    model_fingerprint: str
    runtime_fingerprint: str
    settings_fingerprint: str
    source_id: str
    predecessor_artifact_fingerprint: str | None
    execution_fingerprint: str
    format_label: str
    shape: tuple[int, ...]
    created_at_ms: int
    expires_at_ms: int
    output_fingerprint: str | None = None
    byte_length: int = 0
    failure_code: str | None = None
    receipt_fingerprint: str | None = None
    artifact_kind: ArtifactKind = ArtifactKind.SEGMENT_OUTPUT
    schema: str = SEGMENT_ARTIFACT_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SEGMENT_ARTIFACT_RECEIPT_SCHEMA:
            raise SegmentArtifactError("unsupported_receipt_schema")
        if type(self.artifact_kind) is not ArtifactKind:
            raise SegmentArtifactError("artifact_kind")
        if self.artifact_kind is not ArtifactKind.SEGMENT_OUTPUT:
            raise SegmentArtifactError("unsupported_artifact_kind")
        if type(self.state) is not ArtifactLifecycleState:
            raise SegmentArtifactError("artifact_state")
        _identifier(self.artifact_id, "artifact_id")
        _identifier(self.workspace_id, "workspace_id")
        _positive_integer(self.workspace_revision, "workspace_revision", 1_000_000)
        _fingerprint(self.workspace_fingerprint, "workspace")
        _identifier(self.segment_id, "segment_id")
        _fingerprint(self.manifest_fingerprint, "manifest")
        _fingerprint(self.producer_fingerprint, "producer")
        _fingerprint(self.transaction_fingerprint, "transaction")
        _fingerprint(self.graph_fingerprint, "graph")
        _fingerprint(self.native_binding_fingerprint, "native_binding")
        _fingerprint(self.model_fingerprint, "model")
        _fingerprint(self.runtime_fingerprint, "runtime")
        _fingerprint(self.settings_fingerprint, "settings")
        _identifier(self.source_id, "source_id")
        _optional_fingerprint(
            self.predecessor_artifact_fingerprint,
            "predecessor_artifact",
        )
        _fingerprint(self.execution_fingerprint, "execution")
        _identifier(self.format_label, "format_label")
        if (
            type(self.shape) is not tuple
            or not 1 <= len(self.shape) <= MAX_SEGMENT_ARTIFACT_DIMENSIONS
        ):
            raise SegmentArtifactError("artifact_shape")
        for index, dimension in enumerate(self.shape):
            _positive_integer(
                dimension,
                f"shape[{index}]",
                MAX_SEGMENT_ARTIFACT_DIMENSION,
            )
        _positive_integer(self.created_at_ms, "created_at_ms", 9_999_999_999_999)
        _positive_integer(self.expires_at_ms, "expires_at_ms", 9_999_999_999_999)
        if not 0 < self.expires_at_ms - self.created_at_ms <= MAX_SEGMENT_ARTIFACT_TTL_MILLISECONDS:
            raise SegmentArtifactError("artifact_ttl")
        if (
            type(self.byte_length) is not int
            or not 0 <= self.byte_length <= MAX_SEGMENT_ARTIFACT_BYTES
        ):
            raise SegmentArtifactError("artifact_byte_length")
        _optional_fingerprint(self.output_fingerprint, "output")
        if self.failure_code is not None:
            _identifier(self.failure_code, "failure_code")
        self._validate_lifecycle_shape()
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.receipt_fingerprint is None:
            object.__setattr__(self, "receipt_fingerprint", expected)
        elif self.receipt_fingerprint != expected:
            raise SegmentArtifactError("receipt_fingerprint_mismatch")
        if len(self.to_wire_bytes()) > MAX_SEGMENT_ARTIFACT_RECEIPT_BYTES:
            raise SegmentArtifactError("receipt_wire_limit")

    def _validate_lifecycle_shape(self) -> None:
        if self.state is ArtifactLifecycleState.COMPLETE:
            if (
                self.output_fingerprint is None
                or self.byte_length <= 0
                or self.failure_code is not None
            ):
                raise SegmentArtifactError("complete_receipt_shape")
        elif self.state is ArtifactLifecycleState.PARTIAL:
            if (
                self.output_fingerprint is not None
                or self.byte_length != 0
                or self.failure_code is not None
            ):
                raise SegmentArtifactError("partial_receipt_shape")
        elif (
            self.output_fingerprint is not None
            or self.byte_length != 0
            or self.failure_code is None
        ):
            raise SegmentArtifactError("failed_receipt_shape")

    @property
    def fingerprint(self) -> str:
        if self.receipt_fingerprint is None:  # pragma: no cover - initialized above
            raise SegmentArtifactError("receipt_fingerprint_uninitialized")
        return self.receipt_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "artifact_kind": self.artifact_kind.value,
            "artifact_id": self.artifact_id,
            "state": self.state.value,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "segment_id": self.segment_id,
            "manifest_fingerprint": self.manifest_fingerprint,
            "producer_fingerprint": self.producer_fingerprint,
            "transaction_fingerprint": self.transaction_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "native_binding_fingerprint": self.native_binding_fingerprint,
            "model_fingerprint": self.model_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "settings_fingerprint": self.settings_fingerprint,
            "source_id": self.source_id,
            "predecessor_artifact_fingerprint": self.predecessor_artifact_fingerprint,
            "execution_fingerprint": self.execution_fingerprint,
            "format_label": self.format_label,
            "shape": list(self.shape),
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "output_fingerprint": self.output_fingerprint,
            "byte_length": self.byte_length,
            "failure_code": self.failure_code,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["receipt_fingerprint"] = self.fingerprint
        return value

    def to_wire_bytes(self) -> bytes:
        return canonical_bytes(self.to_wire())

    def to_public_dict(self) -> dict[str, object]:
        """Return an explicit locator-free allowlist with redacted opaque tokens."""

        return {
            "schema": SEGMENT_ARTIFACT_PUBLIC_SCHEMA,
            "artifact_kind": self.artifact_kind.value,
            "artifact_id": self.artifact_id,
            "state": self.state.value,
            "format_label": self.format_label,
            "shape": list(self.shape),
            "byte_length": self.byte_length,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "receipt_token": redact_segment_artifact_fingerprint(
                self.fingerprint,
                domain="receipt",
            ),
            "output_token": redact_segment_artifact_fingerprint(
                self.output_fingerprint,
                domain="output",
            ),
        }


def begin_segment_artifact_receipt(
    *,
    manifest: SegmentContextManifest,
    transaction: PipelineTransaction,
    artifact_id: str,
    model_fingerprint: str,
    runtime_fingerprint: str,
    execution_fingerprint: str,
    predecessor_artifact_fingerprint: str | None,
    format_label: str,
    shape: tuple[int, ...],
    created_at_ms: int,
    expires_at_ms: int,
) -> SegmentArtifactReceipt:
    """Bind one running canonical transaction to one partial segment artifact."""

    if type(manifest) is not SegmentContextManifest:
        raise SegmentArtifactError("manifest_type")
    if type(transaction) is not PipelineTransaction:
        raise SegmentArtifactError("transaction_type")
    if transaction.state is not PipelineTransactionState.RUNNING:
        raise SegmentArtifactError("transaction_not_running")
    if (
        manifest.workspace_id != transaction.workspace_id
        or manifest.workspace_revision != transaction.workspace_revision
        or manifest.workspace_fingerprint != transaction.workspace_fingerprint
        or manifest.fingerprint not in transaction.manifest_fingerprints
        or manifest.segment_id not in transaction.dirty_segment_ids
        or transaction.graph_fingerprint is None
    ):
        raise SegmentArtifactError("transaction_manifest_authority_mismatch")
    requires_predecessor = bool(manifest.dependency_segment_ids)
    if requires_predecessor != (predecessor_artifact_fingerprint is not None):
        raise SegmentArtifactError("predecessor_artifact_identity")
    return SegmentArtifactReceipt(
        artifact_id=artifact_id,
        state=ArtifactLifecycleState.PARTIAL,
        workspace_id=manifest.workspace_id,
        workspace_revision=manifest.workspace_revision,
        workspace_fingerprint=manifest.workspace_fingerprint,
        segment_id=manifest.segment_id,
        manifest_fingerprint=manifest.fingerprint,
        producer_fingerprint=manifest.producer_fingerprint,
        transaction_fingerprint=transaction.fingerprint,
        graph_fingerprint=transaction.graph_fingerprint,
        native_binding_fingerprint=manifest.native_binding_fingerprint,
        model_fingerprint=model_fingerprint,
        runtime_fingerprint=runtime_fingerprint,
        settings_fingerprint=manifest.producer_settings_fingerprint,
        source_id=manifest.source_id,
        predecessor_artifact_fingerprint=predecessor_artifact_fingerprint,
        execution_fingerprint=execution_fingerprint,
        format_label=format_label,
        shape=shape,
        created_at_ms=created_at_ms,
        expires_at_ms=expires_at_ms,
    )


def complete_segment_artifact_receipt(
    receipt: SegmentArtifactReceipt,
    *,
    output_fingerprint: str,
    byte_length: int,
) -> SegmentArtifactReceipt:
    if type(receipt) is not SegmentArtifactReceipt:
        raise SegmentArtifactError("receipt_type")
    if receipt.state is not ArtifactLifecycleState.PARTIAL:
        raise SegmentArtifactError("receipt_not_partial")
    return replace(
        receipt,
        state=ArtifactLifecycleState.COMPLETE,
        output_fingerprint=output_fingerprint,
        byte_length=byte_length,
        receipt_fingerprint=None,
    )


def fail_segment_artifact_receipt(
    receipt: SegmentArtifactReceipt,
    *,
    failure_code: str,
) -> SegmentArtifactReceipt:
    if type(receipt) is not SegmentArtifactReceipt:
        raise SegmentArtifactError("receipt_type")
    if receipt.state is not ArtifactLifecycleState.PARTIAL:
        raise SegmentArtifactError("receipt_not_partial")
    _identifier(failure_code, "failure_code")
    return replace(
        receipt,
        state=ArtifactLifecycleState.FAILED,
        failure_code=failure_code,
        receipt_fingerprint=None,
    )


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SegmentArtifactError("duplicate_receipt_member")
        result[key] = value
    return result


def decode_segment_artifact_receipt(payload: bytes) -> SegmentArtifactReceipt:
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_SEGMENT_ARTIFACT_RECEIPT_BYTES:
        raise SegmentArtifactError("receipt_wire_limit")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_members,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SegmentArtifactError("invalid_receipt_json") from exc
    if type(value) is not dict:
        raise SegmentArtifactError("receipt_wire_type")
    expected_keys = {
        "schema",
        "artifact_kind",
        "artifact_id",
        "state",
        "workspace_id",
        "workspace_revision",
        "workspace_fingerprint",
        "segment_id",
        "manifest_fingerprint",
        "producer_fingerprint",
        "transaction_fingerprint",
        "graph_fingerprint",
        "native_binding_fingerprint",
        "model_fingerprint",
        "runtime_fingerprint",
        "settings_fingerprint",
        "source_id",
        "predecessor_artifact_fingerprint",
        "execution_fingerprint",
        "format_label",
        "shape",
        "created_at_ms",
        "expires_at_ms",
        "output_fingerprint",
        "byte_length",
        "failure_code",
        "receipt_fingerprint",
    }
    if set(value) != expected_keys:
        raise SegmentArtifactError("receipt_wire_members")
    shape = value["shape"]
    if type(shape) is not list:
        raise SegmentArtifactError("artifact_shape")
    try:
        return SegmentArtifactReceipt(
            schema=value["schema"],
            artifact_kind=ArtifactKind(value["artifact_kind"]),
            artifact_id=value["artifact_id"],
            state=ArtifactLifecycleState(value["state"]),
            workspace_id=value["workspace_id"],
            workspace_revision=value["workspace_revision"],
            workspace_fingerprint=value["workspace_fingerprint"],
            segment_id=value["segment_id"],
            manifest_fingerprint=value["manifest_fingerprint"],
            producer_fingerprint=value["producer_fingerprint"],
            transaction_fingerprint=value["transaction_fingerprint"],
            graph_fingerprint=value["graph_fingerprint"],
            native_binding_fingerprint=value["native_binding_fingerprint"],
            model_fingerprint=value["model_fingerprint"],
            runtime_fingerprint=value["runtime_fingerprint"],
            settings_fingerprint=value["settings_fingerprint"],
            source_id=value["source_id"],
            predecessor_artifact_fingerprint=value["predecessor_artifact_fingerprint"],
            execution_fingerprint=value["execution_fingerprint"],
            format_label=value["format_label"],
            shape=tuple(shape),
            created_at_ms=value["created_at_ms"],
            expires_at_ms=value["expires_at_ms"],
            output_fingerprint=value["output_fingerprint"],
            byte_length=value["byte_length"],
            failure_code=value["failure_code"],
            receipt_fingerprint=value["receipt_fingerprint"],
        )
    except (ValueError, TypeError) as exc:
        if isinstance(exc, SegmentArtifactError):
            raise
        raise SegmentArtifactError("receipt_wire_value") from exc


__all__ = [
    "ArtifactKind",
    "ArtifactLifecycleState",
    "MAX_SEGMENT_ARTIFACT_BYTES",
    "MAX_SEGMENT_ARTIFACT_DIMENSION",
    "MAX_SEGMENT_ARTIFACT_DIMENSIONS",
    "MAX_SEGMENT_ARTIFACT_RECEIPT_BYTES",
    "SEGMENT_ARTIFACT_PUBLIC_SCHEMA",
    "MAX_SEGMENT_ARTIFACT_TTL_MILLISECONDS",
    "SEGMENT_ARTIFACT_RECEIPT_SCHEMA",
    "SegmentArtifactError",
    "SegmentArtifactReceipt",
    "begin_segment_artifact_receipt",
    "complete_segment_artifact_receipt",
    "decode_segment_artifact_receipt",
    "fail_segment_artifact_receipt",
    "redact_segment_artifact_fingerprint",
]
