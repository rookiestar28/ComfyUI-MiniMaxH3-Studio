"""Deterministic multimodal Full-Reference timeline planning.

This module joins already validated request, video, audio, identity, and directive contracts.  It
does not inspect media or infer meaning.  Source spans remain explicit, audio overlap is preserved,
missing visual intervals are visible gap segments, and the resulting canonical ``IntentGraph`` is
the same pure-core hand-off consumed by the existing renderer/validator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum

from .audio_analysis import (
    AudioAnalysisBatch,
    AudioAnalysisStatus,
    AudioObservation,
    AudioObservationKind,
)
from .canonical import binary64_token, canonical_fingerprint
from .constraints import TimePoint
from .context_reporting import ContextPlan, Limitation, PlanStage, PlanStep, PlanStepStatus
from .contracts import (
    MediaKind,
    PromptProfile,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .cross_reference_graph import (
    CrossReferenceEntity,
    CrossReferenceGraph,
    CrossReferenceGraphStatus,
    CrossReferenceResolution,
    ReferenceEntityKind,
)
from .errors import ContractValidationError, FullReferenceTimelineError
from .evidence import EvidenceRecord, EvidenceSet, merge_evidence
from .intent_graph import (
    AudioIntent,
    AudioLayer,
    AudioRetentionMarker,
    CameraIntent,
    EventCopy,
    EventCopyMode,
    IntentAction,
    IntentGraph,
    IntentScene,
    IntentSubject,
    RetentionDomain,
    RetentionRelation,
    RetentionScope,
    TimelineSegment,
    VisualRetentionMarker,
    build_intent_graph,
)
from .normalization import NormalizedContextRequest
from .reference_directives import (
    DirectiveAction,
    DirectiveSetStatus,
    DirectiveTargetKind,
    ResolvedDirectiveSet,
    RetentionAspect,
)
from .reference_window import ReferenceConditioningWindow, reference_conditioning_window
from .registry import ReferenceRegistry
from .video_analysis import VideoAnalysisBatch, VideoAnalysisStatus, VideoObservationKind

FULL_REFERENCE_TIMELINE_SCHEMA = "h3.full_reference.timeline.v1"
MAX_FULL_REFERENCE_SEGMENTS = 256
MAX_FULL_REFERENCE_SPANS = 4096
MAX_FULL_REFERENCE_IDS = 512
MAX_FULL_REFERENCE_TEXT_LENGTH = 4096

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class TimelineTransition(str, Enum):
    """Explicit transition at a target segment boundary."""

    OPENING = "opening"
    CUT = "cut"
    CONTINUOUS = "continuous"
    GAP_FILL = "gap_fill"


class MultimodalTimelineStatus(str, Enum):
    """Plan state; partial evidence is inspectable and conflicting input is non-renderable."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    CONFLICTING = "conflicting"
    EMPTY = "empty"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise FullReferenceTimelineError(f"{field} must be a bounded identifier")
    return value


def _id_tuple(value: object, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise FullReferenceTimelineError(
            f"{field} must be a tuple of at most {maximum} identifiers"
        )
    result = tuple(_identifier(item, f"{field} item") for item in value)
    if len(result) != len(set(result)):
        raise FullReferenceTimelineError(f"{field} must not contain duplicate identifiers")
    return result


def _text(value: object, field: str, maximum: int = MAX_FULL_REFERENCE_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise FullReferenceTimelineError(f"{field} must be a bounded non-empty string")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise FullReferenceTimelineError(f"{field} contains an unsafe wire code point")
    return value


def _time(value: object, field: str) -> TimePoint:
    if not isinstance(value, TimePoint):
        raise FullReferenceTimelineError(f"{field} must be a TimePoint")
    return value


@dataclass(frozen=True, slots=True)
class TimelineSourceSpan:
    """One source span mapped to an explicit target interval."""

    span_id: str
    modality: MediaKind
    asset_id: str
    source_id: str
    source_start: TimePoint
    source_end: TimePoint
    target_start: TimePoint
    target_end: TimePoint
    observation_id: str | None = None
    shot_id: str | None = None
    overlap_group_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.span_id, "span_id")
        if not isinstance(self.modality, MediaKind):
            raise FullReferenceTimelineError("span modality must be a MediaKind")
        _identifier(self.asset_id, "span asset_id")
        _identifier(self.source_id, "span source_id")
        for value, field in (
            (self.source_start, "source_start"),
            (self.source_end, "source_end"),
            (self.target_start, "target_start"),
            (self.target_end, "target_end"),
        ):
            _time(value, field)
        if self.source_end.seconds <= self.source_start.seconds:
            raise FullReferenceTimelineError("source span end must be after start")
        if self.target_end.seconds <= self.target_start.seconds:
            raise FullReferenceTimelineError("target span end must be after start")
        if self.observation_id is None and self.shot_id is None:
            raise FullReferenceTimelineError("source span requires an observation_id or shot_id")
        if self.observation_id is not None:
            _identifier(self.observation_id, "span observation_id")
        if self.shot_id is not None:
            _identifier(self.shot_id, "span shot_id")
        if self.overlap_group_id is not None:
            _identifier(self.overlap_group_id, "span overlap_group_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "span_id": self.span_id,
            "modality": self.modality.value,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "source_start": self.source_start.to_wire(),
            "source_end": self.source_end.to_wire(),
            "target_start": self.target_start.to_wire(),
            "target_end": self.target_end.to_wire(),
            "observation_id": self.observation_id,
            "shot_id": self.shot_id,
            "overlap_group_id": self.overlap_group_id,
        }


@dataclass(frozen=True, slots=True)
class MultimodalTimelineSegment:
    """A target interval with explicit source, entity, directive, and gap ownership."""

    segment_id: str
    start: TimePoint
    end: TimePoint
    transition: TimelineTransition
    source_span_ids: tuple[str, ...] = ()
    source_asset_ids: tuple[str, ...] = ()
    source_shot_ids: tuple[str, ...] = ()
    video_observation_ids: tuple[str, ...] = ()
    audio_observation_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    directive_ids: tuple[str, ...] = ()
    missing_source: bool = False

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "segment_id")
        _time(self.start, "segment start")
        _time(self.end, "segment end")
        if self.end.seconds <= self.start.seconds:
            raise FullReferenceTimelineError("segment end must be after start")
        if not isinstance(self.transition, TimelineTransition):
            raise FullReferenceTimelineError("segment transition must be a TimelineTransition")
        for value, field in (
            (self.source_span_ids, "source_span_ids"),
            (self.source_asset_ids, "source_asset_ids"),
            (self.source_shot_ids, "source_shot_ids"),
            (self.video_observation_ids, "video_observation_ids"),
            (self.audio_observation_ids, "audio_observation_ids"),
            (self.entity_ids, "entity_ids"),
            (self.directive_ids, "directive_ids"),
        ):
            _id_tuple(value, field, MAX_FULL_REFERENCE_IDS)
        if not isinstance(self.missing_source, bool):
            raise FullReferenceTimelineError("missing_source must be a boolean")
        if self.missing_source and self.source_shot_ids:
            raise FullReferenceTimelineError(
                "a missing-source segment cannot carry video shot spans"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "transition": self.transition.value,
            "source_span_ids": list(self.source_span_ids),
            "source_asset_ids": list(self.source_asset_ids),
            "source_shot_ids": list(self.source_shot_ids),
            "video_observation_ids": list(self.video_observation_ids),
            "audio_observation_ids": list(self.audio_observation_ids),
            "entity_ids": list(self.entity_ids),
            "directive_ids": list(self.directive_ids),
            "missing_source": self.missing_source,
        }


