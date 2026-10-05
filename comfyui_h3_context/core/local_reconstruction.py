"""Exact local-reconstruction acceptance join for the public deterministic route."""

from __future__ import annotations

import hashlib
import json
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .constraints import HardConstraintSet
from .context_reporting import ContextPlan, ContextReport, ValidationResult, ValidationStatus
from .cross_reference_graph import CrossReferenceGraph, CrossReferenceGraphStatus
from .downstream_producer import (
    DirectiveAuthorityBundle,
    DownstreamProducerReport,
    DownstreamStage,
    _safe_fingerprint,
    _validate_exact_graph,
    assert_downstream_pair,
)
from .errors import ContractValidationError
from .intent_graph import IntentGraph
from .native_h3 import NativeH3Wiring, assert_native_h3_wiring_authority
from .normalization import RawContextRequest
from .perception_producer import PerceptionProducerResult, ProducerDisposition
from .reconstruction_stages import (
    ReconstructionReductionStage,
    ReconstructionSemanticStage,
    ReconstructionTimelineStage,
    assert_reconstruction_plan,
    assert_reconstruction_profiled,
    assert_reconstruction_reduction,
    assert_reconstruction_semantic,
    assert_reconstruction_timeline,
    assert_reconstruction_validation,
)
from .registry import ReferenceRegistry
from .source_profiled_prompt import SourceProfiledPromptResult
from .unified_evidence_graph import UnifiedEvidenceGraph

LOCAL_RECONSTRUCTION_SCHEMA = "h3-local-reconstruction/1"
MAX_LOCAL_RECONSTRUCTION_WIRE_BYTES = 32_768


class ReconstructionRoute(str, Enum):
    DETERMINISTIC_MANUAL = "deterministic_manual"
    FULL_REFERENCE_PERCEPTION = "full_reference_perception"
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"
    OFFICIAL_ORACLE = "official_oracle"
    FIXED_H3 = "fixed_h3_generation"


class ReconstructionRouteDisposition(str, Enum):
    COMPLETE = "complete"
    NOT_QUALIFIED = "not_qualified"
    NOT_AUTHORIZED = "not_authorized"


_ROUTE_DISPOSITIONS: tuple[tuple[ReconstructionRoute, ReconstructionRouteDisposition], ...] = (
    (ReconstructionRoute.DETERMINISTIC_MANUAL, ReconstructionRouteDisposition.COMPLETE),
    (
        ReconstructionRoute.FULL_REFERENCE_PERCEPTION,
        ReconstructionRouteDisposition.NOT_QUALIFIED,
    ),
    (ReconstructionRoute.COMFYUI_NATIVE, ReconstructionRouteDisposition.NOT_QUALIFIED),
    (ReconstructionRoute.OLLAMA, ReconstructionRouteDisposition.NOT_QUALIFIED),
    (ReconstructionRoute.SPECIALIST, ReconstructionRouteDisposition.NOT_QUALIFIED),
    (ReconstructionRoute.OFFICIAL_ORACLE, ReconstructionRouteDisposition.NOT_AUTHORIZED),
    (ReconstructionRoute.FIXED_H3, ReconstructionRouteDisposition.NOT_AUTHORIZED),
)

_STAGES = (
    "request",
    "reference_registry",
    "media_admission",
    "hard_constraints",
    "hard_constraints_report",
    "intent_graph",
    "intent_report",
    "evidence_graph",
    "evidence_report",
    "cross_reference_graph",
    "cross_reference_report",
    "directive_authority",
    "directive_report",
    "feasible_timeline",
    "hierarchical_reduction",
    "semantic_planning",
    "plan",
    "source_profiled_prompt",
    "validation",
    "context_report",
    "native_h3_wiring",
)


def reconstruction_route_dispositions() -> dict[
    ReconstructionRoute, ReconstructionRouteDisposition
]:
    return dict(_ROUTE_DISPOSITIONS)


