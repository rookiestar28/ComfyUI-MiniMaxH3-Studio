"""Prospective App Mode generation authority and exact artifact closure.

The coordinator is a host adapter: it consumes one retained Production workspace, admits only a
content-free prepared-graph observation, and advances the existing pure generation-sequence core.
It never receives prompt text, media, model filenames or a browser-authored job/closure/success.
"""

from __future__ import annotations

import importlib
import io
import json
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import NoReturn

from ..core.canonical import binary64_token, canonical_bytes, canonical_fingerprint
from ..core.generation_sequence import (
    FingerprintDomain,
    GenerationJobRuntime,
    GenerationJobSpec,
    GenerationJobState,
    GenerationSequenceError,
    GenerationSequenceProjection,
    GenerationSequenceState,
    build_generation_sequence_plan,
    build_generation_sequence_projection,
    cancel_generation_sequence,
    create_generation_sequence_state,
    mark_generation_unknown_ownership,
    record_generation_failure,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
    record_output_verification_failure,
)
from ..core.managed_generation_publication import compose_completed_managed_generation
from ..core.managed_run import (
    ManagedRun,
    ManagedRunError,
    ManagedRunState,
    ManagedRunTrigger,
    holds_live_sequence,
    single_segment_sequence,
)
from ..core.managed_run_release import ReleaseIntent, terminal_fingerprint
from ..core.managed_sequence import (
    MANAGED_SEQUENCE_RECONCILIATION_GRACE_SECONDS,
    ManagedSequenceStateV1,
    ManagedSequenceV1,
    SequenceSlotStateV1,
)
from ..core.production_import import ProductionAutomaticPlanAuthorityV2
from ..core.production_membership import ProductionMemberAttemptV1
from ..core.production_storyboard import ProposalBlockerCodeV1
from ..core.production_workbench import ProductionWorkbenchProjection
from ..core.recompute_closure import extend_recompute_plan, plan_recompute
from ..core.safe_paths import UnsafePathError, validate_directory
from ..core.segment_artifacts import (
    SegmentArtifactError,
    SegmentArtifactReceipt,
    begin_segment_artifact_receipt,
)
from ..core.segment_workspace import MultiSegmentWorkspace, derive_segment_manifests
from ..core.ui_projection import ExecutionCorrelation
from .comfyui_input_geometry import (
    GeometryPreflightError,
    InputGeometryReceipt,
    claim_input_geometry_receipt,
)
from .comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionDestinationAdmissionClaim,
    ProductionMemberPublication,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from .comfyui_route_seam import (
    RoutePolicy,
    RouteResult,
    offload_route_handler,
    register_owned_route,
)
from .composition_root import SEQUENCE_COORDINATOR, component
from .managed_run_registry import (
    ManagedRunRegistry,
    ManagedRunRegistryError,
    ManagedRunSlotReservationV1,
)
from .managed_run_release_service import (
    ManagedRunReleaseRefused,
    ManagedRunReleaseService,
    ReleaseRequest,
)
from .managed_sequence_service import (
    ManagedChildBindingClaimV1,
    ManagedChildBindingRequestV1,
    ManagedSequenceServiceError,
    ManagedSequenceStartResourceClaimV1,
    ManagedSequenceStartResourceRequestV1,
)
from .media_preview_authority import MediaPreviewSourceAuthority
from .segment_artifact_store import (
    ArtifactCapacityReservationV1,
    ArtifactInspectionStatus,
    ArtifactStoreError,
    ArtifactStorePolicy,
    PrivateSegmentArtifactStore,
    _read_regular_bytes,
)

COORDINATOR_ACTION_SCHEMA = "h3.context.generation_coordinator.action.v1"
COORDINATOR_RESPONSE_SCHEMA = "h3.context.generation_coordinator.response.v1"
MANAGED_CHILD_RESPONSE_SCHEMA = "h3.context.generation_coordinator.managed_child_response.v1"
COORDINATOR_PRODUCTION_AUTHORITY_SCHEMA = (
    "h3.context.generation_coordinator.production_authority.v1"
)
MANAGED_MEMBER_RESPONSE_SCHEMA = "h3.context.generation_coordinator.managed_member_response.v1"
COORDINATOR_PRODUCTION_MEMBER_AUTHORITY_SCHEMA = (
    "h3.context.generation_coordinator.production_member_authority.v1"
)
COORDINATOR_ARTIFACT_AUTHORITY_SCHEMA = "h3.context.generation_coordinator.artifact_authority.v1"
RELEASE_REQUEST_V2_SCHEMA = "h3.context.managed_run_release_request.v2"
COORDINATOR_ERROR_SCHEMA = "h3.context.generation_coordinator.error.v1"
PREPARED_GRAPH_OBSERVATION_SCHEMA = "h3.context.prepared_graph_observation.v4"
COORDINATOR_ACTION_ROUTE = "/h3-context/v1/generation/coordinator"

MAX_COORDINATOR_ACTION_BYTES = 32_768
MAX_COORDINATOR_ACTION_DEPTH = 5
MAX_COORDINATOR_ACTION_NODES = 128
# IMPORTANT: owned-identity arrays share the aggregate node budget; a smaller per-array cap can
# reject a valid repository-authored observation before the coordinator worker sees it.
MAX_COORDINATOR_ARRAY = MAX_COORDINATOR_ACTION_NODES
MAX_COORDINATOR_RUNS = 16
MAX_COORDINATOR_LEDGER = MAX_COORDINATOR_RUNS * 8
MAX_COORDINATOR_TOMBSTONES = 64
COORDINATOR_TTL_SECONDS = 900
COORDINATOR_TOMBSTONE_TTL_SECONDS = 300
MAX_COORDINATOR_RETAINED_BYTES = 1_048_576
MAX_COORDINATOR_RESPONSE_BYTES = MAX_COORDINATOR_RETAINED_BYTES // MAX_COORDINATOR_LEDGER
MAX_MANAGED_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_ARTIFACT_SUBFOLDER = 512
MAX_ARTIFACT_FILENAME = 255
MAX_ARTIFACT_VERIFICATIONS = 2
ARTIFACT_INSPECTION_SECONDS = 30.0
MAX_ARTIFACT_VERIFICATION_ATTEMPTS = 2
MAX_ARTIFACT_PRODUCTION_PUBLISH_ATTEMPTS = 2
_TRANSIENT_PRODUCTION_PUBLISH_CODES = frozenset({"workspace_busy"})

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_RUN_HANDLE = re.compile(r"mc_[A-Za-z0-9_-]{32,96}\Z")
_WORKSPACE_HANDLE = re.compile(r"pw_[A-Za-z0-9_-]{32,96}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MANAGED_ROUTES = frozenset({"new", "replace", "existing"})
_TERMINALS = frozenset({"success", "error", "interrupted"})
_OBSERVED_VIDEO_FORMATS = frozenset({"mp4", "mkv", "webm"})
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_generation_coordinator_v1__"
_ROUTE_REGISTERED = False
_PROCESS_SCOPE = secrets.token_urlsafe(24)
_TERMINAL_AUTHORITY_UNRESOLVED = object()
_TERMINAL_JOB_STATES = frozenset(
    {
        GenerationJobState.SUCCEEDED,
        GenerationJobState.FAILED,
        GenerationJobState.TIMED_OUT,
        GenerationJobState.CANCELLED,
        GenerationJobState.UNKNOWN_OWNERSHIP,
    }
)

_ERROR_CATEGORIES = frozenset(
    {
        "artifact_content_invalid",
        "artifact_locator_rejected",
        "artifact_authority_mismatch",
        "artifact_store_unavailable",
        "run_authority_mismatch",
        "unsupported_failure",
        "internal_failure",
    }
)
_RETRY_DISPOSITIONS = frozenset(
    {"none", "retry_output_verification", "inspect_native", "use_native"}
)
_ARTIFACT_CONTENT_CODES = frozenset(
    {
        "artifact_media_invalid",
        "artifact_shape_mismatch",
        "artifact_duration_mismatch",
        "artifact_format_mismatch",
    }
)
_ARTIFACT_LOCATOR_CODES = frozenset(
    {
        "artifact_locator_unsafe",
        "artifact_unavailable",
        "artifact_output_changed",
    }
)
_ARTIFACT_AUTHORITY_CODES = frozenset(
    {
        "artifact_receipt_mismatch",
        "transaction_manifest_authority_mismatch",
    }
)
_ARTIFACT_STORE_CODES = frozenset({"artifact_store_unavailable", "artifact_store_failed"})
_RUN_AUTHORITY_CODES = frozenset(
    {
        "conflicting_terminal",
        "duplicate_submission",
        "foreign_artifact_event",
        "foreign_host_event",
        "invalid_generation_transition",
        "request_id_conflict",
        "run_gone",
        "run_unavailable",
        "stale_generation_sequence",
        "stale_sequence_state",
        "submission_identity_mismatch",
        "workspace_busy",
        "stale_workspace",
        "generation_authority_closed",
        "artifact_publication_unavailable",
    }
)
_TRANSIENT_ARTIFACT_CODES = frozenset(
    {
        "artifact_inspection_timeout",
        "artifact_inspector_unavailable",
        "artifact_store_failed",
        "artifact_store_unavailable",
    }
)


def _safe_error_category(code: str) -> str:
    if code in _ARTIFACT_CONTENT_CODES:
        return "artifact_content_invalid"
    if code in _ARTIFACT_LOCATOR_CODES:
        return "artifact_locator_rejected"
    if code in _ARTIFACT_AUTHORITY_CODES:
        return "artifact_authority_mismatch"
    if code in _ARTIFACT_STORE_CODES:
        return "artifact_store_unavailable"
    if code in _RUN_AUTHORITY_CODES:
        return "run_authority_mismatch"
    if code == "internal_failure":
        return "internal_failure"
    return "unsupported_failure"


class SequenceCoordinatorError(ValueError):
    """A closed content-free coordinator failure."""

    def __init__(
        self,
        code: str,
        status: int,
        *,
        category: str | None = None,
        retry_disposition: str = "none",
        same_run_authority: bool = False,
    ) -> None:
        self.code = code
        self.status = status
        selected_category = _safe_error_category(code) if category is None else category
        if selected_category not in _ERROR_CATEGORIES:
            raise ValueError("invalid coordinator error category")
        if retry_disposition not in _RETRY_DISPOSITIONS:
            raise ValueError("invalid coordinator retry disposition")
        if type(same_run_authority) is not bool:
            raise TypeError("invalid same-run authority hint")
        self.category = selected_category
        self.retry_disposition = retry_disposition
        self.same_run_authority = same_run_authority
        super().__init__(code)

    def with_artifact_context(
        self,
        *,
        retry_disposition: str,
        same_run_authority: bool = True,
    ) -> SequenceCoordinatorError:
        return SequenceCoordinatorError(
            self.code,
            self.status,
            category=self.category,
            retry_disposition=retry_disposition,
            same_run_authority=same_run_authority,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": COORDINATOR_ERROR_SCHEMA,
            "category": self.category,
            "retry_disposition": self.retry_disposition,
            "same_run_authority": self.same_run_authority,
        }


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise SequenceCoordinatorError(f"invalid_{field}", 400)
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise SequenceCoordinatorError(f"invalid_{field}", 400)
    return value


def _bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SequenceCoordinatorError(f"invalid_{field}", 400)
    return value


def _closed(value: object, keys: set[str], field: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise SequenceCoordinatorError(f"invalid_{field}", 400)
    return value


@dataclass(frozen=True, slots=True)
class ObservedVideoArtifact:
    """Bounded video facts measured from one decoded immutable artifact."""

    format_label: str
    shape: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if self.format_label not in _OBSERVED_VIDEO_FORMATS:
            raise ValueError("unsupported_observed_video_format")
        if type(self.shape) is not tuple or len(self.shape) != 4:
            raise ValueError("invalid_observed_video_shape")
        frames, height, width, channels = self.shape
        if (
            type(frames) is not int
            or not 1 <= frames <= 512
            or type(height) is not int
            or not 64 <= height <= 8_192
            or type(width) is not int
            or not 64 <= width <= 8_192
            or channels != 3
        ):
            raise ValueError("invalid_observed_video_shape")


@dataclass(frozen=True, slots=True)
class PreparedGraphObservation:
    route: str
    graph_fingerprint: str
    compiled_prompt_fingerprint: str
    owned_projection_fingerprint: str
    owned_node_ids: tuple[str, ...]
    owned_link_ids: tuple[str, ...]
    model_fingerprint: str
    runtime_fingerprint: str
    fingerprint_domain: FingerprintDomain
    expected_frames: int
    source_identity: InputGeometryReceipt | None
    timeout_ms: int
    native_anchor_node_id: str
    schema: str = PREPARED_GRAPH_OBSERVATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PREPARED_GRAPH_OBSERVATION_SCHEMA:
            raise SequenceCoordinatorError("invalid_observation_schema", 400)
        if self.route not in _MANAGED_ROUTES:
            raise SequenceCoordinatorError("unmanaged_route", 422)
        for value, field_name in (
            (self.graph_fingerprint, "graph_fingerprint"),
            (self.compiled_prompt_fingerprint, "compiled_prompt_fingerprint"),
            (self.owned_projection_fingerprint, "owned_projection_fingerprint"),
            (self.model_fingerprint, "model_fingerprint"),
            (self.runtime_fingerprint, "runtime_fingerprint"),
        ):
            _fingerprint(value, field_name)
        if self.fingerprint_domain is not FingerprintDomain.OUTPUT_PRODUCING_GRAPH:
            raise SequenceCoordinatorError("invalid_fingerprint_domain", 400)
        _bounded_int(self.expected_frames, "expected_frames", 1, 512)
        _bounded_int(self.timeout_ms, "timeout_ms", 1, 86_400_000)
        _identifier(self.native_anchor_node_id, "native_anchor_node_id")
        for node_id in self.owned_node_ids:
            _identifier(node_id, "owned_node_id")
        for link_id in self.owned_link_ids:
            _identifier(link_id, "owned_link_id")

    @classmethod
    def from_wire(cls, value: object) -> PreparedGraphObservation:
        wire = _closed(
            value,
            {
                "schema",
                "route",
                "graph_fingerprint",
                "compiled_prompt_fingerprint",
                "owned_projection_fingerprint",
                "owned_node_ids",
                "owned_link_ids",
                "model_fingerprint",
                "runtime_fingerprint",
                "fingerprint_domain",
                "expected_frames",
                "source_identity",
                "timeout_ms",
                "native_anchor_node_id",
            },
            "observation",
        )
        owned_node_ids = wire["owned_node_ids"]
        owned_link_ids = wire["owned_link_ids"]
        if type(owned_node_ids) is not list or type(owned_link_ids) is not list:
            raise SequenceCoordinatorError("invalid_owned_graph_identity", 400)
        try:
            domain = FingerprintDomain(wire["fingerprint_domain"])
        except (TypeError, ValueError) as exc:
            raise SequenceCoordinatorError("invalid_fingerprint_domain", 400) from exc
        schema = wire["schema"]
        route = wire["route"]
        if type(schema) is not str:
            raise SequenceCoordinatorError("invalid_observation_schema", 400)
        if type(route) is not str:
            raise SequenceCoordinatorError("invalid_route", 400)
        try:
            source_identity = (
                None
                if wire["source_identity"] is None
                else InputGeometryReceipt.from_wire(wire["source_identity"])
            )
        except GeometryPreflightError as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        return cls(
            schema=schema,
            route=route,
            graph_fingerprint=_fingerprint(wire["graph_fingerprint"], "graph_fingerprint"),
            compiled_prompt_fingerprint=_fingerprint(
                wire["compiled_prompt_fingerprint"], "compiled_prompt_fingerprint"
            ),
            owned_projection_fingerprint=_fingerprint(
                wire["owned_projection_fingerprint"], "owned_projection_fingerprint"
            ),
            owned_node_ids=tuple(_identifier(value, "owned_node_id") for value in owned_node_ids),
            owned_link_ids=tuple(_identifier(value, "owned_link_id") for value in owned_link_ids),
            model_fingerprint=_fingerprint(wire["model_fingerprint"], "model_fingerprint"),
            runtime_fingerprint=_fingerprint(wire["runtime_fingerprint"], "runtime_fingerprint"),
            fingerprint_domain=domain,
            expected_frames=wire["expected_frames"],  # type: ignore[arg-type]
            source_identity=source_identity,
            timeout_ms=wire["timeout_ms"],  # type: ignore[arg-type]
            native_anchor_node_id=_identifier(
                wire["native_anchor_node_id"], "native_anchor_node_id"
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "route": self.route,
            "graph_fingerprint": self.graph_fingerprint,
            "compiled_prompt_fingerprint": self.compiled_prompt_fingerprint,
            "owned_projection_fingerprint": self.owned_projection_fingerprint,
            "owned_node_ids": list(self.owned_node_ids),
            "owned_link_ids": list(self.owned_link_ids),
            "model_fingerprint": self.model_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "fingerprint_domain": self.fingerprint_domain.value,
            "expected_frames": self.expected_frames,
            "source_identity": (
                None if self.source_identity is None else self.source_identity.to_wire()
            ),
            "timeout_ms": self.timeout_ms,
            "native_anchor_node_id": self.native_anchor_node_id,
        }


@dataclass(frozen=True, slots=True)
class SavedArtifactLocator:
    filename: str
    subfolder: str
    folder_type: str

    @classmethod
    def from_wire(cls, value: object) -> SavedArtifactLocator:
        wire = _closed(value, {"filename", "subfolder", "type"}, "artifact_locator")
        filename = wire["filename"]
        subfolder = wire["subfolder"]
        folder_type = wire["type"]
        if (
            type(filename) is not str
            or not 1 <= len(filename) <= MAX_ARTIFACT_FILENAME
            or type(subfolder) is not str
            or len(subfolder) > MAX_ARTIFACT_SUBFOLDER
            or folder_type != "output"
        ):
            raise SequenceCoordinatorError("artifact_locator_unsafe", 422)
        # IMPORTANT: stock SaveVideo emits OS-native separators on Windows; canonicalize only
        # the already-bounded relative subfolder before the path admission checks below.
        canonical_subfolder = subfolder.replace("\\", "/")
        return cls(filename, canonical_subfolder, "output")


@dataclass(frozen=True, slots=True)
class SequenceArtifactAuthorityV1:
    receipt_fingerprint: str
    byte_length: int
    schema: str = COORDINATOR_ARTIFACT_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != COORDINATOR_ARTIFACT_AUTHORITY_SCHEMA:
            raise SequenceCoordinatorError("invalid_response", 500)
        _fingerprint(self.receipt_fingerprint, "artifact_receipt_fingerprint")
        _bounded_int(
            self.byte_length,
            "artifact_byte_length",
            1,
            MAX_MANAGED_ARTIFACT_BYTES,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "receipt_fingerprint": self.receipt_fingerprint,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True, slots=True)
class SequenceProductionAuthorityV1:
    """Reference to the retained original owner, never a replacement workspace projection."""

    workspace_handle: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    automatic_plan_fingerprint: str
    schema: str = COORDINATOR_PRODUCTION_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema != COORDINATOR_PRODUCTION_AUTHORITY_SCHEMA
            or type(self.workspace_handle) is not str
            or _WORKSPACE_HANDLE.fullmatch(self.workspace_handle) is None
        ):
            raise SequenceCoordinatorError("invalid_response", 500)
        _identifier(self.workspace_id, "workspace_id")
        _bounded_int(self.workspace_revision, "workspace_revision", 0, 1_000_000)
        _fingerprint(self.workspace_fingerprint, "workspace_fingerprint")
        _fingerprint(self.automatic_plan_fingerprint, "automatic_plan_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workspace_handle": self.workspace_handle,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "automatic_plan_fingerprint": self.automatic_plan_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SequenceProductionMemberAuthorityV1:
    """Reference to the accumulating project a member run publishes into."""

    workspace_handle: str
    workspace_id: str
    member_segment_id: str
    schema: str = COORDINATOR_PRODUCTION_MEMBER_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema != COORDINATOR_PRODUCTION_MEMBER_AUTHORITY_SCHEMA
            or type(self.workspace_handle) is not str
            or _WORKSPACE_HANDLE.fullmatch(self.workspace_handle) is None
        ):
            raise SequenceCoordinatorError("invalid_response", 500)
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.member_segment_id, "member_segment_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "workspace_handle": self.workspace_handle,
            "workspace_id": self.workspace_id,
            "member_segment_id": self.member_segment_id,
        }


@dataclass(frozen=True, slots=True)
class SequenceCoordinatorResponse:
    run_handle: str
    disposition: str
    sequence: GenerationSequenceProjection
    production: ProductionWorkbenchProjection
    artifact_authority: SequenceArtifactAuthorityV1 | None
    terminal_fingerprint: str | None
    schema: str = COORDINATOR_RESPONSE_SCHEMA
    production_authority: SequenceProductionAuthorityV1 | None = field(default=None, kw_only=True)
    production_member_authority: SequenceProductionMemberAuthorityV1 | None = field(
        default=None, kw_only=True
    )

    def __post_init__(self) -> None:
        if self.production_authority is not None and self.production_member_authority is not None:
            raise SequenceCoordinatorError("invalid_response", 500)
        expected_schema = (
            MANAGED_CHILD_RESPONSE_SCHEMA
            if self.production_authority is not None
            else MANAGED_MEMBER_RESPONSE_SCHEMA
            if self.production_member_authority is not None
            else COORDINATOR_RESPONSE_SCHEMA
        )
        if self.schema != expected_schema:
            raise SequenceCoordinatorError("invalid_response", 500)
        if _RUN_HANDLE.fullmatch(self.run_handle) is None:
            raise SequenceCoordinatorError("invalid_response", 500)
        _identifier(self.disposition, "response_disposition")
        if type(self.sequence) is not GenerationSequenceProjection:
            raise SequenceCoordinatorError("invalid_response", 500)
        if type(self.production) is not ProductionWorkbenchProjection:
            raise SequenceCoordinatorError("invalid_response", 500)
        if self.production_authority is not None:
            authority = self.production_authority
            plan = self.sequence.state.plan
            if (
                type(authority) is not SequenceProductionAuthorityV1
                or authority.workspace_handle != self.production.workspace_handle
                or authority.workspace_id != plan.workspace_id
                or authority.workspace_revision != plan.workspace_revision
                or authority.workspace_fingerprint != plan.workspace_fingerprint
                or self.production.workspace_id != plan.workspace_id
                or self.production.workspace_revision != plan.workspace_revision
                or self.production.workspace_fingerprint != plan.workspace_fingerprint
                or self.production.generation_sequence is None
                or self.production.generation_sequence.state_fingerprint
                != self.sequence.state.fingerprint
                or self.production.generation_sequence.sequence_id != plan.sequence_id
                or self.production.generation_sequence.sequence_fingerprint != plan.fingerprint
                or self.production.generation_sequence.workspace_id != plan.workspace_id
                or self.production.generation_sequence.workspace_revision != plan.workspace_revision
                or self.production.generation_sequence.workspace_fingerprint
                != plan.workspace_fingerprint
                or self.production.generation_sequence.correlation != self.sequence.correlation
                or len(plan.jobs) != 1
            ):
                raise SequenceCoordinatorError("cross_authority_response", 500)
        if self.production_member_authority is not None:
            member = self.production_member_authority
            plan = self.sequence.state.plan
            summary = self.production.generation_sequence
            # CRITICAL: a member sequence describes its own execution workspace, never the project.
            # Join the reference only through the one job it executed and the private projection
            # derived from that sequence; never compare the project id with the plan's workspace.
            if (
                type(member) is not SequenceProductionMemberAuthorityV1
                or member.workspace_handle != self.production.workspace_handle
                or len(plan.jobs) != 1
                or plan.jobs[0].segment_id != member.member_segment_id
                or summary is None
                or summary.state_fingerprint != self.sequence.state.fingerprint
                or summary.sequence_fingerprint != plan.fingerprint
                or self.production.workspace_id != plan.workspace_id
            ):
                raise SequenceCoordinatorError("cross_authority_response", 500)
        if (
            self.artifact_authority is not None
            and type(self.artifact_authority) is not SequenceArtifactAuthorityV1
        ):
            raise SequenceCoordinatorError("invalid_response", 500)
        if self.terminal_fingerprint is not None:
            _fingerprint(self.terminal_fingerprint, "terminal_fingerprint")

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "schema": self.schema,
            "run_handle": self.run_handle,
            "disposition": self.disposition,
            "artifact_authority": (
                None if self.artifact_authority is None else self.artifact_authority.to_wire()
            ),
            "terminal_fingerprint": self.terminal_fingerprint,
            "sequence": self.sequence.to_wire(),
        }
        if self.production_authority is not None:
            wire["production_authority"] = self.production_authority.to_wire()
        elif self.production_member_authority is not None:
            wire["production_member_authority"] = self.production_member_authority.to_wire()
        else:
            wire["production"] = self.production.to_wire()
        return wire


