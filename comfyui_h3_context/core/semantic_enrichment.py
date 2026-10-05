"""Constrained, provenance-aware semantic additions for accepted context plans.

This module never calls a reasoning runtime itself. It validates proposals from an explicitly
injected M5-01 local adapter, applies only additive graph-description changes, and records an
immutable before/after audit. User intent, hard constraints, timing, asset identity, and reference
order are snapshotted and fail closed if an enrichment attempt could change them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol, cast, runtime_checkable

from .canonical import canonical_fingerprint
from .context_reporting import ContextPlan, PlanStage, PlanStep, PlanStepStatus
from .contracts import ValidationDiagnostic, ValidationSeverity
from .errors import ContractValidationError, SemanticEnrichmentError
from .evidence import EvidenceOrigin, EvidenceRecord, EvidenceSet, merge_evidence
from .intent_graph import (
    AudioIntent,
    CameraIntent,
    IntentAction,
    IntentGraph,
    IntentScene,
    IntentSubject,
    StyleIntent,
)
from .local_adapters import (
    LocalAdapterDescriptor,
    LocalAdapterExecutionRequest,
    LocalAdapterResult,
    LocalAdapterRuntime,
    LocalBudgetGuard,
    LocalCancellationProbe,
    LocalDeviceKind,
    LocalDeviceSpec,
    run_local_adapter,
)

SEMANTIC_ENRICHMENT_SCHEMA = "h3.semantic.enrichment.v1"
MAX_ENRICHMENT_PROPOSALS = 64
MAX_ENRICHMENT_TEXT_LENGTH = 4096
MAX_ENRICHMENT_CONFLICTS = 64
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FORBIDDEN_TEXT_MARKERS = (
    "http://",
    "https://",
    "file://",
    "authorization",
    "bearer ",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token=",
)
_DescribedIntent = (
    IntentSubject | IntentScene | IntentAction | CameraIntent | StyleIntent | AudioIntent
)


class EnrichmentTargetKind(str, Enum):
    """Closed graph targets; protected hard fields remain representable for explicit rejection."""

    SUBJECT = "subject"
    SCENE = "scene"
    ACTION = "action"
    CAMERA = "camera"
    STYLE = "style"
    AUDIO = "audio"
    DIALOGUE = "dialogue"
    LYRICS = "lyrics"
    VISIBLE_TEXT = "visible_text"
    TIMING = "timing"
    ASSET = "asset"


class SemanticEnrichmentStatus(str, Enum):
    """Terminal status distinguishing complete, partial, rejected, and no-op enrichment."""

    NOOP = "noop"
    APPLIED = "applied"
    PARTIAL = "partial"
    REJECTED = "rejected"


_PROTECTED_TARGETS = frozenset(
    {
        EnrichmentTargetKind.DIALOGUE,
        EnrichmentTargetKind.LYRICS,
        EnrichmentTargetKind.VISIBLE_TEXT,
        EnrichmentTargetKind.TIMING,
        EnrichmentTargetKind.ASSET,
    }
)


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise SemanticEnrichmentError(f"{field_name} must be a bounded identifier")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ENRICHMENT_TEXT_LENGTH:
        raise SemanticEnrichmentError(
            f"{field_name} must be a non-empty string of at most "
            f"{MAX_ENRICHMENT_TEXT_LENGTH} characters"
        )
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise SemanticEnrichmentError(f"{field_name} contains an unsafe wire code point")
    if any(marker in value.casefold() for marker in _FORBIDDEN_TEXT_MARKERS):
        raise SemanticEnrichmentError(
            f"{field_name} contains a forbidden locator or credential marker"
        )
    return value


@dataclass(frozen=True, slots=True)
class SemanticProposal:
    """One additive, untrusted semantic proposal tied to an explicit evidence record."""

    proposal_id: str
    target_kind: EnrichmentTargetKind
    target_id: str
    text: str
    evidence: EvidenceRecord
    untrusted: bool = True
    schema: str = SEMANTIC_ENRICHMENT_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.proposal_id, "proposal_id")
        if self.schema != SEMANTIC_ENRICHMENT_SCHEMA:
            raise SemanticEnrichmentError("unsupported semantic enrichment schema")
        if not isinstance(self.target_kind, EnrichmentTargetKind):
            raise SemanticEnrichmentError("target_kind must be an EnrichmentTargetKind")
        _identifier(self.target_id, "target_id")
        _text(self.text, "proposal text")
        if not isinstance(self.evidence, EvidenceRecord):
            raise SemanticEnrichmentError("proposal evidence must be an EvidenceRecord")
        if self.evidence.origin is not EvidenceOrigin.ASSISTED_PROPOSAL:
            raise SemanticEnrichmentError("semantic proposal evidence must be ASSISTED_PROPOSAL")
        if self.evidence.claim != self.text:
            raise SemanticEnrichmentError("proposal text must match its evidence claim")
        if not isinstance(self.untrusted, bool) or not self.untrusted:
            raise SemanticEnrichmentError("semantic proposals must remain untrusted")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "proposal_id": self.proposal_id,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "text": self.text,
            "evidence": self.evidence.to_wire(),
            "untrusted": self.untrusted,
        }


@dataclass(frozen=True, slots=True)
class EnrichmentConflict:
    """One rejected proposal with a stable code and no silent auto-resolution."""

    conflict_id: str
    proposal_id: str
    code: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.ERROR
    schema: str = SEMANTIC_ENRICHMENT_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.conflict_id, "conflict_id")
        _identifier(self.proposal_id, "proposal_id")
        _identifier(self.code, "conflict code")
        _text(self.message, "conflict message")
        if not isinstance(self.severity, ValidationSeverity):
            raise SemanticEnrichmentError("conflict severity must be a ValidationSeverity")
        if self.schema != SEMANTIC_ENRICHMENT_SCHEMA:
            raise SemanticEnrichmentError("unsupported semantic conflict schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "conflict_id": self.conflict_id,
            "proposal_id": self.proposal_id,
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
        }


@dataclass(frozen=True, slots=True)
class EnrichmentAudit:
    """Reproducible before/after identity and proposal decision record."""

    audit_id: str
    before_plan_id: str
    after_plan_id: str | None
    before_fingerprint: str
    after_fingerprint: str
    accepted_proposal_ids: tuple[str, ...]
    rejected_proposal_ids: tuple[str, ...]
    status: SemanticEnrichmentStatus = SemanticEnrichmentStatus.NOOP
    schema: str = SEMANTIC_ENRICHMENT_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.audit_id, "audit_id")
        _identifier(self.before_plan_id, "before_plan_id")
        if self.after_plan_id is not None:
            _identifier(self.after_plan_id, "after_plan_id")
        for value, field_name in (
            (self.before_fingerprint, "before_fingerprint"),
            (self.after_fingerprint, "after_fingerprint"),
        ):
            if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
                raise SemanticEnrichmentError(f"{field_name} must be a SHA-256 fingerprint")
        for values, field_name in (
            (self.accepted_proposal_ids, "accepted_proposal_ids"),
            (self.rejected_proposal_ids, "rejected_proposal_ids"),
        ):
            if not isinstance(values, tuple) or len(values) > MAX_ENRICHMENT_PROPOSALS:
                raise SemanticEnrichmentError(f"{field_name} is outside its finite limit")
            if len(values) != len(set(values)) or not all(
                _IDENTIFIER_PATTERN.fullmatch(value) for value in values
            ):
                raise SemanticEnrichmentError(f"{field_name} must contain unique identifiers")
        if set(self.accepted_proposal_ids) & set(self.rejected_proposal_ids):
            raise SemanticEnrichmentError("a proposal cannot be both accepted and rejected")
        if not isinstance(self.status, SemanticEnrichmentStatus):
            raise SemanticEnrichmentError("audit status must be a SemanticEnrichmentStatus")
        if self.schema != SEMANTIC_ENRICHMENT_SCHEMA:
            raise SemanticEnrichmentError("unsupported semantic audit schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "audit_id": self.audit_id,
            "before_plan_id": self.before_plan_id,
            "after_plan_id": self.after_plan_id,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "accepted_proposal_ids": list(self.accepted_proposal_ids),
            "rejected_proposal_ids": list(self.rejected_proposal_ids),
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class SemanticEnrichmentResult:
    """Immutable enrichment decision with explicit partial/rejected semantics."""

    before_plan: ContextPlan
    after_plan: ContextPlan | None
    status: SemanticEnrichmentStatus
    accepted: tuple[SemanticProposal, ...]
    conflicts: tuple[EnrichmentConflict, ...]
    audit: EnrichmentAudit
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.before_plan, ContextPlan):
            raise SemanticEnrichmentError("before_plan must be a ContextPlan")
        if self.after_plan is not None and not isinstance(self.after_plan, ContextPlan):
            raise SemanticEnrichmentError("after_plan must be a ContextPlan or None")
        if not isinstance(self.status, SemanticEnrichmentStatus):
            raise SemanticEnrichmentError("status must be a SemanticEnrichmentStatus")
        if not isinstance(self.accepted, tuple) or not all(
            isinstance(item, SemanticProposal) for item in self.accepted
        ):
            raise SemanticEnrichmentError("accepted must be a tuple of SemanticProposal values")
        if not isinstance(self.conflicts, tuple) or not all(
            isinstance(item, EnrichmentConflict) for item in self.conflicts
        ):
            raise SemanticEnrichmentError("conflicts must be a tuple of EnrichmentConflict values")
        if len(self.conflicts) > MAX_ENRICHMENT_CONFLICTS:
            raise SemanticEnrichmentError("conflicts exceed their finite limit")
        if not isinstance(self.audit, EnrichmentAudit):
            raise SemanticEnrichmentError("audit must be an EnrichmentAudit")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise SemanticEnrichmentError(
                "diagnostics must be a tuple of ValidationDiagnostic values"
            )
        if self.status is SemanticEnrichmentStatus.REJECTED and self.after_plan is not None:
            raise SemanticEnrichmentError("rejected enrichment cannot carry an after plan")
        if (
            self.status
            in {
                SemanticEnrichmentStatus.APPLIED,
                SemanticEnrichmentStatus.PARTIAL,
                SemanticEnrichmentStatus.NOOP,
            }
            and self.after_plan is None
        ):
            raise SemanticEnrichmentError("non-rejected enrichment requires an after plan")

    @property
    def is_complete(self) -> bool:
        return self.status in {SemanticEnrichmentStatus.APPLIED, SemanticEnrichmentStatus.NOOP}

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": SEMANTIC_ENRICHMENT_SCHEMA,
            "before_plan": self.before_plan.to_wire(),
            "after_plan": None if self.after_plan is None else self.after_plan.to_wire(),
            "status": self.status.value,
            "accepted": [item.to_wire() for item in self.accepted],
            "conflicts": [item.to_wire() for item in self.conflicts],
            "audit": self.audit.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


def _plan_fingerprint(plan: ContextPlan) -> str:
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


def _conflict(proposal: SemanticProposal, code: str, message: str) -> EnrichmentConflict:
    return EnrichmentConflict(
        conflict_id=f"conflict.{proposal.proposal_id}",
        proposal_id=proposal.proposal_id,
        code=code,
        message=message,
    )


def _target_exists(graph: IntentGraph, proposal: SemanticProposal) -> bool:
    collection: tuple[object, ...]
    field: str
    if proposal.target_kind is EnrichmentTargetKind.SCENE:
        collection, field = graph.scenes, "scene_id"
    elif proposal.target_kind is EnrichmentTargetKind.SUBJECT:
        collection, field = graph.subjects, "subject_id"
    elif proposal.target_kind is EnrichmentTargetKind.ACTION:
        collection, field = graph.actions, "action_id"
    elif proposal.target_kind is EnrichmentTargetKind.CAMERA:
        collection, field = graph.cameras, "camera_id"
    elif proposal.target_kind is EnrichmentTargetKind.STYLE:
        collection, field = graph.styles, "style_id"
    elif proposal.target_kind is EnrichmentTargetKind.AUDIO:
        collection, field = graph.audios, "audio_id"
    else:
        return False
    return any(getattr(item, field, None) == proposal.target_id for item in collection)


def _append_description(graph: IntentGraph, proposal: SemanticProposal) -> IntentGraph:
    def append(
        values: tuple[_DescribedIntent, ...], field: str, identifier_field: str
    ) -> tuple[_DescribedIntent, ...]:
        output: list[_DescribedIntent] = []
        found = False
        for value in values:
            if getattr(value, identifier_field) != proposal.target_id:
                output.append(value)
                continue
            found = True
            current = getattr(value, field)
            updated = proposal.text if current is None else f"{current} {proposal.text}"
            output.append(cast(_DescribedIntent, replace(cast(Any, value), **{field: updated})))
        if not found:
            raise SemanticEnrichmentError("validated enrichment target disappeared before mutation")
        return tuple(output)

    if proposal.target_kind is EnrichmentTargetKind.SCENE:
        return replace(
            graph,
            scenes=cast(tuple[IntentScene, ...], append(graph.scenes, "description", "scene_id")),
        )
    if proposal.target_kind is EnrichmentTargetKind.SUBJECT:
        return replace(
            graph,
            subjects=cast(
                tuple[IntentSubject, ...], append(graph.subjects, "description", "subject_id")
            ),
        )
    if proposal.target_kind is EnrichmentTargetKind.ACTION:
        return replace(
            graph,
            actions=cast(
                tuple[IntentAction, ...], append(graph.actions, "description", "action_id")
            ),
        )
    if proposal.target_kind is EnrichmentTargetKind.CAMERA:
        return replace(
            graph,
            cameras=cast(
                tuple[CameraIntent, ...], append(graph.cameras, "description", "camera_id")
            ),
        )
    if proposal.target_kind is EnrichmentTargetKind.STYLE:
        return replace(
            graph,
            styles=cast(tuple[StyleIntent, ...], append(graph.styles, "description", "style_id")),
        )
    if proposal.target_kind is EnrichmentTargetKind.AUDIO:
        return replace(
            graph,
            audios=cast(tuple[AudioIntent, ...], append(graph.audios, "description", "audio_id")),
        )
    raise SemanticEnrichmentError("protected enrichment target cannot be appended")


def _enriched_plan_id(
    plan: ContextPlan,
    graph: IntentGraph,
    evidence: EvidenceSet,
    accepted: tuple[str, ...],
) -> str:
    fingerprint = canonical_fingerprint(
        {
            "schema": SEMANTIC_ENRICHMENT_SCHEMA,
            "before_plan_id": plan.plan_id,
            "graph": graph.to_wire(),
            "evidence": evidence.to_wire(),
            "accepted": list(accepted),
        }
    )
    return "plan_" + fingerprint.split(":", 1)[1][:32]


def _build_after_plan(
    plan: ContextPlan,
    graph: IntentGraph,
    evidence: EvidenceSet,
    accepted: tuple[SemanticProposal, ...],
) -> ContextPlan:
    plan_request = replace(plan.request, evidence=evidence)
    accepted_ids = tuple(item.proposal_id for item in accepted)
    insert_at = next(
        (
            index
            for index, step in enumerate(plan.steps)
            if step.stage in {PlanStage.RENDER, PlanStage.VALIDATE, PlanStage.PROVIDER}
        ),
        len(plan.steps),
    )
    enrichment_step = PlanStep(
        "step_semantic_enrichment",
        PlanStage.ASSEMBLE_INTENT,
        PlanStepStatus.COMPLETED,
        "append provenance-aware semantic proposals",
        input_evidence_ids=accepted_ids,
        output_ids=tuple(item.target_id for item in accepted),
    )
    steps = plan.steps[:insert_at] + (enrichment_step,) + plan.steps[insert_at:]
    return ContextPlan(
        plan_id=_enriched_plan_id(plan, graph, evidence, accepted_ids),
        schema_version=plan.schema_version,
        request=plan_request,
        intent_graph=graph,
        hard_constraints=plan.hard_constraints,
        evidence=evidence,
        steps=steps,
        limitations=plan.limitations,
        diagnostics=plan.diagnostics,
    )


def apply_semantic_enrichment(
    plan: ContextPlan,
    proposals: tuple[SemanticProposal, ...],
) -> SemanticEnrichmentResult:
    """Apply only validated additive proposals and retain every conflict in an audit."""

    if not isinstance(plan, ContextPlan):
        raise SemanticEnrichmentError("plan must be a ContextPlan")
    if not isinstance(proposals, tuple) or len(proposals) > MAX_ENRICHMENT_PROPOSALS:
        raise SemanticEnrichmentError("proposals must be a bounded tuple")
    if not all(isinstance(item, SemanticProposal) for item in proposals):
        raise SemanticEnrichmentError("proposals must contain SemanticProposal values")

    before_fingerprint = _plan_fingerprint(plan)
    accepted: list[SemanticProposal] = []
    conflicts: list[EnrichmentConflict] = []
    proposal_counts: dict[str, int] = {}
    for item in proposals:
        proposal_counts[item.proposal_id] = proposal_counts.get(item.proposal_id, 0) + 1
    duplicate_ids = {proposal_id for proposal_id, count in proposal_counts.items() if count > 1}
    reported_duplicate_ids: set[str] = set()
    evidence = plan.evidence
    graph = plan.intent_graph
    for item in proposals:
        if item.proposal_id in duplicate_ids:
            if item.proposal_id not in reported_duplicate_ids:
                conflicts.append(
                    _conflict(item, "duplicate_proposal", "proposal ID was received more than once")
                )
                reported_duplicate_ids.add(item.proposal_id)
            continue
        if item.target_kind in _PROTECTED_TARGETS:
            conflicts.append(
                _conflict(
                    item,
                    "protected_target",
                    "dialogue, visible text, timing, and asset fields are immutable",
                )
            )
            continue
        if not _target_exists(graph, item):
            conflicts.append(
                _conflict(
                    item, "target_not_found", "proposal target is not present in the intent graph"
                )
            )
            continue
        source_asset_id = item.evidence.provenance.source.asset_id
        if source_asset_id is not None and source_asset_id not in {
            asset.asset_id for asset in plan.request.reference_registry.assets
        }:
            conflicts.append(
                _conflict(item, "evidence_unowned", "proposal evidence names an unowned asset")
            )
            continue
        if any(
            forbidden.content in item.text for forbidden in plan.hard_constraints.forbidden_content
        ):
            conflicts.append(
                _conflict(item, "hard_constraint_conflict", "proposal would add forbidden content")
            )
            continue
        try:
            evidence = merge_evidence(evidence, EvidenceSet((item.evidence,)))
        except ContractValidationError as exc:
            conflicts.append(_conflict(item, "evidence_conflict", str(exc)))
            continue
        graph = _append_description(graph, item)
        accepted.append(item)

    accepted_tuple = tuple(accepted)
    conflicts_tuple = tuple(conflicts)
    if accepted_tuple:
        graph_diagnostics = graph.validate()
        if any(
            diagnostic.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for diagnostic in graph_diagnostics
        ):
            detail = "; ".join(
                f"{diagnostic.code}: {diagnostic.message}" for diagnostic in graph_diagnostics
            )
            raise SemanticEnrichmentError(
                f"semantic enrichment produced an invalid graph: {detail}"
            )
        after_plan = _build_after_plan(plan, graph, evidence, accepted_tuple)
        status = (
            SemanticEnrichmentStatus.PARTIAL
            if conflicts_tuple
            else SemanticEnrichmentStatus.APPLIED
        )
    elif conflicts_tuple:
        after_plan = None
        status = SemanticEnrichmentStatus.REJECTED
    else:
        after_plan = plan
        status = SemanticEnrichmentStatus.NOOP
    after_fingerprint = before_fingerprint if after_plan is None else _plan_fingerprint(after_plan)
    audit = EnrichmentAudit(
        audit_id="audit_"
        + canonical_fingerprint(
            {
                "before": before_fingerprint,
                "after": after_fingerprint,
                "accepted": [item.proposal_id for item in accepted_tuple],
                "rejected": [item.proposal_id for item in conflicts_tuple],
                "status": status.value,
            }
        ).split(":", 1)[1][:32],
        before_plan_id=plan.plan_id,
        after_plan_id=None if after_plan is None else after_plan.plan_id,
        before_fingerprint=before_fingerprint,
        after_fingerprint=after_fingerprint,
        accepted_proposal_ids=tuple(item.proposal_id for item in accepted_tuple),
        rejected_proposal_ids=tuple(item.proposal_id for item in conflicts_tuple),
        status=status,
    )
    if after_plan is not None:
        if after_plan.request.user_intent != plan.request.user_intent:
            raise SemanticEnrichmentError("semantic enrichment changed user intent")
        if after_plan.hard_constraints != plan.hard_constraints:
            raise SemanticEnrichmentError("semantic enrichment changed hard constraints")
        if after_plan.request.reference_registry != plan.request.reference_registry:
            raise SemanticEnrichmentError("semantic enrichment changed reference ownership")
        before_bounds = tuple(
            (item.segment_id, item.start, item.end) for item in plan.intent_graph.segments
        )
        after_bounds = tuple(
            (item.segment_id, item.start, item.end) for item in after_plan.intent_graph.segments
        )
        if before_bounds != after_bounds:
            raise SemanticEnrichmentError("semantic enrichment changed timeline bounds")
    return SemanticEnrichmentResult(
        before_plan=plan,
        after_plan=after_plan,
        status=status,
        accepted=accepted_tuple,
        conflicts=conflicts_tuple,
        audit=audit,
    )


@runtime_checkable
class SemanticEnrichmentAdapter(Protocol):
    """Protocol-like runtime seam implemented by an optional local reasoning adapter."""

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        """Return the local adapter capability descriptor."""

    def propose(self, plan: ContextPlan, guard: LocalBudgetGuard) -> tuple[SemanticProposal, ...]:
        """Return bounded proposals without mutating the input plan."""


class _SemanticAdapterBridge:
    def __init__(self, adapter: SemanticEnrichmentAdapter) -> None:
        self._adapter = adapter

    @property
    def descriptor(self) -> LocalAdapterDescriptor:
        return self._adapter.descriptor

    def run(
        self,
        request: LocalAdapterExecutionRequest,
        guard: LocalBudgetGuard,
    ) -> LocalAdapterResult:
        if not isinstance(request.input_value, ContextPlan):
            raise SemanticEnrichmentError("semantic adapter input is not a ContextPlan")
        values = self._adapter.propose(request.input_value, guard)
        if not isinstance(values, tuple) or not all(
            isinstance(item, SemanticProposal) for item in values
        ):
            raise SemanticEnrichmentError("semantic adapter returned invalid proposal values")
        output_bytes = sum(len(item.text.encode("utf-8")) for item in values)
        return LocalAdapterResult(
            adapter_id=self.descriptor.adapter_id,
            adapter_version=self.descriptor.adapter_version,
            device=request.device,
            value=values,
            output_bytes=output_bytes,
            output_items=len(values),
        )


def execute_semantic_enrichment(
    adapter: SemanticEnrichmentAdapter,
    plan: ContextPlan,
    *,
    runtime: LocalAdapterRuntime | None = None,
    device: LocalDeviceSpec | None = None,
    deterministic_required: bool = True,
    seed: int | None = None,
    cancellation_probe: LocalCancellationProbe | None = None,
    clock: Callable[[], float] | None = None,
    memory_meter: Callable[[], int] | None = None,
) -> SemanticEnrichmentResult:
    """Run one explicit local reasoner through M5-01 budgets before applying proposals."""

    if not isinstance(adapter, SemanticEnrichmentAdapter):
        raise SemanticEnrichmentError("adapter must implement SemanticEnrichmentAdapter")
    if not isinstance(plan, ContextPlan):
        raise SemanticEnrichmentError("plan must be a ContextPlan")
    device_value = LocalDeviceSpec(LocalDeviceKind.AUTO) if device is None else device
    if not isinstance(device_value, LocalDeviceSpec):
        raise SemanticEnrichmentError("device must be a LocalDeviceSpec")
    execution_request = LocalAdapterExecutionRequest(
        adapter_id=adapter.descriptor.adapter_id,
        task_mode=plan.request.task_mode,
        reference_count=len(plan.request.reference_registry.assets),
        device=device_value,
        deterministic_required=deterministic_required,
        seed=seed,
        cancellation_required=cancellation_probe is not None,
        input_value=plan,
    )
    bridge = _SemanticAdapterBridge(adapter)
    if clock is None:
        result = run_local_adapter(
            bridge,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            memory_meter=memory_meter,
        )
    else:
        result = run_local_adapter(
            bridge,
            execution_request,
            runtime=runtime,
            cancellation_probe=cancellation_probe,
            clock=clock,
            memory_meter=memory_meter,
        )
    if not isinstance(result.value, tuple) or not all(
        isinstance(item, SemanticProposal) for item in result.value
    ):
        raise SemanticEnrichmentError("semantic adapter result does not contain proposals")
    return apply_semantic_enrichment(plan, result.value)


__all__ = [
    "SEMANTIC_ENRICHMENT_SCHEMA",
    "MAX_ENRICHMENT_CONFLICTS",
    "MAX_ENRICHMENT_PROPOSALS",
    "MAX_ENRICHMENT_TEXT_LENGTH",
    "EnrichmentAudit",
    "EnrichmentConflict",
    "EnrichmentTargetKind",
    "SemanticEnrichmentAdapter",
    "SemanticEnrichmentResult",
    "SemanticEnrichmentStatus",
    "SemanticProposal",
    "apply_semantic_enrichment",
    "execute_semantic_enrichment",
]
