"""Pure Production planning context, storyboard admission, and segment proposal contracts.

The module consumes only immutable typed values. It keeps the original single-clip clock distinct
from the whole-video target and creates reviewable cut-based proposals without owning runtime work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from dataclasses import field as dataclass_field
from decimal import Decimal
from enum import Enum
from typing import Any, cast, overload

from .canonical import canonical_fingerprint
from .contracts import AssetRole, TaskMode
from .errors import PromptRenderingError
from .length import FPS
from .native_mode_matrix import build_default_native_mode_matrix
from .production_duration import (
    MAX_H3_SEGMENT_SECONDS,
    MAX_PRODUCTION_DURATION_SECONDS,
    MAX_PRODUCTION_SEGMENTS,
    ProductionDurationError,
    ProductionDurationIntentV1,
    SegmentationPolicyV1,
    SegmentCutConstraintsV1,
    SegmentDurationAllocationV1,
    SegmentDurationPlanRefusalV1,
    SegmentDurationPlanV1,
    solve_segment_duration_plan,
)
from .production_semantics import (
    ProductionLocalShotV1,
    ProductionSemanticAuthorityV1,
    ProductionSemanticError,
    ProductionSemanticSliceV1,
    SemanticCensusV1,
    assert_complete_semantic_census,
    bind_canonical_semantic_references,
    canonical_text_asset_ids,
    canonical_text_subject_ids,
    clip_derived_semantics_removed,
    merge_semantic_authorities,
    parse_canonical_semantics,
    slice_production_semantics,
    validate_semantic_references,
)
from .rendering import render_production_segment_prompt

PRODUCTION_PLANNING_CONTEXT_SCHEMA = "h3.context.production_planning_context.v1"
PLANNING_ASSET_SCHEMA = "h3.context.production_planning_asset.v1"
STORYBOARD_SHOT_SCHEMA = "h3.context.storyboard_shot.v1"
STORYBOARD_ADMISSION_REQUEST_SCHEMA = "h3.context.storyboard_admission_request.v1"
STORYBOARD_ADMISSION_SCHEMA = "h3.context.storyboard_admission.v1"
STORYBOARD_ADMISSION_REFUSAL_SCHEMA = "h3.context.storyboard_admission_refusal.v1"
SHOT_MAPPING_SCHEMA = "h3.context.segment_shot_mapping.v1"
PROPOSAL_SEGMENT_SCHEMA = "h3.context.segmentation_proposal_segment.v1"
SEGMENTATION_PROPOSAL_SCHEMA = "h3.context.segmentation_proposal.v1"
SEGMENTATION_PROPOSAL_REFUSAL_SCHEMA = "h3.context.segmentation_proposal_refusal.v1"
PROMPT_TEXT_FINGERPRINT_SCHEMA = "h3.context.prompt_text_fingerprint.v1"
PRODUCTION_PLANNING_CONTEXT_V2_SCHEMA = "h3.context.production_planning_context.v2"
STORYBOARD_ADMISSION_V2_SCHEMA = "h3.context.storyboard_admission.v2"
SEGMENTATION_PROPOSAL_V2_SCHEMA = "h3.context.segmentation_proposal.v2"

MAX_STORYBOARD_SHOTS = 64
MAX_TEXT_LENGTH = 8_192
MAX_PROMPT_LENGTH = 65_536
MAX_IDS = 64
MAX_SERVICE_ENTRIES = 128

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STORYBOARD_SECTION = re.compile(
    r"(?:\A|\n\n)(?:integrated_multimodal_description|detailed_description): "
    r"(?P<body>.*?)(?=\n\n[a-z][a-z0-9_]{0,63}: |\Z)",
    re.DOTALL,
)
_FIRST_SHOT_MARKER = re.compile(r"(?<!from )\[Shot 1\](?! At )")
_TIMED_SHOT_MARKER = re.compile(
    r"\[Shot (?P<ordinal>(?:[2-9]|[1-9][0-9]{1,2}))\] At "
    r"(?P<minute>[0-9]{2}):(?P<second>[0-5][0-9])"
    r"\.(?P<millisecond>[0-9]{3}),"
)
_TIMED_SHOT_LIKE = re.compile(r"\[Shot (?:[2-9]|[1-9][0-9]{1,2})\] At ")
_DIALOGUE = re.compile(r"<d>[^\x00]{1,4096}?</d>")


class ProductionStoryboardError(ValueError):
    """One M26-02 wire value or invariant is not closed and canonical."""


class StoryboardSourceKindV1(str, Enum):
    CANONICAL_OPTIMIZED_PROMPT = "canonical_optimized_prompt"
    USER_REVIEWED_TYPED_ROWS = "user_reviewed_typed_rows"


class StoryboardAdmissionRefusalCodeV1(str, Enum):
    STORYBOARD_UNAVAILABLE = "storyboard_unavailable"
    USER_REVIEW_REQUIRED = "user_review_required"
    INVALID_COVERAGE = "invalid_coverage"
    SOURCE_FINGERPRINT_MISMATCH = "source_fingerprint_mismatch"
    STALE_SOURCE_STATE = "stale_source_state"
    REQUEST_CONFLICT = "request_conflict"
    REFERENCE_MEMBERSHIP_INVALID = "reference_membership_invalid"


class ProposalBlockerCodeV1(str, Enum):
    HARD_CONTENT_CROSSES_BOUNDARY = "hard_content_crosses_boundary"
    NATIVE_MAPPING_UNAVAILABLE = "native_mapping_unavailable"
    REQUIRED_ASSET_MISSING = "required_asset_missing"
    MANAGED_EXECUTION_QUALIFICATION_PENDING = "managed_execution_qualification_pending"
    MANAGED_EXECUTION_UNSUPPORTED = "managed_execution_unsupported"
    LOCAL_REFERENCE_UNAVAILABLE = "local_reference_unavailable"


class ManagedExecutionQualificationV1(str, Enum):
    PENDING = "pending"
    QUALIFIED = "qualified"
    UNSUPPORTED = "unsupported"


class SegmentationProposalRefusalCodeV1(str, Enum):
    SOURCE_BINDING_STALE = "source_binding_stale"
    STORYBOARD_COVERAGE_INVALID = "storyboard_coverage_invalid"
    DURATION_PLAN_UNAVAILABLE = "duration_plan_unavailable"
    SEMANTIC_PRESERVATION_UNAVAILABLE = "semantic_preservation_unavailable"


def _closed_dict(value: object, field: str, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise ProductionStoryboardError(field)
    wire = cast(dict[str, object], value)
    if frozenset(wire) != keys:
        raise ProductionStoryboardError(field)
    return wire


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ProductionStoryboardError(field)
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise ProductionStoryboardError(field)
    return value


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProductionStoryboardError(field)
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ProductionStoryboardError(field)
    return value


def _text(value: object, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if type(value) is not str or not value or "\x00" in value or len(value) > maximum:
        raise ProductionStoryboardError(field)
    return value


def _identifiers(value: object, field: str, maximum: int = MAX_IDS) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise ProductionStoryboardError(field)
    identifiers = tuple(_identifier(item, field) for item in value)
    if len(identifiers) != len(set(identifiers)):
        raise ProductionStoryboardError(field)
    return identifiers


def _text_tuple(value: object, field: str, maximum: int = MAX_IDS) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise ProductionStoryboardError(field)
    return tuple(_text(item, field) for item in value)


def _wire_tuple(value: object, field: str, maximum: int = MAX_IDS) -> tuple[object, ...]:
    if type(value) is not list or len(value) > maximum:
        raise ProductionStoryboardError(field)
    return tuple(cast(list[object], value))


def _enum(value: object, expected: type[Enum], field: str) -> Enum:
    if type(value) is not str:
        raise ProductionStoryboardError(field)
    try:
        return expected(value)
    except ValueError as exc:
        raise ProductionStoryboardError(field) from exc


def fingerprint_prompt_text(text: object) -> str:
    """Return a bounded identity for exact accepted text without retaining it in context state."""

    return canonical_fingerprint(
        {
            "schema": PROMPT_TEXT_FINGERPRINT_SCHEMA,
            "text": _text(text, "prompt_text", MAX_PROMPT_LENGTH),
        }
    )


@dataclass(frozen=True, slots=True)
class PlanningAssetV1:
    asset_id: str
    role: AssetRole

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "planning_asset_id")
        if type(self.role) is not AssetRole:
            raise ProductionStoryboardError("planning_asset_role")

    def to_wire(self) -> dict[str, object]:
        return {"schema": PLANNING_ASSET_SCHEMA, "asset_id": self.asset_id, "role": self.role.value}

    @classmethod
    def from_wire(cls, value: object) -> PlanningAssetV1:
        wire = _closed_dict(value, "planning_asset_wire", frozenset({"schema", "asset_id", "role"}))
        if wire["schema"] != PLANNING_ASSET_SCHEMA:
            raise ProductionStoryboardError("planning_asset_schema")
        return cls(
            asset_id=cast(str, wire["asset_id"]),
            role=cast(AssetRole, _enum(wire["role"], AssetRole, "planning_asset_role")),
        )


@dataclass(frozen=True, slots=True)
class ProductionPlanningContextV1:
    """Planning-only global context with non-interchangeable source and target clocks."""

    planning_context_id: str
    revision: int
    optimized_candidate_id: str
    optimized_candidate_text_fingerprint: str
    source_request_fingerprint: str
    source_intent_graph_fingerprint: str
    source_profile_fingerprint: str
    reference_registry_fingerprint: str
    source_context_duration_seconds: int
    source_context_duration_provenance: str
    production_duration_intent: ProductionDurationIntentV1
    global_task_mode: TaskMode
    assets: tuple[PlanningAssetV1, ...] = ()
    subject_ids: tuple[str, ...] = ()
    reference_ids: tuple[str, ...] = ()
    hard_constraints: tuple[str, ...] = ()
    managed_execution_qualification: ManagedExecutionQualificationV1 = (
        ManagedExecutionQualificationV1.PENDING
    )

    def __post_init__(self) -> None:
        _identifier(self.planning_context_id, "planning_context_id")
        _integer(self.revision, "planning_context_revision", 1, 1_000_000)
        _identifier(self.optimized_candidate_id, "optimized_candidate_id")
        for value, field in (
            (self.optimized_candidate_text_fingerprint, "optimized_candidate_text_fingerprint"),
            (self.source_request_fingerprint, "source_request_fingerprint"),
            (self.source_intent_graph_fingerprint, "source_intent_graph_fingerprint"),
            (self.source_profile_fingerprint, "source_profile_fingerprint"),
            (self.reference_registry_fingerprint, "reference_registry_fingerprint"),
        ):
            _fingerprint(value, field)
        _integer(self.source_context_duration_seconds, "source_context_duration_seconds", 4, 15)
        _text(self.source_context_duration_provenance, "source_context_duration_provenance", 256)
        if type(self.production_duration_intent) is not ProductionDurationIntentV1:
            raise ProductionStoryboardError("production_duration_intent")
        if type(self.global_task_mode) is not TaskMode:
            raise ProductionStoryboardError("global_task_mode")
        if (
            type(self.assets) is not tuple
            or len(self.assets) > 12
            or any(type(item) is not PlanningAssetV1 for item in self.assets)
        ):
            raise ProductionStoryboardError("planning_assets")
        asset_ids = tuple(item.asset_id for item in self.assets)
        if len(asset_ids) != len(set(asset_ids)):
            raise ProductionStoryboardError("planning_assets")
        _identifiers(self.subject_ids, "planning_subject_ids")
        _identifiers(self.reference_ids, "planning_reference_ids")
        if not set(self.reference_ids).issubset(asset_ids):
            raise ProductionStoryboardError("planning_reference_ids")
        _text_tuple(self.hard_constraints, "planning_hard_constraints")
        if type(self.managed_execution_qualification) is not ManagedExecutionQualificationV1:
            raise ProductionStoryboardError("managed_execution_qualification")

    @property
    def production_target_duration_seconds(self) -> int:
        return self.production_duration_intent.target_seconds

    @property
    def duration_intent_fingerprint(self) -> str:
        return canonical_fingerprint(self.production_duration_intent.to_wire())

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire(include_fingerprint=False))

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": PRODUCTION_PLANNING_CONTEXT_SCHEMA,
            "planning_context_id": self.planning_context_id,
            "revision": self.revision,
            "optimized_candidate": {
                "candidate_id": self.optimized_candidate_id,
                "text_fingerprint": self.optimized_candidate_text_fingerprint,
            },
            "source_context": {
                "request_fingerprint": self.source_request_fingerprint,
                "intent_graph_fingerprint": self.source_intent_graph_fingerprint,
                "profile_fingerprint": self.source_profile_fingerprint,
                "reference_registry_fingerprint": self.reference_registry_fingerprint,
                "duration_seconds": self.source_context_duration_seconds,
                "duration_provenance": self.source_context_duration_provenance,
            },
            "production_duration_intent": self.production_duration_intent.to_wire(),
            "global_task_mode": self.global_task_mode.value,
            "assets": [item.to_wire() for item in self.assets],
            "subject_ids": list(self.subject_ids),
            "reference_ids": list(self.reference_ids),
            "hard_constraints": list(self.hard_constraints),
            "managed_execution_qualification": self.managed_execution_qualification.value,
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_wire(cls, value: object) -> ProductionPlanningContextV1:
        wire = _closed_dict(
            value,
            "planning_context_wire",
            frozenset(
                {
                    "schema",
                    "planning_context_id",
                    "revision",
                    "optimized_candidate",
                    "source_context",
                    "production_duration_intent",
                    "global_task_mode",
                    "assets",
                    "subject_ids",
                    "reference_ids",
                    "hard_constraints",
                    "managed_execution_qualification",
                    "fingerprint",
                }
            ),
        )
        if wire["schema"] != PRODUCTION_PLANNING_CONTEXT_SCHEMA:
            raise ProductionStoryboardError("planning_context_schema")
        candidate = _closed_dict(
            wire["optimized_candidate"],
            "planning_context_candidate_wire",
            frozenset({"candidate_id", "text_fingerprint"}),
        )
        source = _closed_dict(
            wire["source_context"],
            "planning_context_source_wire",
            frozenset(
                {
                    "request_fingerprint",
                    "intent_graph_fingerprint",
                    "profile_fingerprint",
                    "reference_registry_fingerprint",
                    "duration_seconds",
                    "duration_provenance",
                }
            ),
        )
        assets = _wire_tuple(wire["assets"], "planning_assets_wire", 12)
        subjects = _wire_tuple(wire["subject_ids"], "planning_subject_ids_wire")
        references = _wire_tuple(wire["reference_ids"], "planning_reference_ids_wire")
        constraints = _wire_tuple(wire["hard_constraints"], "planning_hard_constraints_wire")
        result = cls(
            planning_context_id=cast(str, wire["planning_context_id"]),
            revision=cast(int, wire["revision"]),
            optimized_candidate_id=cast(str, candidate["candidate_id"]),
            optimized_candidate_text_fingerprint=cast(str, candidate["text_fingerprint"]),
            source_request_fingerprint=cast(str, source["request_fingerprint"]),
            source_intent_graph_fingerprint=cast(str, source["intent_graph_fingerprint"]),
            source_profile_fingerprint=cast(str, source["profile_fingerprint"]),
            reference_registry_fingerprint=cast(str, source["reference_registry_fingerprint"]),
            source_context_duration_seconds=cast(int, source["duration_seconds"]),
            source_context_duration_provenance=cast(str, source["duration_provenance"]),
            production_duration_intent=ProductionDurationIntentV1.from_wire(
                wire["production_duration_intent"]
            ),
            global_task_mode=cast(
                TaskMode, _enum(wire["global_task_mode"], TaskMode, "global_task_mode")
            ),
            assets=tuple(PlanningAssetV1.from_wire(item) for item in assets),
            subject_ids=tuple(cast(tuple[str, ...], subjects)),
            reference_ids=tuple(cast(tuple[str, ...], references)),
            hard_constraints=tuple(cast(tuple[str, ...], constraints)),
            managed_execution_qualification=cast(
                ManagedExecutionQualificationV1,
                _enum(
                    wire["managed_execution_qualification"],
                    ManagedExecutionQualificationV1,
                    "managed_execution_qualification",
                ),
            ),
        )
        if wire["fingerprint"] != result.fingerprint:
            raise ProductionStoryboardError("planning_context_fingerprint")
        return result


@dataclass(frozen=True, slots=True)
class StoryboardShotV1:
    shot_id: str
    ordinal: int
    start_milliseconds: int
    end_milliseconds: int
    text: str
    subject_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()
    exact_dialogue: tuple[str, ...] = ()
    visible_text: tuple[str, ...] = ()
    hard_boundary: bool = False
    source_span: tuple[int, int] = (0, 0)

    def __post_init__(self) -> None:
        _identifier(self.shot_id, "storyboard_shot_id")
        _integer(self.ordinal, "storyboard_shot_ordinal", 1, MAX_STORYBOARD_SHOTS)
        start = _integer(
            self.start_milliseconds,
            "storyboard_shot_start_milliseconds",
            0,
            MAX_PRODUCTION_DURATION_SECONDS * 1000 - 1,
        )
        end = _integer(
            self.end_milliseconds,
            "storyboard_shot_end_milliseconds",
            1,
            MAX_PRODUCTION_DURATION_SECONDS * 1000,
        )
        if end <= start:
            raise ProductionStoryboardError("storyboard_shot_span")
        _text(self.text, "storyboard_shot_text")
        _identifiers(self.subject_ids, "storyboard_shot_subject_ids")
        _identifiers(self.asset_ids, "storyboard_shot_asset_ids")
        _text_tuple(self.exact_dialogue, "storyboard_shot_exact_dialogue")
        _text_tuple(self.visible_text, "storyboard_shot_visible_text")
        _boolean(self.hard_boundary, "storyboard_shot_hard_boundary")
        if (
            type(self.source_span) is not tuple
            or len(self.source_span) != 2
            or any(type(item) is not int or item < 0 for item in self.source_span)
            or self.source_span[1] < self.source_span[0]
        ):
            raise ProductionStoryboardError("storyboard_shot_source_span")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": STORYBOARD_SHOT_SCHEMA,
            "shot_id": self.shot_id,
            "ordinal": self.ordinal,
            "start_milliseconds": self.start_milliseconds,
            "end_milliseconds": self.end_milliseconds,
            "text": self.text,
            "subject_ids": list(self.subject_ids),
            "asset_ids": list(self.asset_ids),
            "exact_dialogue": list(self.exact_dialogue),
            "visible_text": list(self.visible_text),
            "hard_boundary": self.hard_boundary,
            "source_span": list(self.source_span),
        }

    @classmethod
    def from_wire(cls, value: object) -> StoryboardShotV1:
        keys = frozenset(
            {
                "schema",
                "shot_id",
                "ordinal",
                "start_milliseconds",
                "end_milliseconds",
                "text",
                "subject_ids",
                "asset_ids",
                "exact_dialogue",
                "visible_text",
                "hard_boundary",
                "source_span",
            }
        )
        wire = _closed_dict(value, "storyboard_shot_wire", keys)
        if wire["schema"] != STORYBOARD_SHOT_SCHEMA:
            raise ProductionStoryboardError("storyboard_shot_schema")
        subject_ids = _wire_tuple(wire["subject_ids"], "storyboard_shot_subject_ids_wire")
        asset_ids = _wire_tuple(wire["asset_ids"], "storyboard_shot_asset_ids_wire")
        dialogue = _wire_tuple(wire["exact_dialogue"], "storyboard_shot_dialogue_wire")
        visible = _wire_tuple(wire["visible_text"], "storyboard_shot_visible_wire")
        span = _wire_tuple(wire["source_span"], "storyboard_shot_source_span_wire", 2)
        if len(span) != 2:
            raise ProductionStoryboardError("storyboard_shot_source_span_wire")
        return cls(
            shot_id=cast(str, wire["shot_id"]),
            ordinal=cast(int, wire["ordinal"]),
            start_milliseconds=cast(int, wire["start_milliseconds"]),
            end_milliseconds=cast(int, wire["end_milliseconds"]),
            text=cast(str, wire["text"]),
            subject_ids=tuple(cast(tuple[str, ...], subject_ids)),
            asset_ids=tuple(cast(tuple[str, ...], asset_ids)),
            exact_dialogue=tuple(cast(tuple[str, ...], dialogue)),
            visible_text=tuple(cast(tuple[str, ...], visible)),
            hard_boundary=cast(bool, wire["hard_boundary"]),
            source_span=(cast(int, span[0]), cast(int, span[1])),
        )


def _validate_coverage(shots: tuple[StoryboardShotV1, ...], target_milliseconds: int) -> None:
    if not shots or len(shots) > MAX_STORYBOARD_SHOTS:
        raise ProductionStoryboardError("storyboard_coverage")
    cursor = 0
    for ordinal, shot in enumerate(shots, start=1):
        if type(shot) is not StoryboardShotV1:
            raise ProductionStoryboardError("storyboard_shot_type")
        if shot.ordinal != ordinal or shot.start_milliseconds != cursor:
            raise ProductionStoryboardError("storyboard_coverage")
        cursor = shot.end_milliseconds
    if cursor != target_milliseconds:
        raise ProductionStoryboardError("storyboard_coverage")


def _validate_shot_references(
    context: ProductionPlanningContextV1, shots: tuple[StoryboardShotV1, ...]
) -> None:
    # CRITICAL: a syntactically valid ID is not registry membership. Checking only the wire
    # shape admits foreign subjects/assets and lets later local rendering silently erase them.
    subjects = frozenset(context.subject_ids)
    assets = frozenset(item.asset_id for item in context.assets)
    if any(
        not subjects.issuperset(shot.subject_ids) or not assets.issuperset(shot.asset_ids)
        for shot in shots
    ):
        raise ProductionStoryboardError("storyboard_reference_membership")


def _parse_canonical_storyboard(
    text: str, target_milliseconds: int
) -> tuple[StoryboardShotV1, ...]:
    sections = tuple(_STORYBOARD_SECTION.finditer(text))
    if len(sections) > 1:
        raise ProductionStoryboardError("storyboard_unavailable")
    body = sections[0].group("body") if sections else text
    body_offset = sections[0].start("body") if sections else 0
    first = tuple(_FIRST_SHOT_MARKER.finditer(body))
    timed = tuple(_TIMED_SHOT_MARKER.finditer(body))
    if (
        len(first) != 1
        or bool(body[: first[0].start()].strip())
        or first[0].start() > (timed[0].start() if timed else len(body))
        or len(timed) != len(tuple(_TIMED_SHOT_LIKE.finditer(body)))
    ):
        raise ProductionStoryboardError("storyboard_unavailable")
    matches = (first[0], *timed)
    if len(matches) > MAX_STORYBOARD_SHOTS:
        raise ProductionStoryboardError("storyboard_unavailable")
    rows: list[StoryboardShotV1] = []
    starts: list[int] = []
    for index, match in enumerate(matches):
        ordinal = 1 if index == 0 else int(cast(str, match.group("ordinal")))
        if ordinal != index + 1:
            raise ProductionStoryboardError("storyboard_unavailable")
        if index == 0:
            start = 0
        else:
            start = (
                int(cast(str, match.group("minute"))) * 60_000
                + int(cast(str, match.group("second"))) * 1000
                + int(cast(str, match.group("millisecond")))
            )
        if starts and start <= starts[-1]:
            raise ProductionStoryboardError("storyboard_unavailable")
        if start >= target_milliseconds:
            raise ProductionStoryboardError("storyboard_unavailable")
        starts.append(start)
    for index, match in enumerate(matches):
        content_end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        content = body[match.end() : content_end].strip()
        if not content:
            raise ProductionStoryboardError("storyboard_unavailable")
        end = starts[index + 1] if index + 1 < len(starts) else target_milliseconds
        dialogue = tuple(item.group(0) for item in _DIALOGUE.finditer(content))
        rows.append(
            StoryboardShotV1(
                shot_id=f"shot_{index + 1}",
                ordinal=index + 1,
                start_milliseconds=starts[index],
                end_milliseconds=end,
                text=content,
                exact_dialogue=dialogue,
                hard_boundary=bool(dialogue),
                source_span=(body_offset + match.start(), body_offset + content_end),
            )
        )
    result = tuple(rows)
    _validate_coverage(result, target_milliseconds)
    return result


def _assert_canonical_cut_evidence(
    context: ProductionPlanningContextV2, shots: tuple[StoryboardShotV1, ...]
) -> None:
    """Refuse a canonical storyboard that has no honest cut evidence for this target."""

    # GUARD: the canonical candidate is the source Context's own single-clip prompt, so its cut
    # times live on the 4-15 second source clock. Reading them on a longer production target
    # extends the last shot to the end, and a one-shot candidate has no cut at all, which turns
    # `auto_storyboard` into unannounced numeric partitioning. Both are `storyboard_unavailable`:
    # long content comes from reviewed rows. Do not relax this to "any parseable grammar"; a
    # fixed policy over one shot stays admitted because repeating the template is its explicit
    # meaning.
    target = context.production_target_duration_seconds
    if len(shots) > 1:
        if target != context.source_context_duration_seconds:
            raise ProductionStoryboardError("storyboard_unavailable")
        return
    if (
        context.production_duration_intent.policy is SegmentationPolicyV1.AUTO_STORYBOARD
        and target > MAX_H3_SEGMENT_SECONDS
    ):
        raise ProductionStoryboardError("storyboard_unavailable")


@dataclass(frozen=True, slots=True)
class ProductionStoryboardAdmissionRequestV1:
    request_id: str
    planning_context_id: str
    planning_context_revision: int
    planning_context_fingerprint: str
    optimized_candidate_id: str
    optimized_candidate_text_fingerprint: str
    production_duration_intent_fingerprint: str
    source_kind: StoryboardSourceKindV1
    candidate_text: str | None = None
    typed_rows: tuple[StoryboardShotV1, ...] = ()
    user_reviewed: bool = False

    def __post_init__(self) -> None:
        _identifier(self.request_id, "storyboard_request_id")
        _identifier(self.planning_context_id, "storyboard_request_context_id")
        _integer(
            self.planning_context_revision, "storyboard_request_context_revision", 1, 1_000_000
        )
        _fingerprint(self.planning_context_fingerprint, "storyboard_request_context_fingerprint")
        _identifier(self.optimized_candidate_id, "storyboard_request_candidate_id")
        _fingerprint(
            self.optimized_candidate_text_fingerprint,
            "storyboard_request_candidate_text_fingerprint",
        )
        _fingerprint(
            self.production_duration_intent_fingerprint,
            "storyboard_request_duration_intent_fingerprint",
        )
        if type(self.source_kind) is not StoryboardSourceKindV1:
            raise ProductionStoryboardError("storyboard_request_source_kind")
        _boolean(self.user_reviewed, "storyboard_request_user_reviewed")
        if (
            type(self.typed_rows) is not tuple
            or len(self.typed_rows) > MAX_STORYBOARD_SHOTS
            or any(type(item) is not StoryboardShotV1 for item in self.typed_rows)
        ):
            raise ProductionStoryboardError("storyboard_request_typed_rows")
        if self.source_kind is StoryboardSourceKindV1.CANONICAL_OPTIMIZED_PROMPT:
            if self.candidate_text is None or self.typed_rows or self.user_reviewed:
                raise ProductionStoryboardError("storyboard_request_source_payload")
            _text(self.candidate_text, "storyboard_request_candidate_text", MAX_PROMPT_LENGTH)
        elif self.candidate_text is not None or not self.typed_rows:
            raise ProductionStoryboardError("storyboard_request_source_payload")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": STORYBOARD_ADMISSION_REQUEST_SCHEMA,
            "request_id": self.request_id,
            "planning_context_id": self.planning_context_id,
            "planning_context_revision": self.planning_context_revision,
            "planning_context_fingerprint": self.planning_context_fingerprint,
            "optimized_candidate_id": self.optimized_candidate_id,
            "optimized_candidate_text_fingerprint": self.optimized_candidate_text_fingerprint,
            "production_duration_intent_fingerprint": self.production_duration_intent_fingerprint,
            "source_kind": self.source_kind.value,
            "candidate_text": self.candidate_text,
            "typed_rows": [item.to_wire() for item in self.typed_rows],
            "user_reviewed": self.user_reviewed,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProductionStoryboardAdmissionRequestV1:
        wire = _closed_dict(
            value,
            "storyboard_request_wire",
            frozenset(
                {
                    "schema",
                    "request_id",
                    "planning_context_id",
                    "planning_context_revision",
                    "planning_context_fingerprint",
                    "optimized_candidate_id",
                    "optimized_candidate_text_fingerprint",
                    "production_duration_intent_fingerprint",
                    "source_kind",
                    "candidate_text",
                    "typed_rows",
                    "user_reviewed",
                }
            ),
        )
        if wire["schema"] != STORYBOARD_ADMISSION_REQUEST_SCHEMA:
            raise ProductionStoryboardError("storyboard_request_schema")
        rows = _wire_tuple(wire["typed_rows"], "storyboard_request_rows_wire", MAX_STORYBOARD_SHOTS)
        candidate_text = wire["candidate_text"]
        if candidate_text is not None and type(candidate_text) is not str:
            raise ProductionStoryboardError("storyboard_request_candidate_text_wire")
        return cls(
            request_id=cast(str, wire["request_id"]),
            planning_context_id=cast(str, wire["planning_context_id"]),
            planning_context_revision=cast(int, wire["planning_context_revision"]),
            planning_context_fingerprint=cast(str, wire["planning_context_fingerprint"]),
            optimized_candidate_id=cast(str, wire["optimized_candidate_id"]),
            optimized_candidate_text_fingerprint=cast(
                str, wire["optimized_candidate_text_fingerprint"]
            ),
            production_duration_intent_fingerprint=cast(
                str, wire["production_duration_intent_fingerprint"]
            ),
            source_kind=cast(
                StoryboardSourceKindV1,
                _enum(
                    wire["source_kind"],
                    StoryboardSourceKindV1,
                    "storyboard_request_source_kind",
                ),
            ),
            candidate_text=candidate_text,
            typed_rows=tuple(StoryboardShotV1.from_wire(item) for item in rows),
            user_reviewed=cast(bool, wire["user_reviewed"]),
        )


@dataclass(frozen=True, slots=True)
class ProductionStoryboardAdmissionV1:
    admission_id: str
    request_id: str
    request_fingerprint: str
    planning_context_id: str
    planning_context_revision: int
    planning_context_fingerprint: str
    optimized_candidate_id: str
    optimized_candidate_text_fingerprint: str
    production_duration_intent_fingerprint: str
    source_kind: StoryboardSourceKindV1
    shots: tuple[StoryboardShotV1, ...]
    cut_milliseconds: tuple[int, ...]

    def __post_init__(self) -> None:
        _identifier(self.admission_id, "storyboard_admission_id")
        _identifier(self.request_id, "storyboard_admission_request_id")
        _fingerprint(self.request_fingerprint, "storyboard_admission_request_fingerprint")
        _identifier(self.planning_context_id, "storyboard_admission_context_id")
        _integer(
            self.planning_context_revision, "storyboard_admission_context_revision", 1, 1_000_000
        )
        _fingerprint(self.planning_context_fingerprint, "storyboard_admission_context_fingerprint")
        _identifier(self.optimized_candidate_id, "storyboard_admission_candidate_id")
        _fingerprint(
            self.optimized_candidate_text_fingerprint,
            "storyboard_admission_candidate_text_fingerprint",
        )
        _fingerprint(
            self.production_duration_intent_fingerprint,
            "storyboard_admission_duration_intent_fingerprint",
        )
        if type(self.source_kind) is not StoryboardSourceKindV1:
            raise ProductionStoryboardError("storyboard_admission_source_kind")
        if type(self.shots) is not tuple or any(
            type(item) is not StoryboardShotV1 for item in self.shots
        ):
            raise ProductionStoryboardError("storyboard_admission_shots")
        expected = tuple(item.end_milliseconds for item in self.shots[:-1])
        if self.cut_milliseconds != expected:
            raise ProductionStoryboardError("storyboard_admission_cuts")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire(include_fingerprint=False))

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": STORYBOARD_ADMISSION_SCHEMA,
            "admission_id": self.admission_id,
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "planning_context_id": self.planning_context_id,
            "planning_context_revision": self.planning_context_revision,
            "planning_context_fingerprint": self.planning_context_fingerprint,
            "optimized_candidate_id": self.optimized_candidate_id,
            "optimized_candidate_text_fingerprint": self.optimized_candidate_text_fingerprint,
            "production_duration_intent_fingerprint": self.production_duration_intent_fingerprint,
            "source_kind": self.source_kind.value,
            "shots": [item.to_wire() for item in self.shots],
            "cut_milliseconds": list(self.cut_milliseconds),
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_wire(cls, value: object) -> ProductionStoryboardAdmissionV1:
        keys = frozenset(
            {
                "schema",
                "admission_id",
                "request_id",
                "request_fingerprint",
                "planning_context_id",
                "planning_context_revision",
                "planning_context_fingerprint",
                "optimized_candidate_id",
                "optimized_candidate_text_fingerprint",
                "production_duration_intent_fingerprint",
                "source_kind",
                "shots",
                "cut_milliseconds",
                "fingerprint",
            }
        )
        wire = _closed_dict(value, "storyboard_admission_wire", keys)
        if wire["schema"] != STORYBOARD_ADMISSION_SCHEMA:
            raise ProductionStoryboardError("storyboard_admission_schema")
        shots = _wire_tuple(wire["shots"], "storyboard_admission_shots_wire", MAX_STORYBOARD_SHOTS)
        cuts = _wire_tuple(wire["cut_milliseconds"], "storyboard_admission_cuts_wire")
        result = cls(
            admission_id=cast(str, wire["admission_id"]),
            request_id=cast(str, wire["request_id"]),
            request_fingerprint=cast(str, wire["request_fingerprint"]),
            planning_context_id=cast(str, wire["planning_context_id"]),
            planning_context_revision=cast(int, wire["planning_context_revision"]),
            planning_context_fingerprint=cast(str, wire["planning_context_fingerprint"]),
            optimized_candidate_id=cast(str, wire["optimized_candidate_id"]),
            optimized_candidate_text_fingerprint=cast(
                str, wire["optimized_candidate_text_fingerprint"]
            ),
            production_duration_intent_fingerprint=cast(
                str, wire["production_duration_intent_fingerprint"]
            ),
            source_kind=cast(
                StoryboardSourceKindV1,
                _enum(
                    wire["source_kind"], StoryboardSourceKindV1, "storyboard_admission_source_kind"
                ),
            ),
            shots=tuple(StoryboardShotV1.from_wire(item) for item in shots),
            cut_milliseconds=tuple(cast(tuple[int, ...], cuts)),
        )
        if wire["fingerprint"] != result.fingerprint:
            raise ProductionStoryboardError("storyboard_admission_fingerprint")
        return result


@dataclass(frozen=True, slots=True)
class StoryboardAdmissionRefusalV1:
    request_id: str
    code: StoryboardAdmissionRefusalCodeV1

    def __post_init__(self) -> None:
        _identifier(self.request_id, "storyboard_refusal_request_id")
        if type(self.code) is not StoryboardAdmissionRefusalCodeV1:
            raise ProductionStoryboardError("storyboard_refusal_code")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": STORYBOARD_ADMISSION_REFUSAL_SCHEMA,
            "request_id": self.request_id,
            "code": self.code.value,
        }

    @classmethod
    def from_wire(cls, value: object) -> StoryboardAdmissionRefusalV1:
        wire = _closed_dict(
            value,
            "storyboard_refusal_wire",
            frozenset({"schema", "request_id", "code"}),
        )
        if wire["schema"] != STORYBOARD_ADMISSION_REFUSAL_SCHEMA:
            raise ProductionStoryboardError("storyboard_refusal_schema")
        return cls(
            request_id=cast(str, wire["request_id"]),
            code=cast(
                StoryboardAdmissionRefusalCodeV1,
                _enum(
                    wire["code"],
                    StoryboardAdmissionRefusalCodeV1,
                    "storyboard_refusal_code",
                ),
            ),
        )


class ProductionStoryboardAdmissionService:
    """Bounded process-local replay owner for the closed storyboard admission action."""

    def __init__(self, *, max_entries: int = MAX_SERVICE_ENTRIES) -> None:
        self._max_entries = _integer(
            max_entries, "storyboard_service_max_entries", 1, MAX_SERVICE_ENTRIES
        )
        self._entries: dict[str, tuple[str, ProductionStoryboardAdmissionV1]] = {}

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @overload
    def admit(
        self, context: ProductionPlanningContextV2, request: ProductionStoryboardAdmissionRequestV1
    ) -> ProductionStoryboardAdmissionV2 | StoryboardAdmissionRefusalV1: ...

    @overload
    def admit(
        self, context: ProductionPlanningContextV1, request: ProductionStoryboardAdmissionRequestV1
    ) -> ProductionStoryboardAdmissionV1 | StoryboardAdmissionRefusalV1: ...

    def admit(
        self,
        context: ProductionPlanningContextV1,
        request: ProductionStoryboardAdmissionRequestV1,
    ) -> ProductionStoryboardAdmissionV1 | StoryboardAdmissionRefusalV1:
        if type(context) not in (ProductionPlanningContextV1, ProductionPlanningContextV2):
            raise ProductionStoryboardError("storyboard_service_context")
        if type(request) is not ProductionStoryboardAdmissionRequestV1:
            raise ProductionStoryboardError("storyboard_service_request")
        request_fingerprint = request.fingerprint
        retained = self._entries.get(request.request_id)
        if retained is not None:
            if retained[0] == request_fingerprint:
                return retained[1]
            return StoryboardAdmissionRefusalV1(
                request.request_id, StoryboardAdmissionRefusalCodeV1.REQUEST_CONFLICT
            )
        if (
            request.planning_context_id != context.planning_context_id
            or request.planning_context_revision != context.revision
            or request.planning_context_fingerprint != context.fingerprint
            or request.optimized_candidate_id != context.optimized_candidate_id
            or request.production_duration_intent_fingerprint != context.duration_intent_fingerprint
        ):
            return StoryboardAdmissionRefusalV1(
                request.request_id, StoryboardAdmissionRefusalCodeV1.STALE_SOURCE_STATE
            )
        if (
            request.optimized_candidate_text_fingerprint
            != context.optimized_candidate_text_fingerprint
        ):
            return StoryboardAdmissionRefusalV1(
                request.request_id,
                StoryboardAdmissionRefusalCodeV1.SOURCE_FINGERPRINT_MISMATCH,
            )
        semantic_authority = (
            context.semantic_authority
            if isinstance(context, ProductionPlanningContextV2)
            else ProductionSemanticAuthorityV1()
        )
        if request.source_kind is StoryboardSourceKindV1.CANONICAL_OPTIMIZED_PROMPT:
            candidate_text = cast(str, request.candidate_text)
            if (
                fingerprint_prompt_text(candidate_text)
                != context.optimized_candidate_text_fingerprint
            ):
                return StoryboardAdmissionRefusalV1(
                    request.request_id,
                    StoryboardAdmissionRefusalCodeV1.SOURCE_FINGERPRINT_MISMATCH,
                )
            try:
                shots = _parse_canonical_storyboard(
                    candidate_text, context.production_target_duration_seconds * 1000
                )
                if isinstance(context, ProductionPlanningContextV2):
                    _assert_canonical_cut_evidence(context, shots)
                    semantic_authority = merge_semantic_authorities(
                        semantic_authority,
                        clip_derived_semantics_removed(
                            parse_canonical_semantics(candidate_text),
                            typed_definitions=bool(
                                semantic_authority.subject_definitions
                                or semantic_authority.reference_definitions
                            ),
                        ),
                    )
            except ValueError:
                return StoryboardAdmissionRefusalV1(
                    request.request_id, StoryboardAdmissionRefusalCodeV1.STORYBOARD_UNAVAILABLE
                )
        else:
            if not request.user_reviewed:
                return StoryboardAdmissionRefusalV1(
                    request.request_id, StoryboardAdmissionRefusalCodeV1.USER_REVIEW_REQUIRED
                )
            shots = request.typed_rows
            try:
                _validate_coverage(shots, context.production_target_duration_seconds * 1000)
            except ProductionStoryboardError:
                return StoryboardAdmissionRefusalV1(
                    request.request_id, StoryboardAdmissionRefusalCodeV1.INVALID_COVERAGE
                )
        try:
            if isinstance(context, ProductionPlanningContextV2):
                semantic_authority = bind_canonical_semantic_references(semantic_authority)
                shots = tuple(
                    replace(
                        shot,
                        subject_ids=tuple(
                            dict.fromkeys(
                                (
                                    *shot.subject_ids,
                                    *canonical_text_subject_ids(shot.text, semantic_authority),
                                )
                            )
                        ),
                        asset_ids=tuple(
                            dict.fromkeys(
                                (
                                    *shot.asset_ids,
                                    *canonical_text_asset_ids(shot.text, semantic_authority),
                                )
                            )
                        ),
                    )
                    for shot in shots
                )
            _validate_shot_references(context, shots)
            validate_semantic_references(
                semantic_authority,
                subject_ids=context.subject_ids,
                asset_ids=tuple(item.asset_id for item in context.assets),
            )
        except ValueError:
            return StoryboardAdmissionRefusalV1(
                request.request_id, StoryboardAdmissionRefusalCodeV1.REFERENCE_MEMBERSHIP_INVALID
            )
        if len(self._entries) >= self._max_entries:
            raise ProductionStoryboardError("storyboard_service_capacity")
        material: dict[str, object] = {
            "request_fingerprint": request_fingerprint,
            "context_fingerprint": context.fingerprint,
            "shots": [item.to_wire() for item in shots],
        }
        if isinstance(context, ProductionPlanningContextV2):
            material["semantic_authority"] = semantic_authority.to_wire()
        admission = ProductionStoryboardAdmissionV1(
            admission_id="admission_" + canonical_fingerprint(material)[7:31],
            request_id=request.request_id,
            request_fingerprint=request_fingerprint,
            planning_context_id=context.planning_context_id,
            planning_context_revision=context.revision,
            planning_context_fingerprint=context.fingerprint,
            optimized_candidate_id=context.optimized_candidate_id,
            optimized_candidate_text_fingerprint=context.optimized_candidate_text_fingerprint,
            production_duration_intent_fingerprint=context.duration_intent_fingerprint,
            source_kind=request.source_kind,
            shots=shots,
            cut_milliseconds=tuple(item.end_milliseconds for item in shots[:-1]),
        )
        if isinstance(context, ProductionPlanningContextV2):
            admission = ProductionStoryboardAdmissionV2(
                **_inherited_values(admission), semantic_authority=semantic_authority
            )
        self._entries[request.request_id] = (request_fingerprint, admission)
        return admission


def admit_production_storyboard(
    context: ProductionPlanningContextV1,
    request: ProductionStoryboardAdmissionRequestV1,
    *,
    service: ProductionStoryboardAdmissionService | None = None,
) -> ProductionStoryboardAdmissionV1 | StoryboardAdmissionRefusalV1:
    """Execute the named admission action through an optional replay owner."""

    owner = ProductionStoryboardAdmissionService() if service is None else service
    if type(owner) is not ProductionStoryboardAdmissionService:
        raise ProductionStoryboardError("storyboard_admission_service")
    return owner.admit(context, request)


@dataclass(frozen=True, slots=True)
class SegmentShotMappingV1:
    global_shot_id: str
    local_shot_id: str
    global_start_milliseconds: int
    global_end_milliseconds: int
    local_start_milliseconds: int
    local_end_milliseconds: int

    def __post_init__(self) -> None:
        _identifier(self.global_shot_id, "shot_mapping_global_id")
        _identifier(self.local_shot_id, "shot_mapping_local_id")
        values = (
            self.global_start_milliseconds,
            self.global_end_milliseconds,
            self.local_start_milliseconds,
            self.local_end_milliseconds,
        )
        if any(type(item) is not int or item < 0 for item in values):
            raise ProductionStoryboardError("shot_mapping_time")
        if (
            self.global_end_milliseconds <= self.global_start_milliseconds
            or self.local_end_milliseconds <= self.local_start_milliseconds
        ):
            raise ProductionStoryboardError("shot_mapping_span")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": SHOT_MAPPING_SCHEMA,
            "global_shot_id": self.global_shot_id,
            "local_shot_id": self.local_shot_id,
            "global_start_milliseconds": self.global_start_milliseconds,
            "global_end_milliseconds": self.global_end_milliseconds,
            "local_start_milliseconds": self.local_start_milliseconds,
            "local_end_milliseconds": self.local_end_milliseconds,
        }

    @classmethod
    def from_wire(cls, value: object) -> SegmentShotMappingV1:
        keys = frozenset(
            {
                "schema",
                "global_shot_id",
                "local_shot_id",
                "global_start_milliseconds",
                "global_end_milliseconds",
                "local_start_milliseconds",
                "local_end_milliseconds",
            }
        )
        wire = _closed_dict(value, "shot_mapping_wire", keys)
        if wire["schema"] != SHOT_MAPPING_SCHEMA:
            raise ProductionStoryboardError("shot_mapping_schema")
        return cls(
            global_shot_id=cast(str, wire["global_shot_id"]),
            local_shot_id=cast(str, wire["local_shot_id"]),
            global_start_milliseconds=cast(int, wire["global_start_milliseconds"]),
            global_end_milliseconds=cast(int, wire["global_end_milliseconds"]),
            local_start_milliseconds=cast(int, wire["local_start_milliseconds"]),
            local_end_milliseconds=cast(int, wire["local_end_milliseconds"]),
        )


@dataclass(frozen=True, slots=True)
class ProposalSegmentV1:
    segment_id: str
    ordinal: int
    global_start_milliseconds: int
    global_end_milliseconds: int
    duration: SegmentDurationAllocationV1
    task_mode: TaskMode
    asset_ids: tuple[str, ...]
    assigned_shot_ids: tuple[str, ...]
    shot_mappings: tuple[SegmentShotMappingV1, ...]
    local_prompt: str
    predecessor_segment_id: str | None
    join_policy: str = "cut"
    predecessor_artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "proposal_segment_id")
        _integer(self.ordinal, "proposal_segment_ordinal", 1, MAX_PRODUCTION_SEGMENTS)
        if type(self.duration) is not SegmentDurationAllocationV1:
            raise ProductionStoryboardError("proposal_segment_duration")
        if (
            self.duration.ordinal != self.ordinal
            or self.global_start_milliseconds != self.duration.requested_start_seconds * 1000
            or self.global_end_milliseconds != self.duration.requested_end_seconds * 1000
        ):
            raise ProductionStoryboardError("proposal_segment_span")
        if type(self.task_mode) is not TaskMode:
            raise ProductionStoryboardError("proposal_segment_task_mode")
        _identifiers(self.asset_ids, "proposal_segment_asset_ids", 12)
        _identifiers(self.assigned_shot_ids, "proposal_segment_shot_ids", MAX_STORYBOARD_SHOTS)
        if type(self.shot_mappings) is not tuple or any(
            type(item) is not SegmentShotMappingV1 for item in self.shot_mappings
        ):
            raise ProductionStoryboardError("proposal_segment_shot_mappings")
        if tuple(item.global_shot_id for item in self.shot_mappings) != self.assigned_shot_ids:
            raise ProductionStoryboardError("proposal_segment_shot_mapping_ids")
        if not self.shot_mappings or tuple(
            item.local_shot_id for item in self.shot_mappings
        ) != tuple(f"shot_{index}" for index in range(1, len(self.shot_mappings) + 1)):
            raise ProductionStoryboardError("proposal_segment_shot_mapping_ids")
        # CRITICAL: an outer fingerprint can authenticate a corrupted mapping as written. Keep the
        # local clock an exact reversible offset or downstream review can display the wrong span.
        cursor = self.global_start_milliseconds
        for mapping in self.shot_mappings:
            if (
                mapping.global_start_milliseconds != cursor
                or mapping.local_start_milliseconds
                != mapping.global_start_milliseconds - self.global_start_milliseconds
                or mapping.local_end_milliseconds
                != mapping.global_end_milliseconds - self.global_start_milliseconds
                or mapping.global_end_milliseconds > self.global_end_milliseconds
            ):
                raise ProductionStoryboardError("proposal_segment_shot_mapping_timebase")
            cursor = mapping.global_end_milliseconds
        if cursor != self.global_end_milliseconds:
            raise ProductionStoryboardError("proposal_segment_shot_mapping_timebase")
        _text(self.local_prompt, "proposal_segment_local_prompt", MAX_PROMPT_LENGTH)
        if self.predecessor_segment_id is not None:
            _identifier(self.predecessor_segment_id, "proposal_segment_predecessor")
        if self.join_policy != "cut" or self.predecessor_artifact_ids:
            raise ProductionStoryboardError("proposal_segment_join_policy")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PROPOSAL_SEGMENT_SCHEMA,
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "global_start_milliseconds": self.global_start_milliseconds,
            "global_end_milliseconds": self.global_end_milliseconds,
            "duration": self.duration.to_wire(),
            "task_mode": self.task_mode.value,
            "asset_ids": list(self.asset_ids),
            "assigned_shot_ids": list(self.assigned_shot_ids),
            "shot_mappings": [item.to_wire() for item in self.shot_mappings],
            "local_prompt": self.local_prompt,
            "predecessor_segment_id": self.predecessor_segment_id,
            "join_policy": self.join_policy,
            "predecessor_artifact_ids": list(self.predecessor_artifact_ids),
        }

    @classmethod
    def from_wire(cls, value: object) -> ProposalSegmentV1:
        keys = frozenset(
            {
                "schema",
                "segment_id",
                "ordinal",
                "global_start_milliseconds",
                "global_end_milliseconds",
                "duration",
                "task_mode",
                "asset_ids",
                "assigned_shot_ids",
                "shot_mappings",
                "local_prompt",
                "predecessor_segment_id",
                "join_policy",
                "predecessor_artifact_ids",
            }
        )
        wire = _closed_dict(value, "proposal_segment_wire", keys)
        if wire["schema"] != PROPOSAL_SEGMENT_SCHEMA:
            raise ProductionStoryboardError("proposal_segment_schema")
        assets = _wire_tuple(wire["asset_ids"], "proposal_segment_asset_ids_wire", 12)
        shots = _wire_tuple(
            wire["assigned_shot_ids"], "proposal_segment_shot_ids_wire", MAX_STORYBOARD_SHOTS
        )
        mappings = _wire_tuple(
            wire["shot_mappings"], "proposal_segment_shot_mappings_wire", MAX_STORYBOARD_SHOTS
        )
        artifacts = _wire_tuple(
            wire["predecessor_artifact_ids"], "proposal_segment_artifact_ids_wire", 0
        )
        predecessor = wire["predecessor_segment_id"]
        if predecessor is not None and type(predecessor) is not str:
            raise ProductionStoryboardError("proposal_segment_predecessor_wire")
        return cls(
            segment_id=cast(str, wire["segment_id"]),
            ordinal=cast(int, wire["ordinal"]),
            global_start_milliseconds=cast(int, wire["global_start_milliseconds"]),
            global_end_milliseconds=cast(int, wire["global_end_milliseconds"]),
            duration=SegmentDurationAllocationV1.from_wire(wire["duration"]),
            task_mode=cast(
                TaskMode, _enum(wire["task_mode"], TaskMode, "proposal_segment_task_mode")
            ),
            asset_ids=tuple(cast(tuple[str, ...], assets)),
            assigned_shot_ids=tuple(cast(tuple[str, ...], shots)),
            shot_mappings=tuple(SegmentShotMappingV1.from_wire(item) for item in mappings),
            local_prompt=cast(str, wire["local_prompt"]),
            predecessor_segment_id=predecessor,
            join_policy=cast(str, wire["join_policy"]),
            predecessor_artifact_ids=tuple(cast(tuple[str, ...], artifacts)),
        )


@dataclass(frozen=True, slots=True)
class SegmentationProposalV1:
    proposal_id: str
    revision: int
    planning_context_fingerprint: str
    storyboard_admission_fingerprint: str
    duration_intent_fingerprint: str
    duration_plan: SegmentDurationPlanV1
    segments: tuple[ProposalSegmentV1, ...]
    blocker_codes: tuple[ProposalBlockerCodeV1, ...] = ()
    start_hold_codes: tuple[ProposalBlockerCodeV1, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.proposal_id, "segmentation_proposal_id")
        _integer(self.revision, "segmentation_proposal_revision", 1, 1_000_000)
        _fingerprint(self.planning_context_fingerprint, "segmentation_proposal_context_fingerprint")
        _fingerprint(
            self.storyboard_admission_fingerprint,
            "segmentation_proposal_admission_fingerprint",
        )
        _fingerprint(self.duration_intent_fingerprint, "segmentation_proposal_duration_fingerprint")
        if type(self.duration_plan) is not SegmentDurationPlanV1:
            raise ProductionStoryboardError("segmentation_proposal_duration_plan")
        if (
            type(self.segments) is not tuple
            or not self.segments
            or len(self.segments) != len(self.duration_plan.allocations)
            or any(type(item) is not ProposalSegmentV1 for item in self.segments)
        ):
            raise ProductionStoryboardError("segmentation_proposal_segments")
        for index, segment in enumerate(self.segments, start=1):
            predecessor = None if index == 1 else self.segments[index - 2].segment_id
            if segment.ordinal != index or segment.predecessor_segment_id != predecessor:
                raise ProductionStoryboardError("segmentation_proposal_segment_order")
        for values, field in (
            (self.blocker_codes, "segmentation_proposal_blockers"),
            (self.start_hold_codes, "segmentation_proposal_start_holds"),
        ):
            if (
                type(values) is not tuple
                or len(values) != len(set(values))
                or any(type(item) is not ProposalBlockerCodeV1 for item in values)
            ):
                raise ProductionStoryboardError(field)

    @property
    def importable(self) -> bool:
        return not self.blocker_codes

    @property
    def startable(self) -> bool:
        return self.importable and not self.start_hold_codes

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire(include_fingerprint=False))

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": SEGMENTATION_PROPOSAL_SCHEMA,
            "proposal_id": self.proposal_id,
            "revision": self.revision,
            "planning_context_fingerprint": self.planning_context_fingerprint,
            "storyboard_admission_fingerprint": self.storyboard_admission_fingerprint,
            "duration_intent_fingerprint": self.duration_intent_fingerprint,
            "duration_plan": self.duration_plan.to_wire(),
            "segments": [item.to_wire() for item in self.segments],
            "blocker_codes": [item.value for item in self.blocker_codes],
            "start_hold_codes": [item.value for item in self.start_hold_codes],
            "importable": self.importable,
            "startable": self.startable,
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value

    @classmethod
    def from_wire(cls, value: object) -> SegmentationProposalV1:
        keys = frozenset(
            {
                "schema",
                "proposal_id",
                "revision",
                "planning_context_fingerprint",
                "storyboard_admission_fingerprint",
                "duration_intent_fingerprint",
                "duration_plan",
                "segments",
                "blocker_codes",
                "start_hold_codes",
                "importable",
                "startable",
                "fingerprint",
            }
        )
        wire = _closed_dict(value, "segmentation_proposal_wire", keys)
        if wire["schema"] != SEGMENTATION_PROPOSAL_SCHEMA:
            raise ProductionStoryboardError("segmentation_proposal_schema")
        segments = _wire_tuple(wire["segments"], "segmentation_proposal_segments_wire")
        blockers = _wire_tuple(wire["blocker_codes"], "segmentation_proposal_blockers_wire")
        holds = _wire_tuple(wire["start_hold_codes"], "segmentation_proposal_holds_wire")
        result = cls(
            proposal_id=cast(str, wire["proposal_id"]),
            revision=cast(int, wire["revision"]),
            planning_context_fingerprint=cast(str, wire["planning_context_fingerprint"]),
            storyboard_admission_fingerprint=cast(str, wire["storyboard_admission_fingerprint"]),
            duration_intent_fingerprint=cast(str, wire["duration_intent_fingerprint"]),
            duration_plan=SegmentDurationPlanV1.from_wire(wire["duration_plan"]),
            segments=tuple(ProposalSegmentV1.from_wire(item) for item in segments),
            blocker_codes=tuple(
                cast(
                    ProposalBlockerCodeV1,
                    _enum(item, ProposalBlockerCodeV1, "segmentation_proposal_blocker"),
                )
                for item in blockers
            ),
            start_hold_codes=tuple(
                cast(
                    ProposalBlockerCodeV1,
                    _enum(item, ProposalBlockerCodeV1, "segmentation_proposal_hold"),
                )
                for item in holds
            ),
        )
        if wire["importable"] is not result.importable or wire["startable"] is not result.startable:
            raise ProductionStoryboardError("segmentation_proposal_capability_flags")
        if wire["fingerprint"] != result.fingerprint:
            raise ProductionStoryboardError("segmentation_proposal_fingerprint")
        return result


@dataclass(frozen=True, slots=True)
class SegmentationProposalRefusalV1:
    planning_context_fingerprint: str
    storyboard_admission_fingerprint: str
    code: SegmentationProposalRefusalCodeV1

    def __post_init__(self) -> None:
        _fingerprint(self.planning_context_fingerprint, "proposal_refusal_context_fingerprint")
        _fingerprint(
            self.storyboard_admission_fingerprint, "proposal_refusal_admission_fingerprint"
        )
        if type(self.code) is not SegmentationProposalRefusalCodeV1:
            raise ProductionStoryboardError("proposal_refusal_code")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": SEGMENTATION_PROPOSAL_REFUSAL_SCHEMA,
            "planning_context_fingerprint": self.planning_context_fingerprint,
            "storyboard_admission_fingerprint": self.storyboard_admission_fingerprint,
            "code": self.code.value,
        }

    @classmethod
    def from_wire(cls, value: object) -> SegmentationProposalRefusalV1:
        wire = _closed_dict(
            value,
            "proposal_refusal_wire",
            frozenset(
                {
                    "schema",
                    "planning_context_fingerprint",
                    "storyboard_admission_fingerprint",
                    "code",
                }
            ),
        )
        if wire["schema"] != SEGMENTATION_PROPOSAL_REFUSAL_SCHEMA:
            raise ProductionStoryboardError("proposal_refusal_schema")
        return cls(
            planning_context_fingerprint=cast(str, wire["planning_context_fingerprint"]),
            storyboard_admission_fingerprint=cast(str, wire["storyboard_admission_fingerprint"]),
            code=cast(
                SegmentationProposalRefusalCodeV1,
                _enum(
                    wire["code"],
                    SegmentationProposalRefusalCodeV1,
                    "proposal_refusal_code",
                ),
            ),
        )


def _task_assets(
    context: ProductionPlanningContextV1, ordinal: int, count: int
) -> tuple[TaskMode, tuple[str, ...], bool]:
    assets_by_role: dict[AssetRole, tuple[str, ...]] = {}
    for role in AssetRole:
        assets_by_role[role] = tuple(item.asset_id for item in context.assets if item.role is role)
    first = assets_by_role[AssetRole.FIRST_FRAME]
    last = assets_by_role[AssetRole.LAST_FRAME]
    references = tuple(item.asset_id for item in context.assets)
    mode = context.global_task_mode
    if count == 1:
        required = {
            TaskMode.T2VA: True,
            TaskMode.I2VA: len(first) == 1,
            TaskMode.FL2VA: len(first) == 1 and len(last) == 1,
            TaskMode.L2VA: len(last) == 1,
            TaskMode.REF2VA: bool(references),
        }[mode]
        return mode, references, required
    if mode is TaskMode.T2VA:
        return TaskMode.T2VA, (), True
    if mode is TaskMode.I2VA:
        return (
            (TaskMode.I2VA, first, len(first) == 1) if ordinal == 1 else (TaskMode.T2VA, (), True)
        )
    if mode is TaskMode.FL2VA:
        if ordinal == 1:
            return TaskMode.I2VA, first, len(first) == 1
        if ordinal == count:
            return TaskMode.L2VA, last, len(last) == 1
        return TaskMode.T2VA, (), True
    if mode is TaskMode.L2VA:
        return (
            (TaskMode.L2VA, last, len(last) == 1) if ordinal == count else (TaskMode.T2VA, (), True)
        )
    return TaskMode.REF2VA, references, bool(references)


def _clip_duration_seconds(allocation: SegmentDurationAllocationV1) -> Decimal:
    """The clip's delivered length: what its own Context reports as effective duration."""

    # GUARD: an alignment sentence states the mark of the clip it is rendered for. The
    # requested whole seconds are not that mark: ten seconds is 243 frames, 10.125 s, and that
    # lattice length is what the materialized segment Context reports as its effective duration.
    # Rendering the requested seconds here makes the prompt disagree with its own Context.
    return Decimal(allocation.frame_count) / Decimal(FPS)


