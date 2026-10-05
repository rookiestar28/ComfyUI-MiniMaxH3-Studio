"""Pure selective rerun planning over the M17-02, M17-07 and M17-08 authorities.

This module only composes content-free identities and store inspection evidence. It never reads
artifact bytes, opens a path, contacts a host, submits a prompt, or owns generation lifecycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from .canonical import canonical_bytes, canonical_fingerprint
from .generation_sequence import (
    MAX_GENERATION_SEQUENCE_CONCURRENCY,
    GenerationJobSpec,
    GenerationSequencePlan,
    artifact_receipt_matches_job_contract,
    build_generation_sequence_plan,
)
from .recompute_closure import (
    RecomputeDisposition,
    RecomputePlan,
    extend_recompute_plan,
    plan_recompute,
)
from .segment_artifacts import (
    ArtifactKind,
    ArtifactLifecycleState,
    SegmentArtifactReceipt,
)
from .segment_workspace import (
    MAX_WORKSPACE_REVISION,
    MAX_WORKSPACE_SEGMENTS,
    MAX_WORKSPACE_WIRE_BYTES,
    MultiSegmentWorkspace,
    SegmentContextManifest,
    derive_segment_manifests,
)

SELECTIVE_RERUN_PLAN_SCHEMA = "h3.context.selective_rerun_plan.v1"
SELECTIVE_RERUN_APPROVAL_SCHEMA = "h3.context.selective_rerun_approval.v1"
MAX_SELECTIVE_RERUN_WIRE_BYTES = MAX_WORKSPACE_WIRE_BYTES

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SelectiveRerunError(ValueError):
    """Raised when selective planning or approval cannot preserve exact identity."""


class SelectiveRerunDisposition(str, Enum):
    QUEUE = "queue"
    REUSE = "reuse"
    BLOCKED = "blocked"


class ArtifactReuseStatus(str, Enum):
    """Content-free status values accepted from the private M17-07 store adapter."""

    REUSABLE = "reusable"
    MISSING = "missing"
    INCOMPATIBLE = "incompatible"
    TAMPERED = "tampered"
    EXPIRED = "expired"
    STALE = "stale"
    WRONG_KIND = "wrong_kind"
    NOT_COMPLETE = "not_complete"
    DISABLED = "disabled"
    UNSAFE = "unsafe"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise SelectiveRerunError(f"bounded_identifier:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise SelectiveRerunError(f"sha256_fingerprint:{field}")
    return value


def _positive_int(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise SelectiveRerunError(f"positive_integer:{field}")
    return value


def _identifiers(values: object, field: str) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > MAX_WORKSPACE_SEGMENTS:
        raise SelectiveRerunError(field)
    result = tuple(_identifier(value, f"{field}[{index}]") for index, value in enumerate(values))
    if len(result) != len(set(result)):
        raise SelectiveRerunError(f"duplicate_{field}")
    return result


def _fingerprints(values: object, field: str) -> tuple[str, ...]:
    if type(values) is not tuple or not 1 <= len(values) <= MAX_WORKSPACE_SEGMENTS:
        raise SelectiveRerunError(field)
    result = tuple(_fingerprint(value, f"{field}[{index}]") for index, value in enumerate(values))
    if len(result) != len(set(result)):
        raise SelectiveRerunError(f"duplicate_{field}")
    return result


def _selection(
    requested: tuple[str, ...] | None,
    current_ids: tuple[str, ...],
) -> tuple[str, ...] | None:
    if requested is None:
        return None
    result = _identifiers(requested, "requested_segment_ids")
    if not set(result).issubset(current_ids):
        raise SelectiveRerunError("unknown_requested_segment")
    return result


@dataclass(frozen=True, slots=True)
class SelectiveArtifactEvidence:
    """One content-free store inspection joined to an optional immutable receipt."""

    segment_id: str
    status: ArtifactReuseStatus
    inspected_at_ms: int
    receipt: SegmentArtifactReceipt | None = None
    receipt_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "evidence.segment_id")
        if type(self.status) is not ArtifactReuseStatus:
            raise SelectiveRerunError("evidence.status")
        _positive_int(self.inspected_at_ms, "evidence.inspected_at_ms", 9_999_999_999_999)
        if self.receipt is not None and type(self.receipt) is not SegmentArtifactReceipt:
            raise SelectiveRerunError("evidence.receipt")
        if self.receipt is not None:
            expected = self.receipt.fingerprint
            if self.receipt_fingerprint is None:
                object.__setattr__(self, "receipt_fingerprint", expected)
            elif _fingerprint(self.receipt_fingerprint, "evidence.receipt_fingerprint") != expected:
                raise SelectiveRerunError("evidence_receipt_fingerprint_mismatch")
        elif self.receipt_fingerprint is not None:
            _fingerprint(self.receipt_fingerprint, "evidence.receipt_fingerprint")
        if self.status is ArtifactReuseStatus.REUSABLE and self.receipt is None:
            raise SelectiveRerunError("reusable_evidence_receipt_missing")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "status": self.status.value,
            "inspected_at_ms": self.inspected_at_ms,
            "receipt_fingerprint": self.receipt_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SelectiveRerunDecision:
    segment_id: str
    disposition: SelectiveRerunDisposition
    reason_codes: tuple[str, ...]
    triggering_segment_ids: tuple[str, ...] = ()
    reused_receipt_fingerprint: str | None = None
    reused_evidence_inspected_at_ms: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "decision.segment_id")
        if type(self.disposition) is not SelectiveRerunDisposition:
            raise SelectiveRerunError("decision.disposition")
        reasons = _identifiers(self.reason_codes, "decision.reason_codes")
        if not reasons:
            raise SelectiveRerunError("decision.reason_codes")
        _identifiers(self.triggering_segment_ids, "decision.triggering_segment_ids")
        if self.reused_receipt_fingerprint is not None:
            _fingerprint(self.reused_receipt_fingerprint, "decision.reused_receipt")
        if self.disposition is SelectiveRerunDisposition.REUSE:
            if self.reused_receipt_fingerprint is None:
                raise SelectiveRerunError("reuse_receipt_missing")
            _positive_int(
                self.reused_evidence_inspected_at_ms,
                "decision.reused_evidence_inspected_at_ms",
                9_999_999_999_999,
            )
        elif self.reused_receipt_fingerprint is not None:
            raise SelectiveRerunError("non_reuse_receipt")
        elif self.reused_evidence_inspected_at_ms is not None:
            raise SelectiveRerunError("non_reuse_evidence_timestamp")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "disposition": self.disposition.value,
            "reason_codes": list(self.reason_codes),
            "triggering_segment_ids": list(self.triggering_segment_ids),
            "reused_receipt_fingerprint": self.reused_receipt_fingerprint,
            "reused_evidence_inspected_at_ms": self.reused_evidence_inspected_at_ms,
        }


@dataclass(frozen=True, slots=True)
class SelectiveRerunPlan:
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    manifest_fingerprints: tuple[str, ...]
    job_spec_fingerprints: tuple[str, ...]
    requested_segment_ids: tuple[str, ...] | None
    decisions: tuple[SelectiveRerunDecision, ...]
    queued_segment_ids: tuple[str, ...]
    reused_segment_ids: tuple[str, ...]
    blocked_segment_ids: tuple[str, ...]
    required_enlargement_segment_ids: tuple[str, ...]
    selection_safe: bool
    requires_full_recompute: bool
    recompute_plan: RecomputePlan
    reusable_receipts: tuple[SegmentArtifactReceipt, ...] = ()
    max_concurrency: int = 1
    plan_fingerprint: str | None = None
    schema: str = SELECTIVE_RERUN_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SELECTIVE_RERUN_PLAN_SCHEMA:
            raise SelectiveRerunError("unsupported_selective_rerun_schema")
        _identifier(self.workspace_id, "workspace_id")
        _positive_int(self.workspace_revision, "workspace_revision", MAX_WORKSPACE_REVISION)
        _fingerprint(self.workspace_fingerprint, "workspace_fingerprint")
        fingerprints = _fingerprints(self.manifest_fingerprints, "manifest_fingerprints")
        spec_fingerprints = _fingerprints(self.job_spec_fingerprints, "job_spec_fingerprints")
        if len(fingerprints) != len(spec_fingerprints):
            raise SelectiveRerunError("job_spec_manifest_count")
        if type(self.decisions) is not tuple or len(self.decisions) != len(fingerprints):
            raise SelectiveRerunError("decision_count")
        if not all(type(item) is SelectiveRerunDecision for item in self.decisions):
            raise SelectiveRerunError("decision_type")
        decision_ids = tuple(item.segment_id for item in self.decisions)
        if len(set(decision_ids)) != len(decision_ids):
            raise SelectiveRerunError("duplicate_decision_segment")
        if type(self.requested_segment_ids) is not tuple and self.requested_segment_ids is not None:
            raise SelectiveRerunError("requested_segment_ids")
        if self.requested_segment_ids is not None:
            _identifiers(self.requested_segment_ids, "requested_segment_ids")
            if not set(self.requested_segment_ids).issubset(decision_ids):
                raise SelectiveRerunError("requested_segment_unknown")
        for values, field in (
            (self.queued_segment_ids, "queued_segment_ids"),
            (self.reused_segment_ids, "reused_segment_ids"),
            (self.blocked_segment_ids, "blocked_segment_ids"),
            (self.required_enlargement_segment_ids, "required_enlargement_segment_ids"),
        ):
            _identifiers(values, field)
            if not set(values).issubset(decision_ids):
                raise SelectiveRerunError(f"{field}_unknown")
        partitions = (
            set(self.queued_segment_ids),
            set(self.reused_segment_ids),
            set(self.blocked_segment_ids),
        )
        if any(
            partitions[index] & partitions[other] for index in range(3) for other in range(index)
        ):
            raise SelectiveRerunError("disposition_overlap")
        if set().union(*partitions) != set(decision_ids):
            raise SelectiveRerunError("disposition_partition")
        if type(self.selection_safe) is not bool or type(self.requires_full_recompute) is not bool:
            raise SelectiveRerunError("plan_flags")
        if type(self.recompute_plan) is not RecomputePlan:
            raise SelectiveRerunError("recompute_plan_type")
        if tuple(item.segment_id for item in self.recompute_plan.decisions) != decision_ids:
            raise SelectiveRerunError("recompute_decision_mismatch")
        if self.selection_safe != self.recompute_plan.selection_safe:
            raise SelectiveRerunError("selection_safe_mismatch")
        if self.requires_full_recompute != self.recompute_plan.requires_full_recompute:
            raise SelectiveRerunError("full_recompute_mismatch")
        if (
            self.required_enlargement_segment_ids
            != self.recompute_plan.missing_required_segment_ids
        ):
            raise SelectiveRerunError("enlargement_mismatch")
        expected_queued = tuple(
            item.segment_id
            for item in self.recompute_plan.decisions
            if item.disposition
            not in {
                RecomputeDisposition.CLEAN,
                RecomputeDisposition.BLOCKED_MISSING_PREDECESSOR,
                RecomputeDisposition.REQUIRES_FULL_RECOMPUTE,
            }
        )
        expected_blocked = tuple(
            item.segment_id
            for item in self.recompute_plan.decisions
            if item.disposition
            in {
                RecomputeDisposition.BLOCKED_MISSING_PREDECESSOR,
                RecomputeDisposition.REQUIRES_FULL_RECOMPUTE,
            }
        )
        expected_reused = tuple(
            item.segment_id
            for item in self.recompute_plan.decisions
            if item.disposition is RecomputeDisposition.CLEAN
        )
        if (
            self.queued_segment_ids != expected_queued
            or self.blocked_segment_ids != expected_blocked
            or self.reused_segment_ids != expected_reused
        ):
            raise SelectiveRerunError("disposition_recompute_mismatch")
        if type(self.reusable_receipts) is not tuple:
            raise SelectiveRerunError("reusable_receipts")
        receipt_ids = tuple(item.segment_id for item in self.reusable_receipts)
        if receipt_ids != self.reused_segment_ids or not all(
            type(item) is SegmentArtifactReceipt and item.state is ArtifactLifecycleState.COMPLETE
            for item in self.reusable_receipts
        ):
            raise SelectiveRerunError("reusable_receipt_mismatch")
        reusable_by_id = {item.segment_id: item for item in self.reusable_receipts}
        for decision in self.decisions:
            if decision.disposition is not SelectiveRerunDisposition.REUSE:
                continue
            receipt = reusable_by_id.get(decision.segment_id)
            if receipt is None or receipt.fingerprint != decision.reused_receipt_fingerprint:
                raise SelectiveRerunError("reusable_receipt_decision_mismatch")
            if receipt.fingerprint != canonical_fingerprint(receipt._wire_without_fingerprint()):
                raise SelectiveRerunError("reusable_receipt_tampered")
        _positive_int(
            self.max_concurrency,
            "max_concurrency",
            MAX_GENERATION_SEQUENCE_CONCURRENCY,
        )
        if self.plan_fingerprint is None:
            object.__setattr__(
                self, "plan_fingerprint", canonical_fingerprint(self._wire_without_fingerprint())
            )
        else:
            _fingerprint(self.plan_fingerprint, "plan_fingerprint")
        if len(canonical_bytes(self.to_wire())) > MAX_SELECTIVE_RERUN_WIRE_BYTES:
            raise SelectiveRerunError("selective_rerun_wire_limit")

    @property
    def fingerprint(self) -> str:
        if self.plan_fingerprint is None:  # pragma: no cover - initialized above
            raise SelectiveRerunError("plan_fingerprint_uninitialized")
        return self.plan_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "manifest_fingerprints": list(self.manifest_fingerprints),
            "job_spec_fingerprints": list(self.job_spec_fingerprints),
            "requested_segment_ids": (
                None if self.requested_segment_ids is None else list(self.requested_segment_ids)
            ),
            "decisions": [item.to_public_dict() for item in self.decisions],
            "queued_segment_ids": list(self.queued_segment_ids),
            "reused_segment_ids": list(self.reused_segment_ids),
            "blocked_segment_ids": list(self.blocked_segment_ids),
            "required_enlargement_segment_ids": list(self.required_enlargement_segment_ids),
            "selection_safe": self.selection_safe,
            "requires_full_recompute": self.requires_full_recompute,
            "recompute_plan": self.recompute_plan.to_public_dict(),
            "reusable_receipt_fingerprints": [item.fingerprint for item in self.reusable_receipts],
            "max_concurrency": self.max_concurrency,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["plan_fingerprint"] = self.fingerprint
        return value

    def to_public_dict(self) -> dict[str, object]:
        return self.to_wire()


@dataclass(frozen=True, slots=True)
class SelectiveRerunApproval:
    plan_fingerprint: str
    approved_segment_ids: tuple[str, ...]
    approval_fingerprint: str | None = None
    schema: str = SELECTIVE_RERUN_APPROVAL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SELECTIVE_RERUN_APPROVAL_SCHEMA:
            raise SelectiveRerunError("unsupported_selective_approval_schema")
        _fingerprint(self.plan_fingerprint, "approval.plan_fingerprint")
        _identifiers(self.approved_segment_ids, "approved_segment_ids")
        if self.approval_fingerprint is None:
            object.__setattr__(
                self,
                "approval_fingerprint",
                canonical_fingerprint(self._wire_without_fingerprint()),
            )
        else:
            _fingerprint(self.approval_fingerprint, "approval_fingerprint")

    @property
    def fingerprint(self) -> str:
        if self.approval_fingerprint is None:  # pragma: no cover
            raise SelectiveRerunError("approval_fingerprint_uninitialized")
        return self.approval_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_fingerprint": self.plan_fingerprint,
            "approved_segment_ids": list(self.approved_segment_ids),
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["approval_fingerprint"] = self.fingerprint
        return value


def _verify_plan(plan: SelectiveRerunPlan) -> None:
    if type(plan) is not SelectiveRerunPlan:
        raise SelectiveRerunError("plan_type")
    if plan.fingerprint != canonical_fingerprint(plan._wire_without_fingerprint()):
        raise SelectiveRerunError("tampered_selective_plan")


def _verify_approval(approval: SelectiveRerunApproval) -> None:
    if type(approval) is not SelectiveRerunApproval:
        raise SelectiveRerunError("approval_type")
    if approval.fingerprint != canonical_fingerprint(approval._wire_without_fingerprint()):
        raise SelectiveRerunError("tampered_selective_approval")


def _status_reason(status: ArtifactReuseStatus) -> str:
    return {
        ArtifactReuseStatus.MISSING: "artifact_missing",
        ArtifactReuseStatus.INCOMPATIBLE: "artifact_incompatible",
        ArtifactReuseStatus.TAMPERED: "artifact_tampered",
        ArtifactReuseStatus.EXPIRED: "artifact_expired",
        ArtifactReuseStatus.STALE: "artifact_stale",
        ArtifactReuseStatus.WRONG_KIND: "artifact_wrong_kind",
        ArtifactReuseStatus.NOT_COMPLETE: "artifact_not_complete",
        ArtifactReuseStatus.DISABLED: "artifact_disabled",
        ArtifactReuseStatus.UNSAFE: "artifact_unsafe",
        ArtifactReuseStatus.REUSABLE: "artifact_incompatible",
    }[status]


def _receipt_mismatch_reason(
    evidence: SelectiveArtifactEvidence,
    manifest: SegmentContextManifest,
    spec: GenerationJobSpec,
    *,
    receipt_manifest: SegmentContextManifest | None = None,
    predecessor_output_fingerprint: str | None | object = None,
) -> str | None:
    receipt = evidence.receipt
    if evidence.status is not ArtifactReuseStatus.REUSABLE:
        return _status_reason(evidence.status)
    if receipt is None:  # pragma: no cover - evidence constructor rejects this
        return "artifact_missing"
    if evidence.receipt_fingerprint != receipt.fingerprint:
        return "artifact_tampered"
    if receipt.artifact_kind is not ArtifactKind.SEGMENT_OUTPUT:
        return "artifact_wrong_kind"
    if receipt.state is not ArtifactLifecycleState.COMPLETE:
        return "artifact_not_complete"
    # The store receipt is immutable by contract. Recompute its canonical identity as a
    # defensive boundary check so an in-memory field mutation cannot replay a stale receipt.
    if receipt.fingerprint != canonical_fingerprint(receipt._wire_without_fingerprint()):
        return "artifact_tampered"
    if receipt.expires_at_ms <= evidence.inspected_at_ms:
        return "artifact_expired"
    identity_manifest = manifest if receipt_manifest is None else receipt_manifest
    checks = (
        # A reused receipt is exact for the accepted predecessor manifest, while the current
        # revision joins it only through the unchanged producer identity. This preserves safe
        # reuse when an unrelated segment advances the workspace revision.
        (receipt.workspace_id, identity_manifest.workspace_id),
        (identity_manifest.workspace_id, manifest.workspace_id),
        (receipt.workspace_revision, identity_manifest.workspace_revision),
        (receipt.workspace_fingerprint, identity_manifest.workspace_fingerprint),
        (receipt.segment_id, identity_manifest.segment_id),
        (identity_manifest.segment_id, manifest.segment_id),
        (receipt.manifest_fingerprint, identity_manifest.fingerprint),
        (receipt.producer_fingerprint, identity_manifest.producer_fingerprint),
        (receipt.producer_fingerprint, manifest.producer_fingerprint),
        (receipt.native_binding_fingerprint, identity_manifest.native_binding_fingerprint),
        (receipt.native_binding_fingerprint, manifest.native_binding_fingerprint),
        (receipt.settings_fingerprint, identity_manifest.producer_settings_fingerprint),
        (receipt.settings_fingerprint, manifest.producer_settings_fingerprint),
        (receipt.source_id, identity_manifest.source_id),
        (receipt.source_id, manifest.source_id),
        (receipt.graph_fingerprint, spec.graph_fingerprint),
        (receipt.model_fingerprint, spec.model_fingerprint),
        (receipt.runtime_fingerprint, spec.runtime_fingerprint),
    )
    if any(actual != expected for actual, expected in checks) or not (
        artifact_receipt_matches_job_contract(spec, receipt)
    ):
        return "artifact_incompatible"
    if receipt.output_fingerprint is None or receipt.byte_length <= 0:
        return "artifact_not_complete"
    if manifest.dependency_segment_ids:
        if predecessor_output_fingerprint is not None and (
            receipt.predecessor_artifact_fingerprint != predecessor_output_fingerprint
        ):
            return "artifact_incompatible"
    elif receipt.predecessor_artifact_fingerprint is not None:
        return "artifact_incompatible"
    return None


def _validate_specs(
    manifests: tuple[SegmentContextManifest, ...],
    job_specs: tuple[GenerationJobSpec, ...],
) -> dict[str, GenerationJobSpec]:
    if type(job_specs) is not tuple or len(job_specs) != len(manifests):
        raise SelectiveRerunError("job_spec_count")
    if not all(type(item) is GenerationJobSpec for item in job_specs):
        raise SelectiveRerunError("job_spec_type")
    specs = {item.segment_id: item for item in job_specs}
    if len(specs) != len(job_specs) or set(specs) != {item.segment_id for item in manifests}:
        raise SelectiveRerunError("job_spec_manifest_mismatch")
    return specs


def build_selective_rerun_plan(
    workspace: MultiSegmentWorkspace,
    previous_manifests: tuple[SegmentContextManifest, ...],
    current_manifests: tuple[SegmentContextManifest, ...],
    *,
    job_specs: tuple[GenerationJobSpec, ...],
    requested_segment_ids: tuple[str, ...] | None = None,
    artifact_evidence: tuple[SelectiveArtifactEvidence, ...] = (),
    max_concurrency: int = 1,
) -> SelectiveRerunPlan:
    """Plan reuse and rerun dispositions without performing any host-visible action."""

    if type(workspace) is not MultiSegmentWorkspace:
        raise SelectiveRerunError("workspace_type")
    if type(current_manifests) is not tuple or not all(
        type(item) is SegmentContextManifest for item in current_manifests
    ):
        raise SelectiveRerunError("current_manifest_type")
    expected = derive_segment_manifests(workspace)
    if tuple(item.fingerprint for item in current_manifests) != tuple(
        item.fingerprint for item in expected
    ):
        raise SelectiveRerunError("workspace_manifest_mismatch")
    if type(previous_manifests) is not tuple or not all(
        type(item) is SegmentContextManifest for item in previous_manifests
    ):
        raise SelectiveRerunError("previous_manifest_type")
    current_ids = tuple(item.segment_id for item in current_manifests)
    requested = _selection(requested_segment_ids, current_ids)
    specs = _validate_specs(current_manifests, job_specs)
    _positive_int(max_concurrency, "max_concurrency", MAX_GENERATION_SEQUENCE_CONCURRENCY)
    if type(artifact_evidence) is not tuple or len(artifact_evidence) > MAX_WORKSPACE_SEGMENTS:
        raise SelectiveRerunError("artifact_evidence")
    evidence_by_id: dict[str, SelectiveArtifactEvidence] = {}
    for item in artifact_evidence:
        if type(item) is not SelectiveArtifactEvidence:
            raise SelectiveRerunError("artifact_evidence_type")
        if item.segment_id in evidence_by_id:
            raise SelectiveRerunError("duplicate_artifact_evidence")
        if item.segment_id not in current_ids:
            raise SelectiveRerunError("unknown_artifact_evidence_segment")
        evidence_by_id[item.segment_id] = item

    base_plan = plan_recompute(previous_manifests, current_manifests)
    manifests_by_id = {item.segment_id: item for item in current_manifests}
    previous_by_id = {item.segment_id: item for item in previous_manifests}
    forced_reasons: dict[str, tuple[str, ...]] = {
        segment_id: ("requested_rerun",) for segment_id in (requested or ())
    }
    reusable_candidates: dict[str, SegmentArtifactReceipt] = {}
    for segment_id in current_ids:
        base_decision = base_plan.decisions[current_ids.index(segment_id)]
        if base_decision.disposition is not RecomputeDisposition.CLEAN:
            continue
        evidence = evidence_by_id.get(segment_id)
        if evidence is None:
            forced_reasons.setdefault(segment_id, ("artifact_missing",))
            continue
        receipt_manifest = previous_by_id.get(segment_id)
        if receipt_manifest is None:
            forced_reasons.setdefault(segment_id, ("artifact_incompatible",))
            continue
        mismatch = _receipt_mismatch_reason(
            evidence,
            manifests_by_id[segment_id],
            specs[segment_id],
            receipt_manifest=receipt_manifest,
        )
        if mismatch is None:
            if evidence.receipt is None:  # pragma: no cover - mismatch guards this
                forced_reasons.setdefault(segment_id, ("artifact_missing",))
            else:
                reusable_candidates[segment_id] = evidence.receipt
        else:
            forced_reasons.setdefault(segment_id, (mismatch,))

    # A clean receipt must also point at the exact clean predecessor artifact. This is checked
    # before propagation so a bad boundary becomes an origin rather than a hidden child failure.
    for segment_id in current_ids:
        receipt = reusable_candidates.get(segment_id)
        if receipt is None:
            continue
        manifest = manifests_by_id[segment_id]
        receipt_manifest = previous_by_id.get(segment_id)
        if receipt_manifest is None:
            forced_reasons[segment_id] = ("artifact_incompatible",)
            reusable_candidates.pop(segment_id, None)
            continue
        if manifest.dependency_segment_ids:
            predecessor = manifest.dependency_segment_ids[0]
            predecessor_receipt = reusable_candidates.get(predecessor)
            if predecessor_receipt is not None and (
                _receipt_mismatch_reason(
                    evidence_by_id[segment_id],
                    manifest,
                    specs[segment_id],
                    receipt_manifest=receipt_manifest,
                    predecessor_output_fingerprint=predecessor_receipt.output_fingerprint,
                )
                is not None
            ):
                forced_reasons[segment_id] = ("artifact_incompatible",)
                reusable_candidates.pop(segment_id, None)
        elif receipt.predecessor_artifact_fingerprint is not None:
            forced_reasons[segment_id] = ("artifact_incompatible",)
            reusable_candidates.pop(segment_id, None)

    forced_ids = tuple(item for item in current_ids if item in forced_reasons)
    recompute_plan = extend_recompute_plan(
        current_manifests,
        base_plan,
        forced_dirty_segment_ids=forced_ids,
        forced_reason_codes=tuple((item, forced_reasons[item]) for item in forced_ids),
        requested_segment_ids=requested,
    )

    decisions: list[SelectiveRerunDecision] = []
    reusable_receipts: list[SegmentArtifactReceipt] = []
    for decision in recompute_plan.decisions:
        inspected_at_ms: int | None = None
        if decision.disposition in {
            RecomputeDisposition.BLOCKED_MISSING_PREDECESSOR,
            RecomputeDisposition.REQUIRES_FULL_RECOMPUTE,
        }:
            disposition = SelectiveRerunDisposition.BLOCKED
            reasons = decision.reason_codes
            receipt_fingerprint = None
        elif decision.disposition is RecomputeDisposition.CLEAN:
            receipt = reusable_candidates.get(decision.segment_id)
            if receipt is None:
                raise SelectiveRerunError("missing_reuse_evidence")
            disposition = SelectiveRerunDisposition.REUSE
            reasons = ("exact_clean_artifact",)
            receipt_fingerprint = receipt.fingerprint
            evidence = evidence_by_id[decision.segment_id]
            inspected_at_ms = evidence.inspected_at_ms
            reusable_receipts.append(receipt)
        else:
            disposition = SelectiveRerunDisposition.QUEUE
            reasons = decision.reason_codes
            receipt_fingerprint = None
            inspected_at_ms = None
        decisions.append(
            SelectiveRerunDecision(
                segment_id=decision.segment_id,
                disposition=disposition,
                reason_codes=reasons,
                triggering_segment_ids=decision.triggering_segment_ids,
                reused_receipt_fingerprint=receipt_fingerprint,
                reused_evidence_inspected_at_ms=inspected_at_ms,
            )
        )

    return SelectiveRerunPlan(
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        manifest_fingerprints=tuple(item.fingerprint for item in current_manifests),
        job_spec_fingerprints=tuple(
            canonical_fingerprint(specs[item.segment_id].to_wire()) for item in current_manifests
        ),
        requested_segment_ids=requested,
        decisions=tuple(decisions),
        queued_segment_ids=tuple(
            item.segment_id
            for item in decisions
            if item.disposition is SelectiveRerunDisposition.QUEUE
        ),
        reused_segment_ids=tuple(
            item.segment_id
            for item in decisions
            if item.disposition is SelectiveRerunDisposition.REUSE
        ),
        blocked_segment_ids=tuple(
            item.segment_id
            for item in decisions
            if item.disposition is SelectiveRerunDisposition.BLOCKED
        ),
        required_enlargement_segment_ids=recompute_plan.missing_required_segment_ids,
        selection_safe=recompute_plan.selection_safe,
        requires_full_recompute=recompute_plan.requires_full_recompute,
        recompute_plan=recompute_plan,
        reusable_receipts=tuple(reusable_receipts),
        max_concurrency=max_concurrency,
    )


def build_approved_generation_sequence_plan(
    plan: SelectiveRerunPlan,
    approval: SelectiveRerunApproval,
    *,
    workspace: MultiSegmentWorkspace,
    manifests: tuple[SegmentContextManifest, ...],
    job_specs: tuple[GenerationJobSpec, ...],
    artifact_evidence: tuple[SelectiveArtifactEvidence, ...] = (),
    approval_inspected_at_ms: int | None = None,
) -> GenerationSequencePlan:
    """Bind approval and fresh store evidence, then delegate lifecycle to M17-08.

    Reused receipts must be re-inspected at the approval boundary. The initial planning
    inspection is bound into the plan, while this fresh evidence prevents an expired,
    replaced, or otherwise unusable artifact from crossing into host submission.
    """

    _verify_plan(plan)
    _verify_approval(approval)
    if approval.plan_fingerprint != plan.fingerprint:
        raise SelectiveRerunError("approval_plan_mismatch")
    if plan.requires_full_recompute or plan.blocked_segment_ids:
        raise SelectiveRerunError("blocked_selective_rerun")
    if tuple(approval.approved_segment_ids) != plan.queued_segment_ids:
        if not set(plan.queued_segment_ids).issubset(approval.approved_segment_ids):
            raise SelectiveRerunError("approval_missing_required_segment")
        raise SelectiveRerunError("approval_segment_set_mismatch")
    if type(workspace) is not MultiSegmentWorkspace:
        raise SelectiveRerunError("workspace_type")
    expected_manifests = derive_segment_manifests(workspace)
    if (
        type(manifests) is not tuple
        or tuple(item.fingerprint for item in manifests)
        != tuple(item.fingerprint for item in expected_manifests)
        or tuple(item.fingerprint for item in manifests) != plan.manifest_fingerprints
    ):
        raise SelectiveRerunError("plan_manifest_mismatch")
    specs = _validate_specs(manifests, job_specs)
    if (
        tuple(canonical_fingerprint(specs[item.segment_id].to_wire()) for item in manifests)
        != plan.job_spec_fingerprints
    ):
        raise SelectiveRerunError("plan_job_spec_mismatch")
    if plan.reused_segment_ids:
        if approval_inspected_at_ms is None:
            raise SelectiveRerunError("reuse_approval_inspection_missing")
        approval_time = _positive_int(
            approval_inspected_at_ms,
            "approval_inspected_at_ms",
            9_999_999_999_999,
        )
        if type(artifact_evidence) is not tuple:
            raise SelectiveRerunError("approval_artifact_evidence")
        evidence_by_id: dict[str, SelectiveArtifactEvidence] = {}
        for evidence in artifact_evidence:
            if type(evidence) is not SelectiveArtifactEvidence:
                raise SelectiveRerunError("approval_artifact_evidence_type")
            if evidence.segment_id in evidence_by_id:
                raise SelectiveRerunError("duplicate_approval_artifact_evidence")
            if evidence.segment_id not in plan.reused_segment_ids:
                raise SelectiveRerunError("unexpected_approval_artifact_evidence")
            evidence_by_id[evidence.segment_id] = evidence
        planned_receipts = {item.segment_id: item for item in plan.reusable_receipts}
        for decision in plan.decisions:
            if decision.disposition is not SelectiveRerunDisposition.REUSE:
                continue
            fresh_evidence = evidence_by_id.get(decision.segment_id)
            if fresh_evidence is None:
                raise SelectiveRerunError("reuse_evidence_missing")
            if fresh_evidence.status is not ArtifactReuseStatus.REUSABLE:
                raise SelectiveRerunError("reuse_evidence_not_reusable")
            receipt = fresh_evidence.receipt
            planned_receipt = planned_receipts.get(decision.segment_id)
            if (
                receipt is None
                or planned_receipt is None
                or fresh_evidence.receipt_fingerprint != decision.reused_receipt_fingerprint
                or receipt.fingerprint != decision.reused_receipt_fingerprint
            ):
                raise SelectiveRerunError("reuse_evidence_receipt_mismatch")
            if fresh_evidence.inspected_at_ms < (decision.reused_evidence_inspected_at_ms or 0):
                raise SelectiveRerunError("reuse_evidence_stale")
            if fresh_evidence.inspected_at_ms != approval_time:
                raise SelectiveRerunError("reuse_evidence_not_at_approval")
            if receipt.artifact_kind is not ArtifactKind.SEGMENT_OUTPUT:
                raise SelectiveRerunError("reuse_evidence_wrong_kind")
            if receipt.state is not ArtifactLifecycleState.COMPLETE:
                raise SelectiveRerunError("reuse_evidence_not_complete")
            if receipt.fingerprint != canonical_fingerprint(receipt._wire_without_fingerprint()):
                raise SelectiveRerunError("reuse_evidence_tampered")
            if (
                planned_receipt.fingerprint != decision.reused_receipt_fingerprint
                or planned_receipt.fingerprint
                != canonical_fingerprint(planned_receipt._wire_without_fingerprint())
            ):
                raise SelectiveRerunError("reuse_plan_receipt_mismatch")
            if receipt.expires_at_ms <= approval_time:
                raise SelectiveRerunError("reuse_evidence_expired")
    approved_recompute = replace(
        plan.recompute_plan,
        requested_segment_ids=approval.approved_segment_ids,
        missing_required_segment_ids=(),
        selection_safe=True,
    )
    dirty_specs = tuple(specs[item] for item in plan.queued_segment_ids)
    return build_generation_sequence_plan(
        workspace,
        manifests,
        approved_recompute,
        dirty_specs,
        reusable_receipts=plan.reusable_receipts,
        max_concurrency=plan.max_concurrency,
    )


__all__ = [
    "ArtifactReuseStatus",
    "MAX_SELECTIVE_RERUN_WIRE_BYTES",
    "SELECTIVE_RERUN_APPROVAL_SCHEMA",
    "SELECTIVE_RERUN_PLAN_SCHEMA",
    "SelectiveArtifactEvidence",
    "SelectiveRerunApproval",
    "SelectiveRerunDecision",
    "SelectiveRerunDisposition",
    "SelectiveRerunError",
    "SelectiveRerunPlan",
    "build_approved_generation_sequence_plan",
    "build_selective_rerun_plan",
]