def _managed_child_response_size_bound(response: SequenceCoordinatorResponse) -> int:
    """Size-only envelope; these placeholders are never admitted as execution evidence."""
    if (
        response.production_authority is None and response.production_member_authority is None
    ) or len(response.sequence.eligible_commands) != 1:
        raise SequenceCoordinatorError("invalid_response", 500)
    wire = response.to_wire()
    sequence = response.sequence.to_wire()
    progress = response.sequence.progress[0].to_wire()
    # CRITICAL: reserve future optional fields before binding. Measuring only the planned
    # response discovers overflow after submission/artifact mutation and loses its replay row.
    identifier = "x" * 128
    fingerprint = "sha256:" + "0" * 64
    for key in ("transaction_id", "queue_prompt_id", "host_owner_id", "failure_code"):
        progress[key] = identifier
    for key in ("artifact_receipt_fingerprint", "artifact_output_fingerprint"):
        progress[key] = fingerprint
    progress["attempt"] = 8
    progress["state"] = max((state.value for state in GenerationJobState), key=len)
    sequence["progress"] = [progress]
    sequence["correlation"] = {"prompt_id": identifier, "execution_node_id": identifier}
    command = response.sequence.eligible_commands[0].to_wire()
    command["transaction_id"] = identifier
    command["attempt"] = 8
    sequence["eligible_commands"] = [command]
    sequence["cancellation_requested"] = False
    sequence["complete"] = False
    wire["sequence"] = sequence
    wire["disposition"] = identifier
    wire["terminal_fingerprint"] = fingerprint
    wire["artifact_authority"] = {
        "schema": COORDINATOR_ARTIFACT_AUTHORITY_SCHEMA,
        "receipt_fingerprint": fingerprint,
        "byte_length": MAX_MANAGED_ARTIFACT_BYTES,
    }
    return len(canonical_bytes(wire))


@dataclass(frozen=True, slots=True)
class SequenceCoordinatorDispatchResult:
    status: int
    response: SequenceCoordinatorResponse | None = None


@dataclass(slots=True)
class _ArtifactVerificationCandidate:
    locator: SavedArtifactLocator
    output_fingerprint: str
    byte_length: int
    partial_receipt: SegmentArtifactReceipt | None = None
    verification_attempts: int = 1
    store_started: bool = False
    completed_receipt: SegmentArtifactReceipt | None = None
    production_publish_attempts: int = 0


@dataclass(slots=True)
class _RunEntry:
    run_handle: str
    production_handle: str
    workspace: MultiSegmentWorkspace
    observation: PreparedGraphObservation
    correlation: ExecutionCorrelation
    state: GenerationSequenceState
    production: ProductionWorkbenchProjection
    touched_at: float
    managed_creation_request_id: str | None = None
    queue_prompt_id: str | None = None
    #: When the host accepted this prompt, on this coordinator's clock. A detached run's
    #: deadline is computed from it and never from the moment the client left.
    submitted_at: float | None = None
    terminal_kind: str | None = None
    artifact_receipt: SegmentArtifactReceipt | None = None
    artifact_locator: SavedArtifactLocator | None = None
    artifact_candidate: _ArtifactVerificationCandidate | None = None
    artifact_preview_source: MediaPreviewSourceAuthority | None = None
    original_automatic_authority: ProductionAutomaticPlanAuthorityV2 | None = None
    #: Set for a run that publishes one member of an accumulating project. `workspace` is then
    #: the member's own execution workspace and `production_handle` the project's.
    member_workspace_id: str | None = None
    member_attempt: ProductionMemberAttemptV1 | None = None


@dataclass(frozen=True, slots=True)
class _ArtifactVerificationTicket:
    request_id: str
    request_digest: str
    entry: _RunEntry
    snapshot: _RunEntry
    managed_run: ManagedRun
    detached_deadline: float | None
    cancelled: threading.Event


@dataclass(frozen=True, slots=True)
class _CompletedManagedChild:
    state: GenerationSequenceState
    receipt: SegmentArtifactReceipt
    correlation: ExecutionCorrelation
    run_handle: str
    terminal_fingerprint: str

    def retained_bytes(self) -> int:
        return (
            len(canonical_bytes(self.state.plan.to_wire()))
            + len(canonical_bytes(self.state.to_wire()))
            + len(canonical_bytes(self.receipt.to_wire()))
            + 512
        )


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    request_digest: str
    response: SequenceCoordinatorResponse
    touched_at: float


@dataclass(slots=True)
class _ManagedStartResourceState:
    request_fingerprint: str
    parent_sequence_id: str
    parent_authorization_fingerprint: str
    reserved_rows: int
    reserved_bytes: int
    idle_expires_at: float
    expires_at: float
    child_slot: ManagedRunSlotReservationV1
    segment_count: int
    artifact_capacity: ArtifactCapacityReservationV1 | None = None
    consumed_rows: int = 1
    consumed_requests: dict[str, int] = field(default_factory=dict)
    ledger_released: bool = False
    child_slot_released: bool = False
    artifact_capacity_released: bool = False
    bound_child_run_handle: str | None = None
    pre_submit_cleaned_child_run_handle: str | None = None
    expiry_pending: bool = False
    expiry_transition_complete: bool = False
    original_authority: ProductionAutomaticPlanAuthorityV2 | None = None
    original_handle: str | None = None
    completed_children: dict[str, _CompletedManagedChild] = field(default_factory=dict)

    @property
    def coordinator_fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "schema": "h3.context.managed_coordinator_ledger_reservation.v1",
                "request_fingerprint": self.request_fingerprint,
                "parent_sequence_id": self.parent_sequence_id,
                "parent_authorization_fingerprint": self.parent_authorization_fingerprint,
                "reserved_rows": self.reserved_rows,
                "reserved_bytes": self.reserved_bytes,
                "idle_expires_at": binary64_token(self.idle_expires_at),
                "expires_at": binary64_token(self.expires_at),
            }
        )


#: The V2 payload members, closed per intent. CRITICAL: closed *per intent*, not as one union
#: of every member. A terminal proof sent alongside a detach, or omitted from a terminal
#: cleanup, is then a 400 before anything is read -- a single permissive key set would let a
#: client attach a proof to an intent that ignores it and believe the removal was authorized.
_RELEASE_V2_BASE = {"schema", "run_handle", "intent", "expected_state_fingerprint"}
_RELEASE_V2_MEMBERS = {
    ReleaseIntent.CLEANUP_PRE_SUBMIT: _RELEASE_V2_BASE,
    ReleaseIntent.DETACH_CLIENT: _RELEASE_V2_BASE,
    ReleaseIntent.CLEANUP_TERMINAL: _RELEASE_V2_BASE | {"observed_terminal_fingerprint"},
}

ArtifactInspector = Callable[..., ObservedVideoArtifact]
GeometryReceiptClaimant = Callable[..., InputGeometryReceipt]


def _ebml_document_type(payload: bytes) -> str | None:
    """Read the bounded EBML DocType needed to distinguish WebM from Matroska."""

    header = payload[:4_096]
    marker = b"\x42\x82"
    offset = header.find(marker)
    while offset >= 0:
        size_offset = offset + len(marker)
        if size_offset >= len(header):
            return None
        first = header[size_offset]
        mask = 0x80
        width = 1
        while width <= 8 and first & mask == 0:
            mask >>= 1
            width += 1
        if width <= 8 and size_offset + width <= len(header):
            size = first & (mask - 1)
            for value in header[size_offset + 1 : size_offset + width]:
                size = (size << 8) | value
            value_offset = size_offset + width
            if 1 <= size <= 32 and value_offset + size <= len(header):
                try:
                    return header[value_offset : value_offset + size].decode("ascii").lower()
                except UnicodeDecodeError:
                    return None
        offset = header.find(marker, offset + 1)
    return None


def _observed_format_label(format_name: object, payload: bytes) -> str:
    names = {part.strip().lower() for part in str(format_name).split(",") if part.strip()}
    if "mp4" in names:
        return "mp4"
    if names.intersection({"matroska", "webm"}):
        # IMPORTANT: libav exposes Matroska and WebM through one demuxer name. Read the actual
        # bounded container header instead of guessing from the private filename extension.
        document_type = _ebml_document_type(payload)
        if document_type == "webm":
            return "webm"
        if document_type == "matroska":
            return "mkv"
    raise SequenceCoordinatorError("artifact_format_mismatch", 422)


def _default_artifact_inspector(
    payload: bytes,
    *,
    expected_frames: int,
    deadline: float,
    should_cancel: Callable[[], bool],
) -> ObservedVideoArtifact:
    """Decode one bounded immutable payload through the host's optional PyAV runtime."""

    try:
        av = importlib.import_module("av")
        container = av.open(io.BytesIO(payload), mode="r")
        try:
            streams = tuple(stream for stream in container.streams if stream.type == "video")
            if len(streams) != 1:
                raise SequenceCoordinatorError("artifact_media_invalid", 422)
            stream = streams[0]
            format_label = _observed_format_label(
                getattr(container.format, "name", ""),
                payload,
            )
            observed_width = int(getattr(stream, "width", 0))
            observed_height = int(getattr(stream, "height", 0))
            # CRITICAL: reject hostile dimensions from stream metadata before decode can allocate
            # an unbounded frame; the compressed-file byte ceiling cannot bound decoded memory.
            if (
                not 64 <= observed_width <= 8_192
                or not 64 <= observed_height <= 8_192
                or observed_width * observed_height > 8_192 * 8_192
            ):
                raise SequenceCoordinatorError("artifact_shape_mismatch", 422)
            frame_count = 0
            for frame in container.decode(stream):
                if should_cancel():
                    raise SequenceCoordinatorError("artifact_inspection_cancelled", 409)
                if time.monotonic() >= deadline:
                    raise SequenceCoordinatorError("artifact_inspection_timeout", 408)
                frame_count += 1
                if (
                    frame_count > expected_frames
                    or int(frame.width) != observed_width
                    or int(frame.height) != observed_height
                ):
                    raise SequenceCoordinatorError("artifact_shape_mismatch", 422)
            if frame_count != expected_frames:
                raise SequenceCoordinatorError("artifact_shape_mismatch", 422)
            return ObservedVideoArtifact(
                format_label=format_label,
                shape=(frame_count, observed_height, observed_width, 3),
            )
        finally:
            container.close()
    except SequenceCoordinatorError:
        raise
    except Exception as exc:
        raise SequenceCoordinatorError("artifact_inspector_unavailable", 422) from exc


#: The aggregate states after which a run takes no further lifecycle transition.
_AGGREGATE_ENDED = frozenset(
    {
        ManagedRunState.TERMINAL_SUCCEEDED,
        ManagedRunState.TERMINAL_FAILED,
        ManagedRunState.TERMINAL_CANCELLED,
        ManagedRunState.TERMINAL_UNKNOWN_OWNERSHIP,
        ManagedRunState.EXPIRED,
    }
)

#: The job states whose arrival proves the host began executing the prompt. `_artifact` and
#: `_terminal` advance a SUBMITTED runtime to RUNNING before doing their own work, so every one of
#: these can arrive in a publish that never shows RUNNING, and the aggregate has to reconstruct the
#: step. CRITICAL: `OUTPUT_VERIFICATION_FAILED` belongs here. It has no trigger of its own -- the
#: run has not ended -- but omitting it from *both* places left the aggregate saying `submitted`
#: while the coordinator said verification had failed, which made the `output_verification_failed`
#: row of `tests/test_m23_31_coordinator_projection.py::_CONSISTENT` false. Nothing caught it,
#: because that row was the one the projection tests never drove.
#: The job states in which the host may still be executing the prompt, and therefore the states a
#: cancel must answer with `unknown_ownership` rather than `cancelled`. CRITICAL: this is the only
#: definition; `_cancel` and its tests read it rather than restating `{SUBMITTED, RUNNING}`.
#:
#: It is deliberately *not* claimed to be the same object as
#: `core.managed_run.HOST_MAY_HOLD_THE_PROMPT`, and an earlier comment here said so wrongly. The two
#: answer one question over two vocabularies related many-to-many by
#: `tests/test_m23_31_coordinator_projection.py::_CONSISTENT`, and no choice of either set makes
#: them agree row for row: job `running` also covers aggregate `artifact_recorded`, while job
#: `output_verification_failed` covers `running` and `artifact_recorded` yet is not a state in which
#: the host may still hold the prompt -- verification failing proves it already ran. What is true,
#: and what `tests/test_m23_31_cancel_predicate.py` pins, is the one-way implication: every
#: aggregate state consistent with a job state in this set is in `HOST_MAY_HOLD_THE_PROMPT`.
HOST_MAY_HOLD_JOB_STATES = frozenset(
    {
        GenerationJobState.SUBMITTED,
        GenerationJobState.RUNNING,
    }
)

_PAST_SUBMISSION = frozenset(
    {
        GenerationJobState.RUNNING,
        GenerationJobState.OUTPUT_VERIFICATION_FAILED,
        GenerationJobState.SUCCEEDED,
        GenerationJobState.FAILED,
        GenerationJobState.TIMED_OUT,
    }
)

