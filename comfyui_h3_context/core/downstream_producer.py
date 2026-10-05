"""Deterministic M15-02 producer adapters over accepted pure-core contracts."""

from __future__ import annotations

import hashlib
import json
import weakref
from dataclasses import dataclass, fields, is_dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import cast

from .constraints import (
    ContentScope,
    DialogueDelivery,
    DialogueSpeaker,
    DirectiveTarget,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    KeepChangeAction,
    KeepChangeDirective,
    RequiredContent,
    TimePoint,
    TimingConstraint,
)
from .context_reporting import PlanStage, PlanStepStatus
from .contracts import (
    AssetRole,
    EvidenceLevel,
    MediaKind,
    ModelVariant,
    PromptProfile,
    ProviderIdentity,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .cross_reference_graph import (
    CrossReferenceGraph,
    CrossReferenceGraphStatus,
    CrossReferenceProposal,
    CrossReferenceRequest,
    CrossReferenceResolution,
    ObservationReference,
    ReferenceEntityKind,
    build_cross_reference_graph,
)
from .directive_authority_engine import (
    DirectiveAuthorityEngineStatus,
    DirectiveAuthorityReport,
    DirectiveEngineDisposition,
    build_directive_authority_engine,
)
from .errors import ContractValidationError
from .evidence import (
    EvidenceOrigin,
    EvidenceSourceKind,
    SupportStatus,
    Uncertainty,
    UncertaintyKind,
)
from .full_reference_timeline import (
    FullReferenceTimelineRequest,
    FullReferenceTimelineResult,
    MultimodalTimelineStatus,
    TimelineTransition,
    plan_full_reference_timeline,
)
from .intent_graph import (
    AudioLayer,
    AudioOwnership,
    AudioRetentionMarker,
    AudioScope,
    EventCopyMode,
    IntentAction,
    IntentGraph,
    IntentScene,
    IntentSubject,
    RetentionDomain,
    RetentionScope,
    SegmentDevelopment,
    SoundscapeDisposition,
    TimelineSegment,
    VisualRetentionMarker,
    build_intent_graph,
)
from .normalization import DurationSource, RawContextRequest, normalize_request
from .perception_execution import VISUAL_PROFILE, QualifiedPerceptionResult
from .perception_producer import (
    FingerprintClaim,
    PerceptionProducerResult,
    ProducerDisposition,
    ProducerKind,
)
from .reference_directives import (
    DirectiveAction,
    DirectiveAuthority,
    DirectiveDecision,
    DirectiveRequest,
    DirectiveSetStatus,
    DirectiveTargetKind,
    ReferenceDirective,
    ResolvedDirectiveSet,
    RetentionAspect,
    resolve_reference_directives,
)
from .reference_window import reference_conditioning_window
from .registry import BackendLabelKind, BackendTarget, ReferenceRegistry
from .unified_evidence_graph import (
    GraphEntityKind,
    GraphEventKind,
    GraphNodeKind,
    GraphObservation,
    GraphObservationKind,
    GraphResolution,
    GraphSourceSpan,
    GraphStatus,
    GraphSupport,
    GraphUncertainty,
    UnifiedEvidenceGraph,
    build_unified_evidence_graph,
)
from .video_analysis import VideoAnalysisStatus, VideoObservationKind, VideoOrientation


class ManualKeyframeBinding(str, Enum):
    """Explicit source roles the manual primary subject owns."""

    UNBOUND = "unbound"
    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    BOTH = "both"


DOWNSTREAM_PRODUCER_SCHEMA = "h3.context.downstream-producer.v1"
MAX_DOWNSTREAM_WIRE_BYTES = 65_536
MAX_MANUAL_INPUT_BYTES = 65_536


class DownstreamStage(str, Enum):
    HARD_CONSTRAINT = "hard_constraint"
    INTENT_GRAPH = "intent_graph"
    EVIDENCE_FUSION = "evidence_fusion"
    CROSS_REFERENCE = "cross_reference"
    DIRECTIVE_AUTHORITY = "directive_authority"
    FULL_REFERENCE_TIMELINE = "full_reference_timeline"


class DownstreamDisposition(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class DownstreamReasonCode(str, Enum):
    MANUAL_COMPONENT_BUILT = "manual_component_built"
    ADMITTED_SOURCE_PROJECTED = "admitted_source_projected"
    PERCEPTION_PROFILE_UNAVAILABLE = "perception_profile_unavailable"
    QUALIFIED_TIMELINE_BUILT = "qualified_timeline_built"


_REASON_MESSAGES = {
    DownstreamReasonCode.QUALIFIED_TIMELINE_BUILT: (
        "Partial timeline built from qualified uncertain frame observations."
    ),
    DownstreamReasonCode.MANUAL_COMPONENT_BUILT: "Deterministic manual component built.",
    DownstreamReasonCode.ADMITTED_SOURCE_PROJECTED: (
        "Caller-declared admitted source projected without semantic inference."
    ),
    DownstreamReasonCode.PERCEPTION_PROFILE_UNAVAILABLE: (
        "A qualified perception profile is unavailable; no renderable timeline was produced."
    ),
}

# SECURITY: this frozen exact-type set is the authority boundary;
# module-name ownership is spoofable.
_ALLOWED_ENUM_TYPES: frozenset[type[Enum]] = frozenset(
    {
        AssetRole,
        AudioLayer,
        AudioOwnership,
        AudioRetentionMarker,
        AudioScope,
        BackendLabelKind,
        BackendTarget,
        ContentScope,
        DialogueDelivery,
        CrossReferenceGraphStatus,
        CrossReferenceResolution,
        DirectiveAction,
        DirectiveAuthority,
        DirectiveAuthorityEngineStatus,
        DirectiveDecision,
        DirectiveEngineDisposition,
        DirectiveSetStatus,
        DirectiveTarget,
        DirectiveTargetKind,
        DownstreamDisposition,
        DownstreamReasonCode,
        DownstreamStage,
        DurationSource,
        EventCopyMode,
        EvidenceLevel,
        EvidenceOrigin,
        EvidenceSourceKind,
        ExactTextKind,
        FingerprintClaim,
        GraphEntityKind,
        GraphEventKind,
        GraphNodeKind,
        GraphObservationKind,
        GraphResolution,
        GraphStatus,
        GraphSupport,
        KeepChangeAction,
        MediaKind,
        ManualKeyframeBinding,
        ModelVariant,
        ProducerDisposition,
        ProducerKind,
        PromptProfile,
        ProviderIdentity,
        PlanStage,
        PlanStepStatus,
        ReferenceEntityKind,
        RetentionAspect,
        RetentionDomain,
        RetentionScope,
        SegmentDevelopment,
        SoundscapeDisposition,
        SupportStatus,
        TaskMode,
        UncertaintyKind,
        ValidationSeverity,
        VisualRetentionMarker,
        MultimodalTimelineStatus,
        TimelineTransition,
        VideoAnalysisStatus,
        VideoObservationKind,
        VideoOrientation,
    }
)


@dataclass(frozen=True, eq=False)
class DownstreamProducerReport:
    stage: DownstreamStage
    disposition: DownstreamDisposition
    reason_code: DownstreamReasonCode
    component_fingerprint: str | None
    upstream_fingerprints: tuple[str, ...]
    provenance: str
    schema: str = DOWNSTREAM_PRODUCER_SCHEMA

    def __post_init__(self) -> None:
        if type(self) is not DownstreamProducerReport:
            raise ContractValidationError("downstream report must be an exact concrete value")
        if type(self.stage) is not DownstreamStage:
            raise ContractValidationError("downstream stage is invalid")
        if type(self.disposition) is not DownstreamDisposition:
            raise ContractValidationError("downstream disposition is invalid")
        if type(self.reason_code) is not DownstreamReasonCode:
            raise ContractValidationError("downstream reason code is invalid")
        if type(self.schema) is not str or self.schema != DOWNSTREAM_PRODUCER_SCHEMA:
            raise ContractValidationError("downstream schema is unsupported")
        if type(self.provenance) is not str or self.provenance not in {
            "manual_user_authored",
            "caller_declared_unverified",
            "qualified_local_observation",
        }:
            raise ContractValidationError("downstream provenance is unsupported")
        if type(self.upstream_fingerprints) is not tuple or len(self.upstream_fingerprints) > 16:
            raise ContractValidationError("upstream fingerprints are outside the bounded envelope")
        for value in self.upstream_fingerprints:
            _fingerprint(value, "upstream fingerprint")
        if self.disposition in {
            DownstreamDisposition.COMPLETE,
            DownstreamDisposition.PARTIAL,
        }:
            _fingerprint(self.component_fingerprint, "component fingerprint")
        elif self.component_fingerprint is not None:
            raise ContractValidationError("non-complete report cannot authorize a component")
        if self.reason_code is DownstreamReasonCode.ADMITTED_SOURCE_PROJECTED:
            if (
                self.stage not in {DownstreamStage.EVIDENCE_FUSION, DownstreamStage.CROSS_REFERENCE}
                or self.disposition is not DownstreamDisposition.PARTIAL
                or self.provenance != "caller_declared_unverified"
                or not self.upstream_fingerprints
            ):
                raise ContractValidationError("admitted-source report joins are inconsistent")
        elif self.reason_code is DownstreamReasonCode.MANUAL_COMPONENT_BUILT:
            if (
                self.stage
                not in {
                    DownstreamStage.HARD_CONSTRAINT,
                    DownstreamStage.INTENT_GRAPH,
                    DownstreamStage.DIRECTIVE_AUTHORITY,
                }
                or self.disposition is not DownstreamDisposition.COMPLETE
                or self.provenance != "manual_user_authored"
            ):
                raise ContractValidationError("manual report joins are inconsistent")
        elif self.reason_code is DownstreamReasonCode.QUALIFIED_TIMELINE_BUILT:
            if (
                self.stage is not DownstreamStage.FULL_REFERENCE_TIMELINE
                or self.disposition is not DownstreamDisposition.PARTIAL
                or self.provenance != "qualified_local_observation"
                or len(self.upstream_fingerprints) != 8
            ):
                raise ContractValidationError("qualified timeline report joins are inconsistent")
        elif (
            self.stage is not DownstreamStage.FULL_REFERENCE_TIMELINE
            or self.disposition is not DownstreamDisposition.UNAVAILABLE
            or self.provenance != "manual_user_authored"
        ):
            raise ContractValidationError("unavailable timeline report joins are inconsistent")

    @property
    def reason(self) -> str:
        _assert_report_current(self)
        return _REASON_MESSAGES[self.reason_code]

    def to_wire(self) -> dict[str, object]:
        _assert_report_current(self)
        value: dict[str, object] = {
            "schema": self.schema,
            "stage": self.stage.value,
            "disposition": self.disposition.value,
            "reason_code": self.reason_code.value,
            "reason": self.reason,
            "component_fingerprint": self.component_fingerprint,
            "upstream_fingerprints": list(self.upstream_fingerprints),
            "provenance": self.provenance,
        }
        if (
            len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
            > MAX_DOWNSTREAM_WIRE_BYTES
        ):
            raise ContractValidationError("downstream report exceeds its wire bound")
        return value


@dataclass(frozen=True)
class _ReportAuthority:
    digest: str
    component: object | None
    upstream: tuple[object, ...]


_TRUSTED_REPORTS: weakref.WeakKeyDictionary[DownstreamProducerReport, _ReportAuthority] = (
    weakref.WeakKeyDictionary()
)


def _fingerprint(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ContractValidationError(f"{field} must be a SHA-256 fingerprint")
    return value


def _report_authority(report: DownstreamProducerReport) -> str:
    payload = (
        report.schema,
        report.stage.value,
        report.disposition.value,
        report.reason_code.value,
        report.component_fingerprint,
        report.upstream_fingerprints,
        report.provenance,
    )
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


def _validate_exact_graph(value: object, *, _seen: set[int] | None = None) -> None:
    """Reject polymorphic nested contract values before any contract method can dispatch."""

    if value is None or type(value) in {str, int, float, bool, Decimal}:
        return
    if isinstance(value, Enum):
        if type(value) not in _ALLOWED_ENUM_TYPES:
            raise ContractValidationError("contract graph contains a non-owned enum")
        return
    if type(value) in {tuple, list}:
        for item in cast(tuple[object, ...] | list[object], value):
            _validate_exact_graph(item, _seen=_seen)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ContractValidationError("contract mapping keys must be exact strings")
            _validate_exact_graph(item, _seen=_seen)
        return
    if type(value) is PerceptionProducerResult:
        value.assert_current()
        return
    if type(value) is QualifiedPerceptionResult:
        value.assert_current()
        return
    if not is_dataclass(value):
        raise ContractValidationError("contract graph contains an unsupported value")
    value_type: type[object] = type(value)
    if any(is_dataclass(base) for base in value_type.__mro__[1:]):
        raise ContractValidationError("contract graph contains a polymorphic dataclass")
    seen = set() if _seen is None else _seen
    if id(value) in seen:
        raise ContractValidationError("contract graph contains a cycle")
    seen.add(id(value))
    try:
        for field in fields(value):
            _validate_exact_graph(object.__getattribute__(value, field.name), _seen=seen)
        post_init = value_type.__dict__.get("__post_init__")
        if post_init is not None:
            post_init(value)
    except ContractValidationError:
        raise
    except Exception as exc:
        raise ContractValidationError("contract graph validation failed") from exc
    finally:
        seen.remove(id(value))


def _safe_fingerprint(value: object) -> str:
    _validate_exact_graph(value)
    method = type(value).__dict__.get("to_wire")
    try:
        payload = method(value) if method is not None else repr(value)
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()
    except ContractValidationError:
        raise
    except Exception as exc:
        raise ContractValidationError("contract fingerprinting failed") from exc


def _assert_report_current(value: object) -> None:
    if type(value) is not DownstreamProducerReport:
        raise ContractValidationError("downstream report must be exact")
    DownstreamProducerReport.__post_init__(value)
    authority = _TRUSTED_REPORTS.get(value)
    if authority is None or authority.digest != _report_authority(value):
        raise ContractValidationError("downstream report lacks current module authority")
    if authority.component is not None:
        if _safe_fingerprint(authority.component) != value.component_fingerprint:
            raise ContractValidationError("downstream report component is stale")
    if tuple(_safe_fingerprint(item) for item in authority.upstream) != value.upstream_fingerprints:
        raise ContractValidationError("downstream report upstream join is stale")


def assert_downstream_pair(
    report: DownstreamProducerReport,
    component: object | None,
    upstream: tuple[object, ...],
) -> None:
    _assert_report_current(report)
    authority = _TRUSTED_REPORTS[report]
    if authority.component is not component or len(authority.upstream) != len(upstream):
        raise ContractValidationError("downstream report does not authorize this component")
    if any(left is not right for left, right in zip(authority.upstream, upstream, strict=True)):
        raise ContractValidationError("downstream report does not authorize these upstreams")


def _issue_report(
    stage: DownstreamStage,
    disposition: DownstreamDisposition,
    reason: DownstreamReasonCode,
    component: object | None,
    upstream: tuple[object, ...] = (),
    provenance: str = "manual_user_authored",
) -> DownstreamProducerReport:
    _validate_stage_roles(stage, component, upstream)
    component_fp = None if component is None else _safe_fingerprint(component)
    upstream_fingerprints = tuple(_safe_fingerprint(item) for item in upstream)
    report = DownstreamProducerReport(
        stage, disposition, reason, component_fp, upstream_fingerprints, provenance
    )
    _TRUSTED_REPORTS[report] = _ReportAuthority(_report_authority(report), component, upstream)
    return report


def issue_manual_component_report(
    stage: DownstreamStage,
    component: object,
    upstream: tuple[object, ...] = (),
) -> DownstreamProducerReport:
    if type(stage) is not DownstreamStage:
        raise ContractValidationError("manual component stage must be exact")
    if stage not in {DownstreamStage.HARD_CONSTRAINT, DownstreamStage.INTENT_GRAPH}:
        raise ContractValidationError("manual component stage is unsupported")
    return _issue_report(
        stage,
        DownstreamDisposition.COMPLETE,
        DownstreamReasonCode.MANUAL_COMPONENT_BUILT,
        component,
        upstream,
    )


def build_manual_hard_constraints(
    *,
    dialogue: str = "",
    visible_text: str = "",
    required_content: str = "",
    forbidden_content: str = "",
    timing_start: str = "",
    timing_end: str = "",
    keep_target: str = "",
    keep_value: str = "",
    change_replacement: str = "",
    dialogue_language: str = "auto",
    dialogue_speaker: str = "",
    dialogue_delivery: str = "on_screen",
) -> HardConstraintSet:
    for value, field in (
        (dialogue, "dialogue"),
        (visible_text, "visible_text"),
        (required_content, "required_content"),
        (forbidden_content, "forbidden_content"),
        (timing_start, "timing_start"),
        (timing_end, "timing_end"),
        (keep_target, "keep_target"),
        (keep_value, "keep_value"),
        (change_replacement, "change_replacement"),
        (dialogue_language, "dialogue_language"),
        (dialogue_speaker, "dialogue_speaker"),
        (dialogue_delivery, "dialogue_delivery"),
    ):
        if type(value) is not str:
            raise ContractValidationError(f"{field} must be a string")
    if (
        sum(
            len(value.encode("utf-8"))
            for value, _ in (
                (dialogue, "dialogue"),
                (visible_text, "visible_text"),
                (required_content, "required_content"),
                (forbidden_content, "forbidden_content"),
                (timing_start, "timing_start"),
                (timing_end, "timing_end"),
                (keep_target, "keep_target"),
                (keep_value, "keep_value"),
                (change_replacement, "change_replacement"),
                (dialogue_language, "dialogue_language"),
                (dialogue_speaker, "dialogue_speaker"),
                (dialogue_delivery, "dialogue_delivery"),
            )
        )
        > MAX_MANUAL_INPUT_BYTES
    ):
        raise ContractValidationError("manual hard constraints exceed the aggregate byte limit")
    values: list[object] = []
    try:
        delivery = DialogueDelivery(dialogue_delivery)
    except ValueError as exc:
        raise ContractValidationError("invalid dialogue delivery") from exc
    if dialogue:
        values.append(
            ExactTextConstraint(
                "manual.dialogue.1",
                ExactTextKind.DIALOGUE,
                dialogue,
                None if dialogue_language == "auto" else dialogue_language,
                speakers=(DialogueSpeaker("manual.speaker.1", identity=dialogue_speaker),)
                if dialogue_speaker
                else (),
                delivery=delivery,
            )
        )
    if visible_text:
        values.append(
            ExactTextConstraint("manual.visible_text.1", ExactTextKind.VISIBLE_TEXT, visible_text)
        )
    if required_content:
        values.append(RequiredContent("manual.required.1", ContentScope.GENERAL, required_content))
    if forbidden_content:
        values.append(
            ForbiddenContent("manual.forbidden.1", ContentScope.GENERAL, forbidden_content)
        )
    if timing_start:
        values.append(
            TimingConstraint(
                "manual.timing.1",
                TimePoint.from_text(timing_start),
                TimePoint.from_text(timing_end) if timing_end else None,
            )
        )
    elif timing_end:
        raise ContractValidationError("timing_end requires timing_start")
    if keep_value or change_replacement:
        if not keep_target or not keep_value:
            raise ContractValidationError("keep/change requires target and value")
        action = KeepChangeAction.CHANGE if change_replacement else KeepChangeAction.KEEP
        try:
            target = DirectiveTarget(keep_target)
        except ValueError as exc:
            raise ContractValidationError("keep/change target is unsupported") from exc
        values.append(
            KeepChangeDirective(
                "manual.keep_change.1",
                action,
                target,
                keep_value,
                change_replacement or None,
            )
        )
    return HardConstraintSet(tuple(values))  # type: ignore[arg-type]


def build_manual_intent(
    request: RawContextRequest,
    registry: ReferenceRegistry,
    *,
    subject_label: str = "",
    action_description: str = "",
    secondary_subject_label: str = "",
    secondary_action_description: str = "",
    complete_silence: bool = False,
    keyframe_binding: ManualKeyframeBinding = ManualKeyframeBinding.UNBOUND,
    segment_development: SegmentDevelopment = SegmentDevelopment.UNSPECIFIED,
) -> IntentGraph:
    if type(request) is not RawContextRequest or type(registry) is not ReferenceRegistry:
        raise ContractValidationError("manual intent requires exact request and registry values")
    if any(
        type(value) is not str
        for value in (
            subject_label,
            action_description,
            secondary_subject_label,
            secondary_action_description,
        )
    ):
        raise ContractValidationError("manual intent widget values must be exact strings")
    if type(complete_silence) is not bool:
        raise ContractValidationError("complete_silence must be an exact bool")
    if type(keyframe_binding) is not ManualKeyframeBinding:
        raise ContractValidationError("keyframe_binding must be a ManualKeyframeBinding")
    if type(segment_development) is not SegmentDevelopment:
        raise ContractValidationError("segment_development must be a SegmentDevelopment")
    _validate_exact_graph(request)
    _validate_exact_graph(registry)
    if request.reference_registry.assets and _safe_fingerprint(
        request.reference_registry
    ) != _safe_fingerprint(registry):
        raise ContractValidationError(
            "manual intent request registry does not match the supplied registry"
        )
    normalized = normalize_request(replace(request, reference_registry=registry))
    if not normalized.is_valid or normalized.request is None:
        raise ContractValidationError("request cannot be normalized for manual intent")
    mode = normalized.request.task_mode
    allowed_bindings = {
        TaskMode.T2VA: frozenset({ManualKeyframeBinding.UNBOUND}),
        TaskMode.I2VA: frozenset(
            {ManualKeyframeBinding.UNBOUND, ManualKeyframeBinding.FIRST_FRAME}
        ),
        TaskMode.FL2VA: frozenset(ManualKeyframeBinding),
        TaskMode.L2VA: frozenset({ManualKeyframeBinding.UNBOUND, ManualKeyframeBinding.LAST_FRAME}),
        TaskMode.REF2VA: frozenset({ManualKeyframeBinding.UNBOUND}),
    }[mode]
    if keyframe_binding not in allowed_bindings:
        raise ContractValidationError("keyframe_binding is incompatible with the request task mode")
    if mode in {TaskMode.T2VA, TaskMode.REF2VA} and segment_development is not (
        SegmentDevelopment.UNSPECIFIED
    ):
        raise ContractValidationError("segment_development requires a keyframe task mode")
    selected_roles = {
        ManualKeyframeBinding.UNBOUND: frozenset(),
        ManualKeyframeBinding.FIRST_FRAME: frozenset({AssetRole.FIRST_FRAME}),
        ManualKeyframeBinding.LAST_FRAME: frozenset({AssetRole.LAST_FRAME}),
        ManualKeyframeBinding.BOTH: frozenset({AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}),
    }[keyframe_binding]
    if selected_roles and not subject_label:
        raise ContractValidationError("keyframe_binding requires a primary subject")
    source_asset_ids = tuple(
        asset.asset_id for asset in registry.assets if asset.role in selected_roles
    )
    if len(source_asset_ids) != len(selected_roles):
        raise ContractValidationError("keyframe_binding requires every selected registry role")
    duration = TimePoint.from_text(str(normalized.request.effective_duration_seconds))
    scene = IntentScene("manual.scene.1", normalized.request.user_intent)
    subject = (
        IntentSubject("manual.subject.1", subject_label, source_asset_ids)
        if subject_label
        else None
    )
    secondary_subject = (
        IntentSubject("manual.subject.2", secondary_subject_label)
        if secondary_subject_label
        else None
    )
    action = (
        IntentAction(
            "manual.action.1",
            action_description,
            (subject.subject_id,) if subject is not None else (),
            scene.scene_id,
        )
        if action_description
        else None
    )
    secondary_action = (
        IntentAction(
            "manual.action.2",
            secondary_action_description,
            (secondary_subject.subject_id,) if secondary_subject is not None else (),
            scene.scene_id,
        )
        if secondary_action_description
        else None
    )
    subjects = tuple(item for item in (subject, secondary_subject) if item is not None)
    actions = tuple(item for item in (action, secondary_action) if item is not None)
    result = build_intent_graph(
        effective_duration=duration,
        registry=registry,
        scenes=(scene,),
        subjects=subjects,
        actions=actions,
        segments=(
            TimelineSegment(
                "manual.segment.1",
                TimePoint.from_text("0"),
                duration,
                scene_id=scene.scene_id,
                subject_ids=tuple(item.subject_id for item in subjects),
                action_ids=tuple(item.action_id for item in actions),
                development=segment_development,
            ),
        ),
        # IMPORTANT: an absent audio tuple is not a silence request. Only this explicit,
        # default-off control may authorize the guide's complete-silence marker downstream.
        soundscape=(
            SoundscapeDisposition.EXPLICIT_COMPLETE_SILENCE
            if complete_silence
            else SoundscapeDisposition.UNSPECIFIED
        ),
    )
    if not result.is_valid or result.graph is None:
        raise ContractValidationError("manual intent graph failed validation")
    return result.graph


def _exact_media(value: object) -> PerceptionProducerResult:
    if type(value) is not PerceptionProducerResult:
        raise ContractValidationError("media must be an exact producer result")
    value.assert_current()
    if (
        value.kind is not ProducerKind.MEDIA
        or value.disposition is not ProducerDisposition.COMPLETE
    ):
        raise ContractValidationError("media admission must be complete")
    return value


def build_admission_evidence_graph(
    media: PerceptionProducerResult,
) -> tuple[UnifiedEvidenceGraph, DownstreamProducerReport]:
    source = _exact_media(media)
    evidence = source.admission_evidence
    end_ms = int(round(evidence.duration_seconds * 1000))
    uncertainty = GraphUncertainty(
        f"uncertainty.{source.asset_id}",
        UncertaintyKind.UNSUPPORTED,
        "Source metadata is caller-declared and no semantic perception profile executed.",
    )
    span = GraphSourceSpan(
        source.asset_id,
        f"admission.{source.asset_id}",
        "sha256:" + evidence.fingerprint,
        0,
        end_ms,
    )
    observation = GraphObservation(
        f"observation.{source.asset_id}",
        source.asset_id,
        MediaKind(source.media_kind),
        GraphObservationKind.OTHER,
        f"Admitted {source.media_kind} source",
        span,
        support=GraphSupport.UNCERTAIN,
        uncertainty_ids=(uncertainty.uncertainty_id,),
    )
    graph = build_unified_evidence_graph(observations=(observation,), uncertainties=(uncertainty,))
    report = _issue_report(
        DownstreamStage.EVIDENCE_FUSION,
        DownstreamDisposition.PARTIAL,
        DownstreamReasonCode.ADMITTED_SOURCE_PROJECTED,
        graph,
        (source,),
        FingerprintClaim.CALLER_DECLARED_UNVERIFIED.value,
    )
    return graph, report


def build_source_cross_reference(
    registry: ReferenceRegistry,
    media: PerceptionProducerResult,
    *,
    resolution: str = "source_only",
    entity_kind: str = "subject",
    candidate_a: str = "",
    candidate_b: str = "",
) -> tuple[CrossReferenceGraph, DownstreamProducerReport]:
    if type(registry) is not ReferenceRegistry:
        raise ContractValidationError("registry must be an exact ReferenceRegistry")
    _validate_exact_graph(registry)
    source = _exact_media(media)
    if any(type(value) is not str for value in (resolution, entity_kind, candidate_a, candidate_b)):
        raise ContractValidationError("cross-reference widget values must be exact strings")
    observation = ObservationReference(
        f"observation.{source.asset_id}",
        source.asset_id,
        f"admission.{source.asset_id}",
        MediaKind(source.media_kind),
    )
    proposals: tuple[CrossReferenceProposal, ...] = ()
    if resolution != "source_only":
        try:
            resolution_value = CrossReferenceResolution(resolution)
            kind_value = ReferenceEntityKind(entity_kind)
        except ValueError as exc:
            raise ContractValidationError("cross-reference manual enum is unsupported") from exc
        candidates = tuple(value for value in (candidate_a, candidate_b) if value)
        proposals = (
            CrossReferenceProposal(
                "manual.proposal.1",
                kind_value,
                candidates,
                None,
                (observation.observation_id,),
                (source.asset_id,),
                (),
                resolution_value,
                (
                    Uncertainty(
                        UncertaintyKind.AMBIGUOUS,
                        "Caller retained unresolved identity candidates without inference.",
                    ),
                ),
            ),
        )
    graph = build_cross_reference_graph(
        CrossReferenceRequest(TaskMode.REF2VA, registry, (observation,)), proposals
    )
    report = _issue_report(
        DownstreamStage.CROSS_REFERENCE,
        DownstreamDisposition.PARTIAL,
        DownstreamReasonCode.ADMITTED_SOURCE_PROJECTED,
        graph,
        (registry, source),
        FingerprintClaim.CALLER_DECLARED_UNVERIFIED.value,
    )
    return graph, report


@dataclass(frozen=True, slots=True)
class DirectiveAuthorityBundle:
    resolved: ResolvedDirectiveSet
    authority: DirectiveAuthorityReport

    def __post_init__(self) -> None:
        if type(self.resolved) is not ResolvedDirectiveSet:
            raise ContractValidationError("resolved directives must be exact")
        if type(self.authority) is not DirectiveAuthorityReport:
            raise ContractValidationError("directive authority must be exact")

    def to_wire(self) -> dict[str, object]:
        return {"resolved": self.resolved.to_wire(), "authority": self.authority.to_wire()}


_STAGE_COMPONENT_TYPES: dict[DownstreamStage, tuple[type[object], ...]] = {
    DownstreamStage.HARD_CONSTRAINT: (HardConstraintSet,),
    DownstreamStage.INTENT_GRAPH: (IntentGraph,),
    DownstreamStage.EVIDENCE_FUSION: (UnifiedEvidenceGraph,),
    DownstreamStage.CROSS_REFERENCE: (CrossReferenceGraph,),
    DownstreamStage.DIRECTIVE_AUTHORITY: (DirectiveAuthorityBundle,),
    DownstreamStage.FULL_REFERENCE_TIMELINE: (FullReferenceTimelineResult,),
}
_STAGE_UPSTREAM_TYPES: dict[DownstreamStage, tuple[type[object], ...]] = {
    DownstreamStage.HARD_CONSTRAINT: (),
    DownstreamStage.INTENT_GRAPH: (RawContextRequest, ReferenceRegistry),
    DownstreamStage.EVIDENCE_FUSION: (PerceptionProducerResult,),
    DownstreamStage.CROSS_REFERENCE: (ReferenceRegistry, PerceptionProducerResult),
    DownstreamStage.DIRECTIVE_AUTHORITY: (
        RawContextRequest,
        ReferenceRegistry,
        HardConstraintSet,
        IntentGraph,
    ),
    DownstreamStage.FULL_REFERENCE_TIMELINE: (
        RawContextRequest,
        ReferenceRegistry,
        PerceptionProducerResult,
        UnifiedEvidenceGraph,
        CrossReferenceGraph,
        DirectiveAuthorityBundle,
        IntentGraph,
    ),
}


def _validate_stage_roles(
    stage: DownstreamStage, component: object | None, upstream: tuple[object, ...]
) -> None:
    if type(stage) is not DownstreamStage or type(upstream) is not tuple:
        raise ContractValidationError("downstream stage roles must be exact")
    component_types = _STAGE_COMPONENT_TYPES[stage]
    if component is None:
        if stage is not DownstreamStage.FULL_REFERENCE_TIMELINE:
            raise ContractValidationError("downstream stage requires a concrete component")
    elif type(component) not in component_types:
        raise ContractValidationError("downstream component role is inconsistent with its stage")
    expected_upstream_types = _STAGE_UPSTREAM_TYPES[stage]
    if stage is DownstreamStage.FULL_REFERENCE_TIMELINE and len(upstream) == 8:
        expected_upstream_types += (QualifiedPerceptionResult,)
    if len(upstream) != len(expected_upstream_types) or any(
        type(value) is not expected
        for value, expected in zip(upstream, expected_upstream_types, strict=True)
    ):
        raise ContractValidationError("downstream upstream roles are inconsistent with their stage")


def build_manual_directive_authority(
    request: RawContextRequest,
    registry: ReferenceRegistry,
    constraints: HardConstraintSet,
    constraints_report: DownstreamProducerReport,
    intent: IntentGraph,
    intent_report: DownstreamProducerReport,
    *,
    action: str = "",
    target_kind: str = "subject",
    target_id: str = "",
    source_asset_id: str = "",
    retention_aspect: str = "identity",
    adaptation: str = "",
    exclusion_reason: str = "",
    authority: str = "user_preference",
    priority: int = 0,
) -> tuple[DirectiveAuthorityBundle, DownstreamProducerReport]:
    if (
        type(request) is not RawContextRequest
        or type(registry) is not ReferenceRegistry
        or type(constraints) is not HardConstraintSet
        or type(intent) is not IntentGraph
    ):
        raise ContractValidationError("directive authority inputs must be exact")
    _validate_exact_graph(request)
    _validate_exact_graph(registry)
    _validate_exact_graph(constraints)
    _validate_exact_graph(intent)
    if request.mode is not TaskMode.REF2VA:
        raise ContractValidationError("directive authority requires an exact REF2VA request")
    assert_downstream_pair(constraints_report, constraints, ())
    assert_downstream_pair(intent_report, intent, (request, registry))
    scalar_values = (
        action,
        target_kind,
        target_id,
        source_asset_id,
        retention_aspect,
        adaptation,
        exclusion_reason,
        authority,
    )
    if any(type(value) is not str for value in scalar_values):
        raise ContractValidationError("directive widget values must be exact strings")
    if type(priority) is not int:
        raise ContractValidationError("directive priority must be an exact integer")
    directives: tuple[ReferenceDirective, ...] = ()
    if action or target_id:
        if not action or not target_id:
            raise ContractValidationError("manual directive requires action and target_id")
        try:
            action_value = DirectiveAction(action)
            target_value = DirectiveTargetKind(target_kind)
            authority_value = DirectiveAuthority(authority)
            aspect_value = RetentionAspect(retention_aspect)
        except ValueError as exc:
            raise ContractValidationError("manual directive enum is unsupported") from exc
        known_targets = {
            DirectiveTargetKind.SUBJECT: {item.subject_id for item in intent.subjects},
            DirectiveTargetKind.ACTION: {item.action_id for item in intent.actions},
            DirectiveTargetKind.SCENE: {item.scene_id for item in intent.scenes},
            DirectiveTargetKind.ASSET: {item.asset_id for item in registry.assets},
        }
        if target_value not in known_targets or target_id not in known_targets[target_value]:
            raise ContractValidationError("manual directive target is not present in the intent")
        directives = (
            ReferenceDirective(
                "manual.directive.1",
                action_value,
                target_value,
                target_id,
                source_asset_ids=(source_asset_id,) if source_asset_id else (),
                retention_aspects=(aspect_value,) if action_value is DirectiveAction.RETAIN else (),
                adaptation=adaptation or None,
                reason=exclusion_reason or None,
                authority=authority_value,
                priority=priority,
            ),
        )
    resolved = resolve_reference_directives(
        DirectiveRequest(TaskMode.REF2VA, registry, directives, constraints)
    )
    authority_report = build_directive_authority_engine(resolved)
    bundle = DirectiveAuthorityBundle(resolved, authority_report)
    return bundle, _issue_report(
        DownstreamStage.DIRECTIVE_AUTHORITY,
        DownstreamDisposition.COMPLETE,
        DownstreamReasonCode.MANUAL_COMPONENT_BUILT,
        bundle,
        (request, registry, constraints, intent),
    )


def unavailable_full_reference_timeline(
    request: RawContextRequest,
    registry: ReferenceRegistry,
    media: PerceptionProducerResult,
    evidence: UnifiedEvidenceGraph,
    evidence_report: DownstreamProducerReport,
    cross_reference: CrossReferenceGraph,
    cross_reference_report: DownstreamProducerReport,
    directives: DirectiveAuthorityBundle,
    directive_report: DownstreamProducerReport,
    intent: IntentGraph,
    intent_report: DownstreamProducerReport,
    *,
    visual_result: QualifiedPerceptionResult | None = None,
) -> tuple[FullReferenceTimelineResult, DownstreamProducerReport]:
    if type(request) is not RawContextRequest or type(registry) is not ReferenceRegistry:
        raise ContractValidationError("timeline request and registry must be exact")
    _validate_exact_graph(request)
    _validate_exact_graph(registry)
    for value, field, expected in (
        (evidence, "evidence", UnifiedEvidenceGraph),
        (cross_reference, "cross_reference", CrossReferenceGraph),
        (directives, "directives", DirectiveAuthorityBundle),
        (intent, "intent", IntentGraph),
    ):
        if type(value) is not expected:
            raise ContractValidationError(f"timeline {field} must be exact")
        _validate_exact_graph(value)
    if request.mode is not TaskMode.REF2VA:
        raise ContractValidationError("Full Reference timeline requires an exact REF2VA request")
    source = _exact_media(media)
    if request.reference_registry.assets and _safe_fingerprint(
        request.reference_registry
    ) != _safe_fingerprint(registry):
        raise ContractValidationError(
            "timeline request registry does not match the supplied registry"
        )
    assets = {asset.asset_id: asset for asset in registry.assets}
    asset = assets.get(source.asset_id)
    if asset is None or asset.kind.value != source.media_kind:
        raise ContractValidationError("timeline media is not owned by the supplied registry")
    assert_downstream_pair(evidence_report, evidence, (source,))
    assert_downstream_pair(cross_reference_report, cross_reference, (registry, source))
    assert_downstream_pair(intent_report, intent, (request, registry))
    assert_downstream_pair(
        directive_report,
        directives,
        (request, registry, directives.resolved.request.hard_constraints, intent),
    )
    if tuple(item.asset_id for item in evidence.observations) != (source.asset_id,):
        raise ContractValidationError("timeline evidence does not match the admitted source")
    if cross_reference.selected_asset_ids != (source.asset_id,):
        raise ContractValidationError("timeline cross-reference does not match the admitted source")
    if visual_result is not None:
        from dataclasses import replace

        if type(visual_result) is not QualifiedPerceptionResult:
            raise ContractValidationError("timeline requires exact qualified perception")
        visual_result.assert_current(source)
        if visual_result.profile_id != VISUAL_PROFILE or source.media_kind != "video":
            raise ContractValidationError("timeline requires qualified decoded video observations")
        if (
            cross_reference.status is not CrossReferenceGraphStatus.EMPTY
            or directives.resolved.request.directives
        ):
            # CRITICAL: admission-only identities are not qualified observation IDs.
            raise ContractValidationError("automatic timeline does not infer reference directives")
        normalized = normalize_request(
            replace(
                request,
                reference_registry=registry,
                hard_constraints=directives.resolved.request.hard_constraints,
            )
        )
        if normalized.request is None:
            raise ContractValidationError("timeline request failed normalization")
        from .video_analysis import VideoAnalysisBatch

        if type(visual_result.analysis) is not VideoAnalysisBatch:
            raise ContractValidationError("qualified visual analysis must be exact")
        if visual_result.analysis.admitted_frame_count is None:
            raise ContractValidationError("qualified visual analysis lacks admitted frame metadata")
        window = reference_conditioning_window(
            normalized.request.effective_frame_count, visual_result.analysis.admitted_frame_count
        )
        if (
            visual_result.analysis.conditioning_window is not None
            and visual_result.analysis.conditioning_window != window
        ):
            raise ContractValidationError(
                "perception_window_mismatch: Run visual perception with the same request "
                "so it reads the frames the generator keeps."
            )
        qualified_cross = build_cross_reference_graph(
            CrossReferenceRequest(
                TaskMode.REF2VA,
                registry,
                tuple(
                    ObservationReference(
                        item.observation_id, source.asset_id, item.source_id, MediaKind.VIDEO
                    )
                    for item in visual_result.analysis.observations
                ),
            ),
            (),
        )
        result = plan_full_reference_timeline(
            FullReferenceTimelineRequest(
                normalized.request,
                visual_result.analysis,
                None,
                qualified_cross,
                directives.resolved,
                conditioning_window=window,
            )
        )
        if result.has_errors or result.timeline is None or not result.timeline.segments:
            raise ContractValidationError("qualified observations did not produce a valid timeline")
        visual_result.assert_current(source)
        return result, _issue_report(
            DownstreamStage.FULL_REFERENCE_TIMELINE,
            DownstreamDisposition.PARTIAL,
            DownstreamReasonCode.QUALIFIED_TIMELINE_BUILT,
            result,
            (
                request,
                registry,
                source,
                evidence,
                cross_reference,
                directives,
                intent,
                visual_result,
            ),
            "qualified_local_observation",
        )
    result = FullReferenceTimelineResult(
        None,
        None,
        (
            ValidationDiagnostic(
                ValidationSeverity.ERROR,
                DownstreamReasonCode.PERCEPTION_PROFILE_UNAVAILABLE.value,
                _REASON_MESSAGES[DownstreamReasonCode.PERCEPTION_PROFILE_UNAVAILABLE],
                "full_reference_timeline",
            ),
        ),
    )
    report = _issue_report(
        DownstreamStage.FULL_REFERENCE_TIMELINE,
        DownstreamDisposition.UNAVAILABLE,
        DownstreamReasonCode.PERCEPTION_PROFILE_UNAVAILABLE,
        None,
        (request, registry, source, evidence, cross_reference, directives, intent),
    )
    return result, report


def validate_downstream_producer_wire(
    value: object, *, component: object | None, upstream: tuple[object, ...]
) -> None:
    if type(value) is not dict:
        raise ContractValidationError("downstream wire must be an object")
    required = {
        "schema",
        "stage",
        "disposition",
        "reason_code",
        "reason",
        "component_fingerprint",
        "upstream_fingerprints",
        "provenance",
    }
    if set(value) != required:
        raise ContractValidationError("downstream wire members are not exact")
    for field in ("schema", "stage", "disposition", "reason_code", "reason", "provenance"):
        if type(value[field]) is not str:
            raise ContractValidationError(f"downstream wire {field} must be an exact string")
    try:
        stage = DownstreamStage(value["stage"])
        disposition = DownstreamDisposition(value["disposition"])
        reason = DownstreamReasonCode(value["reason_code"])
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("downstream wire enum is invalid") from exc
    if value["schema"] != DOWNSTREAM_PRODUCER_SCHEMA or value["reason"] != _REASON_MESSAGES[reason]:
        raise ContractValidationError("downstream wire schema/reason is inconsistent")
    wire_upstream = value["upstream_fingerprints"]
    if type(wire_upstream) is not list:
        raise ContractValidationError("downstream upstream_fingerprints must be an array")
    validated_upstream = tuple(
        _fingerprint(item, "upstream_fingerprints item") for item in wire_upstream
    )
    component_fingerprint = value["component_fingerprint"]
    if component_fingerprint is not None and type(component_fingerprint) is not str:
        raise ContractValidationError("component_fingerprint must be a string or null")
    provenance = value["provenance"]
    if type(provenance) is not str:
        raise ContractValidationError("provenance must be a string")
    _validate_stage_roles(stage, component, upstream)
    if len(wire_upstream) != len(upstream):
        raise ContractValidationError("downstream wire upstream inventory is inconsistent")
    expected_component = None if component is None else _safe_fingerprint(component)
    expected_upstream = tuple(_safe_fingerprint(item) for item in upstream)
    if component_fingerprint != expected_component or validated_upstream != expected_upstream:
        raise ContractValidationError("downstream wire does not join the live contract values")
    DownstreamProducerReport(
        stage,
        disposition,
        reason,
        component_fingerprint,
        validated_upstream,
        provenance,
    )


__all__ = [
    "DOWNSTREAM_PRODUCER_SCHEMA",
    "DirectiveAuthorityBundle",
    "DownstreamDisposition",
    "DownstreamProducerReport",
    "DownstreamReasonCode",
    "DownstreamStage",
    "ManualKeyframeBinding",
    "build_admission_evidence_graph",
    "build_manual_directive_authority",
    "build_manual_hard_constraints",
    "build_manual_intent",
    "build_source_cross_reference",
    "assert_downstream_pair",
    "issue_manual_component_report",
    "unavailable_full_reference_timeline",
    "validate_downstream_producer_wire",
]
