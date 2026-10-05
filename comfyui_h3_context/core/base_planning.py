"""Deterministic Base-mode timeline planning over the accepted pure-core contracts.

The planner maps declared user intent, canonical frame-reference ownership, and optional M5-02
observed evidence into a small typed shot vector. It never decodes media, infers semantics with a
model, rewrites hard constraints, or selects a provider. Every returned graph is validated before
it is wrapped in a renderable :class:`ContextPlan`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal

from .canonical import canonical_fingerprint
from .constraints import TimePoint
from .context_reporting import ContextPlan, Limitation, PlanStage, PlanStep, PlanStepStatus
from .contracts import (
    AssetRole,
    PromptProfile,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import BasePlanningError, ContractValidationError
from .evidence import EvidenceSet, merge_evidence
from .image_observation import ImageObservationBatch, ImageObservationBatchStatus
from .intent_graph import IntentGraph, IntentScene, TimelineSegment, build_intent_graph
from .normalization import NormalizedContextRequest, RawContextRequest, normalize_request

BASE_TIMELINE_PLAN_SCHEMA = "h3.base.timeline.plan.v1"
MAX_BASE_SHOTS = 64
MAX_BASE_ASSET_IDS = 9
MAX_BASE_EVIDENCE_IDS = 256
MAX_BASE_SEED = (2**63) - 1
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_BASE_MODES = frozenset({TaskMode.T2VA, TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA})


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise BasePlanningError(f"{field_name} must be a bounded identifier")
    return value


def _id_tuple(value: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise BasePlanningError(f"{field_name} must be a tuple of at most {maximum} identifiers")
    values = tuple(_identifier(item, f"{field_name} item") for item in value)
    if len(values) != len(set(values)):
        raise BasePlanningError(f"{field_name} must not contain duplicate identifiers")
    return values


def _optional_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field_name)


@dataclass(frozen=True, slots=True)
class BasePlanningConfig:
    """Explicit deterministic planning settings; no hidden stochastic fallback exists."""

    deterministic: bool = True
    seed: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.deterministic, bool):
            raise BasePlanningError("deterministic must be a boolean")
        if not self.deterministic:
            raise BasePlanningError("non-deterministic Base planning is not implemented")
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not -(2**63) <= self.seed <= MAX_BASE_SEED
        ):
            raise BasePlanningError("seed must be a signed 64-bit integer")

    def to_wire(self) -> dict[str, object]:
        return {"deterministic": self.deterministic, "seed": self.seed}


@dataclass(frozen=True, slots=True)
class BaseShot:
    """One deterministic half-open Base-mode shot with explicit ownership metadata."""

    shot_id: str
    start: TimePoint
    end: TimePoint
    scene_id: str
    subject_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    camera_id: str | None = None
    style_id: str | None = None
    audio_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.shot_id, "shot_id")
        if not isinstance(self.start, TimePoint) or not isinstance(self.end, TimePoint):
            raise BasePlanningError("shot bounds must be TimePoint values")
        if self.end.seconds <= self.start.seconds:
            raise BasePlanningError("shot end must be after shot start")
        _identifier(self.scene_id, "scene_id")
        _id_tuple(self.subject_ids, "subject_ids", MAX_BASE_ASSET_IDS)
        _id_tuple(self.action_ids, "action_ids", MAX_BASE_ASSET_IDS)
        _optional_identifier(self.camera_id, "camera_id")
        _optional_identifier(self.style_id, "style_id")
        _id_tuple(self.audio_ids, "audio_ids", MAX_BASE_ASSET_IDS)
        _id_tuple(self.asset_ids, "asset_ids", MAX_BASE_ASSET_IDS)
        _id_tuple(self.evidence_ids, "evidence_ids", MAX_BASE_EVIDENCE_IDS)

    def to_wire(self) -> dict[str, object]:
        return {
            "shot_id": self.shot_id,
            "start": self.start.to_wire(),
            "end": self.end.to_wire(),
            "scene_id": self.scene_id,
            "subject_ids": list(self.subject_ids),
            "action_ids": list(self.action_ids),
            "camera_id": self.camera_id,
            "style_id": self.style_id,
            "audio_ids": list(self.audio_ids),
            "asset_ids": list(self.asset_ids),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class BaseTimelinePlan:
    """Validated deterministic shot vector consumed by the Base plan/compiler boundary."""

    timeline_id: str
    task_mode: TaskMode
    effective_duration: TimePoint
    shots: tuple[BaseShot, ...]
    config: BasePlanningConfig = BasePlanningConfig()
    evidence_ids: tuple[str, ...] = ()
    schema: str = BASE_TIMELINE_PLAN_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.timeline_id, "timeline_id")
        if self.schema != BASE_TIMELINE_PLAN_SCHEMA:
            raise BasePlanningError("unsupported Base timeline plan schema")
        if not isinstance(self.task_mode, TaskMode) or self.task_mode not in _BASE_MODES:
            raise BasePlanningError("Base timeline planning supports only Base task modes")
        if not isinstance(self.effective_duration, TimePoint):
            raise BasePlanningError("effective_duration must be a TimePoint")
        if self.effective_duration.seconds <= 0:
            raise BasePlanningError("effective_duration must be positive")
        if (
            not isinstance(self.shots, tuple)
            or not self.shots
            or len(self.shots) > MAX_BASE_SHOTS
            or not all(isinstance(shot, BaseShot) for shot in self.shots)
        ):
            raise BasePlanningError("shots must contain one to sixty-four BaseShot values")
        if not isinstance(self.config, BasePlanningConfig):
            raise BasePlanningError("config must be a BasePlanningConfig")
        evidence_ids = _id_tuple(self.evidence_ids, "evidence_ids", MAX_BASE_EVIDENCE_IDS)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise BasePlanningError("evidence_ids must be unique")
        previous: BaseShot | None = None
        for shot in self.shots:
            if shot.start.seconds < 0 or shot.end.seconds > self.effective_duration.seconds:
                raise BasePlanningError("shot lies outside the effective duration")
            if previous is not None:
                if shot.start.seconds < previous.end.seconds:
                    raise BasePlanningError("shots must not overlap")
                if shot.start.seconds > previous.end.seconds:
                    raise BasePlanningError("shots must cover the timeline without gaps")
            if not set(shot.evidence_ids).issubset(evidence_ids):
                raise BasePlanningError("shot references evidence outside the plan")
            previous = shot
        if self.shots[0].start.seconds != 0:
            raise BasePlanningError("first shot must start at zero")
        if self.shots[-1].end.seconds != self.effective_duration.seconds:
            raise BasePlanningError("last shot must end at effective duration")

    @property
    def fingerprint(self) -> str:
        """Return the canonical identity of the deterministic typed timeline."""

        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "timeline_id": self.timeline_id,
            "task_mode": self.task_mode.value,
            "effective_duration": self.effective_duration.to_wire(),
            "shots": [shot.to_wire() for shot in self.shots],
            "evidence_ids": list(self.evidence_ids),
            "config": self.config.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class BasePlanningResult:
    """Planner output; failures never carry a renderable plan or plausible success."""

    timeline: BaseTimelinePlan | None
    plan: ContextPlan | None
    limitations: tuple[Limitation, ...] = ()
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.timeline is None and self.plan is not None:
            raise BasePlanningError("a ContextPlan requires a BaseTimelinePlan")
        if self.timeline is not None and not isinstance(self.timeline, BaseTimelinePlan):
            raise BasePlanningError("timeline must be a BaseTimelinePlan or None")
        if self.plan is not None and not isinstance(self.plan, ContextPlan):
            raise BasePlanningError("plan must be a ContextPlan or None")
        if not isinstance(self.limitations, tuple) or not all(
            isinstance(item, Limitation) for item in self.limitations
        ):
            raise BasePlanningError("limitations must be a tuple of Limitation values")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, ValidationDiagnostic) for item in self.diagnostics
        ):
            raise BasePlanningError("diagnostics must be a tuple of ValidationDiagnostic values")

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
            "limitations": [item.to_wire() for item in self.limitations],
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


def _diagnostic(code: str, message: str) -> ValidationDiagnostic:
    return ValidationDiagnostic(ValidationSeverity.ERROR, code, message)


def _limitation(limitation_id: str, code: str, message: str) -> Limitation:
    return Limitation(limitation_id, code, message)


def _failure(*diagnostics: ValidationDiagnostic) -> BasePlanningResult:
    return BasePlanningResult(None, None, diagnostics=tuple(diagnostics))


def _normalize(
    value: RawContextRequest | NormalizedContextRequest,
) -> tuple[NormalizedContextRequest | None, tuple[ValidationDiagnostic, ...]]:
    if isinstance(value, NormalizedContextRequest):
        return value, ()
    if not isinstance(value, RawContextRequest):
        raise BasePlanningError(
            "plan_base_timeline requires a RawContextRequest or NormalizedContextRequest"
        )
    result = normalize_request(value)
    return result.request, result.diagnostics


def _validate_observations(
    request: NormalizedContextRequest,
    observations: ImageObservationBatch | None,
) -> tuple[EvidenceSet, tuple[Limitation, ...], tuple[ValidationDiagnostic, ...]]:
    if observations is None:
        return (
            request.evidence,
            (
                _limitation(
                    "limitation_manual_semantics",
                    "manual_semantics",
                    "No observation batch was supplied; semantic detail remains the declared "
                    "user intent.",
                ),
            ),
            (),
        )
    if not isinstance(observations, ImageObservationBatch):
        raise BasePlanningError("observations must be an ImageObservationBatch or None")
    assets = {asset.asset_id: asset for asset in request.reference_registry.assets}
    selected = set(observations.selected_asset_ids)
    if any(asset_id not in assets for asset_id in selected):
        return (
            request.evidence,
            (),
            (
                _diagnostic(
                    "observation_asset_unowned",
                    "observation batch contains an asset outside the request registry",
                ),
            ),
        )
    if any(assets[asset_id].kind.value != "image" for asset_id in selected):
        return (
            request.evidence,
            (),
            (
                _diagnostic(
                    "observation_asset_not_image",
                    "Base timeline observations may select only image assets",
                ),
            ),
        )
    if observations.status in {
        ImageObservationBatchStatus.CORRUPT,
        ImageObservationBatchStatus.UNSUPPORTED,
    }:
        return (
            request.evidence,
            (),
            (
                _diagnostic(
                    "observation_batch_unusable",
                    "corrupt or unsupported observations cannot produce a Base plan",
                ),
            ),
        )
    records = tuple(item.evidence for item in observations.observations) + tuple(
        item.evidence for item in observations.visible_text
    )
    try:
        merged = merge_evidence(request.evidence, EvidenceSet(records))
    except ContractValidationError as exc:
        return request.evidence, (), (_diagnostic("observation_evidence_conflict", str(exc)),)
    limitations: list[Limitation] = []
    if observations.status is ImageObservationBatchStatus.PARTIAL:
        limitations.append(
            _limitation(
                "limitation_partial_observation",
                "partial_observation",
                "The observation batch is partial; unobserved image regions remain unknown.",
            )
        )
    if observations.status is ImageObservationBatchStatus.EMPTY:
        limitations.append(
            _limitation(
                "limitation_empty_observation",
                "empty_observation",
                "The observation batch is empty; no visual claim was added to the plan.",
            )
        )
    if not records:
        limitations.append(
            _limitation(
                "limitation_no_observation_evidence",
                "no_observation_evidence",
                "No observation evidence is available for semantic enrichment.",
            )
        )
    return merged, tuple(limitations), ()


def _asset_for_role(request: NormalizedContextRequest, role: AssetRole) -> str | None:
    matches = [asset.asset_id for asset in request.reference_registry.assets if asset.role is role]
    return matches[0] if len(matches) == 1 else None


def _build_shots(
    request: NormalizedContextRequest,
    duration: TimePoint,
    evidence: EvidenceSet,
) -> tuple[BaseShot, ...]:
    first = _asset_for_role(request, AssetRole.FIRST_FRAME)
    last = _asset_for_role(request, AssetRole.LAST_FRAME)
    evidence_by_asset: dict[str, list[str]] = {}
    for record in evidence.records:
        asset_id = record.provenance.source.asset_id
        if asset_id is not None:
            evidence_by_asset.setdefault(asset_id, []).append(record.evidence_id)

    def evidence_for(assets: tuple[str, ...]) -> tuple[str, ...]:
        values: list[str] = []
        for asset_id in assets:
            values.extend(evidence_by_asset.get(asset_id, ()))
        return tuple(dict.fromkeys(values))

    if request.task_mode is TaskMode.FL2VA:
        if first is None or last is None:
            raise BasePlanningError("fl2va requires one first and one last frame asset")
        midpoint = duration.seconds / Decimal(2)
        middle = TimePoint.from_text(format(midpoint, "f"))
        return (
            BaseShot(
                "shot_1",
                TimePoint.from_text("0"),
                middle,
                "scene_1",
                asset_ids=(first,),
                evidence_ids=evidence_for((first,)),
            ),
            BaseShot(
                "shot_2",
                middle,
                duration,
                "scene_1",
                asset_ids=(last,),
                evidence_ids=evidence_for((last,)),
            ),
        )
    assets: tuple[str, ...] = ()
    if request.task_mode is TaskMode.I2VA:
        if first is None:
            raise BasePlanningError("i2va requires one first frame asset")
        assets = (first,)
    elif request.task_mode is TaskMode.L2VA:
        if last is None:
            raise BasePlanningError("l2va requires one last frame asset")
        assets = (last,)
    return (
        BaseShot(
            "shot_1",
            TimePoint.from_text("0"),
            duration,
            "scene_1",
            asset_ids=assets,
            evidence_ids=evidence_for(assets),
        ),
    )


def _plan_id(
    request: NormalizedContextRequest,
    timeline: BaseTimelinePlan,
    evidence: EvidenceSet,
) -> str:
    material = {
        "schema": BASE_TIMELINE_PLAN_SCHEMA,
        "task_mode": request.task_mode.value,
        "user_intent": request.user_intent,
        "registry": request.reference_registry.to_wire(),
        "constraints": request.hard_constraints.to_wire(),
        "evidence": evidence.to_wire(),
        "timeline": timeline.to_wire(),
    }
    return "plan_" + canonical_fingerprint(material).split(":", 1)[1][:32]


def plan_base_timeline(
    request: RawContextRequest | NormalizedContextRequest,
    observations: ImageObservationBatch | None = None,
    *,
    config: BasePlanningConfig | None = None,
) -> BasePlanningResult:
    """Build and validate a deterministic Base-mode timeline and renderable context plan."""

    config_value = BasePlanningConfig() if config is None else config
    if not isinstance(config_value, BasePlanningConfig):
        raise BasePlanningError("config must be a BasePlanningConfig or None")
    if isinstance(request, RawContextRequest) and request.mode in {
        TaskMode.REF2VA,
        TaskMode.REF2VA.value,
    }:
        return _failure(
            _diagnostic(
                "unsupported_base_mode",
                "Base timeline planning does not support ref2va",
            )
        )
    normalized, normalization_diagnostics = _normalize(request)
    if normalized is None:
        return _failure(*normalization_diagnostics)
    if normalized.task_mode not in _BASE_MODES:
        return _failure(
            _diagnostic(
                "unsupported_base_mode",
                f"Base timeline planning does not support {normalized.task_mode.value}",
            )
        )
    if normalized.profile.name is not PromptProfile.BASE:
        return _failure(_diagnostic("unsupported_base_profile", "request profile is not h3_base"))
    try:
        evidence, limitations, observation_diagnostics = _validate_observations(
            normalized, observations
        )
    except BasePlanningError:
        raise
    if observation_diagnostics:
        return _failure(*observation_diagnostics)
    duration = TimePoint.from_text(str(normalized.effective_duration_seconds))
    try:
        shots = _build_shots(normalized, duration, evidence)
        timeline = BaseTimelinePlan(
            timeline_id="timeline_pending",
            task_mode=normalized.task_mode,
            effective_duration=duration,
            shots=shots,
            config=config_value,
            evidence_ids=tuple(record.evidence_id for record in evidence.records),
        )
    except (BasePlanningError, ContractValidationError) as exc:
        return _failure(_diagnostic("timeline_construction_failed", str(exc)))
    plan_id = _plan_id(normalized, timeline, evidence)
    timeline = replace(timeline, timeline_id=plan_id.replace("plan_", "timeline_", 1))
    scene = IntentScene("scene_1", normalized.user_intent)
    segments = tuple(
        TimelineSegment(
            shot.shot_id,
            shot.start,
            shot.end,
            scene_id=shot.scene_id,
            subject_ids=shot.subject_ids,
            action_ids=shot.action_ids,
            camera_id=shot.camera_id,
            style_id=shot.style_id,
            audio_ids=shot.audio_ids,
        )
        for shot in timeline.shots
    )
    graph_result = build_intent_graph(
        effective_duration=duration,
        registry=normalized.reference_registry,
        scenes=(scene,),
        segments=segments,
    )
    if graph_result.graph is None:
        return _failure(*graph_result.diagnostics)
    graph: IntentGraph = graph_result.graph
    plan_request = replace(normalized, evidence=evidence)
    steps = (
        PlanStep(
            "step_normalize",
            PlanStage.NORMALIZE,
            PlanStepStatus.COMPLETED,
            "normalize Base-mode request",
            output_ids=(plan_request.task_mode.value,),
        ),
        PlanStep(
            "step_bind_references",
            PlanStage.BIND_REFERENCES,
            PlanStepStatus.COMPLETED,
            "bind canonical image/frame ownership",
            input_evidence_ids=tuple(record.evidence_id for record in evidence.records),
            output_ids=tuple(asset.asset_id for asset in normalized.reference_registry.assets),
        ),
        PlanStep(
            "step_assemble_intent",
            PlanStage.ASSEMBLE_INTENT,
            PlanStepStatus.COMPLETED,
            "assemble deterministic Base scenes and shots",
            input_evidence_ids=tuple(record.evidence_id for record in evidence.records),
            output_ids=tuple(shot.shot_id for shot in timeline.shots),
        ),
        PlanStep(
            "step_validate",
            PlanStage.VALIDATE,
            PlanStepStatus.COMPLETED,
            "validate timeline before Base rendering",
            output_ids=(timeline.timeline_id,),
        ),
    )
    plan = ContextPlan(
        plan_id=plan_id,
        schema_version=plan_request.schema_version,
        request=plan_request,
        intent_graph=graph,
        hard_constraints=plan_request.hard_constraints,
        evidence=plan_request.evidence,
        steps=steps,
        limitations=limitations,
        diagnostics=tuple(normalization_diagnostics),
    )
    return BasePlanningResult(
        timeline=timeline,
        plan=plan,
        limitations=limitations,
        diagnostics=tuple(normalization_diagnostics),
    )


__all__ = [
    "BASE_TIMELINE_PLAN_SCHEMA",
    "MAX_BASE_ASSET_IDS",
    "MAX_BASE_EVIDENCE_IDS",
    "MAX_BASE_SEED",
    "MAX_BASE_SHOTS",
    "BasePlanningConfig",
    "BasePlanningResult",
    "BaseShot",
    "BaseTimelinePlan",
    "plan_base_timeline",
]
