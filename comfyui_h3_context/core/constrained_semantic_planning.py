"""Strict, provider-neutral semantic proposal validation for M13-08.

The model is an explicitly selected proposal engine.  This module parses one complete JSON
document, checks it against the accepted reduction/timeline hand-off, and emits a new immutable
revision receipt.  It never calls a model, imports ComfyUI/Ollama, authors the final H3 protocol, or
mutates accepted evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .constraints import ExactTextConstraint
from .contracts import TaskMode, ValidationDiagnostic, ValidationSeverity
from .errors import ConstrainedSemanticPlanningError
from .feasible_av_timeline_planner import FeasibleAVTimelinePlan
from .hierarchical_evidence_reduction import (
    HierarchicalReductionPlan,
    ReductionStatus,
)
from .model_manifest import (
    MODEL_GENERATION_RESULT_SCHEMA,
    ModelBackendFamily,
    ModelGenerationRequest,
    ModelGenerationResult,
)
from .semantic_intents import (
    SemanticIntentError,
    TypedSemanticIntent,
    decode_typed_semantic_intent,
    validate_semantic_dialogue_bindings,
)

CONSTRAINED_SEMANTIC_PLANNING_SCHEMA = "h3.constrained_semantic_planning.v1"
TYPED_SEMANTIC_PLANNING_SCHEMA = "h3.constrained_semantic_planning.v2"
SEMANTIC_PROPOSAL_OUTPUT_SCHEMA = "h3.constrained_semantic_proposal.v1"
MAX_SEMANTIC_PROPOSALS = 64
MAX_SEMANTIC_SOURCE_IDS = 64
MAX_SEMANTIC_TEXT = 4096
MAX_SEMANTIC_PROTECTED_TEXT = 64
MAX_SEMANTIC_DIFF_FIELDS = 128
MAX_SEMANTIC_OUTPUT_BYTES = 262_144
MAX_SEMANTIC_PROMPT_CHARS = 32_000

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
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
_INJECTION = (
    "ignore previous",
    "ignore all previous",
    "system:",
    "developer:",
    "assistant:",
    "user:",
    "tool_call",
    "jailbreak",
    "<|",
    "execute command",
)


class SemanticPlanningPolicy(str, Enum):
    STRICT = "strict"
    EVIDENCE_BOUNDED = "evidence_bounded"
    CREATIVE_ALLOWED = "creative_allowed"


class SemanticPlanningStatus(str, Enum):
    COMPLETE = "complete"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class SemanticEvidenceLabel(str, Enum):
    SOURCE = "source"
    INFERRED = "inferred"
    CREATIVE = "creative"


class SemanticTargetKind(str, Enum):
    ASSET = "asset"
    SHOT = "shot"
    SEGMENT = "segment"
    SUBJECT = "subject"
    SCENE = "scene"
    ACTION = "action"
    CAMERA = "camera"
    STYLE = "style"
    AUDIO = "audio"
    EVENT = "event"
    RELATION = "relation"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ConstrainedSemanticPlanningError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise ConstrainedSemanticPlanningError(
            f"{field_name} contains sensitive or locator material"
        )
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ConstrainedSemanticPlanningError(f"{field_name} must be a SHA-256 fingerprint")
    return value


def _text(value: object, field_name: str, maximum: int = MAX_SEMANTIC_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ConstrainedSemanticPlanningError(f"{field_name} is outside its bounded text limit")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise ConstrainedSemanticPlanningError(
            f"{field_name} contains sensitive or locator material"
        )
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ConstrainedSemanticPlanningError(f"{field_name} contains an unsafe wire code point")
    return value


def _ids(
    values: object, field_name: str, maximum: int = MAX_SEMANTIC_SOURCE_IDS
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise ConstrainedSemanticPlanningError(f"{field_name} is outside its bounded envelope")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise ConstrainedSemanticPlanningError(f"{field_name} must not contain duplicates")
    return result


def _enum(value: object, expected: type[Enum], field_name: str) -> Enum:
    if isinstance(value, expected):
        return value
    try:
        return expected(value)
    except (TypeError, ValueError):
        raise ConstrainedSemanticPlanningError(f"{field_name} is unsupported") from None


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, str) and _DECIMAL.fullmatch(value):
        try:
            result = Decimal(value)
        except InvalidOperation:
            raise ConstrainedSemanticPlanningError(f"{field_name} is not a decimal") from None
    else:
        raise ConstrainedSemanticPlanningError(f"{field_name} must be a decimal string")
    if not result.is_finite() or result < Decimal("0") or result > Decimal("1"):
        raise ConstrainedSemanticPlanningError(f"{field_name} must be between 0 and 1")
    return result


def _positive_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ConstrainedSemanticPlanningError(f"{field_name} is outside its finite bound")
    return value


def _nonnegative_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ConstrainedSemanticPlanningError(f"{field_name} is outside its finite bound")
    return value


def _diagnostic(
    code: str, message: str, severity: ValidationSeverity = ValidationSeverity.ERROR
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, "constrained_semantic_planning")


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _parse_json(text: str) -> dict[str, object]:
    if not isinstance(text, str) or not text:
        raise ConstrainedSemanticPlanningError("model proposal is outside the bounded output")
    try:
        byte_length = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ConstrainedSemanticPlanningError("model proposal contains unsafe Unicode") from None
    if byte_length > MAX_SEMANTIC_OUTPUT_BYTES:
        raise ConstrainedSemanticPlanningError("model proposal is outside the bounded output")
    stripped = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", stripped)
    if fence is not None:
        text = fence.group(1)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ConstrainedSemanticPlanningError("model proposal is not complete valid JSON") from exc
    if not isinstance(value, dict):
        raise ConstrainedSemanticPlanningError("model proposal must be one JSON object")
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class SemanticPlanningBudget:
    max_proposals: int = MAX_SEMANTIC_PROPOSALS
    max_output_tokens: int = 2048
    max_claim_chars: int = MAX_SEMANTIC_TEXT
    max_diff_fields: int = MAX_SEMANTIC_DIFF_FIELDS
    schema: str = CONSTRAINED_SEMANTIC_PLANNING_SCHEMA

    def __post_init__(self) -> None:
        _positive_int(self.max_proposals, "max_proposals", MAX_SEMANTIC_PROPOSALS)
        _positive_int(self.max_output_tokens, "max_output_tokens", 4_000_000)
        _positive_int(self.max_claim_chars, "max_claim_chars", MAX_SEMANTIC_TEXT)
        _positive_int(self.max_diff_fields, "max_diff_fields", MAX_SEMANTIC_DIFF_FIELDS)
        if self.schema != CONSTRAINED_SEMANTIC_PLANNING_SCHEMA:
            raise ConstrainedSemanticPlanningError("unsupported semantic planning budget schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_proposals": self.max_proposals,
            "max_output_tokens": self.max_output_tokens,
            "max_claim_chars": self.max_claim_chars,
            "max_diff_fields": self.max_diff_fields,
        }


@dataclass(frozen=True, slots=True)
class SemanticPlanningProfile:
    """One explicitly selected native or Ollama proposal profile."""

    profile_id: str
    backend_family: ModelBackendFamily
    model_id: str
    model_digest: str
    seed: int | None = 0
    raw_media_capability: bool = False
    schema: str = CONSTRAINED_SEMANTIC_PLANNING_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "profile_id")
        if not isinstance(self.backend_family, ModelBackendFamily):
            raise ConstrainedSemanticPlanningError("backend_family must be ModelBackendFamily")
        _identifier(self.model_id, "model_id")
        _fingerprint(self.model_digest, "model_digest")
        if self.seed is not None and (
            isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0
        ):
            raise ConstrainedSemanticPlanningError("seed must be a non-negative integer or None")
        if not isinstance(self.raw_media_capability, bool):
            raise ConstrainedSemanticPlanningError("raw_media_capability must be a boolean")
        if self.schema != CONSTRAINED_SEMANTIC_PLANNING_SCHEMA:
            raise ConstrainedSemanticPlanningError("unsupported semantic planning profile schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "backend_family": self.backend_family.value,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "seed": self.seed,
            "raw_media_capability": self.raw_media_capability,
        }


@dataclass(frozen=True, slots=True)
class ExactTextSnapshot:
    target_id: str
    text: str
    schema: str = CONSTRAINED_SEMANTIC_PLANNING_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.target_id, "exact text target_id")
        _text(self.text, "exact text", MAX_SEMANTIC_TEXT)
        if self.schema != CONSTRAINED_SEMANTIC_PLANNING_SCHEMA:
            raise ConstrainedSemanticPlanningError("unsupported exact text schema")

    def to_wire(self) -> dict[str, object]:
        return {"schema": self.schema, "target_id": self.target_id, "text": self.text}


@dataclass(frozen=True, slots=True)
class SemanticPlanningProposal:
    proposal_id: str
    target_kind: SemanticTargetKind | str
    target_id: str
    claim: str
    evidence_label: SemanticEvidenceLabel | str
    source_ids: tuple[str, ...]
    confidence: Decimal = Decimal("0.50")
    rationale: str | None = None
    schema: str = CONSTRAINED_SEMANTIC_PLANNING_SCHEMA
    intent: TypedSemanticIntent | None = None

    def __post_init__(self) -> None:
        _identifier(self.proposal_id, "proposal_id")
        object.__setattr__(
            self, "target_kind", _enum(self.target_kind, SemanticTargetKind, "target_kind")
        )
        _identifier(self.target_id, "target_id")
        _text(self.claim, "claim")
        object.__setattr__(
            self,
            "evidence_label",
            _enum(self.evidence_label, SemanticEvidenceLabel, "evidence_label"),
        )
        object.__setattr__(self, "source_ids", _ids(self.source_ids, "source_ids"))
        if not self.source_ids:
            raise ConstrainedSemanticPlanningError("proposal source_ids must not be empty")
        object.__setattr__(self, "confidence", _decimal(self.confidence, "confidence"))
        if self.rationale is not None:
            _text(self.rationale, "proposal rationale", 1024)
        if self.schema == TYPED_SEMANTIC_PLANNING_SCHEMA:
            if (
                type(self.intent) is not TypedSemanticIntent
                or self.intent.kind.value != cast(SemanticTargetKind, self.target_kind).value
                or self.intent.description != self.claim
            ):
                raise ConstrainedSemanticPlanningError(
                    "typed intent does not match proposal target"
                )
        elif self.schema != CONSTRAINED_SEMANTIC_PLANNING_SCHEMA or self.intent is not None:
            raise ConstrainedSemanticPlanningError("unsupported semantic proposal schema")

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": self.schema,
            "proposal_id": self.proposal_id,
            "target_kind": cast(SemanticTargetKind, self.target_kind).value,
            "target_id": self.target_id,
            "claim": self.claim,
            "evidence_label": cast(SemanticEvidenceLabel, self.evidence_label).value,
            "source_ids": list(self.source_ids),
            "confidence": format(self.confidence, "f"),
            "rationale": self.rationale,
        }
        if self.intent is not None:
            del result["claim"]
            result["intent"] = self.intent.to_wire()
        return result


@dataclass(frozen=True, slots=True)
class SemanticProposalDocument:
    document_id: str
    policy: SemanticPlanningPolicy | str
    task_mode: TaskMode | str
    effective_duration: str
    asset_ids: tuple[str, ...]
    reference_order: tuple[str, ...]
    timeline_fingerprint: str
    reduction_fingerprint: str
    preserved_exact_text: tuple[ExactTextSnapshot, ...]
    proposals: tuple[SemanticPlanningProposal, ...]
    complete: bool = True
    schema: str = CONSTRAINED_SEMANTIC_PLANNING_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.document_id, "document_id")
        object.__setattr__(self, "policy", _enum(self.policy, SemanticPlanningPolicy, "policy"))
        object.__setattr__(self, "task_mode", _enum(self.task_mode, TaskMode, "task_mode"))
        _text(self.effective_duration, "effective_duration", 128)
        object.__setattr__(self, "asset_ids", _ids(self.asset_ids, "asset_ids"))
        object.__setattr__(self, "reference_order", _ids(self.reference_order, "reference_order"))
        _fingerprint(self.timeline_fingerprint, "timeline_fingerprint")
        _fingerprint(self.reduction_fingerprint, "reduction_fingerprint")
        if (
            not isinstance(self.preserved_exact_text, tuple)
            or len(self.preserved_exact_text) > MAX_SEMANTIC_PROTECTED_TEXT
            or not all(isinstance(item, ExactTextSnapshot) for item in self.preserved_exact_text)
        ):
            raise ConstrainedSemanticPlanningError("preserved_exact_text is outside its bound")
        text_ids = tuple(item.target_id for item in self.preserved_exact_text)
        if len(text_ids) != len(set(text_ids)):
            raise ConstrainedSemanticPlanningError("preserved_exact_text contains duplicate IDs")
        if (
            not isinstance(self.proposals, tuple)
            or len(self.proposals) > MAX_SEMANTIC_PROPOSALS
            or not all(isinstance(item, SemanticPlanningProposal) for item in self.proposals)
        ):
            raise ConstrainedSemanticPlanningError("proposals are outside their finite bound")
        proposal_ids = tuple(item.proposal_id for item in self.proposals)
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ConstrainedSemanticPlanningError("proposals contain duplicate IDs")
        if not isinstance(self.complete, bool) or not self.complete:
            raise ConstrainedSemanticPlanningError("proposal document must be complete")
        if self.schema not in {
            CONSTRAINED_SEMANTIC_PLANNING_SCHEMA,
            TYPED_SEMANTIC_PLANNING_SCHEMA,
        }:
            raise ConstrainedSemanticPlanningError("unsupported proposal document schema")
        if any(proposal.schema != self.schema for proposal in self.proposals):
            raise ConstrainedSemanticPlanningError("mixed semantic proposal schema versions")
        if len(self.to_wire_bytes()) > MAX_SEMANTIC_OUTPUT_BYTES:
            raise ConstrainedSemanticPlanningError("proposal document exceeds output limit")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document_id": self.document_id,
            "policy": cast(SemanticPlanningPolicy, self.policy).value,
            "task_mode": cast(TaskMode, self.task_mode).value,
            "effective_duration": self.effective_duration,
            "asset_ids": list(self.asset_ids),
            "reference_order": list(self.reference_order),
            "timeline_fingerprint": self.timeline_fingerprint,
            "reduction_fingerprint": self.reduction_fingerprint,
            "preserved_exact_text": [item.to_wire() for item in self.preserved_exact_text],
            "proposals": [item.to_wire() for item in self.proposals],
            "complete": self.complete,
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


@dataclass(frozen=True, slots=True)
class SemanticPlanningRequest:
    reduction_plan: HierarchicalReductionPlan
    timeline_plan: FeasibleAVTimelinePlan
    profile: SemanticPlanningProfile
    policy: SemanticPlanningPolicy | str
    protected_exact_text: tuple[ExactTextSnapshot, ...] = ()
    budget: SemanticPlanningBudget = SemanticPlanningBudget()
    media_fingerprints: tuple[str, ...] = ()
    schema: str = CONSTRAINED_SEMANTIC_PLANNING_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.reduction_plan, HierarchicalReductionPlan):
            raise ConstrainedSemanticPlanningError(
                "reduction_plan must be HierarchicalReductionPlan"
            )
        if self.reduction_plan.status not in {ReductionStatus.COMPLETE, ReductionStatus.PARTIAL}:
            raise ConstrainedSemanticPlanningError("reduction_plan is not renderable")
        if not isinstance(self.timeline_plan, FeasibleAVTimelinePlan):
            raise ConstrainedSemanticPlanningError("timeline_plan must be FeasibleAVTimelinePlan")
        if self.reduction_plan.timeline_fingerprint != self.timeline_plan.fingerprint:
            raise ConstrainedSemanticPlanningError("reduction and timeline fingerprints disagree")
        if not isinstance(self.profile, SemanticPlanningProfile):
            raise ConstrainedSemanticPlanningError("profile must be SemanticPlanningProfile")
        object.__setattr__(self, "policy", _enum(self.policy, SemanticPlanningPolicy, "policy"))
        if not isinstance(self.budget, SemanticPlanningBudget):
            raise ConstrainedSemanticPlanningError("budget must be SemanticPlanningBudget")
        if (
            not isinstance(self.protected_exact_text, tuple)
            or len(self.protected_exact_text) > MAX_SEMANTIC_PROTECTED_TEXT
            or not all(isinstance(item, ExactTextSnapshot) for item in self.protected_exact_text)
        ):
            raise ConstrainedSemanticPlanningError("protected_exact_text is outside its bound")
        text_ids = tuple(item.target_id for item in self.protected_exact_text)
        if len(text_ids) != len(set(text_ids)):
            raise ConstrainedSemanticPlanningError("protected_exact_text contains duplicate IDs")
        if not isinstance(self.media_fingerprints, tuple) or len(self.media_fingerprints) > 16:
            raise ConstrainedSemanticPlanningError("media_fingerprints is outside its bound")
        for item in self.media_fingerprints:
            _fingerprint(item, "media fingerprint")
        if self.media_fingerprints and not self.profile.raw_media_capability:
            raise ConstrainedSemanticPlanningError(
                "raw media was requested without an explicit profile capability"
            )
        if self.schema != CONSTRAINED_SEMANTIC_PLANNING_SCHEMA:
            raise ConstrainedSemanticPlanningError("unsupported semantic planning request schema")

    @property
    def allowed_source_ids(self) -> frozenset[str]:
        values: set[str] = {self.reduction_plan.target_id}
        values.update(item.item_id for item in self.reduction_plan.retained_items)
        values.update(
            source_id
            for item in self.reduction_plan.retained_items
            for source_id in item.source_ids
        )
        values.update(self.timeline_plan.reference_order)
        values.update(item.shot_id for item in self.timeline_plan.shots)
        values.update(item.event_id for item in self.timeline_plan.events)
        values.update(item.anchor_id for item in self.timeline_plan.anchors)
        values.update(item.relation_id for item in self.timeline_plan.relations)
        return frozenset(values)

    @property
    def protected_target_ids(self) -> frozenset[str]:
        values = {item.target_id for item in self.protected_exact_text}
        for item in self.reduction_plan.retained_items:
            if item.protected:
                values.add(item.item_id)
                values.update(item.source_ids)
                values.update(item.constraint_ids)
                values.update(item.ambiguity_ids)
                values.update(item.conflict_ids)
        return frozenset(values)

    @property
    def baseline_fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "schema": CONSTRAINED_SEMANTIC_PLANNING_SCHEMA,
                "policy": cast(SemanticPlanningPolicy, self.policy).value,
                "profile": self.profile.to_wire(),
                "timeline_fingerprint": self.timeline_plan.fingerprint,
                "reduction_fingerprint": self.reduction_plan.fingerprint,
                "protected_exact_text": [item.to_wire() for item in self.protected_exact_text],
                "budget": self.budget.to_wire(),
                "media_fingerprints": list(self.media_fingerprints),
            }
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "reduction_fingerprint": self.reduction_plan.fingerprint,
            "timeline_fingerprint": self.timeline_plan.fingerprint,
            "profile": self.profile.to_wire(),
            "policy": cast(SemanticPlanningPolicy, self.policy).value,
            "protected_exact_text": [item.to_wire() for item in self.protected_exact_text],
            "budget": self.budget.to_wire(),
            "media_fingerprints": list(self.media_fingerprints),
        }


@dataclass(frozen=True, slots=True)
class SemanticPlanningDiff:
    before_fingerprint: str
    after_fingerprint: str
    added_proposal_ids: tuple[str, ...]
    changed_fields: tuple[str, ...]
    schema: str = CONSTRAINED_SEMANTIC_PLANNING_SCHEMA

    def __post_init__(self) -> None:
        _fingerprint(self.before_fingerprint, "diff before_fingerprint")
        _fingerprint(self.after_fingerprint, "diff after_fingerprint")
        object.__setattr__(
            self, "added_proposal_ids", _ids(self.added_proposal_ids, "added_proposal_ids")
        )
        object.__setattr__(
            self,
            "changed_fields",
            _ids(self.changed_fields, "changed_fields", MAX_SEMANTIC_DIFF_FIELDS),
        )
        if self.schema != CONSTRAINED_SEMANTIC_PLANNING_SCHEMA:
            raise ConstrainedSemanticPlanningError("unsupported semantic diff schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire(include_fingerprint=False))

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "added_proposal_ids": list(self.added_proposal_ids),
            "changed_fields": list(self.changed_fields),
        }
        if include_fingerprint:
            value["diff_fingerprint"] = self.fingerprint
        return value


@dataclass(frozen=True, slots=True)
class SemanticPlanningReceipt:
    status: SemanticPlanningStatus | str
    profile_id: str
    backend_family: ModelBackendFamily
    model_id: str
    model_digest: str
    model_output_fingerprint: str | None
    input_reduction_fingerprint: str
    input_timeline_fingerprint: str
    policy: SemanticPlanningPolicy | str
    seed: int | None
    raw_media_used: bool
    before_fingerprint: str
    after_fingerprint: str | None
    revision_id: str | None
    diff_fingerprint: str | None
    budget: SemanticPlanningBudget
    diagnostics: tuple[str, ...] = ()
    schema: str = CONSTRAINED_SEMANTIC_PLANNING_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", _enum(self.status, SemanticPlanningStatus, "receipt status")
        )
        _identifier(self.profile_id, "receipt profile_id")
        if not isinstance(self.backend_family, ModelBackendFamily):
            raise ConstrainedSemanticPlanningError("receipt backend_family is invalid")
        _identifier(self.model_id, "receipt model_id")
        _fingerprint(self.model_digest, "receipt model_digest")
        if self.model_output_fingerprint is not None:
            _fingerprint(self.model_output_fingerprint, "model_output_fingerprint")
        _fingerprint(self.input_reduction_fingerprint, "input_reduction_fingerprint")
        _fingerprint(self.input_timeline_fingerprint, "input_timeline_fingerprint")
        object.__setattr__(
            self, "policy", _enum(self.policy, SemanticPlanningPolicy, "receipt policy")
        )
        if self.seed is not None and (
            isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0
        ):
            raise ConstrainedSemanticPlanningError("receipt seed is invalid")
        if not isinstance(self.raw_media_used, bool):
            raise ConstrainedSemanticPlanningError("raw_media_used must be a boolean")
        _fingerprint(self.before_fingerprint, "before_fingerprint")
        if self.after_fingerprint is not None:
            _fingerprint(self.after_fingerprint, "after_fingerprint")
        if self.revision_id is not None:
            _identifier(self.revision_id, "revision_id")
        if self.diff_fingerprint is not None:
            _fingerprint(self.diff_fingerprint, "diff_fingerprint")
        if not isinstance(self.budget, SemanticPlanningBudget):
            raise ConstrainedSemanticPlanningError("receipt budget is invalid")
        object.__setattr__(self, "diagnostics", _ids(self.diagnostics, "diagnostics", 64))
        if self.schema != CONSTRAINED_SEMANTIC_PLANNING_SCHEMA:
            raise ConstrainedSemanticPlanningError("unsupported semantic receipt schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": cast(SemanticPlanningStatus, self.status).value,
            "profile_id": self.profile_id,
            "backend_family": self.backend_family.value,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "model_output_fingerprint": self.model_output_fingerprint,
            "input_reduction_fingerprint": self.input_reduction_fingerprint,
            "input_timeline_fingerprint": self.input_timeline_fingerprint,
            "policy": cast(SemanticPlanningPolicy, self.policy).value,
            "seed": self.seed,
            "raw_media_used": self.raw_media_used,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "revision_id": self.revision_id,
            "diff_fingerprint": self.diff_fingerprint,
            "budget": self.budget.to_wire(),
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class SemanticPlanningResult:
    status: SemanticPlanningStatus | str
    document: SemanticProposalDocument | None
    diff: SemanticPlanningDiff | None
    receipt: SemanticPlanningReceipt
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    schema: str = CONSTRAINED_SEMANTIC_PLANNING_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", _enum(self.status, SemanticPlanningStatus, "result status")
        )
        if self.document is not None and not isinstance(self.document, SemanticProposalDocument):
            raise ConstrainedSemanticPlanningError("result document is invalid")
        if self.diff is not None and not isinstance(self.diff, SemanticPlanningDiff):
            raise ConstrainedSemanticPlanningError("result diff is invalid")
        if not isinstance(self.receipt, SemanticPlanningReceipt):
            raise ConstrainedSemanticPlanningError("result receipt is invalid")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise ConstrainedSemanticPlanningError("result diagnostics are invalid")
        if self.status is SemanticPlanningStatus.COMPLETE and (
            self.document is None or self.diff is None
        ):
            raise ConstrainedSemanticPlanningError("complete result requires document and diff")
        if self.schema != CONSTRAINED_SEMANTIC_PLANNING_SCHEMA:
            raise ConstrainedSemanticPlanningError("unsupported semantic result schema")

    @property
    def is_valid(self) -> bool:
        return self.status is SemanticPlanningStatus.COMPLETE and self.document is not None

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": cast(SemanticPlanningStatus, self.status).value,
            "document": None if self.document is None else self.document.to_wire(),
            "diff": None if self.diff is None else self.diff.to_wire(),
            "receipt": self.receipt.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


def build_semantic_generation_request(request: SemanticPlanningRequest) -> ModelGenerationRequest:
    """Build the deterministic native/Ollama prompt over typed reduced evidence."""

    if not isinstance(request, SemanticPlanningRequest):
        raise ConstrainedSemanticPlanningError("request must be SemanticPlanningRequest")
    retained = [item.to_wire() for item in request.reduction_plan.retained_items]
    prompt_payload = {
        "task": "produce one complete constrained semantic proposal document",
        "policy": cast(SemanticPlanningPolicy, request.policy).value,
        "task_mode": request.timeline_plan.task_mode.value,
        "effective_duration": request.timeline_plan.effective_duration.raw,
        "asset_ids": list(request.timeline_plan.reference_order),
        "reference_order": list(request.timeline_plan.reference_order),
        "timeline_fingerprint": request.timeline_plan.fingerprint,
        "reduction_fingerprint": request.reduction_plan.fingerprint,
        "protected_exact_text": [item.to_wire() for item in request.protected_exact_text],
        "evidence": retained,
        "required_output": (
            "JSON object only; preserve every identity, time, reference, and exact-text field"
        ),
    }
    prompt = json.dumps(prompt_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(prompt) > MAX_SEMANTIC_PROMPT_CHARS:
        raise ConstrainedSemanticPlanningError("semantic planning prompt exceeds its finite bound")
    return ModelGenerationRequest(
        prompt=prompt,
        max_tokens=request.budget.max_output_tokens,
        output_schema=SEMANTIC_PROPOSAL_OUTPUT_SCHEMA,
        do_sample=False,
        temperature=0.0,
        top_k=0,
        top_p=1.0,
        min_p=0.0,
        repetition_penalty=1.0,
        presence_penalty=0.0,
        seed=request.profile.seed,
        thinking=False,
        use_default_template=True,
        media_fingerprints=request.media_fingerprints,
        structured_schema=semantic_proposal_json_schema(),
    )


def semantic_proposal_json_schema() -> dict[str, object]:
    """Return the provider hint; it is never a substitute for local validation."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "document_id",
            "policy",
            "task_mode",
            "effective_duration",
            "asset_ids",
            "reference_order",
            "timeline_fingerprint",
            "reduction_fingerprint",
            "preserved_exact_text",
            "proposals",
            "complete",
        ],
        "properties": {
            "schema": {"const": CONSTRAINED_SEMANTIC_PLANNING_SCHEMA},
            "document_id": {"type": "string"},
            "policy": {"enum": [item.value for item in SemanticPlanningPolicy]},
            "task_mode": {"enum": [item.value for item in TaskMode]},
            "effective_duration": {"type": "string"},
            "asset_ids": {"type": "array", "items": {"type": "string"}},
            "reference_order": {"type": "array", "items": {"type": "string"}},
            "timeline_fingerprint": {"type": "string"},
            "reduction_fingerprint": {"type": "string"},
            "preserved_exact_text": {"type": "array"},
            "proposals": {"type": "array", "maxItems": MAX_SEMANTIC_PROPOSALS},
            "complete": {"const": True},
        },
    }


