"""Explicit review and canonical apply authority for transient semantic proposals."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum

from .canonical import canonical_bytes, canonical_fingerprint
from .constrained_semantic_planning import (
    SemanticPlanningRequest,
    SemanticPlanningResult,
    SemanticPlanningStatus,
    SemanticTargetKind,
)
from .context_reporting import ContextPlan
from .contracts import ValidationSeverity
from .intent_graph import IntentGraph
from .segment_workspace import (
    AcceptedIntentAuthority,
    MultiSegmentWorkspace,
    SegmentDeclaration,
    revise_workspace,
)
from .semantic_enrichment import (
    SemanticEnrichmentResult,
    SemanticEnrichmentStatus,
)

SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA = "h3.semantic.proposal_transaction.v1"
MAX_SEMANTIC_PROPOSAL_ATTEMPTS = 8
MAX_SEMANTIC_PROPOSAL_REVISIONS = 32
MAX_SEMANTIC_CLARIFICATIONS = 32
MAX_SEMANTIC_TRANSACTION_WIRE_BYTES = 32_768

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GRAPH_COLLECTION_ORDER = (
    "subjects",
    "scenes",
    "actions",
    "cameras",
    "styles",
    "audios",
)
_PROTECTED_GRAPH_FIELDS = (
    "effective_duration",
    "registry",
    "events",
    "retention",
    "segments",
    "allow_gaps",
)


class SemanticProposalTransactionError(ValueError):
    """Raised when semantic proposal review cannot preserve canonical authority."""


class SemanticProposalTransactionState(str, Enum):
    CLARIFICATION_REQUIRED = "clarification_required"
    READY_FOR_REVIEW = "ready_for_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


class SemanticProposalAction(str, Enum):
    GENERATE = "generate"
    CLARIFY = "clarify"
    EDIT = "edit"
    REGENERATE = "regenerate"
    ACCEPT = "accept"
    REJECT = "reject"
    CANCEL = "cancel"
    FAIL = "fail"


_TERMINAL_STATES = {
    SemanticProposalTransactionState.ACCEPTED,
    SemanticProposalTransactionState.REJECTED,
    SemanticProposalTransactionState.CANCELLED,
    SemanticProposalTransactionState.FAILED,
}


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise SemanticProposalTransactionError(f"{field_name}_identifier")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise SemanticProposalTransactionError(f"{field_name}_fingerprint")
    return value


def _optional_fingerprint(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, field_name)


def _identifiers(values: object, field_name: str) -> tuple[str, ...]:
    if (
        type(values) is not tuple
        or len(values) > MAX_SEMANTIC_CLARIFICATIONS
        or len(set(values)) != len(values)
    ):
        raise SemanticProposalTransactionError(f"{field_name}_identifiers")
    return tuple(_identifier(value, field_name) for value in values)


def _fingerprints(values: object, field_name: str) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > MAX_SEMANTIC_CLARIFICATIONS:
        raise SemanticProposalTransactionError(f"{field_name}_fingerprints")
    return tuple(_fingerprint(value, field_name) for value in values)


@dataclass(frozen=True, slots=True)
class SemanticProposalAuthorization:
    """Content-free capability and consent facts for one proposal attempt."""

    capability_fingerprint: str
    profile_fingerprint: str
    raw_media_allowed: bool = False
    consent_fingerprint: str | None = None
    schema: str = SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA

    def __post_init__(self) -> None:
        _fingerprint(self.capability_fingerprint, "capability")
        _fingerprint(self.profile_fingerprint, "profile")
        if type(self.raw_media_allowed) is not bool:
            raise SemanticProposalTransactionError("raw_media_allowed")
        _optional_fingerprint(self.consent_fingerprint, "consent")
        if self.schema != SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA:
            raise SemanticProposalTransactionError("unsupported_authorization_schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_public_dict())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "capability_fingerprint": self.capability_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "raw_media_allowed": self.raw_media_allowed,
            "consent_fingerprint": self.consent_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SemanticIntentGraphDiff:
    """Bounded graph-level identity; graph content remains transient."""

    before_graph_fingerprint: str
    after_graph_fingerprint: str
    changed_collections: tuple[str, ...]
    planning_diff_fingerprint: str
    enrichment_audit_fingerprint: str
    schema: str = SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA

    def __post_init__(self) -> None:
        _fingerprint(self.before_graph_fingerprint, "before_graph")
        _fingerprint(self.after_graph_fingerprint, "after_graph")
        if self.before_graph_fingerprint == self.after_graph_fingerprint:
            raise SemanticProposalTransactionError("candidate_graph_noop")
        if (
            type(self.changed_collections) is not tuple
            or not self.changed_collections
            or tuple(item for item in _GRAPH_COLLECTION_ORDER if item in self.changed_collections)
            != self.changed_collections
            or len(set(self.changed_collections)) != len(self.changed_collections)
        ):
            raise SemanticProposalTransactionError("changed_collections")
        _fingerprint(self.planning_diff_fingerprint, "planning_diff")
        _fingerprint(self.enrichment_audit_fingerprint, "enrichment_audit")
        if self.schema != SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA:
            raise SemanticProposalTransactionError("unsupported_graph_diff_schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_public_dict())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "before_graph_fingerprint": self.before_graph_fingerprint,
            "after_graph_fingerprint": self.after_graph_fingerprint,
            "changed_collections": list(self.changed_collections),
            "planning_diff_fingerprint": self.planning_diff_fingerprint,
            "enrichment_audit_fingerprint": self.enrichment_audit_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SemanticProposalTransaction:
    transaction_id: str
    attempt: int
    revision: int
    state: SemanticProposalTransactionState
    last_action: SemanticProposalAction
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    segment_id: str
    baseline_plan_fingerprint: str
    baseline_graph_fingerprint: str
    planning_baseline_fingerprint: str
    planning_result_fingerprint: str
    authorization_fingerprint: str
    graph_diff: SemanticIntentGraphDiff
    candidate_graph: IntentGraph = field(repr=False, compare=False)
    clarification_ids: tuple[str, ...] = ()
    resolution_fingerprints: tuple[str, ...] = ()
    previous_transaction_fingerprint: str | None = None
    failure_code: str | None = None
    applied_workspace_fingerprint: str | None = None
    transaction_fingerprint: str | None = None
    schema: str = SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA:
            raise SemanticProposalTransactionError("unsupported_transaction_schema")
        _identifier(self.transaction_id, "transaction_id")
        if type(self.attempt) is not int or not 1 <= self.attempt <= MAX_SEMANTIC_PROPOSAL_ATTEMPTS:
            raise SemanticProposalTransactionError("transaction_attempt")
        if (
            type(self.revision) is not int
            or not 1 <= self.revision <= MAX_SEMANTIC_PROPOSAL_REVISIONS
        ):
            raise SemanticProposalTransactionError("transaction_revision")
        if type(self.state) is not SemanticProposalTransactionState:
            raise SemanticProposalTransactionError("transaction_state")
        if type(self.last_action) is not SemanticProposalAction:
            raise SemanticProposalTransactionError("transaction_action")
        _identifier(self.workspace_id, "workspace_id")
        if type(self.workspace_revision) is not int or self.workspace_revision <= 0:
            raise SemanticProposalTransactionError("workspace_revision")
        for value, name in (
            (self.workspace_fingerprint, "workspace"),
            (self.baseline_plan_fingerprint, "baseline_plan"),
            (self.baseline_graph_fingerprint, "baseline_graph"),
            (self.planning_baseline_fingerprint, "planning_baseline"),
            (self.planning_result_fingerprint, "planning_result"),
            (self.authorization_fingerprint, "authorization"),
        ):
            _fingerprint(value, name)
        _identifier(self.segment_id, "segment_id")
        if type(self.graph_diff) is not SemanticIntentGraphDiff:
            raise SemanticProposalTransactionError("graph_diff_type")
        if type(self.candidate_graph) is not IntentGraph:
            raise SemanticProposalTransactionError("candidate_graph_type")
        if canonical_fingerprint(self.candidate_graph.to_wire()) != (
            self.graph_diff.after_graph_fingerprint
        ):
            raise SemanticProposalTransactionError("candidate_graph_fingerprint_mismatch")
        object.__setattr__(
            self,
            "clarification_ids",
            _identifiers(self.clarification_ids, "clarification_id"),
        )
        object.__setattr__(
            self,
            "resolution_fingerprints",
            _fingerprints(self.resolution_fingerprints, "resolution"),
        )
        _optional_fingerprint(self.previous_transaction_fingerprint, "previous_transaction")
        if self.failure_code is not None:
            _identifier(self.failure_code, "failure_code")
        _optional_fingerprint(self.applied_workspace_fingerprint, "applied_workspace")
        self._validate_state_shape()
        expected = canonical_fingerprint(self._public_without_fingerprint())
        if self.transaction_fingerprint is None:
            object.__setattr__(self, "transaction_fingerprint", expected)
        elif self.transaction_fingerprint != expected:
            raise SemanticProposalTransactionError("transaction_fingerprint_mismatch")
        if len(canonical_bytes(self.to_public_dict())) > MAX_SEMANTIC_TRANSACTION_WIRE_BYTES:
            raise SemanticProposalTransactionError("transaction_wire_limit")

    def _validate_state_shape(self) -> None:
        if self.state is SemanticProposalTransactionState.CLARIFICATION_REQUIRED:
            if not self.clarification_ids or self.resolution_fingerprints:
                raise SemanticProposalTransactionError("clarification_state")
        elif self.state is SemanticProposalTransactionState.READY_FOR_REVIEW:
            if self.clarification_ids and len(self.resolution_fingerprints) != len(
                self.clarification_ids
            ):
                raise SemanticProposalTransactionError("clarification_resolution")
        if self.state is SemanticProposalTransactionState.ACCEPTED:
            if self.applied_workspace_fingerprint is None:
                raise SemanticProposalTransactionError("accepted_workspace_missing")
        elif self.applied_workspace_fingerprint is not None:
            raise SemanticProposalTransactionError("nonaccepted_workspace_receipt")
        if self.state is SemanticProposalTransactionState.FAILED:
            if self.failure_code is None:
                raise SemanticProposalTransactionError("failure_code_missing")
        elif self.failure_code is not None:
            raise SemanticProposalTransactionError("nonfailed_failure_code")

    @property
    def fingerprint(self) -> str:
        if self.transaction_fingerprint is None:  # pragma: no cover
            raise SemanticProposalTransactionError("transaction_fingerprint_uninitialized")
        return self.transaction_fingerprint

    def _public_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction_id": self.transaction_id,
            "attempt": self.attempt,
            "revision": self.revision,
            "state": self.state.value,
            "last_action": self.last_action.value,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "segment_id": self.segment_id,
            "baseline_plan_fingerprint": self.baseline_plan_fingerprint,
            "baseline_graph_fingerprint": self.baseline_graph_fingerprint,
            "planning_baseline_fingerprint": self.planning_baseline_fingerprint,
            "planning_result_fingerprint": self.planning_result_fingerprint,
            "authorization_fingerprint": self.authorization_fingerprint,
            "graph_diff": self.graph_diff.to_public_dict(),
            "clarification_ids": list(self.clarification_ids),
            "resolution_fingerprints": list(self.resolution_fingerprints),
            "previous_transaction_fingerprint": self.previous_transaction_fingerprint,
            "failure_code": self.failure_code,
            "applied_workspace_fingerprint": self.applied_workspace_fingerprint,
        }

    def to_public_dict(self) -> dict[str, object]:
        value = self._public_without_fingerprint()
        value["transaction_fingerprint"] = self.fingerprint
        return value


@dataclass(frozen=True, slots=True)
class SemanticProposalApplyResult:
    transaction: SemanticProposalTransaction
    workspace: MultiSegmentWorkspace
    intent_graph: IntentGraph = field(repr=False)
    proposal_receipt_fingerprint: str
    schema: str = SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA:
            raise SemanticProposalTransactionError("unsupported_apply_result_schema")
        if (
            type(self.transaction) is not SemanticProposalTransaction
            or self.transaction.state is not SemanticProposalTransactionState.ACCEPTED
        ):
            raise SemanticProposalTransactionError("apply_transaction_state")
        if type(self.workspace) is not MultiSegmentWorkspace:
            raise SemanticProposalTransactionError("apply_workspace_type")
        if type(self.intent_graph) is not IntentGraph:
            raise SemanticProposalTransactionError("apply_graph_type")
        _fingerprint(self.proposal_receipt_fingerprint, "proposal_receipt")
        if self.transaction.applied_workspace_fingerprint != self.workspace.fingerprint:
            raise SemanticProposalTransactionError("apply_workspace_fingerprint")
        if canonical_fingerprint(self.intent_graph.to_wire()) != (
            self.transaction.graph_diff.after_graph_fingerprint
        ):
            raise SemanticProposalTransactionError("apply_graph_fingerprint")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "transaction": self.transaction.to_public_dict(),
            "workspace_fingerprint": self.workspace.fingerprint,
            "intent_graph_fingerprint": self.transaction.graph_diff.after_graph_fingerprint,
            "proposal_receipt_fingerprint": self.proposal_receipt_fingerprint,
        }


def _plan_fingerprint(plan: ContextPlan) -> str:
    # IMPORTANT: reuse the accepted semantic-enrichment authority projection. ContextPlan's public
    # wire intentionally contains raw duration floats that the strict canonicalizer must reject.
    return _enrichment_plan_fingerprint(plan)


def _enrichment_plan_fingerprint(plan: ContextPlan) -> str:
    return canonical_fingerprint(
        {
            "plan_id": plan.plan_id,
            "request": {
                "profile": plan.request.profile.to_wire(),
                "task_mode": plan.request.task_mode.value,
                "user_intent": plan.request.user_intent,
                "assets": [asset.to_wire() for asset in plan.request.assets],
                "registry": plan.request.reference_registry.to_wire(),
            },
            "hard_constraints": plan.hard_constraints.to_wire(),
            "evidence": plan.evidence.to_wire(),
            "intent_graph": plan.intent_graph.to_wire(),
        }
    )


def _segment(workspace: MultiSegmentWorkspace, segment_id: str) -> SegmentDeclaration:
    for item in workspace.segments:
        if item.segment_id == segment_id:
            return item
    raise SemanticProposalTransactionError("unknown_segment")


def _validate_planning_result(
    request: SemanticPlanningRequest,
    result: SemanticPlanningResult,
    authorization: SemanticProposalAuthorization,
) -> None:
    if type(request) is not SemanticPlanningRequest:
        raise SemanticProposalTransactionError("planning_request_type")
    if type(result) is not SemanticPlanningResult:
        raise SemanticProposalTransactionError("planning_result_type")
    if (
        result.status is not SemanticPlanningStatus.COMPLETE
        or not result.is_valid
        or result.document is None
        or result.diff is None
    ):
        raise SemanticProposalTransactionError("planning_result_incomplete")
    if (
        result.receipt.before_fingerprint != request.baseline_fingerprint
        or result.diff.before_fingerprint != request.baseline_fingerprint
        or result.receipt.after_fingerprint != result.document.fingerprint
        or result.diff.after_fingerprint != result.document.fingerprint
        or result.receipt.diff_fingerprint != result.diff.fingerprint
        or result.receipt.input_reduction_fingerprint != request.reduction_plan.fingerprint
        or result.receipt.input_timeline_fingerprint != request.timeline_plan.fingerprint
        or result.receipt.profile_id != request.profile.profile_id
        or result.receipt.model_digest != request.profile.model_digest
    ):
        raise SemanticProposalTransactionError("planning_receipt_mismatch")
    profile_fingerprint = canonical_fingerprint(request.profile.to_wire())
    if authorization.profile_fingerprint != profile_fingerprint:
        raise SemanticProposalTransactionError("authorization_profile_mismatch")
    uses_raw_media = bool(request.media_fingerprints)
    if result.receipt.raw_media_used is not uses_raw_media:
        raise SemanticProposalTransactionError("raw_media_receipt_mismatch")
    if uses_raw_media and (
        not authorization.raw_media_allowed or authorization.consent_fingerprint is None
    ):
        raise SemanticProposalTransactionError("raw_media_consent_required")


def _validate_correspondence(
    planning_result: SemanticPlanningResult,
    enrichment_result: SemanticEnrichmentResult,
) -> None:
    if type(enrichment_result) is not SemanticEnrichmentResult:
        raise SemanticProposalTransactionError("enrichment_result_type")
    if (
        enrichment_result.status is not SemanticEnrichmentStatus.APPLIED
        or not enrichment_result.is_complete
        or enrichment_result.after_plan is None
        or enrichment_result.conflicts
    ):
        raise SemanticProposalTransactionError("enrichment_candidate_incomplete")
    if planning_result.document is None:
        raise SemanticProposalTransactionError("planning_document_missing")
    planned = {item.proposal_id: item for item in planning_result.document.proposals}
    enriched = {item.proposal_id: item for item in enrichment_result.accepted}
    if len(planned) != len(planning_result.document.proposals) or len(enriched) != len(
        enrichment_result.accepted
    ):
        raise SemanticProposalTransactionError("proposal_correspondence")
    if set(planned) != set(enriched):
        raise SemanticProposalTransactionError("proposal_correspondence")
    for proposal_id, planned_item in planned.items():
        enriched_item = enriched[proposal_id]
        if not isinstance(planned_item.target_kind, SemanticTargetKind):
            raise SemanticProposalTransactionError("proposal_correspondence")
        if (
            planned_item.target_kind.value != enriched_item.target_kind.value
            or planned_item.target_id != enriched_item.target_id
            or planned_item.claim != enriched_item.text
            or enriched_item.evidence.evidence_id != proposal_id
        ):
            raise SemanticProposalTransactionError("proposal_correspondence")
        source_asset_id = enriched_item.evidence.provenance.source.asset_id
        if source_asset_id is not None and source_asset_id not in planned_item.source_ids:
            raise SemanticProposalTransactionError("proposal_correspondence")


def _build_graph_diff(
    baseline: IntentGraph,
    candidate: IntentGraph,
    planning_result: SemanticPlanningResult,
    enrichment_result: SemanticEnrichmentResult,
) -> SemanticIntentGraphDiff:
    before = baseline.to_wire()
    after = candidate.to_wire()
    for field_name in _PROTECTED_GRAPH_FIELDS:
        if before[field_name] != after[field_name]:
            raise SemanticProposalTransactionError(f"protected_graph_mutation:{field_name}")
    changed = tuple(
        field_name
        for field_name in _GRAPH_COLLECTION_ORDER
        if before[field_name] != after[field_name]
    )
    if planning_result.diff is None:
        raise SemanticProposalTransactionError("planning_diff_missing")
    return SemanticIntentGraphDiff(
        canonical_fingerprint(before),
        canonical_fingerprint(after),
        changed,
        planning_result.diff.fingerprint,
        canonical_fingerprint(enrichment_result.audit.to_wire()),
    )


def _build_transaction(
    *,
    workspace: MultiSegmentWorkspace,
    segment_id: str,
    baseline_plan: ContextPlan,
    planning_request: SemanticPlanningRequest,
    planning_result: SemanticPlanningResult,
    enrichment_result: SemanticEnrichmentResult,
    authorization: SemanticProposalAuthorization,
    transaction_id: str,
    attempt: int,
    revision: int,
    action: SemanticProposalAction,
    clarification_ids: tuple[str, ...],
    previous_transaction_fingerprint: str | None,
) -> SemanticProposalTransaction:
    if type(workspace) is not MultiSegmentWorkspace:
        raise SemanticProposalTransactionError("workspace_type")
    if type(baseline_plan) is not ContextPlan:
        raise SemanticProposalTransactionError("baseline_plan_type")
    if type(authorization) is not SemanticProposalAuthorization:
        raise SemanticProposalTransactionError("authorization_type")
    _identifier(transaction_id, "transaction_id")
    segment = _segment(workspace, segment_id)
    baseline_graph_fingerprint = canonical_fingerprint(baseline_plan.intent_graph.to_wire())
    if segment.accepted_intent_fingerprint != baseline_graph_fingerprint:
        raise SemanticProposalTransactionError("baseline_segment_authority_mismatch")
    reference_order = tuple(
        asset.asset_id for asset in baseline_plan.request.reference_registry.assets
    )
    duration_milliseconds = planning_request.timeline_plan.effective_duration.seconds * 1000
    duration_matches = (
        segment.duration.frame_count == baseline_plan.request.effective_frame_count
        if segment.duration.frame_count is not None
        else duration_milliseconds == duration_milliseconds.to_integral_value()
        and segment.duration.duration_milliseconds == int(duration_milliseconds)
    )
    expected_profile_fingerprint = canonical_fingerprint(planning_request.profile.to_wire())
    expected_registry_fingerprint = canonical_fingerprint(
        baseline_plan.request.reference_registry.to_wire()
    )
    if (
        segment.task_mode is not baseline_plan.request.task_mode
        or segment.reference_ids != reference_order
        or not duration_matches
        or segment.profile_fingerprint != expected_profile_fingerprint
        or segment.reference_registry_fingerprint != expected_registry_fingerprint
        or planning_request.timeline_plan.task_mode is not baseline_plan.request.task_mode
        or planning_request.timeline_plan.reference_order != reference_order
        or planning_request.timeline_plan.effective_duration
        != baseline_plan.intent_graph.effective_duration
    ):
        raise SemanticProposalTransactionError("baseline_contract_mismatch")
    if any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for item in baseline_plan.intent_graph.validate()
    ):
        raise SemanticProposalTransactionError("baseline_graph_invalid")
    _validate_planning_result(planning_request, planning_result, authorization)
    _validate_correspondence(planning_result, enrichment_result)
    if (
        _enrichment_plan_fingerprint(enrichment_result.before_plan)
        != (enrichment_result.audit.before_fingerprint)
        or _enrichment_plan_fingerprint(baseline_plan) != enrichment_result.audit.before_fingerprint
    ):
        raise SemanticProposalTransactionError("enrichment_baseline_mismatch")
    candidate_plan = enrichment_result.after_plan
    if candidate_plan is None:  # covered by correspondence; keeps type narrowing explicit
        raise SemanticProposalTransactionError("candidate_plan_missing")
    accepted_proposal_ids = tuple(item.proposal_id for item in enrichment_result.accepted)
    if (
        enrichment_result.audit.before_plan_id != baseline_plan.plan_id
        or enrichment_result.audit.after_plan_id != candidate_plan.plan_id
        or enrichment_result.audit.before_fingerprint != _enrichment_plan_fingerprint(baseline_plan)
        or enrichment_result.audit.after_fingerprint != _enrichment_plan_fingerprint(candidate_plan)
        or enrichment_result.audit.status is not enrichment_result.status
        or enrichment_result.audit.accepted_proposal_ids != accepted_proposal_ids
        or enrichment_result.audit.rejected_proposal_ids
    ):
        raise SemanticProposalTransactionError("enrichment_audit_mismatch")
    if (
        replace(candidate_plan.request, evidence=baseline_plan.request.evidence)
        != baseline_plan.request
        or candidate_plan.hard_constraints != baseline_plan.hard_constraints
        or candidate_plan.intent_graph.effective_duration
        != baseline_plan.intent_graph.effective_duration
        or tuple(
            (item.segment_id, item.start, item.end) for item in candidate_plan.intent_graph.segments
        )
        != tuple(
            (item.segment_id, item.start, item.end) for item in baseline_plan.intent_graph.segments
        )
    ):
        raise SemanticProposalTransactionError("candidate_contract_mutation")
    if any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for item in candidate_plan.intent_graph.validate()
    ):
        raise SemanticProposalTransactionError("candidate_graph_invalid")
    graph_diff = _build_graph_diff(
        baseline_plan.intent_graph,
        candidate_plan.intent_graph,
        planning_result,
        enrichment_result,
    )
    clarification_values = _identifiers(clarification_ids, "clarification_id")
    state = (
        SemanticProposalTransactionState.CLARIFICATION_REQUIRED
        if clarification_values
        else SemanticProposalTransactionState.READY_FOR_REVIEW
    )
    return SemanticProposalTransaction(
        transaction_id=transaction_id,
        attempt=attempt,
        revision=revision,
        state=state,
        last_action=action,
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        segment_id=segment_id,
        baseline_plan_fingerprint=_plan_fingerprint(baseline_plan),
        baseline_graph_fingerprint=baseline_graph_fingerprint,
        planning_baseline_fingerprint=planning_request.baseline_fingerprint,
        planning_result_fingerprint=canonical_fingerprint(planning_result.to_wire()),
        authorization_fingerprint=authorization.fingerprint,
        graph_diff=graph_diff,
        candidate_graph=candidate_plan.intent_graph,
        clarification_ids=clarification_values,
        previous_transaction_fingerprint=previous_transaction_fingerprint,
    )


def prepare_semantic_proposal_transaction(
    *,
    workspace: MultiSegmentWorkspace,
    segment_id: str,
    baseline_plan: ContextPlan,
    planning_request: SemanticPlanningRequest,
    planning_result: SemanticPlanningResult,
    enrichment_result: SemanticEnrichmentResult,
    authorization: SemanticProposalAuthorization,
    transaction_id: str,
    clarification_ids: tuple[str, ...] = (),
) -> SemanticProposalTransaction:
    return _build_transaction(
        workspace=workspace,
        segment_id=segment_id,
        baseline_plan=baseline_plan,
        planning_request=planning_request,
        planning_result=planning_result,
        enrichment_result=enrichment_result,
        authorization=authorization,
        transaction_id=transaction_id,
        attempt=1,
        revision=1,
        action=SemanticProposalAction.GENERATE,
        clarification_ids=clarification_ids,
        previous_transaction_fingerprint=None,
    )


def edit_semantic_proposal_transaction(
    transaction: SemanticProposalTransaction,
    *,
    expected_transaction_fingerprint: str,
    workspace: MultiSegmentWorkspace,
    baseline_plan: ContextPlan,
    planning_request: SemanticPlanningRequest,
    planning_result: SemanticPlanningResult,
    enrichment_result: SemanticEnrichmentResult,
    authorization: SemanticProposalAuthorization,
    clarification_ids: tuple[str, ...] = (),
) -> SemanticProposalTransaction:
    _require_active(transaction, expected_transaction_fingerprint)
    if transaction.revision >= MAX_SEMANTIC_PROPOSAL_REVISIONS:
        raise SemanticProposalTransactionError("revision_limit")
    _require_same_workspace(transaction, workspace)
    return _build_transaction(
        workspace=workspace,
        segment_id=transaction.segment_id,
        baseline_plan=baseline_plan,
        planning_request=planning_request,
        planning_result=planning_result,
        enrichment_result=enrichment_result,
        authorization=authorization,
        transaction_id=transaction.transaction_id,
        attempt=transaction.attempt,
        revision=transaction.revision + 1,
        action=SemanticProposalAction.EDIT,
        clarification_ids=clarification_ids,
        previous_transaction_fingerprint=transaction.fingerprint,
    )


def regenerate_semantic_proposal_transaction(
    transaction: SemanticProposalTransaction,
    *,
    expected_transaction_fingerprint: str,
    workspace: MultiSegmentWorkspace,
    baseline_plan: ContextPlan,
    planning_request: SemanticPlanningRequest,
    planning_result: SemanticPlanningResult,
    enrichment_result: SemanticEnrichmentResult,
    authorization: SemanticProposalAuthorization,
    transaction_id: str,
    clarification_ids: tuple[str, ...] = (),
) -> SemanticProposalTransaction:
    _require_active(transaction, expected_transaction_fingerprint)
    if transaction.attempt >= MAX_SEMANTIC_PROPOSAL_ATTEMPTS:
        raise SemanticProposalTransactionError("attempt_limit")
    if transaction_id == transaction.transaction_id:
        raise SemanticProposalTransactionError("regenerate_transaction_id")
    _require_same_workspace(transaction, workspace)
    return _build_transaction(
        workspace=workspace,
        segment_id=transaction.segment_id,
        baseline_plan=baseline_plan,
        planning_request=planning_request,
        planning_result=planning_result,
        enrichment_result=enrichment_result,
        authorization=authorization,
        transaction_id=transaction_id,
        attempt=transaction.attempt + 1,
        revision=1,
        action=SemanticProposalAction.REGENERATE,
        clarification_ids=clarification_ids,
        previous_transaction_fingerprint=transaction.fingerprint,
    )


def resolve_semantic_proposal_clarification(
    transaction: SemanticProposalTransaction,
    *,
    expected_transaction_fingerprint: str,
    resolution_fingerprints: tuple[str, ...],
) -> SemanticProposalTransaction:
    _require_current(transaction, expected_transaction_fingerprint)
    if transaction.state is not SemanticProposalTransactionState.CLARIFICATION_REQUIRED:
        raise SemanticProposalTransactionError("clarification_transition")
    values = _fingerprints(resolution_fingerprints, "resolution")
    if len(values) != len(transaction.clarification_ids):
        raise SemanticProposalTransactionError("clarification_resolution")
    return replace(
        transaction,
        state=SemanticProposalTransactionState.READY_FOR_REVIEW,
        last_action=SemanticProposalAction.CLARIFY,
        resolution_fingerprints=values,
        transaction_fingerprint=None,
    )


def accept_semantic_proposal_transaction(
    transaction: SemanticProposalTransaction,
    *,
    expected_transaction_fingerprint: str,
    workspace: MultiSegmentWorkspace,
    expected_workspace_fingerprint: str,
) -> SemanticProposalApplyResult:
    _require_current(transaction, expected_transaction_fingerprint)
    if transaction.state in _TERMINAL_STATES:
        raise SemanticProposalTransactionError("terminal_transaction")
    if transaction.state is not SemanticProposalTransactionState.READY_FOR_REVIEW:
        raise SemanticProposalTransactionError("not_review_ready")
    if type(workspace) is not MultiSegmentWorkspace:
        raise SemanticProposalTransactionError("workspace_type")
    _fingerprint(expected_workspace_fingerprint, "expected_workspace")
    if expected_workspace_fingerprint != workspace.fingerprint:
        raise SemanticProposalTransactionError("stale_workspace")
    _require_same_workspace(transaction, workspace)
    target = _segment(workspace, transaction.segment_id)
    if target.accepted_intent_fingerprint != transaction.baseline_graph_fingerprint:
        raise SemanticProposalTransactionError("stale_baseline_graph")
    receipt = transaction.fingerprint
    segments = tuple(
        replace(
            item,
            accepted_intent_fingerprint=transaction.graph_diff.after_graph_fingerprint,
            semantic_receipt_fingerprint=receipt,
        )
        if item.segment_id == transaction.segment_id
        else item
        for item in workspace.segments
    )
    authorities = tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in segments
    )
    revised = revise_workspace(
        workspace,
        expected_workspace_fingerprint=expected_workspace_fingerprint,
        accepted_intent_authorities=authorities,
        segments=segments,
    )
    accepted = replace(
        transaction,
        state=SemanticProposalTransactionState.ACCEPTED,
        last_action=SemanticProposalAction.ACCEPT,
        applied_workspace_fingerprint=revised.fingerprint,
        transaction_fingerprint=None,
    )
    return SemanticProposalApplyResult(
        accepted,
        revised,
        transaction.candidate_graph,
        receipt,
    )


def reject_semantic_proposal_transaction(
    transaction: SemanticProposalTransaction,
    *,
    expected_transaction_fingerprint: str,
) -> SemanticProposalTransaction:
    return _terminal_transition(
        transaction,
        expected_transaction_fingerprint,
        SemanticProposalTransactionState.REJECTED,
        SemanticProposalAction.REJECT,
    )


def cancel_semantic_proposal_transaction(
    transaction: SemanticProposalTransaction,
    *,
    expected_transaction_fingerprint: str,
) -> SemanticProposalTransaction:
    return _terminal_transition(
        transaction,
        expected_transaction_fingerprint,
        SemanticProposalTransactionState.CANCELLED,
        SemanticProposalAction.CANCEL,
    )


def fail_semantic_proposal_transaction(
    transaction: SemanticProposalTransaction,
    *,
    expected_transaction_fingerprint: str,
    failure_code: str,
) -> SemanticProposalTransaction:
    _require_active(transaction, expected_transaction_fingerprint)
    _identifier(failure_code, "failure_code")
    return replace(
        transaction,
        state=SemanticProposalTransactionState.FAILED,
        last_action=SemanticProposalAction.FAIL,
        failure_code=failure_code,
        transaction_fingerprint=None,
    )


def _terminal_transition(
    transaction: SemanticProposalTransaction,
    expected_transaction_fingerprint: str,
    state: SemanticProposalTransactionState,
    action: SemanticProposalAction,
) -> SemanticProposalTransaction:
    _require_active(transaction, expected_transaction_fingerprint)
    return replace(
        transaction,
        state=state,
        last_action=action,
        transaction_fingerprint=None,
    )


def _require_same_workspace(
    transaction: SemanticProposalTransaction, workspace: MultiSegmentWorkspace
) -> None:
    if type(workspace) is not MultiSegmentWorkspace:
        raise SemanticProposalTransactionError("workspace_type")
    if (
        transaction.workspace_id != workspace.workspace_id
        or transaction.workspace_revision != workspace.revision
        or transaction.workspace_fingerprint != workspace.fingerprint
    ):
        raise SemanticProposalTransactionError("stale_workspace")


def _require_active(
    transaction: SemanticProposalTransaction, expected_transaction_fingerprint: str
) -> None:
    _require_current(transaction, expected_transaction_fingerprint)
    if transaction.state in _TERMINAL_STATES:
        raise SemanticProposalTransactionError("terminal_transaction")


def _require_current(
    transaction: SemanticProposalTransaction, expected_transaction_fingerprint: str
) -> None:
    if type(transaction) is not SemanticProposalTransaction:
        raise SemanticProposalTransactionError("transaction_type")
    _verify_transaction_integrity(transaction)
    _fingerprint(expected_transaction_fingerprint, "expected_transaction")
    if transaction.fingerprint != expected_transaction_fingerprint:
        raise SemanticProposalTransactionError("stale_transaction")


def _verify_transaction_integrity(transaction: SemanticProposalTransaction) -> None:
    if transaction.fingerprint != canonical_fingerprint(transaction._public_without_fingerprint()):
        raise SemanticProposalTransactionError("tampered_transaction")
    if canonical_fingerprint(transaction.candidate_graph.to_wire()) != (
        transaction.graph_diff.after_graph_fingerprint
    ):
        raise SemanticProposalTransactionError("tampered_transaction")


__all__ = [
    "MAX_SEMANTIC_PROPOSAL_ATTEMPTS",
    "MAX_SEMANTIC_PROPOSAL_REVISIONS",
    "MAX_SEMANTIC_TRANSACTION_WIRE_BYTES",
    "SEMANTIC_PROPOSAL_TRANSACTION_SCHEMA",
    "SemanticIntentGraphDiff",
    "SemanticProposalAction",
    "SemanticProposalApplyResult",
    "SemanticProposalAuthorization",
    "SemanticProposalTransaction",
    "SemanticProposalTransactionError",
    "SemanticProposalTransactionState",
    "accept_semantic_proposal_transaction",
    "cancel_semantic_proposal_transaction",
    "edit_semantic_proposal_transaction",
    "fail_semantic_proposal_transaction",
    "prepare_semantic_proposal_transaction",
    "regenerate_semantic_proposal_transaction",
    "reject_semantic_proposal_transaction",
    "resolve_semantic_proposal_clarification",
]