#: Which aggregate trigger one job-state arrival represents. `OUTPUT_VERIFICATION_FAILED` and
#: `PLANNED`/`PROJECTED` are deliberately absent: the run has not ended, and inventing a terminal
#: transition for them would put the aggregate in a state the coordinator is not in. Absence here
#: is not absence from `_PAST_SUBMISSION`; the two answer different questions.
_JOB_STATE_TRIGGERS: Mapping[GenerationJobState, ManagedRunTrigger] = {
    GenerationJobState.SUBMITTED: ManagedRunTrigger.RECORD_SUBMISSION,
    GenerationJobState.RUNNING: ManagedRunTrigger.RECORD_RUNNING,
    GenerationJobState.SUCCEEDED: ManagedRunTrigger.RECORD_SUCCEEDED,
    GenerationJobState.FAILED: ManagedRunTrigger.RECORD_FAILED,
    GenerationJobState.TIMED_OUT: ManagedRunTrigger.RECORD_FAILED,
    GenerationJobState.CANCELLED: ManagedRunTrigger.CANCEL_BEFORE_HOST,
    GenerationJobState.UNKNOWN_OWNERSHIP: ManagedRunTrigger.CANCEL_AFTER_HOST,
}


def lifecycle_triggers(
    previous: GenerationJobState,
    current: GenerationJobState,
    aggregate: ManagedRunState,
    *,
    artifact_recorded: bool,
) -> tuple[ManagedRunTrigger, ...]:
    """The aggregate transitions one publish represents, in the order they must be applied.

    CRITICAL: the coordinator collapses several real events into one publish, and the order the
    aggregate needs is not the order they are visible in. Two rules make this correct and both are
    easy to lose:

    * `_terminal` and `_artifact` advance a SUBMITTED runtime to RUNNING before doing their own
      work, so one state change can arrive as SUBMITTED -> FAILED with no RUNNING in between. The
      implied `record_running` is reconstructed here rather than admitted as an extra source on
      every terminal transition, because that would make the published table describe transitions
      the system never takes. `_PAST_SUBMISSION` is the whole set of arrivals that prove it, and
      `output_verification_failed` is one of them even though it ends nothing.
    * A run whose aggregate has already reached a terminal or expired state takes no further
      transitions. The coordinator still re-marks its generation sequence after a terminal -- a
      cancel arriving then is a re-marking, not a second ending -- and replaying it would move the
      aggregate to a state the run is not in.

    Every other refusal is left to raise. Skipping unrecognised transitions silently is how the
    aggregate would become a decorative second record instead of a checked one.
    """

    if aggregate in _AGGREGATE_ENDED:
        return ()
    triggers: list[ManagedRunTrigger] = []
    mapped = None if current is previous else _JOB_STATE_TRIGGERS.get(current)
    if mapped is ManagedRunTrigger.RECORD_RUNNING:
        triggers.append(ManagedRunTrigger.RECORD_RUNNING)
    elif aggregate is ManagedRunState.SUBMITTED and (
        current in _PAST_SUBMISSION or artifact_recorded
    ):
        triggers.append(ManagedRunTrigger.RECORD_RUNNING)
    if artifact_recorded:
        triggers.append(ManagedRunTrigger.RECORD_ARTIFACT)
    if mapped is not None and mapped is not ManagedRunTrigger.RECORD_RUNNING:
        triggers.append(mapped)
    return tuple(triggers)


def _first_runtime(state: GenerationSequenceState) -> GenerationJobRuntime:
    """The run's only runtime.

    CRITICAL: legacy `_prepare` retains its single-segment workspace guard. The managed parent
    path carries full original manifests but constructs exactly one job/runtime per child.
    Neither path admits multiple runtimes here; never turn this into an arbitrary first-job read.
    """

    if len(state.runtimes) != 1:
        raise SequenceCoordinatorError("managed_runtime_shape", 500)
    return state.runtimes[0]