def _base_receipt(
    request: SemanticPlanningRequest,
    status: SemanticPlanningStatus,
    *,
    model_output_fingerprint: str | None = None,
    after_fingerprint: str | None = None,
    revision_id: str | None = None,
    diff_fingerprint: str | None = None,
    diagnostics: tuple[str, ...] = (),
) -> SemanticPlanningReceipt:
    return SemanticPlanningReceipt(
        status=status,
        profile_id=request.profile.profile_id,
        backend_family=request.profile.backend_family,
        model_id=request.profile.model_id,
        model_digest=request.profile.model_digest,
        model_output_fingerprint=model_output_fingerprint,
        input_reduction_fingerprint=request.reduction_plan.fingerprint,
        input_timeline_fingerprint=request.timeline_plan.fingerprint,
        policy=request.policy,
        seed=request.profile.seed,
        raw_media_used=bool(request.media_fingerprints),
        before_fingerprint=request.baseline_fingerprint,
        after_fingerprint=after_fingerprint,
        revision_id=revision_id,
        diff_fingerprint=diff_fingerprint,
        budget=request.budget,
        diagnostics=diagnostics,
    )


def unavailable_semantic_planning(
    request: SemanticPlanningRequest, reason: str = "selected profile is unavailable"
) -> SemanticPlanningResult:
    """Return explicit profile unavailability without invoking another backend."""

    _text(reason, "unavailable reason", 512)
    diagnostic = _diagnostic("profile_unavailable", reason)
    receipt = _base_receipt(
        request, SemanticPlanningStatus.UNAVAILABLE, diagnostics=("profile_unavailable",)
    )
    return SemanticPlanningResult(
        SemanticPlanningStatus.UNAVAILABLE, None, None, receipt, (diagnostic,)
    )


