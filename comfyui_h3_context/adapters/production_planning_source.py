"""Server-owned bridge from one live Context workspace to Production planning."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..core.canonical import binary64_token, canonical_fingerprint, fingerprint_context_report
from ..core.context_reporting import ContextReport
from ..core.errors import NativeH3AdapterError
from ..core.native_h3 import NativeH3Wiring, assert_native_h3_wiring_authority
from ..core.normalization import NormalizedContextRequest
from ..core.production_duration import ProductionDurationIntentV1
from ..core.production_semantics import derive_production_semantic_authority
from ..core.production_storyboard import (
    ManagedExecutionQualificationV1,
    PlanningAssetV1,
    ProductionPlanningContextV2,
    fingerprint_prompt_text,
)

_WORKSPACE_ID = re.compile(r"ws_[A-Za-z0-9_-]{32,128}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ProductionPlanningSourceError(ValueError):
    """One retained Context source cannot authorize Production planning."""


def fingerprint_production_source_request(request: NormalizedContextRequest) -> str:
    """Bind the exact normalized request with explicit binary64 duration tokens."""

    if type(request) is not NormalizedContextRequest:
        raise ProductionPlanningSourceError("planning_source_request")
    request_wire = request.to_wire()
    requested = request_wire.pop("requested_duration_seconds")
    effective = request_wire.pop("effective_duration_seconds")
    if requested is not None and (
        isinstance(requested, bool) or not isinstance(requested, (int, float))
    ):
        raise ProductionPlanningSourceError("planning_source_requested_duration")
    if isinstance(effective, bool) or not isinstance(effective, (int, float)):
        raise ProductionPlanningSourceError("planning_source_effective_duration")
    request_wire["requested_duration_seconds_binary64"] = (
        None if requested is None else binary64_token(requested)
    )
    request_wire["effective_duration_seconds_binary64"] = binary64_token(effective)
    return canonical_fingerprint(
        {
            "schema": "h3.context.production_planning_source.request.v1",
            "request": request_wire,
        }
    )


def _native_binding_fingerprint(wiring: NativeH3Wiring) -> str:
    # CRITICAL: NativeH3Wiring.to_wire() contains the private prompt. Bind only the
    # content-free transport authority here or a planning receipt becomes a prompt cache.
    return canonical_fingerprint(
        {
            "schema": "h3.context.production_planning_source.native_binding.v1",
            "report_id": wiring.report_id,
            "host_version": wiring.host_version,
            "host_revision": wiring.host_revision,
            "native_source": wiring.native_source,
            "native_source_blob": wiring.native_source_blob,
            "native_node_id": wiring.native_node_id,
            "task_mode": wiring.task_mode.value,
            "profile": wiring.profile.to_wire(),
            "prompt_fingerprint": wiring.prompt_fingerprint,
            "native_length_frames": wiring.native_length_frames,
            "native_input_names": list(wiring.native_input_names),
            "bindings": [binding.to_wire() for binding in wiring.bindings],
            "validation_status": wiring.validation_status.value,
            "limitations": list(wiring.limitations),
        }
    )


def _source_fingerprint(
    workspace_id: str,
    report: ContextReport,
    wiring: NativeH3Wiring,
    expires_at_monotonic: float,
) -> str:
    return canonical_fingerprint(
        {
            "schema": "h3.context.production_planning_source.v1",
            "workspace_id": workspace_id,
            "report_revision": report.revision,
            "report_fingerprint": fingerprint_context_report(report),
            "request_fingerprint": fingerprint_production_source_request(report.request),
            "intent_graph_fingerprint": canonical_fingerprint(report.plan.intent_graph.to_wire()),
            "profile_fingerprint": canonical_fingerprint(report.request.profile.to_wire()),
            "reference_registry_fingerprint": canonical_fingerprint(
                report.request.reference_registry.to_wire()
            ),
            "prompt_fingerprint": fingerprint_prompt_text(report.prompt_document.text),
            "native_binding_fingerprint": _native_binding_fingerprint(wiring),
            "expires_at_monotonic": format(expires_at_monotonic, ".17g"),
        }
    )


@dataclass(frozen=True, slots=True)
class ProductionPlanningSourceSnapshot:
    """Immutable exact report/wiring claim with a fixed non-renewing deadline."""

    workspace_id: str
    report: ContextReport
    wiring: NativeH3Wiring
    report_revision: int
    report_fingerprint: str
    expires_at_monotonic: float
    source_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not str or _WORKSPACE_ID.fullmatch(self.workspace_id) is None:
            raise ProductionPlanningSourceError("planning_source_workspace_id")
        if type(self.report) is not ContextReport or type(self.wiring) is not NativeH3Wiring:
            raise ProductionPlanningSourceError("planning_source_authority")
        if not self.report.is_successful or not self.report.validation.is_valid:
            raise ProductionPlanningSourceError("planning_source_report_invalid")
        try:
            assert_native_h3_wiring_authority(self.wiring, self.report)
        except NativeH3AdapterError as exc:
            raise ProductionPlanningSourceError("planning_source_wiring_invalid") from exc
        if type(self.report_revision) is not int or self.report_revision != self.report.revision:
            raise ProductionPlanningSourceError("planning_source_revision")
        if (
            type(self.report_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.report_fingerprint) is None
            or self.report_fingerprint != fingerprint_context_report(self.report)
        ):
            raise ProductionPlanningSourceError("planning_source_report_fingerprint")
        if (
            type(self.expires_at_monotonic) is not float
            or not math.isfinite(self.expires_at_monotonic)
            or self.expires_at_monotonic <= 0
        ):
            raise ProductionPlanningSourceError("planning_source_expiry")
        if (
            type(self.source_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.source_fingerprint) is None
            or self.source_fingerprint
            != _source_fingerprint(
                self.workspace_id,
                self.report,
                self.wiring,
                self.expires_at_monotonic,
            )
        ):
            raise ProductionPlanningSourceError("planning_source_fingerprint")


def make_production_planning_source_snapshot(
    *,
    workspace_id: str,
    report: ContextReport,
    wiring: NativeH3Wiring,
    expires_at_monotonic: float,
) -> ProductionPlanningSourceSnapshot:
    """Construct the sole valid snapshot shape from an already locked registry entry."""

    if isinstance(expires_at_monotonic, bool) or not isinstance(expires_at_monotonic, (int, float)):
        raise ProductionPlanningSourceError("planning_source_expiry")
    exact_expiry = float(expires_at_monotonic)
    return ProductionPlanningSourceSnapshot(
        workspace_id=workspace_id,
        report=report,
        wiring=wiring,
        report_revision=report.revision,
        report_fingerprint=fingerprint_context_report(report),
        expires_at_monotonic=exact_expiry,
        source_fingerprint=_source_fingerprint(
            workspace_id,
            report,
            wiring,
            exact_expiry,
        ),
    )


def build_production_planning_context(
    snapshot: ProductionPlanningSourceSnapshot,
    *,
    planning_context_id: str,
    revision: int,
    duration_intent: ProductionDurationIntentV1,
) -> ProductionPlanningContextV2:
    """Derive V2 planning authority from the retained report, never client fingerprints."""

    if type(snapshot) is not ProductionPlanningSourceSnapshot:
        raise ProductionPlanningSourceError("planning_source_snapshot")
    report = snapshot.report
    request = report.request
    source_seconds = (
        request.effective_duration_seconds
        if request.requested_duration_seconds is None
        else request.requested_duration_seconds
    )
    if (
        isinstance(source_seconds, bool)
        or not isinstance(source_seconds, (int, float))
        or not math.isfinite(source_seconds)
        or source_seconds != int(source_seconds)
        or not 4 <= int(source_seconds) <= 15
    ):
        raise ProductionPlanningSourceError("planning_source_duration")
    registry = request.reference_registry
    semantic_authority = derive_production_semantic_authority(
        report.plan,
        report.prompt_document,
    )
    return ProductionPlanningContextV2(
        planning_context_id=planning_context_id,
        revision=revision,
        optimized_candidate_id=report.report_id,
        optimized_candidate_text_fingerprint=fingerprint_prompt_text(report.prompt_document.text),
        source_request_fingerprint=fingerprint_production_source_request(request),
        source_intent_graph_fingerprint=canonical_fingerprint(report.plan.intent_graph.to_wire()),
        source_profile_fingerprint=canonical_fingerprint(request.profile.to_wire()),
        reference_registry_fingerprint=canonical_fingerprint(registry.to_wire()),
        source_context_duration_seconds=int(source_seconds),
        source_context_duration_provenance=(
            "normalized_context_request.default_effective_duration_seconds"
            if request.requested_duration_seconds is None
            else "normalized_context_request.requested_duration_seconds"
        ),
        production_duration_intent=duration_intent,
        global_task_mode=request.task_mode,
        assets=tuple(PlanningAssetV1(asset.asset_id, asset.role) for asset in registry.assets),
        subject_ids=tuple(subject.subject_id for subject in report.plan.intent_graph.subjects),
        reference_ids=tuple(asset.asset_id for asset in registry.assets),
        # Canonical positive constraints and timing survive in semantic_authority; absence-only
        # forbidden constraints stay typed on the immutable source snapshot used by materialization.
        # Re-rendering either class as free-form strings here would duplicate or reinterpret it.
        hard_constraints=(),
        managed_execution_qualification=ManagedExecutionQualificationV1.PENDING,
        semantic_authority=semantic_authority,
    )


__all__ = [
    "ProductionPlanningSourceError",
    "ProductionPlanningSourceSnapshot",
    "build_production_planning_context",
    "fingerprint_production_source_request",
    "make_production_planning_source_snapshot",
]