class SequenceCoordinatorRegistry:
    """Bounded, replay-safe process-local owner for managed App Mode runs."""

    def __init__(
        self,
        *,
        production_registry: ProductionWorkspaceRegistry,
        output_root_factory: Callable[[], Path],
        private_root_factory: Callable[[], Path],
        artifact_inspector: ArtifactInspector = _default_artifact_inspector,
        geometry_receipt_claimant: GeometryReceiptClaimant = claim_input_geometry_receipt,
        clock: Callable[[], float] = time.monotonic,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(40),
        max_runs: int = MAX_COORDINATOR_RUNS,
        ttl_seconds: int = COORDINATOR_TTL_SECONDS,
    ) -> None:
        if (
            type(production_registry) is not ProductionWorkspaceRegistry
            or not callable(output_root_factory)
            or not callable(private_root_factory)
            or not callable(artifact_inspector)
            or not callable(geometry_receipt_claimant)
            or not callable(clock)
            or not callable(clock_ms)
            or not callable(token_factory)
            or type(max_runs) is not int
            or not 1 <= max_runs <= MAX_COORDINATOR_RUNS
            or type(ttl_seconds) is not int
            or not 1 <= ttl_seconds <= COORDINATOR_TTL_SECONDS
        ):
            raise SequenceCoordinatorError("invalid_coordinator_configuration", 500)
        self._production = production_registry
        self._output_root_factory = output_root_factory
        self._private_root_factory = private_root_factory
        self._artifact_inspector = artifact_inspector
        self._geometry_receipt_claimant = geometry_receipt_claimant
        self._clock = clock
        self._clock_ms = clock_ms
        self._token_factory = token_factory
        self._max_runs = max_runs
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, _RunEntry] = OrderedDict()
        self._ledger: OrderedDict[str, _LedgerEntry] = OrderedDict()
        self._artifact_requests: dict[str, _ArtifactVerificationTicket] = {}
        self._artifact_slots = threading.BoundedSemaphore(MAX_ARTIFACT_VERIFICATIONS)
        self._tombstones: OrderedDict[str, float] = OrderedDict()
        self._managed_start_resources: OrderedDict[str, _ManagedStartResourceState] = OrderedDict()
        self._artifact_store: PrivateSegmentArtifactStore | None = None
        self._artifact_store_init_lock = threading.Lock()
        self._managed = ManagedRunRegistry(clock=clock)
        self._release = ManagedRunReleaseService(self._managed)
        self._managed_lease_transition: Callable[[str, float], None] | None = None
        production_registry.bind_live_sequence_probe(
            self._workspace_has_live_run,
            run_probe=self._run_holds_live_sequence,
        )

    def bind_managed_sequence_lease_transition(
        self,
        transition: Callable[[str, float], None],
    ) -> None:
        """Bind the one parent-first M26 expiry owner during composition."""

        if not callable(transition):
            raise ValueError("managed sequence lease transition is invalid")
        with self._lock:
            if (
                self._managed_lease_transition is not None
                and self._managed_lease_transition is not transition
            ):
                raise ValueError("managed sequence lease transition is already bound")
            self._managed_lease_transition = transition

    def _workspace_has_live_run(self, workspace_handle: str) -> bool:
        """Whether any live run still depends on this production workspace.

        The production registry asks this before releasing a workspace. Finding the run that owns
        a workspace is a join lookup and belongs here; deciding whether that run is still live is
        the rule, and it is not restated here. CRITICAL: call `holds_live_sequence`. Inlining
        `prepared_sequence is not None` reads as identical and is how the rule acquires a second
        text that drifts from the machine guard it is supposed to mirror.

        CRITICAL: the production registry calls this while holding its own lock and the
        workspace's mutation claim, so the process-wide acquisition order is production, then the
        managed-run table, then one run -- extending the table-then-run rule stated in
        `adapters/managed_run_registry._entry`. Nothing may take a managed-run lock and then reach
        into production: that is why the registry's evidence sink fires outside the run lock, and
        why this method takes no coordinator lock of its own. A deadlock from the reverse order
        appears only under real concurrency and no focused test reproduces it.
        """

        for handle in self._managed.live_handles():
            try:
                run = self._managed.read(handle)
            except ManagedRunError:
                continue
            if run.production_workspace == workspace_handle and holds_live_sequence(run):
                return True
        return False

    def _run_holds_live_sequence(self, run_handle: str) -> bool:
        """Whether one exact run still holds its prepared sequence.

        The same rule as `_workspace_has_live_run`, joined by run handle for a project member. The
        same lock rule applies: production calls this under its own lock, so take no coordinator
        lock here.
        """

        try:
            run = self._managed.read(run_handle)
        except ManagedRunError:
            return False
        return run.is_live and holds_live_sequence(run)

    def _prune(self, now: float) -> None:
        for handle, entry in tuple(self._runs.items()):
            managed_resource = self._managed_resource_for_child(handle)
            if managed_resource is not None:
                if managed_resource.expiry_transition_complete:
                    continue
                deadline = managed_resource.idle_expires_at
                if entry.submitted_at is not None:
                    deadline = min(
                        managed_resource.expires_at,
                        entry.submitted_at
                        + (entry.observation.timeout_ms / 1000.0)
                        + MANAGED_SEQUENCE_RECONCILIATION_GRACE_SECONDS,
                    )
                if now > deadline:
                    managed_resource.expiry_pending = True
                # CRITICAL: M26 expiry is parent -> coordinator -> Production -> ManagedRun. This
                # row and its protected aggregate remain addressable until that owner completes;
                # legacy TTL pruning here would delete the child before ownership is recorded.
                continue
            if now - entry.touched_at < self._ttl_seconds:
                continue
            # CRITICAL: this row is the only way to address a run, and its TTL is refreshed only by
            # `_publish` -- which, after the client detaches, never happens again. A detached run is
            # retained by the ManagedRun registry under a fixed lease that is routinely longer than
            # this TTL (the frontend sends `timeout_ms: 3_600_000`), so dropping the row here would
            # answer `run_gone` for a run that exists, still holds one of the sixteen live slots,
            # and still has its result. Ask the registry that owns the lease; never re-derive the
            # deadline here, and never copy it into `_RunEntry`.
            #
            # CRITICAL: this adds no new lock edge. `coordinator -> managed-run table` already
            # exists -- `_mirror` calls `self._managed.advance` under this same lock -- so the
            # process-wide order (production -> managed-run table -> one run, extended by the
            # coordinator ahead of production) is unchanged. Do not answer this from inside the
            # production registry's callback instead: that path holds production's lock, and
            # reaching back into the coordinator from there would close the cycle.
            retained = self._managed.retained_until(handle)
            if retained is not None and now <= retained:
                continue
            self._runs.pop(handle, None)
            self._tombstones[handle] = now
        for request_id, ledger_entry in tuple(self._ledger.items()):
            if now - ledger_entry.touched_at >= self._ttl_seconds:
                self._ledger.pop(request_id, None)
        for handle, created in tuple(self._tombstones.items()):
            if now - created >= COORDINATOR_TOMBSTONE_TTL_SECONDS:
                self._tombstones.pop(handle, None)
        while len(self._tombstones) > MAX_COORDINATOR_TOMBSTONES:
            self._tombstones.popitem(last=False)

    def _managed_ledger_occupancy(self) -> tuple[int, int]:
        rows = 0
        retained_bytes = 0
        for state in self._managed_start_resources.values():
            occupied_rows = state.consumed_rows if state.ledger_released else state.reserved_rows
            rows += occupied_rows
            retained_bytes += occupied_rows * MAX_COORDINATOR_RESPONSE_BYTES
            retained_bytes += sum(
                child.retained_bytes() for child in state.completed_children.values()
            )
        return rows, retained_bytes

    def _held_managed_run_slots(self) -> int:
        """Count unbound M26 child slots against the coordinator's shared run bound."""

        return sum(
            1
            for state in self._managed_start_resources.values()
            if not state.child_slot_released and state.bound_child_run_handle is None
        )

    def _managed_start_state(
        self,
        parent_sequence_id: str,
        request_fingerprint: str,
    ) -> _ManagedStartResourceState:
        state = self._managed_start_resources.get(parent_sequence_id)
        if state is None or state.request_fingerprint != request_fingerprint:
            raise ManagedSequenceServiceError("start_resource_claim_unavailable", 409)
        return state

    def _managed_resource_for_child(
        self,
        run_handle: str,
    ) -> _ManagedStartResourceState | None:
        matched: _ManagedStartResourceState | None = None
        for state in self._managed_start_resources.values():
            if state.bound_child_run_handle != run_handle:
                continue
            if matched is not None:
                raise RuntimeError("managed child has multiple parent resource owners")
            matched = state
        return matched

    def _pending_managed_expiries(self) -> tuple[str, ...]:
        return tuple(
            state.parent_sequence_id
            for state in self._managed_start_resources.values()
            if state.expiry_pending
        )

    def _managed_resource_claim(
        self,
        state: _ManagedStartResourceState,
    ) -> ManagedSequenceStartResourceClaimV1:
        artifact = state.artifact_capacity
        if artifact is None:
            raise ManagedSequenceServiceError("start_resource_claim_incomplete", 500)
        parent_sequence_id = state.parent_sequence_id
        request_fingerprint = state.request_fingerprint
        return ManagedSequenceStartResourceClaimV1(
            request_fingerprint=request_fingerprint,
            coordinator_reservation_fingerprint=state.coordinator_fingerprint,
            child_slot_reservation_fingerprint=state.child_slot.fingerprint,
            artifact_reservation_fingerprint=artifact.fingerprint,
            consume=lambda request_id, rows: self._consume_managed_sequence_rows(
                parent_sequence_id,
                request_fingerprint,
                request_id,
                rows,
            ),
            release=lambda: self._release_managed_sequence_resources(
                parent_sequence_id,
                request_fingerprint,
            ),
        )

    def reserve_managed_sequence_resources(
        self,
        request: ManagedSequenceStartResourceRequestV1,
    ) -> ManagedSequenceStartResourceClaimV1:
        """Atomically admit shared ledger/run capacity, then bind artifact capacity separately."""

        if type(request) is not ManagedSequenceStartResourceRequestV1:
            raise ManagedSequenceServiceError("start_resource_request_type", 400)
        now = self._clock()
        if request.expires_at <= now:
            raise ManagedSequenceServiceError("start_resource_expired", 409)
        with self._lock:
            self._prune(now)
            existing = self._managed_start_resources.get(request.parent_sequence_id)
            if existing is not None:
                if existing.request_fingerprint != request.fingerprint:
                    raise ManagedSequenceServiceError("start_resource_conflict", 409)
                if existing.artifact_capacity is None:
                    raise ManagedSequenceServiceError("start_resource_claim_incomplete", 503)
                return self._managed_resource_claim(existing)
            if len(self._runs) + self._held_managed_run_slots() >= self._max_runs:
                raise ManagedSequenceServiceError("coordinator_run_capacity", 503)
            reserved_rows, reserved_bytes = self._managed_ledger_occupancy()
            retained_bytes = sum(
                len(canonical_bytes(item.response.to_wire())) for item in self._ledger.values()
            )
            if (
                len(self._ledger)
                + len(self._artifact_requests)
                + reserved_rows
                + request.coordinator_reserved_rows
                > MAX_COORDINATOR_LEDGER
                or retained_bytes
                + len(self._artifact_requests) * MAX_COORDINATOR_RESPONSE_BYTES
                + reserved_bytes
                + request.coordinator_reserved_bytes
                > MAX_COORDINATOR_RETAINED_BYTES
            ):
                raise ManagedSequenceServiceError("coordinator_ledger_capacity", 503)
            try:
                child_slot = self._managed.reserve_slot(
                    reservation_id="m26.slot." + request.parent_sequence_id,
                    parent_sequence_id=request.parent_sequence_id,
                    parent_authorization_fingerprint=(request.parent_authorization_fingerprint),
                    expires_at=request.expires_at,
                )
            except ManagedRunRegistryError as exc:
                raise ManagedSequenceServiceError("managed_run_slot_capacity", 503) from exc
            state = _ManagedStartResourceState(
                request_fingerprint=request.fingerprint,
                parent_sequence_id=request.parent_sequence_id,
                parent_authorization_fingerprint=(request.parent_authorization_fingerprint),
                reserved_rows=request.coordinator_reserved_rows,
                reserved_bytes=request.coordinator_reserved_bytes,
                idle_expires_at=request.idle_expires_at,
                expires_at=request.expires_at,
                child_slot=child_slot,
                segment_count=request.segment_count,
            )
            self._managed_start_resources[request.parent_sequence_id] = state

        artifact: ArtifactCapacityReservationV1 | None = None
        try:
            store = self._store()
            artifact = store.reserve_capacity(
                reservation_id="m26.artifact." + request.parent_sequence_id,
                parent_sequence_id=request.parent_sequence_id,
                parent_authorization_fingerprint=request.parent_authorization_fingerprint,
                segment_count=request.segment_count,
                expires_at_ms=request.expires_at_epoch_ms,
            )
            if (
                artifact.reserved_entries != request.artifact_reserved_entries
                or artifact.reserved_bytes != request.artifact_reserved_bytes
                or artifact.per_artifact_max_bytes != request.per_artifact_max_bytes
            ):
                raise ManagedSequenceServiceError("artifact_reservation_drift", 500)
        except Exception as exc:
            artifact_release_failed = False
            if artifact is not None:
                try:
                    self._store().release_capacity_reservation(artifact)
                except Exception:
                    artifact_release_failed = True
            with self._lock:
                current = self._managed_start_resources.get(request.parent_sequence_id)
                if current is state:
                    try:
                        self._managed.release_slot_reservation(state.child_slot)
                        state.child_slot_released = True
                    except ManagedRunRegistryError:
                        pass
                    state.ledger_released = True
            if artifact_release_failed:
                raise ManagedSequenceServiceError(
                    "artifact_reservation_release_failed",
                    500,
                ) from exc
            if isinstance(exc, ManagedSequenceServiceError):
                raise
            raise ManagedSequenceServiceError("artifact_capacity_unavailable", 503) from exc

        with self._lock:
            current = self._managed_start_resources.get(request.parent_sequence_id)
            if current is state and current.request_fingerprint == request.fingerprint:
                current.artifact_capacity = artifact
                return self._managed_resource_claim(current)
        # CRITICAL: artifact store and coordinator locks are never nested. A concurrent release or
        # replacement wins; compensate the exact artifact reservation only after dropping the
        # coordinator lock.
        try:
            self._store().release_capacity_reservation(artifact)
        except Exception as exc:
            raise ManagedSequenceServiceError("start_resource_compensation_failed", 503) from exc
        raise ManagedSequenceServiceError("start_resource_claim_drift", 409)

    def _consume_managed_sequence_rows(
        self,
        parent_sequence_id: str,
        request_fingerprint: str,
        request_id: str,
        rows: int,
    ) -> None:
        if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
            raise ManagedSequenceServiceError("resource_consumption_request_id", 500)
        if type(rows) is not int or not 1 <= rows <= MAX_COORDINATOR_LEDGER:
            raise ManagedSequenceServiceError("resource_consumption_rows", 500)
        with self._lock:
            state = self._managed_start_state(parent_sequence_id, request_fingerprint)
            existing = state.consumed_requests.get(request_id)
            if existing is not None:
                if existing != rows:
                    raise ManagedSequenceServiceError("resource_consumption_conflict", 409)
                return
            if state.ledger_released:
                raise ManagedSequenceServiceError("resource_consumption_released", 409)
            if state.consumed_rows + rows > state.reserved_rows:
                raise ManagedSequenceServiceError("resource_consumption_capacity", 503)
            state.consumed_requests[request_id] = rows
            state.consumed_rows += rows

    def _release_managed_sequence_resources(
        self,
        parent_sequence_id: str,
        request_fingerprint: str,
    ) -> None:
        artifact: ArtifactCapacityReservationV1 | None = None
        with self._lock:
            state = self._managed_start_state(parent_sequence_id, request_fingerprint)
            if not state.child_slot_released:
                try:
                    self._managed.release_slot_reservation(state.child_slot)
                except ManagedRunRegistryError as exc:
                    raise ManagedSequenceServiceError(
                        "managed_run_slot_release_failed", 503
                    ) from exc
                state.child_slot_released = True
            state.ledger_released = True
            if not state.artifact_capacity_released:
                artifact = state.artifact_capacity
        if artifact is not None:
            try:
                self._store().release_capacity_reservation(artifact)
            except ArtifactStoreError as exc:
                raise ManagedSequenceServiceError("artifact_capacity_release_failed", 503) from exc
            with self._lock:
                state = self._managed_start_state(parent_sequence_id, request_fingerprint)
                state.artifact_capacity_released = True

    def bind_managed_sequence_child(
        self,
        request: ManagedChildBindingRequestV1,
    ) -> ManagedChildBindingClaimV1:
        """Build and reserve exactly one child without entering legacy ``_prepare``."""

        if type(request) is not ManagedChildBindingRequestV1:
            raise ManagedSequenceServiceError("child_binding_request_type", 400)
        execution = request.execution
        now = self._clock()
        with self._lock:
            self._prune(now)
            resource = self._managed_start_resources.get(execution.parent_sequence_id)
            if (
                resource is None
                or resource.parent_authorization_fingerprint
                != execution.parent_authorization_fingerprint
                or resource.artifact_capacity is None
                or resource.ledger_released
                or resource.child_slot_released
                or resource.artifact_capacity_released
                or resource.bound_child_run_handle is not None
            ):
                raise ManagedSequenceServiceError("child_binding_resource_unavailable", 409)
            if len(self._runs) >= self._max_runs:
                raise ManagedSequenceServiceError("child_binding_coordinator_capacity", 503)

            if (
                request.source_authority is not None
                or request.production_workspace_handle is not None
            ):
                return self._bind_original_managed_child(request, resource, now)

            correlation = ExecutionCorrelation(
                request.correlation.pending_correlation_id,
                request.correlation.execution_node_id,
            )
            observation = PreparedGraphObservation.from_wire(request.observation.to_wire())
            production_request_id = (
                "managed.child.production."
                + sha256(execution.fingerprint.encode("ascii")).hexdigest()[:48]
            )
            try:
                created = self._production.dispatch(
                    {
                        "schema": PRODUCTION_ACTION_SCHEMA,
                        "request_id": production_request_id,
                        "action": "create_workspace_from_context",
                        "payload": {"context_workspace_handle": request.context_workspace_handle},
                    }
                )
            except (ValueError, ProductionWorkbenchError) as exc:
                raise ManagedSequenceServiceError("child_production_creation_failed", 409) from exc
            production = created.projection
            # A child create must return the concrete workspace, never an aggregate read.
            if not isinstance(production, ProductionWorkbenchProjection):
                raise ManagedSequenceServiceError("child_production_creation_failed", 500)

            production_attached = False
            managed_created = False
            run_handle: str | None = None
            state: GenerationSequenceState | None = None
            workspace: MultiSegmentWorkspace | None = None
            try:
                workspace = self._production.claim_workspace_authority(
                    production.workspace_handle,
                    expected_workspace_revision=production.workspace_revision,
                    expected_workspace_fingerprint=production.workspace_fingerprint,
                )
                if not single_segment_sequence(len(workspace.segments)):
                    raise SequenceCoordinatorError("managed_workspace_shape", 422)
                manifest = derive_segment_manifests(workspace)[0]
                if observation.expected_frames != manifest.duration.frame_count:
                    raise SequenceCoordinatorError("observation_duration_mismatch", 422)
                base = plan_recompute((manifest,), (manifest,))
                recompute = extend_recompute_plan(
                    (manifest,),
                    base,
                    forced_dirty_segment_ids=(manifest.segment_id,),
                    forced_reason_codes=((manifest.segment_id, ("initial_managed_generation",)),),
                )
                digest = observation.graph_fingerprint.removeprefix("sha256:")
                spec = GenerationJobSpec(
                    segment_id=manifest.segment_id,
                    job_id=f"job.{manifest.ordinal}.{digest[:32]}",
                    graph_fingerprint=observation.graph_fingerprint,
                    compiled_prompt_fingerprint=observation.compiled_prompt_fingerprint,
                    model_fingerprint=observation.model_fingerprint,
                    runtime_fingerprint=observation.runtime_fingerprint,
                    expected_format="observed_video",
                    expected_shape=(observation.expected_frames,),
                    timeout_ms=observation.timeout_ms,
                    fingerprint_domain=observation.fingerprint_domain,
                )
                plan = build_generation_sequence_plan(
                    workspace,
                    (manifest,),
                    recompute,
                    (spec,),
                )
                state = create_generation_sequence_state(plan)
                geometry_receipt: InputGeometryReceipt | None = None
                if observation.source_identity is not None:
                    geometry_receipt = self._geometry_receipt_claimant(
                        observation.source_identity.to_wire()
                    )
                run_handle = self._new_handle()
                sequence = build_generation_sequence_projection(state, correlation)
                published = self._production.replace_generation_authority(
                    production.workspace_handle,
                    expected_workspace_revision=production.workspace_revision,
                    expected_workspace_fingerprint=production.workspace_fingerprint,
                    expected_sequence_state_fingerprint=None,
                    generation_sequence=sequence,
                )
                production_attached = True
                self._managed.create_reserved(run_handle, resource.child_slot)
                managed_created = True
                # CRITICAL: the parent-first lease owner must classify expiry before the generic
                # ManagedRun TTL can prune this child. Exact release or detach ends the protection.
                self._managed.protect_for_parent_transition(run_handle)
                self._managed.advance(
                    run_handle,
                    ManagedRunTrigger.STAGE_CONTEXT,
                    context_revision=workspace.revision,
                )
                self._managed.advance(
                    run_handle,
                    ManagedRunTrigger.CREATE_PRODUCTION,
                    production_workspace=production.workspace_handle,
                    segment_count=1,
                )
                self._managed.advance(
                    run_handle,
                    ManagedRunTrigger.PREPARE_SEQUENCE,
                    prepared_sequence=run_handle,
                )
                if geometry_receipt is not None:
                    self._managed.attach_geometry(run_handle, geometry_receipt.receipt_handle)
                entry = _RunEntry(
                    run_handle=run_handle,
                    production_handle=production.workspace_handle,
                    workspace=workspace,
                    observation=observation,
                    correlation=correlation,
                    state=state,
                    production=published,
                    touched_at=now,
                    managed_creation_request_id=production_request_id,
                )
                self._runs[run_handle] = entry
                resource.bound_child_run_handle = run_handle
                resource.pre_submit_cleaned_child_run_handle = None
                resource.expiry_transition_complete = False
            except Exception as exc:
                try:
                    if production_attached and state is not None:
                        self._production._rollback_unpublished_managed_creation(
                            request_id=production_request_id,
                            workspace_handle=production.workspace_handle,
                            expected_workspace_revision=production.workspace_revision,
                            expected_workspace_fingerprint=production.workspace_fingerprint,
                            expected_sequence_state_fingerprint=state.fingerprint,
                            release_live_sequence=(
                                (lambda: self._managed.release(run_handle))
                                if managed_created and run_handle is not None
                                else (lambda: None)
                            ),
                        )
                    else:
                        self._production._rollback_unpublished_creation(
                            request_id=production_request_id,
                            workspace_handle=production.workspace_handle,
                            expected_workspace_revision=production.workspace_revision,
                            expected_workspace_fingerprint=production.workspace_fingerprint,
                        )
                except Exception as rollback_error:
                    raise ManagedSequenceServiceError(
                        "child_binding_compensation_failed", 500
                    ) from rollback_error
                if isinstance(exc, ManagedSequenceServiceError):
                    raise
                raise ManagedSequenceServiceError("child_binding_failed", 409) from exc

            if run_handle is None or state is None:  # pragma: no cover - guarded above
                raise ManagedSequenceServiceError("child_binding_failed", 500)
            child_projection = build_generation_sequence_projection(state, correlation)
            if len(child_projection.eligible_commands) != 1:
                raise ManagedSequenceServiceError("child_binding_command_count", 500)
            child_command = child_projection.eligible_commands[0]
            rollback_lock = threading.Lock()
            rolled_back = False

            def rollback() -> None:
                nonlocal rolled_back
                with rollback_lock:
                    if rolled_back:
                        return
                    with self._lock:
                        current = self._runs.get(run_handle)
                        current_resource = self._managed_start_resources.get(
                            execution.parent_sequence_id
                        )
                        if (
                            current is None
                            or current is not entry
                            or current_resource is not resource
                            or resource.bound_child_run_handle != run_handle
                        ):
                            raise ManagedSequenceServiceError(
                                "child_binding_rollback_authority", 409
                            )
                        self._production._rollback_unpublished_managed_creation(
                            request_id=production_request_id,
                            workspace_handle=entry.production_handle,
                            expected_workspace_revision=entry.workspace.revision,
                            expected_workspace_fingerprint=entry.workspace.fingerprint,
                            expected_sequence_state_fingerprint=entry.state.fingerprint,
                            release_live_sequence=lambda: self._managed.release(run_handle),
                        )
                        self._runs.pop(run_handle)
                        resource.bound_child_run_handle = None
                        resource.pre_submit_cleaned_child_run_handle = run_handle
                        rolled_back = True

            return ManagedChildBindingClaimV1(
                child_run_handle=run_handle,
                child_state_fingerprint=state.fingerprint,
                child_job_id=child_command.job_id,
                child_transaction_id=child_command.transaction_id,
                rollback=rollback,
            )

    def _bind_original_managed_child(
        self,
        request: ManagedChildBindingRequestV1,
        resource: _ManagedStartResourceState,
        now: float,
    ) -> ManagedChildBindingClaimV1:
        source = request.source_authority
        handle = request.production_workspace_handle
        if type(source) is not ProductionAutomaticPlanAuthorityV2 or type(handle) is not str:
            raise ManagedSequenceServiceError("child_original_authority", 409)
        try:
            current = self._production.claim_automatic_plan_authority(
                handle,
                expected_workspace_revision=source.workspace.revision,
                expected_workspace_fingerprint=source.workspace.fingerprint,
                expected_plan_fingerprint=source.fingerprint,
            )
        except ProductionWorkbenchError as exc:
            raise ManagedSequenceServiceError(exc.code, exc.status) from exc
        manifests = derive_segment_manifests(source.workspace)
        matches = tuple(row for row in manifests if row.segment_id == request.execution.segment_id)
        if (
            current is not source
            or any(
                hold is not ProposalBlockerCodeV1.MANAGED_EXECUTION_QUALIFICATION_PENDING
                for hold in source.start_hold_codes
            )
            or len(manifests) != resource.segment_count
            or len(matches) != 1
            or any(row.dependency_segment_ids for row in manifests)
            or (
                resource.original_authority is not None
                and (
                    resource.original_authority is not source or resource.original_handle != handle
                )
            )
        ):
            raise ManagedSequenceServiceError("child_original_authority", 409)
        manifest = matches[0]
        if manifest.ordinal != len(resource.completed_children) + 1:
            raise ManagedSequenceServiceError("child_original_order", 409)
        materialization = source.materialization_receipts[manifest.ordinal - 1]
        if materialization.fingerprint != request.execution.materialization_receipt_fingerprint:
            raise ManagedSequenceServiceError("child_materialization_authority", 409)
        observation = PreparedGraphObservation.from_wire(request.observation.to_wire())
        if observation.expected_frames != manifest.duration.frame_count:
            raise ManagedSequenceServiceError("observation_duration_mismatch", 422)
        # CRITICAL: bind the original manifest before creating the real transaction. Relabeling
        # a child-workspace receipt after capture destroys the immutable provenance chain.
        recompute = extend_recompute_plan(
            manifests,
            plan_recompute(manifests, manifests),
            forced_dirty_segment_ids=(manifest.segment_id,),
            forced_reason_codes=((manifest.segment_id, ("initial_managed_generation",)),),
        )
        spec = GenerationJobSpec(
            segment_id=manifest.segment_id,
            job_id=f"job.{manifest.ordinal}.{observation.graph_fingerprint.removeprefix('sha256:')[:32]}",
            graph_fingerprint=observation.graph_fingerprint,
            compiled_prompt_fingerprint=observation.compiled_prompt_fingerprint,
            model_fingerprint=observation.model_fingerprint,
            runtime_fingerprint=observation.runtime_fingerprint,
            expected_format="observed_video",
            expected_shape=(observation.expected_frames,),
            timeout_ms=observation.timeout_ms,
            fingerprint_domain=observation.fingerprint_domain,
        )
        state = create_generation_sequence_state(
            build_generation_sequence_plan(source.workspace, manifests, recompute, (spec,))
        )
        correlation = ExecutionCorrelation(
            request.correlation.pending_correlation_id, request.correlation.execution_node_id
        )
        sequence = build_generation_sequence_projection(state, correlation)
        production = self._production.project_managed_child_generation(
            source.workspace, handle, sequence
        )
        geometry = (
            None
            if observation.source_identity is None
            else self._geometry_receipt_claimant(observation.source_identity.to_wire())
        )
        run_handle = self._new_handle()
        reference = SequenceProductionAuthorityV1(
            handle,
            source.workspace.workspace_id,
            source.workspace.revision,
            source.workspace.fingerprint,
            source.fingerprint,
        )
        admission_response = SequenceCoordinatorResponse(
            run_handle,
            "current",
            sequence,
            production,
            None,
            None,
            MANAGED_CHILD_RESPONSE_SCHEMA,
            production_authority=reference,
        )
        if _managed_child_response_size_bound(admission_response) > MAX_COORDINATOR_RESPONSE_BYTES:
            raise ManagedSequenceServiceError("child_response_capacity", 503)
        created = False
        try:
            self._managed.create_reserved(run_handle, resource.child_slot)
            created = True
            self._managed.protect_for_parent_transition(run_handle)
            self._managed.advance(
                run_handle,
                ManagedRunTrigger.STAGE_CONTEXT,
                context_revision=source.workspace.revision,
            )
            self._managed.advance(
                run_handle,
                ManagedRunTrigger.CREATE_PRODUCTION,
                production_workspace=handle,
                segment_count=1,
            )
            self._managed.advance(
                run_handle, ManagedRunTrigger.PREPARE_SEQUENCE, prepared_sequence=run_handle
            )
            if geometry is not None:
                self._managed.attach_geometry(run_handle, geometry.receipt_handle)
            entry = _RunEntry(
                run_handle=run_handle,
                production_handle=handle,
                workspace=source.workspace,
                observation=observation,
                correlation=correlation,
                state=state,
                production=production,
                touched_at=now,
                original_automatic_authority=source,
            )
            self._runs[run_handle] = entry
            resource.original_authority = source
            resource.original_handle = handle
            resource.bound_child_run_handle = run_handle
            resource.pre_submit_cleaned_child_run_handle = None
            resource.expiry_transition_complete = False
        except Exception:
            if created:
                self._managed.release(run_handle)
            raise
        rolled_back = False

        def rollback() -> None:
            nonlocal rolled_back
            with self._lock:
                if rolled_back:
                    return
                if (
                    self._runs.get(run_handle) is not entry
                    or resource.bound_child_run_handle != run_handle
                ):
                    raise ManagedSequenceServiceError("child_binding_rollback_authority", 409)
                # CRITICAL: a managed child borrows the original Production handle. Its rollback
                # releases only the child run, never the parent's accepted workspace or outputs.
                self._managed.release(run_handle)
                self._runs.pop(run_handle)
                resource.bound_child_run_handle = None
                resource.pre_submit_cleaned_child_run_handle = run_handle
                rolled_back = True

        command = sequence.eligible_commands[0]
        return ManagedChildBindingClaimV1(
            child_run_handle=run_handle,
            child_state_fingerprint=state.fingerprint,
            child_job_id=command.job_id,
            child_transaction_id=command.transaction_id,
            rollback=rollback,
        )

    def publish_completed_managed_sequence(
        self, source_authority: object, workspace_handle: str, candidate: ManagedSequenceV1
    ) -> None:
        """Publish only coordinator-owned completed children, before parent success commits."""
        if type(source_authority) is not ProductionAutomaticPlanAuthorityV2:
            raise ManagedSequenceServiceError("production_publication_authority", 409)
        with self._lock:
            resource = self._managed_start_resources.get(candidate.authorization.sequence_id)
            if (
                resource is None
                or resource.original_authority is not source_authority
                or resource.original_handle != workspace_handle
                or resource.parent_authorization_fingerprint != candidate.authorization.fingerprint
                or candidate.state is not ManagedSequenceStateV1.SUCCEEDED
                or resource.bound_child_run_handle is not None
                or len(resource.completed_children) != len(candidate.slots)
            ):
                raise ManagedSequenceServiceError("production_publication_authority", 409)
            children: list[_CompletedManagedChild] = []
            for slot in candidate.slots:
                child = resource.completed_children.get(slot.segment_id)
                transaction = None if child is None else child.state.runtimes[0].transaction
                if (
                    child is None
                    or transaction is None
                    or slot.state not in {SequenceSlotStateV1.SUCCEEDED, SequenceSlotStateV1.REUSED}
                    or slot.artifact_receipt_fingerprint != child.receipt.fingerprint
                    or slot.terminal_fingerprint != child.terminal_fingerprint
                    or (
                        slot.state is SequenceSlotStateV1.SUCCEEDED
                        and (
                            slot.child_run_handle != child.run_handle
                            or slot.queue_prompt_id != transaction.queue_prompt_id
                        )
                    )
                ):
                    raise ManagedSequenceServiceError("production_publication_child_mismatch", 409)
                children.append(child)
            accounting = candidate.artifact_reservation
            # CRITICAL: caller-reported byte counts cannot discharge the parent's reservation.
            # Successful publication must account for the exact bytes captured by the store.
            if (
                accounting is None
                or accounting.consumed_entries != len(children)
                or accounting.consumed_bytes != sum(child.receipt.byte_length for child in children)
            ):
                raise ManagedSequenceServiceError("production_publication_accounting", 409)
            captured_children = tuple(resource.completed_children.items())
            captured_release_state = (
                resource.ledger_released,
                resource.artifact_capacity_released,
            )
            if not self._artifact_slots.acquire(blocking=False):
                raise ManagedSequenceServiceError("production_publication_capacity", 503)
        try:
            sequence = compose_completed_managed_generation(
                source_authority.workspace,
                tuple(child.state for child in children),
                tuple(child.receipt for child in children),
                children[0].correlation,
            )
            store = self._store()
            # CRITICAL: retained metadata cannot revive deleted, expired or changed originals.
            # Verify the same bounded store before the original parent's success publication.
            if any(
                store.inspect(child.receipt).status is not ArtifactInspectionStatus.REUSABLE
                for child in children
            ):
                raise ManagedSequenceServiceError(
                    "production_publication_artifact_unavailable", 409
                )
            previews = self._production.prepare_generated_preview_sources(
                sequence,
                tuple(child.receipt for child in children),
                store,
            )
            with self._lock:
                # CRITICAL: no stored-byte verification holds the coordinator lock. Recheck the
                # same completed owners after I/O; released/expired/drifting results cannot publish.
                if (
                    self._managed_start_resources.get(candidate.authorization.sequence_id)
                    is not resource
                    or tuple(resource.completed_children.items()) != captured_children
                    or resource.bound_child_run_handle is not None
                    # CRITICAL: publication can precede partial cleanup failure. Exact replay
                    # may enter with released rows; refuse changes during verification instead.
                    or (resource.ledger_released, resource.artifact_capacity_released)
                    != captured_release_state
                    or resource.expiry_pending
                    or resource.expiry_transition_complete
                    or self._clock() > resource.expires_at
                ):
                    raise ManagedSequenceServiceError("production_publication_authority", 409)
                self._production.replace_generation_authority(
                    workspace_handle,
                    expected_workspace_revision=source_authority.workspace.revision,
                    expected_workspace_fingerprint=source_authority.workspace.fingerprint,
                    expected_sequence_state_fingerprint=None,
                    generation_sequence=sequence,
                    artifact_receipts=tuple(child.receipt for child in children),
                    artifact_store=store,
                    expected_automatic_authority=source_authority,
                    _prepared_preview_sources=previews,
                )
        except ProductionWorkbenchError as exc:
            raise ManagedSequenceServiceError(exc.code, exc.status) from exc
        except GenerationSequenceError as exc:
            raise ManagedSequenceServiceError("production_publication_invalid", 409) from exc
        except ArtifactStoreError as exc:
            raise ManagedSequenceServiceError(
                "production_publication_artifact_unavailable", 409
            ) from exc
        finally:
            self._artifact_slots.release()

    def _new_handle(self) -> str:
        token = self._token_factory()
        handle = f"mc_{token}"
        if (
            _RUN_HANDLE.fullmatch(handle) is None
            or handle in self._runs
            or handle in self._tombstones
        ):
            raise SequenceCoordinatorError("coordinator_identity_unavailable", 503)
        return handle

    def _entry(self, handle: object) -> _RunEntry:
        if type(handle) is not str or _RUN_HANDLE.fullmatch(handle) is None:
            raise SequenceCoordinatorError("invalid_run_handle", 400)
        entry = self._runs.get(handle)
        if entry is not None:
            return entry
        if handle in self._tombstones:
            raise SequenceCoordinatorError("run_gone", 410)
        raise SequenceCoordinatorError("run_unavailable", 404)

    @staticmethod
    def _expect_state(entry: _RunEntry, value: object) -> None:
        expected = _fingerprint(value, "expected_state_fingerprint")
        if expected != entry.state.fingerprint:
            raise SequenceCoordinatorError("stale_sequence_state", 409)

    def _response(
        self,
        entry: _RunEntry,
        disposition: str,
        *,
        terminal_authority: object = _TERMINAL_AUTHORITY_UNRESOLVED,
    ) -> SequenceCoordinatorResponse:
        projection = build_generation_sequence_projection(entry.state, entry.correlation)
        receipt = entry.artifact_receipt
        artifact_authority = (
            None
            if receipt is None
            else SequenceArtifactAuthorityV1(
                receipt_fingerprint=receipt.fingerprint,
                byte_length=receipt.byte_length,
            )
        )
        if terminal_authority is _TERMINAL_AUTHORITY_UNRESOLVED:
            observed_terminal_fingerprint = (
                terminal_fingerprint(self._managed.read(entry.run_handle))
                if _first_runtime(entry.state).state in _TERMINAL_JOB_STATES
                else None
            )
        elif terminal_authority is None or type(terminal_authority) is str:
            # CRITICAL: release already decided and mutated under its own authority. Never read the
            # registry again here: a detach whose fixed deadline has passed can be pruned on that
            # second read even though the release decision itself succeeded.
            observed_terminal_fingerprint = terminal_authority
        else:  # pragma: no cover - internal call contract
            raise RuntimeError("invalid terminal response authority")
        original = entry.original_automatic_authority
        reference = (
            None
            if original is None
            else SequenceProductionAuthorityV1(
                entry.production_handle,
                original.workspace.workspace_id,
                original.workspace.revision,
                original.workspace.fingerprint,
                original.fingerprint,
            )
        )
        member = (
            None
            if entry.member_workspace_id is None
            else SequenceProductionMemberAuthorityV1(
                entry.production_handle,
                entry.member_workspace_id,
                entry.state.plan.jobs[0].segment_id,
            )
        )
        # CRITICAL: the original owner may contain fifteen segments, and a project up to sixty-four.
        # Keep the complete private projection for validation, but transport only a reference.
        return SequenceCoordinatorResponse(
            run_handle=entry.run_handle,
            disposition=disposition,
            sequence=projection,
            production=entry.production,
            artifact_authority=artifact_authority,
            terminal_fingerprint=observed_terminal_fingerprint,
            schema=(
                MANAGED_CHILD_RESPONSE_SCHEMA
                if reference is not None
                else MANAGED_MEMBER_RESPONSE_SCHEMA
                if member is not None
                else COORDINATOR_RESPONSE_SCHEMA
            ),
            production_authority=reference,
            production_member_authority=member,
        )

    def _mirror(
        self,
        entry: _RunEntry,
        state: GenerationSequenceState,
        artifact_receipt: SegmentArtifactReceipt | None,
        queue_prompt_id: str | None = None,
    ) -> None:
        """Apply to the aggregate the transitions this publish represents.

        CRITICAL: this is called from `_publish`, which is the coordinator's only state mutation
        point, so a new handler cannot forget to mirror. The aggregate is a second record of the
        same run, which is only safe because `test_m23_31_coordinator_projection.py` asserts the two
        never disagree; if that check is ever removed, this becomes exactly the divergent-authority
        defect M23-31 exists to remove.

        CRITICAL: stay after Production publication (legacy) or validated private child projection
        (managed parent), and before entry mutation. Mirroring before a refused CAS/projection
        leaves the aggregate ahead of a coordinator entry that never moved. An aggregate refusal
        means these two records disagree and must remain a loud invariant failure.
        """

        # CRITICAL: only `ManagedRunRegistryError` -- the row is gone -- may be translated, and it
        # must be caught *before* any `except ManagedRunError`, which it subclasses. The two records
        # agreeing is an asserted invariant, so a disagreement (an illegal transition) is a genuine
        # defect and has to stay a loud failure; catching the parent here would answer a tidy 410
        # for it and hide exactly what the comment above says must not be hidden.
        #
        # The row can be gone while this coordinator entry survives because the two lifetimes are
        # independent in *both* directions. `retained_until` closed the direction where the lease
        # outlives the row; this is the converse, where a lease shorter than the 900-second row
        # expires first. Left unguarded it escaped as a 500 `internal_failure`, and the client's own
        # `appModeCorrelation.ts` counts only 404/410/`run_authority_mismatch` as authority-gone, so
        # a resolved run read as retryable and the browser retried what could never succeed.
        try:
            aggregate_state = self._managed.read(entry.run_handle).state
        except ManagedRunRegistryError as exc:
            raise SequenceCoordinatorError("run_gone", 410) from exc

        triggers = lifecycle_triggers(
            _first_runtime(entry.state).state,
            _first_runtime(state).state,
            aggregate_state,
            artifact_recorded=entry.artifact_receipt is None and artifact_receipt is not None,
        )
        for trigger in triggers:
            # CRITICAL: a mirrored transition must carry the identity it establishes, not just its
            # name. `record_submission` without `prompt_id` leaves the aggregate in `submitted`
            # holding no prompt, and the aggregate is what a detached run is retained *as* -- a
            # returning client would then have no exact prompt id to read `/history/{prompt_id}`
            # with, and the M23-51 terminal proof would be computed over an absent field for every
            # run alike. The missing prompt id raises rather than defaulting: a submission the
            # coordinator recorded without one is a coordinator defect, and silently storing
            # `None` is how it would stay invisible.
            fields: dict[str, object] = {}
            if trigger is ManagedRunTrigger.RECORD_SUBMISSION:
                if queue_prompt_id is None:
                    raise RuntimeError("mirrored submission without an accepted prompt id")
                fields["prompt_id"] = queue_prompt_id
            elif trigger is ManagedRunTrigger.RECORD_ARTIFACT and artifact_receipt is not None:
                # CRITICAL: the published receipt fingerprint, never `artifact_id`. The terminal
                # proof is hashed over this field and has to be reconstructible by whoever observed
                # the terminal projection; `artifact_id` is an internal store identifier the client
                # never sees, so hashing it would leave `cleanup_terminal` unreachable and would
                # put a storage identifier into every retained row.
                fields["artifact_receipt"] = artifact_receipt.fingerprint
            try:
                self._managed.advance(entry.run_handle, trigger, **fields)
            except ManagedRunRegistryError as exc:
                raise SequenceCoordinatorError("run_gone", 410) from exc

    def _publish(
        self,
        entry: _RunEntry,
        *,
        previous_state_fingerprint: str | None,
        state: GenerationSequenceState,
        artifact_receipt: SegmentArtifactReceipt | None,
        now: float,
        queue_prompt_id: str | None = None,
        correlation: ExecutionCorrelation | None = None,
    ) -> None:
        published_correlation = entry.correlation if correlation is None else correlation
        sequence = build_generation_sequence_projection(state, published_correlation)
        member_attempt = entry.member_attempt
        preview_sources = self._production.bind_prepared_preview_sources(
            sequence,
            (() if artifact_receipt is None else (artifact_receipt,)),
            entry.artifact_preview_source,
        )
        try:
            if entry.member_workspace_id is not None:
                if member_attempt is None:  # pragma: no cover - prepared with its attempt
                    raise SequenceCoordinatorError("run_authority_mismatch", 500)
                production = self._production.project_managed_child_generation(
                    entry.workspace, entry.production_handle, sequence, artifact_receipt
                )
                # IMPORTANT: CAS the member's exact latest attempt, not the project revision.
                # Other members append, and the user reorders or selects, while this one runs.
                member_attempt = self._production.publish_member_attempt(
                    entry.production_handle,
                    segment_id=member_attempt.segment_id,
                    previous_attempt=member_attempt,
                    generation_sequence=sequence,
                    artifact_receipt=artifact_receipt,
                    artifact_store=None if artifact_receipt is None else self._store(),
                    _prepared_preview_sources=preview_sources,
                )
            elif entry.original_automatic_authority is not None:
                production = self._production.project_managed_child_generation(
                    entry.workspace, entry.production_handle, sequence, artifact_receipt
                )
            else:
                production = self._production.replace_generation_authority(
                    entry.production_handle,
                    expected_workspace_revision=entry.workspace.revision,
                    expected_workspace_fingerprint=entry.workspace.fingerprint,
                    expected_sequence_state_fingerprint=previous_state_fingerprint,
                    generation_sequence=sequence,
                    artifact_receipts=(() if artifact_receipt is None else (artifact_receipt,)),
                    artifact_store=(None if artifact_receipt is None else self._store()),
                    _prepared_preview_sources=preview_sources,
                )
        except ProductionWorkbenchError as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        self._mirror(entry, state, artifact_receipt, queue_prompt_id)
        entry.state = state
        entry.correlation = published_correlation
        entry.production = production
        entry.member_attempt = member_attempt
        entry.artifact_receipt = artifact_receipt
        entry.touched_at = now
        self._runs.move_to_end(entry.run_handle)

    @staticmethod
    def _single_job_state(
        workspace: MultiSegmentWorkspace,
        observation: PreparedGraphObservation,
    ) -> GenerationSequenceState:
        """Plan the one managed job a single-segment workspace executes."""

        if not single_segment_sequence(len(workspace.segments)):
            raise SequenceCoordinatorError("managed_workspace_shape", 422)
        manifests = derive_segment_manifests(workspace)
        manifest = manifests[0]
        if observation.expected_frames != manifest.duration.frame_count:
            raise SequenceCoordinatorError("observation_duration_mismatch", 422)
        base = plan_recompute(manifests, manifests)
        recompute = extend_recompute_plan(
            manifests,
            base,
            forced_dirty_segment_ids=(manifest.segment_id,),
            forced_reason_codes=((manifest.segment_id, ("initial_managed_generation",)),),
        )
        digest = observation.graph_fingerprint.removeprefix("sha256:")
        spec = GenerationJobSpec(
            segment_id=manifest.segment_id,
            job_id=f"job.{manifest.ordinal}.{digest[:32]}",
            graph_fingerprint=observation.graph_fingerprint,
            compiled_prompt_fingerprint=observation.compiled_prompt_fingerprint,
            model_fingerprint=observation.model_fingerprint,
            runtime_fingerprint=observation.runtime_fingerprint,
            expected_format="observed_video",
            expected_shape=(observation.expected_frames,),
            timeout_ms=observation.timeout_ms,
            fingerprint_domain=observation.fingerprint_domain,
        )
        try:
            plan = build_generation_sequence_plan(
                workspace,
                manifests,
                recompute,
                (spec,),
            )
            return create_generation_sequence_state(plan)
        except (GenerationSequenceError, ValueError) as exc:
            raise SequenceCoordinatorError("sequence_plan_rejected", 422) from exc

    def _prepare(self, payload: dict[str, object], now: float) -> SequenceCoordinatorResponse:
        wire = _closed(
            payload,
            {
                "workspace_handle",
                "expected_workspace_revision",
                "expected_workspace_fingerprint",
                "correlation",
                "observation",
            },
            "prepare_payload",
        )
        handle = wire["workspace_handle"]
        if type(handle) is not str or _WORKSPACE_HANDLE.fullmatch(handle) is None:
            raise SequenceCoordinatorError("invalid_workspace_handle", 400)
        revision = _bounded_int(
            wire["expected_workspace_revision"], "workspace_revision", 1, 1_000_000
        )
        workspace_fingerprint = _fingerprint(
            wire["expected_workspace_fingerprint"], "workspace_fingerprint"
        )
        correlation_wire = _closed(
            wire["correlation"], {"prompt_id", "execution_node_id"}, "correlation"
        )
        try:
            correlation = ExecutionCorrelation(
                _identifier(correlation_wire["prompt_id"], "prompt_id"),
                _identifier(correlation_wire["execution_node_id"], "execution_node_id"),
            )
            observation = PreparedGraphObservation.from_wire(wire["observation"])
            workspace = self._production.claim_workspace_authority(
                handle,
                expected_workspace_revision=revision,
                expected_workspace_fingerprint=workspace_fingerprint,
            )
        except ProductionWorkbenchError as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        state = self._single_job_state(workspace, observation)
        if len(self._runs) + self._held_managed_run_slots() >= self._max_runs:
            raise SequenceCoordinatorError("coordinator_capacity", 429)
        run_handle = self._new_handle()
        initial_sequence = build_generation_sequence_projection(state, correlation)
        geometry_receipt: InputGeometryReceipt | None = None
        if observation.source_identity is not None:
            try:
                geometry_receipt = self._geometry_receipt_claimant(
                    observation.source_identity.to_wire()
                )
            except GeometryPreflightError as exc:
                raise SequenceCoordinatorError(exc.code, exc.status) from exc
        try:
            production = self._production.replace_generation_authority(
                handle,
                expected_workspace_revision=revision,
                expected_workspace_fingerprint=workspace_fingerprint,
                expected_sequence_state_fingerprint=None,
                generation_sequence=initial_sequence,
            )
        except ProductionWorkbenchError as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        entry = _RunEntry(
            run_handle=run_handle,
            production_handle=handle,
            workspace=workspace,
            observation=observation,
            correlation=correlation,
            state=state,
            production=production,
            touched_at=now,
        )
        self._runs[run_handle] = entry
        # CRITICAL: the aggregate is created here, at the first moment a run has an identity, and
        # the two joins it has already made are recorded with the authoritative values `_prepare`
        # holds. The context and production facts predate the run handle, so recording them later
        # would mean reconstructing them from a second source -- which is the class of defect
        # M23-19 was.
        self._managed.create(run_handle)
        self._managed.advance(
            run_handle, ManagedRunTrigger.STAGE_CONTEXT, context_revision=workspace.revision
        )
        self._managed.advance(
            run_handle,
            ManagedRunTrigger.CREATE_PRODUCTION,
            production_workspace=handle,
            segment_count=len(workspace.segments),
        )
        self._managed.advance(
            run_handle, ManagedRunTrigger.PREPARE_SEQUENCE, prepared_sequence=run_handle
        )
        if geometry_receipt is not None:
            # The geometry store observes a receipt and this claims it; the aggregate is what
            # records that the claim belongs to this run, and refuses a second claim of the same
            # receipt. The store alone cannot, because it has no run identity.
            self._managed.attach_geometry(run_handle, geometry_receipt.receipt_handle)
        return self._response(entry, "prepared")

    def _prepare_managed(
        self,
        request_id: str,
        payload: dict[str, object],
        now: float,
    ) -> SequenceCoordinatorResponse:
        if type(payload) is dict and "production_admission_request_id" in payload:
            return self._prepare_member(payload, now)
        wire = _closed(
            payload,
            {"context_workspace_handle", "correlation", "observation"},
            "managed_prepare_payload",
        )
        context_handle = wire["context_workspace_handle"]
        if type(context_handle) is not str:
            raise SequenceCoordinatorError("invalid_context_workspace_handle", 400)
        production_request_id = (
            "managed.production." + sha256(request_id.encode("ascii")).hexdigest()[:48]
        )
        try:
            created = self._production.dispatch(
                {
                    "schema": PRODUCTION_ACTION_SCHEMA,
                    "request_id": production_request_id,
                    "action": "create_workspace_from_context",
                    "payload": {"context_workspace_handle": context_handle},
                }
            )
        except ValueError as exc:
            # CRITICAL: Production validates its private Context-handle grammar first. Translate
            # that boundary failure so the aggregate never leaks an untyped adapter exception.
            raise SequenceCoordinatorError("invalid_context_workspace_handle", 400) from exc
        except ProductionWorkbenchError as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        production = created.projection
        # A child create must return the concrete workspace, never an aggregate read.
        if not isinstance(production, ProductionWorkbenchProjection):
            raise SequenceCoordinatorError("managed_prepare_failed", 500)
        try:
            return self._prepare(
                {
                    "workspace_handle": production.workspace_handle,
                    "expected_workspace_revision": production.workspace_revision,
                    "expected_workspace_fingerprint": production.workspace_fingerprint,
                    "correlation": wire["correlation"],
                    "observation": wire["observation"],
                },
                now,
            )
        except Exception as exc:
            try:
                self._production._rollback_unpublished_creation(
                    request_id=production_request_id,
                    workspace_handle=production.workspace_handle,
                    expected_workspace_revision=production.workspace_revision,
                    expected_workspace_fingerprint=production.workspace_fingerprint,
                )
            except ProductionWorkbenchError as rollback_error:
                # CRITICAL: do not report an ordinary prepare refusal after exact rollback failed;
                # doing so hides retained backend authority and makes a caller retry leak capacity.
                raise SequenceCoordinatorError(
                    "managed_prepare_rollback_failed", 500
                ) from rollback_error
            raise exc

    def _prepare_member(
        self,
        payload: dict[str, object],
        now: float,
    ) -> SequenceCoordinatorResponse:
        """Prepare one admitted member of an accumulating Production project."""

        wire = _closed(
            payload,
            {
                "context_workspace_handle",
                "correlation",
                "observation",
                "production_admission_request_id",
            },
            "managed_member_prepare_payload",
        )
        admission_id = _identifier(
            wire["production_admission_request_id"], "production_admission_request_id"
        )
        try:
            claim = self._production.claim_destination_admission(admission_id)
        except ProductionWorkbenchError as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        try:
            return self._prepare_claimed_member(claim, wire, now)
        except Exception:
            # IMPORTANT: a prepare that fails before its member is published must release the
            # admission; leaving it open keeps the project busy for the whole admission TTL. A
            # published member consumed the admission, which this release leaves untouched.
            self._production.release_destination_claim(claim)
            raise

    def _prepare_claimed_member(
        self,
        claim: ProductionDestinationAdmissionClaim,
        wire: dict[str, object],
        now: float,
    ) -> SequenceCoordinatorResponse:
        context_handle = wire["context_workspace_handle"]
        if type(context_handle) is not str:
            raise SequenceCoordinatorError("invalid_context_workspace_handle", 400)
        correlation_wire = _closed(
            wire["correlation"], {"prompt_id", "execution_node_id"}, "correlation"
        )
        correlation = ExecutionCorrelation(
            _identifier(correlation_wire["prompt_id"], "prompt_id"),
            _identifier(correlation_wire["execution_node_id"], "execution_node_id"),
        )
        observation = PreparedGraphObservation.from_wire(wire["observation"])
        try:
            execution = self._production.build_member_execution(claim, context_handle)
        except ProductionWorkbenchError as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        except ValueError as exc:
            # CRITICAL: Production validates its private Context-handle grammar first. Translate
            # that boundary failure so the aggregate never leaks an untyped adapter exception.
            raise SequenceCoordinatorError("invalid_context_workspace_handle", 400) from exc
        state = self._single_job_state(execution.workspace, observation)
        if len(self._runs) + self._held_managed_run_slots() >= self._max_runs:
            raise SequenceCoordinatorError("coordinator_capacity", 429)
        geometry_receipt: InputGeometryReceipt | None = None
        if observation.source_identity is not None:
            try:
                geometry_receipt = self._geometry_receipt_claimant(
                    observation.source_identity.to_wire()
                )
            except GeometryPreflightError as exc:
                raise SequenceCoordinatorError(exc.code, exc.status) from exc
        run_handle = self._new_handle()
        sequence = build_generation_sequence_projection(state, correlation)
        # Maximal reference members: the project handle and id are not minted yet for a create.
        placeholder_handle = "pw_" + "x" * 96
        try:
            private_projection = self._production.project_managed_child_generation(
                execution.workspace, placeholder_handle, sequence
            )
        except ProductionWorkbenchError as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        # CRITICAL: bound the largest response this run can ever produce before publishing the
        # member. Discovering an oversized reference after the Start has consumed the admission
        # would leave a project member that no response can describe.
        bound = SequenceCoordinatorResponse(
            run_handle,
            "current",
            sequence,
            private_projection,
            None,
            None,
            MANAGED_MEMBER_RESPONSE_SCHEMA,
            production_member_authority=SequenceProductionMemberAuthorityV1(
                placeholder_handle, "x" * 128, execution.segment_id
            ),
        )
        if _managed_child_response_size_bound(bound) > MAX_COORDINATOR_RESPONSE_BYTES:
            raise SequenceCoordinatorError("member_response_capacity", 503)
        try:
            publication = self._production.publish_member_admission(
                execution, sequence, run_handle=run_handle
            )
        except ProductionWorkbenchError as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        created = False
        try:
            production = self._production.project_managed_child_generation(
                execution.workspace, publication.workspace_handle, sequence
            )
            self._managed.create(run_handle)
            created = True
            self._managed.advance(
                run_handle,
                ManagedRunTrigger.STAGE_CONTEXT,
                context_revision=execution.workspace.revision,
            )
            self._managed.advance(
                run_handle,
                ManagedRunTrigger.CREATE_PRODUCTION,
                production_workspace=publication.workspace_handle,
                segment_count=1,
            )
            self._managed.advance(
                run_handle, ManagedRunTrigger.PREPARE_SEQUENCE, prepared_sequence=run_handle
            )
            if geometry_receipt is not None:
                self._managed.attach_geometry(run_handle, geometry_receipt.receipt_handle)
            self._production.confirm_member_run(publication)
        except Exception as exc:
            self._rollback_member_prepare(publication, run_handle, created=created)
            if isinstance(exc, SequenceCoordinatorError):
                raise
            if isinstance(exc, ProductionWorkbenchError):
                raise SequenceCoordinatorError(exc.code, exc.status) from exc
            raise SequenceCoordinatorError("managed_prepare_failed", 500) from exc
        entry = _RunEntry(
            run_handle=run_handle,
            production_handle=publication.workspace_handle,
            workspace=execution.workspace,
            observation=observation,
            correlation=correlation,
            state=state,
            production=production,
            touched_at=now,
            member_workspace_id=publication.workspace_id,
            member_attempt=publication.attempt,
        )
        self._runs[run_handle] = entry
        return self._response(entry, "prepared")

    def _rollback_member_prepare(
        self,
        publication: ProductionMemberPublication,
        run_handle: str,
        *,
        created: bool,
    ) -> None:
        try:
            self._production._rollback_unpublished_member_attempt(
                publication,
                release_live_sequence=(
                    (lambda: self._managed.release(run_handle)) if created else (lambda: None)
                ),
            )
        except Exception as rollback_error:
            # CRITICAL: never report an ordinary prepare refusal after exact rollback failed; the
            # project would retain a member no run owns and a caller retry would leak it.
            raise SequenceCoordinatorError(
                "managed_prepare_rollback_failed", 500
            ) from rollback_error

    def _submission(self, payload: dict[str, object], now: float) -> SequenceCoordinatorResponse:
        wire = _closed(
            payload,
            {
                "run_handle",
                "expected_state_fingerprint",
                "job_id",
                "transaction_id",
                "graph_fingerprint",
                "compiled_prompt_fingerprint",
                "queue_prompt_id",
            },
            "submission_payload",
        )
        entry = self._entry(wire["run_handle"])
        self._expect_state(entry, wire["expected_state_fingerprint"])
        if entry.queue_prompt_id is not None:
            raise SequenceCoordinatorError("duplicate_submission", 409)
        job_id = _identifier(wire["job_id"], "job_id")
        transaction_id = _identifier(wire["transaction_id"], "transaction_id")
        graph_fingerprint = _fingerprint(wire["graph_fingerprint"], "graph_fingerprint")
        compiled_fingerprint = _fingerprint(
            wire["compiled_prompt_fingerprint"], "compiled_prompt_fingerprint"
        )
        queue_prompt_id = _identifier(wire["queue_prompt_id"], "queue_prompt_id")
        previous = entry.state.fingerprint
        try:
            projected = record_generation_projection(
                entry.state,
                job_id,
                transaction_id=transaction_id,
                graph_fingerprint=graph_fingerprint,
                compiled_prompt_fingerprint=compiled_fingerprint,
                fingerprint_domain=entry.observation.fingerprint_domain,
            )
            submitted = record_generation_submission(
                projected,
                job_id,
                queue_prompt_id=queue_prompt_id,
            )
        except GenerationSequenceError as exc:
            raise SequenceCoordinatorError("submission_identity_mismatch", 422) from exc
        self._publish(
            entry,
            previous_state_fingerprint=previous,
            state=submitted,
            artifact_receipt=None,
            now=now,
            queue_prompt_id=queue_prompt_id,
            # CRITICAL: bind carries a visibly provisional, content-free correlation because the
            # host has not assigned a prompt yet. Replace it only in the same publish that records
            # the accepted prompt; exposing the provisional value as host identity would fabricate
            # ownership, while changing it earlier would survive a refused Production CAS.
            correlation=ExecutionCorrelation(
                queue_prompt_id,
                entry.correlation.execution_node_id,
            ),
        )
        entry.queue_prompt_id = queue_prompt_id
        entry.submitted_at = now
        return self._response(entry, "submitted")

    @staticmethod
    def _validate_host_prompt(entry: _RunEntry, payload: dict[str, object]) -> str:
        prompt_id = _identifier(payload.get("queue_prompt_id"), "queue_prompt_id")
        if entry.queue_prompt_id != prompt_id:
            raise SequenceCoordinatorError("foreign_host_event", 409)
        return prompt_id

    def _running(self, payload: dict[str, object], now: float) -> SequenceCoordinatorResponse:
        wire = _closed(
            payload,
            {
                "run_handle",
                "expected_state_fingerprint",
                "queue_prompt_id",
                "host_owner_id",
            },
            "running_payload",
        )
        entry = self._entry(wire["run_handle"])
        self._expect_state(entry, wire["expected_state_fingerprint"])
        self._validate_host_prompt(entry, wire)
        host_owner_id = _identifier(wire["host_owner_id"], "host_owner_id")
        runtime = _first_runtime(entry.state)
        if runtime.state is GenerationJobState.RUNNING:
            return self._response(entry, "running")
        if runtime.state is not GenerationJobState.SUBMITTED:
            raise SequenceCoordinatorError("invalid_generation_transition", 409)
        previous = entry.state.fingerprint
        try:
            running = record_generation_running(
                entry.state,
                runtime.job_id,
                host_owner_id=host_owner_id,
            )
        except GenerationSequenceError as exc:
            raise SequenceCoordinatorError("invalid_generation_transition", 409) from exc
        self._publish(
            entry,
            previous_state_fingerprint=previous,
            state=running,
            artifact_receipt=entry.artifact_receipt,
            now=now,
        )
        return self._response(entry, "running")

    def _resolved_artifact_payload(
        self,
        locator: SavedArtifactLocator,
    ) -> bytes:
        if (
            locator.filename in {".", ".."}
            or any(ord(character) < 32 for character in locator.filename)
            or any(character in locator.filename for character in "/\\:")
            or locator.subfolder.startswith(("/", "\\"))
            or "\\" in locator.subfolder
            or ":" in locator.subfolder
            or any(ord(character) < 32 for character in locator.subfolder)
        ):
            raise SequenceCoordinatorError("artifact_locator_unsafe", 422)
        parts = () if locator.subfolder == "" else tuple(locator.subfolder.split("/"))
        if any(not part or part in {".", ".."} or len(part) > 128 for part in parts):
            raise SequenceCoordinatorError("artifact_locator_unsafe", 422)
        try:
            root_value = self._output_root_factory()
            if not isinstance(root_value, Path):
                raise UnsafePathError("output root type")
            root = validate_directory(root_value)
            candidate = root.joinpath(*parts, locator.filename)
            # CRITICAL: lexical component admission plus the pinned descriptor read must stay
            # together; resolving or opening the leaf alone reintroduces traversal/reparse races.
            payload = _read_regular_bytes(candidate, maximum_bytes=MAX_MANAGED_ARTIFACT_BYTES)
        except (UnsafePathError, ArtifactStoreError, OSError) as exc:
            raise SequenceCoordinatorError("artifact_locator_unsafe", 422) from exc
        if not payload:
            raise SequenceCoordinatorError("artifact_unavailable", 422)
        return payload

    def _store(self) -> PrivateSegmentArtifactStore:
        if self._artifact_store is None:
            with self._artifact_store_init_lock:
                if self._artifact_store is None:
                    root = self._private_root_factory()
                    if not isinstance(root, Path):
                        raise SequenceCoordinatorError("artifact_store_unavailable", 503)
                    policy = ArtifactStorePolicy(
                        max_artifact_bytes=MAX_MANAGED_ARTIFACT_BYTES,
                        max_total_bytes=8 * MAX_MANAGED_ARTIFACT_BYTES,
                        max_entries=64,
                        ttl_seconds=7 * 24 * 60 * 60,
                        max_concurrent_writes=2,
                        max_recovery_entries=128,
                    )
                    try:
                        self._artifact_store = PrivateSegmentArtifactStore(
                            root,
                            policy=policy,
                            clock_ms=self._clock_ms,
                        )
                    except ArtifactStoreError as exc:
                        raise SequenceCoordinatorError("artifact_store_unavailable", 503) from exc
        return self._artifact_store

    @staticmethod
    def _artifact_retry_disposition(
        error: SequenceCoordinatorError,
        candidate: _ArtifactVerificationCandidate | None,
        *,
        is_retry: bool,
    ) -> str:
        if (
            not is_retry
            and candidate is not None
            and candidate.verification_attempts < MAX_ARTIFACT_VERIFICATION_ATTEMPTS
            and error.code in _TRANSIENT_ARTIFACT_CODES
        ):
            return "retry_output_verification"
        if error.category in {"artifact_content_invalid", "artifact_locator_rejected"}:
            return "inspect_native"
        return "use_native"

    def _record_artifact_failure(
        self,
        entry: _RunEntry,
        *,
        working: GenerationSequenceState,
        previous_state_fingerprint: str,
        error: SequenceCoordinatorError,
        candidate: _ArtifactVerificationCandidate | None,
        is_retry: bool,
        now: float,
    ) -> SequenceCoordinatorError:
        retry_disposition = self._artifact_retry_disposition(
            error,
            candidate,
            is_retry=is_retry,
        )
        if not is_retry:
            try:
                failed = record_output_verification_failure(
                    working,
                    _first_runtime(working).job_id,
                    failure_code=error.category,
                )
            except GenerationSequenceError as exc:
                raise SequenceCoordinatorError("invalid_generation_transition", 409) from exc
            self._publish(
                entry,
                previous_state_fingerprint=previous_state_fingerprint,
                state=failed,
                artifact_receipt=None,
                now=now,
            )
        entry.artifact_candidate = (
            candidate if retry_disposition == "retry_output_verification" else None
        )
        return error.with_artifact_context(retry_disposition=retry_disposition)

    def _artifact(
        self, payload: dict[str, object], now: float, ticket: _ArtifactVerificationTicket
    ) -> SequenceCoordinatorResponse:
        with self._lock:
            self._assert_artifact_ticket(ticket)
            wire = _closed(
                payload,
                {
                    "run_handle",
                    "expected_state_fingerprint",
                    "queue_prompt_id",
                    "output_node_id",
                    "locator",
                },
                "artifact_payload",
            )
            entry = ticket.snapshot
            self._expect_state(entry, wire["expected_state_fingerprint"])
            self._validate_host_prompt(entry, wire)
            managed_resource = self._managed_resource_for_child(entry.run_handle)
            artifact_capacity = None
            if managed_resource is not None:
                artifact_capacity = managed_resource.artifact_capacity
                if artifact_capacity is None or managed_resource.artifact_capacity_released:
                    raise SequenceCoordinatorError("artifact_store_failed", 503)
            output_node_id = _identifier(wire["output_node_id"], "output_node_id")
            locator = SavedArtifactLocator.from_wire(wire["locator"])
            if entry.artifact_receipt is not None:
                # The first verified compatible video owns the managed run. ComfyUI may emit later
                # previews or duplicate save events from unrelated nodes; none may replace it.
                return self._response(
                    entry,
                    "succeeded"
                    if _first_runtime(entry.state).state is GenerationJobState.SUCCEEDED
                    else "artifact_verified",
                )
            runtime = _first_runtime(entry.state)
            working = entry.state
            is_retry = runtime.state is GenerationJobState.OUTPUT_VERIFICATION_FAILED
            candidate = (
                None if entry.artifact_candidate is None else replace(entry.artifact_candidate)
            )
            is_publication_retry = candidate is not None and candidate.completed_receipt is not None
            if (
                is_publication_retry
                and candidate is not None
                and candidate.production_publish_attempts
                >= MAX_ARTIFACT_PRODUCTION_PUBLISH_ATTEMPTS
            ):
                raise SequenceCoordinatorError(
                    "artifact_publication_unavailable",
                    503,
                    retry_disposition="use_native",
                    same_run_authority=False,
                )
            if runtime.state is GenerationJobState.SUBMITTED:
                try:
                    working = record_generation_running(
                        entry.state,
                        runtime.job_id,
                        host_owner_id=entry.queue_prompt_id or "host.unknown",
                    )
                except GenerationSequenceError as exc:
                    raise SequenceCoordinatorError("invalid_generation_transition", 409) from exc
                runtime = _first_runtime(working)
            if (
                runtime.state
                not in {
                    GenerationJobState.RUNNING,
                    GenerationJobState.OUTPUT_VERIFICATION_FAILED,
                }
                or runtime.transaction is None
                or (is_retry and entry.artifact_candidate is None)
            ):
                raise SequenceCoordinatorError("invalid_generation_transition", 409)
            previous = entry.state.fingerprint
        publication_attempted = False
        try:
            immutable_payload = self._resolved_artifact_payload(locator)
            self._check_artifact_ticket(ticket)
            output_fingerprint = "sha256:" + sha256(immutable_payload).hexdigest()
            job = working.plan.jobs[0]
            if is_publication_retry:
                if (
                    candidate is None
                    or candidate.locator != locator
                    or candidate.output_fingerprint != output_fingerprint
                    or candidate.byte_length != len(immutable_payload)
                ):
                    raise SequenceCoordinatorError("artifact_output_changed", 409)
            elif is_retry:
                if (
                    candidate is None
                    or candidate.locator != locator
                    or candidate.output_fingerprint != output_fingerprint
                    or candidate.byte_length != len(immutable_payload)
                    or candidate.verification_attempts >= MAX_ARTIFACT_VERIFICATION_ATTEMPTS
                ):
                    candidate = None
                    raise SequenceCoordinatorError("artifact_output_changed", 409)
                candidate.verification_attempts += 1
            else:
                candidate = _ArtifactVerificationCandidate(
                    locator=locator,
                    output_fingerprint=output_fingerprint,
                    byte_length=len(immutable_payload),
                )
            if not is_publication_retry:
                deadline = time.monotonic() + ARTIFACT_INSPECTION_SECONDS
                observed = self._artifact_inspector(
                    immutable_payload,
                    expected_frames=entry.observation.expected_frames,
                    deadline=deadline,
                    should_cancel=lambda: self._artifact_ticket_cancelled(ticket),
                )
                if type(observed) is not ObservedVideoArtifact:
                    raise SequenceCoordinatorError("artifact_media_invalid", 422)
                if candidate.partial_receipt is None:
                    # CRITICAL: managed children share the complete original workspace; selecting
                    # the first manifest would silently mint every later output for segment one.
                    manifest = next(
                        item
                        for item in derive_segment_manifests(entry.workspace)
                        if item.segment_id == job.segment_id
                    )
                    created_at_ms = self._clock_ms()
                    execution_fingerprint = canonical_fingerprint(
                        {
                            "domain": "h3.context.managed_execution.v1",
                            "queue_prompt_id": entry.queue_prompt_id,
                            "output_node_id": output_node_id,
                        }
                    )
                    artifact_id = (
                        "artifact."
                        + canonical_fingerprint(
                            {
                                "domain": "h3.context.managed_artifact.v1",
                                "sequence": working.plan.fingerprint,
                                "job": job.job_id,
                                "attempt": runtime.attempt,
                            }
                        ).removeprefix("sha256:")[:40]
                    )
                    candidate.partial_receipt = begin_segment_artifact_receipt(
                        manifest=manifest,
                        transaction=runtime.transaction,
                        artifact_id=artifact_id,
                        model_fingerprint=job.model_fingerprint,
                        runtime_fingerprint=job.runtime_fingerprint,
                        execution_fingerprint=execution_fingerprint,
                        predecessor_artifact_fingerprint=job.predecessor_artifact_fingerprint,
                        format_label=observed.format_label,
                        shape=observed.shape,
                        created_at_ms=created_at_ms,
                        expires_at_ms=created_at_ms + 7 * 24 * 60 * 60 * 1_000,
                    )
                elif (
                    candidate.partial_receipt.format_label != observed.format_label
                    or candidate.partial_receipt.shape != observed.shape
                ):
                    raise SequenceCoordinatorError("artifact_output_changed", 409)
                self._check_artifact_ticket(ticket)
                partial = candidate.partial_receipt
                store = self._store()
                if not candidate.store_started:
                    store.begin(partial, capacity_reservation=artifact_capacity)
                    candidate.store_started = True
                if candidate.completed_receipt is None:
                    candidate.completed_receipt = store.commit(
                        partial,
                        immutable_payload,
                        capacity_reservation=artifact_capacity,
                    )
            complete = candidate.completed_receipt
            if complete is None:  # pragma: no cover - guarded by the assignment above
                raise RuntimeError("artifact completion invariant")
            self._check_artifact_ticket(ticket)
            preview_state = record_generation_success(
                working,
                job.job_id,
                result_fingerprint=canonical_fingerprint(
                    {"domain": "h3.context.managed_preview.v1", "receipt": complete.fingerprint}
                ),
                receipt=complete,
            )
            preview_sources = self._production.prepare_generated_preview_sources(
                build_generation_sequence_projection(preview_state, entry.correlation),
                (complete,),
                self._store(),
            )
            with self._lock:
                self._assert_artifact_ticket(ticket)
                entry = ticket.entry
                entry.artifact_preview_source = next(iter(preview_sources.values()), None)
                now = self._clock()
                final_state = working
                disposition = "artifact_verified"
                if entry.terminal_kind == "success":
                    try:
                        final_state = record_generation_success(
                            working,
                            job.job_id,
                            result_fingerprint=canonical_fingerprint(
                                {
                                    "domain": "h3.context.managed_result.v1",
                                    "execution": complete.execution_fingerprint,
                                    "receipt": complete.fingerprint,
                                }
                            ),
                            receipt=complete,
                        )
                    except GenerationSequenceError as exc:
                        raise SequenceCoordinatorError("artifact_receipt_mismatch", 422) from exc
                    disposition = "succeeded"
                # CRITICAL: retain the committed candidate before Production CAS; transient
                # authority failure must remain retryable without inspecting or storing again.
                entry.artifact_candidate = candidate
                candidate.production_publish_attempts += 1
                publication_attempted = True
                self._publish(
                    entry,
                    previous_state_fingerprint=previous,
                    state=final_state,
                    artifact_receipt=complete,
                    now=now,
                )
                entry.artifact_locator = locator
                entry.artifact_candidate = None
                return self._response(entry, disposition)
        except SequenceCoordinatorError as exc:
            with self._lock:
                self._assert_artifact_ticket(ticket)
                entry = ticket.entry
                now = self._clock()
                if publication_attempted and candidate is not None and candidate.completed_receipt:
                    entry.artifact_candidate = candidate
                    retryable_publish = (
                        exc.code in _TRANSIENT_PRODUCTION_PUBLISH_CODES
                        and candidate.production_publish_attempts
                        < MAX_ARTIFACT_PRODUCTION_PUBLISH_ATTEMPTS
                    )
                    if not retryable_publish:
                        # CRITICAL: retain bounded reconciliation evidence, but never cross
                        # Production CAS again after stale or closed authority.
                        candidate.production_publish_attempts = (
                            MAX_ARTIFACT_PRODUCTION_PUBLISH_ATTEMPTS
                        )
                    raise exc.with_artifact_context(
                        retry_disposition=(
                            "retry_output_verification" if retryable_publish else "use_native"
                        ),
                        same_run_authority=retryable_publish,
                    ) from exc
                if is_publication_retry:
                    entry.artifact_candidate = candidate
                    raise exc.with_artifact_context(
                        retry_disposition=(
                            "inspect_native"
                            if exc.category == "artifact_locator_rejected"
                            else "use_native"
                        )
                    ) from exc
                raise self._record_artifact_failure(
                    entry,
                    working=working,
                    previous_state_fingerprint=previous,
                    error=exc,
                    candidate=candidate,
                    is_retry=is_retry,
                    now=now,
                ) from exc
        except SegmentArtifactError as exc:
            with self._lock:
                self._assert_artifact_ticket(ticket)
                entry = ticket.entry
                now = self._clock()
                # CRITICAL: receipt identity failures are authority errors, not store availability.
                receipt_code = (
                    "transaction_manifest_authority_mismatch"
                    if str(exc) == "transaction_manifest_authority_mismatch"
                    else "artifact_receipt_mismatch"
                )
                error = SequenceCoordinatorError(receipt_code, 422)
                raise self._record_artifact_failure(
                    entry,
                    working=working,
                    previous_state_fingerprint=previous,
                    error=error,
                    candidate=candidate,
                    is_retry=is_retry,
                    now=now,
                ) from exc
        except ArtifactStoreError as exc:
            with self._lock:
                self._assert_artifact_ticket(ticket)
                entry = ticket.entry
                now = self._clock()
                error = SequenceCoordinatorError("artifact_store_failed", 503)
                raise self._record_artifact_failure(
                    entry,
                    working=working,
                    previous_state_fingerprint=previous,
                    error=error,
                    candidate=candidate,
                    is_retry=is_retry,
                    now=now,
                ) from exc
        except Exception as exc:
            with self._lock:
                self._assert_artifact_ticket(ticket)
                entry = ticket.entry
                now = self._clock()
                error = SequenceCoordinatorError("internal_failure", 500)
                raise self._record_artifact_failure(
                    entry,
                    working=working,
                    previous_state_fingerprint=previous,
                    error=error,
                    candidate=candidate,
                    is_retry=is_retry,
                    now=now,
                ) from exc

    def _terminal(self, payload: dict[str, object], now: float) -> SequenceCoordinatorResponse:
        wire = _closed(
            payload,
            {
                "run_handle",
                "expected_state_fingerprint",
                "queue_prompt_id",
                "kind",
            },
            "terminal_payload",
        )
        entry = self._entry(wire["run_handle"])
        self._expect_state(entry, wire["expected_state_fingerprint"])
        self._validate_host_prompt(entry, wire)
        kind = wire["kind"]
        if type(kind) is not str or kind not in _TERMINALS:
            raise SequenceCoordinatorError("invalid_terminal_kind", 400)
        if entry.terminal_kind is not None:
            if entry.terminal_kind != kind:
                raise SequenceCoordinatorError("conflicting_terminal", 409)
            return self._response(
                entry,
                "succeeded"
                if _first_runtime(entry.state).state is GenerationJobState.SUCCEEDED
                else "verification_pending"
                if kind == "success"
                else "failed",
            )
        previous = entry.state.fingerprint
        runtime = _first_runtime(entry.state)
        working = entry.state
        if runtime.state is GenerationJobState.SUBMITTED:
            try:
                working = record_generation_running(
                    entry.state,
                    runtime.job_id,
                    host_owner_id=entry.queue_prompt_id or "host.unknown",
                )
            except GenerationSequenceError as exc:
                raise SequenceCoordinatorError("invalid_generation_transition", 409) from exc
            runtime = _first_runtime(working)
        if runtime.state is GenerationJobState.OUTPUT_VERIFICATION_FAILED and kind == "success":
            entry.terminal_kind = kind
            return self._response(entry, "output_verification_failed")
        if runtime.state not in {
            GenerationJobState.RUNNING,
            GenerationJobState.OUTPUT_VERIFICATION_FAILED,
        }:
            raise SequenceCoordinatorError("invalid_generation_transition", 409)
        if kind == "success":
            final_state = working
            disposition = "verification_pending"
            receipt = entry.artifact_receipt
            if receipt is not None:
                try:
                    final_state = record_generation_success(
                        working,
                        runtime.job_id,
                        result_fingerprint=canonical_fingerprint(
                            {
                                "domain": "h3.context.managed_result.v1",
                                "queue_prompt_id": entry.queue_prompt_id,
                                "receipt": receipt.fingerprint,
                            }
                        ),
                        receipt=receipt,
                    )
                except GenerationSequenceError as exc:
                    raise SequenceCoordinatorError("artifact_receipt_mismatch", 422) from exc
                disposition = "succeeded"
        else:
            try:
                final_state = record_generation_failure(
                    working,
                    runtime.job_id,
                    result_fingerprint=canonical_fingerprint(
                        {
                            "domain": "h3.context.managed_terminal.v1",
                            "queue_prompt_id": entry.queue_prompt_id,
                            "kind": kind,
                        }
                    ),
                    failure_code=(
                        "host_interrupted" if kind == "interrupted" else "host_execution_failed"
                    ),
                )
            except GenerationSequenceError as exc:
                raise SequenceCoordinatorError("invalid_generation_transition", 409) from exc
            entry.artifact_candidate = None
            disposition = "interrupted" if kind == "interrupted" else "failed"
        self._publish(
            entry,
            previous_state_fingerprint=previous,
            state=final_state,
            artifact_receipt=entry.artifact_receipt,
            now=now,
        )
        entry.terminal_kind = kind
        return self._response(entry, disposition)

    def _close_managed(
        self,
        payload: dict[str, object],
        now: float,
        ticket: _ArtifactVerificationTicket | None = None,
    ) -> SequenceCoordinatorResponse:
        wire = _closed(
            payload,
            {
                "run_handle",
                "expected_state_fingerprint",
                "queue_prompt_id",
                "kind",
                "artifact",
            },
            "managed_close_payload",
        )
        kind = wire["kind"]
        artifact = wire["artifact"]
        if type(kind) is not str or kind not in _TERMINALS:
            raise SequenceCoordinatorError("invalid_terminal_kind", 400)
        terminal_payload = {
            "run_handle": wire["run_handle"],
            "expected_state_fingerprint": wire["expected_state_fingerprint"],
            "queue_prompt_id": wire["queue_prompt_id"],
            "kind": kind,
        }
        if kind != "success":
            if artifact is not None:
                raise SequenceCoordinatorError("invalid_managed_artifact", 400)
            with self._lock:
                return self._terminal(terminal_payload, now)
        # CRITICAL: host completion can precede SaveVideo's artifact event. Record the exact
        # terminal now; only the existing verified-artifact join may mark the run succeeded.
        if artifact is None:
            with self._lock:
                return self._terminal(terminal_payload, now)
        artifact_wire = _closed(
            artifact,
            {"output_node_id", "locator"},
            "managed_artifact",
        )
        if ticket is None:
            raise RuntimeError("artifact verification ticket required")
        verified = self._artifact(
            {
                "run_handle": wire["run_handle"],
                "expected_state_fingerprint": wire["expected_state_fingerprint"],
                "queue_prompt_id": wire["queue_prompt_id"],
                "output_node_id": artifact_wire["output_node_id"],
                "locator": artifact_wire["locator"],
            },
            now,
            ticket,
        )
        # IMPORTANT: artifact verification advances the CAS fingerprint. Terminalize against the
        # returned authority, never the stale fingerprint supplied for the combined close.
        terminal_payload["expected_state_fingerprint"] = verified.sequence.state.fingerprint
        with self._lock:
            self._assert_artifact_ticket(ticket, after_publication=True)
            return self._terminal(terminal_payload, self._clock())

    def _cancel(self, payload: dict[str, object], now: float) -> SequenceCoordinatorResponse:
        wire = _closed(
            payload,
            {"run_handle", "expected_state_fingerprint"},
            "cancel_payload",
        )
        entry = self._entry(wire["run_handle"])
        self._expect_state(entry, wire["expected_state_fingerprint"])
        previous = entry.state.fingerprint
        runtime = _first_runtime(entry.state)
        # CRITICAL: one predicate, evaluated once, over one authority. This asks a single question
        # -- may the host still be executing this prompt? -- and both the recorded transition and
        # the disposition returned to the client are derived from the answer. They used to be
        # decided separately: the transition tested the runtime's job state while the disposition
        # tested `entry.queue_prompt_id`, so a run holding a prompt id whose runtime was still
        # `projected` was recorded `cancelled` and reported `unknown_ownership`, and a `submitted`
        # run with no recorded prompt id was recorded `unknown_ownership` and reported `cancelled`.
        # The response could contradict the state it accompanied. Never restate the set inline
        # here or in a test; `HOST_MAY_HOLD_JOB_STATES` is the one definition, and its comment
        # explains why it is a different object from the aggregate's constant rather than a copy.
        host_may_hold_the_prompt = runtime.state in HOST_MAY_HOLD_JOB_STATES
        try:
            state = (
                mark_generation_unknown_ownership(entry.state, runtime.job_id)
                if host_may_hold_the_prompt
                else cancel_generation_sequence(entry.state)
            )
        except GenerationSequenceError as exc:
            raise SequenceCoordinatorError("invalid_generation_transition", 409) from exc
        self._publish(
            entry,
            previous_state_fingerprint=previous,
            state=state,
            artifact_receipt=entry.artifact_receipt,
            now=now,
        )
        return self._response(
            entry, "unknown_ownership" if host_may_hold_the_prompt else "cancelled"
        )

    def _release_request(self, payload: dict[str, object]) -> tuple[_RunEntry, ReleaseRequest]:
        """Decode exactly one of the two accepted release payloads, or refuse before any read."""

        if set(payload) == {"run_handle"}:
            # Legacy V1. It carries no intent, so it keeps the only intent it ever had: undo a
            # preparation the host never accepted. CRITICAL: it is no longer unconditional. The
            # state table now refuses it for any run the host may own, and that refusal is the
            # defect fix -- a caller that treats a 409 here as a reason to retry with V2's
            # `detach_client` is correct, and one that treats it as a reason to force a removal is
            # reintroducing the data loss.
            entry = self._entry(payload["run_handle"])
            return entry, ReleaseRequest(entry.run_handle, ReleaseIntent.CLEANUP_PRE_SUBMIT)
        if payload.get("schema") != RELEASE_REQUEST_V2_SCHEMA:
            raise SequenceCoordinatorError("unsupported_release_schema", 400)
        try:
            intent = ReleaseIntent(payload.get("intent"))
        except ValueError as exc:
            raise SequenceCoordinatorError("invalid_release_intent", 400) from exc
        wire = _closed(payload, _RELEASE_V2_MEMBERS[intent], "release_payload")
        entry = self._entry(wire["run_handle"])
        self._expect_state(entry, wire["expected_state_fingerprint"])
        proof: str | None = None
        if intent is ReleaseIntent.CLEANUP_TERMINAL:
            proof = _fingerprint(
                wire["observed_terminal_fingerprint"], "observed_terminal_fingerprint"
            )
        return entry, ReleaseRequest(entry.run_handle, intent, proof)

    def _release_sequence(
        self,
        payload: dict[str, object],
        now: float,
        *,
        parent_absolute_deadline: float | None = None,
    ) -> SequenceCoordinatorResponse:
        entry, request = self._release_request(payload)
        managed_resource = self._managed_resource_for_child(entry.run_handle)
        removal_transaction = None
        if managed_resource is not None and entry.original_automatic_authority is not None:
            retained_child: _CompletedManagedChild | None = None
            if _first_runtime(entry.state).state is GenerationJobState.SUCCEEDED:
                receipt = entry.artifact_receipt
                proof = terminal_fingerprint(self._managed.read(entry.run_handle))
                if receipt is None or proof is None:
                    raise SequenceCoordinatorError("managed_child_release_failed", 503)
                retained_child = _CompletedManagedChild(
                    entry.state, receipt, entry.correlation, entry.run_handle, proof
                )
                segment_id = receipt.segment_id
                existing = managed_resource.completed_children.get(segment_id)
                if existing is not None and existing != retained_child:
                    raise SequenceCoordinatorError("managed_child_release_failed", 409)
                size = (
                    sum(
                        child.retained_bytes()
                        for key, child in managed_resource.completed_children.items()
                        if key != segment_id
                    )
                    + retained_child.retained_bytes()
                )
                if (
                    len(managed_resource.completed_children) + int(existing is None)
                    > managed_resource.segment_count
                    or size > managed_resource.reserved_bytes
                    or self._managed_ledger_occupancy()[1]
                    + (0 if existing is not None else retained_child.retained_bytes())
                    + sum(
                        len(canonical_bytes(item.response.to_wire()))
                        for item in self._ledger.values()
                    )
                    > MAX_COORDINATOR_RETAINED_BYTES
                ):
                    raise SequenceCoordinatorError("managed_child_release_failed", 503)

            def removal_transaction(release_live_sequence: Callable[[], None]) -> None:
                # CRITICAL: retain the actual immutable completion before dropping the child.
                # Cleanup must work after parent CAS revocation and must never delete its workspace.
                if retained_child is not None:
                    managed_resource.completed_children[retained_child.receipt.segment_id] = (
                        retained_child
                    )
                release_live_sequence()

        elif managed_resource is not None:
            creation_request_id = entry.managed_creation_request_id
            if creation_request_id is None:
                raise SequenceCoordinatorError("managed_child_release_failed", 500)

            def removal_transaction(release_live_sequence: Callable[[], None]) -> None:
                self._production._release_managed_child_workspace(
                    creation_request_id=creation_request_id,
                    workspace_handle=entry.production.workspace_handle,
                    expected_workspace_revision=entry.production.workspace_revision,
                    expected_workspace_fingerprint=entry.production.workspace_fingerprint,
                    expected_sequence_state_fingerprint=entry.state.fingerprint,
                    release_live_sequence=release_live_sequence,
                )

        elif entry.member_workspace_id is not None and entry.member_attempt is not None:
            member_attempt = entry.member_attempt
            settled_sequence: GenerationSequenceProjection | None = None
            if _first_runtime(entry.state).state not in _TERMINAL_JOB_STATES:
                try:
                    settled_sequence = build_generation_sequence_projection(
                        cancel_generation_sequence(entry.state), entry.correlation
                    )
                except GenerationSequenceError as exc:
                    raise SequenceCoordinatorError("invalid_generation_transition", 409) from exc

            def removal_transaction(release_live_sequence: Callable[[], None]) -> None:
                # IMPORTANT: only a removal reaches here. The project keeps its member facts; the
                # run and its execution workspace go, so the project never holds a run per clip.
                self._production.release_member_run(
                    entry.production_handle,
                    segment_id=member_attempt.segment_id,
                    previous_attempt=member_attempt,
                    settled_sequence=settled_sequence,
                    release_live_sequence=release_live_sequence,
                )

        try:
            outcome = self._release.apply(
                request,
                submitted_at=entry.submitted_at,
                timeout_ms=entry.observation.timeout_ms,
                parent_absolute_deadline=parent_absolute_deadline,
                removal_transaction=removal_transaction,
            )
        except ManagedRunReleaseRefused as exc:
            raise SequenceCoordinatorError(exc.code, exc.status) from exc
        except ProductionWorkbenchError as exc:
            raise SequenceCoordinatorError("managed_child_release_failed", 503) from exc
        response = self._response(
            entry,
            outcome.disposition,
            terminal_authority=outcome.terminal_fingerprint,
        )
        if outcome.removes_authority:
            # CRITICAL: the service has already dropped the ManagedRun, and this drops only the
            # coordinator's own row for the same run. Never call `self._managed.release` here as
            # well: the service is the single owner of that mutation, and a second caller is how
            # the two records of one run drift apart. Equally, never pop the row on a detach --
            # `removes_authority` is the whole condition, and a detached run must stay addressable
            # so a late terminal from the host still lands on it.
            self._runs.pop(entry.run_handle, None)
            if managed_resource is not None:
                if managed_resource.bound_child_run_handle != entry.run_handle:
                    raise RuntimeError("managed child resource ownership drift")
                managed_resource.bound_child_run_handle = None
                if request.intent is ReleaseIntent.CLEANUP_PRE_SUBMIT:
                    managed_resource.pre_submit_cleaned_child_run_handle = entry.run_handle
            self._tombstones[entry.run_handle] = now
        return response

    def transition_managed_sequence_lease(
        self,
        before: ManagedSequenceV1,
        candidate: ManagedSequenceV1,
        observed: float,
    ) -> None:
        """Apply the child half of one parent-owned expiry transaction."""

        if type(before) is not ManagedSequenceV1 or type(candidate) is not ManagedSequenceV1:
            raise ManagedSequenceServiceError("lease_parent_contract", 500)
        parent_id = before.authorization.sequence_id
        if (
            candidate.authorization.fingerprint != before.authorization.fingerprint
            or candidate.revision not in {before.revision, before.revision + 1}
        ):
            raise ManagedSequenceServiceError("lease_parent_drift", 409)
        with self._lock:
            resource = self._managed_start_resources.get(parent_id)
            if (
                resource is None
                or resource.parent_authorization_fingerprint != before.authorization.fingerprint
            ):
                raise ManagedSequenceServiceError("lease_resource_unavailable", 409)
            active_id = before.active_segment_id
            if active_id is not None:
                before_slot = next(
                    (row for row in before.slots if row.segment_id == active_id),
                    None,
                )
                candidate_slot = next(
                    (row for row in candidate.slots if row.segment_id == active_id),
                    None,
                )
                if before_slot is None or candidate_slot is None:
                    raise ManagedSequenceServiceError("lease_child_drift", 409)
                run_handle = before_slot.child_run_handle
                if run_handle is None:
                    if before_slot.state is not SequenceSlotStateV1.PREPARED:
                        raise ManagedSequenceServiceError("lease_child_unavailable", 409)
                else:
                    entry = self._runs.get(run_handle)
                    # CRITICAL: removal can finish before the parent publishes its transition.
                    # Replay only this owner's exact pre-submit success, never a merely missing
                    # run or an older child's cleanup; otherwise expiry can erase live ownership.
                    if (
                        entry is None
                        and resource.bound_child_run_handle is None
                        and resource.pre_submit_cleaned_child_run_handle == run_handle
                        and before_slot.state is SequenceSlotStateV1.BOUND
                        and candidate.active_segment_id is None
                        and candidate_slot.state is SequenceSlotStateV1.ELIGIBLE
                        and candidate_slot.child_run_handle is None
                    ):
                        resource.expiry_pending = False
                        resource.expiry_transition_complete = True
                        return
                    if (
                        entry is None
                        or resource.bound_child_run_handle != run_handle
                        or entry.run_handle != run_handle
                    ):
                        raise ManagedSequenceServiceError("lease_child_unavailable", 409)
                    if candidate.active_segment_id is None:
                        if (
                            before_slot.state is not SequenceSlotStateV1.BOUND
                            or candidate_slot.state is not SequenceSlotStateV1.ELIGIBLE
                        ):
                            raise ManagedSequenceServiceError("lease_child_transition", 409)
                        self._release_sequence(
                            {
                                "schema": RELEASE_REQUEST_V2_SCHEMA,
                                "run_handle": run_handle,
                                "intent": ReleaseIntent.CLEANUP_PRE_SUBMIT.value,
                                "expected_state_fingerprint": entry.state.fingerprint,
                            },
                            observed,
                        )
                    elif candidate.state is ManagedSequenceStateV1.PAUSED_UNKNOWN_OWNERSHIP:
                        if candidate_slot.state is not SequenceSlotStateV1.UNKNOWN_OWNERSHIP:
                            raise ManagedSequenceServiceError("lease_child_transition", 409)
                        self._release_sequence(
                            {
                                "schema": RELEASE_REQUEST_V2_SCHEMA,
                                "run_handle": run_handle,
                                "intent": ReleaseIntent.DETACH_CLIENT.value,
                                "expected_state_fingerprint": entry.state.fingerprint,
                            },
                            observed,
                            parent_absolute_deadline=before.lease.absolute_deadline
                            if before.lease is not None
                            else None,
                        )
                    else:
                        raise ManagedSequenceServiceError("lease_child_transition", 409)
            resource.expiry_pending = False
            resource.expiry_transition_complete = True

    def dispatch(self, action: object) -> SequenceCoordinatorDispatchResult:
        wire = _closed(action, {"schema", "request_id", "action", "payload"}, "action")
        if wire["schema"] != COORDINATOR_ACTION_SCHEMA:
            raise SequenceCoordinatorError("unsupported_action_schema", 400)
        request_id = wire["request_id"]
        if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
            raise SequenceCoordinatorError("invalid_request_id", 400)
        action_name = wire["action"]
        if type(action_name) is not str:
            raise SequenceCoordinatorError("invalid_action", 400)
        payload = wire["payload"]
        if type(payload) is not dict:
            raise SequenceCoordinatorError("invalid_payload", 400)
        digest = canonical_fingerprint(wire)
        now = self._clock()
        with self._lock:
            self._prune(now)
            pending_expiries = self._pending_managed_expiries()
            lease_transition = self._managed_lease_transition
        if pending_expiries:
            if lease_transition is None:
                raise SequenceCoordinatorError("managed_lease_transition_unavailable", 503)
            # CRITICAL: never call the parent while holding the coordinator lock. The lease owner
            # acquires parent first and comes back through coordinator -> Production -> ManagedRun.
            for parent_sequence_id in pending_expiries:
                try:
                    lease_transition(parent_sequence_id, now)
                except ManagedSequenceServiceError as exc:
                    raise SequenceCoordinatorError(exc.code, exc.status) from exc
                except Exception as exc:
                    raise SequenceCoordinatorError("managed_lease_transition_failed", 503) from exc
        with self._lock:
            self._prune(now)
            if self._pending_managed_expiries():
                raise SequenceCoordinatorError("managed_lease_transition_pending", 503)
            replay = self._ledger.get(request_id)
            if replay is not None:
                if replay.request_digest != digest:
                    raise SequenceCoordinatorError("request_id_conflict", 409)
                return SequenceCoordinatorDispatchResult(200, replay.response)
            in_flight = self._artifact_requests.get(request_id)
            if in_flight is not None:
                if in_flight.request_digest != digest:
                    raise SequenceCoordinatorError("request_id_conflict", 409)
                return SequenceCoordinatorDispatchResult(
                    200, self._response(in_flight.entry, "verification_pending")
                )
            # CRITICAL: reject bounded-ledger exhaustion before an action can mutate either
            # coordinator state or its attached Production authority.
            managed_rows, managed_bytes = self._managed_ledger_occupancy()
            if (
                len(self._ledger) + len(self._artifact_requests) + managed_rows
                >= MAX_COORDINATOR_LEDGER
            ):
                raise SequenceCoordinatorError("coordinator_ledger_capacity", 429)
            retained_bytes = sum(
                len(canonical_bytes(item.response.to_wire())) for item in self._ledger.values()
            )
            if (
                retained_bytes
                + managed_bytes
                + (len(self._artifact_requests) + 1) * MAX_COORDINATOR_RESPONSE_BYTES
                > MAX_COORDINATOR_RETAINED_BYTES
            ):
                raise SequenceCoordinatorError("coordinator_ledger_capacity", 429)
            artifact_action = action_name == "record_artifact" or (
                action_name == "close_managed_run"
                and payload.get("artifact") is not None
                and payload.get("kind") == "success"
            )
            if not artifact_action:
                response = self._dispatch_action(action_name, request_id, payload, now, None)
                return self._retain_response(request_id, digest, response, now)
            entry = self._entry(payload.get("run_handle"))
            if any(item.entry is entry for item in self._artifact_requests.values()):
                raise SequenceCoordinatorError("artifact_verification_capacity", 429)
            if len(self._artifact_requests) >= MAX_ARTIFACT_VERIFICATIONS:
                raise SequenceCoordinatorError("artifact_verification_capacity", 429)
            try:
                managed_run = self._managed.read(entry.run_handle)
            except ManagedRunError as exc:
                raise SequenceCoordinatorError("run_gone", 410) from exc
            ticket = _ArtifactVerificationTicket(
                request_id,
                digest,
                entry,
                replace(entry),
                managed_run,
                self._managed.retained_until(entry.run_handle),
                threading.Event(),
            )
            if not self._artifact_slots.acquire(blocking=False):
                raise SequenceCoordinatorError("artifact_verification_capacity", 429)
            self._artifact_requests[request_id] = ticket
        # CRITICAL: no coordinator lock spans file read/hash/decode/store or preview admission.
        # Hold the request/row reservation until this worker returns, even after cancellation.
        try:
            response = self._dispatch_action(action_name, request_id, payload, now, ticket)
            with self._lock:
                return self._retain_response(request_id, digest, response, self._clock())
        finally:
            with self._lock:
                self._artifact_requests.pop(request_id, None)
                self._artifact_slots.release()

    def _dispatch_action(
        self,
        action_name: str,
        request_id: str,
        payload: dict[str, object],
        now: float,
        ticket: _ArtifactVerificationTicket | None,
    ) -> SequenceCoordinatorResponse:
        if action_name == "prepare_managed_run":
            response = self._prepare_managed(request_id, payload, now)
        elif action_name == "submit_managed_run":
            response = self._submission(payload, now)
        elif action_name == "close_managed_run":
            response = self._close_managed(payload, now, ticket)
        elif action_name == "read_managed_run":
            read = _closed(payload, {"run_handle"}, "managed_read_payload")
            entry = self._entry(read["run_handle"])
            # CRITICAL: a reconnect must retain recorded completion without inventing delivery.
            # The running job projection alone cannot distinguish this wait from generation.
            disposition = (
                "verification_pending"
                if entry.terminal_kind == "success"
                and entry.artifact_receipt is None
                and _first_runtime(entry.state).state is GenerationJobState.RUNNING
                else "current"
            )
            response = self._response(entry, disposition)
        elif action_name == "prepare_sequence":
            response = self._prepare(payload, now)
        elif action_name == "record_submission":
            response = self._submission(payload, now)
        elif action_name == "record_running":
            response = self._running(payload, now)
        elif action_name == "record_artifact":
            response = self._artifact(payload, now, self._required_artifact_ticket(ticket))
        elif action_name == "record_terminal":
            response = self._terminal(payload, now)
        elif action_name == "read_sequence":
            read = _closed(payload, {"run_handle"}, "read_payload")
            response = self._response(self._entry(read["run_handle"]), "current")
        elif action_name == "cancel_sequence":
            response = self._cancel(payload, now)
        elif action_name == "release_sequence":
            response = self._release_sequence(payload, now)
        else:
            raise SequenceCoordinatorError("invalid_action", 400)
        return response

    def _retain_response(
        self,
        request_id: str,
        digest: str,
        response: SequenceCoordinatorResponse,
        now: float,
    ) -> SequenceCoordinatorDispatchResult:
        response_bytes = canonical_bytes(response.to_wire())
        if len(response_bytes) > MAX_COORDINATOR_RESPONSE_BYTES:
            raise RuntimeError("bounded coordinator response invariant violated")
        self._ledger[request_id] = _LedgerEntry(digest, response, now)
        return SequenceCoordinatorDispatchResult(200, response)

    @staticmethod
    def _required_artifact_ticket(
        ticket: _ArtifactVerificationTicket | None,
    ) -> _ArtifactVerificationTicket:
        if ticket is None:
            raise RuntimeError("artifact verification ticket required")
        return ticket

    def _artifact_ticket_cancelled(self, ticket: _ArtifactVerificationTicket) -> bool:
        if ticket.cancelled.is_set():
            return True
        with self._lock:
            try:
                self._assert_artifact_ticket(ticket)
            except SequenceCoordinatorError:
                ticket.cancelled.set()
        return ticket.cancelled.is_set()

    def _check_artifact_ticket(self, ticket: _ArtifactVerificationTicket) -> None:
        with self._lock:
            self._assert_artifact_ticket(ticket)

    def _assert_artifact_ticket(
        self,
        ticket: _ArtifactVerificationTicket,
        *,
        after_publication: bool = False,
    ) -> None:
        # CRITICAL: failures and successes use the same CAS; a decoder returning after cancel,
        # detach or expiry must never publish failure/success over the newer lifecycle authority.
        now = self._clock()
        self._prune(now)
        current = self._runs.get(ticket.entry.run_handle)
        resource = self._managed_resource_for_child(ticket.entry.run_handle)
        try:
            managed = self._managed.read(ticket.entry.run_handle)
        except ManagedRunError:
            managed = None
        invalid = (
            ticket.cancelled.is_set()
            or current is not ticket.entry
            or (
                not after_publication
                and (
                    current.state.fingerprint != ticket.snapshot.state.fingerprint
                    or current.queue_prompt_id != ticket.snapshot.queue_prompt_id
                    or managed != ticket.managed_run
                    or self._managed.retained_until(ticket.entry.run_handle)
                    != ticket.detached_deadline
                )
            )
            or (
                resource is not None
                and (resource.expiry_pending or resource.expiry_transition_complete)
            )
        )
        if invalid:
            ticket.cancelled.set()
            raise SequenceCoordinatorError(
                "stale_artifact_verification", 409, retry_disposition="use_native"
            )


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON number")


