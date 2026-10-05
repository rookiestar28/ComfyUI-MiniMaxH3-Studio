"""Explicit Full-Reference copy, retention, adaptation, and exclusion semantics.

The resolver in this module is deliberately deterministic and provider-free.  A directive is a
typed declaration tied to canonical source ownership; its free-text fields are inert descriptions,
never instructions to this process.  Equal-authority disagreements stay visible as conflicts and
reference-only proposals cannot replace immutable user constraints.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from .constraints import (
    ContentScope,
    DirectiveTarget,
    ExactTextKind,
    HardConstraintSet,
    KeepChangeAction,
)
from .contracts import (
    MediaKind,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import DirectiveSemanticsError
from .intent_graph import RetentionScope
from .registry import ReferenceRegistry

DIRECTIVE_SEMANTICS_SCHEMA = "h3.reference.directives.v2"
MAX_DIRECTIVES = 256
MAX_DIRECTIVE_SOURCE_IDS = 32
MAX_DIRECTIVE_OBSERVATION_IDS = 64
MAX_DIRECTIVE_EVIDENCE_IDS = 64
MAX_DIRECTIVE_CONSTRAINT_IDS = 64
MAX_DIRECTIVE_TEXT_LENGTH = 4096
MAX_DIRECTIVE_PRIORITY = 1000

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
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
    "sig=",
    "x-amz-",
)


class DirectiveAction(str, Enum):
    """Closed operations a Full-Reference directive may request."""

    COPY_EVENT = "copy_event"
    RETAIN = "retain"
    ADAPT = "adapt"
    EXCLUDE = "exclude"


class DirectiveTargetKind(str, Enum):
    """Explicit target families; no target is recovered from prose."""

    EVENT = "event"
    SUBJECT = "subject"
    VOICE = "voice"
    OBJECT = "object"
    SCENE = "scene"
    ACTION = "action"
    STYLE = "style"
    CAMERA = "camera"
    AUDIO = "audio"
    DIALOGUE = "dialogue"
    LYRICS = "lyrics"
    VISIBLE_TEXT = "visible_text"
    TIMELINE = "timeline"
    ASSET = "asset"


class RetentionAspect(str, Enum):
    """Closed aspects that can be retained independently."""

    IDENTITY = "identity"
    STYLE = "style"
    CAMERA = "camera"
    AUDIO = "audio"
    VOICE = "voice"
    SCENE = "scene"
    ACTION = "action"
    OBJECT = "object"


class DirectiveAuthority(str, Enum):
    """Precedence class, from immutable caller intent to untrusted assistance."""

    USER_HARD = "user_hard"
    USER_PREFERENCE = "user_preference"
    REFERENCE_ONLY = "reference_only"
    ASSISTED_PROPOSAL = "assisted_proposal"


class DirectiveDecision(str, Enum):
    """Decision made for each supplied directive."""

    ACCEPTED = "accepted"
    SHADOWED = "shadowed"
    REJECTED = "rejected"
    CONFLICTING = "conflicting"


class DirectiveSetStatus(str, Enum):
    """Aggregate outcome; non-complete states are inspectable, never plausible success."""

    EMPTY = "empty"
    COMPLETE = "complete"
    PARTIAL = "partial"
    CONFLICTING = "conflicting"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise DirectiveSemanticsError(f"{field} must be a bounded identifier")
    return value


def _optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_DIRECTIVE_TEXT_LENGTH:
        raise DirectiveSemanticsError(
            f"{field} must be a non-empty string of at most {MAX_DIRECTIVE_TEXT_LENGTH} characters"
        )
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise DirectiveSemanticsError(f"{field} contains an unsafe wire code point")
    lowered = value.casefold()
    if any(marker in lowered for marker in _FORBIDDEN_TEXT_MARKERS) or "\\" in value:
        raise DirectiveSemanticsError(f"{field} contains a forbidden locator or credential marker")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _id_tuple(value: object, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise DirectiveSemanticsError(f"{field} must be a tuple of at most {maximum} identifiers")
    result = tuple(_identifier(item, f"{field} item") for item in value)
    if len(result) != len(set(result)):
        raise DirectiveSemanticsError(f"{field} must not contain duplicate identifiers")
    return result


def _enum_tuple(value: object, expected: type[Enum], field: str, maximum: int) -> tuple[Enum, ...]:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise DirectiveSemanticsError(f"{field} must be a tuple of at most {maximum} values")
    if not all(isinstance(item, expected) for item in value):
        raise DirectiveSemanticsError(f"{field} contains an invalid value")
    if len(value) != len(set(value)):
        raise DirectiveSemanticsError(f"{field} must not contain duplicate values")
    return value


@dataclass(frozen=True, slots=True)
class ReferenceDirective:
    """One immutable typed directive with explicit source and authority ownership."""

    directive_id: str
    action: DirectiveAction
    target_kind: DirectiveTargetKind
    target_id: str
    source_asset_ids: tuple[str, ...] = ()
    source_observation_ids: tuple[str, ...] = ()
    source_event_id: str | None = None
    target_segment_id: str | None = None
    retention_aspects: tuple[RetentionAspect, ...] = ()
    adaptation: str | None = None
    reason: str | None = None
    authority: DirectiveAuthority = DirectiveAuthority.USER_PREFERENCE
    priority: int = 0
    evidence_ids: tuple[str, ...] = ()
    hard_constraint_ids: tuple[str, ...] = ()
    schema: str = DIRECTIVE_SEMANTICS_SCHEMA
    retention_scope: RetentionScope = RetentionScope.UNSPECIFIED

    def __post_init__(self) -> None:
        _identifier(self.directive_id, "directive_id")
        if not isinstance(self.action, DirectiveAction):
            raise DirectiveSemanticsError("action must be a DirectiveAction")
        if not isinstance(self.target_kind, DirectiveTargetKind):
            raise DirectiveSemanticsError("target_kind must be a DirectiveTargetKind")
        _identifier(self.target_id, "target_id")
        _id_tuple(self.source_asset_ids, "source_asset_ids", MAX_DIRECTIVE_SOURCE_IDS)
        _id_tuple(
            self.source_observation_ids, "source_observation_ids", MAX_DIRECTIVE_OBSERVATION_IDS
        )
        _optional_identifier(self.source_event_id, "source_event_id")
        _optional_identifier(self.target_segment_id, "target_segment_id")
        aspects = _enum_tuple(self.retention_aspects, RetentionAspect, "retention_aspects", 8)
        if not isinstance(self.retention_scope, RetentionScope):
            raise DirectiveSemanticsError("retention_scope must be a RetentionScope")
        if self.retention_scope is not RetentionScope.UNSPECIFIED:
            if self.action is not DirectiveAction.RETAIN:
                raise DirectiveSemanticsError("only RETAIN may declare retention scope")
            audio_scope = self.retention_scope in {
                RetentionScope.AUDIO_LAYER,
                RetentionScope.COMPLETE_FINAL_AUDIO_TRACK,
            }
            if audio_scope != (
                self.target_kind in {DirectiveTargetKind.AUDIO, DirectiveTargetKind.VOICE}
            ):
                raise DirectiveSemanticsError("retention scope contradicts target kind")
            # GUARD: scope names the retained entity; accepting a subject as a picture target
            # loses the declared role when the timeline producer joins the retention relation.
            if self.retention_scope is RetentionScope.SUBJECT:
                if self.target_kind is not DirectiveTargetKind.SUBJECT:
                    raise DirectiveSemanticsError(
                        "subject retention scope requires a subject target"
                    )
            elif self.retention_scope in {RetentionScope.PICTURE, RetentionScope.VIDEO_STRUCTURE}:
                if self.target_kind is not DirectiveTargetKind.ASSET:
                    raise DirectiveSemanticsError(
                        "picture/video retention scope requires an asset target"
                    )
        _id_tuple(self.evidence_ids, "evidence_ids", MAX_DIRECTIVE_EVIDENCE_IDS)
        _id_tuple(self.hard_constraint_ids, "hard_constraint_ids", MAX_DIRECTIVE_CONSTRAINT_IDS)
        if not isinstance(self.authority, DirectiveAuthority):
            raise DirectiveSemanticsError("authority must be a DirectiveAuthority")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise DirectiveSemanticsError("priority must be an integer")
        if not 0 <= self.priority <= MAX_DIRECTIVE_PRIORITY:
            raise DirectiveSemanticsError("priority is outside its finite bound")
        if self.schema != DIRECTIVE_SEMANTICS_SCHEMA:
            raise DirectiveSemanticsError("unsupported directive schema")

        if self.action is DirectiveAction.COPY_EVENT:
            if self.target_kind is not DirectiveTargetKind.EVENT:
                raise DirectiveSemanticsError("COPY_EVENT targets must use the event target kind")
            if self.source_event_id is None or self.target_segment_id is None:
                raise DirectiveSemanticsError(
                    "COPY_EVENT requires source_event_id and target_segment_id"
                )
            if not self.source_asset_ids:
                raise DirectiveSemanticsError("COPY_EVENT requires source asset ownership")
            if aspects or self.adaptation is not None or self.reason is not None:
                raise DirectiveSemanticsError("COPY_EVENT cannot carry retain/adapt/exclude fields")
        elif self.action is DirectiveAction.RETAIN:
            if not aspects:
                raise DirectiveSemanticsError("RETAIN requires at least one retention aspect")
            if self.adaptation is not None or self.reason is not None:
                raise DirectiveSemanticsError("RETAIN cannot carry adaptation or exclusion text")
        elif self.action is DirectiveAction.ADAPT:
            if self.adaptation is None:
                raise DirectiveSemanticsError("ADAPT requires an inert adaptation description")
            _text(self.adaptation, "adaptation")
            if aspects or self.reason is not None:
                raise DirectiveSemanticsError("ADAPT cannot carry retention or exclusion fields")
        else:
            if self.reason is None:
                raise DirectiveSemanticsError("EXCLUDE requires an inert reason")
            _text(self.reason, "reason")
            if aspects or self.adaptation is not None:
                raise DirectiveSemanticsError("EXCLUDE cannot carry retention or adaptation fields")

        if self.authority in {
            DirectiveAuthority.REFERENCE_ONLY,
            DirectiveAuthority.ASSISTED_PROPOSAL,
        }:
            if not self.source_asset_ids and not self.source_observation_ids:
                raise DirectiveSemanticsError(
                    "untrusted directives require explicit source ownership"
                )
            if not self.evidence_ids:
                raise DirectiveSemanticsError("untrusted directives require evidence_ids")
        if self.action is not DirectiveAction.ADAPT and self.adaptation is not None:
            raise DirectiveSemanticsError("only ADAPT may carry adaptation text")
        if self.action is not DirectiveAction.EXCLUDE and self.reason is not None:
            raise DirectiveSemanticsError("only EXCLUDE may carry a reason")

    @property
    def target(self) -> DirectiveTargetKind:
        """Compatibility alias for callers that use ``target`` terminology."""

        return self.target_kind

    def semantic_key(self) -> tuple[object, ...]:
        """Return a stable value key excluding the caller-owned directive ID."""

        return (
            self.action.value,
            self.target_kind.value,
            self.target_id,
            self.source_asset_ids,
            self.source_observation_ids,
            self.source_event_id,
            self.target_segment_id,
            tuple(item.value for item in self.retention_aspects),
            self.adaptation,
            self.reason,
            self.authority.value,
            self.priority,
            self.evidence_ids,
            self.hard_constraint_ids,
            self.retention_scope.value,
        )

    def target_keys(self) -> tuple[tuple[str, ...], ...]:
        """Return one or more canonical conflict keys for this directive."""

        if self.action is DirectiveAction.COPY_EVENT:
            return (("event", self.source_event_id or "", self.target_segment_id or ""),)
        if self.action is DirectiveAction.RETAIN:
            return tuple(
                ("target", self.target_kind.value, self.target_id, aspect.value)
                for aspect in self.retention_aspects
            )
        return (("target", self.target_kind.value, self.target_id),)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "directive_id": self.directive_id,
            "action": self.action.value,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "source_asset_ids": list(self.source_asset_ids),
            "source_observation_ids": list(self.source_observation_ids),
            "source_event_id": self.source_event_id,
            "target_segment_id": self.target_segment_id,
            "retention_aspects": [item.value for item in self.retention_aspects],
            "retention_scope": self.retention_scope.value,
            "adaptation": self.adaptation,
            "reason": self.reason,
            "authority": self.authority.value,
            "priority": self.priority,
            "evidence_ids": list(self.evidence_ids),
            "hard_constraint_ids": list(self.hard_constraint_ids),
        }


@dataclass(frozen=True, slots=True)
class DirectiveConflict:
    """A deterministic non-winning or unsafe directive decision."""

    conflict_id: str
    code: str
    directive_ids: tuple[str, ...]
    message: str
    severity: ValidationSeverity = ValidationSeverity.ERROR
    schema: str = DIRECTIVE_SEMANTICS_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.conflict_id, "conflict_id")
        if _CODE_PATTERN.fullmatch(self.code) is None:
            raise DirectiveSemanticsError("conflict code must be a bounded lower-case code")
        _id_tuple(self.directive_ids, "directive_ids", MAX_DIRECTIVES)
        _text(self.message, "conflict message")
        if not isinstance(self.severity, ValidationSeverity):
            raise DirectiveSemanticsError("conflict severity must be a ValidationSeverity")
        if self.schema != DIRECTIVE_SEMANTICS_SCHEMA:
            raise DirectiveSemanticsError("unsupported conflict schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "conflict_id": self.conflict_id,
            "code": self.code,
            "directive_ids": list(self.directive_ids),
            "message": self.message,
            "severity": self.severity.value,
        }


@dataclass(frozen=True, slots=True)
class DirectiveDecisionRecord:
    """Auditable per-input decision, including exact duplicates and shadowed alternatives."""

    directive_id: str
    decision: DirectiveDecision
    conflict_ids: tuple[str, ...] = ()
    reason: str | None = None
    schema: str = DIRECTIVE_SEMANTICS_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.directive_id, "decision directive_id")
        if not isinstance(self.decision, DirectiveDecision):
            raise DirectiveSemanticsError("decision must be a DirectiveDecision")
        _id_tuple(self.conflict_ids, "decision conflict_ids", MAX_DIRECTIVES)
        _optional_text(self.reason, "decision reason")
        if self.schema != DIRECTIVE_SEMANTICS_SCHEMA:
            raise DirectiveSemanticsError("unsupported decision schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "directive_id": self.directive_id,
            "decision": self.decision.value,
            "conflict_ids": list(self.conflict_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DirectiveRequest:
    """Resolver input with canonical registry and immutable hard-constraint ownership."""

    task_mode: TaskMode
    reference_registry: ReferenceRegistry
    directives: tuple[ReferenceDirective, ...] = ()
    hard_constraints: HardConstraintSet = HardConstraintSet.empty()
    schema: str = DIRECTIVE_SEMANTICS_SCHEMA

    def __post_init__(self) -> None:
        if self.task_mode is not TaskMode.REF2VA:
            raise DirectiveSemanticsError("directive requests require REF2VA task mode")
        if not isinstance(self.reference_registry, ReferenceRegistry):
            raise DirectiveSemanticsError("reference_registry must be a ReferenceRegistry")
        if not isinstance(self.directives, tuple) or len(self.directives) > MAX_DIRECTIVES:
            raise DirectiveSemanticsError(
                f"directives must contain at most {MAX_DIRECTIVES} values"
            )
        if not all(isinstance(item, ReferenceDirective) for item in self.directives):
            raise DirectiveSemanticsError("directives must contain ReferenceDirective values")
        if not isinstance(self.hard_constraints, HardConstraintSet):
            raise DirectiveSemanticsError("hard_constraints must be a HardConstraintSet")
        if self.schema != DIRECTIVE_SEMANTICS_SCHEMA:
            raise DirectiveSemanticsError("unsupported directive request schema")

        by_id: dict[str, ReferenceDirective] = {}
        for directive in self.directives:
            existing = by_id.get(directive.directive_id)
            if existing is not None and existing != directive:
                raise DirectiveSemanticsError(
                    f"directive_id {directive.directive_id!r} has conflicting values"
                )
            by_id[directive.directive_id] = directive
            known_assets = {asset.asset_id: asset for asset in self.reference_registry.assets}
            for asset_id in directive.source_asset_ids:
                asset = known_assets.get(asset_id)
                if asset is None:
                    raise DirectiveSemanticsError(
                        f"directive references unknown asset {asset_id!r}"
                    )
                _validate_media_compatibility(directive, asset.kind)
            known_constraints = {value.constraint_id for value in self.hard_constraints.constraints}
            for constraint_id in directive.hard_constraint_ids:
                if constraint_id not in known_constraints:
                    raise DirectiveSemanticsError(
                        f"directive references unknown hard constraint {constraint_id!r}"
                    )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "task_mode": self.task_mode.value,
            "reference_registry": self.reference_registry.to_wire(),
            "directives": [item.to_wire() for item in self.directives],
            "hard_constraints": self.hard_constraints.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ResolvedDirectiveSet:
    """Immutable result retaining accepted directives and every non-winning decision."""

    request: DirectiveRequest
    status: DirectiveSetStatus
    accepted: tuple[ReferenceDirective, ...]
    decision_records: tuple[DirectiveDecisionRecord, ...]
    conflicts: tuple[DirectiveConflict, ...]
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    schema: str = DIRECTIVE_SEMANTICS_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.request, DirectiveRequest):
            raise DirectiveSemanticsError("result request must be a DirectiveRequest")
        if not isinstance(self.status, DirectiveSetStatus):
            raise DirectiveSemanticsError("status must be a DirectiveSetStatus")
        if not isinstance(self.accepted, tuple) or not all(
            isinstance(item, ReferenceDirective) for item in self.accepted
        ):
            raise DirectiveSemanticsError("accepted must contain ReferenceDirective values")
        if not isinstance(self.decision_records, tuple) or not all(
            isinstance(item, DirectiveDecisionRecord) for item in self.decision_records
        ):
            raise DirectiveSemanticsError("decision_records must contain typed records")
        if not isinstance(self.conflicts, tuple) or not all(
            isinstance(item, DirectiveConflict) for item in self.conflicts
        ):
            raise DirectiveSemanticsError("conflicts must contain DirectiveConflict values")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise DirectiveSemanticsError("diagnostics must contain ValidationDiagnostic values")
        if self.schema != DIRECTIVE_SEMANTICS_SCHEMA:
            raise DirectiveSemanticsError("unsupported result schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "request": self.request.to_wire(),
            "accepted": [item.to_wire() for item in self.accepted],
            "decision_records": [item.to_wire() for item in self.decision_records],
            "conflicts": [item.to_wire() for item in self.conflicts],
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


_AUTHORITY_RANK = {
    DirectiveAuthority.USER_HARD: 4,
    DirectiveAuthority.USER_PREFERENCE: 3,
    DirectiveAuthority.REFERENCE_ONLY: 2,
    DirectiveAuthority.ASSISTED_PROPOSAL: 1,
}


def _validate_media_compatibility(directive: ReferenceDirective, kind: MediaKind) -> None:
    if directive.action is DirectiveAction.COPY_EVENT:
        allowed = {MediaKind.VIDEO, MediaKind.AUDIO}
    elif directive.target_kind in {
        DirectiveTargetKind.AUDIO,
        DirectiveTargetKind.VOICE,
        DirectiveTargetKind.DIALOGUE,
        DirectiveTargetKind.LYRICS,
    }:
        allowed = {MediaKind.AUDIO}
    elif directive.target_kind is DirectiveTargetKind.TIMELINE:
        allowed = {MediaKind.VIDEO}
    elif directive.target_kind is DirectiveTargetKind.ASSET:
        allowed = {MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.AUDIO}
    else:
        allowed = {MediaKind.IMAGE, MediaKind.VIDEO}
    if kind not in allowed:
        names = ", ".join(sorted(item.value for item in allowed))
        raise DirectiveSemanticsError(
            f"{directive.directive_id} target {directive.target_kind.value!r} cannot use "
            f"{kind.value!r}; "
            f"allowed media kinds: {names}"
        )


def _constraint_target(kind: DirectiveTargetKind) -> DirectiveTarget | None:
    mapping = {
        DirectiveTargetKind.SUBJECT: DirectiveTarget.SUBJECT,
        DirectiveTargetKind.SCENE: DirectiveTarget.SCENE,
        DirectiveTargetKind.ACTION: DirectiveTarget.ACTION,
        DirectiveTargetKind.CAMERA: DirectiveTarget.CAMERA,
        DirectiveTargetKind.STYLE: DirectiveTarget.STYLE,
        DirectiveTargetKind.AUDIO: DirectiveTarget.AUDIO,
        DirectiveTargetKind.DIALOGUE: DirectiveTarget.DIALOGUE,
        DirectiveTargetKind.LYRICS: DirectiveTarget.LYRICS,
        DirectiveTargetKind.VISIBLE_TEXT: DirectiveTarget.VISIBLE_TEXT,
        DirectiveTargetKind.ASSET: DirectiveTarget.ASSET,
    }
    return mapping.get(kind)


def _hard_constraint_conflicts(
    directive: ReferenceDirective, constraints: HardConstraintSet
) -> tuple[str, ...]:
    """Return hard constraint IDs touched by lower-authority reference instructions."""

    if directive.authority in {DirectiveAuthority.USER_HARD, DirectiveAuthority.USER_PREFERENCE}:
        return ()
    touched: set[str] = set(directive.hard_constraint_ids)
    if directive.target_kind in {
        DirectiveTargetKind.DIALOGUE,
        DirectiveTargetKind.LYRICS,
        DirectiveTargetKind.VISIBLE_TEXT,
    }:
        expected_kind = {
            DirectiveTargetKind.DIALOGUE: ExactTextKind.DIALOGUE,
            DirectiveTargetKind.LYRICS: ExactTextKind.LYRICS,
            DirectiveTargetKind.VISIBLE_TEXT: ExactTextKind.VISIBLE_TEXT,
        }[directive.target_kind]
        touched.update(
            item.constraint_id for item in constraints.exact_texts if item.kind is expected_kind
        )
    if directive.target_kind is DirectiveTargetKind.TIMELINE:
        touched.update(item.constraint_id for item in constraints.timings)
    if directive.target_kind is DirectiveTargetKind.ASSET:
        touched.update(
            item.constraint_id for item in constraints.required_content if item.asset_id is not None
        )
        touched.update(
            item.constraint_id
            for item in constraints.forbidden_content
            if item.asset_id is not None
        )

    mapped = _constraint_target(directive.target_kind)
    if mapped is not None:
        for item in constraints.keep_change_directives:
            if item.target is mapped:
                if (
                    directive.action is not DirectiveAction.RETAIN
                    or item.action is not KeepChangeAction.KEEP
                ):
                    touched.add(item.constraint_id)
        scope = {
            DirectiveTargetKind.SUBJECT: ContentScope.VISUAL,
            DirectiveTargetKind.SCENE: ContentScope.SCENE,
            DirectiveTargetKind.ACTION: ContentScope.ACTION,
            DirectiveTargetKind.CAMERA: ContentScope.CAMERA,
            DirectiveTargetKind.STYLE: ContentScope.STYLE,
            DirectiveTargetKind.AUDIO: ContentScope.AUDIO,
            DirectiveTargetKind.DIALOGUE: ContentScope.DIALOGUE,
            DirectiveTargetKind.LYRICS: ContentScope.LYRICS,
            DirectiveTargetKind.VISIBLE_TEXT: ContentScope.VISIBLE_TEXT,
        }.get(directive.target_kind)
        if scope is not None:
            touched.update(
                item.constraint_id
                for item in constraints.required_content
                if item.scope in {scope, ContentScope.GENERAL}
            )
            touched.update(
                item.constraint_id
                for item in constraints.forbidden_content
                if item.scope in {scope, ContentScope.GENERAL}
            )
    return tuple(sorted(touched))


def _conflict_id(code: str, directive_ids: Iterable[str]) -> str:
    joined = "-".join(sorted(set(directive_ids))) or "none"
    raw = f"conflict.{code}.{joined}"
    return raw[:128]


def _diagnostic(severity: ValidationSeverity, code: str, message: str) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, "directives")


def resolve_reference_directives(request: DirectiveRequest) -> ResolvedDirectiveSet:
    """Resolve directives by explicit authority and deterministic conflict rules."""

    if not isinstance(request, DirectiveRequest):
        raise DirectiveSemanticsError("request must be a DirectiveRequest")
    if not request.directives:
        return ResolvedDirectiveSet(request, DirectiveSetStatus.EMPTY, (), (), (), ())

    conflicts: list[DirectiveConflict] = []
    diagnostics: list[ValidationDiagnostic] = []
    decisions: dict[str, list[DirectiveDecisionRecord]] = {
        item.directive_id: [] for item in request.directives
    }
    accepted: set[str] = set()
    unique: dict[str, ReferenceDirective] = {}
    exact_duplicate_ids: set[str] = set()
    for directive in request.directives:
        previous = unique.get(directive.directive_id)
        if previous is None:
            unique[directive.directive_id] = directive
        elif previous == directive:
            exact_duplicate_ids.add(directive.directive_id)

    # Lower-authority proposals touching immutable user constraints fail before precedence grouping.
    eligible: list[ReferenceDirective] = []
    for directive in sorted(unique.values(), key=lambda item: item.directive_id):
        hard_ids = _hard_constraint_conflicts(directive, request.hard_constraints)
        if hard_ids:
            conflict = DirectiveConflict(
                _conflict_id("hard_constraint_conflict", (directive.directive_id,)),
                "hard_constraint_conflict",
                (directive.directive_id,),
                f"directive {directive.directive_id!r} cannot override hard constraints "
                f"{', '.join(hard_ids)}",
            )
            conflicts.append(conflict)
            decisions[directive.directive_id].append(
                DirectiveDecisionRecord(
                    directive.directive_id,
                    DirectiveDecision.REJECTED,
                    (conflict.conflict_id,),
                    "reference-only directive cannot override immutable user constraints",
                )
            )
        else:
            eligible.append(directive)

    grouped_ids: set[str] = set()
    by_key: dict[tuple[str, ...], list[ReferenceDirective]] = {}
    for directive in eligible:
        for key in directive.target_keys():
            by_key.setdefault(key, []).append(directive)

    def resolve_group(key: tuple[str, ...], values: list[ReferenceDirective]) -> None:
        group = sorted(values, key=lambda item: item.directive_id)
        if any(item.directive_id in grouped_ids for item in group):
            # A multi-aspect RETAIN may already have been resolved by another aspect key.
            return
        grouped_ids.update(item.directive_id for item in group)
        highest = max((_AUTHORITY_RANK[item.authority], item.priority) for item in group)
        winners = [
            item for item in group if (_AUTHORITY_RANK[item.authority], item.priority) == highest
        ]
        if len(winners) > 1:
            ids = tuple(sorted(item.directive_id for item in winners))
            conflict = DirectiveConflict(
                _conflict_id("equal_precedence_conflict", ids),
                "equal_precedence_conflict",
                ids,
                f"directives {', '.join(ids)} have incompatible equal precedence for target "
                f"{key!r}",
            )
            conflicts.append(conflict)
            for item in winners:
                decisions[item.directive_id].append(
                    DirectiveDecisionRecord(
                        item.directive_id,
                        DirectiveDecision.CONFLICTING,
                        (conflict.conflict_id,),
                        "equal precedence requires an explicit caller decision",
                    )
                )
            for item in group:
                if item not in winners:
                    decisions[item.directive_id].append(
                        DirectiveDecisionRecord(
                            item.directive_id,
                            DirectiveDecision.SHADOWED,
                            (conflict.conflict_id,),
                            "lower precedence than an unresolved conflict",
                        )
                    )
            return

        winner = winners[0]
        accepted.add(winner.directive_id)
        decisions[winner.directive_id].append(
            DirectiveDecisionRecord(winner.directive_id, DirectiveDecision.ACCEPTED)
        )
        for item in group:
            if item.directive_id == winner.directive_id:
                continue
            conflict = DirectiveConflict(
                _conflict_id("shadowed", (winner.directive_id, item.directive_id)),
                "shadowed",
                tuple(sorted({winner.directive_id, item.directive_id})),
                f"directive {item.directive_id!r} is shadowed by higher-precedence directive "
                f"{winner.directive_id!r}",
                ValidationSeverity.WARNING,
            )
            conflicts.append(conflict)
            decisions[item.directive_id].append(
                DirectiveDecisionRecord(
                    item.directive_id,
                    DirectiveDecision.SHADOWED,
                    (conflict.conflict_id,),
                    "lower authority or priority for the same target",
                )
            )
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.WARNING,
                    "directive_shadowed",
                    f"directive {item.directive_id!r} was shadowed by {winner.directive_id!r}",
                )
            )

    # A RETAIN aspect and an ADAPT/EXCLUDE/COPY operation on the same target are incompatible even
    # when the retain value contains a more specific aspect key.  Retain-only groups stay split by
    # aspect so identity and style can be declared independently.
    base_groups: dict[tuple[str, ...], list[ReferenceDirective]] = {}
    for directive in eligible:
        base_key: tuple[str, ...]
        if directive.action is DirectiveAction.RETAIN:
            base_key = ("target", directive.target_kind.value, directive.target_id)
        else:
            base_key = directive.target_keys()[0]
        base_groups.setdefault(base_key, []).append(directive)
    for key in sorted(base_groups):
        values = base_groups[key]
        if any(item.action is not DirectiveAction.RETAIN for item in values) and any(
            item.action is DirectiveAction.RETAIN for item in values
        ):
            resolve_group(key, values)

    for key in sorted(by_key):
        resolve_group(key, by_key[key])

    # Preserve exact duplicate rows for audit while only one value can be accepted.
    for directive_id in sorted(exact_duplicate_ids):
        duplicate_count = sum(item.directive_id == directive_id for item in request.directives) - 1
        for _ in range(max(0, duplicate_count)):
            decisions[directive_id].append(
                DirectiveDecisionRecord(
                    directive_id,
                    DirectiveDecision.SHADOWED,
                    (),
                    "exact duplicate retained once in the accepted set",
                )
            )

    conflicts.sort(key=lambda item: (item.code, item.conflict_id))
    decision_records = tuple(
        record for directive_id in sorted(decisions) for record in decisions[directive_id]
    )
    accepted_values = tuple(unique[item] for item in sorted(accepted))
    if any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL} for item in conflicts
    ):
        status = DirectiveSetStatus.CONFLICTING
    elif accepted_values and len(accepted_values) == len(unique):
        status = DirectiveSetStatus.COMPLETE
    elif accepted_values:
        status = DirectiveSetStatus.PARTIAL
    else:
        status = DirectiveSetStatus.CONFLICTING
    return ResolvedDirectiveSet(
        request,
        status,
        accepted_values,
        decision_records,
        tuple(conflicts),
        tuple(diagnostics),
    )


__all__ = [
    "DIRECTIVE_SEMANTICS_SCHEMA",
    "MAX_DIRECTIVES",
    "MAX_DIRECTIVE_CONSTRAINT_IDS",
    "MAX_DIRECTIVE_EVIDENCE_IDS",
    "MAX_DIRECTIVE_OBSERVATION_IDS",
    "MAX_DIRECTIVE_PRIORITY",
    "MAX_DIRECTIVE_SOURCE_IDS",
    "MAX_DIRECTIVE_TEXT_LENGTH",
    "DirectiveAction",
    "DirectiveAuthority",
    "DirectiveConflict",
    "DirectiveDecision",
    "DirectiveDecisionRecord",
    "DirectiveRequest",
    "DirectiveSetStatus",
    "DirectiveTargetKind",
    "ReferenceDirective",
    "ResolvedDirectiveSet",
    "RetentionAspect",
    "resolve_reference_directives",
]