def _parse_exact_text(value: object, field_name: str) -> ExactTextSnapshot:
    if not isinstance(value, dict):
        raise ConstrainedSemanticPlanningError(f"{field_name} must be an object")
    if set(value) != {"schema", "target_id", "text"}:
        raise ConstrainedSemanticPlanningError(f"{field_name} contains unknown or missing fields")
    return ExactTextSnapshot(str(value["target_id"]), str(value["text"]), str(value["schema"]))


def _parse_proposal(value: object, budget: SemanticPlanningBudget) -> SemanticPlanningProposal:
    if not isinstance(value, dict):
        raise ConstrainedSemanticPlanningError("proposal must be an object")
    required = {
        "schema",
        "proposal_id",
        "target_kind",
        "target_id",
        "claim",
        "evidence_label",
        "source_ids",
        "confidence",
        "rationale",
    }
    intent = None
    if value.get("schema") == TYPED_SEMANTIC_PLANNING_SCHEMA:
        required.remove("claim")
        required.add("intent")
    if set(value) != required:
        raise ConstrainedSemanticPlanningError("proposal contains unknown or missing fields")
    if "intent" in required:
        try:
            intent = decode_typed_semantic_intent(value["intent"])
        except SemanticIntentError as exc:
            raise ConstrainedSemanticPlanningError(exc.code) from None
    claim = _text(
        value["claim"] if intent is None else intent.description,
        "proposal claim",
        budget.max_claim_chars,
    )
    if not isinstance(value["source_ids"], list) or not all(
        type(item) is str for item in value["source_ids"]
    ):
        raise ConstrainedSemanticPlanningError("source_ids must be an array of identifiers")
    label = _enum(value["evidence_label"], SemanticEvidenceLabel, "evidence_label")
    rationale_value = value["rationale"]
    rationale = (
        None if rationale_value is None else _text(rationale_value, "proposal rationale", 1024)
    )
    if label is SemanticEvidenceLabel.CREATIVE and rationale is None:
        raise ConstrainedSemanticPlanningError("creative proposal requires a rationale")
    return SemanticPlanningProposal(
        str(value["proposal_id"]),
        str(value["target_kind"]),
        str(value["target_id"]),
        claim,
        cast(SemanticEvidenceLabel, label),
        tuple(str(item) for item in cast(list[object], value["source_ids"])),
        _decimal(value["confidence"], "confidence"),
        rationale,
        str(value["schema"]),
        intent,
    )