def _sha(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ContractValidationError(f"{field} must be a canonical SHA-256 fingerprint")
    return value


def _exact_string(value: object, field: str, *, maximum: int = 256) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ContractValidationError(f"{field} must be a bounded exact string")
    return value


def _wire_payload(
    *,
    route: ReconstructionRoute,
    disposition: ReconstructionRouteDisposition,
    stage_fingerprints: tuple[tuple[str, str], ...],
    prompt_fingerprint: str,
    report_fingerprint: str,
    wiring_fingerprint: str,
    limitations: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema": LOCAL_RECONSTRUCTION_SCHEMA,
        "route": route.value,
        "disposition": disposition.value,
        "stage_fingerprints": [
            {"stage": stage, "fingerprint": fingerprint}
            for stage, fingerprint in stage_fingerprints
        ],
        "prompt_fingerprint": prompt_fingerprint,
        "report_fingerprint": report_fingerprint,
        "wiring_fingerprint": wiring_fingerprint,
        "limitations": list(limitations),
        "route_dispositions": [
            {"route": item_route.value, "disposition": item_disposition.value}
            for item_route, item_disposition in _ROUTE_DISPOSITIONS
        ],
    }


@dataclass(frozen=True)
class _ReconstructionAuthority:
    digest: str
    evidence: tuple[object, ...]


# CRITICAL: keep the standard layout; Python 3.10 has no weakref_slot and this value is
# retained by a weak-reference registry.
@dataclass(frozen=True, eq=False)
class LocalReconstructionResult:
    route: ReconstructionRoute
    disposition: ReconstructionRouteDisposition
    stage_fingerprints: tuple[tuple[str, str], ...]
    prompt_fingerprint: str
    report_fingerprint: str
    wiring_fingerprint: str
    limitations: tuple[str, ...]
    run_fingerprint: str
    schema: str = LOCAL_RECONSTRUCTION_SCHEMA

    def __post_init__(self) -> None:
        if type(self) is not LocalReconstructionResult:
            raise ContractValidationError("local reconstruction result must be exact")
        if type(self.schema) is not str or self.schema != LOCAL_RECONSTRUCTION_SCHEMA:
            raise ContractValidationError("local reconstruction schema is unsupported")
        if type(self.route) is not ReconstructionRoute:
            raise ContractValidationError("local reconstruction route must be exact")
        if type(self.disposition) is not ReconstructionRouteDisposition:
            raise ContractValidationError("local reconstruction disposition must be exact")
        if (
            self.route is not ReconstructionRoute.DETERMINISTIC_MANUAL
            or self.disposition is not ReconstructionRouteDisposition.COMPLETE
        ):
            raise ContractValidationError("only the deterministic manual route may be complete")
        if type(self.stage_fingerprints) is not tuple:
            raise ContractValidationError("local reconstruction stage inventory is not canonical")
        for item in self.stage_fingerprints:
            if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str:
                raise ContractValidationError("local reconstruction stage entry is malformed")
            _sha(item[1], f"stage {item[0]} fingerprint")
        if tuple(item[0] for item in self.stage_fingerprints) != _STAGES:
            raise ContractValidationError("local reconstruction stage inventory is not canonical")
        _sha(self.prompt_fingerprint, "prompt_fingerprint")
        _sha(self.report_fingerprint, "report_fingerprint")
        _sha(self.wiring_fingerprint, "wiring_fingerprint")
        if type(self.limitations) is not tuple or len(self.limitations) > 16:
            raise ContractValidationError("local reconstruction limitations are malformed")
        for limitation in self.limitations:
            _exact_string(limitation, "limitation")
        _sha(self.run_fingerprint, "run_fingerprint")

    @property
    def is_complete(self) -> bool:
        _assert_result_current(self)
        return True

    def to_wire(self) -> dict[str, object]:
        _assert_result_current(self)
        payload = _wire_payload(
            route=self.route,
            disposition=self.disposition,
            stage_fingerprints=self.stage_fingerprints,
            prompt_fingerprint=self.prompt_fingerprint,
            report_fingerprint=self.report_fingerprint,
            wiring_fingerprint=self.wiring_fingerprint,
            limitations=self.limitations,
        )
        payload["run_fingerprint"] = self.run_fingerprint
        if len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")) > (
            MAX_LOCAL_RECONSTRUCTION_WIRE_BYTES
        ):
            raise ContractValidationError("local reconstruction wire exceeds its bound")
        return payload