@dataclass(frozen=True, slots=True)
class MultimodalTimelineConfig:
    """Finite deterministic planner controls."""

    fill_missing_spans: bool = True
    max_segments: int = MAX_FULL_REFERENCE_SEGMENTS
    max_source_spans: int = MAX_FULL_REFERENCE_SPANS

    def __post_init__(self) -> None:
        if not isinstance(self.fill_missing_spans, bool):
            raise FullReferenceTimelineError("fill_missing_spans must be a boolean")
        if (
            isinstance(self.max_segments, bool)
            or not isinstance(self.max_segments, int)
            or not 1 <= self.max_segments <= MAX_FULL_REFERENCE_SEGMENTS
        ):
            raise FullReferenceTimelineError("max_segments is outside its finite bound")
        if (
            isinstance(self.max_source_spans, bool)
            or not isinstance(self.max_source_spans, int)
            or not 1 <= self.max_source_spans <= MAX_FULL_REFERENCE_SPANS
        ):
            raise FullReferenceTimelineError("max_source_spans is outside its finite bound")

    def to_wire(self) -> dict[str, object]:
        return {
            "fill_missing_spans": self.fill_missing_spans,
            "max_segments": self.max_segments,
            "max_source_spans": self.max_source_spans,
        }


@dataclass(frozen=True, slots=True)
class FullReferenceTimelineRequest:
    """Validated inputs from the five prior Full-Reference boundaries."""

    normalized_request: NormalizedContextRequest
    video_analysis: VideoAnalysisBatch
    audio_analysis: AudioAnalysisBatch | None
    cross_reference_graph: CrossReferenceGraph
    directives: ResolvedDirectiveSet
    config: MultimodalTimelineConfig = MultimodalTimelineConfig()
    conditioning_window: ReferenceConditioningWindow | None = None

    def __post_init__(self) -> None:
        request = self.normalized_request
        if not isinstance(request, NormalizedContextRequest):
            raise FullReferenceTimelineError(
                "normalized_request must be a NormalizedContextRequest"
            )
        if (
            request.task_mode is not TaskMode.REF2VA
            or request.profile.name is not PromptProfile.FULL_REFERENCE
        ):
            raise FullReferenceTimelineError(
                "Full-Reference timeline requires normalized REF2VA request"
            )
        if not isinstance(self.video_analysis, VideoAnalysisBatch):
            raise FullReferenceTimelineError("video_analysis must be a VideoAnalysisBatch")
        if self.audio_analysis is not None and not isinstance(
            self.audio_analysis, AudioAnalysisBatch
        ):
            raise FullReferenceTimelineError("audio_analysis must be an AudioAnalysisBatch or None")
        if not isinstance(self.cross_reference_graph, CrossReferenceGraph):
            raise FullReferenceTimelineError("cross_reference_graph must be a CrossReferenceGraph")
        if not isinstance(self.directives, ResolvedDirectiveSet):
            raise FullReferenceTimelineError("directives must be a ResolvedDirectiveSet")
        if not isinstance(self.config, MultimodalTimelineConfig):
            raise FullReferenceTimelineError("config must be a MultimodalTimelineConfig")
        admitted = self.video_analysis.admitted_frame_count
        if admitted is not None:
            expected_window = reference_conditioning_window(request.effective_frame_count, admitted)
            if self.conditioning_window is None:
                object.__setattr__(self, "conditioning_window", expected_window)
            elif (
                type(self.conditioning_window) is not ReferenceConditioningWindow
                or self.conditioning_window != expected_window
            ):
                raise FullReferenceTimelineError(
                    "perception_window_mismatch: Run visual perception with the same request "
                    "so it reads the frames the generator keeps."
                )
        if self.conditioning_window is not None:
            if type(self.conditioning_window) is not ReferenceConditioningWindow:
                raise FullReferenceTimelineError("conditioning window must be exact")
            self.conditioning_window.__post_init__()
            if (
                admitted is None
                or self.video_analysis.frame_rate is None
                or self.video_analysis.frame_rate <= 0
                or len(self.video_analysis.selected_asset_ids) != 1
                or self.conditioning_window.window_frame_count > admitted
            ):
                raise FullReferenceTimelineError("conditioning window requires one admitted video")
            if (
                self.video_analysis.conditioning_window is not None
                and self.video_analysis.conditioning_window != self.conditioning_window
            ):
                raise FullReferenceTimelineError(
                    "perception_window_mismatch: Run visual perception with the same request "
                    "so it reads the frames the generator keeps."
                )
        assets = {asset.asset_id: asset for asset in request.reference_registry.assets}
        for asset_id in self.video_analysis.selected_asset_ids:
            if asset_id not in assets or assets[asset_id].kind is not MediaKind.VIDEO:
                raise FullReferenceTimelineError(
                    "video analysis selects an unowned/non-video asset"
                )
        if self.audio_analysis is not None:
            for asset_id in self.audio_analysis.selected_asset_ids:
                if asset_id not in assets or assets[asset_id].kind is not MediaKind.AUDIO:
                    raise FullReferenceTimelineError(
                        "audio analysis selects an unowned/non-audio asset"
                    )
        if not set(self.cross_reference_graph.selected_asset_ids).issubset(assets):
            raise FullReferenceTimelineError("cross-reference graph contains an unowned asset")
        available_observation_ids = {
            item.observation_id for item in self.video_analysis.observations
        }
        if self.audio_analysis is not None:
            available_observation_ids.update(
                item.observation_id for item in self.audio_analysis.observations
            )
        graph_observation_ids = {
            observation_id
            for entity in self.cross_reference_graph.entities
            for observation_id in entity.observation_ids
        }
        graph_observation_ids.update(
            link.observation_id for link in self.cross_reference_graph.links
        )
        if not graph_observation_ids.issubset(available_observation_ids):
            raise FullReferenceTimelineError(
                "cross-reference graph contains an observation outside supplied analyses"
            )
        directive_observation_ids = {
            observation_id
            for directive in self.directives.request.directives
            for observation_id in directive.source_observation_ids
        }
        if not directive_observation_ids.issubset(available_observation_ids):
            raise FullReferenceTimelineError(
                "directive set contains an observation outside supplied analyses"
            )
        if self.directives.request.reference_registry != request.reference_registry:
            raise FullReferenceTimelineError("directive registry does not match normalized request")
        if self.directives.request.hard_constraints != request.hard_constraints:
            raise FullReferenceTimelineError(
                "directive hard constraints do not match normalized request"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": FULL_REFERENCE_TIMELINE_SCHEMA,
            "normalized_request": self.normalized_request.to_wire(),
            "video_analysis": self.video_analysis.to_wire(),
            "audio_analysis": None
            if self.audio_analysis is None
            else self.audio_analysis.to_wire(),
            "cross_reference_graph": self.cross_reference_graph.to_wire(),
            "directives": self.directives.to_wire(),
            "config": self.config.to_wire(),
            "conditioning_window": None
            if self.conditioning_window is None
            else self.conditioning_window.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class FullReferenceTimelinePlan:
    """Validated multimodal timeline plus the canonical IntentGraph hand-off."""

    timeline_id: str
    status: MultimodalTimelineStatus
    request: FullReferenceTimelineRequest
    segments: tuple[MultimodalTimelineSegment, ...]
    source_spans: tuple[TimelineSourceSpan, ...]
    intent_graph: IntentGraph
    directives: ResolvedDirectiveSet
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    schema: str = FULL_REFERENCE_TIMELINE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.timeline_id, "timeline_id")
        if self.schema != FULL_REFERENCE_TIMELINE_SCHEMA:
            raise FullReferenceTimelineError("unsupported Full-Reference timeline schema")
        if not isinstance(self.status, MultimodalTimelineStatus):
            raise FullReferenceTimelineError("timeline status must be a MultimodalTimelineStatus")
        if not isinstance(self.request, FullReferenceTimelineRequest):
            raise FullReferenceTimelineError(
                "timeline request must be a FullReferenceTimelineRequest"
            )
        if (
            not isinstance(self.segments, tuple)
            or not self.segments
            or len(self.segments) > MAX_FULL_REFERENCE_SEGMENTS
        ):
            raise FullReferenceTimelineError("segments must contain a bounded non-empty tuple")
        if not all(isinstance(item, MultimodalTimelineSegment) for item in self.segments):
            raise FullReferenceTimelineError(
                "segments must contain MultimodalTimelineSegment values"
            )
        if (
            not isinstance(self.source_spans, tuple)
            or len(self.source_spans) > MAX_FULL_REFERENCE_SPANS
        ):
            raise FullReferenceTimelineError("source_spans exceed their finite bound")
        if not all(isinstance(item, TimelineSourceSpan) for item in self.source_spans):
            raise FullReferenceTimelineError("source_spans must contain TimelineSourceSpan values")
        if not isinstance(self.intent_graph, IntentGraph):
            raise FullReferenceTimelineError("intent_graph must be an IntentGraph")
        if not isinstance(self.directives, ResolvedDirectiveSet):
            raise FullReferenceTimelineError("directives must be a ResolvedDirectiveSet")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise FullReferenceTimelineError("diagnostics must contain ValidationDiagnostic values")
        if not isinstance(self.limitations, tuple) or not all(
            isinstance(item, Limitation) for item in self.limitations
        ):
            raise FullReferenceTimelineError("limitations must contain Limitation values")
        segment_ids = tuple(item.segment_id for item in self.segments)
        span_ids = tuple(item.span_id for item in self.source_spans)
        if len(segment_ids) != len(set(segment_ids)) or len(span_ids) != len(set(span_ids)):
            raise FullReferenceTimelineError("timeline segment and source span IDs must be unique")
        assets = {
            asset.asset_id: asset
            for asset in self.request.normalized_request.reference_registry.assets
        }
        duration_point = _duration_time(self.request.normalized_request)
        for span in self.source_spans:
            asset = assets.get(span.asset_id)
            if asset is None or asset.kind is not span.modality:
                raise FullReferenceTimelineError(
                    "timeline source span asset ownership or modality does not match the registry"
                )
            if span.target_start.seconds < 0 or span.target_end.seconds > duration_point.seconds:
                raise FullReferenceTimelineError(
                    "timeline source span target bounds exceed effective duration"
                )
        spans = set(span_ids)
        directive_ids = {item.directive_id for item in self.directives.accepted}
        for segment in self.segments:
            if not set(segment.source_span_ids).issubset(spans):
                raise FullReferenceTimelineError("segment references an unknown source span")
            if not set(segment.directive_ids).issubset(directive_ids):
                raise FullReferenceTimelineError("segment references an unknown accepted directive")
            if segment.missing_source and any(
                span.modality is MediaKind.VIDEO
                for span in self.source_spans
                if span.span_id in segment.source_span_ids
            ):
                raise FullReferenceTimelineError(
                    "a missing-source segment cannot reference a video source span"
                )
        # CRITICAL: frame-aligned float seconds are compared through their canonical decimal
        # spelling. Direct Decimal/float equality rejects valid fractional-frame endpoints.
        duration = Decimal(str(self.request.normalized_request.effective_duration_seconds))
        if self.segments[0].start.seconds != 0 or self.segments[-1].end.seconds != duration:
            raise FullReferenceTimelineError(
                "timeline must start at zero and end at effective duration"
            )
        previous = self.segments[0]
        for segment in self.segments[1:]:
            if segment.start.seconds < previous.end.seconds:
                raise FullReferenceTimelineError("timeline segments must not overlap")
            if segment.start.seconds != previous.end.seconds:
                raise FullReferenceTimelineError(
                    "timeline segments must cover every target interval"
                )
            previous = segment

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(_canonical_wire(self.to_wire()))

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "timeline_id": self.timeline_id,
            "status": self.status.value,
            "request": self.request.to_wire(),
            "segments": [item.to_wire() for item in self.segments],
            "source_spans": [item.to_wire() for item in self.source_spans],
            "intent_graph": self.intent_graph.to_wire(),
            "directives": self.directives.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "limitations": [item.to_wire() for item in self.limitations],
        }