def _parse_document(
    raw: dict[str, object], budget: SemanticPlanningBudget
) -> SemanticProposalDocument:
    required = {
        "schema",
        "document_id",
        "policy",
        "task_mode",
        "effective_duration",
        "asset_ids",
        "reference_order",
        "timeline_fingerprint",
        "reduction_fingerprint",
        "preserved_exact_text",
        "proposals",
        "complete",
    }
    if set(raw) != required:
        raise ConstrainedSemanticPlanningError(
            "proposal document contains unknown or missing fields"
        )
    exact_raw = raw["preserved_exact_text"]
    proposals_raw = raw["proposals"]
    if not isinstance(exact_raw, list) or len(exact_raw) > MAX_SEMANTIC_PROTECTED_TEXT:
        raise ConstrainedSemanticPlanningError("preserved_exact_text is outside its bound")
    if not isinstance(proposals_raw, list) or len(proposals_raw) > budget.max_proposals:
        raise ConstrainedSemanticPlanningError("proposals exceed the selected budget")
    if not isinstance(raw["asset_ids"], list) or not isinstance(raw["reference_order"], list):
        raise ConstrainedSemanticPlanningError("asset_ids/reference_order must be arrays")
    return SemanticProposalDocument(
        str(raw["document_id"]),
        str(raw["policy"]),
        str(raw["task_mode"]),
        str(raw["effective_duration"]),
        tuple(str(item) for item in raw["asset_ids"]),
        tuple(str(item) for item in raw["reference_order"]),
        str(raw["timeline_fingerprint"]),
        str(raw["reduction_fingerprint"]),
        tuple(_parse_exact_text(item, "preserved_exact_text item") for item in exact_raw),
        tuple(_parse_proposal(item, budget) for item in proposals_raw),
        raw["complete"] if isinstance(raw["complete"], bool) else False,
        str(raw["schema"]),
    )