_TRUSTED_RESULTS: weakref.WeakKeyDictionary[LocalReconstructionResult, _ReconstructionAuthority] = (
    weakref.WeakKeyDictionary()
)


def _result_digest(result: LocalReconstructionResult) -> str:
    return canonical_fingerprint(
        _wire_payload(
            route=result.route,
            disposition=result.disposition,
            stage_fingerprints=result.stage_fingerprints,
            prompt_fingerprint=result.prompt_fingerprint,
            report_fingerprint=result.report_fingerprint,
            wiring_fingerprint=result.wiring_fingerprint,
            limitations=result.limitations,
        )
    )


_LATE_STAGE_TYPES: tuple[type[object], ...] = (
    ContextPlan,
    SourceProfiledPromptResult,
    ValidationResult,
    ContextReport,
    NativeH3Wiring,
)
_RECONSTRUCTION_STAGE_TYPES: tuple[type[object], ...] = (
    ReconstructionTimelineStage,
    ReconstructionReductionStage,
    ReconstructionSemanticStage,
)


def _late_stage_fingerprint(value: object, expected: type[object]) -> str:
    if type(value) is not expected:
        raise ContractValidationError("local reconstruction stage type is not exact")
    post_init = expected.__dict__.get("__post_init__")
    to_wire = expected.__dict__.get("to_wire")
    try:
        if post_init is not None:
            post_init(value)
        if to_wire is None:
            raise ContractValidationError("local reconstruction stage is not serializable")
        wire = to_wire(value)
    except ContractValidationError:
        raise
    except Exception as exc:
        raise ContractValidationError("local reconstruction stage validation failed") from exc
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(wire, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()
    )


def _evidence_fingerprints(evidence: tuple[object, ...]) -> tuple[str, ...]:
    expected_types: tuple[type[object], ...] = (
        RawContextRequest,
        ReferenceRegistry,
        PerceptionProducerResult,
        HardConstraintSet,
        DownstreamProducerReport,
        IntentGraph,
        DownstreamProducerReport,
        UnifiedEvidenceGraph,
        DownstreamProducerReport,
        CrossReferenceGraph,
        DownstreamProducerReport,
        DirectiveAuthorityBundle,
        DownstreamProducerReport,
        ReconstructionTimelineStage,
        ReconstructionReductionStage,
        ReconstructionSemanticStage,
        *_LATE_STAGE_TYPES,
    )
    fingerprints: list[str] = []
    for item, expected in zip(evidence, expected_types, strict=True):
        if expected is DownstreamProducerReport:
            if type(item) is not DownstreamProducerReport:
                raise ContractValidationError("producer report type is not exact")
            try:
                wire = DownstreamProducerReport.to_wire(item)
            except ContractValidationError:
                raise
            except Exception as exc:
                raise ContractValidationError("producer report validation failed") from exc
            fingerprints.append(canonical_fingerprint(wire))
        elif expected in _LATE_STAGE_TYPES:
            fingerprints.append(_late_stage_fingerprint(item, expected))
        elif expected in _RECONSTRUCTION_STAGE_TYPES:
            if type(item) is not expected:
                raise ContractValidationError("reconstruction stage type is not exact")
            fingerprints.append(canonical_fingerprint(expected.to_wire(item)))  # type: ignore[attr-defined]
        else:
            fingerprints.append(_safe_fingerprint(item))
    return tuple(fingerprints)


