"""Deterministic, target-directed hierarchical evidence reduction.

This module selects caller-owned typed evidence under explicit finite budgets. It never generates
summaries, infers missing facts, chooses a provider, or mutates the accepted evidence graph or
feasible audiovisual timeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .contracts import ValidationDiagnostic, ValidationSeverity
from .errors import HierarchicalEvidenceReductionError
from .feasible_av_timeline_planner import FeasibleAVTimelinePlan, TimelinePlannerStatus
from .unified_evidence_graph import GraphStatus, UnifiedEvidenceGraph

HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA = "h3.hierarchical_evidence_reduction.v1"
MAX_REDUCTION_ITEMS = 512
MAX_REDUCTION_SOURCE_IDS = 256
MAX_REDUCTION_TEXT = 4096
MAX_REDUCTION_RATIONALE = 128
MAX_REDUCTION_OUTPUT_BYTES = 262_144

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE = (
    "http://",
    "https://",
    "file://",
    "/mnt/",
    "c:\\",
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
)


class ReductionStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CONFLICTING = "conflicting"


class ReductionLevel(str, Enum):
    ASSET = "asset"
    SHOT = "shot"
    SEGMENT = "segment"
    MODALITY_SUMMARY = "modality_summary"
    CROSS_MODAL_RELATION = "cross_modal_relation"


class ReductionModality(str, Enum):
    VISUAL = "visual"
    AUDIO = "audio"
    TEXT = "text"
    MULTIMODAL = "multimodal"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise HierarchicalEvidenceReductionError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise HierarchicalEvidenceReductionError(
            f"{field_name} contains sensitive or locator material"
        )
    return value


def _text(value: object, field_name: str, maximum: int = MAX_REDUCTION_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise HierarchicalEvidenceReductionError(f"{field_name} must be bounded non-empty text")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise HierarchicalEvidenceReductionError(
            f"{field_name} contains sensitive or locator material"
        )
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise HierarchicalEvidenceReductionError(f"{field_name} contains an unsafe wire code point")
    return value


def _ids(
    values: object, field_name: str, maximum: int = MAX_REDUCTION_SOURCE_IDS
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise HierarchicalEvidenceReductionError(f"{field_name} is outside the bounded envelope")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise HierarchicalEvidenceReductionError(f"{field_name} must not contain duplicates")
    return result


def _enum(value: object, expected: type[Enum], field_name: str) -> Enum:
    if isinstance(value, expected):
        return value
    try:
        return expected(value)
    except (TypeError, ValueError):
        raise HierarchicalEvidenceReductionError(f"{field_name} is unsupported") from None


def _decimal(value: object, field_name: str) -> Decimal:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < Decimal("0")
        or value > Decimal("1")
    ):
        raise HierarchicalEvidenceReductionError(
            f"{field_name} must be a finite Decimal between 0 and 1"
        )
    return value


def _positive_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise HierarchicalEvidenceReductionError(f"{field_name} is outside its finite bound")
    return value


def _nonnegative_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise HierarchicalEvidenceReductionError(f"{field_name} is outside its finite bound")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise HierarchicalEvidenceReductionError(f"{field_name} must be a SHA-256 fingerprint")
    return value


@dataclass(frozen=True, slots=True)
class ReductionBudget:
    """Finite output and structural-quality budgets for one reduction run."""

    max_output_items: int = MAX_REDUCTION_ITEMS
    max_output_tokens: int = 65_536
    min_source_coverage: Decimal = Decimal("0.80")
    min_target_coverage: Decimal = Decimal("0.80")
    min_fidelity: Decimal = Decimal("0.80")
    schema: str = HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA

    def __post_init__(self) -> None:
        _positive_int(self.max_output_items, "max_output_items", MAX_REDUCTION_ITEMS)
        _positive_int(self.max_output_tokens, "max_output_tokens", 4_000_000)
        _decimal(self.min_source_coverage, "min_source_coverage")
        _decimal(self.min_target_coverage, "min_target_coverage")
        _decimal(self.min_fidelity, "min_fidelity")
        if self.schema != HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA:
            raise HierarchicalEvidenceReductionError("unsupported reduction budget schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_output_items": self.max_output_items,
            "max_output_tokens": self.max_output_tokens,
            "min_source_coverage": format(self.min_source_coverage, "f"),
            "min_target_coverage": format(self.min_target_coverage, "f"),
            "min_fidelity": format(self.min_fidelity, "f"),
        }


@dataclass(frozen=True, slots=True)
class ReductionTarget:
    """Explicit target focus and protection IDs; no target is inferred from prose."""

    target_id: str
    focus_source_ids: tuple[str, ...] = ()
    required_item_ids: tuple[str, ...] = ()
    hard_constraint_ids: tuple[str, ...] = ()
    high_risk_ids: tuple[str, ...] = ()
    schema: str = HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.target_id, "target_id")
        object.__setattr__(
            self, "focus_source_ids", _ids(self.focus_source_ids, "focus_source_ids")
        )
        object.__setattr__(
            self, "required_item_ids", _ids(self.required_item_ids, "required_item_ids")
        )
        object.__setattr__(
            self, "hard_constraint_ids", _ids(self.hard_constraint_ids, "hard_constraint_ids")
        )
        object.__setattr__(self, "high_risk_ids", _ids(self.high_risk_ids, "high_risk_ids"))
        if self.schema != HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA:
            raise HierarchicalEvidenceReductionError("unsupported reduction target schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "target_id": self.target_id,
            "focus_source_ids": list(self.focus_source_ids),
            "required_item_ids": list(self.required_item_ids),
            "hard_constraint_ids": list(self.hard_constraint_ids),
            "high_risk_ids": list(self.high_risk_ids),
        }


@dataclass(frozen=True, slots=True)
class ReductionEvidence:
    """One bounded caller-owned claim at a declared hierarchy level."""

    item_id: str
    level: ReductionLevel | str
    modality: ReductionModality | str
    claim: str
    source_ids: tuple[str, ...]
    token_estimate: int
    fidelity_weight: int = 1
    salience: Decimal = Decimal("0.50")
    hard: bool = False
    high_risk: bool = False
    constraint_ids: tuple[str, ...] = ()
    ambiguity_ids: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    schema: str = HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        object.__setattr__(self, "level", _enum(self.level, ReductionLevel, "reduction level"))
        object.__setattr__(
            self, "modality", _enum(self.modality, ReductionModality, "reduction modality")
        )
        _text(self.claim, "claim")
        object.__setattr__(self, "source_ids", _ids(self.source_ids, "source_ids"))
        _positive_int(self.token_estimate, "token_estimate", 4_000_000)
        _positive_int(self.fidelity_weight, "fidelity_weight", 1_000_000)
        _decimal(self.salience, "salience")
        if not isinstance(self.hard, bool) or not isinstance(self.high_risk, bool):
            raise HierarchicalEvidenceReductionError("hard/high_risk must be booleans")
        object.__setattr__(self, "constraint_ids", _ids(self.constraint_ids, "constraint_ids"))
        object.__setattr__(self, "ambiguity_ids", _ids(self.ambiguity_ids, "ambiguity_ids"))
        object.__setattr__(self, "conflict_ids", _ids(self.conflict_ids, "conflict_ids"))
        object.__setattr__(self, "depends_on", _ids(self.depends_on, "depends_on"))
        if not self.source_ids:
            raise HierarchicalEvidenceReductionError("source_ids must not be empty")
        if self.schema != HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA:
            raise HierarchicalEvidenceReductionError("unsupported reduction evidence schema")

    @property
    def protected(self) -> bool:
        return (
            self.hard
            or self.high_risk
            or bool(self.constraint_ids or self.ambiguity_ids or self.conflict_ids)
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "item_id": self.item_id,
            "level": cast(ReductionLevel, self.level).value,
            "modality": cast(ReductionModality, self.modality).value,
            "claim": self.claim,
            "source_ids": list(self.source_ids),
            "token_estimate": self.token_estimate,
            "fidelity_weight": self.fidelity_weight,
            "salience": format(self.salience, "f"),
            "hard": self.hard,
            "high_risk": self.high_risk,
            "constraint_ids": list(self.constraint_ids),
            "ambiguity_ids": list(self.ambiguity_ids),
            "conflict_ids": list(self.conflict_ids),
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True, slots=True)
class ReductionLevelSummary:
    level: ReductionLevel | str
    input_item_ids: tuple[str, ...]
    retained_item_ids: tuple[str, ...]
    dropped_item_ids: tuple[str, ...]
    input_token_estimate: int
    output_token_estimate: int
    schema: str = HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", _enum(self.level, ReductionLevel, "summary level"))
        for field_name in ("input_item_ids", "retained_item_ids", "dropped_item_ids"):
            object.__setattr__(self, field_name, _ids(getattr(self, field_name), field_name))
        _positive_int(self.input_token_estimate, "input_token_estimate", 4_000_000)
        _nonnegative_int(self.output_token_estimate, "output_token_estimate", 4_000_000)
        if self.schema != HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA:
            raise HierarchicalEvidenceReductionError("unsupported reduction summary schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "level": cast(ReductionLevel, self.level).value,
            "input_item_ids": list(self.input_item_ids),
            "retained_item_ids": list(self.retained_item_ids),
            "dropped_item_ids": list(self.dropped_item_ids),
            "input_token_estimate": self.input_token_estimate,
            "output_token_estimate": self.output_token_estimate,
        }


@dataclass(frozen=True, slots=True)
class ReductionReceipt:
    """Auditable selection receipt, including both successful and blocked attempts."""

    graph_fingerprint: str
    timeline_fingerprint: str
    input_item_count: int
    output_item_count: int
    input_token_estimate: int
    output_token_estimate: int
    retained_item_ids: tuple[str, ...]
    dropped_item_ids: tuple[str, ...]
    protected_item_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    rationale: tuple[str, ...]
    budget: ReductionBudget
    source_coverage: Decimal
    target_coverage: Decimal
    fidelity_score: Decimal
    schema: str = HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA

    def __post_init__(self) -> None:
        _fingerprint(self.graph_fingerprint, "graph_fingerprint")
        _fingerprint(self.timeline_fingerprint, "timeline_fingerprint")
        _nonnegative_int(self.input_item_count, "input_item_count", MAX_REDUCTION_ITEMS)
        _nonnegative_int(self.output_item_count, "output_item_count", MAX_REDUCTION_ITEMS)
        _nonnegative_int(self.input_token_estimate, "input_token_estimate", 4_000_000)
        _nonnegative_int(self.output_token_estimate, "output_token_estimate", 4_000_000)
        for field_name in (
            "retained_item_ids",
            "dropped_item_ids",
            "protected_item_ids",
            "conflict_ids",
        ):
            object.__setattr__(self, field_name, _ids(getattr(self, field_name), field_name))
        if not isinstance(self.rationale, tuple) or len(self.rationale) > MAX_REDUCTION_RATIONALE:
            raise HierarchicalEvidenceReductionError("rationale is outside its finite bound")
        object.__setattr__(
            self, "rationale", tuple(_text(item, "rationale", 512) for item in self.rationale)
        )
        if not isinstance(self.budget, ReductionBudget):
            raise HierarchicalEvidenceReductionError("budget must be a ReductionBudget")
        for field_name in ("source_coverage", "target_coverage", "fidelity_score"):
            _decimal(getattr(self, field_name), field_name)
        if self.schema != HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA:
            raise HierarchicalEvidenceReductionError("unsupported reduction receipt schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "graph_fingerprint": self.graph_fingerprint,
            "timeline_fingerprint": self.timeline_fingerprint,
            "input_item_count": self.input_item_count,
            "output_item_count": self.output_item_count,
            "input_token_estimate": self.input_token_estimate,
            "output_token_estimate": self.output_token_estimate,
            "retained_item_ids": list(self.retained_item_ids),
            "dropped_item_ids": list(self.dropped_item_ids),
            "protected_item_ids": list(self.protected_item_ids),
            "conflict_ids": list(self.conflict_ids),
            "rationale": list(self.rationale),
            "budget": self.budget.to_wire(),
            "source_coverage": format(self.source_coverage, "f"),
            "target_coverage": format(self.target_coverage, "f"),
            "fidelity_score": format(self.fidelity_score, "f"),
        }


@dataclass(frozen=True, slots=True)
class HierarchicalEvidenceReductionRequest:
    evidence_graph: UnifiedEvidenceGraph
    timeline_plan: FeasibleAVTimelinePlan
    items: tuple[ReductionEvidence, ...]
    target: ReductionTarget
    budget: ReductionBudget
    schema: str = HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_graph, UnifiedEvidenceGraph):
            raise HierarchicalEvidenceReductionError(
                "evidence_graph must be a UnifiedEvidenceGraph"
            )
        if not isinstance(self.timeline_plan, FeasibleAVTimelinePlan):
            raise HierarchicalEvidenceReductionError(
                "timeline_plan must be a FeasibleAVTimelinePlan"
            )
        if (
            not isinstance(self.items, tuple)
            or not self.items
            or len(self.items) > MAX_REDUCTION_ITEMS
            or not all(isinstance(item, ReductionEvidence) for item in self.items)
        ):
            raise HierarchicalEvidenceReductionError("items are outside the bounded envelope")
        identifiers = tuple(item.item_id for item in self.items)
        if len(identifiers) != len(set(identifiers)):
            raise HierarchicalEvidenceReductionError("items contain duplicate IDs")
        if not isinstance(self.target, ReductionTarget):
            raise HierarchicalEvidenceReductionError("target must be a ReductionTarget")
        if not isinstance(self.budget, ReductionBudget):
            raise HierarchicalEvidenceReductionError("budget must be a ReductionBudget")
        if self.schema != HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA:
            raise HierarchicalEvidenceReductionError("unsupported reduction request schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "evidence_graph_fingerprint": self.evidence_graph.fingerprint,
            "timeline_plan_fingerprint": self.timeline_plan.fingerprint,
            "items": [item.to_wire() for item in self.items],
            "target": self.target.to_wire(),
            "budget": self.budget.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class HierarchicalReductionPlan:
    status: ReductionStatus | str
    target_id: str
    graph_fingerprint: str
    timeline_fingerprint: str
    retained_items: tuple[ReductionEvidence, ...]
    dropped_item_ids: tuple[str, ...]
    levels: tuple[ReductionLevelSummary, ...]
    receipt: ReductionReceipt
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    limitations: tuple[ValidationDiagnostic, ...] = ()
    plan_fingerprint: str | None = None
    schema: str = HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _enum(self.status, ReductionStatus, "plan status"))
        _identifier(self.target_id, "plan target_id")
        _fingerprint(self.graph_fingerprint, "plan graph_fingerprint")
        _fingerprint(self.timeline_fingerprint, "plan timeline_fingerprint")
        if (
            not isinstance(self.retained_items, tuple)
            or not self.retained_items
            or not all(isinstance(item, ReductionEvidence) for item in self.retained_items)
        ):
            raise HierarchicalEvidenceReductionError("retained_items are invalid")
        object.__setattr__(
            self, "dropped_item_ids", _ids(self.dropped_item_ids, "dropped_item_ids")
        )
        if (
            not isinstance(self.levels, tuple)
            or not self.levels
            or not all(isinstance(item, ReductionLevelSummary) for item in self.levels)
        ):
            raise HierarchicalEvidenceReductionError("levels are invalid")
        if not isinstance(self.receipt, ReductionReceipt):
            raise HierarchicalEvidenceReductionError("receipt must be a ReductionReceipt")
        for field_name in ("diagnostics", "limitations"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or not all(
                isinstance(item, ValidationDiagnostic) for item in values
            ):
                raise HierarchicalEvidenceReductionError(f"{field_name} are invalid")
        if self.schema != HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA:
            raise HierarchicalEvidenceReductionError("unsupported reduction plan schema")
        expected = canonical_fingerprint(self.to_wire(include_fingerprint=False))
        if self.plan_fingerprint is None:
            object.__setattr__(self, "plan_fingerprint", expected)
        elif self.plan_fingerprint != expected:
            raise HierarchicalEvidenceReductionError("plan fingerprint does not match contents")
        if len(self.to_wire_bytes()) > MAX_REDUCTION_OUTPUT_BYTES:
            raise HierarchicalEvidenceReductionError("reduction plan exceeds output limit")

    @property
    def fingerprint(self) -> str:
        if self.plan_fingerprint is None:  # pragma: no cover
            raise HierarchicalEvidenceReductionError("plan fingerprint is not initialized")
        return self.plan_fingerprint

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "status": cast(ReductionStatus, self.status).value,
            "target_id": self.target_id,
            "graph_fingerprint": self.graph_fingerprint,
            "timeline_fingerprint": self.timeline_fingerprint,
            "retained_items": [item.to_wire() for item in self.retained_items],
            "dropped_item_ids": list(self.dropped_item_ids),
            "levels": [item.to_wire() for item in self.levels],
            "receipt": self.receipt.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "limitations": [item.to_wire() for item in self.limitations],
        }
        if include_fingerprint:
            value["plan_fingerprint"] = self.fingerprint
        return value

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()


@dataclass(frozen=True, slots=True)
class HierarchicalEvidenceReductionResult:
    status: ReductionStatus | str
    plan: HierarchicalReductionPlan | None
    receipt: ReductionReceipt
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    limitations: tuple[ValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _enum(self.status, ReductionStatus, "result status"))
        if self.plan is not None and not isinstance(self.plan, HierarchicalReductionPlan):
            raise HierarchicalEvidenceReductionError("result plan is invalid")
        if not isinstance(self.receipt, ReductionReceipt):
            raise HierarchicalEvidenceReductionError("result receipt is invalid")
        for field_name in ("diagnostics", "limitations"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or not all(
                isinstance(item, ValidationDiagnostic) for item in values
            ):
                raise HierarchicalEvidenceReductionError(f"result {field_name} are invalid")

    @property
    def is_valid(self) -> bool:
        return self.plan is not None and self.status in {
            ReductionStatus.COMPLETE,
            ReductionStatus.PARTIAL,
        }

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA,
            "status": cast(ReductionStatus, self.status).value,
            "plan": None if self.plan is None else self.plan.to_wire(),
            "receipt": self.receipt.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "limitations": [item.to_wire() for item in self.limitations],
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()


def _diagnostic(code: str, message: str) -> ValidationDiagnostic:
    return ValidationDiagnostic(ValidationSeverity.ERROR, code, message, "hierarchical_reduction")


def _known_ids(graph: UnifiedEvidenceGraph, timeline: FeasibleAVTimelinePlan) -> set[str]:
    result: set[str] = set()
    for values, field in (
        (graph.observations, "observation_id"),
        (graph.entities, "entity_id"),
        (graph.tracks, "track_id"),
        (graph.events, "event_id"),
        (graph.evidence, "evidence_id"),
        (graph.uncertainties, "uncertainty_id"),
        (graph.alternatives, "alternative_id"),
        (graph.conflicts, "conflict_id"),
    ):
        result.update(str(getattr(item, field)) for item in values)
    result.update(timeline.reference_order)
    result.update(shot.shot_id for shot in timeline.shots)
    result.update(event.event_id for event in timeline.events)
    result.update(anchor.anchor_id for anchor in timeline.anchors)
    result.update(relation.relation_id for relation in timeline.relations)
    return result


def _graph_risk_ids(graph: UnifiedEvidenceGraph) -> set[str]:
    result = {item.alternative_id for item in graph.alternatives}
    result.update(item.conflict_id for item in graph.conflicts)
    result.update(
        item.uncertainty_id
        for item in graph.uncertainties
        if item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
    )
    return result


def _item_priority(item: ReductionEvidence, focus: set[str]) -> tuple[object, ...]:
    level_rank = {
        ReductionLevel.CROSS_MODAL_RELATION: 5,
        ReductionLevel.MODALITY_SUMMARY: 4,
        ReductionLevel.SHOT: 3,
        ReductionLevel.SEGMENT: 2,
        ReductionLevel.ASSET: 1,
    }
    level = cast(ReductionLevel, item.level)
    return (
        -len(focus.intersection(item.source_ids)),
        -level_rank[level],
        -item.salience,
        -item.fidelity_weight,
        item.item_id,
    )


def _coverage(retained: set[str], all_sources: set[str]) -> Decimal:
    if not all_sources:
        return Decimal("1")
    return Decimal(len(retained.intersection(all_sources))) / Decimal(len(all_sources))


def _fidelity(retained: set[str], items: dict[str, ReductionEvidence]) -> Decimal:
    total = sum(item.fidelity_weight for item in items.values())
    if total == 0:
        return Decimal("1")
    value = sum(item.fidelity_weight for item_id, item in items.items() if item_id in retained)
    return Decimal(value) / Decimal(total)


def _receipt(
    request: HierarchicalEvidenceReductionRequest,
    items: dict[str, ReductionEvidence],
    retained: set[str],
    protected: set[str],
    rationale: tuple[str, ...],
) -> ReductionReceipt:
    all_sources = {source for item in items.values() for source in item.source_ids}
    retained_sources = {
        source
        for item_id, item in items.items()
        if item_id in retained
        for source in item.source_ids
    }
    focus = set(request.target.focus_source_ids)
    input_tokens = sum(item.token_estimate for item in items.values())
    output_tokens = sum(
        item.token_estimate for item_id, item in items.items() if item_id in retained
    )
    return ReductionReceipt(
        request.evidence_graph.fingerprint,
        request.timeline_plan.fingerprint,
        len(items),
        len(retained),
        input_tokens,
        output_tokens,
        tuple(sorted(retained)),
        tuple(sorted(set(items).difference(retained))),
        tuple(sorted(protected)),
        tuple(sorted({conflict for item in items.values() for conflict in item.conflict_ids})),
        rationale,
        request.budget,
        _coverage(retained_sources, all_sources),
        _coverage(retained_sources, focus),
        _fidelity(retained, items),
    )


def build_hierarchical_evidence_reduction(
    request: HierarchicalEvidenceReductionRequest,
) -> HierarchicalEvidenceReductionResult:
    """Select typed evidence under explicit budgets, failing closed on unsafe loss."""

    if not isinstance(request, HierarchicalEvidenceReductionRequest):
        raise HierarchicalEvidenceReductionError(
            "request must be a HierarchicalEvidenceReductionRequest"
        )
    items = {item.item_id: item for item in request.items}
    diagnostics: list[ValidationDiagnostic] = []
    known = _known_ids(request.evidence_graph, request.timeline_plan)
    upstream_conflict = request.evidence_graph.status is GraphStatus.CONFLICTING or (
        request.timeline_plan.status is TimelinePlannerStatus.CONFLICTING
    )
    upstream_block = request.timeline_plan.status is TimelinePlannerStatus.BLOCKED
    if upstream_conflict:
        diagnostics.append(
            _diagnostic(
                "upstream_conflict",
                "conflicting graph or timeline input cannot be compressed into a winner",
            )
        )
    elif upstream_block:
        diagnostics.append(
            _diagnostic(
                "upstream_plan_blocked",
                "a blocked timeline cannot be reduced into a renderable target",
            )
        )
    for item in items.values():
        if not set(item.source_ids).issubset(known):
            diagnostics.append(
                _diagnostic(
                    "unknown_source_id",
                    f"reduction item {item.item_id!r} references an unknown source ID",
                )
            )
        if not set(item.depends_on).issubset(items):
            diagnostics.append(
                _diagnostic(
                    "unknown_dependency",
                    f"reduction item {item.item_id!r} references an unknown dependency",
                )
            )
    if not set(request.target.focus_source_ids).issubset(known):
        diagnostics.append(
            _diagnostic("unknown_target_source", "target focus contains an unknown source ID")
        )
    missing_required = set(request.target.required_item_ids).difference(items)
    if missing_required:
        diagnostics.append(_diagnostic("required_item_missing", "target requires an unknown item"))
    risk_ids = _graph_risk_ids(request.evidence_graph).union(request.target.high_risk_ids)
    represented_risk = {
        risk_id for item in items.values() for risk_id in (*item.ambiguity_ids, *item.conflict_ids)
    }
    if not risk_ids.issubset(represented_risk):
        diagnostics.append(
            _diagnostic(
                "unrepresented_high_risk_evidence",
                "every graph conflict, ambiguity, and high-risk uncertainty needs a protected item",
            )
        )
    represented_constraints = {
        constraint for item in items.values() for constraint in item.constraint_ids
    }
    if not set(request.target.hard_constraint_ids).issubset(represented_constraints):
        diagnostics.append(
            _diagnostic(
                "unrepresented_hard_constraint",
                "every target hard constraint needs an item",
            )
        )

    protected: set[str] = set(request.target.required_item_ids)
    protected.update(item.item_id for item in items.values() if item.protected)
    protected.update(
        item.item_id
        for item in items.values()
        if set(item.constraint_ids).intersection(request.target.hard_constraint_ids)
        or set(item.ambiguity_ids).intersection(risk_ids)
        or set(item.conflict_ids).intersection(risk_ids)
    )

    visiting: set[str] = set()
    visited: set[str] = set()

    def close_dependencies(item_id: str, selected: set[str]) -> None:
        if item_id in visiting:
            diagnostics.append(
                _diagnostic("dependency_cycle", "reduction dependencies contain a cycle")
            )
            return
        if item_id in visited:
            return
        visiting.add(item_id)
        item = items.get(item_id)
        if item is not None:
            for dependency in item.depends_on:
                if dependency in items:
                    selected.add(dependency)
                    close_dependencies(dependency, selected)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in tuple(sorted(protected)):
        close_dependencies(item_id, protected)

    retained: set[str] = set(protected)
    rationale: list[str] = ["protected hard, required, and high-risk evidence first"]
    protected_tokens = sum(
        items[item_id].token_estimate for item_id in retained if item_id in items
    )
    if (
        diagnostics
        or len(retained) > request.budget.max_output_items
        or protected_tokens > request.budget.max_output_tokens
    ):
        if (
            len(retained) > request.budget.max_output_items
            or protected_tokens > request.budget.max_output_tokens
        ):
            diagnostics.append(
                _diagnostic(
                    "protected_budget_exceeded",
                    "protected evidence cannot fit the explicit reduction budget",
                )
            )
        receipt = _receipt(
            request,
            items,
            retained.intersection(items),
            protected.intersection(items),
            tuple(rationale),
        )
        status = ReductionStatus.CONFLICTING if upstream_conflict else ReductionStatus.BLOCKED
        return HierarchicalEvidenceReductionResult(status, None, receipt, tuple(diagnostics))

    optional = sorted(
        (item for item_id, item in items.items() if item_id not in retained),
        key=lambda item: _item_priority(item, set(request.target.focus_source_ids)),
    )
    for item in optional:
        closure = {item.item_id}
        close_dependencies(item.item_id, closure)
        if diagnostics:
            break
        candidate = retained.union(closure)
        candidate_tokens = sum(
            items[item_id].token_estimate for item_id in candidate if item_id in items
        )
        if (
            len(candidate) <= request.budget.max_output_items
            and candidate_tokens <= request.budget.max_output_tokens
        ):
            retained.update(closure)
            rationale.append(f"retained {item.item_id} for target coverage and hierarchy value")
        else:
            rationale.append(f"dropped {item.item_id} at the finite budget boundary")

    receipt = _receipt(
        request,
        items,
        retained.intersection(items),
        protected.intersection(items),
        tuple(rationale),
    )
    if receipt.source_coverage < request.budget.min_source_coverage:
        diagnostics.append(
            _diagnostic(
                "source_coverage_below_threshold",
                "source coverage is below the configured threshold",
            )
        )
    if receipt.target_coverage < request.budget.min_target_coverage:
        diagnostics.append(
            _diagnostic(
                "target_source_uncovered",
                "target focus coverage is below the configured threshold",
            )
        )
    if receipt.fidelity_score < request.budget.min_fidelity:
        diagnostics.append(
            _diagnostic(
                "fidelity_below_threshold",
                "weighted compression fidelity is below the configured threshold",
            )
        )
    if diagnostics:
        status = ReductionStatus.CONFLICTING if upstream_conflict else ReductionStatus.BLOCKED
        return HierarchicalEvidenceReductionResult(status, None, receipt, tuple(diagnostics))

    level_summaries: list[ReductionLevelSummary] = []
    for level in ReductionLevel:
        level_items = tuple(item for item in items.values() if item.level is level)
        if not level_items:
            continue
        input_ids = tuple(sorted(item.item_id for item in level_items))
        retained_ids = tuple(sorted(item_id for item_id in input_ids if item_id in retained))
        dropped_ids = tuple(sorted(set(input_ids).difference(retained_ids)))
        level_summaries.append(
            ReductionLevelSummary(
                level,
                input_ids,
                retained_ids,
                dropped_ids,
                sum(item.token_estimate for item in level_items),
                sum(item.token_estimate for item in level_items if item.item_id in retained),
            )
        )
    retained_items = tuple(items[item_id] for item_id in sorted(retained))
    base_status = (
        ReductionStatus.PARTIAL
        if request.evidence_graph.status in {GraphStatus.PARTIAL, GraphStatus.AMBIGUOUS}
        or request.timeline_plan.status is TimelinePlannerStatus.PARTIAL
        else ReductionStatus.COMPLETE
    )
    plan = HierarchicalReductionPlan(
        base_status,
        request.target.target_id,
        request.evidence_graph.fingerprint,
        request.timeline_plan.fingerprint,
        retained_items,
        receipt.dropped_item_ids,
        tuple(level_summaries),
        receipt,
    )
    return HierarchicalEvidenceReductionResult(base_status, plan, receipt)


build_hierarchical_reduction = build_hierarchical_evidence_reduction


__all__ = [
    "HIERARCHICAL_EVIDENCE_REDUCTION_SCHEMA",
    "MAX_REDUCTION_ITEMS",
    "MAX_REDUCTION_OUTPUT_BYTES",
    "ReductionBudget",
    "ReductionEvidence",
    "ReductionLevel",
    "ReductionLevelSummary",
    "ReductionModality",
    "ReductionReceipt",
    "ReductionStatus",
    "ReductionTarget",
    "HierarchicalEvidenceReductionRequest",
    "HierarchicalEvidenceReductionResult",
    "HierarchicalReductionPlan",
    "build_hierarchical_evidence_reduction",
    "build_hierarchical_reduction",
]