def _validate_document(
    request: SemanticPlanningRequest,
    document: SemanticProposalDocument,
    accepted_exact_text: tuple[ExactTextConstraint, ...],
) -> tuple[ValidationDiagnostic, ...]:
    diagnostics: list[ValidationDiagnostic] = []
    policy = cast(SemanticPlanningPolicy, request.policy)
    if document.policy is not policy:
        diagnostics.append(_diagnostic("policy_mismatch", "proposal policy differs from request"))
    if document.task_mode is not request.timeline_plan.task_mode:
        diagnostics.append(_diagnostic("task_mode_mutation", "proposal changed task mode"))
    if document.effective_duration != request.timeline_plan.effective_duration.raw:
        diagnostics.append(_diagnostic("duration_mutation", "proposal changed effective duration"))
    expected_assets = request.timeline_plan.reference_order
    if document.asset_ids != expected_assets:
        diagnostics.append(
            _diagnostic("asset_identity_mutation", "proposal changed asset identity")
        )
    if document.reference_order != expected_assets:
        diagnostics.append(
            _diagnostic("reference_order_mutation", "proposal changed reference order")
        )
    if document.timeline_fingerprint != request.timeline_plan.fingerprint:
        diagnostics.append(
            _diagnostic("timeline_fingerprint_mismatch", "proposal timeline is stale")
        )
    if document.reduction_fingerprint != request.reduction_plan.fingerprint:
        diagnostics.append(
            _diagnostic("reduction_fingerprint_mismatch", "proposal reduction is stale")
        )
    if tuple(document.preserved_exact_text) != request.protected_exact_text:
        diagnostics.append(
            _diagnostic("exact_text_mutation", "proposal changed exact protected text")
        )
    allowed = request.allowed_source_ids
    protected = request.protected_target_ids
    seen_ids: set[str] = set()
    seen_targets: set[tuple[SemanticTargetKind, str]] = set()
    for item in document.proposals:
        target = (cast(SemanticTargetKind, item.target_kind), item.target_id)
        if document.schema == TYPED_SEMANTIC_PLANNING_SCHEMA and target in seen_targets:
            diagnostics.append(
                _diagnostic("duplicate_proposal_target", "typed target was repeated")
            )
        seen_targets.add(target)
        if item.intent is not None:
            try:
                # SECURITY: standalone parsing has no dialogue authority by default. Only the
                # producer's caller-owned baseline may authorize exact language/speaker bindings.
                validate_semantic_dialogue_bindings(item.intent, accepted_exact_text)
            except SemanticIntentError:
                diagnostics.append(
                    _diagnostic("dialogue_binding_mutation", "unapproved dialogue binding")
                )
        if item.proposal_id in seen_ids:
            diagnostics.append(_diagnostic("duplicate_proposal_id", "proposal IDs must be unique"))
        seen_ids.add(item.proposal_id)
        if any(source_id not in allowed for source_id in item.source_ids):
            diagnostics.append(
                _diagnostic(
                    "unsupported_source", f"proposal {item.proposal_id} has unknown source IDs"
                )
            )
        if item.target_id not in allowed:
            diagnostics.append(
                _diagnostic(
                    "unsupported_target", f"proposal {item.proposal_id} has an unknown target"
                )
            )
        if item.target_id in protected:
            diagnostics.append(
                _diagnostic(
                    "hard_constraint_mutation",
                    f"proposal {item.proposal_id} targets protected evidence",
                )
            )
        lowered = item.claim.casefold()
        if any(marker in lowered for marker in _INJECTION):
            diagnostics.append(
                _diagnostic(
                    "policy_injection", f"proposal {item.proposal_id} contains instruction text"
                )
            )
        if (
            policy is SemanticPlanningPolicy.STRICT
            and item.evidence_label is not SemanticEvidenceLabel.SOURCE
        ):
            diagnostics.append(
                _diagnostic(
                    "strict_requires_source", f"proposal {item.proposal_id} is not source-labelled"
                )
            )
        if (
            policy is SemanticPlanningPolicy.EVIDENCE_BOUNDED
            and item.evidence_label is SemanticEvidenceLabel.CREATIVE
        ):
            diagnostics.append(
                _diagnostic("creative_not_allowed", f"proposal {item.proposal_id} is creative")
            )
        if item.evidence_label is SemanticEvidenceLabel.CREATIVE and item.rationale is None:
            diagnostics.append(
                _diagnostic(
                    "creative_label_missing_rationale",
                    f"proposal {item.proposal_id} lacks a rationale",
                )
            )
    return tuple(diagnostics)