def _assert_result_current(value: object) -> LocalReconstructionResult:
    if type(value) is not LocalReconstructionResult:
        raise ContractValidationError("local reconstruction result must be exact")
    result = value
    result.__post_init__()
    authority = _TRUSTED_RESULTS.get(result)
    if authority is None or authority.digest != _result_digest(result):
        raise ContractValidationError("local reconstruction result is forged, copied, or stale")
    if _evidence_fingerprints(authority.evidence) != tuple(
        fingerprint for _, fingerprint in result.stage_fingerprints
    ):
        raise ContractValidationError("local reconstruction evidence changed after issuance")
    timeline = authority.evidence[13]
    reduction = authority.evidence[14]
    semantic = authority.evidence[15]
    plan = authority.evidence[16]
    profiled = authority.evidence[17]
    validation = authority.evidence[18]
    report = authority.evidence[19]
    assert_reconstruction_timeline(timeline)
    assert_reconstruction_reduction(reduction)
    assert_reconstruction_semantic(semantic, cast(ContextPlan, plan))
    assert_reconstruction_plan(plan)
    assert_reconstruction_profiled(profiled, cast(ContextPlan, plan))
    assert_reconstruction_validation(
        cast(ContextPlan, plan),
        cast(SourceProfiledPromptResult, profiled),
        validation,
        report,
    )
    return result


