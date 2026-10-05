"""Bounded process-local application service for M26 managed serial generation."""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from typing import NoReturn, Protocol, cast

from comfyui_h3_context.core.canonical import (
    binary64_token,
    canonical_bytes,
    canonical_fingerprint,
    fingerprint_context_report,
)
from comfyui_h3_context.core.context_reporting import ContextReport
from comfyui_h3_context.core.contracts import ProfileIdentity, TaskMode
from comfyui_h3_context.core.managed_sequence import (
    MAX_COORDINATOR_RESPONSE_BYTES,
    EligibleSegmentExecutionV1,
    ManagedModeQualificationV1,
    ManagedModeQualificationV2,
    ManagedSequenceAuthorizationV1,
    ManagedSequenceCurrentProjectionV1,
    ManagedSequenceError,
    ManagedSequenceSegmentAuthorizationV1,
    ManagedSequenceStateV1,
    ManagedSequenceV1,
    SequenceSlotStateV1,
    authorize_managed_sequence,
    bind_prepared_child,
    cancel_managed_sequence,
    check_managed_sequence_canvas_authority,
    coordinator_ledger_rows,
    detach_managed_sequence_client,
    expire_managed_sequence_lease,
    fail_bound_sequence_child,
    fail_prepared_sequence_child,
    mark_managed_sequence_invocation_unknown,
    mark_managed_sequence_unknown_ownership,
    prepare_sequence_child,
    read_managed_sequence_projection,
    record_managed_sequence_artifact,
    record_managed_sequence_canvas_rollback,
    record_managed_sequence_canvas_write,
    record_managed_sequence_reuse,
    record_managed_sequence_running,
    record_managed_sequence_submission,
    record_managed_sequence_terminal,
    resume_managed_sequence,
    retry_managed_sequence_segment,
    start_managed_sequence,
)
from comfyui_h3_context.core.production_import import (
    ProductionAutomaticPlanAuthorityV2,
    SegmentContextMaterializationClaim,
    SegmentContextMaterializationReceiptV1,
)
from comfyui_h3_context.core.production_storyboard import (
    MAX_PROMPT_LENGTH,
    ProposalBlockerCodeV1,
    fingerprint_prompt_text,
)
from comfyui_h3_context.core.registry import ReferenceAsset, build_reference_registry
from comfyui_h3_context.core.segment_workspace import SegmentDuration, derive_segment_manifests

from .comfyui_route_seam import (
    OriginRule,
    RoutePolicy,
    RouteResult,
    already_owned,
    host_web_and_routes,
    register_owned_route,
    route_method_available,
)
from .composition_root import (
    MANAGED_MODE_QUALIFICATION,
    MANAGED_SEQUENCE,
    component,
)
from .production_context_materializer import (
    CanonicalProductionMaterializationSnapshot,
    ProductionCanonicalLoweringV1,
    claim_canonical_production_materialization,
)

MANAGED_SEQUENCE_ACTION_SCHEMA = "h3.context.managed_sequence_action.v1"
MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA = "h3.context.managed_prepared_context_action.v1"
MANAGED_SEQUENCE_ACTION_ROUTE = "/h3-context/v1/managed-sequences"
MANAGED_SEQUENCE_PROJECTION_ROUTE = "/h3-context/v1/managed-sequences/{parent_sequence_id}"
MANAGED_SEQUENCE_RESULT_SCHEMA = "h3.context.managed_sequence_result.v1"
MANAGED_SEQUENCE_READ_RESULT_SCHEMA = "h3.context.managed_sequence_read_result.v1"
MANAGED_SEGMENT_MATERIALIZATION_CLAIM_SCHEMA = "h3.context.managed_segment_materialization_claim.v1"
MANAGED_SEGMENT_MATERIALIZATION_CLAIM_V2_SCHEMA = (
    "h3.context.managed_segment_materialization_claim.v2"
)
MANAGED_PREPARED_CONTEXT_SCHEMA = "h3.context.managed_prepared_context.v1"
MANAGED_CHILD_BINDING_CLAIM_SCHEMA = "h3.context.managed_child_binding_claim.v1"
MANAGED_SEQUENCE_CHILD_AUTHORITY_SCHEMA = "h3.context.managed_sequence_child_authority.v1"
MANAGED_CHILD_BINDING_REQUEST_SCHEMA = "h3.context.managed_child_binding_request.v1"
MANAGED_CHILD_CORRELATION_SCHEMA = "h3.context.managed_child_correlation.v1"
MANAGED_PREPARED_GRAPH_OBSERVATION_SCHEMA = "h3.context.prepared_graph_observation.v4"
MANAGED_INPUT_GEOMETRY_IDENTITY_SCHEMA = "h3.context.input_geometry.receipt.v2"
MANAGED_SEQUENCE_START_RESOURCE_REQUEST_SCHEMA = (
    "h3.context.managed_sequence_start_resource_request.v1"
)
MANAGED_SEQUENCE_START_RESOURCE_CLAIM_SCHEMA = "h3.context.managed_sequence_start_resource_claim.v1"

