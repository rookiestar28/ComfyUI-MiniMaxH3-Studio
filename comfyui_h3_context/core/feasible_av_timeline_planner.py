"""Deterministic, frame-grid-aligned audiovisual timeline planning.

The planner consumes an accepted M13-05 mode/retention report and caller-owned typed events.  It
does not inspect media or ask a model to fill a gap.  Its output is a bounded structural hand-off
for later rendering: half-open shots cover the target duration, reference anchors remain ordered,
and exact text/audio continuity is explicit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .constraints import TimePoint
from .contracts import TaskMode, ValidationDiagnostic, ValidationSeverity
from .errors import FeasibleAVTimelineError
from .registry import ReferenceRegistry
from .task_mode_retention_classifier import (
    TaskModeRetentionReport,
    TaskModeRetentionStatus,
)

FEASIBLE_AV_TIMELINE_SCHEMA = "h3.feasible_av_timeline_planner.v1"
MAX_PLANNER_EVENTS = 512
MAX_PLANNER_SHOTS = 128
MAX_PLANNER_ANCHORS = 128
MAX_PLANNER_RELATIONS = 256
MAX_PLANNER_IDS = 256
MAX_PLANNER_TEXT = 65_536
MAX_PLANNER_OUTPUT_BYTES = 262_144
_FIXED_24_REJECT_FRAME_EPSILON = Decimal("0.000000000001")

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
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


class TimelinePlannerStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CONFLICTING = "conflicting"


class FrameRoundingPolicy(str, Enum):
    REJECT = "reject"
    FLOOR = "floor"
    CEIL = "ceil"
    NEAREST = "nearest"


class LipState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class PlannerEventKind(str, Enum):
    SHOT = "shot"
    ACTION = "action"
    STATE = "state"
    CAMERA = "camera"
    DIALOGUE = "dialogue"
    VISIBLE_TEXT = "visible_text"
    LIP_STATE = "lip_state"
    AMBIENCE = "ambience"
    SFX = "sfx"
    MUSIC = "music"
    SOURCE_AUDIO = "source_audio"
    TRANSITION = "transition"
    REFERENCE = "reference"


class PlannerRelationKind(str, Enum):
    CONTINUES_ACROSS_CUT = "continues_across_cut"
    AUDIO_CROSS_SHOT = "audio_cross_shot"
    BEFORE = "before"
    AFTER = "after"


class ReferenceAnchorKind(str, Enum):
    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    REFERENCE = "reference"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise FeasibleAVTimelineError(f"{field_name} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise FeasibleAVTimelineError(f"{field_name} contains sensitive or locator material")
    return value


def _text(value: object, field_name: str, maximum: int = MAX_PLANNER_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise FeasibleAVTimelineError(f"{field_name} must be bounded non-empty text")
    if any(marker in value.casefold() for marker in _SENSITIVE):
        raise FeasibleAVTimelineError(f"{field_name} contains sensitive or locator material")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise FeasibleAVTimelineError(f"{field_name} contains an unsafe wire code point")
    return value


def _ids(values: object, field_name: str, maximum: int = MAX_PLANNER_IDS) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise FeasibleAVTimelineError(f"{field_name} is outside the bounded envelope")
    result = tuple(_identifier(item, f"{field_name} item") for item in values)
    if len(result) != len(set(result)):
        raise FeasibleAVTimelineError(f"{field_name} must not contain duplicates")
    return result


def _enum(value: object, expected: type[Enum], field_name: str) -> Enum:
    if isinstance(value, expected):
        return value
    try:
        return expected(value)
    except (TypeError, ValueError):
        raise FeasibleAVTimelineError(f"{field_name} is unsupported") from None


def _time(value: object, field_name: str) -> TimePoint:
    if not isinstance(value, TimePoint):
        raise FeasibleAVTimelineError(f"{field_name} must be a TimePoint")
    return value


def _decimal(value: Decimal, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise FeasibleAVTimelineError(f"{field_name} must be a finite non-negative Decimal")
    return value


def _time_point(value: Decimal) -> TimePoint:
    return TimePoint.from_text(format(value, "f"))


@dataclass(frozen=True, slots=True)
class FrameGridPolicy:
    """Rational target frame grid and explicit boundary rounding policy."""

    fps_num: int = 24
    fps_den: int = 1
    rounding: FrameRoundingPolicy = FrameRoundingPolicy.REJECT
    max_frames: int = 2_073_600
    schema: str = FEASIBLE_AV_TIMELINE_SCHEMA

    def __post_init__(self) -> None:
        if (
            isinstance(self.fps_num, bool)
            or not isinstance(self.fps_num, int)
            or not 1 <= self.fps_num <= 1_000_000
        ):
            raise FeasibleAVTimelineError("fps_num is outside its finite bound")
        if (
            isinstance(self.fps_den, bool)
            or not isinstance(self.fps_den, int)
            or not 1 <= self.fps_den <= 1_000_000
        ):
            raise FeasibleAVTimelineError("fps_den is outside its finite bound")
        _enum(self.rounding, FrameRoundingPolicy, "rounding policy")
        if (
            isinstance(self.max_frames, bool)
            or not isinstance(self.max_frames, int)
            or not 1 <= self.max_frames <= 10_000_000
        ):
            raise FeasibleAVTimelineError("max_frames is outside its finite bound")
        if self.schema != FEASIBLE_AV_TIMELINE_SCHEMA:
            raise FeasibleAVTimelineError("unsupported frame-grid schema")

    @property
    def frame_duration(self) -> Decimal:
        return Decimal(self.fps_den) / Decimal(self.fps_num)

    @property
    def fps(self) -> Decimal:
        return Decimal(self.fps_num) / Decimal(self.fps_den)

    def frame_count(self, seconds: Decimal) -> Decimal:
        # Keep the rational grid exact; dividing by a repeating Decimal frame duration would make
        # whole-second boundaries such as 3.0 appear fractionally off-grid at the context precision.
        return _decimal(seconds, "seconds") * Decimal(self.fps_num) / Decimal(self.fps_den)

    def is_aligned(self, seconds: Decimal) -> bool:
        frames = self.frame_count(seconds)
        if frames == frames.to_integral_value():
            return True
        # IMPORTANT: accepted normalization serializes exact H3 24 fps durations through a binary
        # float.  Admit only that fixed REJECT boundary; other public grids/policies stay exact.
        if self.fps_num == 24 and self.fps_den == 1 and self.rounding is FrameRoundingPolicy.REJECT:
            nearest = frames.to_integral_value(rounding=ROUND_HALF_UP)
            return abs(frames - nearest) <= _FIXED_24_REJECT_FRAME_EPSILON
        return False

    def snap(self, seconds: Decimal) -> Decimal:
        """Return an aligned boundary, raising for the explicit reject policy."""

        frames = self.frame_count(_decimal(seconds, "seconds"))
        if frames == frames.to_integral_value():
            return seconds
        policy = cast(
            FrameRoundingPolicy, _enum(self.rounding, FrameRoundingPolicy, "rounding policy")
        )
        if policy is FrameRoundingPolicy.REJECT:
            if self.fps_num == 24 and self.fps_den == 1:
                nearest = frames.to_integral_value(rounding=ROUND_HALF_UP)
                if abs(frames - nearest) <= _FIXED_24_REJECT_FRAME_EPSILON:
                    return seconds
            raise FeasibleAVTimelineError("time boundary is not on the frame grid")
        if policy is FrameRoundingPolicy.FLOOR:
            rounded = frames.to_integral_value(rounding=ROUND_FLOOR)
        elif policy is FrameRoundingPolicy.CEIL:
            rounded = frames.to_integral_value(rounding=ROUND_CEILING)
        else:
            rounded = frames.to_integral_value(rounding=ROUND_HALF_UP)
        result = rounded * self.frame_duration
        if result < 0:
            return Decimal("0")
        return result

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "fps_num": self.fps_num,
            "fps_den": self.fps_den,
            "rounding": cast(
                FrameRoundingPolicy, _enum(self.rounding, FrameRoundingPolicy, "rounding policy")
            ).value,
            "max_frames": self.max_frames,
        }


@dataclass(frozen=True, slots=True)
class ReferenceAnchor:
    """A caller-owned reference anchor at a target time."""

    anchor_id: str
    kind: ReferenceAnchorKind | str
    asset_id: str
    at: TimePoint
    hard: bool = True
    schema: str = FEASIBLE_AV_TIMELINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.anchor_id, "anchor_id")
        object.__setattr__(self, "kind", _enum(self.kind, ReferenceAnchorKind, "anchor kind"))
        _identifier(self.asset_id, "anchor asset_id")
        _time(self.at, "anchor at")
        if not isinstance(self.hard, bool):
            raise FeasibleAVTimelineError("anchor hard must be a boolean")
        if self.schema != FEASIBLE_AV_TIMELINE_SCHEMA:
            raise FeasibleAVTimelineError("unsupported anchor schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "anchor_id": self.anchor_id,
            "kind": cast(ReferenceAnchorKind, self.kind).value,
            "asset_id": self.asset_id,
            "at": self.at.to_wire(),
            "hard": self.hard,
        }


@dataclass(frozen=True, slots=True)
class PlannerEvent:
    """One source-owned audiovisual event; text is never rewritten by the planner."""

    event_id: str
    kind: PlannerEventKind | str
    start: TimePoint
    end: TimePoint
    text: str | None = None
    exact: bool = False
    lip_state: LipState | str | None = None
    asset_ids: tuple[str, ...] = ()
    hard: bool = False
    evidence_ids: tuple[str, ...] = ()
    schema: str = FEASIBLE_AV_TIMELINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        object.__setattr__(self, "kind", _enum(self.kind, PlannerEventKind, "event kind"))
        _time(self.start, "event start")
        _time(self.end, "event end")
        if self.end.seconds <= self.start.seconds:
            raise FeasibleAVTimelineError("event end must be after start")
        if self.text is not None:
            _text(self.text, "event text")
        if not isinstance(self.exact, bool) or not isinstance(self.hard, bool):
            raise FeasibleAVTimelineError("event exact/hard must be booleans")
        if self.exact and self.text is None:
            raise FeasibleAVTimelineError("an exact event requires text")
        if self.lip_state is not None:
            object.__setattr__(self, "lip_state", _enum(self.lip_state, LipState, "lip state"))
        object.__setattr__(self, "asset_ids", _ids(self.asset_ids, "event asset_ids"))
        object.__setattr__(self, "evidence_ids", _ids(self.evidence_ids, "event evidence_ids"))
        if self.schema != FEASIBLE_AV_TIMELINE_SCHEMA:
            raise FeasibleAVTimelineError("unsupported event schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "kind": cast(PlannerEventKind, self.kind).value,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "text": self.text,
            "exact": self.exact,
            "lip_state": None if self.lip_state is None else cast(LipState, self.lip_state).value,
            "asset_ids": list(self.asset_ids),
            "hard": self.hard,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class PlannerRelation:
    """An explicit relation needed to preserve cross-shot continuity."""

    relation_id: str
    kind: PlannerRelationKind | str
    source_event_ids: tuple[str, ...]
    target_event_ids: tuple[str, ...]
    schema: str = FEASIBLE_AV_TIMELINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.relation_id, "relation_id")
        object.__setattr__(self, "kind", _enum(self.kind, PlannerRelationKind, "relation kind"))
        object.__setattr__(
            self, "source_event_ids", _ids(self.source_event_ids, "source_event_ids")
        )
        object.__setattr__(
            self, "target_event_ids", _ids(self.target_event_ids, "target_event_ids")
        )
        if not self.source_event_ids or not self.target_event_ids:
            raise FeasibleAVTimelineError("relation must have source and target event IDs")
        if self.schema != FEASIBLE_AV_TIMELINE_SCHEMA:
            raise FeasibleAVTimelineError("unsupported relation schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "relation_id": self.relation_id,
            "kind": cast(PlannerRelationKind, self.kind).value,
            "source_event_ids": list(self.source_event_ids),
            "target_event_ids": list(self.target_event_ids),
        }


@dataclass(frozen=True, slots=True)
class ShotDraft:
    """Caller-declared shot bounds; the planner adds cut/anchor/event ownership."""

    shot_id: str
    start: TimePoint
    end: TimePoint
    schema: str = FEASIBLE_AV_TIMELINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.shot_id, "shot_id")
        _time(self.start, "shot start")
        _time(self.end, "shot end")
        if self.end.seconds <= self.start.seconds:
            raise FeasibleAVTimelineError("shot end must be after start")
        if self.schema != FEASIBLE_AV_TIMELINE_SCHEMA:
            raise FeasibleAVTimelineError("unsupported shot schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "shot_id": self.shot_id,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class PlannedShot:
    shot_id: str
    start: TimePoint
    end: TimePoint
    cut_time: TimePoint | None
    anchor_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    source_asset_ids: tuple[str, ...] = ()
    schema: str = FEASIBLE_AV_TIMELINE_SCHEMA

    def __post_init__(self) -> None:
        ShotDraft(self.shot_id, self.start, self.end, self.schema)
        if self.cut_time is not None:
            _time(self.cut_time, "shot cut_time")
            if self.cut_time.seconds != self.start.seconds:
                raise FeasibleAVTimelineError("cut_time must equal a later shot start")
        object.__setattr__(self, "anchor_ids", _ids(self.anchor_ids, "shot anchor_ids"))
        object.__setattr__(self, "event_ids", _ids(self.event_ids, "shot event_ids"))
        object.__setattr__(
            self, "source_asset_ids", _ids(self.source_asset_ids, "shot source_asset_ids")
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "shot_id": self.shot_id,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "cut_time": None if self.cut_time is None else self.cut_time.to_wire(),
            "anchor_ids": list(self.anchor_ids),
            "event_ids": list(self.event_ids),
            "source_asset_ids": list(self.source_asset_ids),
        }


@dataclass(frozen=True, slots=True)
class FeasibleAVTimelineRequest:
    """Explicit input envelope for the pure feasible timeline planner."""

    mode_report: TaskModeRetentionReport
    effective_duration: TimePoint
    frame_grid: FrameGridPolicy
    reference_registry: ReferenceRegistry = field(default_factory=ReferenceRegistry.empty)
    events: tuple[PlannerEvent, ...] = ()
    anchors: tuple[ReferenceAnchor, ...] = ()
    shots: tuple[ShotDraft, ...] = ()
    relations: tuple[PlannerRelation, ...] = ()
    reference_order: tuple[str, ...] = ()
    schema: str = FEASIBLE_AV_TIMELINE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.mode_report, TaskModeRetentionReport):
            raise FeasibleAVTimelineError("mode_report must be a TaskModeRetentionReport")
        _time(self.effective_duration, "effective_duration")
        if self.effective_duration.seconds <= 0:
            raise FeasibleAVTimelineError("effective_duration must be positive")
        if not isinstance(self.frame_grid, FrameGridPolicy):
            raise FeasibleAVTimelineError("frame_grid must be a FrameGridPolicy")
        if not isinstance(self.reference_registry, ReferenceRegistry):
            raise FeasibleAVTimelineError("reference_registry must be a ReferenceRegistry")
        if (
            canonical_fingerprint(self.reference_registry.to_wire())
            != self.mode_report.registry_fingerprint
        ):
            raise FeasibleAVTimelineError("reference_registry does not match the M13-05 report")
        for values, expected, field_name, maximum in (
            (self.events, PlannerEvent, "events", MAX_PLANNER_EVENTS),
            (self.anchors, ReferenceAnchor, "anchors", MAX_PLANNER_ANCHORS),
            (self.shots, ShotDraft, "shots", MAX_PLANNER_SHOTS),
            (self.relations, PlannerRelation, "relations", MAX_PLANNER_RELATIONS),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > maximum
                or not all(isinstance(item, expected) for item in values)
            ):
                raise FeasibleAVTimelineError(f"{field_name} is outside the bounded envelope")
            identifiers = tuple(
                getattr(
                    item,
                    "event_id",
                    getattr(
                        item,
                        "anchor_id",
                        getattr(item, "shot_id", getattr(item, "relation_id", "")),
                    ),
                )
                for item in values
            )
            if len(identifiers) != len(set(identifiers)):
                raise FeasibleAVTimelineError(f"{field_name} contain duplicate IDs")
        reference_ids = tuple(asset.asset_id for asset in self.reference_registry.assets)
        order = self.reference_order or reference_ids
        object.__setattr__(self, "reference_order", _ids(order, "reference_order"))
        if tuple(order) != reference_ids:
            raise FeasibleAVTimelineError("reference_order must preserve registry connection order")
        if self.schema != FEASIBLE_AV_TIMELINE_SCHEMA:
            raise FeasibleAVTimelineError("unsupported planner request schema")

    @property
    def mode_report_registry(self) -> ReferenceRegistry:
        """Compatibility alias for the explicit request registry."""

        return self.reference_registry

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "mode_report": self.mode_report.to_wire(),
            "effective_duration": self.effective_duration.to_wire(),
            "frame_grid": self.frame_grid.to_wire(),
            "reference_registry": self.reference_registry.to_wire(),
            "events": [item.to_wire() for item in self.events],
            "anchors": [item.to_wire() for item in self.anchors],
            "shots": [item.to_wire() for item in self.shots],
            "relations": [item.to_wire() for item in self.relations],
            "reference_order": list(self.reference_order),
        }


@dataclass(frozen=True, slots=True)
class FeasibleAVTimelinePlan:
    status: TimelinePlannerStatus
    task_mode: TaskMode
    effective_duration: TimePoint
    frame_grid: FrameGridPolicy
    shots: tuple[PlannedShot, ...]
    events: tuple[PlannerEvent, ...]
    anchors: tuple[ReferenceAnchor, ...]
    relations: tuple[PlannerRelation, ...]
    reference_order: tuple[str, ...]
    mode_report_fingerprint: str
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    limitations: tuple[ValidationDiagnostic, ...] = ()
    plan_fingerprint: str | None = None
    schema: str = FEASIBLE_AV_TIMELINE_SCHEMA

    def __post_init__(self) -> None:
        self_status = _enum(self.status, TimelinePlannerStatus, "plan status")
        object.__setattr__(self, "status", self_status)
        if not isinstance(self.task_mode, TaskMode):
            raise FeasibleAVTimelineError("task_mode must be a TaskMode")
        _time(self.effective_duration, "effective_duration")
        if not isinstance(self.frame_grid, FrameGridPolicy):
            raise FeasibleAVTimelineError("frame_grid must be a FrameGridPolicy")
        if (
            not isinstance(self.shots, tuple)
            or not self.shots
            or not all(isinstance(item, PlannedShot) for item in self.shots)
        ):
            raise FeasibleAVTimelineError("plan shots must be a bounded non-empty tuple")
        for values, expected_type, field_name, maximum in (
            (self.events, PlannerEvent, "events", MAX_PLANNER_EVENTS),
            (self.anchors, ReferenceAnchor, "anchors", MAX_PLANNER_ANCHORS),
            (self.relations, PlannerRelation, "relations", MAX_PLANNER_RELATIONS),
            (self.diagnostics, ValidationDiagnostic, "diagnostics", MAX_PLANNER_EVENTS),
            (self.limitations, ValidationDiagnostic, "limitations", MAX_PLANNER_EVENTS),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > maximum
                or not all(isinstance(item, expected_type) for item in values)
            ):
                raise FeasibleAVTimelineError(f"plan {field_name} is outside the bounded envelope")
        object.__setattr__(self, "reference_order", _ids(self.reference_order, "reference_order"))
        if not isinstance(
            self.mode_report_fingerprint, str
        ) or not self.mode_report_fingerprint.startswith("sha256:"):
            raise FeasibleAVTimelineError("mode_report_fingerprint must be a SHA-256 fingerprint")
        if self.schema != FEASIBLE_AV_TIMELINE_SCHEMA:
            raise FeasibleAVTimelineError("unsupported planner plan schema")
        expected_fingerprint = canonical_fingerprint(self.to_wire(include_fingerprint=False))
        if self.plan_fingerprint is None:
            object.__setattr__(self, "plan_fingerprint", expected_fingerprint)
        elif self.plan_fingerprint != expected_fingerprint:
            raise FeasibleAVTimelineError("plan fingerprint does not match contents")
        if len(self.to_wire_bytes()) > MAX_PLANNER_OUTPUT_BYTES:
            raise FeasibleAVTimelineError("planner plan exceeds output limit")

    @property
    def fingerprint(self) -> str:
        if self.plan_fingerprint is None:  # pragma: no cover
            raise FeasibleAVTimelineError("plan fingerprint is not initialized")
        return self.plan_fingerprint

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "status": self.status.value,
            "task_mode": self.task_mode.value,
            "effective_duration": self.effective_duration.to_wire(),
            "frame_grid": self.frame_grid.to_wire(),
            "shots": [item.to_wire() for item in self.shots],
            "events": [item.to_wire() for item in self.events],
            "anchors": [item.to_wire() for item in self.anchors],
            "relations": [item.to_wire() for item in self.relations],
            "reference_order": list(self.reference_order),
            "mode_report_fingerprint": self.mode_report_fingerprint,
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
class FeasibleAVTimelineResult:
    status: TimelinePlannerStatus
    plan: FeasibleAVTimelinePlan | None
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    limitations: tuple[ValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", _enum(self.status, TimelinePlannerStatus, "result status")
        )
        if self.plan is not None and not isinstance(self.plan, FeasibleAVTimelinePlan):
            raise FeasibleAVTimelineError("result plan must be a FeasibleAVTimelinePlan or None")
        for values, field_name in (
            (self.diagnostics, "diagnostics"),
            (self.limitations, "limitations"),
        ):
            if not isinstance(values, tuple) or not all(
                isinstance(item, ValidationDiagnostic) for item in values
            ):
                raise FeasibleAVTimelineError(f"result {field_name} are invalid")

    @property
    def is_valid(self) -> bool:
        return self.plan is not None and self.status in {
            TimelinePlannerStatus.COMPLETE,
            TimelinePlannerStatus.PARTIAL,
        }

    def to_wire(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "plan": None if self.plan is None else self.plan.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "limitations": [item.to_wire() for item in self.limitations],
        }


def _diagnostic(
    code: str, message: str, severity: ValidationSeverity = ValidationSeverity.ERROR
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, "feasible_av_timeline")


def _blocked(
    status: TimelinePlannerStatus, diagnostics: list[ValidationDiagnostic]
) -> FeasibleAVTimelineResult:
    return FeasibleAVTimelineResult(status, None, tuple(diagnostics))


def _normalise_event(
    event: PlannerEvent,
    grid: FrameGridPolicy,
    duration: Decimal,
    diagnostics: list[ValidationDiagnostic],
) -> PlannerEvent | None:
    start = event.start.seconds
    end = event.end.seconds
    if start < 0 or end > duration or end <= start:
        diagnostics.append(
            _diagnostic(
                "event_outside_timeline",
                f"event {event.event_id!r} lies outside effective duration",
            )
        )
        return None
    if not grid.is_aligned(start) or not grid.is_aligned(end):
        if event.exact or event.hard or grid.rounding is FrameRoundingPolicy.REJECT:
            diagnostics.append(
                _diagnostic(
                    "hard_event_off_frame_grid",
                    f"event {event.event_id!r} is not frame-grid aligned",
                )
            )
            return None
        try:
            snapped_start = grid.snap(start)
            snapped_end = grid.snap(end)
        except FeasibleAVTimelineError:
            diagnostics.append(
                _diagnostic("event_off_frame_grid", f"event {event.event_id!r} cannot be aligned")
            )
            return None
        if snapped_end <= snapped_start:
            diagnostics.append(
                _diagnostic(
                    "event_collapsed_on_frame_grid",
                    f"event {event.event_id!r} collapses after snapping",
                )
            )
            return None
        return replace(event, start=_time_point(snapped_start), end=_time_point(snapped_end))
    return event


def _validate_shots(
    request: FeasibleAVTimelineRequest, diagnostics: list[ValidationDiagnostic]
) -> tuple[ShotDraft, ...] | None:
    duration = request.effective_duration.seconds
    shots = request.shots or (
        ShotDraft("shot.1", TimePoint.from_text("0"), request.effective_duration),
    )
    previous: ShotDraft | None = None
    for shot in shots:
        if shot.start.seconds < 0 or shot.end.seconds > duration:
            diagnostics.append(
                _diagnostic(
                    "shot_outside_timeline",
                    f"shot {shot.shot_id!r} lies outside effective duration",
                )
            )
        if not request.frame_grid.is_aligned(
            shot.start.seconds
        ) or not request.frame_grid.is_aligned(shot.end.seconds):
            diagnostics.append(
                _diagnostic(
                    "shot_boundary_off_frame_grid",
                    f"shot {shot.shot_id!r} is not frame-grid aligned",
                )
            )
        if previous is not None:
            if shot.start.seconds < previous.end.seconds:
                diagnostics.append(
                    _diagnostic(
                        "shot_overlap", f"shot {shot.shot_id!r} overlaps {previous.shot_id!r}"
                    )
                )
            elif shot.start.seconds > previous.end.seconds:
                diagnostics.append(
                    _diagnostic("shot_gap", f"shots leave a gap before {shot.shot_id!r}")
                )
        previous = shot
    if shots[0].start.seconds != 0:
        diagnostics.append(_diagnostic("timeline_not_start_zero", "first shot must start at zero"))
    if shots[-1].end.seconds != duration:
        diagnostics.append(
            _diagnostic("timeline_not_end_duration", "last shot must end at effective duration")
        )
    return shots


def _validate_anchors(
    request: FeasibleAVTimelineRequest, diagnostics: list[ValidationDiagnostic]
) -> None:
    mode = request.mode_report.mode.mode
    duration = request.effective_duration.seconds
    anchors = request.anchors
    by_kind: dict[ReferenceAnchorKind, list[ReferenceAnchor]] = {
        kind: [] for kind in ReferenceAnchorKind
    }
    for anchor in anchors:
        kind = cast(ReferenceAnchorKind, anchor.kind)
        by_kind[kind].append(anchor)
        if anchor.at.seconds > duration:
            diagnostics.append(
                _diagnostic(
                    "anchor_outside_timeline",
                    f"anchor {anchor.anchor_id!r} lies outside effective duration",
                )
            )
        if not request.frame_grid.is_aligned(anchor.at.seconds):
            diagnostics.append(
                _diagnostic(
                    "anchor_off_frame_grid",
                    f"anchor {anchor.anchor_id!r} is not frame-grid aligned",
                )
            )
    if mode is TaskMode.T2VA and any(
        by_kind[ReferenceAnchorKind.FIRST_FRAME] or by_kind[ReferenceAnchorKind.LAST_FRAME]
        for _ in (0,)
    ):
        diagnostics.append(
            _diagnostic(
                "unexpected_keyframe_anchor", "T2VA cannot carry first/last-frame anchor semantics"
            )
        )
    if mode in {TaskMode.I2VA, TaskMode.FL2VA}:
        if len(by_kind[ReferenceAnchorKind.FIRST_FRAME]) != 1:
            diagnostics.append(
                _diagnostic(
                    "missing_first_frame_anchor", "mode requires exactly one first-frame anchor"
                )
            )
        elif by_kind[ReferenceAnchorKind.FIRST_FRAME][0].at.seconds != 0:
            diagnostics.append(
                _diagnostic("anchor_not_at_start", "first-frame anchor must be at zero")
            )
    if mode is TaskMode.FL2VA:
        if len(by_kind[ReferenceAnchorKind.LAST_FRAME]) != 1:
            diagnostics.append(
                _diagnostic(
                    "missing_last_frame_anchor", "FL2VA requires exactly one last-frame anchor"
                )
            )
        elif by_kind[ReferenceAnchorKind.LAST_FRAME][0].at.seconds != duration:
            diagnostics.append(
                _diagnostic("anchor_not_at_end", "last-frame anchor must be at effective duration")
            )
    if mode is TaskMode.L2VA:
        if len(by_kind[ReferenceAnchorKind.LAST_FRAME]) != 1:
            diagnostics.append(
                _diagnostic(
                    "missing_last_frame_anchor", "L2VA requires exactly one last-frame anchor"
                )
            )
        elif by_kind[ReferenceAnchorKind.LAST_FRAME][0].at.seconds != duration:
            diagnostics.append(
                _diagnostic("anchor_not_at_end", "last-frame anchor must be at effective duration")
            )


def _continuation_ids(relations: tuple[PlannerRelation, ...]) -> set[str]:
    result: set[str] = set()
    for relation in relations:
        if cast(PlannerRelationKind, relation.kind) in {
            PlannerRelationKind.CONTINUES_ACROSS_CUT,
            PlannerRelationKind.AUDIO_CROSS_SHOT,
        }:
            result.update(relation.source_event_ids)
            result.update(relation.target_event_ids)
    return result


def build_feasible_av_timeline(request: FeasibleAVTimelineRequest) -> FeasibleAVTimelineResult:
    """Build a contiguous, mode-aware timeline or return typed blocking diagnostics."""

    if not isinstance(request, FeasibleAVTimelineRequest):
        raise FeasibleAVTimelineError("request must be a FeasibleAVTimelineRequest")
    diagnostics: list[ValidationDiagnostic] = []
    limitations: list[ValidationDiagnostic] = []
    mode_report = request.mode_report
    registry_ids = {asset.asset_id for asset in request.reference_registry.assets}
    asset_roles = {asset.asset_id: asset.role.value for asset in request.reference_registry.assets}
    for anchor in request.anchors:
        if anchor.asset_id not in registry_ids:
            diagnostics.append(
                _diagnostic(
                    "anchor_asset_unowned",
                    f"anchor {anchor.anchor_id!r} references an unowned asset",
                )
            )
        elif (
            anchor.kind is ReferenceAnchorKind.FIRST_FRAME
            and asset_roles[anchor.asset_id] != "first_frame"
        ):
            diagnostics.append(
                _diagnostic(
                    "anchor_role_mismatch",
                    f"anchor {anchor.anchor_id!r} is not owned by a first-frame asset",
                )
            )
        elif (
            anchor.kind is ReferenceAnchorKind.LAST_FRAME
            and asset_roles[anchor.asset_id] != "last_frame"
        ):
            diagnostics.append(
                _diagnostic(
                    "anchor_role_mismatch",
                    f"anchor {anchor.anchor_id!r} is not owned by a last-frame asset",
                )
            )
    for event in request.events:
        if not set(event.asset_ids).issubset(registry_ids):
            diagnostics.append(
                _diagnostic(
                    "event_asset_unowned", f"event {event.event_id!r} references an unowned asset"
                )
            )
    if mode_report.mode.disposition.value != "accepted":
        diagnostics.append(_diagnostic("mode_not_accepted", "M13-05 mode decision is not accepted"))
    if mode_report.status in {TaskModeRetentionStatus.BLOCKED, TaskModeRetentionStatus.CONFLICTING}:
        diagnostics.append(
            _diagnostic("classifier_blocked", "M13-05 report blocks timeline planning")
        )
        return _blocked(
            TimelinePlannerStatus.CONFLICTING
            if mode_report.status is TaskModeRetentionStatus.CONFLICTING
            else TimelinePlannerStatus.BLOCKED,
            diagnostics,
        )
    if not request.frame_grid.is_aligned(request.effective_duration.seconds):
        diagnostics.append(
            _diagnostic("duration_off_frame_grid", "effective duration is not frame-grid aligned")
        )
    shots = _validate_shots(request, diagnostics)
    _validate_anchors(request, diagnostics)
    event_ids = {event.event_id for event in request.events}
    for relation in request.relations:
        if not set(relation.source_event_ids + relation.target_event_ids).issubset(event_ids):
            diagnostics.append(
                _diagnostic(
                    "relation_unknown_event",
                    f"relation {relation.relation_id!r} references an unknown event",
                )
            )
    normalised_events: list[PlannerEvent] = []
    for event in request.events:
        value = _normalise_event(
            event, request.frame_grid, request.effective_duration.seconds, diagnostics
        )
        if value is not None:
            normalised_events.append(value)
    if shots is None or any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for item in diagnostics
    ):
        return _blocked(TimelinePlannerStatus.BLOCKED, diagnostics)
    continuations = _continuation_ids(request.relations)
    event_shots: dict[str, tuple[str, ...]] = {}
    for event in normalised_events:
        overlapping = tuple(
            shot.shot_id
            for shot in shots
            if shot.start.seconds < event.end.seconds and shot.end.seconds > event.start.seconds
        )
        if not overlapping:
            diagnostics.append(
                _diagnostic(
                    "event_outside_timeline", f"event {event.event_id!r} has no owning shot"
                )
            )
            continue
        event_shots[event.event_id] = overlapping
        if (
            cast(PlannerEventKind, event.kind) is PlannerEventKind.DIALOGUE
            and len(overlapping) > 1
            and event.event_id not in continuations
        ):
            diagnostics.append(
                _diagnostic(
                    "cross_shot_dialogue_without_continuation",
                    f"dialogue {event.event_id!r} crosses a cut without an explicit "
                    "continuation relation",
                )
            )
    if any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for item in diagnostics
    ):
        return _blocked(TimelinePlannerStatus.BLOCKED, diagnostics)
    anchor_by_shot: dict[str, list[str]] = {shot.shot_id: [] for shot in shots}
    for anchor in request.anchors:
        target = next(
            (shot for shot in shots if shot.start.seconds <= anchor.at.seconds < shot.end.seconds),
            shots[-1] if anchor.at.seconds == request.effective_duration.seconds else None,
        )
        if target is None:
            diagnostics.append(
                _diagnostic("anchor_unowned", f"anchor {anchor.anchor_id!r} has no owning shot")
            )
        else:
            anchor_by_shot[target.shot_id].append(anchor.anchor_id)
    planned_shots: list[PlannedShot] = []
    for index, shot in enumerate(shots):
        owned_events = tuple(
            event_id for event_id, owners in event_shots.items() if shot.shot_id in owners
        )
        assets: list[str] = []
        for event in normalised_events:
            if event.event_id in owned_events:
                assets.extend(event.asset_ids)
        assets.extend(
            anchor.asset_id
            for anchor in request.anchors
            if anchor.anchor_id in anchor_by_shot[shot.shot_id]
        )
        planned_shots.append(
            PlannedShot(
                shot.shot_id,
                shot.start,
                shot.end,
                None if index == 0 else shot.start,
                tuple(anchor_by_shot[shot.shot_id]),
                owned_events,
                tuple(dict.fromkeys(assets)),
            )
        )
    if mode_report.mode.mode is TaskMode.FL2VA:
        last = next(
            (item for item in request.anchors if item.kind is ReferenceAnchorKind.LAST_FRAME), None
        )
        if last is not None and last.anchor_id not in planned_shots[-1].anchor_ids:
            diagnostics.append(
                _diagnostic(
                    "last_frame_not_in_final_shot",
                    "FL2VA last-frame anchor is not owned by the final shot",
                )
            )
    if mode_report.mode.mode is TaskMode.L2VA:
        last = next(
            (item for item in request.anchors if item.kind is ReferenceAnchorKind.LAST_FRAME), None
        )
        if last is not None and last.anchor_id not in planned_shots[-1].anchor_ids:
            diagnostics.append(
                _diagnostic(
                    "last_frame_not_in_final_shot",
                    "L2VA last-frame anchor is not owned by the final shot",
                )
            )
    if any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for item in diagnostics
    ):
        return _blocked(TimelinePlannerStatus.BLOCKED, diagnostics)
    if mode_report.status is TaskModeRetentionStatus.PARTIAL:
        limitations.append(
            _diagnostic(
                "classifier_partial",
                "M13-05 mode/retention evidence is partial",
                ValidationSeverity.WARNING,
            )
        )
    status = TimelinePlannerStatus.PARTIAL if limitations else TimelinePlannerStatus.COMPLETE
    plan = FeasibleAVTimelinePlan(
        status,
        mode_report.mode.mode,
        request.effective_duration,
        request.frame_grid,
        tuple(planned_shots),
        tuple(normalised_events),
        request.anchors,
        request.relations,
        request.reference_order,
        mode_report.fingerprint,
        tuple(diagnostics),
        tuple(limitations),
    )
    return FeasibleAVTimelineResult(status, plan, tuple(diagnostics), tuple(limitations))


# Readability aliases used by later compiler/planner stages.
FeasibleTimelineRequest = FeasibleAVTimelineRequest
FeasibleTimelinePlan = FeasibleAVTimelinePlan
FeasibleTimelineResult = FeasibleAVTimelineResult
build_feasible_timeline = build_feasible_av_timeline


__all__ = [
    "FEASIBLE_AV_TIMELINE_SCHEMA",
    "MAX_PLANNER_EVENTS",
    "MAX_PLANNER_SHOTS",
    "FrameGridPolicy",
    "FrameRoundingPolicy",
    "LipState",
    "PlannerEventKind",
    "PlannerRelationKind",
    "ReferenceAnchorKind",
    "TimelinePlannerStatus",
    "PlannerEvent",
    "PlannerRelation",
    "ReferenceAnchor",
    "ShotDraft",
    "PlannedShot",
    "FeasibleAVTimelineRequest",
    "FeasibleAVTimelinePlan",
    "FeasibleAVTimelineResult",
    "FeasibleTimelineRequest",
    "FeasibleTimelinePlan",
    "FeasibleTimelineResult",
    "build_feasible_av_timeline",
    "build_feasible_timeline",
]