@dataclass(frozen=True, slots=True)
class FullReferenceTimelineResult:
    """Planner output; a failure never carries a renderable ContextPlan."""

    timeline: FullReferenceTimelinePlan | None
    plan: ContextPlan | None
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.timeline is None and self.plan is not None:
            raise FullReferenceTimelineError("a ContextPlan requires a timeline")
        if self.timeline is not None and not isinstance(self.timeline, FullReferenceTimelinePlan):
            raise FullReferenceTimelineError("timeline must be a FullReferenceTimelinePlan or None")
        if self.plan is not None and not isinstance(self.plan, ContextPlan):
            raise FullReferenceTimelineError("plan must be a ContextPlan or None")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise FullReferenceTimelineError("diagnostics must contain ValidationDiagnostic values")

    @property
    def has_errors(self) -> bool:
        return any(
            item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for item in self.diagnostics
        )

    @property
    def is_valid(self) -> bool:
        return self.timeline is not None and self.plan is not None and not self.has_errors

    def to_wire(self) -> dict[str, object]:
        return {
            "timeline": None if self.timeline is None else self.timeline.to_wire(),
            "plan": None if self.plan is None else self.plan.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


def _diagnostic(
    code: str, message: str, severity: ValidationSeverity = ValidationSeverity.ERROR
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity, code, message, "full_reference_timeline")


def _limitation(identifier: str, code: str, message: str) -> Limitation:
    return Limitation(identifier, code, message)


def _source_span_id(prefix: str, identifier: str, index: int = 0) -> str:
    suffix = f".{index}" if index else ""
    return _identifier(f"{prefix}.{identifier}{suffix}", "source span ID")


def _duration_time(request: NormalizedContextRequest) -> TimePoint:
    return TimePoint.from_text(format(Decimal(str(request.effective_duration_seconds)), "f"))


def _canonical_wire(value: object) -> object:
    """Convert the normalized request's finite floats to explicit canonical tokens."""

    if isinstance(value, float):
        return binary64_token(value)
    if isinstance(value, dict):
        return {key: _canonical_wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_wire(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_canonical_wire(item) for item in value)
    return value


def _asset_order(registry: ReferenceRegistry) -> dict[str, int]:
    return {asset.asset_id: asset.connection_order for asset in registry.assets}


def _window_observation_ids(request: FullReferenceTimelineRequest) -> frozenset[str]:
    if request.conditioning_window is None:
        return frozenset(item.observation_id for item in request.video_analysis.observations)
    count = request.conditioning_window.window_frame_count
    rate = request.video_analysis.frame_rate
    if rate is None or rate <= 0:
        raise FullReferenceTimelineError("conditioning window requires an admitted frame rate")
    frames: dict[tuple[str, str, Decimal], list[int | None]] = {}
    for frame in request.video_analysis.keyframes:
        frames.setdefault((frame.asset_id, frame.source_id, frame.timestamp.seconds), []).append(
            frame.frame_index
        )
    allowed = set()
    for observation in request.video_analysis.observations:
        indices = frames.get(
            (observation.asset_id, observation.source_id, observation.start.seconds), []
        )
        if (
            len(indices) == 1
            and indices[0] is not None
            and 0 <= indices[0] - 1 < count
            and observation.start.seconds == Decimal(indices[0] - 1) / rate
            and observation.end.seconds <= Decimal(count) / rate
        ):
            allowed.add(observation.observation_id)
    return frozenset(allowed)


def _video_segments(
    request: FullReferenceTimelineRequest,
    diagnostics: list[ValidationDiagnostic],
    limitations: list[Limitation],
) -> tuple[list[MultimodalTimelineSegment], list[TimelineSourceSpan]]:
    duration = _duration_time(request.normalized_request)
    if request.video_analysis.status in {
        VideoAnalysisStatus.CORRUPT,
        VideoAnalysisStatus.UNSUPPORTED,
    }:
        diagnostics.append(
            _diagnostic(
                "video_analysis_unusable", "corrupt or unsupported video analysis cannot be planned"
            )
        )
        return [], []
    if request.video_analysis.status in {VideoAnalysisStatus.PARTIAL, VideoAnalysisStatus.EMPTY}:
        limitations.append(
            _limitation(
                "limitation_partial_video",
                "partial_video_analysis",
                "Video analysis is partial or empty; uncovered target intervals remain explicit.",
            )
        )
    selected = set(request.video_analysis.selected_asset_ids)
    allowed = _window_observation_ids(request)
    dropped = len(request.video_analysis.observations) - len(allowed)
    if dropped:
        message = (
            f"Dropped {dropped} visual observations outside the reference conditioning window "
            "or without an unambiguous in-window frame."
        )
        diagnostics.append(
            _diagnostic(
                "perception_outside_conditioning_window", message, ValidationSeverity.WARNING
            )
        )
        limitations.append(
            _limitation(
                "limitation_conditioning_window", "perception_outside_conditioning_window", message
            )
        )
    order = _asset_order(request.normalized_request.reference_registry)
    shots = sorted(
        request.video_analysis.shots,
        key=lambda item: (
            order.get(item.asset_id, MAX_FULL_REFERENCE_SEGMENTS),
            item.start.seconds,
            item.shot_id,
        ),
    )
    segments: list[MultimodalTimelineSegment] = []
    spans: list[TimelineSourceSpan] = []
    cursor = Decimal("0")
    previous_asset: str | None = None
    for shot in shots:
        # CRITICAL: both clipped and unclipped shots may cite only frames native H3 keeps.
        # Removing this filter silently puts evidence from discarded footage into the prompt.
        observation_ids = tuple(value for value in shot.observation_ids if value in allowed)
        if request.conditioning_window is not None and not observation_ids:
            continue
        if shot.asset_id not in selected:
            diagnostics.append(
                _diagnostic(
                    "video_shot_unselected",
                    f"shot {shot.shot_id!r} is outside selected video assets",
                )
            )
            continue
        start = shot.start.seconds
        end = min(shot.end.seconds, duration.seconds)
        if start >= duration.seconds or end <= start:
            diagnostics.append(
                _diagnostic(
                    "source_span_out_of_bounds",
                    f"shot {shot.shot_id!r} lies outside effective duration",
                )
            )
            continue
        if start < cursor:
            diagnostics.append(
                _diagnostic(
                    "visual_overlap",
                    f"shot {shot.shot_id!r} overlaps a prior target video interval",
                )
            )
            continue
        if start > cursor:
            if not request.config.fill_missing_spans:
                diagnostics.append(
                    _diagnostic(
                        "missing_timeline_span", "video source does not cover the target interval"
                    )
                )
                return [], []
            gap_start = TimePoint.from_text(format(cursor, "f"))
            gap_end = TimePoint.from_text(format(start, "f"))
            gap_id = f"segment_gap_{len(segments) + 1}"
            segments.append(
                MultimodalTimelineSegment(
                    gap_id,
                    gap_start,
                    gap_end,
                    TimelineTransition.GAP_FILL,
                    missing_source=True,
                )
            )
            diagnostics.append(
                _diagnostic(
                    "missing_timeline_span",
                    (
                        "video source does not cover an intermediate target interval; "
                        "inserted a gap-fill segment"
                    ),
                    ValidationSeverity.WARNING,
                )
            )
            limitations.append(
                _limitation(
                    f"limitation_gap_{len(segments)}",
                    "missing_timeline_span",
                    (
                        f"Target interval {format(cursor, 'f')}–{format(start, 'f')} "
                        "has no video source."
                    ),
                )
            )
        target_start = TimePoint.from_text(format(start, "f"))
        target_end = TimePoint.from_text(format(end, "f"))
        span = TimelineSourceSpan(
            _source_span_id("video", shot.shot_id),
            MediaKind.VIDEO,
            shot.asset_id,
            shot.source_id,
            shot.start,
            shot.end,
            target_start,
            target_end,
            shot_id=shot.shot_id,
        )
        spans.append(span)
        segment = MultimodalTimelineSegment(
            f"segment_{shot.shot_id}",
            target_start,
            target_end,
            TimelineTransition.OPENING
            if not segments
            else (
                TimelineTransition.CONTINUOUS
                if previous_asset == shot.asset_id
                else TimelineTransition.CUT
            ),
            source_span_ids=(span.span_id,),
            source_asset_ids=(shot.asset_id,),
            source_shot_ids=(shot.shot_id,),
            video_observation_ids=observation_ids,
        )
        segments.append(segment)
        cursor = end
        previous_asset = shot.asset_id
    if cursor < duration.seconds:
        if not request.config.fill_missing_spans:
            diagnostics.append(
                _diagnostic("missing_timeline_span", "video source does not cover the target tail")
            )
            return [], []
        gap_start = TimePoint.from_text(format(cursor, "f"))
        segments.append(
            MultimodalTimelineSegment(
                f"segment_gap_{len(segments) + 1}",
                gap_start,
                duration,
                TimelineTransition.GAP_FILL,
                missing_source=True,
            )
        )
        diagnostics.append(
            _diagnostic(
                "missing_timeline_span",
                "video source does not cover the target tail; inserted a gap-fill segment",
                ValidationSeverity.WARNING,
            )
        )
        limitations.append(
            _limitation(
                f"limitation_gap_{len(segments)}",
                "missing_timeline_span",
                (
                    f"Target interval {format(cursor, 'f')}–{format(duration.seconds, 'f')} "
                    "has no video source."
                ),
            )
        )
    if not segments:
        if not request.config.fill_missing_spans:
            diagnostics.append(
                _diagnostic("missing_video_source", "no video shot can cover the target timeline")
            )
            return [], []
        segments.append(
            MultimodalTimelineSegment(
                "segment_gap_1",
                TimePoint.from_text("0"),
                duration,
                TimelineTransition.GAP_FILL,
                missing_source=True,
            )
        )
        diagnostics.append(
            _diagnostic(
                "missing_video_source",
                "no video shot was available; inserted a full-duration gap-fill segment",
                ValidationSeverity.WARNING,
            )
        )
        limitations.append(
            _limitation(
                "limitation_missing_video",
                "missing_video_source",
                "No video shot was available for the target timeline.",
            )
        )
    return segments, spans


def _audio_spans(
    request: FullReferenceTimelineRequest,
    segments: list[MultimodalTimelineSegment],
    diagnostics: list[ValidationDiagnostic],
    limitations: list[Limitation],
) -> list[TimelineSourceSpan]:
    audio = request.audio_analysis
    if audio is None:
        limitations.append(
            _limitation(
                "limitation_no_audio",
                "no_audio_analysis",
                "No audio analysis was supplied; audio remains unspecified.",
            )
        )
        return []
    if audio.status in {AudioAnalysisStatus.CORRUPT, AudioAnalysisStatus.UNSUPPORTED}:
        diagnostics.append(
            _diagnostic(
                "audio_analysis_unusable", "corrupt or unsupported audio analysis cannot be used"
            )
        )
        return []
    if audio.status in {AudioAnalysisStatus.PARTIAL, AudioAnalysisStatus.EMPTY}:
        limitations.append(
            _limitation(
                "limitation_partial_audio",
                "partial_audio_analysis",
                "Audio analysis is partial or empty; no missing audio is invented.",
            )
        )
    duration = _duration_time(request.normalized_request)
    observations = sorted(
        audio.observations, key=lambda item: (item.start.seconds, item.observation_id)
    )
    overlap_root: dict[str, str] = {}
    previous: list[AudioObservation] = []
    for observation in observations:
        overlaps = [item for item in previous if item.end.seconds > observation.start.seconds]
        if overlaps:
            root = min([observation.observation_id, *(item.observation_id for item in overlaps)])
            for item in overlaps:
                overlap_root[item.observation_id] = root
            overlap_root[observation.observation_id] = root
        previous.append(observation)
    spans: list[TimelineSourceSpan] = []
    for observation in observations:
        if observation.start.seconds >= duration.seconds or observation.end.seconds <= 0:
            diagnostics.append(
                _diagnostic(
                    "audio_span_out_of_bounds",
                    (
                        f"audio observation {observation.observation_id!r} lies outside "
                        "effective duration"
                    ),
                    ValidationSeverity.WARNING,
                )
            )
            continue
        source_start = observation.start
        source_end = observation.end
        for index, segment in enumerate(segments):
            start = max(segment.start.seconds, source_start.seconds)
            end = min(segment.end.seconds, source_end.seconds, duration.seconds)
            if end <= start:
                continue
            target_start = TimePoint.from_text(format(start, "f"))
            target_end = TimePoint.from_text(format(end, "f"))
            span = TimelineSourceSpan(
                _source_span_id("audio", observation.observation_id, index),
                MediaKind.AUDIO,
                observation.asset_id,
                observation.source_id,
                source_start,
                source_end,
                target_start,
                target_end,
                observation_id=observation.observation_id,
                overlap_group_id=(
                    f"audio_overlap_{overlap_root[observation.observation_id]}"
                    if observation.observation_id in overlap_root
                    else None
                ),
            )
            spans.append(span)
            segment_index = segments.index(segment)
            current = segments[segment_index]
            segments[segment_index] = replace(
                current,
                source_span_ids=current.source_span_ids + (span.span_id,),
                source_asset_ids=tuple(
                    dict.fromkeys(current.source_asset_ids + (observation.asset_id,))
                ),
                audio_observation_ids=tuple(
                    dict.fromkeys(current.audio_observation_ids + (observation.observation_id,))
                ),
            )
    return spans


def _entity_map(
    graph: CrossReferenceGraph,
) -> tuple[dict[str, tuple[str, ...]], dict[str, CrossReferenceEntity]]:
    links: dict[str, list[str]] = {}
    entities: dict[str, CrossReferenceEntity] = {}
    for item in graph.entities:
        if item.entity_id is not None:
            entities[item.entity_id] = item
    for link in graph.links:
        if link.entity_id is not None and link.resolution in {
            CrossReferenceResolution.RESOLVED,
            CrossReferenceResolution.USER_SELECTED,
        }:
            links.setdefault(link.observation_id, []).append(link.entity_id)
    return {key: tuple(sorted(set(value))) for key, value in links.items()}, entities


def _apply_entities(
    request: FullReferenceTimelineRequest,
    segments: list[MultimodalTimelineSegment],
    diagnostics: list[ValidationDiagnostic],
) -> tuple[
    tuple[IntentSubject, ...],
    tuple[IntentScene, ...],
    tuple[IntentAction, ...],
    tuple[CameraIntent, ...],
    tuple[AudioIntent, ...],
    dict[str, tuple[str, ...]],
]:
    links, entities = _entity_map(request.cross_reference_graph)
    allowed_video = _window_observation_ids(request)
    allowed = allowed_video | frozenset(
        ()
        if request.audio_analysis is None
        else (item.observation_id for item in request.audio_analysis.observations)
    )
    if request.conditioning_window is not None:
        entities = {
            key: value
            for key, value in entities.items()
            if not value.observation_ids or allowed.intersection(value.observation_ids)
        }
        links = {
            key: tuple(value for value in values if value in entities)
            for key, values in links.items()
            if key in allowed
        }
    subjects = tuple(
        IntentSubject(entity_id, entity.label or entity_id, entity.source_asset_ids)
        for entity_id, entity in sorted(entities.items())
        if entity.entity_kind is ReferenceEntityKind.SUBJECT
        and entity.resolution
        in {CrossReferenceResolution.RESOLVED, CrossReferenceResolution.USER_SELECTED}
    )
    scenes = tuple(
        IntentScene(entity_id, entity.label or entity_id)
        for entity_id, entity in sorted(entities.items())
        if entity.entity_kind is ReferenceEntityKind.SCENE
        and entity.resolution
        in {CrossReferenceResolution.RESOLVED, CrossReferenceResolution.USER_SELECTED}
    )
    if not scenes:
        scenes = (IntentScene("scene_1", request.normalized_request.user_intent),)
    actions: list[IntentAction] = []
    cameras: list[CameraIntent] = []
    for observation in request.video_analysis.observations:
        if observation.observation_id not in allowed_video:
            continue
        subject_ids = tuple(
            item
            for item in links.get(observation.observation_id, ())
            if item in {value.subject_id for value in subjects}
        )
        if observation.kind is VideoObservationKind.ACTION:
            actions.append(
                IntentAction(
                    f"action.{observation.observation_id}",
                    observation.claim,
                    subject_ids,
                    scenes[0].scene_id,
                )
            )
        elif observation.kind is VideoObservationKind.CAMERA:
            cameras.append(
                CameraIntent(
                    f"camera.{observation.observation_id}",
                    observation.claim,
                    scenes[0].scene_id,
                    subject_ids,
                )
            )
    audio_values: list[AudioIntent] = []
    layer_map = {
        AudioObservationKind.TRANSCRIPT: AudioLayer.DIALOGUE,
        AudioObservationKind.SPEAKER: AudioLayer.DIALOGUE,
        AudioObservationKind.VOICE: AudioLayer.DIALOGUE,
        AudioObservationKind.MUSIC: AudioLayer.MUSIC,
        AudioObservationKind.AMBIENCE: AudioLayer.AMBIENCE,
        AudioObservationKind.SFX: AudioLayer.SFX,
    }
    audio_observations: tuple[AudioObservation, ...] = (
        () if request.audio_analysis is None else request.audio_analysis.observations
    )
    for audio_observation in audio_observations:
        audio_values.append(
            AudioIntent(
                f"audio.{audio_observation.observation_id}",
                layer_map[audio_observation.kind],
                audio_observation.claim,
                (audio_observation.asset_id,),
            )
        )
    subject_id_set = {item.subject_id for item in subjects}
    for index, segment in enumerate(segments):
        entities_for_segment: list[str] = []
        observation_ids = (*segment.video_observation_ids, *segment.audio_observation_ids)
        for observation_id in observation_ids:
            entities_for_segment.extend(
                item
                for item in links.get(observation_id, ())
                if item in subject_id_set or item in entities
            )
        unresolved = [
            link.observation_id
            for link in request.cross_reference_graph.links
            if link.observation_id in observation_ids and link.entity_id is None
        ]
        if unresolved:
            diagnostics.append(
                _diagnostic(
                    "unresolved_identity",
                    f"observations {', '.join(sorted(set(unresolved)))} remain unresolved",
                    ValidationSeverity.WARNING,
                )
            )
        segments[index] = replace(segment, entity_ids=tuple(sorted(set(entities_for_segment))))
    return subjects, scenes, tuple(actions), tuple(cameras), tuple(audio_values), links


def _build_intent_graph(
    request: FullReferenceTimelineRequest,
    segments: list[MultimodalTimelineSegment],
    diagnostics: list[ValidationDiagnostic],
    limitations: list[Limitation],
) -> IntentGraph | None:
    subjects, scenes, actions, cameras, audios, _links = _apply_entities(
        request, segments, diagnostics
    )
    subject_ids = {item.subject_id for item in subjects}
    action_by_obs = {item.action_id.split(".", 1)[1]: item.action_id for item in actions}
    camera_by_obs = {item.camera_id.split(".", 1)[1]: item.camera_id for item in cameras}
    audio_by_obs = {item.audio_id.split(".", 1)[1]: item.audio_id for item in audios}
    action_ids_by_segment: dict[str, tuple[str, ...]] = {}
    camera_ids_by_segment: dict[str, tuple[str, ...]] = {}
    audio_ids_by_segment: dict[str, tuple[str, ...]] = {}
    for segment in segments:
        action_ids_by_segment[segment.segment_id] = tuple(
            action_by_obs[observation_id]
            for observation_id in segment.video_observation_ids
            if observation_id in action_by_obs
        )
        camera_ids_by_segment[segment.segment_id] = tuple(
            camera_by_obs[observation_id]
            for observation_id in segment.video_observation_ids
            if observation_id in camera_by_obs
        )
        audio_ids_by_segment[segment.segment_id] = tuple(
            audio_by_obs[observation_id]
            for observation_id in segment.audio_observation_ids
            if observation_id in audio_by_obs
        )

    accepted_directives = request.directives.accepted
    for directive in accepted_directives:
        matched_indexes: list[int] = []
        for index, segment in enumerate(segments):
            action_ids = action_ids_by_segment[segment.segment_id]
            camera_ids = camera_ids_by_segment[segment.segment_id]
            audio_ids = audio_ids_by_segment[segment.segment_id]
            if (
                directive.action is DirectiveAction.COPY_EVENT
                and directive.target_segment_id is not None
            ):
                matches_target = directive.target_segment_id == segment.segment_id
            else:
                matches_target = (
                    bool(
                        set(directive.source_observation_ids)
                        & {
                            *segment.video_observation_ids,
                            *segment.audio_observation_ids,
                        }
                    )
                    or bool(set(directive.source_asset_ids) & set(segment.source_asset_ids))
                    or directive.target_kind is DirectiveTargetKind.TIMELINE
                    or (
                        directive.target_kind is DirectiveTargetKind.SUBJECT
                        and directive.target_id in segment.entity_ids
                    )
                    or (
                        directive.target_kind is DirectiveTargetKind.SCENE
                        and directive.target_id == scenes[0].scene_id
                    )
                    or (
                        directive.target_kind is DirectiveTargetKind.ACTION
                        and directive.target_id in action_ids
                    )
                    or (
                        directive.target_kind is DirectiveTargetKind.CAMERA
                        and directive.target_id in camera_ids
                    )
                    or (
                        directive.target_kind
                        in {
                            DirectiveTargetKind.AUDIO,
                            DirectiveTargetKind.VOICE,
                            DirectiveTargetKind.DIALOGUE,
                            DirectiveTargetKind.LYRICS,
                        }
                        and directive.target_id in audio_ids
                    )
                    or (
                        directive.target_kind is DirectiveTargetKind.ASSET
                        and directive.target_id in segment.source_asset_ids
                    )
                )
            if matches_target:
                matched_indexes.append(index)
        if not matched_indexes:
            severity = (
                ValidationSeverity.ERROR
                if directive.action is DirectiveAction.COPY_EVENT
                else ValidationSeverity.WARNING
            )
            diagnostics.append(
                _diagnostic(
                    "directive_target_unmapped",
                    (
                        f"accepted directive {directive.directive_id!r} has no matching "
                        "timeline segment"
                    ),
                    severity,
                )
            )
            limitations.append(
                _limitation(
                    f"limitation_directive_{directive.directive_id}",
                    "directive_target_unmapped",
                    (
                        f"Directive {directive.directive_id!r} remains explicit but is not "
                        "attached to a target segment."
                    ),
                )
            )
            continue
        for index in matched_indexes:
            segment = segments[index]
            segments[index] = replace(
                segment,
                directive_ids=tuple(
                    dict.fromkeys((*segment.directive_ids, directive.directive_id))
                ),
            )
    timeline_segments: list[TimelineSegment] = []
    for segment in segments:
        action_ids = action_ids_by_segment[segment.segment_id]
        camera_ids = camera_ids_by_segment[segment.segment_id]
        audio_ids = audio_ids_by_segment[segment.segment_id]
        event_ids = tuple(
            directive.directive_id
            for directive in accepted_directives
            if directive.action is DirectiveAction.COPY_EVENT
            and directive.target_segment_id == segment.segment_id
        )
        timeline_segments.append(
            TimelineSegment(
                segment.segment_id,
                segment.start,
                segment.end,
                scene_id=scenes[0].scene_id,
                subject_ids=tuple(item for item in segment.entity_ids if item in subject_ids),
                action_ids=action_ids,
                camera_id=camera_ids[0] if camera_ids else None,
                audio_ids=audio_ids,
                event_ids=event_ids,
            )
        )
    events: list[EventCopy] = []
    retention: list[RetentionRelation] = []
    segment_ids = {item.segment_id for item in segments}
    for directive in request.directives.accepted:
        if directive.action is DirectiveAction.COPY_EVENT:
            target = directive.target_segment_id or ""
            if target not in segment_ids:
                continue
            source_asset = directive.source_asset_ids[0]
            kind = next(
                item.kind
                for item in request.normalized_request.reference_registry.assets
                if item.asset_id == source_asset
            )
            domain = RetentionDomain.AUDIO if kind is MediaKind.AUDIO else RetentionDomain.VISUAL
            events.append(
                EventCopy(
                    directive.directive_id, domain, source_asset, target, EventCopyMode.FULL_COPY
                )
            )
        elif directive.action is DirectiveAction.RETAIN and directive.source_asset_ids:
            if directive.target_kind in {DirectiveTargetKind.AUDIO, DirectiveTargetKind.VOICE}:
                if directive.target_id not in {item.audio_id for item in audios}:
                    continue
                for aspect in directive.retention_aspects:
                    audio_marker = (
                        AudioRetentionMarker.FULLY_COPY
                        if aspect is RetentionAspect.AUDIO
                        and directive.retention_scope is not RetentionScope.AUDIO_LAYER
                        else AudioRetentionMarker.PARTIALLY_COPY
                        if aspect is RetentionAspect.AUDIO
                        else AudioRetentionMarker.REFERENCE
                    )
                    retention.append(
                        RetentionRelation(
                            directive.directive_id + "." + aspect.value,
                            RetentionDomain.AUDIO,
                            directive.source_asset_ids,
                            directive.target_id,
                            audio_marker,
                            scope=directive.retention_scope,
                        )
                    )
            # GUARD: standalone picture/video denotations keep their role-qualified asset target;
            # routing them through the subject fallback silently drops the declared relation.
            elif (
                directive.target_kind is DirectiveTargetKind.ASSET
                and directive.retention_scope
                in {RetentionScope.PICTURE, RetentionScope.VIDEO_STRUCTURE}
            ):
                visual_marker = (
                    VisualRetentionMarker.FULLY_PRESERVED
                    if RetentionAspect.IDENTITY in directive.retention_aspects
                    else VisualRetentionMarker.ATTRIBUTE_TRANSFER
                )
                retention.append(
                    RetentionRelation(
                        directive.directive_id,
                        RetentionDomain.VISUAL,
                        directive.source_asset_ids,
                        directive.target_id,
                        visual_marker,
                        scope=directive.retention_scope,
                    )
                )
            elif directive.target_id in subject_ids:
                visual_marker = (
                    VisualRetentionMarker.FULLY_PRESERVED
                    if RetentionAspect.IDENTITY in directive.retention_aspects
                    else VisualRetentionMarker.ATTRIBUTE_TRANSFER
                )
                retention.append(
                    RetentionRelation(
                        directive.directive_id,
                        RetentionDomain.VISUAL,
                        directive.source_asset_ids,
                        directive.target_id,
                        visual_marker,
                        scope=directive.retention_scope,
                    )
                )
    graph_result = build_intent_graph(
        effective_duration=_duration_time(request.normalized_request),
        registry=request.normalized_request.reference_registry,
        subjects=subjects,
        scenes=scenes,
        actions=actions,
        cameras=cameras,
        audios=audios,
        events=events,
        retention=retention,
        segments=timeline_segments,
        allow_gaps=False,
    )
    if graph_result.graph is None:
        diagnostics.extend(graph_result.diagnostics)
        return None
    return graph_result.graph


def plan_full_reference_timeline(
    request: FullReferenceTimelineRequest,
) -> FullReferenceTimelineResult:
    """Merge accepted multimodal evidence into a deterministic target timeline."""

    if not isinstance(request, FullReferenceTimelineRequest):
        raise FullReferenceTimelineError("request must be a FullReferenceTimelineRequest")
    if request.directives.status is DirectiveSetStatus.CONFLICTING:
        return FullReferenceTimelineResult(
            None,
            None,
            (
                _diagnostic(
                    "directive_conflict",
                    "conflicting directive decisions block a renderable timeline",
                ),
            ),
        )
    if request.cross_reference_graph.status is CrossReferenceGraphStatus.CONFLICTING:
        return FullReferenceTimelineResult(
            None,
            None,
            (
                _diagnostic(
                    "cross_reference_conflict",
                    "conflicting cross-reference identity decisions block a renderable timeline",
                ),
            ),
        )
    diagnostics: list[ValidationDiagnostic] = []
    limitations: list[Limitation] = []
    segments, video_spans = _video_segments(request, diagnostics, limitations)
    if any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for item in diagnostics
    ):
        return FullReferenceTimelineResult(None, None, tuple(diagnostics))
    audio_spans = _audio_spans(request, segments, diagnostics, limitations)
    if len(video_spans) + len(audio_spans) > request.config.max_source_spans:
        return FullReferenceTimelineResult(
            None,
            None,
            tuple(diagnostics)
            + (_diagnostic("source_span_limit", "timeline source spans exceed the finite bound"),),
        )
    if len(segments) > request.config.max_segments:
        return FullReferenceTimelineResult(
            None,
            None,
            tuple(diagnostics)
            + (_diagnostic("segment_limit", "timeline segments exceed the finite bound"),),
        )
    graph = _build_intent_graph(request, segments, diagnostics, limitations)
    if graph is None or any(
        item.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for item in diagnostics
    ):
        return FullReferenceTimelineResult(None, None, tuple(diagnostics))
    try:
        evidence_records: list[EvidenceRecord] = []
        allowed = _window_observation_ids(request)
        discarded_evidence_ids = {
            item.evidence.evidence_id
            for item in request.video_analysis.observations
            if item.observation_id not in allowed
        }
        evidence_records.extend(
            item.evidence
            for item in request.video_analysis.observations
            if item.observation_id in allowed
        )
        if request.audio_analysis is not None:
            evidence_records.extend(item.evidence for item in request.audio_analysis.observations)
        evidence = merge_evidence(
            EvidenceSet(
                tuple(
                    item
                    for item in request.normalized_request.evidence.records
                    if item.evidence_id not in discarded_evidence_ids
                )
            ),
            EvidenceSet(tuple(evidence_records)),
        )
    except ContractValidationError as exc:
        return FullReferenceTimelineResult(
            None, None, (_diagnostic("evidence_conflict", str(exc)),)
        )
    plan_request = replace(request.normalized_request, evidence=evidence)
    timeline_id = (
        "timeline_"
        + canonical_fingerprint(
            {
                "schema": FULL_REFERENCE_TIMELINE_SCHEMA,
                "request": _canonical_wire(plan_request.to_wire()),
                "segments": [item.to_wire() for item in segments],
                "spans": [item.to_wire() for item in (*video_spans, *audio_spans)],
                "directives": request.directives.to_wire(),
            }
        ).split(":", 1)[1][:32]
    )
    steps = (
        PlanStep(
            "step_normalize",
            PlanStage.NORMALIZE,
            PlanStepStatus.COMPLETED,
            "reuse normalized REF2VA request",
        ),
        PlanStep(
            "step_bind_references",
            PlanStage.BIND_REFERENCES,
            PlanStepStatus.COMPLETED,
            "bind video/audio source spans and cross-reference ownership",
            input_evidence_ids=tuple(record.evidence_id for record in evidence.records),
            output_ids=tuple(asset.asset_id for asset in plan_request.reference_registry.assets),
        ),
        PlanStep(
            "step_assemble_intent",
            PlanStage.ASSEMBLE_INTENT,
            PlanStepStatus.COMPLETED,
            "assemble deterministic multimodal target segments",
            input_evidence_ids=tuple(record.evidence_id for record in evidence.records),
            output_ids=tuple(item.segment_id for item in segments),
        ),
        PlanStep(
            "step_validate",
            PlanStage.VALIDATE,
            PlanStepStatus.COMPLETED,
            "validate timeline bounds, ownership, and canonical graph",
            output_ids=(timeline_id,),
        ),
    )
    context_plan = ContextPlan(
        plan_id=timeline_id.replace("timeline_", "plan_", 1),
        schema_version=plan_request.schema_version,
        request=plan_request,
        intent_graph=graph,
        hard_constraints=plan_request.hard_constraints,
        evidence=evidence,
        steps=steps,
        limitations=tuple(limitations),
        diagnostics=tuple(diagnostics),
    )
    status = (
        MultimodalTimelineStatus.PARTIAL
        if limitations
        or request.cross_reference_graph.status
        in {CrossReferenceGraphStatus.PARTIAL, CrossReferenceGraphStatus.AMBIGUOUS}
        else MultimodalTimelineStatus.COMPLETE
    )
    timeline = FullReferenceTimelinePlan(
        timeline_id,
        status,
        request,
        tuple(segments),
        tuple(
            sorted(
                (*video_spans, *audio_spans),
                key=lambda item: (item.target_start.seconds, item.modality.value, item.span_id),
            )
        ),
        graph,
        request.directives,
        tuple(diagnostics),
        tuple(limitations),
    )
    return FullReferenceTimelineResult(timeline, context_plan, tuple(diagnostics))


__all__ = [
    "FULL_REFERENCE_TIMELINE_SCHEMA",
    "MAX_FULL_REFERENCE_SEGMENTS",
    "MAX_FULL_REFERENCE_SPANS",
    "MultimodalTimelineConfig",
    "MultimodalTimelineSegment",
    "MultimodalTimelineStatus",
    "FullReferenceTimelinePlan",
    "FullReferenceTimelineRequest",
    "FullReferenceTimelineResult",
    "TimelineSourceSpan",
    "TimelineTransition",
    "plan_full_reference_timeline",
]