MAX_MANAGED_SEQUENCE_MUTATION_ROWS = 115
MAX_MANAGED_SEQUENCE_ACTION_BYTES = 32_768
MAX_MANAGED_SEQUENCE_ACTION_DEPTH = 5
MAX_MANAGED_SEQUENCE_ACTION_NODES = 256
MAX_MANAGED_SEQUENCE_ACTION_ARRAY = 32
MAX_MANAGED_PREPARED_CONTEXT_BYTES = 96 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_WORKSPACE_HANDLE = re.compile(r"pw_[A-Za-z0-9_-]{32,96}\Z")
_CONTEXT_HANDLE = re.compile(r"ws_[A-Za-z0-9_-]{32,96}\Z")
_RUN_HANDLE = re.compile(r"mc_[A-Za-z0-9_-]{32,96}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GEOMETRY_RECEIPT_HANDLE = re.compile(r"igr_[A-Za-z0-9_-]{32,96}\Z")
_MANAGED_GRAPH_ROUTES = frozenset({"new", "replace", "existing"})
_MANAGED_ROUTE_OWNER_ATTRIBUTE = "__h3_context_managed_sequence_v1__"
_ROUTE_REGISTERED = False


class ManagedSequenceServiceError(ValueError):
    """Closed content-free application-service refusal."""

    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ManagedSequenceServiceError(f"invalid_{field_name}", 400)
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise ManagedSequenceServiceError(f"invalid_{field_name}", 400)
    return value


def _revision(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ManagedSequenceServiceError("invalid_parent_revision", 400)
    return value


def _request_id(value: object) -> str:
    return _identifier(value, "request_id")


def _closed_value(value: object, fields: set[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ManagedSequenceServiceError(f"invalid_{field_name}", 400)
    return value


@dataclass(frozen=True, slots=True)
class ManagedChildCorrelationV1:
    pending_correlation_id: str
    execution_node_id: str
    schema: str = MANAGED_CHILD_CORRELATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_CHILD_CORRELATION_SCHEMA:
            raise ManagedSequenceServiceError("child_correlation_schema", 400)
        pending = _identifier(
            self.pending_correlation_id,
            "child_pending_correlation_id",
        )
        if not pending.startswith("managed.pending."):
            raise ManagedSequenceServiceError("child_pending_correlation_id", 400)
        _identifier(self.execution_node_id, "child_correlation_execution_node_id")

    @classmethod
    def from_wire(cls, value: object) -> ManagedChildCorrelationV1:
        wire = _closed_value(
            value,
            {"schema", "pending_correlation_id", "execution_node_id"},
            "child_correlation",
        )
        return cls(
            schema=wire["schema"],  # type: ignore[arg-type]
            pending_correlation_id=wire["pending_correlation_id"],  # type: ignore[arg-type]
            execution_node_id=wire["execution_node_id"],  # type: ignore[arg-type]
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "pending_correlation_id": self.pending_correlation_id,
            "execution_node_id": self.execution_node_id,
        }


@dataclass(frozen=True, slots=True)
class ManagedInputGeometryIdentityV1:
    receipt_handle: str
    source_fingerprint: str
    schema: str = MANAGED_INPUT_GEOMETRY_IDENTITY_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema != MANAGED_INPUT_GEOMETRY_IDENTITY_SCHEMA
            or type(self.receipt_handle) is not str
            or _GEOMETRY_RECEIPT_HANDLE.fullmatch(self.receipt_handle) is None
        ):
            raise ManagedSequenceServiceError("invalid_child_source_identity", 400)
        _fingerprint(self.source_fingerprint, "child_source_fingerprint")

    @classmethod
    def from_wire(cls, value: object) -> ManagedInputGeometryIdentityV1:
        wire = _closed_value(
            value,
            {"schema", "receipt_handle", "source_fingerprint"},
            "child_source_identity",
        )
        return cls(
            schema=wire["schema"],  # type: ignore[arg-type]
            receipt_handle=wire["receipt_handle"],  # type: ignore[arg-type]
            source_fingerprint=wire["source_fingerprint"],  # type: ignore[arg-type]
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "receipt_handle": self.receipt_handle,
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ManagedPreparedGraphObservationV1:
    route: str
    graph_fingerprint: str
    compiled_prompt_fingerprint: str
    owned_projection_fingerprint: str
    owned_node_ids: tuple[str, ...]
    owned_link_ids: tuple[str, ...]
    model_fingerprint: str
    runtime_fingerprint: str
    fingerprint_domain: str
    expected_frames: int
    source_identity: ManagedInputGeometryIdentityV1 | None
    timeout_ms: int
    native_anchor_node_id: str
    schema: str = MANAGED_PREPARED_GRAPH_OBSERVATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_PREPARED_GRAPH_OBSERVATION_SCHEMA:
            raise ManagedSequenceServiceError("prepared_graph_observation_schema", 400)
        if self.route not in _MANAGED_GRAPH_ROUTES:
            raise ManagedSequenceServiceError("prepared_graph_observation_route", 422)
        for value, field_name in (
            (self.graph_fingerprint, "child_graph_fingerprint"),
            (self.compiled_prompt_fingerprint, "child_compiled_prompt_fingerprint"),
            (self.owned_projection_fingerprint, "child_owned_projection_fingerprint"),
            (self.model_fingerprint, "child_model_fingerprint"),
            (self.runtime_fingerprint, "child_runtime_fingerprint"),
        ):
            _fingerprint(value, field_name)
        if (
            type(self.owned_node_ids) is not tuple
            or not 1 <= len(self.owned_node_ids) <= MAX_MANAGED_SEQUENCE_ACTION_ARRAY
            or type(self.owned_link_ids) is not tuple
            or len(self.owned_link_ids) > MAX_MANAGED_SEQUENCE_ACTION_ARRAY
        ):
            raise ManagedSequenceServiceError("invalid_child_owned_graph_identity", 400)
        for value in self.owned_node_ids:
            _identifier(value, "child_owned_node_id")
        for value in self.owned_link_ids:
            _identifier(value, "child_owned_link_id")
        if self.fingerprint_domain != "output_producing_graph":
            raise ManagedSequenceServiceError("child_fingerprint_domain", 400)
        if type(self.expected_frames) is not int or not 1 <= self.expected_frames <= 512:
            raise ManagedSequenceServiceError("child_expected_frames", 400)
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= 86_400_000:
            raise ManagedSequenceServiceError("child_timeout_ms", 400)
        if (
            self.source_identity is not None
            and type(self.source_identity) is not ManagedInputGeometryIdentityV1
        ):
            raise ManagedSequenceServiceError("invalid_child_source_identity", 400)
        _identifier(self.native_anchor_node_id, "child_native_anchor_node_id")

    @classmethod
    def from_wire(cls, value: object) -> ManagedPreparedGraphObservationV1:
        wire = _closed_value(
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
            "prepared_graph_observation",
        )
        node_ids = wire["owned_node_ids"]
        link_ids = wire["owned_link_ids"]
        if type(node_ids) is not list or type(link_ids) is not list:
            raise ManagedSequenceServiceError("invalid_child_owned_graph_identity", 400)
        source = wire["source_identity"]
        return cls(
            schema=wire["schema"],  # type: ignore[arg-type]
            route=wire["route"],  # type: ignore[arg-type]
            graph_fingerprint=wire["graph_fingerprint"],  # type: ignore[arg-type]
            compiled_prompt_fingerprint=wire["compiled_prompt_fingerprint"],  # type: ignore[arg-type]
            owned_projection_fingerprint=wire["owned_projection_fingerprint"],  # type: ignore[arg-type]
            owned_node_ids=tuple(node_ids),
            owned_link_ids=tuple(link_ids),
            model_fingerprint=wire["model_fingerprint"],  # type: ignore[arg-type]
            runtime_fingerprint=wire["runtime_fingerprint"],  # type: ignore[arg-type]
            fingerprint_domain=wire["fingerprint_domain"],  # type: ignore[arg-type]
            expected_frames=wire["expected_frames"],  # type: ignore[arg-type]
            source_identity=(
                None if source is None else ManagedInputGeometryIdentityV1.from_wire(source)
            ),
            timeout_ms=wire["timeout_ms"],  # type: ignore[arg-type]
            native_anchor_node_id=wire["native_anchor_node_id"],  # type: ignore[arg-type]
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
            "fingerprint_domain": self.fingerprint_domain,
            "expected_frames": self.expected_frames,
            "source_identity": (
                None if self.source_identity is None else self.source_identity.to_wire()
            ),
            "timeout_ms": self.timeout_ms,
            "native_anchor_node_id": self.native_anchor_node_id,
        }


@dataclass(frozen=True, slots=True)
class AuthorizeManagedSequenceRequestV1:
    request_id: str
    workspace_handle: str
    expected_workspace_revision: int
    expected_workspace_fingerprint: str
    expected_plan_fingerprint: str
    generation_plan_fingerprint: str
    compiler_fingerprint: str
    host_capability_fingerprint: str
    explicit_intent: str
    schema: str = MANAGED_SEQUENCE_ACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_ACTION_SCHEMA:
            raise ManagedSequenceServiceError("authorization_request_schema", 400)
        _request_id(self.request_id)
        if (
            type(self.workspace_handle) is not str
            or _WORKSPACE_HANDLE.fullmatch(self.workspace_handle) is None
        ):
            raise ManagedSequenceServiceError("invalid_workspace_handle", 400)
        _revision(self.expected_workspace_revision)
        for value, name in (
            (self.expected_workspace_fingerprint, "workspace_fingerprint"),
            (self.expected_plan_fingerprint, "plan_fingerprint"),
            (self.generation_plan_fingerprint, "generation_plan_fingerprint"),
            (self.compiler_fingerprint, "compiler_fingerprint"),
            (self.host_capability_fingerprint, "host_capability_fingerprint"),
        ):
            _fingerprint(value, name)
        if self.explicit_intent != "generate_approved_sequence":
            raise ManagedSequenceServiceError("explicit_intent", 422)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "workspace_handle": self.workspace_handle,
            "expected_workspace_revision": self.expected_workspace_revision,
            "expected_workspace_fingerprint": self.expected_workspace_fingerprint,
            "expected_plan_fingerprint": self.expected_plan_fingerprint,
            "generation_plan_fingerprint": self.generation_plan_fingerprint,
            "compiler_fingerprint": self.compiler_fingerprint,
            "host_capability_fingerprint": self.host_capability_fingerprint,
            "explicit_intent": self.explicit_intent,
        }


@dataclass(frozen=True, slots=True)
class StartManagedSequenceRequestV1:
    request_id: str
    parent_sequence_id: str
    expected_revision: int
    authorization_fingerprint: str
    schema: str = MANAGED_SEQUENCE_ACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_ACTION_SCHEMA:
            raise ManagedSequenceServiceError("start_request_schema", 400)
        _request_id(self.request_id)
        _identifier(self.parent_sequence_id, "parent_sequence_id")
        _revision(self.expected_revision)
        _fingerprint(self.authorization_fingerprint, "authorization_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "parent_sequence_id": self.parent_sequence_id,
            "expected_revision": self.expected_revision,
            "authorization_fingerprint": self.authorization_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class PrepareManagedSequenceChildRequestV1:
    request_id: str
    parent_sequence_id: str
    expected_revision: int
    authorization_fingerprint: str
    segment_id: str
    predecessor_terminal_fingerprint: str | None
    schema: str = MANAGED_SEQUENCE_ACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_ACTION_SCHEMA:
            raise ManagedSequenceServiceError("prepare_request_schema", 400)
        _request_id(self.request_id)
        _identifier(self.parent_sequence_id, "parent_sequence_id")
        _revision(self.expected_revision)
        _fingerprint(self.authorization_fingerprint, "authorization_fingerprint")
        _identifier(self.segment_id, "segment_id")
        if self.predecessor_terminal_fingerprint is not None:
            _fingerprint(
                self.predecessor_terminal_fingerprint,
                "predecessor_terminal_fingerprint",
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "parent_sequence_id": self.parent_sequence_id,
            "expected_revision": self.expected_revision,
            "authorization_fingerprint": self.authorization_fingerprint,
            "segment_id": self.segment_id,
            "predecessor_terminal_fingerprint": self.predecessor_terminal_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ReadManagedPreparedContextRequestV1:
    request_id: str
    parent_sequence_id: str
    expected_revision: int
    authorization_fingerprint: str
    segment_id: str
    eligible_execution_fingerprint: str
    materialization_receipt_fingerprint: str
    context_workspace_handle: str
    schema: str = MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA:
            raise ManagedSequenceServiceError("prepared_context_request_schema", 400)
        _request_id(self.request_id)
        _identifier(self.parent_sequence_id, "parent_sequence_id")
        _revision(self.expected_revision)
        _fingerprint(self.authorization_fingerprint, "authorization_fingerprint")
        _identifier(self.segment_id, "segment_id")
        _fingerprint(self.eligible_execution_fingerprint, "eligible_execution_fingerprint")
        _fingerprint(
            self.materialization_receipt_fingerprint,
            "materialization_receipt_fingerprint",
        )
        if (
            type(self.context_workspace_handle) is not str
            or _CONTEXT_HANDLE.fullmatch(self.context_workspace_handle) is None
        ):
            raise ManagedSequenceServiceError("invalid_context_workspace_handle", 400)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "parent_sequence_id": self.parent_sequence_id,
            "expected_revision": self.expected_revision,
            "authorization_fingerprint": self.authorization_fingerprint,
            "segment_id": self.segment_id,
            "eligible_execution_fingerprint": self.eligible_execution_fingerprint,
            "materialization_receipt_fingerprint": (self.materialization_receipt_fingerprint),
            "context_workspace_handle": self.context_workspace_handle,
        }


@dataclass(frozen=True, slots=True)
class BindManagedSequenceChildRequestV1:
    request_id: str
    parent_sequence_id: str
    expected_revision: int
    authorization_fingerprint: str
    segment_id: str
    eligible_execution_fingerprint: str
    materialization_receipt_fingerprint: str
    graph_fingerprint: str
    compiled_prompt_fingerprint: str
    owned_projection_fingerprint: str
    previous_owned_projection_fingerprint: str
    active_workflow_fingerprint: str
    correlation: ManagedChildCorrelationV1
    observation: ManagedPreparedGraphObservationV1
    schema: str = MANAGED_SEQUENCE_ACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_ACTION_SCHEMA:
            raise ManagedSequenceServiceError("bind_request_schema", 400)
        _request_id(self.request_id)
        _identifier(self.parent_sequence_id, "parent_sequence_id")
        _revision(self.expected_revision)
        _identifier(self.segment_id, "segment_id")
        for value, name in (
            (self.authorization_fingerprint, "authorization_fingerprint"),
            (self.eligible_execution_fingerprint, "eligible_execution_fingerprint"),
            (
                self.materialization_receipt_fingerprint,
                "materialization_receipt_fingerprint",
            ),
            (self.graph_fingerprint, "graph_fingerprint"),
            (self.compiled_prompt_fingerprint, "compiled_prompt_fingerprint"),
            (self.owned_projection_fingerprint, "owned_projection_fingerprint"),
            (
                self.previous_owned_projection_fingerprint,
                "previous_owned_projection_fingerprint",
            ),
            (self.active_workflow_fingerprint, "active_workflow_fingerprint"),
        ):
            _fingerprint(value, name)
        if type(self.correlation) is not ManagedChildCorrelationV1:
            raise ManagedSequenceServiceError("bind_child_correlation", 400)
        if type(self.observation) is not ManagedPreparedGraphObservationV1:
            raise ManagedSequenceServiceError("bind_child_observation", 400)
        if (
            self.graph_fingerprint != self.observation.graph_fingerprint
            or self.compiled_prompt_fingerprint != self.observation.compiled_prompt_fingerprint
            or self.owned_projection_fingerprint != self.observation.owned_projection_fingerprint
        ):
            raise ManagedSequenceServiceError("bind_observation_drift", 409)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "parent_sequence_id": self.parent_sequence_id,
            "expected_revision": self.expected_revision,
            "authorization_fingerprint": self.authorization_fingerprint,
            "segment_id": self.segment_id,
            "eligible_execution_fingerprint": self.eligible_execution_fingerprint,
            "materialization_receipt_fingerprint": self.materialization_receipt_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "compiled_prompt_fingerprint": self.compiled_prompt_fingerprint,
            "owned_projection_fingerprint": self.owned_projection_fingerprint,
            "previous_owned_projection_fingerprint": (self.previous_owned_projection_fingerprint),
            "active_workflow_fingerprint": self.active_workflow_fingerprint,
            "correlation": self.correlation.to_wire(),
            "observation": self.observation.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ManagedSequenceMutationAuthorityV1:
    request_id: str
    parent_sequence_id: str
    expected_revision: int
    authorization_fingerprint: str
    schema: str = MANAGED_SEQUENCE_ACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_ACTION_SCHEMA:
            raise ManagedSequenceServiceError("mutation_authority_schema", 400)
        _request_id(self.request_id)
        _identifier(self.parent_sequence_id, "parent_sequence_id")
        _revision(self.expected_revision)
        _fingerprint(self.authorization_fingerprint, "authorization_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "parent_sequence_id": self.parent_sequence_id,
            "expected_revision": self.expected_revision,
            "authorization_fingerprint": self.authorization_fingerprint,
        }


def _transition_wire(
    authority: ManagedSequenceMutationAuthorityV1,
    **values: object,
) -> dict[str, object]:
    if type(authority) is not ManagedSequenceMutationAuthorityV1:
        raise ManagedSequenceServiceError("mutation_authority_type", 400)
    return {**authority.to_wire(), **values}


@dataclass(frozen=True, slots=True)
class FailPreparedManagedSequenceChildRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    eligible_execution_fingerprint: str
    failure_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        _fingerprint(
            self.eligible_execution_fingerprint,
            "eligible_execution_fingerprint",
        )
        _fingerprint(self.failure_fingerprint, "failure_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            eligible_execution_fingerprint=self.eligible_execution_fingerprint,
            failure_fingerprint=self.failure_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class FailBoundManagedSequenceChildRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    child_run_handle: str
    failure_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        if (
            type(self.child_run_handle) is not str
            or _RUN_HANDLE.fullmatch(self.child_run_handle) is None
        ):
            raise ManagedSequenceServiceError("invalid_child_run_handle", 400)
        _fingerprint(self.failure_fingerprint, "failure_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            child_run_handle=self.child_run_handle,
            failure_fingerprint=self.failure_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class MarkManagedSequenceInvocationUnknownRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    child_run_handle: str
    timeout_ms: int
    active_workflow_fingerprint: str
    previous_owned_projection_fingerprint: str
    written_owned_projection_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        if (
            type(self.child_run_handle) is not str
            or _RUN_HANDLE.fullmatch(self.child_run_handle) is None
        ):
            raise ManagedSequenceServiceError("invalid_child_run_handle", 400)
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= 86_400_000:
            raise ManagedSequenceServiceError("invalid_timeout", 400)
        for value, name in (
            (self.active_workflow_fingerprint, "active_workflow_fingerprint"),
            (
                self.previous_owned_projection_fingerprint,
                "previous_owned_projection_fingerprint",
            ),
            (
                self.written_owned_projection_fingerprint,
                "written_owned_projection_fingerprint",
            ),
        ):
            _fingerprint(value, name)

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            child_run_handle=self.child_run_handle,
            timeout_ms=self.timeout_ms,
            active_workflow_fingerprint=self.active_workflow_fingerprint,
            previous_owned_projection_fingerprint=(self.previous_owned_projection_fingerprint),
            written_owned_projection_fingerprint=(self.written_owned_projection_fingerprint),
        )


@dataclass(frozen=True, slots=True)
class RecordManagedSequenceSubmissionRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    child_run_handle: str
    child_state_fingerprint: str
    queue_prompt_id: str
    timeout_ms: int
    active_workflow_fingerprint: str
    previous_owned_projection_fingerprint: str
    written_owned_projection_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        if (
            type(self.child_run_handle) is not str
            or _RUN_HANDLE.fullmatch(self.child_run_handle) is None
        ):
            raise ManagedSequenceServiceError("invalid_child_run_handle", 400)
        _identifier(self.queue_prompt_id, "queue_prompt_id")
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= 86_400_000:
            raise ManagedSequenceServiceError("invalid_timeout", 400)
        for value, name in (
            (self.child_state_fingerprint, "child_state_fingerprint"),
            (self.active_workflow_fingerprint, "active_workflow_fingerprint"),
            (
                self.previous_owned_projection_fingerprint,
                "previous_owned_projection_fingerprint",
            ),
            (
                self.written_owned_projection_fingerprint,
                "written_owned_projection_fingerprint",
            ),
        ):
            _fingerprint(value, name)

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            child_run_handle=self.child_run_handle,
            child_state_fingerprint=self.child_state_fingerprint,
            queue_prompt_id=self.queue_prompt_id,
            timeout_ms=self.timeout_ms,
            active_workflow_fingerprint=self.active_workflow_fingerprint,
            previous_owned_projection_fingerprint=(self.previous_owned_projection_fingerprint),
            written_owned_projection_fingerprint=(self.written_owned_projection_fingerprint),
        )


@dataclass(frozen=True, slots=True)
class RecordManagedSequenceRunningRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    queue_prompt_id: str
    child_state_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        _identifier(self.queue_prompt_id, "queue_prompt_id")
        _fingerprint(self.child_state_fingerprint, "child_state_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            queue_prompt_id=self.queue_prompt_id,
            child_state_fingerprint=self.child_state_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class RecordManagedSequenceArtifactRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    queue_prompt_id: str
    artifact_receipt_fingerprint: str
    artifact_bytes: int

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        _identifier(self.queue_prompt_id, "queue_prompt_id")
        _fingerprint(self.artifact_receipt_fingerprint, "artifact_receipt_fingerprint")
        if type(self.artifact_bytes) is not int or not 1 <= self.artifact_bytes <= 2**31:
            raise ManagedSequenceServiceError("invalid_artifact_bytes", 400)

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            queue_prompt_id=self.queue_prompt_id,
            artifact_receipt_fingerprint=self.artifact_receipt_fingerprint,
            artifact_bytes=self.artifact_bytes,
        )


@dataclass(frozen=True, slots=True)
class RecordManagedSequenceTerminalRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    queue_prompt_id: str
    kind: str
    terminal_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        _identifier(self.queue_prompt_id, "queue_prompt_id")
        if self.kind not in {"success", "error", "interrupted"}:
            raise ManagedSequenceServiceError("invalid_terminal_kind", 400)
        _fingerprint(self.terminal_fingerprint, "terminal_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            queue_prompt_id=self.queue_prompt_id,
            kind=self.kind,
            terminal_fingerprint=self.terminal_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class RecordManagedSequenceReuseRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    artifact_receipt_fingerprint: str
    artifact_bytes: int
    terminal_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        _fingerprint(self.artifact_receipt_fingerprint, "artifact_receipt_fingerprint")
        _fingerprint(self.terminal_fingerprint, "terminal_fingerprint")
        if type(self.artifact_bytes) is not int or not 1 <= self.artifact_bytes <= 2**31:
            raise ManagedSequenceServiceError("invalid_artifact_bytes", 400)

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            artifact_receipt_fingerprint=self.artifact_receipt_fingerprint,
            artifact_bytes=self.artifact_bytes,
            terminal_fingerprint=self.terminal_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class ResumeManagedSequenceRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    active_workflow_fingerprint: str
    owned_projection_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _fingerprint(self.active_workflow_fingerprint, "active_workflow_fingerprint")
        _fingerprint(self.owned_projection_fingerprint, "owned_projection_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            active_workflow_fingerprint=self.active_workflow_fingerprint,
            owned_projection_fingerprint=self.owned_projection_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class RecordManagedSequenceCanvasRollbackRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    active_workflow_fingerprint: str
    drifted_owned_projection_fingerprint: str
    restored_owned_projection_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        for value, name in (
            (self.active_workflow_fingerprint, "active_workflow_fingerprint"),
            (
                self.drifted_owned_projection_fingerprint,
                "drifted_owned_projection_fingerprint",
            ),
            (
                self.restored_owned_projection_fingerprint,
                "restored_owned_projection_fingerprint",
            ),
        ):
            _fingerprint(value, name)

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            active_workflow_fingerprint=self.active_workflow_fingerprint,
            drifted_owned_projection_fingerprint=(self.drifted_owned_projection_fingerprint),
            restored_owned_projection_fingerprint=(self.restored_owned_projection_fingerprint),
        )


@dataclass(frozen=True, slots=True)
class UnknownManagedSequenceOwnershipRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    queue_prompt_id: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        _identifier(self.queue_prompt_id, "queue_prompt_id")

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            queue_prompt_id=self.queue_prompt_id,
        )


@dataclass(frozen=True, slots=True)
class ManagedSequenceSegmentMutationRequestV1:
    authority: ManagedSequenceMutationAuthorityV1
    segment_id: str
    active_workflow_fingerprint: str
    owned_projection_fingerprint: str

    def __post_init__(self) -> None:
        _transition_wire(self.authority)
        _identifier(self.segment_id, "segment_id")
        _fingerprint(self.active_workflow_fingerprint, "active_workflow_fingerprint")
        _fingerprint(self.owned_projection_fingerprint, "owned_projection_fingerprint")

    def to_wire(self) -> dict[str, object]:
        return _transition_wire(
            self.authority,
            segment_id=self.segment_id,
            active_workflow_fingerprint=self.active_workflow_fingerprint,
            owned_projection_fingerprint=self.owned_projection_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class DecodedManagedSequenceActionV1:
    action: str
    request: object
    qualification_fingerprint: str | None = None


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON number")


def _action_shape(value: object, *, depth: int = 0) -> int:
    if depth > MAX_MANAGED_SEQUENCE_ACTION_DEPTH:
        raise ValueError("managed sequence JSON depth")
    if type(value) is dict:
        if len(value) > MAX_MANAGED_SEQUENCE_ACTION_NODES:
            raise ValueError("managed sequence object bound")
        return 1 + sum(1 + _action_shape(item, depth=depth + 1) for item in value.values())
    if type(value) is list:
        if len(value) > MAX_MANAGED_SEQUENCE_ACTION_ARRAY:
            raise ValueError("managed sequence array bound")
        return 1 + sum(_action_shape(item, depth=depth + 1) for item in value)
    if value is None or type(value) in {str, int, bool}:
        return 1
    raise ValueError("managed sequence scalar")


def _closed_action_payload(
    value: object,
    expected: set[str],
) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ManagedSequenceServiceError("invalid_action_envelope", 400)
    return value


_PARENT_ACTION_FIELDS = {
    "parent_sequence_id",
    "expected_revision",
    "authorization_fingerprint",
}


def _decoded_mutation_authority(
    payload: dict[str, object],
    request_id: str,
) -> ManagedSequenceMutationAuthorityV1:
    return ManagedSequenceMutationAuthorityV1(
        request_id=request_id,
        parent_sequence_id=payload["parent_sequence_id"],  # type: ignore[arg-type]
        expected_revision=payload["expected_revision"],  # type: ignore[arg-type]
        authorization_fingerprint=payload["authorization_fingerprint"],  # type: ignore[arg-type]
    )


def decode_managed_sequence_action_json(payload: bytes) -> DecodedManagedSequenceActionV1:
    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > MAX_MANAGED_SEQUENCE_ACTION_BYTES
    ):
        raise ValueError("managed sequence body bound")
    value = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_json_pairs,
        parse_constant=_reject_json_constant,
    )
    envelope = _closed_action_payload(value, {"schema", "request_id", "action", "payload"})
    request_id = _request_id(envelope["request_id"])
    action = _identifier(envelope["action"], "action")
    raw = envelope["payload"]
    if _action_shape(value) > MAX_MANAGED_SEQUENCE_ACTION_NODES:
        raise ValueError("managed sequence body shape")

    if envelope["schema"] == MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA:
        if action != "read_prepared_child_context":
            raise ManagedSequenceServiceError("prepared_context_action", 400)
        body = _closed_action_payload(
            raw,
            _PARENT_ACTION_FIELDS
            | {
                "segment_id",
                "eligible_execution_fingerprint",
                "materialization_receipt_fingerprint",
                "context_workspace_handle",
            },
        )
        return DecodedManagedSequenceActionV1(
            action,
            ReadManagedPreparedContextRequestV1(
                request_id=request_id,
                parent_sequence_id=body["parent_sequence_id"],  # type: ignore[arg-type]
                expected_revision=body["expected_revision"],  # type: ignore[arg-type]
                authorization_fingerprint=body["authorization_fingerprint"],  # type: ignore[arg-type]
                segment_id=body["segment_id"],  # type: ignore[arg-type]
                eligible_execution_fingerprint=body["eligible_execution_fingerprint"],  # type: ignore[arg-type]
                materialization_receipt_fingerprint=body["materialization_receipt_fingerprint"],  # type: ignore[arg-type]
                context_workspace_handle=body["context_workspace_handle"],  # type: ignore[arg-type]
            ),
        )
    if envelope["schema"] != MANAGED_SEQUENCE_ACTION_SCHEMA:
        raise ManagedSequenceServiceError("action_schema", 400)

    if action == "authorize_sequence":
        body = _closed_action_payload(
            raw,
            {
                "workspace_handle",
                "expected_workspace_revision",
                "expected_workspace_fingerprint",
                "expected_plan_fingerprint",
                "generation_plan_fingerprint",
                "compiler_fingerprint",
                "host_capability_fingerprint",
                "explicit_intent",
            },
        )
        authorize_request = AuthorizeManagedSequenceRequestV1(
            request_id=request_id,
            **body,  # type: ignore[arg-type]
        )
        return DecodedManagedSequenceActionV1(action, authorize_request)
    if action == "start_sequence":
        body = _closed_action_payload(raw, _PARENT_ACTION_FIELDS | {"qualification_fingerprint"})
        authority_body = {key: body[key] for key in _PARENT_ACTION_FIELDS}
        start_request = StartManagedSequenceRequestV1(
            request_id=request_id,
            parent_sequence_id=authority_body["parent_sequence_id"],  # type: ignore[arg-type]
            expected_revision=authority_body["expected_revision"],  # type: ignore[arg-type]
            authorization_fingerprint=authority_body["authorization_fingerprint"],  # type: ignore[arg-type]
        )
        return DecodedManagedSequenceActionV1(
            action,
            start_request,
            _fingerprint(body["qualification_fingerprint"], "qualification_fingerprint"),
        )
    if action == "prepare_sequence_child":
        body = _closed_action_payload(
            raw,
            _PARENT_ACTION_FIELDS | {"segment_id", "predecessor_terminal_fingerprint"},
        )
        predecessor = body["predecessor_terminal_fingerprint"]
        prepare_request = PrepareManagedSequenceChildRequestV1(
            request_id=request_id,
            parent_sequence_id=body["parent_sequence_id"],  # type: ignore[arg-type]
            expected_revision=body["expected_revision"],  # type: ignore[arg-type]
            authorization_fingerprint=body["authorization_fingerprint"],  # type: ignore[arg-type]
            segment_id=body["segment_id"],  # type: ignore[arg-type]
            predecessor_terminal_fingerprint=predecessor,  # type: ignore[arg-type]
        )
        return DecodedManagedSequenceActionV1(action, prepare_request)
    if action == "bind_prepared_child":
        bind_fields = {
            "segment_id",
            "eligible_execution_fingerprint",
            "materialization_receipt_fingerprint",
            "graph_fingerprint",
            "compiled_prompt_fingerprint",
            "owned_projection_fingerprint",
            "previous_owned_projection_fingerprint",
            "active_workflow_fingerprint",
            "correlation",
            "observation",
        }
        body = _closed_action_payload(raw, _PARENT_ACTION_FIELDS | bind_fields)
        bind_request = BindManagedSequenceChildRequestV1(
            request_id=request_id,
            parent_sequence_id=body["parent_sequence_id"],  # type: ignore[arg-type]
            expected_revision=body["expected_revision"],  # type: ignore[arg-type]
            authorization_fingerprint=body["authorization_fingerprint"],  # type: ignore[arg-type]
            segment_id=body["segment_id"],  # type: ignore[arg-type]
            eligible_execution_fingerprint=body["eligible_execution_fingerprint"],  # type: ignore[arg-type]
            materialization_receipt_fingerprint=body["materialization_receipt_fingerprint"],  # type: ignore[arg-type]
            graph_fingerprint=body["graph_fingerprint"],  # type: ignore[arg-type]
            compiled_prompt_fingerprint=body["compiled_prompt_fingerprint"],  # type: ignore[arg-type]
            owned_projection_fingerprint=body["owned_projection_fingerprint"],  # type: ignore[arg-type]
            previous_owned_projection_fingerprint=body["previous_owned_projection_fingerprint"],  # type: ignore[arg-type]
            active_workflow_fingerprint=body["active_workflow_fingerprint"],  # type: ignore[arg-type]
            correlation=ManagedChildCorrelationV1.from_wire(body["correlation"]),
            observation=ManagedPreparedGraphObservationV1.from_wire(body["observation"]),
        )
        return DecodedManagedSequenceActionV1(action, bind_request)

    fields_by_action = {
        "fail_bound_child": {
            "segment_id",
            "child_run_handle",
            "failure_fingerprint",
        },
        "fail_prepared_child": {
            "segment_id",
            "eligible_execution_fingerprint",
            "failure_fingerprint",
        },
        "record_submission": {
            "segment_id",
            "child_run_handle",
            "child_state_fingerprint",
            "queue_prompt_id",
            "timeout_ms",
            "active_workflow_fingerprint",
            "previous_owned_projection_fingerprint",
            "written_owned_projection_fingerprint",
        },
        "record_running": {"segment_id", "queue_prompt_id", "child_state_fingerprint"},
        "record_artifact": {
            "segment_id",
            "queue_prompt_id",
            "artifact_receipt_fingerprint",
            "artifact_bytes",
        },
        "record_terminal": {
            "segment_id",
            "queue_prompt_id",
            "kind",
            "terminal_fingerprint",
        },
        "record_reuse": {
            "segment_id",
            "artifact_receipt_fingerprint",
            "artifact_bytes",
            "terminal_fingerprint",
        },
        "detach_client": set(),
        "resume_sequence": {
            "active_workflow_fingerprint",
            "owned_projection_fingerprint",
        },
        "record_canvas_rollback": {
            "segment_id",
            "active_workflow_fingerprint",
            "drifted_owned_projection_fingerprint",
            "restored_owned_projection_fingerprint",
        },
        "mark_invocation_unknown": {
            "segment_id",
            "child_run_handle",
            "timeout_ms",
            "active_workflow_fingerprint",
            "previous_owned_projection_fingerprint",
            "written_owned_projection_fingerprint",
        },
        "mark_unknown_ownership": {"segment_id", "queue_prompt_id"},
        "retry_segment": {
            "segment_id",
            "active_workflow_fingerprint",
            "owned_projection_fingerprint",
        },
        "cancel_sequence": set(),
    }
    action_fields = fields_by_action.get(action)
    if action_fields is None:
        raise ManagedSequenceServiceError("unknown_action", 400)
    body = _closed_action_payload(raw, _PARENT_ACTION_FIELDS | action_fields)
    authority = _decoded_mutation_authority(body, request_id)
    specific = {key: body[key] for key in action_fields}
    request_types: dict[str, type] = {
        "fail_bound_child": FailBoundManagedSequenceChildRequestV1,
        "fail_prepared_child": FailPreparedManagedSequenceChildRequestV1,
        "record_submission": RecordManagedSequenceSubmissionRequestV1,
        "record_running": RecordManagedSequenceRunningRequestV1,
        "record_artifact": RecordManagedSequenceArtifactRequestV1,
        "record_terminal": RecordManagedSequenceTerminalRequestV1,
        "record_reuse": RecordManagedSequenceReuseRequestV1,
        "resume_sequence": ResumeManagedSequenceRequestV1,
        "record_canvas_rollback": RecordManagedSequenceCanvasRollbackRequestV1,
        "mark_invocation_unknown": MarkManagedSequenceInvocationUnknownRequestV1,
        "mark_unknown_ownership": UnknownManagedSequenceOwnershipRequestV1,
        "retry_segment": ManagedSequenceSegmentMutationRequestV1,
    }
    request_type = request_types.get(action)
    mutation_request = (
        authority if request_type is None else request_type(authority=authority, **specific)
    )
    return DecodedManagedSequenceActionV1(action, mutation_request)


@dataclass(frozen=True, slots=True)
class ManagedSequenceStartResourceRequestV1:
    parent_sequence_id: str
    parent_authorization_fingerprint: str
    segment_count: int
    coordinator_reserved_rows: int
    coordinator_reserved_bytes: int
    artifact_reserved_entries: int
    artifact_reserved_bytes: int
    per_artifact_max_bytes: int
    idle_expires_at: float
    expires_at: float
    expires_at_epoch_ms: int
    schema: str = MANAGED_SEQUENCE_START_RESOURCE_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_START_RESOURCE_REQUEST_SCHEMA:
            raise ManagedSequenceServiceError("start_resource_request_schema", 500)
        _identifier(self.parent_sequence_id, "resource_parent_sequence_id")
        _fingerprint(
            self.parent_authorization_fingerprint,
            "resource_parent_authorization_fingerprint",
        )
        if type(self.segment_count) is not int or not 1 <= self.segment_count <= 15:
            raise ManagedSequenceServiceError("start_resource_segment_count", 500)
        if (
            type(self.coordinator_reserved_rows) is not int
            or not 1 <= self.coordinator_reserved_rows <= MAX_MANAGED_SEQUENCE_MUTATION_ROWS
            or type(self.coordinator_reserved_bytes) is not int
            or self.coordinator_reserved_bytes
            != self.coordinator_reserved_rows * MAX_COORDINATOR_RESPONSE_BYTES
        ):
            raise ManagedSequenceServiceError("start_resource_coordinator_capacity", 500)
        if (
            type(self.artifact_reserved_entries) is not int
            or self.artifact_reserved_entries != self.segment_count
            or type(self.per_artifact_max_bytes) is not int
            or self.per_artifact_max_bytes < 1
            or type(self.artifact_reserved_bytes) is not int
            or self.artifact_reserved_bytes != self.per_artifact_max_bytes * self.segment_count
        ):
            raise ManagedSequenceServiceError("start_resource_artifact_capacity", 500)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in (self.idle_expires_at, self.expires_at)
        ) or float(self.idle_expires_at) >= float(self.expires_at):
            raise ManagedSequenceServiceError("start_resource_expiry", 500)
        if (
            type(self.expires_at_epoch_ms) is not int
            or not 1 <= self.expires_at_epoch_ms <= 2**53 - 1
        ):
            raise ManagedSequenceServiceError("start_resource_epoch_expiry", 500)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "parent_sequence_id": self.parent_sequence_id,
            "parent_authorization_fingerprint": self.parent_authorization_fingerprint,
            "segment_count": self.segment_count,
            "coordinator_reserved_rows": self.coordinator_reserved_rows,
            "coordinator_reserved_bytes": self.coordinator_reserved_bytes,
            "artifact_reserved_entries": self.artifact_reserved_entries,
            "artifact_reserved_bytes": self.artifact_reserved_bytes,
            "per_artifact_max_bytes": self.per_artifact_max_bytes,
            "idle_expires_at": binary64_token(self.idle_expires_at),
            "expires_at": binary64_token(self.expires_at),
            "expires_at_epoch_ms": self.expires_at_epoch_ms,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


@dataclass(frozen=True, slots=True)
class ManagedSequenceStartResourceClaimV1:
    request_fingerprint: str
    coordinator_reservation_fingerprint: str
    child_slot_reservation_fingerprint: str
    artifact_reservation_fingerprint: str
    consume: Callable[[str, int], None] = field(repr=False, compare=False)
    release: Callable[[], None] = field(repr=False, compare=False)
    schema: str = MANAGED_SEQUENCE_START_RESOURCE_CLAIM_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_START_RESOURCE_CLAIM_SCHEMA:
            raise ManagedSequenceServiceError("start_resource_claim_schema", 500)
        for value, name in (
            (self.request_fingerprint, "start_resource_request_fingerprint"),
            (
                self.coordinator_reservation_fingerprint,
                "coordinator_reservation_fingerprint",
            ),
            (
                self.child_slot_reservation_fingerprint,
                "child_slot_reservation_fingerprint",
            ),
            (
                self.artifact_reservation_fingerprint,
                "artifact_reservation_fingerprint",
            ),
        ):
            _fingerprint(value, name)
        if not callable(self.consume):
            raise ManagedSequenceServiceError("start_resource_claim_consume", 500)
        if not callable(self.release):
            raise ManagedSequenceServiceError("start_resource_claim_release", 500)


@dataclass(frozen=True, slots=True)
class ManagedSegmentMaterializationClaimV1:
    context_workspace_handle: str
    segment_id: str
    materialization_receipt_fingerprint: str
    release: Callable[[], None] = field(repr=False, compare=False)
    schema: str = MANAGED_SEGMENT_MATERIALIZATION_CLAIM_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEGMENT_MATERIALIZATION_CLAIM_SCHEMA:
            raise ManagedSequenceServiceError("materialization_claim_schema", 500)
        if (
            type(self.context_workspace_handle) is not str
            or _CONTEXT_HANDLE.fullmatch(self.context_workspace_handle) is None
        ):
            raise ManagedSequenceServiceError("materialization_claim_handle", 500)
        _identifier(self.segment_id, "materialization_claim_segment_id")
        _fingerprint(
            self.materialization_receipt_fingerprint,
            "materialization_claim_receipt_fingerprint",
        )
        if not callable(self.release):
            raise ManagedSequenceServiceError("materialization_claim_release", 500)


@dataclass(frozen=True, slots=True)
class ManagedSegmentMaterializationClaimV2:
    context_workspace_handle: str
    segment_id: str
    materialization_receipt_fingerprint: str
    receipt: SegmentContextMaterializationReceiptV1 = field(repr=False)
    materialization: CanonicalProductionMaterializationSnapshot = field(repr=False)
    release: Callable[[], None] = field(repr=False, compare=False)
    schema: str = MANAGED_SEGMENT_MATERIALIZATION_CLAIM_V2_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEGMENT_MATERIALIZATION_CLAIM_V2_SCHEMA:
            raise ManagedSequenceServiceError("materialization_claim_schema", 500)
        if (
            type(self.context_workspace_handle) is not str
            or _CONTEXT_HANDLE.fullmatch(self.context_workspace_handle) is None
        ):
            raise ManagedSequenceServiceError("materialization_claim_handle", 500)
        _identifier(self.segment_id, "materialization_claim_segment_id")
        _fingerprint(
            self.materialization_receipt_fingerprint,
            "materialization_claim_receipt_fingerprint",
        )
        if (
            type(self.receipt) is not SegmentContextMaterializationReceiptV1
            or self.receipt.segment_id != self.segment_id
            or self.receipt.fingerprint != self.materialization_receipt_fingerprint
            or type(self.materialization) is not CanonicalProductionMaterializationSnapshot
            or self.materialization.context_workspace_handle != self.context_workspace_handle
        ):
            raise ManagedSequenceServiceError("materialization_claim_drift", 500)
        if not callable(self.release):
            raise ManagedSequenceServiceError("materialization_claim_release", 500)

    def assert_current(self) -> None:
        try:
            self.materialization.assert_current()
        except Exception as exc:
            raise ManagedSequenceServiceError("prepared_context_unavailable", 409) from exc


@dataclass(frozen=True, slots=True)
class ManagedPreparedContextV1:
    parent_sequence_id: str
    parent_revision: int
    authorization_fingerprint: str
    segment_id: str
    eligible_execution_fingerprint: str
    materialization_receipt_fingerprint: str
    context_workspace_handle: str
    context_report_id: str
    context_report_revision: int
    context_report_fingerprint: str
    canonical_prompt: str = field(repr=False)
    canonical_prompt_fingerprint: str
    task_mode: TaskMode
    duration_milliseconds: int
    frame_count: int
    profile: ProfileIdentity
    profile_fingerprint: str
    ordered_references: tuple[ReferenceAsset, ...]
    reference_registry_fingerprint: str
    native_binding_fingerprint: str
    canonical_lowering: ProductionCanonicalLoweringV1 | None
    schema: str = MANAGED_PREPARED_CONTEXT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_PREPARED_CONTEXT_SCHEMA:
            raise ManagedSequenceServiceError("prepared_context_schema", 500)
        _identifier(self.parent_sequence_id, "parent_sequence_id")
        _revision(self.parent_revision)
        _identifier(self.segment_id, "segment_id")
        _identifier(self.context_report_id, "context_report_id")
        _revision(self.context_report_revision)
        for value, name in (
            (self.authorization_fingerprint, "authorization_fingerprint"),
            (self.eligible_execution_fingerprint, "eligible_execution_fingerprint"),
            (
                self.materialization_receipt_fingerprint,
                "materialization_receipt_fingerprint",
            ),
            (self.context_report_fingerprint, "context_report_fingerprint"),
            (self.canonical_prompt_fingerprint, "canonical_prompt_fingerprint"),
            (self.profile_fingerprint, "profile_fingerprint"),
            (self.reference_registry_fingerprint, "reference_registry_fingerprint"),
            (self.native_binding_fingerprint, "native_binding_fingerprint"),
        ):
            _fingerprint(value, name)
        if (
            type(self.context_workspace_handle) is not str
            or _CONTEXT_HANDLE.fullmatch(self.context_workspace_handle) is None
        ):
            raise ManagedSequenceServiceError("invalid_context_workspace_handle", 500)
        if (
            type(self.canonical_prompt) is not str
            or not self.canonical_prompt
            or len(self.canonical_prompt) > MAX_PROMPT_LENGTH
            or canonical_fingerprint(self.canonical_prompt) != self.canonical_prompt_fingerprint
        ):
            raise ManagedSequenceServiceError("prepared_context_prompt", 500)
        if type(self.task_mode) is not TaskMode or type(self.profile) is not ProfileIdentity:
            raise ManagedSequenceServiceError("prepared_context_contract", 500)
        if (
            self.canonical_lowering is not None
            and type(self.canonical_lowering) is not ProductionCanonicalLoweringV1
        ):
            raise ManagedSequenceServiceError("prepared_context_canonical_lowering", 500)
        if (
            type(self.duration_milliseconds) is not int
            or not 4_000 <= self.duration_milliseconds <= 15_000
            or type(self.frame_count) is not int
            or not 1 <= self.frame_count <= 512
        ):
            raise ManagedSequenceServiceError("prepared_context_duration", 500)
        if SegmentDuration(self.duration_milliseconds).frame_count != self.frame_count:
            raise ManagedSequenceServiceError("prepared_context_duration", 500)
        if canonical_fingerprint(self.profile.to_wire()) != self.profile_fingerprint:
            raise ManagedSequenceServiceError("prepared_context_contract", 500)
        if type(self.ordered_references) is not tuple:
            raise ManagedSequenceServiceError("prepared_context_references", 500)
        try:
            registry = build_reference_registry(self.ordered_references)
        except Exception as exc:
            raise ManagedSequenceServiceError("prepared_context_references", 500) from exc
        if canonical_fingerprint(registry.to_wire()) != self.reference_registry_fingerprint:
            raise ManagedSequenceServiceError("prepared_context_references", 500)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "parent_sequence_id": self.parent_sequence_id,
            "parent_revision": self.parent_revision,
            "authorization_fingerprint": self.authorization_fingerprint,
            "segment_id": self.segment_id,
            "eligible_execution_fingerprint": self.eligible_execution_fingerprint,
            "materialization_receipt_fingerprint": (self.materialization_receipt_fingerprint),
            "context_workspace_handle": self.context_workspace_handle,
            "context_report_id": self.context_report_id,
            "context_report_revision": self.context_report_revision,
            "context_report_fingerprint": self.context_report_fingerprint,
            "canonical_prompt": self.canonical_prompt,
            "canonical_prompt_fingerprint": self.canonical_prompt_fingerprint,
            "task_mode": self.task_mode.value,
            "duration_milliseconds": self.duration_milliseconds,
            "frame_count": self.frame_count,
            "profile": self.profile.to_wire(),
            "profile_fingerprint": self.profile_fingerprint,
            "ordered_references": [row.to_wire() for row in self.ordered_references],
            "reference_registry_fingerprint": self.reference_registry_fingerprint,
            "native_binding_fingerprint": self.native_binding_fingerprint,
            "canonical_lowering": (
                None if self.canonical_lowering is None else self.canonical_lowering.to_wire()
            ),
        }


@dataclass(frozen=True, slots=True)
class ManagedChildBindingRequestV1:
    context_workspace_handle: str
    execution: EligibleSegmentExecutionV1
    graph_fingerprint: str
    compiled_prompt_fingerprint: str
    correlation: ManagedChildCorrelationV1
    observation: ManagedPreparedGraphObservationV1
    production_workspace_handle: str | None = field(default=None, kw_only=True)
    source_authority: object | None = field(default=None, repr=False, compare=False, kw_only=True)
    schema: str = MANAGED_CHILD_BINDING_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_CHILD_BINDING_REQUEST_SCHEMA:
            raise ManagedSequenceServiceError("child_binding_request_schema", 500)
        if (
            type(self.context_workspace_handle) is not str
            or _CONTEXT_HANDLE.fullmatch(self.context_workspace_handle) is None
        ):
            raise ManagedSequenceServiceError("child_binding_context_handle", 500)
        if type(self.execution) is not EligibleSegmentExecutionV1:
            raise ManagedSequenceServiceError("child_binding_execution", 500)
        _fingerprint(self.graph_fingerprint, "child_binding_graph_fingerprint")
        _fingerprint(
            self.compiled_prompt_fingerprint,
            "child_binding_compiled_prompt_fingerprint",
        )
        if type(self.correlation) is not ManagedChildCorrelationV1:
            raise ManagedSequenceServiceError("child_binding_correlation", 500)
        if type(self.observation) is not ManagedPreparedGraphObservationV1:
            raise ManagedSequenceServiceError("child_binding_observation", 500)
        if (
            self.graph_fingerprint != self.observation.graph_fingerprint
            or self.compiled_prompt_fingerprint != self.observation.compiled_prompt_fingerprint
        ):
            raise ManagedSequenceServiceError("child_binding_observation_drift", 500)


@dataclass(frozen=True, slots=True)
class ManagedSequenceChildAuthorityV1:
    run_handle: str
    state_fingerprint: str
    job_id: str
    transaction_id: str
    schema: str = MANAGED_SEQUENCE_CHILD_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_SEQUENCE_CHILD_AUTHORITY_SCHEMA:
            raise ManagedSequenceServiceError("child_authority_schema", 500)
        if type(self.run_handle) is not str or _RUN_HANDLE.fullmatch(self.run_handle) is None:
            raise ManagedSequenceServiceError("child_authority_handle", 500)
        _fingerprint(self.state_fingerprint, "child_authority_state")
        _identifier(self.job_id, "child_authority_job_id")
        _identifier(self.transaction_id, "child_authority_transaction_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "run_handle": self.run_handle,
            "state_fingerprint": self.state_fingerprint,
            "job_id": self.job_id,
            "transaction_id": self.transaction_id,
        }


@dataclass(frozen=True, slots=True)
class ManagedChildBindingClaimV1:
    child_run_handle: str
    child_state_fingerprint: str
    child_job_id: str
    child_transaction_id: str
    rollback: Callable[[], None] = field(repr=False, compare=False)
    schema: str = MANAGED_CHILD_BINDING_CLAIM_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANAGED_CHILD_BINDING_CLAIM_SCHEMA:
            raise ManagedSequenceServiceError("child_binding_claim_schema", 500)
        if (
            type(self.child_run_handle) is not str
            or _RUN_HANDLE.fullmatch(self.child_run_handle) is None
        ):
            raise ManagedSequenceServiceError("child_binding_claim_handle", 500)
        _fingerprint(self.child_state_fingerprint, "child_binding_claim_state")
        _identifier(self.child_job_id, "child_binding_claim_job_id")
        _identifier(self.child_transaction_id, "child_binding_claim_transaction_id")
        if not callable(self.rollback):
            raise ManagedSequenceServiceError("child_binding_claim_rollback", 500)

    def child_authority(self) -> ManagedSequenceChildAuthorityV1:
        return ManagedSequenceChildAuthorityV1(
            run_handle=self.child_run_handle,
            state_fingerprint=self.child_state_fingerprint,
            job_id=self.child_job_id,
            transaction_id=self.child_transaction_id,
        )


@dataclass(frozen=True, slots=True)
class ManagedSequenceMutationResultV1:
    parent_sequence_id: str
    authorization_fingerprint: str
    read_authority_fingerprint: str
    state: ManagedSequenceStateV1
    revision: int
    execution: EligibleSegmentExecutionV1 | None = None
    child_authority: ManagedSequenceChildAuthorityV1 | None = None
    context_workspace_handle: str | None = None
    parent_expires_at_epoch_ms: int | None = None
    replayed: bool = False
    schema: str = MANAGED_SEQUENCE_RESULT_SCHEMA

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "parent_sequence_id": self.parent_sequence_id,
            "authorization_fingerprint": self.authorization_fingerprint,
            "read_authority_fingerprint": self.read_authority_fingerprint,
            "state": self.state.value,
            "revision": self.revision,
            "execution": None if self.execution is None else self.execution.to_wire(),
            "execution_fingerprint": (
                None if self.execution is None else self.execution.fingerprint
            ),
            "child_authority": (
                None if self.child_authority is None else self.child_authority.to_wire()
            ),
            "context_workspace_handle": self.context_workspace_handle,
            "parent_expires_at_epoch_ms": self.parent_expires_at_epoch_ms,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class ManagedSequenceReadResultV1:
    status: int
    etag: str
    projection: ManagedSequenceCurrentProjectionV1 | None
    schema: str = MANAGED_SEQUENCE_READ_RESULT_SCHEMA


@dataclass(slots=True)
class _HeldMaterialization:
    claim: ManagedSegmentMaterializationClaimV1 | ManagedSegmentMaterializationClaimV2
    execution: EligibleSegmentExecutionV1 | None
    released: bool = False

    def release_once(self) -> None:
        if not self.released:
            # CRITICAL: a cleanup callback may fail after its Context authority is already
            # terminal. Mark this holder first so later expiry/cancel paths cannot call the same
            # release callback twice and replace the original cleanup failure with false drift.
            self.released = True
            self.claim.release()


@dataclass(slots=True)
class _HeldStartResources:
    claim: ManagedSequenceStartResourceClaimV1
    released: bool = False
    consumed_requests: dict[str, int] = field(default_factory=dict)
    consumed_candidates: dict[str, int] = field(default_factory=dict)

    @property
    def consumed_rows(self) -> int:
        return 1 + sum(self.consumed_requests.values())

    def consume_rows(self, request_id: str, rows: int) -> None:
        if self.released:
            raise ManagedSequenceServiceError("start_resource_claim_released", 500)
        if type(rows) is not int or not 1 <= rows <= MAX_MANAGED_SEQUENCE_MUTATION_ROWS:
            raise ManagedSequenceServiceError("start_resource_consumption", 500)
        self.claim.consume(request_id, rows)
        self.consumed_requests[request_id] = rows

    def release_once(self) -> None:
        if not self.released:
            self.claim.release()
            self.released = True


@dataclass(slots=True)
class _ServiceEntry:
    source_authority: object = field(repr=False)
    sequence: ManagedSequenceV1
    read_authority_fingerprint: str
    production_workspace_handle: str | None = None
    held_materialization: _HeldMaterialization | None = None
    bound_child_binding: ManagedChildBindingClaimV1 | None = None
    pending_child_compensation: bool = False
    bound_child_cleanup_completed: bool = False
    start_resources: _HeldStartResources | None = None
    parent_expires_at_epoch_ms: int | None = None


@dataclass(frozen=True, slots=True)
class _ReplayRow:
    digest: str
    result: ManagedSequenceMutationResultV1


class ManagedSequenceLeaseTransitionService:
    """The sole owner of M26 parent/child expiry transitions."""

    def __init__(self, parent: ManagedSequenceService) -> None:
        self._parent = parent
        self._child_transition: (
            Callable[[ManagedSequenceV1, ManagedSequenceV1, float], None] | None
        ) = None

    def bind_child_transition(
        self,
        transition: Callable[[ManagedSequenceV1, ManagedSequenceV1, float], None],
    ) -> None:
        if not callable(transition):
            raise ValueError("managed sequence lease child transition is invalid")
        if self._child_transition is not None and self._child_transition is not transition:
            raise ValueError("managed sequence lease child transition is already bound")
        self._child_transition = transition

    def apply(
        self,
        parent_sequence_id: str,
        authorization_fingerprint: str,
        *,
        expected_revision: int,
        now: float | None = None,
    ) -> ManagedSequenceMutationResultV1:
        return self._parent._expire_lease_transaction(
            parent_sequence_id,
            authorization_fingerprint,
            expected_revision=expected_revision,
            now=now,
            child_transition=self._child_transition,
        )

    def apply_pending(self, parent_sequence_id: str, now: float) -> None:
        self._parent._expire_pending_lease_transaction(
            parent_sequence_id,
            now=now,
            child_transition=self._child_transition,
        )


def build_managed_sequence_authorization(
    authority: ProductionAutomaticPlanAuthorityV2,
    *,
    sequence_id: str,
    generation_plan_fingerprint: str,
    compiler_fingerprint: str,
    host_capability_fingerprint: str,
) -> ManagedSequenceAuthorizationV1:
    """Translate exactly one retained M26-03 authority into a content-free parent."""

    if type(authority) is not ProductionAutomaticPlanAuthorityV2:
        raise ManagedSequenceServiceError("automatic_plan_semantic_authority_stale", 409)
    unsupported_holds = tuple(
        hold
        for hold in authority.start_hold_codes
        if hold is not ProposalBlockerCodeV1.MANAGED_EXECUTION_QUALIFICATION_PENDING
    )
    if unsupported_holds:
        raise ManagedSequenceServiceError("production_start_hold", 409)
    manifests = derive_segment_manifests(authority.workspace)
    cut_by_successor = {row.successor_segment_id: row for row in authority.cut_boundary_receipts}
    segments: list[ManagedSequenceSegmentAuthorizationV1] = []
    for index, (manifest, proposal, receipt) in enumerate(
        zip(
            manifests,
            authority.proposal.segments,
            authority.materialization_receipts,
            strict=True,
        )
    ):
        if receipt.capability_fingerprint != host_capability_fingerprint:
            raise ManagedSequenceServiceError("authorization_drift", 409)
        predecessor = None if index == 0 else authority.reconstruction_order[index - 1]
        cut = None if predecessor is None else cut_by_successor.get(proposal.segment_id)
        if predecessor is not None and (cut is None or cut.predecessor_segment_id != predecessor):
            raise ManagedSequenceServiceError("production_cut_drift", 409)
        segments.append(
            ManagedSequenceSegmentAuthorizationV1(
                segment_id=proposal.segment_id,
                ordinal=proposal.ordinal,
                task_mode=proposal.task_mode,
                duration_milliseconds=receipt.duration_milliseconds,
                frame_count=receipt.frame_count,
                manifest_fingerprint=manifest.fingerprint,
                materialization_receipt_fingerprint=receipt.fingerprint,
                local_prompt_fingerprint=receipt.local_prompt_fingerprint,
                intent_graph_fingerprint=receipt.intent_graph_fingerprint,
                profile_fingerprint=receipt.profile_fingerprint,
                capability_fingerprint=receipt.capability_fingerprint,
                predecessor_segment_id=predecessor,
                predecessor_cut_receipt_fingerprint=(None if cut is None else cut.fingerprint),
            )
        )
    return ManagedSequenceAuthorizationV1(
        sequence_id=sequence_id,
        workspace_id=authority.workspace.workspace_id,
        workspace_revision=authority.workspace.revision,
        workspace_fingerprint=authority.workspace.fingerprint,
        production_plan_fingerprint=authority.fingerprint,
        proposal_fingerprint=authority.proposal.fingerprint,
        generation_plan_fingerprint=_fingerprint(
            generation_plan_fingerprint, "generation_plan_fingerprint"
        ),
        compiler_fingerprint=_fingerprint(compiler_fingerprint, "compiler_fingerprint"),
        host_capability_fingerprint=_fingerprint(
            host_capability_fingerprint, "host_capability_fingerprint"
        ),
        segments=tuple(segments),
    )


class ManagedModeQualificationRegistry:
    """One process-local current supplied-host qualification; never browser writable."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        producer: object | None = None,
    ) -> None:
        if not callable(clock):
            raise ValueError("qualification registry clock is invalid")
        self._clock = clock
        self._current: ManagedModeQualificationV1 | ManagedModeQualificationV2 | None = None
        self._lock = threading.RLock()
        self._producer = producer
        self._readiness_lock = threading.Lock()
        self._selectors: dict[str, object] | None = None
        self._readiness_requests: dict[str, tuple[str, str | None, float]] = {}

    def readiness(self, action: dict[str, object]) -> dict[str, object]:
        from .comfyui_production_workspace import ProductionWorkbenchError

        # CRITICAL: reserve the request before any observation. Otherwise a simultaneous replay
        # can publish a held result over the first caller's success or reacquire its deadline.
        if not self._readiness_lock.acquire(blocking=False):
            raise ProductionWorkbenchError("qualification_busy", 423)
        try:
            return self._readiness(action)
        finally:
            self._readiness_lock.release()

    def _readiness(self, action: dict[str, object]) -> dict[str, object]:
        from .comfyui_production_workspace import ProductionWorkbenchError
        from .managed_mode_qualification import ManagedQualificationProducer

        if type(self._producer) is not ManagedQualificationProducer:
            raise ProductionWorkbenchError("qualification_producer_unavailable", 503)
        payload = cast(dict[str, object], action["payload"])
        request_id = str(action["request_id"])
        digest = canonical_fingerprint(action)
        selectors = {
            key: value for key, value in payload.items() if key != "qualification_fingerprint"
        }
        with self._lock:
            now = self._now()
            self._readiness_requests = {
                key: value for key, value in self._readiness_requests.items() if value[2] > now
            }
            replay = self._readiness_requests.get(request_id)
            if replay is not None and replay[0] != digest:
                raise ProductionWorkbenchError("request_id_conflict", 409)
            if replay is None and len(self._readiness_requests) >= 256:
                raise ProductionWorkbenchError("qualification_request_capacity", 413)
            current = self._current
        requested = payload.get("qualification_fingerprint")
        if replay is not None:
            requested = replay[1]
        result = None
        reason = "qualification_unavailable"
        if requested is not None:
            if selectors == self._selectors:
                result = self.claim(str(requested))
        elif replay is None and action["action"] == "prepare_managed_readiness":
            try:
                result = self._producer.observe(
                    workspace_handle=str(selectors["workspace_handle"]),
                    expected_workspace_revision=cast(int, selectors["expected_workspace_revision"]),
                    expected_workspace_fingerprint=str(selectors["expected_workspace_fingerprint"]),
                    expected_plan_fingerprint=str(selectors["expected_plan_fingerprint"]),
                )
                with self._producer.guard_current(
                    result,
                    workspace_handle=str(selectors["workspace_handle"]),
                    expected_workspace_revision=cast(int, selectors["expected_workspace_revision"]),
                    expected_workspace_fingerprint=str(selectors["expected_workspace_fingerprint"]),
                    expected_plan_fingerprint=str(selectors["expected_plan_fingerprint"]),
                ):
                    with self._lock:
                        if self._current is not current:
                            raise ProductionWorkbenchError("qualification_changed", 409)
                        self._current = result
                        self._selectors = selectors
            except ProductionWorkbenchError as error:
                result = None
                reason = error.code
                with self._lock:
                    if self._current is current:
                        self._current = None
                        self._selectors = None
        with self._lock:
            if replay is None:
                self._readiness_requests[request_id] = (
                    digest,
                    None if result is None else result.fingerprint,
                    now + 900,
                )
        return {
            "schema": "h3.context.managed_readiness.v1",
            "request_id": request_id,
            "status": "held" if result is None else "ready",
            "reason": reason if result is None else "qualified",
            "qualification_fingerprint": None if result is None else result.fingerprint,
            "qualification": None if result is None else result.to_wire(),
        }

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ManagedSequenceServiceError("qualification_clock", 500)
        return float(value)

    def publish(self, qualification: ManagedModeQualificationV1) -> str:
        if type(qualification) is not ManagedModeQualificationV1:
            raise ManagedSequenceServiceError("qualification_type", 400)
        with self._lock:
            now = self._now()
            if qualification.observed_at > now:
                raise ManagedSequenceServiceError("qualification_observed_in_future", 409)
            if qualification.expires_at <= now:
                raise ManagedSequenceServiceError("qualification_expired", 409)
            current = self._current
            if current is not None:
                if current.fingerprint == qualification.fingerprint:
                    return current.fingerprint
                if current.expires_at > now and qualification.observed_at < current.observed_at:
                    raise ManagedSequenceServiceError("qualification_stale", 409)
            self._current = qualification
            return qualification.fingerprint

    @contextmanager
    def guard(self, qualification: ManagedModeQualificationV2) -> Iterator[None]:
        from .managed_mode_qualification import ManagedQualificationProducer

        with self._lock:
            selectors = self._selectors
            if self._current is not qualification:
                raise ManagedSequenceServiceError("qualification_claim_drift", 409)
        if type(self._producer) is not ManagedQualificationProducer or selectors is None:
            raise ManagedSequenceServiceError("qualification_claim_drift", 409)
        # The parent already holds its lock. Order is parent -> Production -> qualification;
        # no qualification operation may call into a parent while holding either registry lock.
        try:
            with self._producer.guard_current(
                qualification,
                workspace_handle=str(selectors["workspace_handle"]),
                expected_workspace_revision=cast(int, selectors["expected_workspace_revision"]),
                expected_workspace_fingerprint=str(selectors["expected_workspace_fingerprint"]),
                expected_plan_fingerprint=str(selectors["expected_plan_fingerprint"]),
            ):
                with self._lock:
                    if self._current is not qualification:
                        raise ManagedSequenceServiceError("qualification_claim_drift", 409)
                    yield
        except ManagedSequenceServiceError:
            raise
        except Exception:
            raise ManagedSequenceServiceError("qualification_claim_drift", 409) from None

    def claim(
        self, fingerprint: str
    ) -> ManagedModeQualificationV1 | ManagedModeQualificationV2 | None:
        _fingerprint(fingerprint, "qualification_fingerprint")
        with self._lock:
            now = self._now()
            current = self._current
            if current is None:
                return None
            if current.expires_at <= now:
                self._current = None
                return None
            if current.fingerprint != fingerprint:
                return None
            selectors = self._selectors
        if type(current) is ManagedModeQualificationV2:
            from .managed_mode_qualification import ManagedQualificationProducer

            if type(self._producer) is not ManagedQualificationProducer or selectors is None:
                return None
            try:
                self._producer.observe(
                    workspace_handle=str(selectors["workspace_handle"]),
                    expected_workspace_revision=cast(int, selectors["expected_workspace_revision"]),
                    expected_workspace_fingerprint=str(selectors["expected_workspace_fingerprint"]),
                    expected_plan_fingerprint=str(selectors["expected_plan_fingerprint"]),
                    previous=current,
                )
            except Exception:
                with self._lock:
                    if self._current is current:
                        self._current = None
                        self._selectors = None
                return None
        elif self._producer is not None:
            return None
        with self._lock:
            return (
                current if self._current is current and current.expires_at > self._now() else None
            )


class ManagedSequenceProductionRegistry(Protocol):
    def claim_automatic_plan_authority(
        self,
        handle: str,
        *,
        expected_workspace_revision: int,
        expected_workspace_fingerprint: str,
        expected_plan_fingerprint: str,
    ) -> object: ...


class ManagedSequenceService:
    """Single-parent service; no method owns a host write or queue transport."""

    def __init__(
        self,
        *,
        production_registry: ManagedSequenceProductionRegistry,
        materialize_segment: Callable[
            [object, str],
            ManagedSegmentMaterializationClaimV1 | ManagedSegmentMaterializationClaimV2,
        ]
        | None = None,
        bind_child: Callable[[ManagedChildBindingRequestV1], ManagedChildBindingClaimV1]
        | None = None,
        reserve_start_resources: Callable[
            [ManagedSequenceStartResourceRequestV1],
            ManagedSequenceStartResourceClaimV1,
        ]
        | None = None,
        authorization_builder: Callable[..., ManagedSequenceAuthorizationV1] = (
            build_managed_sequence_authorization
        ),
        # CRITICAL: token_urlsafe may begin with '-' or '_', while identifiers require an
        # alphanumeric first character. Prefix the default without stripping random token bits.
        token_factory: Callable[[], str] = lambda: f"read.{secrets.token_urlsafe(40)}",
        clock: Callable[[], float] = time.monotonic,
        epoch_clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        qualification_guard: Callable[[ManagedModeQualificationV2], AbstractContextManager[None]]
        | None = None,
        publish_completion: Callable[[object, str, ManagedSequenceV1], None] | None = None,
    ) -> None:
        claim = getattr(production_registry, "claim_automatic_plan_authority", None)
        if not callable(claim):
            raise ValueError("production registry is invalid")
        if (
            not callable(authorization_builder)
            or not callable(token_factory)
            or not callable(clock)
            or not callable(epoch_clock_ms)
            or (reserve_start_resources is not None and not callable(reserve_start_resources))
        ):
            raise ValueError("managed sequence service dependency is invalid")
        self._production_registry = production_registry
        self._materialize_segment = materialize_segment
        self._bind_child = bind_child
        self._reserve_start_resources = reserve_start_resources
        self._authorization_builder = authorization_builder
        self._token_factory = token_factory
        self._clock = clock
        self._epoch_clock_ms = epoch_clock_ms
        self._qualification_guard = qualification_guard
        self._publish_completion = publish_completion
        self._entry: _ServiceEntry | None = None
        self._ledger: OrderedDict[str, _ReplayRow] = OrderedDict()
        self._lock = threading.RLock()
        self._lease_transition_owner = ManagedSequenceLeaseTransitionService(self)

    def _new_parent_id(self) -> str:
        value = f"managed.{self._token_factory()}"
        return _identifier(value, "generated_parent_sequence_id")

    def _epoch_now_ms(self) -> int:
        value = self._epoch_clock_ms()
        if type(value) is not int or not 0 <= value <= (2**53 - 1):
            raise ManagedSequenceServiceError("epoch_clock", 500)
        return value

    def _read_authority(self, sequence: ManagedSequenceV1) -> str:
        return canonical_fingerprint(
            {
                "schema": "h3.context.managed_sequence_read_authority.v1",
                "parent_sequence_id": sequence.authorization.sequence_id,
                "authorization_fingerprint": sequence.authorization.fingerprint,
                "nonce": _identifier(self._token_factory(), "read_authority_nonce"),
            }
        )

    def _request_digest(self, action: str, wire: dict[str, object]) -> str:
        return canonical_fingerprint(
            {
                "schema": MANAGED_SEQUENCE_ACTION_SCHEMA,
                "action": action,
                "payload": wire,
            }
        )

    def _replay(self, request_id: str, digest: str) -> ManagedSequenceMutationResultV1 | None:
        row = self._ledger.get(request_id)
        if row is None:
            return None
        if row.digest != digest:
            raise ManagedSequenceServiceError("request_id_conflict", 409)
        self._ledger.move_to_end(request_id)
        return replace(row.result, replayed=True)

    def _publish(
        self,
        request_id: str,
        digest: str,
        result: ManagedSequenceMutationResultV1,
    ) -> None:
        self._validate_publication(result)
        self._ledger[request_id] = _ReplayRow(digest, result)

    def _validate_publication(self, result: ManagedSequenceMutationResultV1) -> None:
        if len(canonical_bytes(result.to_wire())) > MAX_MANAGED_PREPARED_CONTEXT_BYTES:
            raise ManagedSequenceServiceError("response_size", 500)
        if len(self._ledger) >= MAX_MANAGED_SEQUENCE_MUTATION_ROWS:
            raise ManagedSequenceServiceError("request_capacity", 503)

    @staticmethod
    def _result(
        entry: _ServiceEntry,
        *,
        execution: EligibleSegmentExecutionV1 | None = None,
        child_authority: ManagedSequenceChildAuthorityV1 | None = None,
        context_workspace_handle: str | None = None,
    ) -> ManagedSequenceMutationResultV1:
        sequence = entry.sequence
        return ManagedSequenceMutationResultV1(
            parent_sequence_id=sequence.authorization.sequence_id,
            authorization_fingerprint=sequence.authorization.fingerprint,
            read_authority_fingerprint=entry.read_authority_fingerprint,
            state=sequence.state,
            revision=sequence.revision,
            execution=execution,
            child_authority=child_authority,
            context_workspace_handle=context_workspace_handle,
            parent_expires_at_epoch_ms=entry.parent_expires_at_epoch_ms,
        )

    def _current(
        self,
        parent_sequence_id: str,
        authorization_fingerprint: str,
        expected_revision: int,
    ) -> _ServiceEntry:
        entry = self._entry
        if entry is None or entry.sequence.authorization.sequence_id != parent_sequence_id:
            raise ManagedSequenceServiceError("parent_sequence_unavailable", 404)
        if entry.sequence.authorization.fingerprint != authorization_fingerprint:
            raise ManagedSequenceServiceError("authorization_drift", 409)
        if entry.sequence.revision != expected_revision:
            raise ManagedSequenceServiceError("stale_parent", 409)
        return entry

    @staticmethod
    def _translate(exc: ManagedSequenceError) -> ManagedSequenceServiceError:
        status = 422 if exc.code.startswith("invalid_") else 409
        return ManagedSequenceServiceError(exc.code, status)

    def _expire_before_mutation(self, entry: _ServiceEntry) -> None:
        if entry.sequence.lease is None:
            return
        observed = self._clock()
        try:
            candidate = expire_managed_sequence_lease(
                entry.sequence,
                expected_revision=entry.sequence.revision,
                now=observed,
            )
        except ManagedSequenceError as exc:
            if exc.code == "lease_active":
                return
            raise self._translate(exc) from exc
        if candidate is entry.sequence:
            return
        # CRITICAL: the lease owner publishes the truthful pause before this attempted mutation is
        # refused. Allowing the stale action to continue would create a host effect after expiry.
        self._lease_transition_owner.apply(
            entry.sequence.authorization.sequence_id,
            entry.sequence.authorization.fingerprint,
            expected_revision=entry.sequence.revision,
            now=observed,
        )
        raise ManagedSequenceServiceError("lease_expired", 409)

    @staticmethod
    def _consumed_rows(sequence: ManagedSequenceV1) -> int:
        reservation = sequence.ledger_reservation
        if reservation is None:
            return 0
        return reservation.consumed_rows + reservation.recovery_rows_consumed

    def _consume_candidate_rows(
        self,
        entry: _ServiceEntry,
        *,
        request_id: str,
        candidate: ManagedSequenceV1,
    ) -> None:
        resources = entry.start_resources
        if resources is None:
            if (
                entry.sequence.state is ManagedSequenceStateV1.AUTHORIZED
                and candidate.state is ManagedSequenceStateV1.CANCELLED
                and entry.sequence.ledger_reservation is None
                and candidate.ledger_reservation is None
                and candidate.artifact_reservation is None
                and candidate.lease is None
            ):
                # CRITICAL: an authorization exists before start resources do. Requiring a start
                # claim after admission refusal makes that host-effect-free parent impossible to
                # cancel and permanently blocks the process-local one-parent slot.
                return
            raise ManagedSequenceServiceError("start_resource_contract_missing", 500)
        logical_delta = self._consumed_rows(candidate) - self._consumed_rows(entry.sequence)
        # IMPORTANT: every retained mutation response occupies one physical coordinator row even
        # when the pure transition changes only attachment/ownership state. Terminal closure is the
        # deliberate exception: its logical delta is two because it includes the child release row.
        rows = max(1, logical_delta)
        candidate_fingerprint = candidate.fingerprint
        committed_rows = resources.consumed_candidates.get(candidate_fingerprint)
        if committed_rows is not None:
            # CRITICAL: cleanup/publication can fail after reservation consumption. A fresh
            # transport ID retrying the exact unpublished candidate reuses that commitment;
            # charging again lets repeated release failures consume the only cancellation row.
            if committed_rows != rows:
                raise ManagedSequenceServiceError("resource_consumption_conflict", 409)
            return
        ledger = candidate.ledger_reservation
        # CRITICAL: logical deltas omit some retained responses and failed-publication attempts.
        # Bound actual consumption, keeping one cancel row and two late-terminal/release rows
        # while a child is owned. Attachment churn must never spend those cleanup reservations.
        cleanup_rows = 2 if candidate.active_segment_id is not None else 0
        if any(slot.state is SequenceSlotStateV1.BOUND for slot in candidate.slots):
            cleanup_rows = 3  # Late submission acknowledgement plus terminal/release.
        cancel_rows = int(
            candidate.state
            not in {
                ManagedSequenceStateV1.CANCELLED,
                ManagedSequenceStateV1.SUCCEEDED,
            }
        )
        if cancel_rows and ledger is not None and candidate.retry_consumed:
            for slot in candidate.slots:
                if slot.attempt_epoch == 2 and slot.state in {
                    SequenceSlotStateV1.ELIGIBLE,
                    SequenceSlotStateV1.PREPARED,
                    SequenceSlotStateV1.BOUND,
                    SequenceSlotStateV1.SUBMITTED,
                    SequenceSlotStateV1.RUNNING,
                    SequenceSlotStateV1.ARTIFACT_VERIFIED,
                    SequenceSlotStateV1.UNKNOWN_OWNERSHIP,
                }:
                    # CRITICAL: core prep is prepaid by retry, but its distinct service response
                    # still consumes a physical row. Reserve the whole remaining retry and the
                    # detached client's resume before admitting more optional attachment traffic.
                    retry_rows = 7 - ledger.recovery_rows_consumed
                    retry_rows += int(slot.state is SequenceSlotStateV1.ELIGIBLE)
                    cleanup_rows = max(cleanup_rows, retry_rows)
            cleanup_rows += int(not candidate.client_attached)
        additional = 0 if request_id in resources.consumed_requests else rows
        if ledger is not None and (
            resources.consumed_rows + additional + cleanup_rows + cancel_rows > ledger.reserved_rows
        ):
            raise ManagedSequenceServiceError("recovery_capacity", 409)
        try:
            resources.consume_rows(request_id, rows)
            resources.consumed_candidates[candidate_fingerprint] = rows
        except ManagedSequenceServiceError:
            raise
        except Exception as exc:
            raise ManagedSequenceServiceError("start_resource_consumption_failed", 503) from exc

    def _apply_transition(
        self,
        action: str,
        authority: ManagedSequenceMutationAuthorityV1,
        wire: dict[str, object],
        transition: Callable[[ManagedSequenceV1], ManagedSequenceV1],
        *,
        release_prepared: bool = False,
    ) -> ManagedSequenceMutationResultV1:
        digest = self._request_digest(action, wire)
        with self._lock:
            replay = self._replay(authority.request_id, digest)
            if replay is not None:
                return replay
            entry = self._current(
                authority.parent_sequence_id,
                authority.authorization_fingerprint,
                authority.expected_revision,
            )
            self._expire_before_mutation(entry)
            try:
                candidate = transition(entry.sequence)
            except ManagedSequenceError as exc:
                raise self._translate(exc) from exc
            if candidate == entry.sequence:
                # IMPORTANT: same-ID replay is handled above. New IDs for no-op observations
                # must not create an unbounded response ledger or consume cancellation capacity.
                raise ManagedSequenceServiceError("mutation_no_change", 409)
            if entry.pending_child_compensation and action != "cancel_sequence":
                raise ManagedSequenceServiceError("child_binding_compensation_pending", 409)
            if entry.bound_child_cleanup_completed:
                raise ManagedSequenceServiceError("bound_child_cleanup_publication_pending", 409)
            result = self._result(replace(entry, sequence=candidate))
            self._validate_publication(result)
            self._consume_candidate_rows(
                entry,
                request_id=authority.request_id,
                candidate=candidate,
            )
            if (
                candidate.state is ManagedSequenceStateV1.SUCCEEDED
                and type(entry.source_authority) is ProductionAutomaticPlanAuthorityV2
            ):
                # CRITICAL: validate replay capacity first, publish exact owned outputs second,
                # and release execution reservations last. A cleanup retry must not publish twice.
                if self._publish_completion is None or entry.production_workspace_handle is None:
                    raise ManagedSequenceServiceError("production_publication_unavailable", 503)
                self._publish_completion(
                    entry.source_authority, entry.production_workspace_handle, candidate
                )
            if entry.pending_child_compensation:
                self._compensate_unpublished_child(entry)
            if release_prepared and entry.held_materialization is not None:
                try:
                    entry.held_materialization.release_once()
                except Exception as exc:
                    raise ManagedSequenceServiceError(
                        "prepared_context_release_failed", 503
                    ) from exc
                entry.held_materialization = None
            ledger = candidate.ledger_reservation
            artifact = candidate.artifact_reservation
            if (
                ledger is not None
                and artifact is not None
                and ledger.released
                and artifact.released
                and entry.start_resources is not None
            ):
                try:
                    entry.start_resources.release_once()
                except Exception as exc:
                    raise ManagedSequenceServiceError("start_resource_release_failed", 503) from exc
            self._publish(authority.request_id, digest, result)
            entry.sequence = candidate
            return result

    @staticmethod
    def _compensate_unpublished_child(entry: _ServiceEntry) -> None:
        binding = entry.bound_child_binding
        if binding is None or not entry.pending_child_compensation:
            return
        # CRITICAL: only unpublished bindings prove that the browser could not queue this child.
        # Keep the exact rollback authority on failure; clearing it strands the reserved child.
        try:
            binding.rollback()
        except Exception as exc:
            raise ManagedSequenceServiceError("child_binding_compensation_failed", 500) from exc
        entry.bound_child_binding = None
        entry.pending_child_compensation = False

    @staticmethod
    def _fail_prepared_binding(
        entry: _ServiceEntry,
        request: BindManagedSequenceChildRequestV1,
        *,
        failure_code: str,
    ) -> ManagedSequenceV1:
        failure_fingerprint = canonical_fingerprint(
            {
                "schema": "h3.context.managed_child_binding_failure.v1",
                "parent_sequence_id": request.parent_sequence_id,
                "authorization_fingerprint": request.authorization_fingerprint,
                "segment_id": request.segment_id,
                "eligible_execution_fingerprint": request.eligible_execution_fingerprint,
                "failure_code": failure_code,
            }
        )
        try:
            return fail_prepared_sequence_child(
                entry.sequence,
                segment_id=request.segment_id,
                expected_revision=request.expected_revision,
                eligible_execution_fingerprint=request.eligible_execution_fingerprint,
                failure_fingerprint=failure_fingerprint,
            )
        except ManagedSequenceError as exc:  # pragma: no cover - guarded by held execution CAS
            raise ManagedSequenceServiceError("binding_failure_transition", 500) from exc

    def _commit_prepared_binding_failure(
        self,
        entry: _ServiceEntry,
        request: BindManagedSequenceChildRequestV1,
        digest: str,
        *,
        failure_code: str,
    ) -> ManagedSequenceMutationResultV1:
        candidate = self._fail_prepared_binding(entry, request, failure_code=failure_code)
        result = self._result(replace(entry, sequence=candidate))
        self._validate_publication(result)
        self._consume_candidate_rows(
            entry,
            request_id=request.request_id,
            candidate=candidate,
        )
        self._publish(request.request_id, digest, result)
        entry.sequence = candidate
        return result

    @staticmethod
    def _can_retire(entry: _ServiceEntry) -> bool:
        sequence = entry.sequence
        # CRITICAL: CANCELLED stops future work, not an already submitted child's ownership.
        # Replacing this sole entry before terminal/cleanup loses the only parent read authority.
        return (
            sequence.state in {ManagedSequenceStateV1.CANCELLED, ManagedSequenceStateV1.SUCCEEDED}
            and sequence.active_segment_id is None
            and all(
                row.state
                in {
                    SequenceSlotStateV1.SUCCEEDED,
                    SequenceSlotStateV1.REUSED,
                    SequenceSlotStateV1.FAILED,
                    SequenceSlotStateV1.INTERRUPTED,
                    SequenceSlotStateV1.CANCELLED,
                }
                for row in sequence.slots
            )
            and entry.held_materialization is None
            and entry.bound_child_binding is None
            and not entry.pending_child_compensation
            and (entry.start_resources is None or entry.start_resources.released)
            and (sequence.ledger_reservation is None or sequence.ledger_reservation.released)
            and (sequence.artifact_reservation is None or sequence.artifact_reservation.released)
        )

    def authorize(
        self, request: AuthorizeManagedSequenceRequestV1
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not AuthorizeManagedSequenceRequestV1:
            raise ManagedSequenceServiceError("authorization_request_type", 400)
        digest = self._request_digest("authorize_sequence", request.to_wire())
        with self._lock:
            replay = self._replay(request.request_id, digest)
            if replay is not None:
                return replay
            if self._entry is not None and not self._can_retire(self._entry):
                raise ManagedSequenceServiceError("active_sequence_capacity", 503)
            source = self._production_registry.claim_automatic_plan_authority(
                request.workspace_handle,
                expected_workspace_revision=request.expected_workspace_revision,
                expected_workspace_fingerprint=request.expected_workspace_fingerprint,
                expected_plan_fingerprint=request.expected_plan_fingerprint,
            )
            parent_id = self._new_parent_id()
            authorization = self._authorization_builder(
                source,
                sequence_id=parent_id,
                generation_plan_fingerprint=request.generation_plan_fingerprint,
                compiler_fingerprint=request.compiler_fingerprint,
                host_capability_fingerprint=request.host_capability_fingerprint,
            )
            if type(authorization) is not ManagedSequenceAuthorizationV1:
                raise ManagedSequenceServiceError("authorization_builder_result", 500)
            if (
                authorization.sequence_id != parent_id
                or authorization.workspace_revision != request.expected_workspace_revision
                or authorization.workspace_fingerprint != request.expected_workspace_fingerprint
                or authorization.production_plan_fingerprint != request.expected_plan_fingerprint
                or authorization.generation_plan_fingerprint != request.generation_plan_fingerprint
                or authorization.compiler_fingerprint != request.compiler_fingerprint
                or authorization.host_capability_fingerprint != request.host_capability_fingerprint
                or authorization.explicit_intent != request.explicit_intent
            ):
                raise ManagedSequenceServiceError("authorization_builder_drift", 500)
            try:
                sequence = authorize_managed_sequence(authorization, now=self._clock())
            except ManagedSequenceError as exc:
                raise self._translate(exc) from exc
            entry = _ServiceEntry(
                source_authority=source,
                sequence=sequence,
                read_authority_fingerprint=self._read_authority(sequence),
                production_workspace_handle=request.workspace_handle,
            )
            result = self._result(entry)
            # IMPORTANT: replay retention is bounded by the next parent's whole reserved path,
            # not just its authorize row. Only safely retired parents reach this point; never
            # evict a live parent's responses to make space or strand its reserved cancellation.
            required_rows = coordinator_ledger_rows(len(sequence.slots))
            if required_rows > MAX_MANAGED_SEQUENCE_MUTATION_ROWS:
                raise ManagedSequenceServiceError("request_capacity", 503)
            previous_ledger = self._ledger
            retained = previous_ledger.copy()
            while len(retained) + required_rows > MAX_MANAGED_SEQUENCE_MUTATION_ROWS:
                retained.popitem(last=False)
            self._ledger = retained
            try:
                self._publish(request.request_id, digest, result)
            except Exception:
                self._ledger = previous_ledger
                raise
            self._entry = entry
            return result

    def start(
        self,
        request: StartManagedSequenceRequestV1,
        *,
        qualification: ManagedModeQualificationV1 | ManagedModeQualificationV2,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not StartManagedSequenceRequestV1 or type(qualification) not in (
            ManagedModeQualificationV1,
            ManagedModeQualificationV2,
        ):
            raise ManagedSequenceServiceError("start_request_type", 400)
        digest = self._request_digest(
            "start_sequence",
            {**request.to_wire(), "qualification_fingerprint": qualification.fingerprint},
        )
        guard: AbstractContextManager[None] = nullcontext()
        if type(qualification) is ManagedModeQualificationV2:
            if self._qualification_guard is None:
                raise ManagedSequenceServiceError("qualification_guard_unavailable", 503)
            guard = self._qualification_guard(qualification)
        with self._lock:
            replay = self._replay(request.request_id, digest)
            if replay is not None:
                return replay
            entry = self._current(
                request.parent_sequence_id,
                request.authorization_fingerprint,
                request.expected_revision,
            )
            try:
                candidate = start_managed_sequence(
                    entry.sequence,
                    qualification,
                    now=self._clock(),
                )
            except ManagedSequenceError as exc:
                raise self._translate(exc) from exc
            ledger = candidate.ledger_reservation
            artifact = candidate.artifact_reservation
            lease = candidate.lease
            if ledger is None or artifact is None or lease is None:
                raise ManagedSequenceServiceError("start_resource_contract_missing", 500)
            lease_duration_ms = round((lease.absolute_deadline - lease.created_at) * 1_000)
            if type(lease_duration_ms) is not int or not 1 <= lease_duration_ms <= 86_400_000:
                raise ManagedSequenceServiceError("parent_expiry_contract", 500)
            parent_expires_at_epoch_ms = self._epoch_now_ms() + lease_duration_ms
            if parent_expires_at_epoch_ms > 2**53 - 1:
                raise ManagedSequenceServiceError("parent_expiry_contract", 500)
            if not callable(self._reserve_start_resources):
                raise ManagedSequenceServiceError("resource_reserver_unavailable", 503)
            resource_request = ManagedSequenceStartResourceRequestV1(
                parent_sequence_id=candidate.authorization.sequence_id,
                parent_authorization_fingerprint=candidate.authorization.fingerprint,
                segment_count=len(candidate.slots),
                coordinator_reserved_rows=ledger.reserved_rows,
                coordinator_reserved_bytes=ledger.reserved_bytes,
                artifact_reserved_entries=artifact.reserved_entries,
                artifact_reserved_bytes=artifact.reserved_bytes,
                per_artifact_max_bytes=artifact.per_artifact_bytes,
                idle_expires_at=lease.idle_deadline,
                expires_at=min(
                    ledger.expires_at,
                    artifact.expires_at,
                    lease.absolute_deadline,
                ),
                expires_at_epoch_ms=parent_expires_at_epoch_ms,
            )
            try:
                resource_claim = self._reserve_start_resources(resource_request)
            except ManagedSequenceServiceError:
                raise
            except Exception as exc:
                raise ManagedSequenceServiceError("start_resource_reservation_failed", 503) from exc
            if type(resource_claim) is not ManagedSequenceStartResourceClaimV1:
                raise ManagedSequenceServiceError("start_resource_claim_type", 500)
            held_resources = _HeldStartResources(resource_claim)
            if resource_claim.request_fingerprint != resource_request.fingerprint:
                try:
                    held_resources.release_once()
                except Exception as exc:
                    raise ManagedSequenceServiceError(
                        "start_resource_compensation_failed", 503
                    ) from exc
                raise ManagedSequenceServiceError("resource_claim_drift", 500)
            candidate_entry = replace(
                entry,
                start_resources=held_resources,
                parent_expires_at_epoch_ms=parent_expires_at_epoch_ms,
                sequence=candidate,
            )
            result = self._result(candidate_entry)
            self._validate_publication(result)
            try:
                # The physical reservation already accounts for the retained authorize row. Start
                # consumes the second parent-control row exactly once before the parent commits.
                held_resources.consume_rows(request.request_id, 1)
            except Exception as exc:
                try:
                    held_resources.release_once()
                except Exception as release_exc:
                    raise ManagedSequenceServiceError(
                        "start_resource_compensation_failed", 503
                    ) from release_exc
                if isinstance(exc, ManagedSequenceServiceError):
                    raise
                raise ManagedSequenceServiceError("start_resource_consumption_failed", 503) from exc
            try:
                # CRITICAL: reserve/consume callbacks run before the Production guard. Holding
                # that registry across coordinator callbacks reverses their existing lock order.
                with guard:
                    self._publish(request.request_id, digest, result)
                    entry.start_resources = held_resources
                    entry.parent_expires_at_epoch_ms = parent_expires_at_epoch_ms
                    entry.sequence = candidate
            except Exception:
                try:
                    held_resources.release_once()
                except Exception:
                    raise ManagedSequenceServiceError(
                        "start_resource_compensation_failed", 503
                    ) from None
                raise
            return result

    def prepare_child(
        self, request: PrepareManagedSequenceChildRequestV1
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not PrepareManagedSequenceChildRequestV1:
            raise ManagedSequenceServiceError("prepare_request_type", 400)
        digest = self._request_digest("prepare_sequence_child", request.to_wire())
        with self._lock:
            replay = self._replay(request.request_id, digest)
            if replay is not None:
                entry = self._entry
                if (
                    entry is None
                    or entry.held_materialization is None
                    or replay.execution is None
                    or entry.held_materialization.execution is None
                    or entry.held_materialization.execution.fingerprint
                    != replay.execution.fingerprint
                ):
                    raise ManagedSequenceServiceError("replay_superseded", 409)
                return replay
            entry = self._current(
                request.parent_sequence_id,
                request.authorization_fingerprint,
                request.expected_revision,
            )
            self._expire_before_mutation(entry)
            if entry.held_materialization is not None:
                raise ManagedSequenceServiceError("materialization_claim_active", 409)
            if not callable(self._materialize_segment):
                raise ManagedSequenceServiceError("materializer_unavailable", 503)
            try:
                claim = self._materialize_segment(
                    entry.source_authority,
                    request.segment_id,
                )
            except ManagedSequenceServiceError:
                raise
            except Exception as exc:
                raise ManagedSequenceServiceError(
                    "segment_context_materialization_failed", 422
                ) from exc
            if type(claim) not in (
                ManagedSegmentMaterializationClaimV1,
                ManagedSegmentMaterializationClaimV2,
            ):
                raise ManagedSequenceServiceError("materialization_claim_type", 500)
            held = _HeldMaterialization(claim, None)
            try:
                if claim.segment_id != request.segment_id:
                    raise ManagedSequenceServiceError("materialization_segment_drift", 409)
                authority_row = next(
                    (
                        row
                        for row in entry.sequence.authorization.segments
                        if row.segment_id == request.segment_id
                    ),
                    None,
                )
                if (
                    authority_row is None
                    or claim.materialization_receipt_fingerprint
                    != authority_row.materialization_receipt_fingerprint
                ):
                    raise ManagedSequenceServiceError("materialization_drift", 409)
                prepared = prepare_sequence_child(
                    entry.sequence,
                    segment_id=request.segment_id,
                    expected_revision=request.expected_revision,
                    materialization_receipt_fingerprint=(claim.materialization_receipt_fingerprint),
                    predecessor_terminal_fingerprint=(request.predecessor_terminal_fingerprint),
                    request_id=request.request_id,
                )
            except ManagedSequenceError as exc:
                held.release_once()
                raise self._translate(exc) from exc
            except Exception:
                held.release_once()
                raise
            held.execution = prepared.execution
            result = self._result(
                replace(entry, sequence=prepared.sequence, held_materialization=held),
                execution=prepared.execution,
                context_workspace_handle=claim.context_workspace_handle,
            )
            self._validate_publication(result)
            try:
                self._consume_candidate_rows(
                    entry,
                    request_id=request.request_id,
                    candidate=prepared.sequence,
                )
            except Exception:
                held.release_once()
                raise
            self._publish(request.request_id, digest, result)
            entry.sequence = prepared.sequence
            entry.held_materialization = held
            return result

    @staticmethod
    def _prepared_context(
        entry: _ServiceEntry,
        claim: ManagedSegmentMaterializationClaimV2,
        execution: EligibleSegmentExecutionV1,
    ) -> ManagedPreparedContextV1:
        snapshot = claim.materialization
        report = snapshot.report
        wiring = snapshot.wiring
        receipt = claim.receipt
        registry = report.request.reference_registry
        profile = report.request.profile
        if (
            type(report) is not ContextReport
            or fingerprint_context_report(report) != snapshot.report_fingerprint
            or report.revision != snapshot.report_revision
            or report.report_id != wiring.report_id
            or report.prompt_document.text != wiring.prompt
            or report.request.user_intent != wiring.prompt
            or wiring.prompt_fingerprint != canonical_fingerprint(wiring.prompt)
            or fingerprint_prompt_text(wiring.prompt) != receipt.local_prompt_fingerprint
            or report.request.task_mode is not receipt.task_mode
            or wiring.task_mode is not receipt.task_mode
            or report.request.effective_frame_count != receipt.frame_count
            or wiring.native_length_frames != receipt.frame_count
            or canonical_fingerprint(profile.to_wire()) != receipt.profile_fingerprint
            or tuple(row.asset_id for row in registry.assets) != receipt.ordered_reference_ids
            or canonical_fingerprint(registry.to_wire()) != receipt.reference_registry_fingerprint
            or execution.materialization_receipt_fingerprint != receipt.fingerprint
        ):
            raise ManagedSequenceServiceError("prepared_context_drift", 409)
        result = ManagedPreparedContextV1(
            parent_sequence_id=entry.sequence.authorization.sequence_id,
            parent_revision=entry.sequence.revision,
            authorization_fingerprint=entry.sequence.authorization.fingerprint,
            segment_id=receipt.segment_id,
            eligible_execution_fingerprint=execution.fingerprint,
            materialization_receipt_fingerprint=receipt.fingerprint,
            context_workspace_handle=claim.context_workspace_handle,
            context_report_id=report.report_id,
            context_report_revision=report.revision,
            context_report_fingerprint=snapshot.report_fingerprint,
            canonical_prompt=wiring.prompt,
            canonical_prompt_fingerprint=wiring.prompt_fingerprint,
            task_mode=receipt.task_mode,
            duration_milliseconds=receipt.duration_milliseconds,
            frame_count=receipt.frame_count,
            profile=profile,
            profile_fingerprint=receipt.profile_fingerprint,
            ordered_references=registry.assets,
            reference_registry_fingerprint=receipt.reference_registry_fingerprint,
            native_binding_fingerprint=receipt.native_binding_fingerprint,
            canonical_lowering=snapshot.canonical_lowering,
        )
        if len(canonical_bytes(result.to_wire())) > MAX_COORDINATOR_RESPONSE_BYTES:
            raise ManagedSequenceServiceError("response_size", 500)
        return result

    def read_prepared_child_context(
        self,
        request: ReadManagedPreparedContextRequestV1,
    ) -> ManagedPreparedContextV1:
        if type(request) is not ReadManagedPreparedContextRequestV1:
            raise ManagedSequenceServiceError("prepared_context_request_type", 400)
        # IMPORTANT: this is an exact held-authority read. It does not sample the clock, touch the
        # scratch Sidebar TTL or allocate a replay row; the next lifecycle mutation owns expiry.
        with self._lock:
            entry = self._current(
                request.parent_sequence_id,
                request.authorization_fingerprint,
                request.expected_revision,
            )
            held = entry.held_materialization
            if (
                held is None
                or type(held.claim) is not ManagedSegmentMaterializationClaimV2
                or held.execution is None
                or held.claim.segment_id != request.segment_id
                or held.claim.context_workspace_handle != request.context_workspace_handle
                or held.claim.materialization_receipt_fingerprint
                != request.materialization_receipt_fingerprint
                or held.execution.fingerprint != request.eligible_execution_fingerprint
            ):
                raise ManagedSequenceServiceError("prepared_context_unavailable", 409)
            held.claim.assert_current()
            return self._prepared_context(entry, held.claim, held.execution)

    def bind_child(
        self, request: BindManagedSequenceChildRequestV1
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not BindManagedSequenceChildRequestV1:
            raise ManagedSequenceServiceError("bind_request_type", 400)
        digest = self._request_digest("bind_prepared_child", request.to_wire())
        with self._lock:
            replay = self._replay(request.request_id, digest)
            if replay is not None:
                return replay
            entry = self._current(
                request.parent_sequence_id,
                request.authorization_fingerprint,
                request.expected_revision,
            )
            self._expire_before_mutation(entry)
            if entry.pending_child_compensation:
                raise ManagedSequenceServiceError("child_binding_compensation_pending", 409)
            held = entry.held_materialization
            if (
                held is None
                or held.execution is None
                or held.execution.fingerprint != request.eligible_execution_fingerprint
            ):
                raise ManagedSequenceServiceError("prepared_context_unavailable", 409)
            if (
                held.claim.segment_id != request.segment_id
                or held.claim.materialization_receipt_fingerprint
                != request.materialization_receipt_fingerprint
            ):
                held.release_once()
                entry.held_materialization = None
                return self._commit_prepared_binding_failure(
                    entry,
                    request,
                    digest,
                    failure_code="materialization_drift",
                )
            if type(held.claim) is ManagedSegmentMaterializationClaimV2:
                try:
                    held.claim.assert_current()
                except ManagedSequenceServiceError:
                    try:
                        held.release_once()
                    except Exception as exc:
                        raise ManagedSequenceServiceError(
                            "prepared_context_release_failed", 503
                        ) from exc
                    entry.held_materialization = None
                    return self._commit_prepared_binding_failure(
                        entry,
                        request,
                        digest,
                        failure_code="prepared_context_drift",
                    )
            execution = held.execution
            if execution.fingerprint != request.eligible_execution_fingerprint:
                raise ManagedSequenceServiceError("eligible_execution_drift", 409)
            if entry.sequence.canvas_authority is not None:
                try:
                    checked = check_managed_sequence_canvas_authority(
                        entry.sequence,
                        expected_revision=request.expected_revision,
                        active_workflow_fingerprint=request.active_workflow_fingerprint,
                        owned_projection_fingerprint=(
                            request.previous_owned_projection_fingerprint
                        ),
                    )
                except ManagedSequenceError as exc:
                    raise self._translate(exc) from exc
                if checked.state is ManagedSequenceStateV1.PAUSED_CANVAS_DRIFT:
                    # CRITICAL: canvas authority is checked before creating the child ManagedRun.
                    # A rollback after binder invocation is too late: it temporarily creates a
                    # second mutable child authority for a graph the user no longer owns.
                    result = self._result(
                        replace(entry, sequence=checked, held_materialization=None)
                    )
                    self._validate_publication(result)
                    self._consume_candidate_rows(
                        entry,
                        request_id=request.request_id,
                        candidate=checked,
                    )
                    held.release_once()
                    self._publish(request.request_id, digest, result)
                    entry.held_materialization = None
                    entry.sequence = checked
                    return result
            if not callable(self._bind_child):
                held.release_once()
                entry.held_materialization = None
                return self._commit_prepared_binding_failure(
                    entry,
                    request,
                    digest,
                    failure_code="child_binder_unavailable",
                )
            binding: ManagedChildBindingClaimV1 | None = None
            candidate: ManagedSequenceV1 | None = None
            failure_code: str | None = None
            try:
                claim = self._bind_child(
                    ManagedChildBindingRequestV1(
                        context_workspace_handle=held.claim.context_workspace_handle,
                        execution=execution,
                        graph_fingerprint=request.graph_fingerprint,
                        compiled_prompt_fingerprint=request.compiled_prompt_fingerprint,
                        correlation=request.correlation,
                        observation=request.observation,
                        production_workspace_handle=(
                            entry.production_workspace_handle
                            if type(entry.source_authority) is ProductionAutomaticPlanAuthorityV2
                            else None
                        ),
                        source_authority=(
                            entry.source_authority
                            if type(entry.source_authority) is ProductionAutomaticPlanAuthorityV2
                            else None
                        ),
                    )
                )
                if type(claim) is not ManagedChildBindingClaimV1:
                    raise ManagedSequenceServiceError("child_binding_claim_type", 500)
                binding = claim
                entry.bound_child_binding = binding
                entry.pending_child_compensation = True
                candidate = bind_prepared_child(
                    entry.sequence,
                    segment_id=request.segment_id,
                    expected_revision=request.expected_revision,
                    eligible_execution_fingerprint=request.eligible_execution_fingerprint,
                    child_run_handle=binding.child_run_handle,
                    child_state_fingerprint=binding.child_state_fingerprint,
                    graph_fingerprint=request.graph_fingerprint,
                    compiled_prompt_fingerprint=request.compiled_prompt_fingerprint,
                    previous_owned_projection_fingerprint=(
                        request.previous_owned_projection_fingerprint
                    ),
                    active_workflow_fingerprint=request.active_workflow_fingerprint,
                )
            except ManagedSequenceError:
                failure_code = "child_binding_rejected"
            except Exception:
                failure_code = "child_binding_failed"
            try:
                # CRITICAL: Context release is part of provisional binding, not an unprotected
                # finally. Its exception must compensate the child just like publication failure.
                try:
                    held.release_once()
                except Exception as exc:
                    raise ManagedSequenceServiceError(
                        "prepared_context_release_failed", 503
                    ) from exc
                finally:
                    entry.held_materialization = None
                if failure_code is not None:
                    self._compensate_unpublished_child(entry)
                    return self._commit_prepared_binding_failure(
                        entry, request, digest, failure_code=failure_code
                    )
                if binding is None or candidate is None:  # pragma: no cover - guarded above
                    raise ManagedSequenceServiceError("child_binding_claim_type", 500)
                result = self._result(
                    replace(entry, sequence=candidate),
                    child_authority=binding.child_authority(),
                )
                self._validate_publication(result)
                self._consume_candidate_rows(
                    entry,
                    request_id=request.request_id,
                    candidate=candidate,
                )
                self._publish(request.request_id, digest, result)
            except Exception as exc:
                # A failed compensation must remain retryable by cancel/lease cleanup. Do not
                # automatically retry rollback in this same action after it already failed.
                if not (
                    isinstance(exc, ManagedSequenceServiceError)
                    and exc.code == "child_binding_compensation_failed"
                ):
                    self._compensate_unpublished_child(entry)
                if isinstance(exc, ManagedSequenceServiceError):
                    raise
                raise ManagedSequenceServiceError("child_binding_publication_failed", 503) from exc
            entry.sequence = candidate
            entry.bound_child_binding = binding
            entry.pending_child_compensation = False
            return result

    def record_submission(
        self,
        request: RecordManagedSequenceSubmissionRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not RecordManagedSequenceSubmissionRequestV1:
            raise ManagedSequenceServiceError("submission_request_type", 400)

        def transition(sequence: ManagedSequenceV1) -> ManagedSequenceV1:
            written = record_managed_sequence_canvas_write(
                sequence,
                segment_id=request.segment_id,
                expected_revision=request.authority.expected_revision,
                active_workflow_fingerprint=request.active_workflow_fingerprint,
                previous_owned_projection_fingerprint=(
                    request.previous_owned_projection_fingerprint
                ),
                written_owned_projection_fingerprint=(request.written_owned_projection_fingerprint),
            )
            return record_managed_sequence_submission(
                written,
                segment_id=request.segment_id,
                expected_revision=written.revision,
                child_run_handle=request.child_run_handle,
                child_state_fingerprint=request.child_state_fingerprint,
                queue_prompt_id=request.queue_prompt_id,
                timeout_ms=request.timeout_ms,
                now=self._clock(),
            )

        result = self._apply_transition(
            "record_submission",
            request.authority,
            request.to_wire(),
            transition,
        )
        with self._lock:
            entry = self._entry
            if entry is not None and entry.sequence.revision == result.revision:
                entry.bound_child_binding = None
        return result

    def fail_prepared_child(
        self,
        request: FailPreparedManagedSequenceChildRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not FailPreparedManagedSequenceChildRequestV1:
            raise ManagedSequenceServiceError("prepared_failure_request_type", 400)
        digest = self._request_digest("fail_prepared_child", request.to_wire())
        authority = request.authority
        with self._lock:
            replay = self._replay(authority.request_id, digest)
            if replay is not None:
                return replay
            entry = self._current(
                authority.parent_sequence_id,
                authority.authorization_fingerprint,
                authority.expected_revision,
            )
            self._expire_before_mutation(entry)
            held = entry.held_materialization
            if (
                held is None
                or held.execution is None
                or held.claim.segment_id != request.segment_id
                or held.execution.fingerprint != request.eligible_execution_fingerprint
            ):
                raise ManagedSequenceServiceError("prepared_context_unavailable", 409)
            try:
                candidate = fail_prepared_sequence_child(
                    entry.sequence,
                    segment_id=request.segment_id,
                    expected_revision=authority.expected_revision,
                    eligible_execution_fingerprint=(request.eligible_execution_fingerprint),
                    failure_fingerprint=request.failure_fingerprint,
                )
            except ManagedSequenceError as exc:
                raise self._translate(exc) from exc
            result = self._result(replace(entry, sequence=candidate))
            self._validate_publication(result)
            self._consume_candidate_rows(
                entry,
                request_id=authority.request_id,
                candidate=candidate,
            )
            # CRITICAL: compile/validation refusal must destroy the exact M26-03 Context before
            # publishing the parent pause. Reversing this order exposes a FAILED parent while its
            # supposedly compile-only mutable workspace is still live and no longer addressable.
            try:
                held.release_once()
            except Exception as exc:
                raise ManagedSequenceServiceError("prepared_context_release_failed", 503) from exc
            self._publish(authority.request_id, digest, result)
            entry.held_materialization = None
            entry.sequence = candidate
            return result

    def fail_bound_child(
        self,
        request: FailBoundManagedSequenceChildRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not FailBoundManagedSequenceChildRequestV1:
            raise ManagedSequenceServiceError("bound_failure_request_type", 400)
        digest = self._request_digest("fail_bound_child", request.to_wire())
        authority = request.authority
        with self._lock:
            replay = self._replay(authority.request_id, digest)
            if replay is not None:
                return replay
            entry = self._current(
                authority.parent_sequence_id,
                authority.authorization_fingerprint,
                authority.expected_revision,
            )
            self._expire_before_mutation(entry)
            binding = entry.bound_child_binding
            if binding is None or binding.child_run_handle != request.child_run_handle:
                raise ManagedSequenceServiceError("bound_child_unavailable", 409)
            try:
                candidate = fail_bound_sequence_child(
                    entry.sequence,
                    segment_id=request.segment_id,
                    expected_revision=authority.expected_revision,
                    child_run_handle=request.child_run_handle,
                    failure_fingerprint=request.failure_fingerprint,
                )
            except ManagedSequenceError as exc:
                raise self._translate(exc) from exc
            result = self._result(replace(entry, sequence=candidate))
            self._validate_publication(result)
            self._consume_candidate_rows(
                entry,
                request_id=authority.request_id,
                candidate=candidate,
            )
            # CRITICAL: an explicit pre-acceptance queue refusal proves the host owns no prompt.
            # Remove the exact provisional child before publishing FAILED; retaining it consumes
            # the sole slot, while applying this compensation to an ambiguous invocation can erase
            # work the host actually accepted.
            # IMPORTANT: later resource release/publication may fail. Retain cleanup completion
            # so retry cannot destroy the same child twice or admit a late submission for it.
            if not entry.bound_child_cleanup_completed:
                try:
                    binding.rollback()
                except Exception as exc:
                    raise ManagedSequenceServiceError("bound_child_cleanup_failed", 503) from exc
                entry.bound_child_cleanup_completed = True
            if (
                candidate.state is ManagedSequenceStateV1.CANCELLED
                and entry.start_resources is not None
            ):
                try:
                    entry.start_resources.release_once()
                except Exception as exc:
                    raise ManagedSequenceServiceError("start_resource_release_failed", 503) from exc
            self._publish(authority.request_id, digest, result)
            entry.bound_child_binding = None
            entry.bound_child_cleanup_completed = False
            entry.sequence = candidate
            return result

    def mark_invocation_unknown(
        self,
        request: MarkManagedSequenceInvocationUnknownRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not MarkManagedSequenceInvocationUnknownRequestV1:
            raise ManagedSequenceServiceError("invocation_unknown_request_type", 400)

        def transition(sequence: ManagedSequenceV1) -> ManagedSequenceV1:
            written = record_managed_sequence_canvas_write(
                sequence,
                segment_id=request.segment_id,
                expected_revision=request.authority.expected_revision,
                active_workflow_fingerprint=request.active_workflow_fingerprint,
                previous_owned_projection_fingerprint=(
                    request.previous_owned_projection_fingerprint
                ),
                written_owned_projection_fingerprint=(request.written_owned_projection_fingerprint),
            )
            return mark_managed_sequence_invocation_unknown(
                written,
                segment_id=request.segment_id,
                expected_revision=written.revision,
                child_run_handle=request.child_run_handle,
                timeout_ms=request.timeout_ms,
                now=self._clock(),
            )

        result = self._apply_transition(
            "mark_invocation_unknown",
            request.authority,
            request.to_wire(),
            transition,
        )
        with self._lock:
            entry = self._entry
            if entry is not None and entry.sequence.revision == result.revision:
                # The rollback callback is unsafe after invocation: no prompt id does not prove
                # non-ownership. The retained coordinator/ManagedRun remains protected for lease
                # reconciliation, but no browser action can accidentally call pre-host cleanup.
                entry.bound_child_binding = None
        return result

    def record_running(
        self,
        request: RecordManagedSequenceRunningRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not RecordManagedSequenceRunningRequestV1:
            raise ManagedSequenceServiceError("running_request_type", 400)
        return self._apply_transition(
            "record_running",
            request.authority,
            request.to_wire(),
            lambda sequence: record_managed_sequence_running(
                sequence,
                segment_id=request.segment_id,
                expected_revision=request.authority.expected_revision,
                queue_prompt_id=request.queue_prompt_id,
                child_state_fingerprint=request.child_state_fingerprint,
                now=self._clock(),
            ),
        )

    def record_artifact(
        self,
        request: RecordManagedSequenceArtifactRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not RecordManagedSequenceArtifactRequestV1:
            raise ManagedSequenceServiceError("artifact_request_type", 400)
        return self._apply_transition(
            "record_artifact",
            request.authority,
            request.to_wire(),
            lambda sequence: record_managed_sequence_artifact(
                sequence,
                segment_id=request.segment_id,
                expected_revision=request.authority.expected_revision,
                queue_prompt_id=request.queue_prompt_id,
                artifact_receipt_fingerprint=request.artifact_receipt_fingerprint,
                artifact_bytes=request.artifact_bytes,
            ),
        )

    def record_terminal(
        self,
        request: RecordManagedSequenceTerminalRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not RecordManagedSequenceTerminalRequestV1:
            raise ManagedSequenceServiceError("terminal_request_type", 400)
        return self._apply_transition(
            "record_terminal",
            request.authority,
            request.to_wire(),
            lambda sequence: record_managed_sequence_terminal(
                sequence,
                segment_id=request.segment_id,
                expected_revision=request.authority.expected_revision,
                queue_prompt_id=request.queue_prompt_id,
                kind=request.kind,
                terminal_fingerprint=request.terminal_fingerprint,
            ),
        )

    def record_reuse(
        self,
        request: RecordManagedSequenceReuseRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not RecordManagedSequenceReuseRequestV1:
            raise ManagedSequenceServiceError("reuse_request_type", 400)
        return self._apply_transition(
            "record_reuse",
            request.authority,
            request.to_wire(),
            lambda sequence: record_managed_sequence_reuse(
                sequence,
                segment_id=request.segment_id,
                expected_revision=request.authority.expected_revision,
                artifact_receipt_fingerprint=request.artifact_receipt_fingerprint,
                artifact_bytes=request.artifact_bytes,
                terminal_fingerprint=request.terminal_fingerprint,
            ),
        )

    def detach(
        self,
        authority: ManagedSequenceMutationAuthorityV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(authority) is not ManagedSequenceMutationAuthorityV1:
            raise ManagedSequenceServiceError("detach_request_type", 400)
        return self._apply_transition(
            "detach_client",
            authority,
            authority.to_wire(),
            lambda sequence: detach_managed_sequence_client(
                sequence,
                expected_revision=authority.expected_revision,
            ),
        )

    def resume(
        self,
        request: ResumeManagedSequenceRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not ResumeManagedSequenceRequestV1:
            raise ManagedSequenceServiceError("resume_request_type", 400)
        return self._apply_transition(
            "resume_sequence",
            request.authority,
            request.to_wire(),
            lambda sequence: resume_managed_sequence(
                sequence,
                expected_revision=request.authority.expected_revision,
                active_workflow_fingerprint=request.active_workflow_fingerprint,
                owned_projection_fingerprint=request.owned_projection_fingerprint,
            ),
        )

    def record_canvas_rollback(
        self,
        request: RecordManagedSequenceCanvasRollbackRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not RecordManagedSequenceCanvasRollbackRequestV1:
            raise ManagedSequenceServiceError("canvas_rollback_request_type", 400)
        return self._apply_transition(
            "record_canvas_rollback",
            request.authority,
            request.to_wire(),
            lambda sequence: record_managed_sequence_canvas_rollback(
                sequence,
                segment_id=request.segment_id,
                expected_revision=request.authority.expected_revision,
                active_workflow_fingerprint=request.active_workflow_fingerprint,
                drifted_owned_projection_fingerprint=(request.drifted_owned_projection_fingerprint),
                restored_owned_projection_fingerprint=(
                    request.restored_owned_projection_fingerprint
                ),
            ),
        )

    def mark_unknown_ownership(
        self,
        request: UnknownManagedSequenceOwnershipRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not UnknownManagedSequenceOwnershipRequestV1:
            raise ManagedSequenceServiceError("unknown_ownership_request_type", 400)
        return self._apply_transition(
            "mark_unknown_ownership",
            request.authority,
            request.to_wire(),
            lambda sequence: mark_managed_sequence_unknown_ownership(
                sequence,
                segment_id=request.segment_id,
                expected_revision=request.authority.expected_revision,
                queue_prompt_id=request.queue_prompt_id,
            ),
        )

    def retry(
        self,
        request: ManagedSequenceSegmentMutationRequestV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(request) is not ManagedSequenceSegmentMutationRequestV1:
            raise ManagedSequenceServiceError("retry_request_type", 400)
        return self._apply_transition(
            "retry_segment",
            request.authority,
            request.to_wire(),
            lambda sequence: retry_managed_sequence_segment(
                sequence,
                segment_id=request.segment_id,
                expected_revision=request.authority.expected_revision,
                active_workflow_fingerprint=request.active_workflow_fingerprint,
                owned_projection_fingerprint=request.owned_projection_fingerprint,
            ),
        )

    def cancel(
        self,
        authority: ManagedSequenceMutationAuthorityV1,
    ) -> ManagedSequenceMutationResultV1:
        if type(authority) is not ManagedSequenceMutationAuthorityV1:
            raise ManagedSequenceServiceError("cancel_request_type", 400)
        return self._apply_transition(
            "cancel_sequence",
            authority,
            authority.to_wire(),
            lambda sequence: cancel_managed_sequence(
                sequence,
                expected_revision=authority.expected_revision,
            ),
            release_prepared=True,
        )

    def _expire_lease_transaction(
        self,
        parent_sequence_id: str,
        authorization_fingerprint: str,
        *,
        expected_revision: int,
        now: float | None = None,
        child_transition: Callable[[ManagedSequenceV1, ManagedSequenceV1, float], None]
        | None = None,
    ) -> ManagedSequenceMutationResultV1:
        with self._lock:
            entry = self._current(
                parent_sequence_id,
                authorization_fingerprint,
                expected_revision,
            )
            observed = self._clock() if now is None else now
            try:
                candidate = expire_managed_sequence_lease(
                    entry.sequence,
                    expected_revision=expected_revision,
                    now=observed,
                )
            except ManagedSequenceError as exc:
                raise self._translate(exc) from exc
            if entry.pending_child_compensation:
                self._compensate_unpublished_child(entry)
            if candidate is entry.sequence:
                return self._result(entry)
            active_slot = next(
                (
                    slot
                    for slot in entry.sequence.slots
                    if slot.segment_id == entry.sequence.active_segment_id
                ),
                None,
            )
            resets_bound_child = (
                active_slot is not None
                and active_slot.state is SequenceSlotStateV1.BOUND
                and candidate.active_segment_id is None
            )
            if resets_bound_child:
                binding = entry.bound_child_binding
                reset_slot = next(
                    (
                        slot
                        for slot in candidate.slots
                        if active_slot is not None and slot.segment_id == active_slot.segment_id
                    ),
                    None,
                )
                if (
                    binding is None
                    or active_slot is None
                    or binding.child_run_handle != active_slot.child_run_handle
                    or reset_slot is None
                    or reset_slot.state is not SequenceSlotStateV1.ELIGIBLE
                    or reset_slot.child_run_handle is not None
                    or child_transition is None
                ):
                    raise ManagedSequenceServiceError("lease_child_authority_unavailable", 409)
            held = entry.held_materialization
            if held is not None and candidate.active_segment_id is None:
                # CRITICAL: publish the parent reset only after the exact short-lived Context
                # authority is gone. Reversing these writes strands an unaddressable handle; marking
                # the claim released before its callback succeeds makes that leak unretryable.
                try:
                    held.release_once()
                except Exception as exc:
                    raise ManagedSequenceServiceError("lease_context_release_failed", 503) from exc
                entry.held_materialization = None
            if child_transition is not None:
                try:
                    child_transition(entry.sequence, candidate, observed)
                except ManagedSequenceServiceError:
                    raise
                except Exception as exc:
                    raise ManagedSequenceServiceError("lease_child_transition_failed", 503) from exc
            if resets_bound_child:
                # CRITICAL: only exact owner-confirmed BOUND cleanup ends this authority.
                # Leaving the binding/flag strands replacement after cancel; clearing them before
                # the callback succeeds loses retry authority or ambiguous host ownership.
                entry.bound_child_binding = None
                entry.bound_child_cleanup_completed = False
            entry.sequence = candidate
            return self._result(entry)

    def _expire_pending_lease_transaction(
        self,
        parent_sequence_id: str,
        *,
        now: float,
        child_transition: Callable[[ManagedSequenceV1, ManagedSequenceV1, float], None] | None,
    ) -> None:
        _identifier(parent_sequence_id, "parent_sequence_id")
        with self._lock:
            entry = self._entry
            if entry is None or entry.sequence.authorization.sequence_id != parent_sequence_id:
                raise ManagedSequenceServiceError("parent_sequence_unavailable", 404)
            self._expire_lease_transaction(
                parent_sequence_id,
                entry.sequence.authorization.fingerprint,
                expected_revision=entry.sequence.revision,
                now=now,
                child_transition=child_transition,
            )

    def expire_lease(
        self,
        parent_sequence_id: str,
        authorization_fingerprint: str,
        *,
        expected_revision: int,
        now: float | None = None,
    ) -> ManagedSequenceMutationResultV1:
        return self._lease_transition_owner.apply(
            parent_sequence_id,
            authorization_fingerprint,
            expected_revision=expected_revision,
            now=now,
        )

    def read_projection(
        self,
        parent_sequence_id: str,
        read_authority_fingerprint: str,
        *,
        if_none_match: str | None = None,
    ) -> ManagedSequenceReadResultV1:
        _identifier(parent_sequence_id, "parent_sequence_id")
        _fingerprint(read_authority_fingerprint, "read_authority_fingerprint")
        if if_none_match is not None:
            _fingerprint(if_none_match, "if_none_match")
        # IMPORTANT: this path intentionally does not call clock, prune, Production, ManagedRun or
        # the mutation replay ledger. Polling must be a side-effect-free current projection read.
        with self._lock:
            entry = self._entry
            if entry is None or entry.sequence.authorization.sequence_id != parent_sequence_id:
                raise ManagedSequenceServiceError("parent_sequence_unavailable", 404)
            if entry.read_authority_fingerprint != read_authority_fingerprint:
                raise ManagedSequenceServiceError("read_authority_drift", 409)
            try:
                projection = read_managed_sequence_projection(entry.sequence)
            except ManagedSequenceError as exc:
                raise self._translate(exc) from exc
            if if_none_match == projection.etag:
                return ManagedSequenceReadResultV1(304, projection.etag, None)
            return ManagedSequenceReadResultV1(200, projection.etag, projection)


def dispatch_decoded_managed_sequence_action(
    service: ManagedSequenceService,
    decoded: DecodedManagedSequenceActionV1,
    *,
    qualification_claimant: Callable[
        [str], ManagedModeQualificationV1 | ManagedModeQualificationV2 | None
    ],
) -> ManagedSequenceMutationResultV1 | ManagedPreparedContextV1:
    if (
        type(service) is not ManagedSequenceService
        or type(decoded) is not DecodedManagedSequenceActionV1
    ):
        raise ManagedSequenceServiceError("managed_sequence_dispatch_type", 500)
    if not callable(qualification_claimant):
        raise ManagedSequenceServiceError("qualification_claimant_unavailable", 503)
    action = decoded.action
    request = decoded.request
    if (
        action == "read_prepared_child_context"
        and type(request) is ReadManagedPreparedContextRequestV1
    ):
        return service.read_prepared_child_context(request)
    if action == "authorize_sequence" and type(request) is AuthorizeManagedSequenceRequestV1:
        return service.authorize(request)
    if action == "start_sequence" and type(request) is StartManagedSequenceRequestV1:
        qualification_fingerprint = decoded.qualification_fingerprint
        if qualification_fingerprint is None:
            raise ManagedSequenceServiceError("qualification_claim_missing", 409)
        qualification = qualification_claimant(qualification_fingerprint)
        if (
            qualification is None
            or type(qualification) not in (ManagedModeQualificationV1, ManagedModeQualificationV2)
            or qualification.fingerprint != qualification_fingerprint
        ):
            raise ManagedSequenceServiceError("qualification_claim_drift", 409)
        return service.start(request, qualification=qualification)
    if action == "prepare_sequence_child" and type(request) is PrepareManagedSequenceChildRequestV1:
        return service.prepare_child(request)
    if action == "bind_prepared_child" and type(request) is BindManagedSequenceChildRequestV1:
        return service.bind_child(request)
    if action == "fail_bound_child" and type(request) is FailBoundManagedSequenceChildRequestV1:
        return service.fail_bound_child(request)
    if (
        action == "fail_prepared_child"
        and type(request) is FailPreparedManagedSequenceChildRequestV1
    ):
        return service.fail_prepared_child(request)
    if action == "record_submission" and type(request) is RecordManagedSequenceSubmissionRequestV1:
        return service.record_submission(request)
    if action == "record_running" and type(request) is RecordManagedSequenceRunningRequestV1:
        return service.record_running(request)
    if action == "record_artifact" and type(request) is RecordManagedSequenceArtifactRequestV1:
        return service.record_artifact(request)
    if action == "record_terminal" and type(request) is RecordManagedSequenceTerminalRequestV1:
        return service.record_terminal(request)
    if action == "record_reuse" and type(request) is RecordManagedSequenceReuseRequestV1:
        return service.record_reuse(request)
    if action == "detach_client" and type(request) is ManagedSequenceMutationAuthorityV1:
        return service.detach(request)
    if action == "resume_sequence" and type(request) is ResumeManagedSequenceRequestV1:
        return service.resume(request)
    if (
        action == "record_canvas_rollback"
        and type(request) is RecordManagedSequenceCanvasRollbackRequestV1
    ):
        return service.record_canvas_rollback(request)
    if (
        action == "mark_invocation_unknown"
        and type(request) is MarkManagedSequenceInvocationUnknownRequestV1
    ):
        return service.mark_invocation_unknown(request)
    if (
        action == "mark_unknown_ownership"
        and type(request) is UnknownManagedSequenceOwnershipRequestV1
    ):
        return service.mark_unknown_ownership(request)
    if action == "retry_segment" and type(request) is ManagedSequenceSegmentMutationRequestV1:
        return service.retry(request)
    if action == "cancel_sequence" and type(request) is ManagedSequenceMutationAuthorityV1:
        return service.cancel(request)
    raise ManagedSequenceServiceError("managed_sequence_dispatch_shape", 500)


def build_managed_mode_qualification_registry(
    *, production_registry: ManagedSequenceProductionRegistry | None = None
) -> ManagedModeQualificationRegistry:
    """Build the backend-only process qualification owner for the composition root."""

    from .comfyui_production_workspace import ProductionWorkspaceRegistry
    from .managed_mode_qualification import ManagedQualificationProducer

    # CRITICAL: keep construction in this adapter factory; inline root construction
    # bypasses factory substitution and lets the process qualification wiring drift.
    if production_registry is None:
        return ManagedModeQualificationRegistry()
    if not isinstance(production_registry, ProductionWorkspaceRegistry):
        raise ValueError("qualification production registry is invalid")
    return ManagedModeQualificationRegistry(
        producer=ManagedQualificationProducer(production_registry)
    )


def build_managed_sequence_service(
    *,
    production_registry: ManagedSequenceProductionRegistry,
    coordinator: object,
    qualification_guard: Callable[[ManagedModeQualificationV2], AbstractContextManager[None]]
    | None = None,
) -> ManagedSequenceService:
    """Build one parent service over the process's existing Production and child owners."""

    materialize = getattr(production_registry, "materialize_automatic_plan_segment", None)
    bind_child = getattr(coordinator, "bind_managed_sequence_child", None)
    reserve = getattr(coordinator, "reserve_managed_sequence_resources", None)
    publish_completion = getattr(coordinator, "publish_completed_managed_sequence", None)
    transition_child = getattr(coordinator, "transition_managed_sequence_lease", None)
    bind_lease_transition = getattr(
        coordinator,
        "bind_managed_sequence_lease_transition",
        None,
    )
    if any(
        not callable(dependency)
        for dependency in (
            materialize,
            bind_child,
            reserve,
            publish_completion,
            transition_child,
            bind_lease_transition,
        )
    ):
        raise ValueError("managed sequence composition dependency is invalid")

    materialize_impl = cast(
        Callable[[object, str], tuple[object, object]],
        materialize,
    )
    bind_child_impl = cast(
        Callable[[ManagedChildBindingRequestV1], ManagedChildBindingClaimV1],
        bind_child,
    )
    reserve_impl = cast(
        Callable[
            [ManagedSequenceStartResourceRequestV1],
            ManagedSequenceStartResourceClaimV1,
        ],
        reserve,
    )
    transition_child_impl = cast(
        Callable[[ManagedSequenceV1, ManagedSequenceV1, float], None],
        transition_child,
    )
    bind_lease_transition_impl = cast(
        Callable[[Callable[[str, float], None]], None],
        bind_lease_transition,
    )

    def materialize_segment(
        authority: object,
        segment_id: str,
    ) -> ManagedSegmentMaterializationClaimV1 | ManagedSegmentMaterializationClaimV2:
        raw_claim, receipt = materialize_impl(authority, segment_id)
        if (
            type(raw_claim) is not SegmentContextMaterializationClaim
            or type(receipt) is not SegmentContextMaterializationReceiptV1
            or receipt.segment_id != segment_id
        ):
            if type(raw_claim) is SegmentContextMaterializationClaim and not raw_claim.released:
                try:
                    raw_claim.release()
                except Exception as exc:
                    raise ManagedSequenceServiceError(
                        "materialization_claim_compensation_failed",
                        500,
                    ) from exc
            raise ManagedSequenceServiceError("materialization_claim_drift", 500)
        try:
            if type(authority) is ProductionAutomaticPlanAuthorityV2:
                snapshot = claim_canonical_production_materialization(raw_claim)
                return ManagedSegmentMaterializationClaimV2(
                    context_workspace_handle=raw_claim.context_workspace_handle,
                    segment_id=receipt.segment_id,
                    materialization_receipt_fingerprint=receipt.fingerprint,
                    receipt=receipt,
                    materialization=snapshot,
                    release=raw_claim.release,
                )
            return ManagedSegmentMaterializationClaimV1(
                context_workspace_handle=raw_claim.context_workspace_handle,
                segment_id=receipt.segment_id,
                materialization_receipt_fingerprint=receipt.fingerprint,
                release=raw_claim.release,
            )
        except Exception:
            try:
                raw_claim.release()
            except Exception as exc:
                raise ManagedSequenceServiceError(
                    "materialization_claim_compensation_failed",
                    500,
                ) from exc
            raise

    service = ManagedSequenceService(
        production_registry=production_registry,
        qualification_guard=qualification_guard,
        materialize_segment=materialize_segment,
        bind_child=bind_child_impl,
        reserve_start_resources=reserve_impl,
        publish_completion=cast(
            Callable[[object, str, ManagedSequenceV1], None], publish_completion
        ),
    )
    service._lease_transition_owner.bind_child_transition(transition_child_impl)
    bind_lease_transition_impl(service._lease_transition_owner.apply_pending)
    return service


_managed_service = component(MANAGED_SEQUENCE, ManagedSequenceService)
_managed_qualification = component(
    MANAGED_MODE_QUALIFICATION,
    ManagedModeQualificationRegistry,
)

_ACTION_POLICY = RoutePolicy(
    path=MANAGED_SEQUENCE_ACTION_ROUTE,
    owner=MANAGED_SEQUENCE_RESULT_SCHEMA,
    owner_attribute=_MANAGED_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_MANAGED_SEQUENCE_ACTION_BYTES,
    refusals=(ManagedSequenceServiceError,),
)

_PROJECTION_POLICY = RoutePolicy(
    path=MANAGED_SEQUENCE_PROJECTION_ROUTE,
    owner=MANAGED_SEQUENCE_READ_RESULT_SCHEMA,
    owner_attribute=_MANAGED_ROUTE_OWNER_ATTRIBUTE,
    method="GET",
    origin=OriginRule.EXACT_OR_ABSENT,
    refusals=(ManagedSequenceServiceError,),
)


def _managed_route_refusal_body(_status: int, reason: str) -> dict[str, str]:
    return {"error": reason}


def _managed_route_refusal(error: BaseException) -> RouteResult:
    if type(error) is not ManagedSequenceServiceError:
        return RouteResult(500, {"error": "internal_failure"})
    return RouteResult(error.status, {"error": error.code})


def _single_request_value(container: object, name: str) -> str | None:
    getall = getattr(container, "getall", None)
    values = getall(name, []) if callable(getall) else []
    if type(values) is not list or len(values) > 1:
        raise ManagedSequenceServiceError("managed_sequence_read_authority", 400)
    if not values:
        return None
    value = values[0]
    if type(value) is not str:
        raise ManagedSequenceServiceError("managed_sequence_read_authority", 400)
    return value


def _managed_projection_prelude(request: object) -> object:
    try:
        match_info = getattr(request, "match_info", None)
        get_match = getattr(match_info, "get", None)
        parent_sequence_id = get_match("parent_sequence_id") if callable(get_match) else None
        parent_sequence_id = _identifier(parent_sequence_id, "parent_sequence_id")
        read_authority = _single_request_value(
            getattr(request, "query", None),
            "read_authority_fingerprint",
        )
        if read_authority is None:
            raise ManagedSequenceServiceError("managed_sequence_read_authority", 400)
        read_authority = _fingerprint(read_authority, "read_authority_fingerprint")
        if_none_match = _single_request_value(
            getattr(request, "headers", None),
            "If-None-Match",
        )
        if if_none_match is not None:
            if_none_match = _fingerprint(if_none_match, "if_none_match")
        return parent_sequence_id, read_authority, if_none_match
    except ManagedSequenceServiceError as exc:
        return _managed_route_refusal(exc)


async def _apply_managed_sequence(payload: bytes, _context: object) -> RouteResult:
    decoded = decode_managed_sequence_action_json(payload)
    result = await asyncio.to_thread(
        dispatch_decoded_managed_sequence_action,
        _managed_service(),
        decoded,
        qualification_claimant=_managed_qualification().claim,
    )
    return RouteResult(200, result.to_wire())


async def _read_managed_sequence(_payload: bytes, context: object) -> RouteResult:
    if type(context) is not tuple or len(context) != 3:
        raise ManagedSequenceServiceError("managed_sequence_read_authority", 400)
    parent_sequence_id, read_authority, if_none_match = context
    result = await asyncio.to_thread(
        _managed_service().read_projection,
        parent_sequence_id,
        read_authority,
        if_none_match=if_none_match,
    )
    projection = result.projection
    wire = None
    if projection is not None:
        to_wire = getattr(projection, "to_wire", None)
        if not callable(to_wire):
            raise ManagedSequenceServiceError("managed_sequence_projection_type", 500)
        wire = to_wire()
    return RouteResult(result.status, wire, (("ETag", result.etag),))


def ensure_managed_sequence_route_registered() -> bool:
    """Register the exact action/projection pair without importing optional host modules."""

    global _ROUTE_REGISTERED
    found = host_web_and_routes()
    if found is None:
        return False
    _, routes = found
    for policy in (_ACTION_POLICY, _PROJECTION_POLICY):
        if not route_method_available(routes, policy):
            _ROUTE_REGISTERED = False
            return False
        if already_owned(routes, policy, __name__) is False:
            _ROUTE_REGISTERED = False
            return False
    action = register_owned_route(
        _ACTION_POLICY,
        __name__,
        _apply_managed_sequence,
        _managed_route_refusal_body,
        refusal_mapper=_managed_route_refusal,
    )
    projection = register_owned_route(
        _PROJECTION_POLICY,
        __name__,
        _read_managed_sequence,
        _managed_route_refusal_body,
        refusal_mapper=_managed_route_refusal,
        prelude=_managed_projection_prelude,
    )
    _ROUTE_REGISTERED = action and projection
    return _ROUTE_REGISTERED


__all__ = [
    "AuthorizeManagedSequenceRequestV1",
    "BindManagedSequenceChildRequestV1",
    "DecodedManagedSequenceActionV1",
    "MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA",
    "MANAGED_PREPARED_CONTEXT_SCHEMA",
    "MANAGED_SEQUENCE_ACTION_SCHEMA",
    "MANAGED_SEQUENCE_ACTION_ROUTE",
    "MANAGED_SEQUENCE_PROJECTION_ROUTE",
    "MAX_MANAGED_SEQUENCE_ACTION_BYTES",
    "MAX_MANAGED_PREPARED_CONTEXT_BYTES",
    "ManagedChildBindingClaimV1",
    "ManagedChildBindingRequestV1",
    "ManagedChildCorrelationV1",
    "ManagedInputGeometryIdentityV1",
    "ManagedModeQualificationRegistry",
    "ManagedPreparedGraphObservationV1",
    "ManagedPreparedContextV1",
    "ManagedSegmentMaterializationClaimV1",
    "ManagedSegmentMaterializationClaimV2",
    "ManagedSequenceMutationAuthorityV1",
    "ManagedSequenceMutationResultV1",
    "ManagedSequenceReadResultV1",
    "ManagedSequenceService",
    "ManagedSequenceServiceError",
    "ManagedSequenceStartResourceClaimV1",
    "ManagedSequenceStartResourceRequestV1",
    "PrepareManagedSequenceChildRequestV1",
    "ReadManagedPreparedContextRequestV1",
    "RecordManagedSequenceArtifactRequestV1",
    "RecordManagedSequenceCanvasRollbackRequestV1",
    "RecordManagedSequenceReuseRequestV1",
    "RecordManagedSequenceRunningRequestV1",
    "RecordManagedSequenceSubmissionRequestV1",
    "RecordManagedSequenceTerminalRequestV1",
    "ResumeManagedSequenceRequestV1",
    "StartManagedSequenceRequestV1",
    "UnknownManagedSequenceOwnershipRequestV1",
    "ManagedSequenceSegmentMutationRequestV1",
    "build_managed_sequence_authorization",
    "build_managed_mode_qualification_registry",
    "build_managed_sequence_service",
    "decode_managed_sequence_action_json",
    "dispatch_decoded_managed_sequence_action",
    "ensure_managed_sequence_route_registered",
]
