"""Pure, content-free Production workbench presentation contracts.

The canonical workspace remains owned by the adapter registry.  This module only derives the
strict browser allowlist and never receives prompts, provider payloads, media, paths, or URLs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .canonical import canonical_bytes, canonical_fingerprint
from .length import LengthError, resolve_milliseconds
from .segment_workspace import (
    MAX_WORKSPACE_SEGMENTS,
    MultiSegmentWorkspace,
    SegmentRelationKind,
)
from .ui_projection import ExecutionCorrelation

PRODUCTION_WORKBENCH_PROJECTION_SCHEMA = "h3.context.production_workbench.projection.v1"
MAX_PRODUCTION_OUTPUTS = 65
MAX_PRODUCTION_PROJECTION_BYTES = 262_144

_HANDLE = re.compile(r"pw_[A-Za-z0-9_-]{32,96}\Z")
_OUTPUT_HANDLE = re.compile(r"out_[A-Za-z0-9_-]{16,96}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TASK_MODES = {"t2va", "i2va", "fl2va", "l2va", "ref2va"}
_RUN_STATES = {"unavailable", "ready", "running", "succeeded", "failed", "cancelled"}
_CLOSURE_STATES = {
    "unavailable",
    "clean",
    "dirty_self",
    "dirty_upstream",
    "blocked_missing_predecessor",
    "requires_full_recompute",
}
_JOB_STATES = {
    "unavailable",
    "clean",
    "planned",
    "projected",
    "submitted",
    "running",
    "output_verification_failed",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "unknown_ownership",
}
_ARTIFACT_STATES = {"unavailable", "partial", "complete", "failed"}
_CONTINUITY_STATES = {"unavailable", "cut", "restart", "native_frame_handoff"}
_RECONSTRUCTION_STATES = {"unavailable", "complete"}
_ASSEMBLY_STATES = {"unavailable", "planned", "running", "cancelling", "succeeded", "failed"}
_AUTHORITY_VERSIONS = {
    "h3.context.generation_sequence_projection.v1",
    "h3.context.segment_artifact_receipt.v1",
    "h3.context.continuity_boundary_receipt.v1",
    "h3.context.av_reconstruction_receipt.v1",
    "h3.context.m26.production_assembly_receipt.v1",
}
_BOUNDARY_BY_RELATION = {
    SegmentRelationKind.INDEPENDENT: "independent",
    SegmentRelationKind.PREDECESSOR: "native_handoff",
    SegmentRelationKind.ADJACENT_PAIR: "adjacent",
    SegmentRelationKind.CUT: "cut",
    SegmentRelationKind.RESET: "reset",
}
_AUTHORING_ACTIONS = {
    "add_segment_from_context",
    "replace_segment_from_context",
    "set_segment_relation",
    "delete_segment",
    "reorder_segments",
    "set_selection",
    "read_projection",
    "release_workspace",
    "submit_generation_job",
    "assemble_sequence",
    "cancel_assembly",
    "retry_assembly",
    "import_production_outputs_to_authoring",
}


class ProductionWorkbenchProjectionError(ValueError):
    """Raised when a browser projection would exceed the closed contract."""


@dataclass(frozen=True, slots=True)
class ProductionDeliveredVideoProjection:
    """Privacy-safe media facts from one complete admitted artifact."""

    format_label: str
    frame_count: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.format_label not in {"mp4", "mkv", "webm"}:
            raise ProductionWorkbenchProjectionError("invalid_delivered_video_format")
        if (
            type(self.frame_count) is not int
            or not 1 <= self.frame_count <= 512
            or type(self.width) is not int
            or not 64 <= self.width <= 8_192
            or type(self.height) is not int
            or not 64 <= self.height <= 8_192
        ):
            raise ProductionWorkbenchProjectionError("invalid_delivered_video_geometry")

    def to_wire(self) -> dict[str, object]:
        return {
            "format": self.format_label,
            "frame_count": self.frame_count,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class ProductionSegmentProjection:
    """One safe ordered segment row without producer or semantic fingerprints."""

    segment_id: str
    ordinal: int
    task_mode: str
    duration_milliseconds: int
    delivered_milliseconds: int
    frame_count: int
    snapped: bool
    relation: str
    predecessor_segment_id: str | None
    boundary_kind: str
    closure_state: str = "unavailable"
    job_state: str = "unavailable"
    artifact_state: str = "unavailable"
    continuity_state: str = "unavailable"
    delivered_geometry: ProductionDeliveredVideoProjection | None = None

    def __post_init__(self) -> None:
        if type(self.segment_id) is not str or _IDENTIFIER.fullmatch(self.segment_id) is None:
            raise ProductionWorkbenchProjectionError("invalid_segment_id")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= MAX_WORKSPACE_SEGMENTS:
            raise ProductionWorkbenchProjectionError("invalid_segment_ordinal")
        if type(self.task_mode) is not str or self.task_mode not in _TASK_MODES:
            raise ProductionWorkbenchProjectionError("invalid_task_mode")
        # M17-25: the projection re-derives rather than trusts. A wire payload
        # that claims a frame count its duration does not imply is rejected here,
        # which is what makes the derived members safe to render.
        if (
            type(self.duration_milliseconds) is not int
            or type(self.delivered_milliseconds) is not int
            or type(self.frame_count) is not int
            or type(self.snapped) is not bool
        ):
            raise ProductionWorkbenchProjectionError("invalid_duration_value")
        try:
            resolved = resolve_milliseconds(self.duration_milliseconds)
        except LengthError as exc:
            raise ProductionWorkbenchProjectionError("invalid_duration_value") from exc
        if (
            resolved.frame_count != self.frame_count
            or resolved.delivered_milliseconds != self.delivered_milliseconds
            or resolved.snapped is not self.snapped
        ):
            raise ProductionWorkbenchProjectionError("invalid_duration_value")
        try:
            if type(self.relation) is not str:
                raise ValueError
            relation = SegmentRelationKind(self.relation)
        except ValueError as exc:
            raise ProductionWorkbenchProjectionError("invalid_relation") from exc
        requires_predecessor = relation in {
            SegmentRelationKind.PREDECESSOR,
            SegmentRelationKind.ADJACENT_PAIR,
        }
        if requires_predecessor != (self.predecessor_segment_id is not None):
            raise ProductionWorkbenchProjectionError("invalid_predecessor")
        if self.predecessor_segment_id is not None and (
            type(self.predecessor_segment_id) is not str
            or _IDENTIFIER.fullmatch(self.predecessor_segment_id) is None
            or self.predecessor_segment_id == self.segment_id
        ):
            raise ProductionWorkbenchProjectionError("invalid_predecessor")
        if (
            type(self.boundary_kind) is not str
            or self.boundary_kind != _BOUNDARY_BY_RELATION[relation]
        ):
            raise ProductionWorkbenchProjectionError("invalid_boundary")
        if self.closure_state not in _CLOSURE_STATES:
            raise ProductionWorkbenchProjectionError("invalid_closure_state")
        if self.job_state not in _JOB_STATES:
            raise ProductionWorkbenchProjectionError("invalid_job_state")
        if self.artifact_state not in _ARTIFACT_STATES:
            raise ProductionWorkbenchProjectionError("invalid_artifact_state")
        if self.continuity_state not in _CONTINUITY_STATES:
            raise ProductionWorkbenchProjectionError("invalid_continuity_state")
        if (
            self.delivered_geometry is not None
            and type(self.delivered_geometry) is not ProductionDeliveredVideoProjection
        ):
            raise ProductionWorkbenchProjectionError("invalid_delivered_geometry")
        if self.delivered_geometry is not None and self.artifact_state != "complete":
            raise ProductionWorkbenchProjectionError("premature_delivered_geometry")

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "task_mode": self.task_mode,
            "duration": {
                "duration_milliseconds": self.duration_milliseconds,
                "delivered_milliseconds": self.delivered_milliseconds,
                "frame_count": self.frame_count,
                "snapped": self.snapped,
            },
            "relation": self.relation,
            "predecessor_segment_id": self.predecessor_segment_id,
            "boundary_kind": self.boundary_kind,
            "closure_state": self.closure_state,
            "job_state": self.job_state,
            "artifact_state": self.artifact_state,
            "continuity_state": self.continuity_state,
            "delivered_geometry": (
                None if self.delivered_geometry is None else self.delivered_geometry.to_wire()
            ),
        }


@dataclass(frozen=True, slots=True)
class ProductionOutputProjection:
    """Opaque output identity; no locator, receipt, media, or internal fingerprint is exposed."""

    output_handle: str
    ordinal: int
    state: str
    segment_id: str | None = None
    preview: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.output_handle) is not str
            or _OUTPUT_HANDLE.fullmatch(self.output_handle) is None
        ):
            raise ProductionWorkbenchProjectionError("invalid_output_handle")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= MAX_PRODUCTION_OUTPUTS:
            raise ProductionWorkbenchProjectionError("invalid_output_ordinal")
        if self.state not in {"pending", "ready", "failed", "unavailable"}:
            raise ProductionWorkbenchProjectionError("invalid_output_state")
        if self.segment_id is not None and (
            type(self.segment_id) is not str or _IDENTIFIER.fullmatch(self.segment_id) is None
        ):
            raise ProductionWorkbenchProjectionError("invalid_output_segment_id")
        if type(self.preview) is not bool or (self.preview and self.state != "ready"):
            raise ProductionWorkbenchProjectionError("invalid_output_preview")

    def to_wire(self) -> dict[str, object]:
        return {
            "output_handle": self.output_handle,
            "ordinal": self.ordinal,
            "state": self.state,
            "segment_id": self.segment_id,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class ProductionGenerationSequenceSummary:
    """Content-free keys for joining one independently decoded M17-08 projection."""

    sequence_id: str
    sequence_fingerprint: str
    state_fingerprint: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    correlation: ExecutionCorrelation
    schema: str = "h3.context.generation_sequence_projection.v1"

    def __post_init__(self) -> None:
        if self.schema != "h3.context.generation_sequence_projection.v1":
            raise ProductionWorkbenchProjectionError("invalid_generation_sequence_schema")
        if type(self.sequence_id) is not str or _IDENTIFIER.fullmatch(self.sequence_id) is None:
            raise ProductionWorkbenchProjectionError("invalid_sequence_id")
        for value in (self.sequence_fingerprint, self.state_fingerprint):
            if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
                raise ProductionWorkbenchProjectionError("invalid_sequence_fingerprint")
        if type(self.workspace_id) is not str or _IDENTIFIER.fullmatch(self.workspace_id) is None:
            raise ProductionWorkbenchProjectionError("invalid_sequence_workspace_id")
        if (
            type(self.workspace_revision) is not int
            or not 1 <= self.workspace_revision <= 1_000_000
        ):
            raise ProductionWorkbenchProjectionError("invalid_sequence_workspace_revision")
        if (
            type(self.workspace_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.workspace_fingerprint) is None
        ):
            raise ProductionWorkbenchProjectionError("invalid_sequence_workspace_fingerprint")
        if type(self.correlation) is not ExecutionCorrelation:
            raise ProductionWorkbenchProjectionError("invalid_sequence_correlation")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "sequence_id": self.sequence_id,
            "sequence_fingerprint": self.sequence_fingerprint,
            "state_fingerprint": self.state_fingerprint,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "correlation": self.correlation.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ProductionAssemblyProjection:
    """Content-free M26 assembly capability and asynchronous lifecycle."""

    state: str = "unavailable"
    completed: int = 0
    total: int = 0
    capability_fingerprint: str | None = None
    managed_sequence_fingerprint: str | None = None
    artifact_receipt_fingerprints: tuple[str, ...] = ()
    cut_boundary_receipt_fingerprints: tuple[str, ...] = ()
    output_profile_id: str = "legacy_av_30fps_48khz_stereo"
    assembly_job_id: str | None = None
    authorization_fingerprint: str | None = None
    receipt_fingerprint: str | None = None
    failure_code: str | None = "media_runtime_not_authorized"
    projection_fingerprint: str | None = None
    schema: str = "h3.context.production_assembly.projection.v1"

    def __post_init__(self) -> None:
        if self.schema != "h3.context.production_assembly.projection.v1":
            raise ProductionWorkbenchProjectionError("invalid_assembly_schema")
        if self.state not in _ASSEMBLY_STATES:
            raise ProductionWorkbenchProjectionError("invalid_assembly_state")
        if (
            type(self.completed) is not int
            or type(self.total) is not int
            or not 0 <= self.completed <= self.total <= MAX_WORKSPACE_SEGMENTS
            or self.output_profile_id != "legacy_av_30fps_48khz_stereo"
        ):
            raise ProductionWorkbenchProjectionError("invalid_assembly_progress")
        for value in (
            self.capability_fingerprint,
            self.managed_sequence_fingerprint,
            self.authorization_fingerprint,
            self.receipt_fingerprint,
        ):
            if value is not None and (
                type(value) is not str or _FINGERPRINT.fullmatch(value) is None
            ):
                raise ProductionWorkbenchProjectionError("invalid_assembly_fingerprint")
        if self.assembly_job_id is not None and (
            type(self.assembly_job_id) is not str
            or _IDENTIFIER.fullmatch(self.assembly_job_id) is None
        ):
            raise ProductionWorkbenchProjectionError("invalid_assembly_job")
        if self.failure_code is not None and (
            type(self.failure_code) is not str or _IDENTIFIER.fullmatch(self.failure_code) is None
        ):
            raise ProductionWorkbenchProjectionError("invalid_assembly_failure")
        if self.capability_fingerprint is None:
            if (
                self.state != "unavailable"
                or self.assembly_job_id is not None
                or self.managed_sequence_fingerprint is not None
                or self.artifact_receipt_fingerprints
                or self.cut_boundary_receipt_fingerprints
                or self.authorization_fingerprint is not None
                or self.receipt_fingerprint is not None
                or self.failure_code is None
                or self.completed != 0
                or self.total != 0
            ):
                raise ProductionWorkbenchProjectionError("invalid_assembly_unavailable")
        elif self.state == "unavailable":
            if (
                any(
                    value is not None
                    for value in (
                        self.assembly_job_id,
                        self.authorization_fingerprint,
                        self.receipt_fingerprint,
                        self.failure_code,
                    )
                )
                or self.completed != 0
            ):
                raise ProductionWorkbenchProjectionError("invalid_assembly_idle")
        elif (
            self.assembly_job_id is None
            or self.authorization_fingerprint is None
            or self.total == 0
        ):
            raise ProductionWorkbenchProjectionError("invalid_assembly_active")
        if (
            type(self.artifact_receipt_fingerprints) is not tuple
            or type(self.cut_boundary_receipt_fingerprints) is not tuple
        ):
            raise ProductionWorkbenchProjectionError("invalid_assembly_predecessors")
        if self.capability_fingerprint is not None and (
            self.managed_sequence_fingerprint is None
            or not self.artifact_receipt_fingerprints
            or self.total != len(self.artifact_receipt_fingerprints)
            or len(self.artifact_receipt_fingerprints)
            != len(self.cut_boundary_receipt_fingerprints) + 1
            or any(
                type(value) is not str or _FINGERPRINT.fullmatch(value) is None
                for value in (
                    self.artifact_receipt_fingerprints + self.cut_boundary_receipt_fingerprints
                )
            )
            or len(self.artifact_receipt_fingerprints)
            != len(set(self.artifact_receipt_fingerprints))
            or len(self.cut_boundary_receipt_fingerprints)
            != len(set(self.cut_boundary_receipt_fingerprints))
        ):
            raise ProductionWorkbenchProjectionError("invalid_assembly_predecessors")
        if self.state == "succeeded" and (
            self.receipt_fingerprint is None
            or self.failure_code is not None
            or self.completed != self.total
        ):
            raise ProductionWorkbenchProjectionError("invalid_assembly_success")
        if self.state == "failed" and (
            self.failure_code is None or self.receipt_fingerprint is not None
        ):
            raise ProductionWorkbenchProjectionError("invalid_assembly_failure")
        if self.state in {"planned", "running", "cancelling"} and (
            self.failure_code is not None or self.receipt_fingerprint is not None
        ):
            raise ProductionWorkbenchProjectionError("invalid_assembly_active")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.projection_fingerprint is None:
            object.__setattr__(self, "projection_fingerprint", expected)
        elif self.projection_fingerprint != expected:
            raise ProductionWorkbenchProjectionError("invalid_assembly_projection_fingerprint")

    @property
    def fingerprint(self) -> str:
        if self.projection_fingerprint is None:  # pragma: no cover
            raise ProductionWorkbenchProjectionError("invalid_assembly_projection_fingerprint")
        return self.projection_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "state": self.state,
            "progress": {"completed": self.completed, "total": self.total},
            "capability_fingerprint": self.capability_fingerprint,
            "managed_sequence_fingerprint": self.managed_sequence_fingerprint,
            "artifact_receipt_fingerprints": list(self.artifact_receipt_fingerprints),
            "cut_boundary_receipt_fingerprints": list(self.cut_boundary_receipt_fingerprints),
            "output_profile_id": self.output_profile_id,
            "assembly_job_id": self.assembly_job_id,
            "authorization_fingerprint": self.authorization_fingerprint,
            "receipt_fingerprint": self.receipt_fingerprint,
            "failure_code": self.failure_code,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["projection_fingerprint"] = self.fingerprint
        return value


@dataclass(frozen=True, slots=True)
class ProductionWorkbenchProjection:
    """The sole M17-12 browser projection wire."""

    workspace_handle: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    segments: tuple[ProductionSegmentProjection, ...]
    selected_segment_ids: tuple[str, ...]
    run_state: str
    run_completed: int
    run_total: int
    generation_sequence: ProductionGenerationSequenceSummary | None
    reconstruction_state: str
    authority_versions: tuple[str, ...]
    outputs: tuple[ProductionOutputProjection, ...]
    allowed_actions: tuple[str, ...]
    blocker_codes: tuple[str, ...]
    assembly: ProductionAssemblyProjection = field(default_factory=ProductionAssemblyProjection)
    schema: str = PRODUCTION_WORKBENCH_PROJECTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_WORKBENCH_PROJECTION_SCHEMA:
            raise ProductionWorkbenchProjectionError("unsupported_projection_schema")
        if (
            type(self.workspace_handle) is not str
            or _HANDLE.fullmatch(self.workspace_handle) is None
        ):
            raise ProductionWorkbenchProjectionError("invalid_workspace_handle")
        if type(self.workspace_id) is not str or _IDENTIFIER.fullmatch(self.workspace_id) is None:
            raise ProductionWorkbenchProjectionError("invalid_workspace_id")
        if (
            type(self.workspace_revision) is not int
            or not 1 <= self.workspace_revision <= 1_000_000
        ):
            raise ProductionWorkbenchProjectionError("invalid_workspace_revision")
        if (
            type(self.workspace_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.workspace_fingerprint) is None
        ):
            raise ProductionWorkbenchProjectionError("invalid_workspace_fingerprint")
        if not 1 <= len(self.segments) <= MAX_WORKSPACE_SEGMENTS:
            raise ProductionWorkbenchProjectionError("segment_limit")
        segment_ids = tuple(segment.segment_id for segment in self.segments)
        if len(segment_ids) != len(set(segment_ids)) or any(
            segment.ordinal != index for index, segment in enumerate(self.segments, start=1)
        ):
            raise ProductionWorkbenchProjectionError("invalid_segment_order")
        if any(
            segment.predecessor_segment_id is not None
            and segment.predecessor_segment_id not in segment_ids
            for segment in self.segments
        ):
            raise ProductionWorkbenchProjectionError("unknown_predecessor")
        if (
            len(self.selected_segment_ids) > MAX_WORKSPACE_SEGMENTS
            or len(self.selected_segment_ids) != len(set(self.selected_segment_ids))
            or not set(self.selected_segment_ids).issubset(segment_ids)
        ):
            raise ProductionWorkbenchProjectionError("invalid_selection")
        if len(self.outputs) > MAX_PRODUCTION_OUTPUTS:
            raise ProductionWorkbenchProjectionError("output_limit")
        if len({output.output_handle for output in self.outputs}) != len(self.outputs) or any(
            output.ordinal != index for index, output in enumerate(self.outputs, start=1)
        ):
            raise ProductionWorkbenchProjectionError("invalid_output_order")
        if any(
            output.segment_id is not None and output.segment_id not in segment_ids
            for output in self.outputs
        ):
            raise ProductionWorkbenchProjectionError("unknown_output_segment")
        output_segment_ids = tuple(
            output.segment_id for output in self.outputs if output.segment_id is not None
        )
        aggregate_outputs = sum(output.segment_id is None for output in self.outputs)
        expected_aggregate_outputs = 1 if self.reconstruction_state == "complete" else 0
        # IMPORTANT: generated segment artifacts predate reconstruction and therefore have no
        # aggregate row; requiring one hides the only clip the managed runtime actually produced.
        if self.outputs and aggregate_outputs != expected_aggregate_outputs:
            raise ProductionWorkbenchProjectionError("invalid_aggregate_output")
        if len(output_segment_ids) != len(set(output_segment_ids)):
            raise ProductionWorkbenchProjectionError("duplicate_output_segment")
        if self.run_state not in _RUN_STATES:
            raise ProductionWorkbenchProjectionError("invalid_run_state")
        if (
            type(self.run_completed) is not int
            or type(self.run_total) is not int
            or not 0 <= self.run_completed <= self.run_total <= MAX_WORKSPACE_SEGMENTS
        ):
            raise ProductionWorkbenchProjectionError("invalid_run_progress")
        if self.generation_sequence is not None:
            if type(self.generation_sequence) is not ProductionGenerationSequenceSummary:
                raise ProductionWorkbenchProjectionError("invalid_generation_sequence")
            # IMPORTANT: selection advances the live CAS revision without replacing the run's
            # historical source. Reject future/forked summaries, not valid older summaries.
            if (
                self.generation_sequence.workspace_id != self.workspace_id
                or self.generation_sequence.workspace_revision > self.workspace_revision
                or (
                    self.generation_sequence.workspace_revision == self.workspace_revision
                    and self.generation_sequence.workspace_fingerprint != self.workspace_fingerprint
                )
            ):
                raise ProductionWorkbenchProjectionError("generation_sequence_workspace_mismatch")
        if self.reconstruction_state not in _RECONSTRUCTION_STATES:
            raise ProductionWorkbenchProjectionError("invalid_reconstruction_state")
        if type(self.assembly) is not ProductionAssemblyProjection:
            raise ProductionWorkbenchProjectionError("invalid_assembly_projection")
        if (
            type(self.authority_versions) is not tuple
            or len(self.authority_versions) != len(set(self.authority_versions))
            or not set(self.authority_versions).issubset(_AUTHORITY_VERSIONS)
        ):
            raise ProductionWorkbenchProjectionError("invalid_authority_versions")
        if len(self.allowed_actions) != len(set(self.allowed_actions)) or not set(
            self.allowed_actions
        ).issubset(_AUTHORING_ACTIONS | {"preview_output"}):
            raise ProductionWorkbenchProjectionError("invalid_allowed_actions")
        if ("preview_output" in self.allowed_actions) != any(
            output.preview for output in self.outputs
        ):
            raise ProductionWorkbenchProjectionError("invalid_preview_capability")
        # IMPORTANT: automatic assembly retains original segment authority. Permit its original
        # rows only with a succeeded M26 identity; a legacy aggregate never grants import access.
        if "import_production_outputs_to_authoring" in self.allowed_actions and (
            not (
                self.reconstruction_state == "unavailable"
                or (
                    self.reconstruction_state == "complete"
                    and self.assembly.state == "succeeded"
                    and "h3.context.m26.production_assembly_receipt.v1" in self.authority_versions
                )
            )
            or not any(
                output.state == "ready" and output.segment_id is not None for output in self.outputs
            )
        ):
            raise ProductionWorkbenchProjectionError("invalid_authoring_import_capability")
        if len(self.blocker_codes) != len(set(self.blocker_codes)) or any(
            type(code) is not str or len(code) > 64 or _IDENTIFIER.fullmatch(code) is None
            for code in self.blocker_codes
        ):
            raise ProductionWorkbenchProjectionError("invalid_blocker_code")
        if (
            len(self.segments) == MAX_WORKSPACE_SEGMENTS
            and "add_segment_from_context" in self.allowed_actions
        ):
            raise ProductionWorkbenchProjectionError("invalid_allowed_actions")
        if len(self.segments) == 1 and set(self.allowed_actions).intersection(
            {"delete_segment", "reorder_segments"}
        ):
            raise ProductionWorkbenchProjectionError("invalid_allowed_actions")
        assembly_actions = set(self.allowed_actions).intersection(
            {"assemble_sequence", "cancel_assembly", "retry_assembly"}
        )
        expected_assembly_actions: set[str] = set()
        if self.assembly.capability_fingerprint is not None:
            if self.assembly.state == "unavailable":
                expected_assembly_actions.add("assemble_sequence")
            elif self.assembly.state in {"planned", "running"}:
                expected_assembly_actions.add("cancel_assembly")
            elif self.assembly.state == "failed":
                expected_assembly_actions.add("retry_assembly")
        if assembly_actions != expected_assembly_actions:
            raise ProductionWorkbenchProjectionError("invalid_assembly_actions")
        if len(canonical_bytes(self.to_wire())) > MAX_PRODUCTION_PROJECTION_BYTES:
            raise ProductionWorkbenchProjectionError("projection_too_large")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workspace_handle": self.workspace_handle,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "segments": [segment.to_wire() for segment in self.segments],
            "selected_segment_ids": list(self.selected_segment_ids),
            "run": {
                "state": self.run_state,
                "completed": self.run_completed,
                "total": self.run_total,
            },
            "generation_sequence": (
                None if self.generation_sequence is None else self.generation_sequence.to_wire()
            ),
            "reconstruction": {"state": self.reconstruction_state},
            "assembly": self.assembly.to_wire(),
            "authority_versions": list(self.authority_versions),
            "outputs": [output.to_wire() for output in self.outputs],
            "allowed_actions": list(self.allowed_actions),
            "blocker_codes": list(self.blocker_codes),
            "limits": {
                "max_segments": MAX_WORKSPACE_SEGMENTS,
                "max_outputs": MAX_PRODUCTION_OUTPUTS,
            },
        }


def build_production_workbench_projection(
    workspace: MultiSegmentWorkspace,
    *,
    workspace_handle: str,
    run_state: str = "unavailable",
    run_completed: int = 0,
    run_total: int = 0,
    generation_sequence: ProductionGenerationSequenceSummary | None = None,
    reconstruction_state: str = "unavailable",
    authority_versions: tuple[str, ...] = (),
    outputs: tuple[ProductionOutputProjection, ...] = (),
    preview_available: bool = False,
    generation_action_available: bool = False,
    authoring_import_available: bool = False,
    closure_states: Mapping[str, str] | None = None,
    job_states: Mapping[str, str] | None = None,
    artifact_states: Mapping[str, str] | None = None,
    continuity_states: Mapping[str, str] | None = None,
    delivered_geometry: Mapping[str, ProductionDeliveredVideoProjection] | None = None,
    assembly: ProductionAssemblyProjection | None = None,
) -> ProductionWorkbenchProjection:
    """Derive one fresh safe projection from the exact immutable workspace."""

    if type(workspace) is not MultiSegmentWorkspace:
        raise ProductionWorkbenchProjectionError("invalid_workspace")
    if type(preview_available) is not bool:
        raise ProductionWorkbenchProjectionError("invalid_preview_capability")
    if type(generation_action_available) is not bool:
        raise ProductionWorkbenchProjectionError("invalid_generation_capability")
    if type(authoring_import_available) is not bool:
        raise ProductionWorkbenchProjectionError("invalid_authoring_import_capability")
    assembly_supplied = assembly is not None
    assembly_projection = ProductionAssemblyProjection() if assembly is None else assembly
    if type(assembly_projection) is not ProductionAssemblyProjection:
        raise ProductionWorkbenchProjectionError("invalid_assembly_projection")
    segment_ids = {item.segment_id for item in workspace.segments}
    state_maps = (
        closure_states or {},
        job_states or {},
        artifact_states or {},
        continuity_states or {},
        delivered_geometry or {},
    )
    if any(not set(values).issubset(segment_ids) for values in state_maps):
        raise ProductionWorkbenchProjectionError("cross_workspace_segment_state")
    if preview_available != any(output.preview for output in outputs):
        raise ProductionWorkbenchProjectionError("invalid_preview_capability")
    segments: list[ProductionSegmentProjection] = []
    for ordinal, segment in enumerate(workspace.segments, start=1):
        resolved = segment.duration.resolved
        segments.append(
            ProductionSegmentProjection(
                segment_id=segment.segment_id,
                ordinal=ordinal,
                task_mode=segment.task_mode.value,
                duration_milliseconds=resolved.requested_milliseconds,
                delivered_milliseconds=resolved.delivered_milliseconds,
                frame_count=resolved.frame_count,
                snapped=resolved.snapped,
                relation=segment.relation.value,
                predecessor_segment_id=segment.predecessor_segment_id,
                boundary_kind=_BOUNDARY_BY_RELATION[segment.relation],
                closure_state=(closure_states or {}).get(segment.segment_id, "unavailable"),
                job_state=(job_states or {}).get(segment.segment_id, "unavailable"),
                artifact_state=(artifact_states or {}).get(segment.segment_id, "unavailable"),
                continuity_state=(continuity_states or {}).get(segment.segment_id, "unavailable"),
                delivered_geometry=(delivered_geometry or {}).get(segment.segment_id),
            )
        )

    actions = [
        "add_segment_from_context",
        "replace_segment_from_context",
        "set_segment_relation",
        "set_selection",
        "read_projection",
        "release_workspace",
    ]
    if len(segments) >= MAX_WORKSPACE_SEGMENTS:
        actions.remove("add_segment_from_context")
    if len(segments) > 1:
        actions.insert(3, "delete_segment")
        actions.insert(4, "reorder_segments")
    if preview_available:
        actions.append("preview_output")
    if authoring_import_available:
        actions.append("import_production_outputs_to_authoring")
    if generation_action_available:
        if "h3.context.generation_sequence_projection.v1" not in authority_versions:
            raise ProductionWorkbenchProjectionError("invalid_generation_capability")
        actions.append("submit_generation_job")
    if assembly_projection.capability_fingerprint is not None:
        if assembly_projection.state == "unavailable":
            actions.append("assemble_sequence")
        elif assembly_projection.state in {"planned", "running"}:
            actions.append("cancel_assembly")
        elif assembly_projection.state == "failed":
            actions.append("retry_assembly")
    blocker_values: list[str] = []
    if run_state == "unavailable":
        blocker_values.append("sequence_authority_unavailable")
    if assembly_supplied and assembly_projection.failure_code is not None:
        blocker_values.append(f"assembly_unavailable:{assembly_projection.failure_code}")
    return ProductionWorkbenchProjection(
        workspace_handle=workspace_handle,
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        segments=tuple(segments),
        selected_segment_ids=workspace.selected_segment_ids,
        run_state=run_state,
        run_completed=run_completed,
        run_total=run_total,
        generation_sequence=generation_sequence,
        reconstruction_state=reconstruction_state,
        authority_versions=authority_versions,
        outputs=outputs,
        allowed_actions=tuple(actions),
        blocker_codes=tuple(blocker_values),
        assembly=assembly_projection,
    )


__all__ = [
    "MAX_PRODUCTION_OUTPUTS",
    "MAX_PRODUCTION_PROJECTION_BYTES",
    "PRODUCTION_WORKBENCH_PROJECTION_SCHEMA",
    "ProductionDeliveredVideoProjection",
    "ProductionGenerationSequenceSummary",
    "ProductionAssemblyProjection",
    "ProductionOutputProjection",
    "ProductionSegmentProjection",
    "ProductionWorkbenchProjection",
    "ProductionWorkbenchProjectionError",
    "build_production_workbench_projection",
]