def _shape(value: object, *, depth: int = 0) -> int:
    if depth > MAX_COORDINATOR_ACTION_DEPTH:
        raise ValueError("coordinator JSON depth")
    if type(value) is dict:
        if len(value) > MAX_COORDINATOR_ACTION_NODES:
            raise ValueError("coordinator object bound")
        return 1 + sum(1 + _shape(item, depth=depth + 1) for item in value.values())
    if type(value) is list:
        if len(value) > MAX_COORDINATOR_ARRAY:
            raise ValueError("coordinator array bound")
        return 1 + sum(_shape(item, depth=depth + 1) for item in value)
    if value is None or type(value) in {str, int, bool}:
        return 1
    raise ValueError("coordinator scalar")


def decode_coordinator_action_json(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or not payload or len(payload) > MAX_COORDINATOR_ACTION_BYTES:
        raise ValueError("coordinator body bound")
    value = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_pairs,
        parse_constant=_reject_constant,
    )
    if type(value) is not dict or _shape(value) > MAX_COORDINATOR_ACTION_NODES:
        raise ValueError("coordinator body shape")
    return value


def _host_root(method: str) -> Path:
    module = sys.modules.get("folder_paths")
    factory = getattr(module, method, None)
    if not callable(factory):
        raise SequenceCoordinatorError("host_storage_unavailable", 503)
    value = factory()
    if type(value) is not str or not value:
        raise SequenceCoordinatorError("host_storage_unavailable", 503)
    return Path(value)