def issue_local_reconstruction_result(
    request: RawContextRequest,
    registry: ReferenceRegistry,
    media: PerceptionProducerResult,
    constraints: HardConstraintSet,
    constraints_report: DownstreamProducerReport,
    intent: IntentGraph,
    intent_report: DownstreamProducerReport,
    evidence_graph: UnifiedEvidenceGraph,
    evidence_report: DownstreamProducerReport,
    cross_graph: CrossReferenceGraph,
    cross_report: DownstreamProducerReport,
    directives: DirectiveAuthorityBundle,
    directive_report: DownstreamProducerReport,
    feasible_timeline: ReconstructionTimelineStage,
    hierarchical_reduction: ReconstructionReductionStage,
    semantic_planning: ReconstructionSemanticStage,
    plan: ContextPlan,
    profiled: SourceProfiledPromptResult,
    validation: ValidationResult,
    report: ContextReport,
    wiring: NativeH3Wiring,
) -> LocalReconstructionResult:
    roots = (
        request,
        registry,
        media,
        constraints,
        intent,
        evidence_graph,
        cross_graph,
        directives,
        feasible_timeline,
        hierarchical_reduction,
        semantic_planning,
        plan,
        profiled,
        validation,
        report,
        wiring,
    )
    for root in roots[:8]:
        _validate_exact_graph(root)
    assert_reconstruction_timeline(feasible_timeline, request, registry, intent, directives)
    assert_reconstruction_reduction(hierarchical_reduction, evidence_graph, feasible_timeline)
    assert_reconstruction_semantic(semantic_planning, plan)
    assert_reconstruction_plan(plan)
    assert_reconstruction_profiled(profiled, plan)
    assert_reconstruction_validation(plan, profiled, validation, report)
    for late_root, expected in zip(roots[11:], _LATE_STAGE_TYPES, strict=True):
        _late_stage_fingerprint(late_root, expected)
    for producer_report in (
        constraints_report,
        intent_report,
        evidence_report,
        cross_report,
        directive_report,
    ):
        if type(producer_report) is not DownstreamProducerReport:
            raise ContractValidationError("producer report type is not exact")
        DownstreamProducerReport.to_wire(producer_report)
    if media.disposition is not ProducerDisposition.COMPLETE:
        raise ContractValidationError("local reconstruction requires admitted host media")
    if request.reference_registry.assets:
        raise ContractValidationError("request-owned and explicit registries cannot both be set")
    if intent.registry is not registry or plan.request.reference_registry is not registry:
        raise ContractValidationError("request, intent, plan, and registry do not join")
    if plan.intent_graph.registry is not registry or plan.hard_constraints is not constraints:
        raise ContractValidationError("plan does not retain the accepted manual authority")
    expected_report_stages = (
        (constraints_report, DownstreamStage.HARD_CONSTRAINT, constraints, ()),
        (intent_report, DownstreamStage.INTENT_GRAPH, intent, (request, registry)),
        (evidence_report, DownstreamStage.EVIDENCE_FUSION, evidence_graph, (media,)),
        (cross_report, DownstreamStage.CROSS_REFERENCE, cross_graph, (registry, media)),
        (
            directive_report,
            DownstreamStage.DIRECTIVE_AUTHORITY,
            directives,
            (request, registry, constraints, intent),
        ),
    )
    for producer_report, stage, component, upstream in expected_report_stages:
        if producer_report.stage is not stage or producer_report.component_fingerprint != (
            _safe_fingerprint(component)
        ):
            raise ContractValidationError("producer report does not authorize its exact component")
        assert_downstream_pair(producer_report, component, upstream)
    if not profiled.is_valid or profiled.document.plan_id != plan.plan_id:
        raise ContractValidationError(
            "source-profiled prompt is invalid or belongs to another plan"
        )
    if validation.status is not ValidationStatus.PASSED:
        raise ContractValidationError("local reconstruction requires passed validation")
    if (
        report.plan is not plan
        or report.prompt_document is not profiled.document
        or report.validation is not validation
        or wiring.report_id != report.report_id
        or wiring.prompt != profiled.document.text
        or wiring.validation_status is not ValidationStatus.PASSED
    ):
        raise ContractValidationError("validated report and native wiring do not join")
    assert_native_h3_wiring_authority(wiring, report)
    evidence = (
        request,
        registry,
        media,
        constraints,
        constraints_report,
        intent,
        intent_report,
        evidence_graph,
        evidence_report,
        cross_graph,
        cross_report,
        directives,
        directive_report,
        feasible_timeline,
        hierarchical_reduction,
        semantic_planning,
        plan,
        profiled,
        validation,
        report,
        wiring,
    )
    stage_fingerprints = tuple(zip(_STAGES, _evidence_fingerprints(evidence), strict=True))
    prompt_fingerprint = canonical_fingerprint(profiled.document.text)
    report_fingerprint = _late_stage_fingerprint(report, ContextReport)
    wiring_fingerprint = _late_stage_fingerprint(wiring, NativeH3Wiring)
    limitations = (
        "caller_declared_media_fingerprint_unverified",
        "perception_profiles_not_qualified",
        "embedded_media_instructions_are_inert",
        "no_official_or_generation_quality_claim",
    ) + (
        ("cross_reference_ambiguity_retained",)
        if cross_graph.status is CrossReferenceGraphStatus.AMBIGUOUS
        else ()
    )
    provisional = LocalReconstructionResult(
        ReconstructionRoute.DETERMINISTIC_MANUAL,
        ReconstructionRouteDisposition.COMPLETE,
        stage_fingerprints,
        prompt_fingerprint,
        report_fingerprint,
        wiring_fingerprint,
        limitations,
        "sha256:" + "0" * 64,
    )
    run_fingerprint = _result_digest(provisional)
    result = LocalReconstructionResult(
        provisional.route,
        provisional.disposition,
        provisional.stage_fingerprints,
        provisional.prompt_fingerprint,
        provisional.report_fingerprint,
        provisional.wiring_fingerprint,
        provisional.limitations,
        run_fingerprint,
    )
    _TRUSTED_RESULTS[result] = _ReconstructionAuthority(_result_digest(result), evidence)
    return result


