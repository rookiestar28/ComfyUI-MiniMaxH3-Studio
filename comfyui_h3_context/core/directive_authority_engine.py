"""M13-04 authority, conflict, clarification, and abstention projection.

The M6 directive resolver remains the source of action and precedence semantics.  This module only
projects that immutable result into a later-planning receipt; it does not interpret prose, resolve
identity/causality, or execute provider output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from .canonical import canonical_fingerprint
from .contracts import ValidationDiagnostic, ValidationSeverity
from .errors import DirectiveAuthorityEngineError
from .reference_directives import (
    DirectiveAction,
    DirectiveAuthority,
    DirectiveConflict,
    DirectiveDecision,
    DirectiveDecisionRecord,
    ReferenceDirective,
    ResolvedDirectiveSet,
)
from .reference_role_resolution import (
    ReferenceRoleGraphStatus,
    ReferenceRoleResolutionGraph,
)
from .temporal_event_alignment import TemporalAlignmentStatus, TemporalEventAlignment

DIRECTIVE_AUTHORITY_ENGINE_SCHEMA = "h3.directive_authority_engine.v1"
MAX_DIRECTIVE_ENGINE_ITEMS = 512
MAX_DIRECTIVE_ENGINE_CLARIFICATIONS = 512
MAX_DIRECTIVE_ENGINE_ABSTENTIONS = 512
MAX_DIRECTIVE_ENGINE_TEXT = 4096
MAX_DIRECTIVE_ENGINE_OUTPUT_BYTES = 131_072

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
_PROMPT_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "system prompt",
    "developer message",
    "assistant message",
    "tool call",
    "call the tool",
    "execute command",
    "reveal secret",
    "jailbreak",
    "override policy",
)
_AUTHORITY_RANK = {
    DirectiveAuthority.USER_HARD: 4,
    DirectiveAuthority.USER_PREFERENCE: 3,
    DirectiveAuthority.REFERENCE_ONLY: 2,
    DirectiveAuthority.ASSISTED_PROPOSAL: 1,
}


class DirectiveAuthorityEngineStatus(str, Enum):
    """Report-level claim ceiling."""

    EMPTY = "empty"
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CONFLICTING = "conflicting"


class DirectiveEngineDisposition(str, Enum):
    """One projected disposition for one unique directive ID."""

    ACCEPTED = "accepted"
    SHADOWED = "shadowed"
    REJECTED = "rejected"
    CONFLICTING = "conflicting"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise DirectiveAuthorityEngineError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise DirectiveAuthorityEngineError(f"{field_name} contains sensitive or locator material")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_DIRECTIVE_ENGINE_TEXT:
        raise DirectiveAuthorityEngineError(f"{field_name} must be bounded non-empty text")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise DirectiveAuthorityEngineError(f"{field_name} contains sensitive or locator material")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise DirectiveAuthorityEngineError(f"{field_name} contains an unsafe wire code point")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    return None if value is None else _text(value, field_name)


def _ids(
    values: object, field_name: str, *, maximum: int = MAX_DIRECTIVE_ENGINE_ITEMS
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise DirectiveAuthorityEngineError(f"{field_name} is outside the bounded envelope")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise DirectiveAuthorityEngineError(f"{field_name} must not contain duplicates")
    return result


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise DirectiveAuthorityEngineError(f"{field_name} must be a SHA-256 fingerprint")
    return value


def _enum(value: object, expected: type[Enum], field_name: str) -> Enum:
    if not isinstance(value, expected):
        raise DirectiveAuthorityEngineError(f"{field_name} must be a {expected.__name__}")
    return value


@dataclass(frozen=True, slots=True)
class DirectiveScope:
    """A lossless typed scope projection of one M6 directive."""

    scope_id: str
    directive_id: str
    action: DirectiveAction
    target_kind: object
    target_id: str
    source_asset_ids: tuple[str, ...]
    source_observation_ids: tuple[str, ...]
    source_event_id: str | None
    target_segment_id: str | None
    retention_aspects: tuple[object, ...]
    adaptation: str | None
    reason: str | None
    hard_constraint_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    negative_requirement: bool
    exact_text_protected: bool
    schema: str = DIRECTIVE_AUTHORITY_ENGINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.scope_id, "scope_id")
        _identifier(self.directive_id, "scope directive_id")
        _enum(self.action, DirectiveAction, "scope action")
        _identifier(cast(str, getattr(self.target_kind, "value", self.target_kind)), "target_kind")
        _identifier(self.target_id, "scope target_id")
        _ids(self.source_asset_ids, "scope source_asset_ids")
        _ids(self.source_observation_ids, "scope source_observation_ids")
        if self.source_event_id is not None:
            _identifier(self.source_event_id, "scope source_event_id")
        if self.target_segment_id is not None:
            _identifier(self.target_segment_id, "scope target_segment_id")
        if not isinstance(self.retention_aspects, tuple):
            raise DirectiveAuthorityEngineError("scope retention_aspects must be a tuple")
        for aspect in self.retention_aspects:
            _identifier(cast(str, getattr(aspect, "value", aspect)), "scope retention aspect")
        _optional_text(self.adaptation, "scope adaptation")
        _optional_text(self.reason, "scope reason")
        _ids(self.hard_constraint_ids, "scope hard_constraint_ids")
        _ids(self.evidence_ids, "scope evidence_ids")
        if not isinstance(self.negative_requirement, bool):
            raise DirectiveAuthorityEngineError("negative_requirement must be boolean")
        if self.negative_requirement is not (self.action is DirectiveAction.EXCLUDE):
            raise DirectiveAuthorityEngineError(
                "negative_requirement does not match EXCLUDE action"
            )
        if not isinstance(self.exact_text_protected, bool):
            raise DirectiveAuthorityEngineError("exact_text_protected must be boolean")
        if self.schema != DIRECTIVE_AUTHORITY_ENGINE_SCHEMA:
            raise DirectiveAuthorityEngineError("unsupported directive authority engine schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "scope_id": self.scope_id,
            "directive_id": self.directive_id,
            "action": self.action.value,
            "target_kind": getattr(self.target_kind, "value", self.target_kind),
            "target_id": self.target_id,
            "source_asset_ids": list(self.source_asset_ids),
            "source_observation_ids": list(self.source_observation_ids),
            "source_event_id": self.source_event_id,
            "target_segment_id": self.target_segment_id,
            "retention_aspects": [getattr(item, "value", item) for item in self.retention_aspects],
            "adaptation": self.adaptation,
            "reason": self.reason,
            "hard_constraint_ids": list(self.hard_constraint_ids),
            "evidence_ids": list(self.evidence_ids),
            "negative_requirement": self.negative_requirement,
            "exact_text_protected": self.exact_text_protected,
        }


@dataclass(frozen=True, slots=True)
class DirectiveAuthorityDecision:
    """One authoritative projection of a unique input directive."""

    directive_id: str
    scope_id: str
    authority: DirectiveAuthority
    authority_rank: int
    priority: int
    disposition: DirectiveEngineDisposition
    resolver_decisions: tuple[DirectiveDecision, ...]
    conflict_ids: tuple[str, ...] = ()
    reason: str | None = None
    schema: str = DIRECTIVE_AUTHORITY_ENGINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.directive_id, "decision directive_id")
        _identifier(self.scope_id, "decision scope_id")
        _enum(self.authority, DirectiveAuthority, "decision authority")
        if self.authority_rank != _AUTHORITY_RANK[self.authority]:
            raise DirectiveAuthorityEngineError("decision authority_rank does not match authority")
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or self.priority < 0
        ):
            raise DirectiveAuthorityEngineError("decision priority is invalid")
        _enum(self.disposition, DirectiveEngineDisposition, "decision disposition")
        if not isinstance(self.resolver_decisions, tuple) or not all(
            isinstance(item, DirectiveDecision) for item in self.resolver_decisions
        ):
            raise DirectiveAuthorityEngineError(
                "resolver_decisions must contain DirectiveDecision values"
            )
        _ids(self.conflict_ids, "decision conflict_ids")
        _optional_text(self.reason, "decision reason")
        if self.schema != DIRECTIVE_AUTHORITY_ENGINE_SCHEMA:
            raise DirectiveAuthorityEngineError("unsupported directive authority engine schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "directive_id": self.directive_id,
            "scope_id": self.scope_id,
            "authority": self.authority.value,
            "authority_rank": self.authority_rank,
            "priority": self.priority,
            "disposition": self.disposition.value,
            "resolver_decisions": [item.value for item in self.resolver_decisions],
            "conflict_ids": list(self.conflict_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DirectivePrecedenceEntry:
    """One entry in the exhaustive deterministic precedence ordering."""

    entry_id: str
    directive_id: str
    authority: DirectiveAuthority
    authority_rank: int
    priority: int
    order: int
    scope_id: str
    disposition: DirectiveEngineDisposition
    schema: str = DIRECTIVE_AUTHORITY_ENGINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.entry_id, "precedence entry_id")
        _identifier(self.directive_id, "precedence directive_id")
        _enum(self.authority, DirectiveAuthority, "precedence authority")
        if self.authority_rank != _AUTHORITY_RANK[self.authority]:
            raise DirectiveAuthorityEngineError(
                "precedence authority_rank does not match authority"
            )
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or self.priority < 0
        ):
            raise DirectiveAuthorityEngineError("precedence priority is invalid")
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order < 0:
            raise DirectiveAuthorityEngineError("precedence order is invalid")
        _identifier(self.scope_id, "precedence scope_id")
        _enum(self.disposition, DirectiveEngineDisposition, "precedence disposition")
        if self.schema != DIRECTIVE_AUTHORITY_ENGINE_SCHEMA:
            raise DirectiveAuthorityEngineError("unsupported directive authority engine schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "entry_id": self.entry_id,
            "directive_id": self.directive_id,
            "authority": self.authority.value,
            "authority_rank": self.authority_rank,
            "priority": self.priority,
            "order": self.order,
            "scope_id": self.scope_id,
            "disposition": self.disposition.value,
        }


@dataclass(frozen=True, slots=True)
class DirectiveClarification:
    """A user-visible question required before an unsafe choice can proceed."""

    clarification_id: str
    code: str
    question: str
    directive_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    severity: ValidationSeverity = ValidationSeverity.ERROR
    blocking: bool = True
    schema: str = DIRECTIVE_AUTHORITY_ENGINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.clarification_id, "clarification_id")
        _identifier(self.code, "clarification code")
        _text(self.question, "clarification question")
        _ids(self.directive_ids, "clarification directive_ids")
        _ids(self.source_ids, "clarification source_ids")
        _enum(self.severity, ValidationSeverity, "clarification severity")
        if not isinstance(self.blocking, bool):
            raise DirectiveAuthorityEngineError("clarification blocking must be boolean")
        if self.blocking and self.severity not in {
            ValidationSeverity.ERROR,
            ValidationSeverity.FATAL,
        }:
            raise DirectiveAuthorityEngineError("blocking clarification must be error or fatal")
        if self.schema != DIRECTIVE_AUTHORITY_ENGINE_SCHEMA:
            raise DirectiveAuthorityEngineError("unsupported directive authority engine schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "clarification_id": self.clarification_id,
            "code": self.code,
            "question": self.question,
            "directive_ids": list(self.directive_ids),
            "source_ids": list(self.source_ids),
            "severity": self.severity.value,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class DirectiveAbstention:
    """An explicit unsupported/uncertain scope that must not be silently filled."""

    abstention_id: str
    code: str
    reason: str
    directive_ids: tuple[str, ...] = ()
    scope_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    high_impact: bool = False
    schema: str = DIRECTIVE_AUTHORITY_ENGINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.abstention_id, "abstention_id")
        _identifier(self.code, "abstention code")
        _text(self.reason, "abstention reason")
        _ids(self.directive_ids, "abstention directive_ids")
        _ids(self.scope_ids, "abstention scope_ids")
        _ids(self.source_ids, "abstention source_ids")
        if not isinstance(self.high_impact, bool):
            raise DirectiveAuthorityEngineError("abstention high_impact must be boolean")
        if self.schema != DIRECTIVE_AUTHORITY_ENGINE_SCHEMA:
            raise DirectiveAuthorityEngineError("unsupported directive authority engine schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "abstention_id": self.abstention_id,
            "code": self.code,
            "reason": self.reason,
            "directive_ids": list(self.directive_ids),
            "scope_ids": list(self.scope_ids),
            "source_ids": list(self.source_ids),
            "high_impact": self.high_impact,
        }


@dataclass(frozen=True, slots=True)
class DirectiveAuthorityMetrics:
    """Structural counts and guard outcomes for the projection."""

    input_count: int
    scope_count: int
    accepted_count: int
    shadowed_count: int
    rejected_count: int
    conflicting_count: int
    clarification_count: int
    blocking_clarification_count: int
    abstention_count: int
    hard_constraint_violation_count: int
    prompt_injection_count: int
    precedence_order_errors: int
    mutation_guard_pass: bool
    prompt_injection_guard_pass: bool

    def __post_init__(self) -> None:
        values = (
            self.input_count,
            self.scope_count,
            self.accepted_count,
            self.shadowed_count,
            self.rejected_count,
            self.conflicting_count,
            self.clarification_count,
            self.blocking_clarification_count,
            self.abstention_count,
            self.hard_constraint_violation_count,
            self.prompt_injection_count,
            self.precedence_order_errors,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise DirectiveAuthorityEngineError("directive authority metrics must be non-negative")
        if not isinstance(self.mutation_guard_pass, bool) or not isinstance(
            self.prompt_injection_guard_pass, bool
        ):
            raise DirectiveAuthorityEngineError("directive authority guard results must be boolean")

    def to_wire(self) -> dict[str, int | bool]:
        return {
            "input_count": self.input_count,
            "scope_count": self.scope_count,
            "accepted_count": self.accepted_count,
            "shadowed_count": self.shadowed_count,
            "rejected_count": self.rejected_count,
            "conflicting_count": self.conflicting_count,
            "clarification_count": self.clarification_count,
            "blocking_clarification_count": self.blocking_clarification_count,
            "abstention_count": self.abstention_count,
            "hard_constraint_violation_count": self.hard_constraint_violation_count,
            "prompt_injection_count": self.prompt_injection_count,
            "precedence_order_errors": self.precedence_order_errors,
            "mutation_guard_pass": self.mutation_guard_pass,
            "prompt_injection_guard_pass": self.prompt_injection_guard_pass,
        }


def _validate_collection(
    values: object, expected: type[Any], field_name: str, maximum: int
) -> None:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise DirectiveAuthorityEngineError(f"{field_name} are outside the bounded envelope")
    if not all(isinstance(item, expected) for item in values):
        raise DirectiveAuthorityEngineError(f"{field_name} are outside the bounded envelope")


@dataclass(frozen=True, slots=True)
class DirectiveAuthorityReport:
    """Inspectable, deterministic authority projection consumed by later M13 planning."""

    status: DirectiveAuthorityEngineStatus
    input_fingerprint: str
    resolved_fingerprint: str
    scopes: tuple[DirectiveScope, ...]
    decisions: tuple[DirectiveAuthorityDecision, ...]
    precedence: tuple[DirectivePrecedenceEntry, ...]
    clarifications: tuple[DirectiveClarification, ...]
    abstentions: tuple[DirectiveAbstention, ...]
    metrics: DirectiveAuthorityMetrics
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    fingerprint: str = ""
    schema: str = DIRECTIVE_AUTHORITY_ENGINE_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.status, DirectiveAuthorityEngineStatus, "report status")
        _fingerprint(self.input_fingerprint, "report input_fingerprint")
        _fingerprint(self.resolved_fingerprint, "report resolved_fingerprint")
        _validate_collection(self.scopes, DirectiveScope, "scopes", MAX_DIRECTIVE_ENGINE_ITEMS)
        _validate_collection(
            self.decisions,
            DirectiveAuthorityDecision,
            "decisions",
            MAX_DIRECTIVE_ENGINE_ITEMS,
        )
        _validate_collection(
            self.precedence,
            DirectivePrecedenceEntry,
            "precedence",
            MAX_DIRECTIVE_ENGINE_ITEMS,
        )
        _validate_collection(
            self.clarifications,
            DirectiveClarification,
            "clarifications",
            MAX_DIRECTIVE_ENGINE_CLARIFICATIONS,
        )
        _validate_collection(
            self.abstentions,
            DirectiveAbstention,
            "abstentions",
            MAX_DIRECTIVE_ENGINE_ABSTENTIONS,
        )
        if not isinstance(self.metrics, DirectiveAuthorityMetrics):
            raise DirectiveAuthorityEngineError("metrics must be DirectiveAuthorityMetrics")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise DirectiveAuthorityEngineError(
                "diagnostics must contain ValidationDiagnostic values"
            )
        if self.fingerprint and not _FINGERPRINT.fullmatch(self.fingerprint):
            raise DirectiveAuthorityEngineError("report fingerprint must be a SHA-256 fingerprint")
        if self.schema != DIRECTIVE_AUTHORITY_ENGINE_SCHEMA:
            raise DirectiveAuthorityEngineError("unsupported directive authority engine schema")
        expected = canonical_fingerprint(self.to_wire())
        if self.fingerprint and self.fingerprint != expected:
            raise DirectiveAuthorityEngineError("report fingerprint does not match contents")
        object.__setattr__(self, "fingerprint", expected)
        if len(self.to_wire_bytes()) > MAX_DIRECTIVE_ENGINE_OUTPUT_BYTES:
            raise DirectiveAuthorityEngineError("directive authority report exceeds output limit")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "input_fingerprint": self.input_fingerprint,
            "resolved_fingerprint": self.resolved_fingerprint,
            "scopes": [item.to_wire() for item in self.scopes],
            "decisions": [item.to_wire() for item in self.decisions],
            "precedence": [item.to_wire() for item in self.precedence],
            "clarifications": [item.to_wire() for item in self.clarifications],
            "abstentions": [item.to_wire() for item in self.abstentions],
            "metrics": self.metrics.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()

    def to_public_dict(self) -> dict[str, object]:
        result = self.to_wire()
        result["fingerprint"] = self.fingerprint
        return result


def _scope(directive: ReferenceDirective) -> DirectiveScope:
    target_kind = directive.target_kind
    return DirectiveScope(
        scope_id=f"scope.{directive.directive_id}",
        directive_id=directive.directive_id,
        action=directive.action,
        target_kind=target_kind,
        target_id=directive.target_id,
        source_asset_ids=directive.source_asset_ids,
        source_observation_ids=directive.source_observation_ids,
        source_event_id=directive.source_event_id,
        target_segment_id=directive.target_segment_id,
        retention_aspects=directive.retention_aspects,
        adaptation=directive.adaptation,
        reason=directive.reason,
        hard_constraint_ids=directive.hard_constraint_ids,
        evidence_ids=directive.evidence_ids,
        negative_requirement=directive.action is DirectiveAction.EXCLUDE,
        exact_text_protected=target_kind.value in {"dialogue", "lyrics", "visible_text"},
    )


def _decision(
    directive: ReferenceDirective,
    records: tuple[DirectiveDecisionRecord, ...],
    conflict_ids: tuple[str, ...],
) -> DirectiveAuthorityDecision:
    resolver_decisions = tuple(record.decision for record in records)
    if DirectiveDecision.CONFLICTING in resolver_decisions:
        disposition = DirectiveEngineDisposition.CONFLICTING
    elif DirectiveDecision.REJECTED in resolver_decisions:
        disposition = DirectiveEngineDisposition.REJECTED
    elif DirectiveDecision.ACCEPTED in resolver_decisions:
        disposition = DirectiveEngineDisposition.ACCEPTED
    elif DirectiveDecision.SHADOWED in resolver_decisions:
        disposition = DirectiveEngineDisposition.SHADOWED
    else:
        disposition = DirectiveEngineDisposition.REJECTED
    reason = next((record.reason for record in records if record.reason), None)
    return DirectiveAuthorityDecision(
        directive_id=directive.directive_id,
        scope_id=f"scope.{directive.directive_id}",
        authority=directive.authority,
        authority_rank=_AUTHORITY_RANK[directive.authority],
        priority=directive.priority,
        disposition=disposition,
        resolver_decisions=resolver_decisions,
        conflict_ids=conflict_ids,
        reason=reason,
    )


def _diagnostic(severity: ValidationSeverity, code: str, message: str) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, "directive_authority_engine")


def _conflict_clarification(conflict: DirectiveConflict) -> DirectiveClarification:
    if conflict.code == "hard_constraint_conflict":
        question = "Which user-owned hard constraint should remain authoritative before planning?"
    elif conflict.code == "equal_precedence_conflict":
        question = "Which equal-precedence directive should win for this scope?"
    else:
        question = "Please resolve this directive conflict before planning continues."
    return DirectiveClarification(
        clarification_id=f"clarification.{conflict.conflict_id}",
        code=conflict.code,
        question=question,
        directive_ids=conflict.directive_ids,
        source_ids=(conflict.conflict_id,),
        severity=conflict.severity,
        blocking=conflict.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL},
    )


def _status(
    resolved: ResolvedDirectiveSet,
    *,
    directives: tuple[ReferenceDirective, ...],
    clarifications: tuple[DirectiveClarification, ...],
    abstentions: tuple[DirectiveAbstention, ...],
) -> DirectiveAuthorityEngineStatus:
    blocking = any(item.blocking for item in clarifications)
    if blocking:
        if resolved.status.value == "conflicting":
            return DirectiveAuthorityEngineStatus.CONFLICTING
        return DirectiveAuthorityEngineStatus.BLOCKED
    if not directives:
        return DirectiveAuthorityEngineStatus.EMPTY
    if abstentions or resolved.status.value == "partial":
        return DirectiveAuthorityEngineStatus.PARTIAL
    if resolved.status.value == "conflicting":
        return DirectiveAuthorityEngineStatus.CONFLICTING
    return DirectiveAuthorityEngineStatus.COMPLETE


def build_directive_authority_engine(
    resolved: ResolvedDirectiveSet,
    *,
    role_graph: ReferenceRoleResolutionGraph | None = None,
    temporal_alignment: TemporalEventAlignment | None = None,
) -> DirectiveAuthorityReport:
    """Project M6 decisions and optional M13 graph ceilings without making hidden choices."""

    if not isinstance(resolved, ResolvedDirectiveSet):
        raise DirectiveAuthorityEngineError("resolved must be a ResolvedDirectiveSet")
    if role_graph is not None and not isinstance(role_graph, ReferenceRoleResolutionGraph):
        raise DirectiveAuthorityEngineError("role_graph must be a ReferenceRoleResolutionGraph")
    if temporal_alignment is not None and not isinstance(
        temporal_alignment, TemporalEventAlignment
    ):
        raise DirectiveAuthorityEngineError("temporal_alignment must be a TemporalEventAlignment")
    if (
        role_graph is not None
        and temporal_alignment is not None
        and temporal_alignment.role_graph_fingerprint != role_graph.fingerprint
    ):
        raise DirectiveAuthorityEngineError(
            "temporal alignment does not belong to the supplied role graph"
        )

    request_before = canonical_fingerprint(resolved.request.to_wire())
    resolved_fingerprint = canonical_fingerprint(resolved.to_wire())
    directives = tuple(sorted(resolved.request.directives, key=lambda item: item.directive_id))
    scopes = tuple(_scope(item) for item in directives)
    records_by_id: dict[str, list[DirectiveDecisionRecord]] = {
        item.directive_id: [] for item in directives
    }
    for record in resolved.decision_records:
        records_by_id.setdefault(record.directive_id, []).append(record)
    conflict_by_id: dict[str, set[str]] = {item.directive_id: set() for item in directives}
    for conflict in resolved.conflicts:
        for directive_id in conflict.directive_ids:
            conflict_by_id.setdefault(directive_id, set()).add(conflict.conflict_id)
    decisions = tuple(
        _decision(
            item,
            tuple(records_by_id[item.directive_id]),
            tuple(sorted(conflict_by_id[item.directive_id])),
        )
        for item in directives
    )
    decision_by_id = {item.directive_id: item for item in decisions}
    precedence_directives = tuple(
        sorted(
            directives,
            key=lambda item: (-_AUTHORITY_RANK[item.authority], -item.priority, item.directive_id),
        )
    )
    precedence = tuple(
        DirectivePrecedenceEntry(
            entry_id=f"precedence.{item.directive_id}",
            directive_id=item.directive_id,
            authority=item.authority,
            authority_rank=_AUTHORITY_RANK[item.authority],
            priority=item.priority,
            order=index,
            scope_id=f"scope.{item.directive_id}",
            disposition=decision_by_id[item.directive_id].disposition,
        )
        for index, item in enumerate(precedence_directives)
    )
    precedence_order_errors = sum(
        1
        for previous, current in zip(precedence, precedence[1:], strict=False)
        if previous.authority_rank < current.authority_rank
        or (
            previous.authority_rank == current.authority_rank
            and previous.priority < current.priority
        )
        or (
            previous.authority_rank == current.authority_rank
            and previous.priority == current.priority
            and previous.directive_id > current.directive_id
        )
    )

    clarifications: list[DirectiveClarification] = [
        _conflict_clarification(conflict)
        for conflict in resolved.conflicts
        if conflict.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
    ]
    abstentions: list[DirectiveAbstention] = []
    diagnostics = list(resolved.diagnostics)
    prompt_injection_count = 0
    for directive in directives:
        if directive.authority in {
            DirectiveAuthority.REFERENCE_ONLY,
            DirectiveAuthority.ASSISTED_PROPOSAL,
        }:
            text_values = tuple(
                value for value in (directive.adaptation, directive.reason) if value
            )
            if any(
                marker in value.casefold()
                for value in text_values
                for marker in _PROMPT_INJECTION_MARKERS
            ):
                prompt_injection_count += 1
                abstentions.append(
                    DirectiveAbstention(
                        abstention_id=f"abstention.untrusted_instruction_text.{directive.directive_id}",
                        code="untrusted_instruction_text",
                        reason="instruction-like directive text remains inert and was not executed",
                        directive_ids=(directive.directive_id,),
                        scope_ids=(f"scope.{directive.directive_id}",),
                        source_ids=directive.evidence_ids or directive.source_observation_ids,
                        high_impact=False,
                    )
                )
                diagnostics.append(
                    _diagnostic(
                        ValidationSeverity.WARNING,
                        "untrusted_instruction_text",
                        (
                            f"directive {directive.directive_id!r} contains instruction-like "
                            "inert text"
                        ),
                    )
                )

    if role_graph is not None:
        if role_graph.status is ReferenceRoleGraphStatus.CONFLICTING:
            clarifications.append(
                DirectiveClarification(
                    "clarification.role_conflict",
                    "role_conflict",
                    "Which cross-asset identity or role candidate should be authoritative?",
                    source_ids=(role_graph.fingerprint,),
                    severity=ValidationSeverity.ERROR,
                    blocking=True,
                )
            )
        elif role_graph.status is ReferenceRoleGraphStatus.AMBIGUOUS:
            abstentions.append(
                DirectiveAbstention(
                    "abstention.role_ambiguity",
                    "role_ambiguity",
                    "cross-asset role candidates remain ambiguous; no identity winner was selected",
                    source_ids=(role_graph.fingerprint,),
                    high_impact=True,
                )
            )
        elif role_graph.status in {
            ReferenceRoleGraphStatus.PARTIAL,
            ReferenceRoleGraphStatus.EMPTY,
        }:
            abstentions.append(
                DirectiveAbstention(
                    "abstention.role_incomplete",
                    "role_incomplete",
                    (
                        "cross-asset role evidence is incomplete; downstream planning must "
                        "preserve the gap"
                    ),
                    source_ids=(role_graph.fingerprint,),
                    high_impact=False,
                )
            )

    if temporal_alignment is not None:
        if temporal_alignment.status is TemporalAlignmentStatus.CONFLICTING:
            clarifications.append(
                DirectiveClarification(
                    "clarification.temporal_conflict",
                    "temporal_conflict",
                    "Which temporal event or relation should be authoritative before planning?",
                    source_ids=(temporal_alignment.fingerprint,),
                    severity=ValidationSeverity.ERROR,
                    blocking=True,
                )
            )
        elif temporal_alignment.status is TemporalAlignmentStatus.AMBIGUOUS:
            abstentions.append(
                DirectiveAbstention(
                    "abstention.temporal_ambiguity",
                    "temporal_ambiguity",
                    "temporal event alignment remains ambiguous; no timeline winner was selected",
                    source_ids=(temporal_alignment.fingerprint,),
                    high_impact=True,
                )
            )
        elif temporal_alignment.status in {
            TemporalAlignmentStatus.PARTIAL,
            TemporalAlignmentStatus.EMPTY,
        }:
            abstentions.append(
                DirectiveAbstention(
                    "abstention.temporal_incomplete",
                    "temporal_incomplete",
                    "temporal evidence is incomplete; downstream planning must preserve the gap",
                    source_ids=(temporal_alignment.fingerprint,),
                    high_impact=False,
                )
            )
        if temporal_alignment.metrics.uncertain_causality_count:
            abstentions.append(
                DirectiveAbstention(
                    "abstention.uncertain_causality",
                    "uncertain_causality",
                    "causal relation is uncertain and was not converted into a directive fact",
                    source_ids=(temporal_alignment.fingerprint,),
                    high_impact=True,
                )
            )

    clarifications.sort(key=lambda item: item.clarification_id)
    abstentions.sort(key=lambda item: item.abstention_id)
    status = _status(
        resolved,
        directives=directives,
        clarifications=tuple(clarifications),
        abstentions=tuple(abstentions),
    )
    request_after = canonical_fingerprint(resolved.request.to_wire())
    mutation_guard_pass = request_before == request_after
    if not mutation_guard_pass:
        raise DirectiveAuthorityEngineError("directive request changed during authority projection")
    metrics = DirectiveAuthorityMetrics(
        input_count=len(resolved.request.directives),
        scope_count=len(scopes),
        accepted_count=sum(
            item.disposition is DirectiveEngineDisposition.ACCEPTED for item in decisions
        ),
        shadowed_count=sum(
            item.disposition is DirectiveEngineDisposition.SHADOWED for item in decisions
        ),
        rejected_count=sum(
            item.disposition is DirectiveEngineDisposition.REJECTED for item in decisions
        ),
        conflicting_count=sum(
            item.disposition is DirectiveEngineDisposition.CONFLICTING for item in decisions
        ),
        clarification_count=len(clarifications),
        blocking_clarification_count=sum(item.blocking for item in clarifications),
        abstention_count=len(abstentions),
        hard_constraint_violation_count=sum(
            item.code == "hard_constraint_conflict" for item in resolved.conflicts
        ),
        prompt_injection_count=prompt_injection_count,
        precedence_order_errors=precedence_order_errors,
        mutation_guard_pass=mutation_guard_pass,
        prompt_injection_guard_pass=True,
    )
    return DirectiveAuthorityReport(
        status=status,
        input_fingerprint=request_before,
        resolved_fingerprint=resolved_fingerprint,
        scopes=scopes,
        decisions=decisions,
        precedence=precedence,
        clarifications=tuple(clarifications),
        abstentions=tuple(abstentions),
        metrics=metrics,
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "DIRECTIVE_AUTHORITY_ENGINE_SCHEMA",
    "MAX_DIRECTIVE_ENGINE_ABSTENTIONS",
    "MAX_DIRECTIVE_ENGINE_CLARIFICATIONS",
    "MAX_DIRECTIVE_ENGINE_ITEMS",
    "MAX_DIRECTIVE_ENGINE_OUTPUT_BYTES",
    "MAX_DIRECTIVE_ENGINE_TEXT",
    "DirectiveAbstention",
    "DirectiveAuthorityDecision",
    "DirectiveAuthorityEngineStatus",
    "DirectiveAuthorityMetrics",
    "DirectiveAuthorityReport",
    "DirectiveClarification",
    "DirectiveEngineDisposition",
    "DirectivePrecedenceEntry",
    "DirectiveScope",
    "build_directive_authority_engine",
]