def _host_output_root() -> Path:
    return _host_root("get_output_directory")


def _host_private_root() -> Path:
    return _host_root("get_temp_directory") / "h3-context" / "managed-artifacts" / _PROCESS_SCOPE


def build_coordinator(
    production_registry: ProductionWorkspaceRegistry,
) -> SequenceCoordinatorRegistry:
    """Construct the process coordinator. Called only by the composition root.

    CRITICAL: the production registry arrives as an argument rather than being reached for here.
    Since M23-31 this constructor binds the process's live-sequence authority onto that registry
    and binding twice is refused, so the coordinator and the production route must be handed the
    same instance. The composition root is what guarantees that, and it is also what makes the
    construction happen exactly once under one lock -- two route handlers run on `asyncio.to_thread`
    worker threads and can both find the table empty on a cold start. Reaching for a module global
    here instead is the tempting simplification that lets the two diverge again.
    """

    return SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=_host_output_root,
        private_root_factory=_host_private_root,
    )


_coordinator = component(SEQUENCE_COORDINATOR, SequenceCoordinatorRegistry)


def dispatch_sequence_coordinator_action(action: object) -> SequenceCoordinatorDispatchResult:
    return _coordinator().dispatch(action)


_ROUTE_POLICY = RoutePolicy(
    path=COORDINATOR_ACTION_ROUTE,
    owner=COORDINATOR_RESPONSE_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_COORDINATOR_ACTION_BYTES,
    refusals=(SequenceCoordinatorError,),
)