def validate_local_reconstruction_wire(value: object) -> None:
    if type(value) is not dict:
        raise ContractValidationError("local reconstruction wire must be an exact object")
    wire = cast(dict[object, object], value)
    expected_keys = {
        "schema",
        "route",
        "disposition",
        "stage_fingerprints",
        "prompt_fingerprint",
        "report_fingerprint",
        "wiring_fingerprint",
        "limitations",
        "route_dispositions",
        "run_fingerprint",
    }
    if any(type(key) is not str for key in wire) or set(wire) != expected_keys:
        raise ContractValidationError("local reconstruction wire members are not closed")
    if _exact_string(wire["schema"], "schema") != LOCAL_RECONSTRUCTION_SCHEMA:
        raise ContractValidationError("local reconstruction wire schema is unsupported")
    try:
        route = ReconstructionRoute(_exact_string(wire["route"], "route"))
        disposition = ReconstructionRouteDisposition(
            _exact_string(wire["disposition"], "disposition")
        )
    except ValueError as exc:
        raise ContractValidationError("local reconstruction route state is unsupported") from exc
    raw_stages = wire["stage_fingerprints"]
    if type(raw_stages) is not list or len(cast(list[object], raw_stages)) != len(_STAGES):
        raise ContractValidationError("local reconstruction stage inventory is malformed")
    stages: list[tuple[str, str]] = []
    for index, item in enumerate(cast(list[object], raw_stages)):
        if type(item) is not dict:
            raise ContractValidationError("local reconstruction stage entry is malformed")
        item_object = cast(dict[object, object], item)
        if any(type(key) is not str for key in item_object) or set(item_object) != {
            "stage",
            "fingerprint",
        }:
            raise ContractValidationError("local reconstruction stage entry is malformed")
        stage = _exact_string(item_object["stage"], "stage")
        fingerprint = _sha(item_object["fingerprint"], "stage fingerprint")
        if stage != _STAGES[index]:
            raise ContractValidationError("local reconstruction stage order is not canonical")
        stages.append((stage, fingerprint))
    raw_limitations = wire["limitations"]
    if type(raw_limitations) is not list or len(cast(list[object], raw_limitations)) > 16:
        raise ContractValidationError("local reconstruction limitations are malformed")
    limitations = tuple(
        _exact_string(item, "limitation") for item in cast(list[object], raw_limitations)
    )
    expected_route_wire = [
        {"route": item_route.value, "disposition": item_disposition.value}
        for item_route, item_disposition in _ROUTE_DISPOSITIONS
    ]
    raw_routes = wire["route_dispositions"]
    if type(raw_routes) is not list or len(cast(list[object], raw_routes)) != len(
        expected_route_wire
    ):
        raise ContractValidationError("local reconstruction route matrix is not canonical")
    normalized_routes: list[dict[str, str]] = []
    for item in cast(list[object], raw_routes):
        if type(item) is not dict:
            raise ContractValidationError("local reconstruction route matrix is not canonical")
        route_item = cast(dict[object, object], item)
        if any(type(key) is not str for key in route_item) or set(route_item) != {
            "route",
            "disposition",
        }:
            raise ContractValidationError("local reconstruction route matrix is not canonical")
        normalized_routes.append(
            {
                "route": _exact_string(route_item["route"], "route matrix route"),
                "disposition": _exact_string(route_item["disposition"], "route matrix disposition"),
            }
        )
    if normalized_routes != expected_route_wire:
        raise ContractValidationError("local reconstruction route matrix is not canonical")
    payload = _wire_payload(
        route=route,
        disposition=disposition,
        stage_fingerprints=tuple(stages),
        prompt_fingerprint=_sha(wire["prompt_fingerprint"], "prompt_fingerprint"),
        report_fingerprint=_sha(wire["report_fingerprint"], "report_fingerprint"),
        wiring_fingerprint=_sha(wire["wiring_fingerprint"], "wiring_fingerprint"),
        limitations=limitations,
    )
    if _sha(wire["run_fingerprint"], "run_fingerprint") != canonical_fingerprint(payload):
        raise ContractValidationError("local reconstruction run fingerprint does not match")
    if route is not ReconstructionRoute.DETERMINISTIC_MANUAL or disposition is not (
        ReconstructionRouteDisposition.COMPLETE
    ):
        raise ContractValidationError("local reconstruction success route is contradictory")
    if len(json.dumps(wire, sort_keys=True, separators=(",", ":")).encode("utf-8")) > (
        MAX_LOCAL_RECONSTRUCTION_WIRE_BYTES
    ):
        raise ContractValidationError("local reconstruction wire exceeds its bound")


__all__ = [
    "LOCAL_RECONSTRUCTION_SCHEMA",
    "LocalReconstructionResult",
    "ReconstructionRoute",
    "ReconstructionRouteDisposition",
    "issue_local_reconstruction_result",
    "reconstruction_route_dispositions",
    "validate_local_reconstruction_wire",
]