def _format_time(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def _render_local_prompt(
    context: ProductionPlanningContextV1,
    allocation: SegmentDurationAllocationV1,
    shots: tuple[StoryboardShotV1, ...],
) -> tuple[str, tuple[SegmentShotMappingV1, ...]]:
    segment_start = allocation.requested_start_seconds * 1000
    segment_end = allocation.requested_end_seconds * 1000
    lines = [
        f"production_segment: {allocation.ordinal}",
        f"global_span: {_format_time(segment_start)}-{_format_time(segment_end)}",
        "join_policy: cut",
    ]
    if context.subject_ids:
        lines.append("global_subject_ids: " + ", ".join(context.subject_ids))
    if context.reference_ids:
        lines.append("global_reference_ids: " + ", ".join(context.reference_ids))
    for constraint in context.hard_constraints:
        lines.append("hard_constraint: " + constraint)
    mappings: list[SegmentShotMappingV1] = []
    for local_ordinal, shot in enumerate(shots, start=1):
        clipped_start = max(shot.start_milliseconds, segment_start)
        clipped_end = min(shot.end_milliseconds, segment_end)
        local_start = clipped_start - segment_start
        local_end = clipped_end - segment_start
        local_id = f"shot_{local_ordinal}"
        prefix = (
            "[Shot 1]"
            if local_ordinal == 1
            else f"[Shot {local_ordinal}] At {_format_time(local_start)},"
        )
        lines.append(f"{prefix} {shot.text}")
        # IMPORTANT: typed exact-text fields are authoritative even when the descriptive prose
        # omits them; dropping these rows here makes an apparently reversible local prompt lossy.
        lines.extend(f"exact_dialogue: {item}" for item in shot.exact_dialogue)
        lines.extend(f"visible_text: {item}" for item in shot.visible_text)
        mappings.append(
            SegmentShotMappingV1(
                global_shot_id=shot.shot_id,
                local_shot_id=local_id,
                global_start_milliseconds=clipped_start,
                global_end_milliseconds=clipped_end,
                local_start_milliseconds=local_start,
                local_end_milliseconds=local_end,
            )
        )
    rendered = "\n".join(lines)
    _text(rendered, "rendered_local_prompt", MAX_PROMPT_LENGTH)
    return rendered, tuple(mappings)


def _proposal_source_matches(
    context: ProductionPlanningContextV1, admission: ProductionStoryboardAdmissionV1
) -> bool:
    return (
        admission.planning_context_id == context.planning_context_id
        and admission.planning_context_revision == context.revision
        and admission.planning_context_fingerprint == context.fingerprint
        and admission.optimized_candidate_id == context.optimized_candidate_id
        and admission.optimized_candidate_text_fingerprint
        == context.optimized_candidate_text_fingerprint
        and admission.production_duration_intent_fingerprint == context.duration_intent_fingerprint
    )


def build_segmentation_proposal(
    context: ProductionPlanningContextV1,
    admission: ProductionStoryboardAdmissionV1,
    *,
    native_modes: frozenset[TaskMode] | None = None,
) -> SegmentationProposalV1 | SegmentationProposalRefusalV1:
    """Create one deterministic immutable proposal, never executable work."""

    try:
        return _build_segmentation_proposal(context, admission, native_modes=native_modes)
    except (ProductionSemanticError, PromptRenderingError, ProductionDurationError):
        return SegmentationProposalRefusalV1(
            context.fingerprint,
            admission.fingerprint,
            SegmentationProposalRefusalCodeV1.SEMANTIC_PRESERVATION_UNAVAILABLE,
        )


def _build_segmentation_proposal(
    context: ProductionPlanningContextV1,
    admission: ProductionStoryboardAdmissionV1,
    *,
    native_modes: frozenset[TaskMode] | None,
) -> SegmentationProposalV1 | SegmentationProposalRefusalV1:

    if type(context) not in (ProductionPlanningContextV1, ProductionPlanningContextV2):
        raise ProductionStoryboardError("proposal_context")
    if type(admission) not in (ProductionStoryboardAdmissionV1, ProductionStoryboardAdmissionV2):
        raise ProductionStoryboardError("proposal_admission")
    if not _proposal_source_matches(context, admission) or (
        isinstance(context, ProductionPlanningContextV2)
        != isinstance(admission, ProductionStoryboardAdmissionV2)
    ):
        return SegmentationProposalRefusalV1(
            context.fingerprint,
            admission.fingerprint,
            SegmentationProposalRefusalCodeV1.SOURCE_BINDING_STALE,
        )
    if isinstance(context, ProductionPlanningContextV2) and isinstance(
        admission, ProductionStoryboardAdmissionV2
    ):
        # CRITICAL: a recomposed admission must extend the retained context's actual semantics.
        # Replacing its source content and recomputing hashes must never erase a hard requirement.
        try:
            source_semantics = bind_canonical_semantic_references(context.semantic_authority)
            if merge_semantic_authorities(source_semantics, admission.semantic_authority) != (
                admission.semantic_authority
            ):
                raise ProductionSemanticError("admission_semantic_source_mismatch")
        except ProductionSemanticError:
            return SegmentationProposalRefusalV1(
                context.fingerprint,
                admission.fingerprint,
                SegmentationProposalRefusalCodeV1.SOURCE_BINDING_STALE,
            )
    # CRITICAL: the admission fingerprint is self-consistent, not proof that a recomposed row set
    # covers this context's target. Revalidate the cross-contract join before planning any cut.
    try:
        _validate_coverage(admission.shots, context.production_target_duration_seconds * 1000)
        _validate_shot_references(context, admission.shots)
        if isinstance(admission, ProductionStoryboardAdmissionV2):
            validate_semantic_references(
                admission.semantic_authority,
                subject_ids=context.subject_ids,
                asset_ids=tuple(item.asset_id for item in context.assets),
            )
    except ProductionStoryboardError:
        return SegmentationProposalRefusalV1(
            context.fingerprint,
            admission.fingerprint,
            SegmentationProposalRefusalCodeV1.STORYBOARD_COVERAGE_INVALID,
        )
    intent = context.production_duration_intent
    if intent.policy.value == "auto_storyboard":
        whole_second_boundaries = tuple(
            value // 1000
            for value in admission.cut_milliseconds
            if value % 1000 == 0 and 0 < value < intent.target_seconds * 1000
        )
        intent = replace(intent, candidate_boundaries_seconds=whole_second_boundaries)
    cut_constraints: SegmentCutConstraintsV1 | None = None
    if isinstance(admission, ProductionStoryboardAdmissionV2):
        # IMPORTANT: feasibility belongs inside the search. Rejecting only the selected numeric
        # result loses legal alternate partitions for exact dialogue, visible text and timed audio.
        forbidden = {
            (shot.start_milliseconds, shot.end_milliseconds)
            for shot in admission.shots
            if shot.hard_boundary or shot.exact_dialogue or shot.visible_text
        }
        forbidden.update(
            (span.start_milliseconds, span.end_milliseconds)
            for span in admission.semantic_authority.timed_spans
            if span.split_policy.value == "immutable"
        )
        forbidden.update(admission.semantic_authority.forbidden_cut_intervals_milliseconds)
        cut_constraints = SegmentCutConstraintsV1(
            required_cut_milliseconds=admission.semantic_authority.required_cut_milliseconds,
            forbidden_cut_intervals_milliseconds=tuple(sorted(forbidden)),
        )
    duration_plan = solve_segment_duration_plan(intent, constraints=cut_constraints)
    if type(duration_plan) is SegmentDurationPlanRefusalV1:
        return SegmentationProposalRefusalV1(
            context.fingerprint,
            admission.fingerprint,
            SegmentationProposalRefusalCodeV1.DURATION_PLAN_UNAVAILABLE,
        )
    if type(duration_plan) is not SegmentDurationPlanV1:  # pragma: no cover
        raise ProductionStoryboardError("proposal_duration_result")
    if native_modes is None:
        native_modes = frozenset(
            item.task_mode for item in build_default_native_mode_matrix().modes
        )
    elif type(native_modes) is not frozenset or any(
        type(item) is not TaskMode for item in native_modes
    ):
        raise ProductionStoryboardError("proposal_native_modes")

    blockers: list[ProposalBlockerCodeV1] = []
    boundaries = {item.requested_end_seconds * 1000 for item in duration_plan.allocations[:-1]}
    if any(
        shot.start_milliseconds < boundary < shot.end_milliseconds
        and (shot.hard_boundary or shot.exact_dialogue or shot.visible_text)
        for boundary in boundaries
        for shot in admission.shots
    ):
        blockers.append(ProposalBlockerCodeV1.HARD_CONTENT_CROSSES_BOUNDARY)

    segments: list[ProposalSegmentV1] = []
    semantic_slices: list[ProductionSemanticSliceV1] = []
    for allocation in duration_plan.allocations:
        start = allocation.requested_start_seconds * 1000
        end = allocation.requested_end_seconds * 1000
        assigned = tuple(
            shot
            for shot in admission.shots
            if shot.start_milliseconds < end and shot.end_milliseconds > start
        )
        task_mode, asset_ids, assets_ready = _task_assets(
            context, allocation.ordinal, len(duration_plan.allocations)
        )
        # CRITICAL: global membership does not grant this cut's local mode access to an image.
        # Preserve the frozen mode/asset table and block the proposal instead of dropping a bind.
        referenced_assets = {asset for shot in assigned for asset in shot.asset_ids}
        if isinstance(admission, ProductionStoryboardAdmissionV2):
            referenced_subjects = {subject for shot in assigned for subject in shot.subject_ids}
            referenced_assets.update(
                asset
                for subject in admission.semantic_authority.subject_definitions
                if subject.subject_id in referenced_subjects
                for asset in subject.source_asset_ids
            )
        if not set(asset_ids).issuperset(referenced_assets):
            if ProposalBlockerCodeV1.LOCAL_REFERENCE_UNAVAILABLE not in blockers:
                blockers.append(ProposalBlockerCodeV1.LOCAL_REFERENCE_UNAVAILABLE)
        if not assets_ready and ProposalBlockerCodeV1.REQUIRED_ASSET_MISSING not in blockers:
            blockers.append(ProposalBlockerCodeV1.REQUIRED_ASSET_MISSING)
        if (
            task_mode not in native_modes
            and ProposalBlockerCodeV1.NATIVE_MAPPING_UNAVAILABLE not in blockers
        ):
            blockers.append(ProposalBlockerCodeV1.NATIVE_MAPPING_UNAVAILABLE)
        if isinstance(admission, ProductionStoryboardAdmissionV2):
            mappings = tuple(
                SegmentShotMappingV1(
                    global_shot_id=shot.shot_id,
                    local_shot_id=f"shot_{index}",
                    global_start_milliseconds=max(start, shot.start_milliseconds),
                    global_end_milliseconds=min(end, shot.end_milliseconds),
                    local_start_milliseconds=max(start, shot.start_milliseconds) - start,
                    local_end_milliseconds=min(end, shot.end_milliseconds) - start,
                )
                for index, shot in enumerate(assigned, start=1)
            )
            # IMPORTANT: bind identity to source inputs before slicing; the slice names this
            # segment, so hashing its rendered output here would create a circular identity.
            segment_id = (
                "segment_"
                + canonical_fingerprint(
                    {
                        "context": context.fingerprint,
                        "admission": admission.fingerprint,
                        "allocation": allocation.to_wire(),
                        "task_mode": task_mode.value,
                        "asset_ids": list(asset_ids),
                    }
                )[7:31]
            )
            semantic_slice = slice_production_semantics(
                admission.semantic_authority,
                segment_id=segment_id,
                global_start_milliseconds=start,
                global_end_milliseconds=end,
                task_mode=task_mode,
                asset_ids=asset_ids,
            )
            semantic_slices.append(semantic_slice)
            local_prompt = render_production_segment_prompt(
                semantic_slice,
                tuple(
                    ProductionLocalShotV1(
                        shot_id=mapping.local_shot_id,
                        ordinal=index,
                        start_milliseconds=mapping.local_start_milliseconds,
                        prose=shot.text,
                        exact_dialogue=shot.exact_dialogue,
                        visible_text=shot.visible_text,
                    )
                    for index, (shot, mapping) in enumerate(zip(assigned, mappings, strict=True), 1)
                ),
                section_heading=(
                    "detailed_description"
                    if task_mode is TaskMode.REF2VA
                    else "integrated_multimodal_description"
                ),
                clip_duration_seconds=_clip_duration_seconds(allocation),
                hard_constraints=context.hard_constraints,
            ).text
        else:
            local_prompt, mappings = _render_local_prompt(context, allocation, assigned)
        segment_material = {
            "context_fingerprint": context.fingerprint,
            "admission_fingerprint": admission.fingerprint,
            "allocation": allocation.to_wire(),
            "task_mode": task_mode.value,
            "asset_ids": list(asset_ids),
            "mappings": [item.to_wire() for item in mappings],
            "local_prompt_fingerprint": fingerprint_prompt_text(local_prompt),
        }
        if not isinstance(admission, ProductionStoryboardAdmissionV2):
            segment_id = "segment_" + canonical_fingerprint(segment_material)[7:31]
        segments.append(
            ProposalSegmentV1(
                segment_id=segment_id,
                ordinal=allocation.ordinal,
                global_start_milliseconds=start,
                global_end_milliseconds=end,
                duration=allocation,
                task_mode=task_mode,
                asset_ids=asset_ids,
                assigned_shot_ids=tuple(item.shot_id for item in assigned),
                shot_mappings=mappings,
                local_prompt=local_prompt,
                predecessor_segment_id=segments[-1].segment_id if segments else None,
            )
        )
    holds: tuple[ProposalBlockerCodeV1, ...]
    if context.managed_execution_qualification is ManagedExecutionQualificationV1.PENDING:
        holds = (ProposalBlockerCodeV1.MANAGED_EXECUTION_QUALIFICATION_PENDING,)
    elif context.managed_execution_qualification is ManagedExecutionQualificationV1.UNSUPPORTED:
        holds = (ProposalBlockerCodeV1.MANAGED_EXECUTION_UNSUPPORTED,)
    else:
        holds = ()
    proposal_material = {
        "context_fingerprint": context.fingerprint,
        "admission_fingerprint": admission.fingerprint,
        "duration_plan": duration_plan.to_wire(),
        "segments": [item.to_wire() for item in segments],
        "blockers": [item.value for item in blockers],
        "holds": [item.value for item in holds],
    }
    proposal = SegmentationProposalV1(
        proposal_id="proposal_" + canonical_fingerprint(proposal_material)[7:31],
        revision=1,
        planning_context_fingerprint=context.fingerprint,
        storyboard_admission_fingerprint=admission.fingerprint,
        duration_intent_fingerprint=context.duration_intent_fingerprint,
        duration_plan=duration_plan,
        segments=tuple(segments),
        blocker_codes=tuple(blockers),
        start_hold_codes=holds,
    )
    if isinstance(admission, ProductionStoryboardAdmissionV2):
        try:
            census = assert_complete_semantic_census(
                admission.semantic_authority, tuple(semantic_slices)
            )
        except ValueError:
            return SegmentationProposalRefusalV1(
                context.fingerprint,
                admission.fingerprint,
                SegmentationProposalRefusalCodeV1.STORYBOARD_COVERAGE_INVALID,
            )
        if cut_constraints is None:  # pragma: no cover - current admission always sets constraints
            raise ProductionStoryboardError("proposal_cut_constraints_missing")
        return SegmentationProposalV2(
            **_inherited_values(proposal),
            semantic_authority=admission.semantic_authority,
            semantic_slices=tuple(semantic_slices),
            semantic_census=census,
            cut_constraints=cut_constraints,
            source_shots=admission.shots,
            hard_constraints=context.hard_constraints,
        )
    return proposal


def _inherited_values(value: object) -> dict[str, Any]:
    """Copy validated dataclass fields, never an untrusted wire or arbitrary attributes."""
    return {item.name: getattr(value, item.name) for item in fields(value)}  # type: ignore[arg-type]


def _read_versioned_base(
    value: object, schema: str, base_schema: str, extra_keys: frozenset[str]
) -> tuple[dict[str, object], dict[str, object]]:
    if type(value) is not dict:
        raise ProductionStoryboardError("semantic_versioned_wire")
    wire = cast(dict[str, object], value)
    if wire.get("schema") != schema or not extra_keys.issubset(wire):
        raise ProductionStoryboardError("semantic_versioned_schema")
    _fingerprint(wire.get("fingerprint"), "semantic_versioned_fingerprint")
    base = {key: item for key, item in wire.items() if key not in extra_keys}
    base["schema"] = base_schema
    base.pop("fingerprint")
    base["fingerprint"] = canonical_fingerprint(base)
    return wire, base


@dataclass(frozen=True, slots=True)
class ProductionPlanningContextV2(ProductionPlanningContextV1):
    """Explicit current planning authority; V1 reads never acquire these semantic inputs."""

    semantic_authority: ProductionSemanticAuthorityV1 = dataclass_field(
        default_factory=ProductionSemanticAuthorityV1, kw_only=True
    )

    def __post_init__(self) -> None:
        ProductionPlanningContextV1.__post_init__(self)
        if type(self.semantic_authority) is not ProductionSemanticAuthorityV1:
            raise ProductionStoryboardError("planning_semantic_authority")
        validate_semantic_references(
            self.semantic_authority,
            subject_ids=self.subject_ids,
            asset_ids=tuple(item.asset_id for item in self.assets),
        )
        if (
            any(
                span.end_milliseconds > self.production_target_duration_seconds * 1000
                for span in self.semantic_authority.timed_spans
            )
            or any(
                end > self.production_target_duration_seconds * 1000
                for _, end in self.semantic_authority.forbidden_cut_intervals_milliseconds
            )
            or any(
                cut >= self.production_target_duration_seconds * 1000
                for cut in self.semantic_authority.required_cut_milliseconds
            )
        ):
            raise ProductionStoryboardError("planning_semantic_time_domain")
        roles = {item.asset_id: item.role for item in self.assets}
        if any(
            roles.get(item.asset_id) is not item.role
            for item in self.semantic_authority.reference_definitions
        ):
            raise ProductionStoryboardError("planning_semantic_reference_role")
        declared_ids = tuple(
            item.asset_id for item in self.semantic_authority.reference_definitions
        )
        if declared_ids != tuple(
            item.asset_id for item in self.assets if item.asset_id in declared_ids
        ):
            raise ProductionStoryboardError("planning_semantic_reference_order")
        declared_subjects = tuple(
            item.subject_id for item in self.semantic_authority.subject_definitions
        )
        if declared_subjects != tuple(
            value for value in self.subject_ids if value in declared_subjects
        ):
            raise ProductionStoryboardError("planning_semantic_subject_order")

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        wire = ProductionPlanningContextV1.to_wire(self, include_fingerprint=False)
        wire["schema"] = PRODUCTION_PLANNING_CONTEXT_V2_SCHEMA
        wire["semantic_authority"] = self.semantic_authority.to_wire()
        if include_fingerprint:
            wire["fingerprint"] = self.fingerprint
        return wire

    @classmethod
    def from_wire(cls, value: object) -> ProductionPlanningContextV2:
        wire, base = _read_versioned_base(
            value,
            PRODUCTION_PLANNING_CONTEXT_V2_SCHEMA,
            PRODUCTION_PLANNING_CONTEXT_SCHEMA,
            frozenset({"semantic_authority"}),
        )
        result = cls(
            **_inherited_values(ProductionPlanningContextV1.from_wire(base)),
            semantic_authority=ProductionSemanticAuthorityV1.from_wire(wire["semantic_authority"]),
        )
        if wire["fingerprint"] != result.fingerprint:
            raise ProductionStoryboardError("planning_semantic_fingerprint")
        return result


@dataclass(frozen=True, slots=True)
class ProductionStoryboardAdmissionV2(ProductionStoryboardAdmissionV1):
    semantic_authority: ProductionSemanticAuthorityV1 = dataclass_field(
        default_factory=ProductionSemanticAuthorityV1, kw_only=True
    )

    def __post_init__(self) -> None:
        ProductionStoryboardAdmissionV1.__post_init__(self)
        if type(self.semantic_authority) is not ProductionSemanticAuthorityV1:
            raise ProductionStoryboardError("admission_semantic_authority")
        if (
            any(
                span.end_milliseconds > self.shots[-1].end_milliseconds
                for span in self.semantic_authority.timed_spans
            )
            or any(
                end > self.shots[-1].end_milliseconds
                for _, end in self.semantic_authority.forbidden_cut_intervals_milliseconds
            )
            or any(
                cut >= self.shots[-1].end_milliseconds
                for cut in self.semantic_authority.required_cut_milliseconds
            )
        ):
            raise ProductionStoryboardError("admission_semantic_time_domain")

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        wire = ProductionStoryboardAdmissionV1.to_wire(self, include_fingerprint=False)
        wire["schema"] = STORYBOARD_ADMISSION_V2_SCHEMA
        wire["semantic_authority"] = self.semantic_authority.to_wire()
        if include_fingerprint:
            wire["fingerprint"] = self.fingerprint
        return wire

    @classmethod
    def from_wire(cls, value: object) -> ProductionStoryboardAdmissionV2:
        wire, base = _read_versioned_base(
            value,
            STORYBOARD_ADMISSION_V2_SCHEMA,
            STORYBOARD_ADMISSION_SCHEMA,
            frozenset({"semantic_authority"}),
        )
        result = cls(
            **_inherited_values(ProductionStoryboardAdmissionV1.from_wire(base)),
            semantic_authority=ProductionSemanticAuthorityV1.from_wire(wire["semantic_authority"]),
        )
        if wire["fingerprint"] != result.fingerprint:
            raise ProductionStoryboardError("admission_semantic_fingerprint")
        return result


@dataclass(frozen=True, slots=True)
class SegmentationProposalV2(SegmentationProposalV1):
    semantic_authority: ProductionSemanticAuthorityV1 = dataclass_field(kw_only=True)
    semantic_slices: tuple[ProductionSemanticSliceV1, ...] = dataclass_field(kw_only=True)
    semantic_census: SemanticCensusV1 = dataclass_field(kw_only=True)
    cut_constraints: SegmentCutConstraintsV1 = dataclass_field(kw_only=True)
    source_shots: tuple[StoryboardShotV1, ...] = dataclass_field(kw_only=True)
    hard_constraints: tuple[str, ...] = dataclass_field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        SegmentationProposalV1.__post_init__(self)
        if type(self.semantic_authority) is not ProductionSemanticAuthorityV1:
            raise ProductionStoryboardError("proposal_semantic_authority")
        if type(self.cut_constraints) is not SegmentCutConstraintsV1:
            raise ProductionStoryboardError("proposal_cut_constraints")
        _validate_coverage(self.source_shots, self.duration_plan.intent.target_seconds * 1000)
        forbidden = {
            (shot.start_milliseconds, shot.end_milliseconds)
            for shot in self.source_shots
            if shot.hard_boundary or shot.exact_dialogue or shot.visible_text
        }
        forbidden.update(
            (span.start_milliseconds, span.end_milliseconds)
            for span in self.semantic_authority.timed_spans
            if span.split_policy.value == "immutable"
        )
        forbidden.update(self.semantic_authority.forbidden_cut_intervals_milliseconds)
        if self.cut_constraints != SegmentCutConstraintsV1(
            self.semantic_authority.required_cut_milliseconds, tuple(sorted(forbidden))
        ):
            raise ProductionStoryboardError("proposal_semantic_constraint_alignment")
        if (
            solve_segment_duration_plan(self.duration_plan.intent, constraints=self.cut_constraints)
            != self.duration_plan
        ):
            raise ProductionStoryboardError("proposal_semantic_partition_alignment")
        if type(self.semantic_slices) is not tuple or len(self.semantic_slices) != len(
            self.segments
        ):
            raise ProductionStoryboardError("proposal_semantic_slices")
        for segment, semantic_slice in zip(self.segments, self.semantic_slices, strict=True):
            if type(semantic_slice) is not ProductionSemanticSliceV1 or (
                semantic_slice.segment_id != segment.segment_id
                or semantic_slice.global_start_milliseconds != segment.global_start_milliseconds
                or semantic_slice.global_end_milliseconds != segment.global_end_milliseconds
                or semantic_slice.task_mode is not segment.task_mode
                or semantic_slice.asset_ids != segment.asset_ids
            ):
                raise ProductionStoryboardError("proposal_semantic_slice_alignment")
            assigned = tuple(
                shot
                for shot in self.source_shots
                if shot.start_milliseconds < segment.global_end_milliseconds
                and shot.end_milliseconds > segment.global_start_milliseconds
            )
            if tuple(shot.shot_id for shot in assigned) != segment.assigned_shot_ids:
                raise ProductionStoryboardError("proposal_semantic_shot_alignment")
            for shot, mapping in zip(assigned, segment.shot_mappings, strict=True):
                if (
                    mapping.global_shot_id != shot.shot_id
                    or mapping.global_start_milliseconds
                    != max(shot.start_milliseconds, segment.global_start_milliseconds)
                    or mapping.global_end_milliseconds
                    != min(shot.end_milliseconds, segment.global_end_milliseconds)
                ):
                    raise ProductionStoryboardError("proposal_semantic_shot_mapping")
            rendered = render_production_segment_prompt(
                semantic_slice,
                tuple(
                    ProductionLocalShotV1(
                        mapping.local_shot_id,
                        index,
                        mapping.local_start_milliseconds,
                        shot.text,
                        shot.exact_dialogue,
                        shot.visible_text,
                    )
                    for index, (shot, mapping) in enumerate(
                        zip(assigned, segment.shot_mappings, strict=True), 1
                    )
                ),
                section_heading=(
                    "detailed_description"
                    if segment.task_mode is TaskMode.REF2VA
                    else "integrated_multimodal_description"
                ),
                clip_duration_seconds=_clip_duration_seconds(segment.duration),
                hard_constraints=self.hard_constraints,
            )
            if rendered.text != segment.local_prompt:
                raise ProductionStoryboardError("proposal_semantic_render_mismatch")
        # CRITICAL: a self-consistent outer hash cannot prove that every source field survived.
        # Recompute the complete source-to-local census before allowing current proposal authority.
        expected = assert_complete_semantic_census(self.semantic_authority, self.semantic_slices)
        if type(self.semantic_census) is not SemanticCensusV1 or self.semantic_census != expected:
            raise ProductionStoryboardError("proposal_semantic_census")

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        wire = SegmentationProposalV1.to_wire(self, include_fingerprint=False)
        wire["schema"] = SEGMENTATION_PROPOSAL_V2_SCHEMA
        wire.update(
            semantic_authority=self.semantic_authority.to_wire(),
            semantic_slices=[item.to_wire() for item in self.semantic_slices],
            semantic_census=self.semantic_census.to_wire(),
            cut_constraints=self.cut_constraints.to_wire(),
            source_shots=[item.to_wire() for item in self.source_shots],
            hard_constraints=list(self.hard_constraints),
        )
        if include_fingerprint:
            wire["fingerprint"] = self.fingerprint
        return wire

    @classmethod
    def from_wire(cls, value: object) -> SegmentationProposalV2:
        wire, base = _read_versioned_base(
            value,
            SEGMENTATION_PROPOSAL_V2_SCHEMA,
            SEGMENTATION_PROPOSAL_SCHEMA,
            frozenset(
                {
                    "semantic_authority",
                    "semantic_slices",
                    "semantic_census",
                    "cut_constraints",
                    "source_shots",
                    "hard_constraints",
                }
            ),
        )
        slices = _wire_tuple(
            wire["semantic_slices"], "proposal_semantic_slices", MAX_PRODUCTION_SEGMENTS
        )
        result = cls(
            **_inherited_values(SegmentationProposalV1.from_wire(base)),
            semantic_authority=ProductionSemanticAuthorityV1.from_wire(wire["semantic_authority"]),
            semantic_slices=tuple(ProductionSemanticSliceV1.from_wire(item) for item in slices),
            semantic_census=SemanticCensusV1.from_wire(wire["semantic_census"]),
            cut_constraints=SegmentCutConstraintsV1.from_wire(wire["cut_constraints"]),
            source_shots=tuple(
                StoryboardShotV1.from_wire(item)
                for item in _wire_tuple(
                    wire["source_shots"], "proposal_source_shots", MAX_STORYBOARD_SHOTS
                )
            ),
            hard_constraints=tuple(
                cast(str, item)
                for item in _wire_tuple(
                    wire["hard_constraints"], "proposal_hard_constraints", MAX_IDS
                )
            ),
        )
        if wire["fingerprint"] != result.fingerprint:
            raise ProductionStoryboardError("proposal_semantic_fingerprint")
        return result


def validate_current_segmentation_proposal(
    context: ProductionPlanningContextV2, proposal: SegmentationProposalV2
) -> None:
    """Join current proposal content to retained context before any automatic import effect."""
    if (
        type(context) is not ProductionPlanningContextV2
        or type(proposal) is not SegmentationProposalV2
    ):
        raise ProductionStoryboardError("current_proposal_version")
    if (
        proposal.planning_context_fingerprint != context.fingerprint
        or proposal.duration_intent_fingerprint != context.duration_intent_fingerprint
        or proposal.hard_constraints != context.hard_constraints
    ):
        raise ProductionStoryboardError("current_proposal_source")
    _validate_shot_references(context, proposal.source_shots)
    source = bind_canonical_semantic_references(context.semantic_authority)
    if (
        merge_semantic_authorities(source, proposal.semantic_authority)
        != proposal.semantic_authority
    ):
        raise ProductionStoryboardError("current_proposal_semantic_source")
    validate_semantic_references(
        proposal.semantic_authority,
        subject_ids=context.subject_ids,
        asset_ids=tuple(item.asset_id for item in context.assets),
    )
    for segment in proposal.segments:
        mode, assets, _ = _task_assets(context, segment.ordinal, len(proposal.segments))
        if segment.task_mode is not mode or segment.asset_ids != assets:
            raise ProductionStoryboardError("current_proposal_task_assets")


__all__ = [
    "validate_current_segmentation_proposal",
    "ProductionPlanningContextV2",
    "ProductionStoryboardAdmissionV2",
    "SegmentationProposalV2",
    "MAX_SERVICE_ENTRIES",
    "MAX_STORYBOARD_SHOTS",
    "ManagedExecutionQualificationV1",
    "PlanningAssetV1",
    "ProductionPlanningContextV1",
    "ProductionStoryboardAdmissionRequestV1",
    "ProductionStoryboardAdmissionService",
    "ProductionStoryboardAdmissionV1",
    "ProductionStoryboardError",
    "ProposalBlockerCodeV1",
    "ProposalSegmentV1",
    "SegmentShotMappingV1",
    "SegmentationProposalRefusalCodeV1",
    "SegmentationProposalRefusalV1",
    "SegmentationProposalV1",
    "StoryboardAdmissionRefusalCodeV1",
    "StoryboardAdmissionRefusalV1",
    "StoryboardShotV1",
    "StoryboardSourceKindV1",
    "admit_production_storyboard",
    "build_segmentation_proposal",
    "fingerprint_prompt_text",
]