def _refusal_body(status: int, reason: str) -> object | None:
    # This route answers a seam-level refusal with the empty-body status surface, except for the
    # unexpected-failure case, which has always carried the typed envelope so a client can tell an
    # internal failure apart from a transport one without reading exception prose.
    if status != 500:
        return None
    return SequenceCoordinatorError(reason, status).to_wire()


def _refuse(error: BaseException) -> RouteResult:
    coordinator_error = (
        error
        if type(error) is SequenceCoordinatorError
        else SequenceCoordinatorError("internal_failure", 500)
    )
    return RouteResult(coordinator_error.status, coordinator_error.to_wire())


def _dispatch_body(action: dict[str, object]) -> RouteResult:
    result = dispatch_sequence_coordinator_action(action)
    if result.response is None:
        return RouteResult(result.status)
    return RouteResult(result.status, result.response.to_wire())


async def _act(payload: bytes, _context: object) -> RouteResult:
    action = decode_coordinator_action_json(payload)
    details = action.get("payload")
    media_work = action.get("action") == "record_artifact" or (
        action.get("action") == "close_managed_run"
        and type(details) is dict
        and details.get("kind") == "success"
        and details.get("artifact") is not None
    )
    # CRITICAL: two long inspections must not occupy the cancellation/read worker lane.
    lane = "coordinator_media" if media_work else "coordinator"
    return await offload_route_handler(lane, lambda: _dispatch_body(action))


def ensure_sequence_coordinator_route_registered() -> bool:
    """Lazily register the same-origin coordinator route on an owned host server."""

    global _ROUTE_REGISTERED
    _ROUTE_REGISTERED = register_owned_route(
        _ROUTE_POLICY,
        __name__,
        _act,
        _refusal_body,
        refusal_mapper=_refuse,
    )
    return _ROUTE_REGISTERED


__all__ = [
    "COORDINATOR_ACTION_ROUTE",
    "COORDINATOR_ACTION_SCHEMA",
    "COORDINATOR_ERROR_SCHEMA",
    "COORDINATOR_RESPONSE_SCHEMA",
    "MAX_COORDINATOR_ACTION_BYTES",
    "ObservedVideoArtifact",
    "PREPARED_GRAPH_OBSERVATION_SCHEMA",
    "PreparedGraphObservation",
    "SavedArtifactLocator",
    "SequenceCoordinatorDispatchResult",
    "SequenceCoordinatorError",
    "SequenceCoordinatorRegistry",
    "SequenceCoordinatorResponse",
    "decode_coordinator_action_json",
    "HOST_MAY_HOLD_JOB_STATES",
    "dispatch_sequence_coordinator_action",
    "ensure_sequence_coordinator_route_registered",
]