def parse_semantic_proposal(
    request: SemanticPlanningRequest,
    text: str,
    *,
    model_result: ModelGenerationResult | None = None,
    accepted_exact_text: tuple[ExactTextConstraint, ...] = (),
) -> SemanticPlanningResult:
    """Parse and validate one complete model document, returning no partial document on failure."""

    if not isinstance(request, SemanticPlanningRequest):
        raise ConstrainedSemanticPlanningError("request must be SemanticPlanningRequest")
    if type(accepted_exact_text) is not tuple or any(
        type(item) is not ExactTextConstraint for item in accepted_exact_text
    ):
        raise ConstrainedSemanticPlanningError(
            "accepted_exact_text must contain caller-owned constraints"
        )
    output_fingerprint = None if model_result is None else model_result.output_fingerprint
    if model_result is not None:
        if not isinstance(model_result, ModelGenerationResult):
            raise ConstrainedSemanticPlanningError("model_result must be ModelGenerationResult")
        if model_result.schema != MODEL_GENERATION_RESULT_SCHEMA:
            raise ConstrainedSemanticPlanningError("model_result schema is unsupported")
        if (
            model_result.backend_family is not request.profile.backend_family
            or model_result.model_id != request.profile.model_id
            or model_result.model_digest != request.profile.model_digest
        ):
            diagnostic = _diagnostic(
                "model_provenance_mismatch", "model result does not match selected profile"
            )
            receipt = _base_receipt(
                request,
                SemanticPlanningStatus.REJECTED,
                model_output_fingerprint=output_fingerprint,
                diagnostics=("model_provenance_mismatch",),
            )
            return SemanticPlanningResult(
                SemanticPlanningStatus.REJECTED, None, None, receipt, (diagnostic,)
            )
    try:
        document = _parse_document(_parse_json(text), request.budget)
        diagnostics = _validate_document(request, document, accepted_exact_text)
    except ConstrainedSemanticPlanningError as exc:
        diagnostic = _diagnostic("proposal_invalid", str(exc))
        receipt = _base_receipt(
            request,
            SemanticPlanningStatus.REJECTED,
            model_output_fingerprint=output_fingerprint,
            diagnostics=("proposal_invalid",),
        )
        return SemanticPlanningResult(
            SemanticPlanningStatus.REJECTED, None, None, receipt, (diagnostic,)
        )
    if diagnostics:
        status = (
            SemanticPlanningStatus.BLOCKED
            if any(
                item.code
                in {
                    "hard_constraint_mutation",
                    "exact_text_mutation",
                    "asset_identity_mutation",
                    "task_mode_mutation",
                    "duration_mutation",
                    "reference_order_mutation",
                }
                for item in diagnostics
            )
            else SemanticPlanningStatus.REJECTED
        )
        receipt = _base_receipt(
            request,
            status,
            model_output_fingerprint=output_fingerprint,
            diagnostics=tuple(item.code for item in diagnostics),
        )
        return SemanticPlanningResult(status, None, None, receipt, diagnostics)
    after_fingerprint = document.fingerprint
    diff = SemanticPlanningDiff(
        request.baseline_fingerprint,
        after_fingerprint,
        tuple(item.proposal_id for item in document.proposals),
        ("proposal_document", "preserved_exact_text"),
    )
    revision_id = "revision." + after_fingerprint.split(":", 1)[1][:32]
    receipt = _base_receipt(
        request,
        SemanticPlanningStatus.COMPLETE,
        model_output_fingerprint=output_fingerprint,
        after_fingerprint=after_fingerprint,
        revision_id=revision_id,
        diff_fingerprint=diff.fingerprint,
    )
    return SemanticPlanningResult(SemanticPlanningStatus.COMPLETE, document, diff, receipt)


__all__ = [
    "CONSTRAINED_SEMANTIC_PLANNING_SCHEMA",
    "SEMANTIC_PROPOSAL_OUTPUT_SCHEMA",
    "MAX_SEMANTIC_PROPOSALS",
    "SemanticPlanningPolicy",
    "SemanticPlanningStatus",
    "SemanticEvidenceLabel",
    "SemanticTargetKind",
    "SemanticPlanningBudget",
    "SemanticPlanningProfile",
    "ExactTextSnapshot",
    "SemanticPlanningProposal",
    "SemanticProposalDocument",
    "SemanticPlanningRequest",
    "SemanticPlanningDiff",
    "SemanticPlanningReceipt",
    "SemanticPlanningResult",
    "build_semantic_generation_request",
    "semantic_proposal_json_schema",
    "unavailable_semantic_planning",
    "parse_semantic_proposal",
]
