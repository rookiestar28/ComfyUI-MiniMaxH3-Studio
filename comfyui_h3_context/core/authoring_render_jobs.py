"""Pure Authoring render request identities, budgets and immutable job transitions.

These values grant no source or executable authority. The service must separately admit the
factory-backed plan, qualified runtime and job-owned leases before allocating or starting work.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields, replace
from enum import Enum
from typing import Final, cast

from .canonical import canonical_fingerprint
from .render_planner import RenderPlanV1

RENDER_JOB_REQUEST_SCHEMA: Final = "h3.authoring.render_job_request.v1"
RENDER_JOB_STATE_SCHEMA: Final = "h3.authoring.render_job_state.v1"
SEMANTIC_RENDER_SCHEMA: Final = "h3.authoring.semantic_render_input.v1"

_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,127}\Z")
_JOB_ID = re.compile(r"render-[0-9a-f]{32}\Z")


class RenderJobError(ValueError):
    """A closed diagnostic; arbitrary caller values never enter the exception."""

    def __init__(self, code: str) -> None:
        if code not in {
            "invalid_request",
            "plan_mismatch",
            "invalid_limits",
            "invalid_state",
            "invalid_transition",
            "version_conflict",
            "terminal_immutable",
        }:
            code = "invalid_request"
        self.code = code
        super().__init__(code)


def _integer(value: object, maximum: int, *, minimum: int = 1) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderJobLimits:
    max_jobs: int = 32
    running_jobs: int = 1
    queue_depth: int = 8
    workspace_queue_depth: int = 2
    deadline_ms: int = 900_000
    source_lease_seconds: int = 900
    max_source_bytes: int = 64 * 1024 * 1024
    max_source_set_bytes: int = 256 * 1024 * 1024
    max_source_duration_ms: int = 30_000
    child_processes: int = 1
    child_threads: int = 2
    child_memory_bytes: int = 1024 * 1024 * 1024
    child_cpu_seconds: int = 600
    max_staging_bytes: int = 1024 * 1024 * 1024
    max_output_bytes: int = 512 * 1024 * 1024
    retained_outputs: int = 16
    retained_output_bytes: int = 2 * 1024 * 1024 * 1024
    retention_seconds: int = 3600
    max_receipt_bytes: int = 65_536
    max_probe_bytes: int = 65_536
    cleanup_grace_seconds: int = 5

    def __post_init__(self) -> None:
        for field in fields(self):
            if not _integer(getattr(self, field.name), cast(int, field.default)):
                raise RenderJobError("invalid_limits")
        if (
            self.workspace_queue_depth > self.queue_depth
            or self.queue_depth + self.running_jobs > self.max_jobs
            or self.max_source_bytes > self.max_source_set_bytes
            or self.max_output_bytes > self.max_staging_bytes
            or self.max_output_bytes > self.retained_output_bytes
        ):
            raise RenderJobError("invalid_limits")


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthoringRenderJobRequestV1:
    schema: str
    workspace_handle: str
    workspace_revision: int
    timeline_revision: int
    snapshot_fingerprint: str
    plan_fingerprint: str
    source_manifest_fingerprint: str
    font_facts_fingerprint: str
    font_package_fingerprint: str
    renderer_profile_fingerprint: str
    currentness_fingerprint: str
    output_profile_fingerprint: str
    idempotency_key: str
    timeout_ms: int

    def __post_init__(self) -> None:
        if (
            self.schema != RENDER_JOB_REQUEST_SCHEMA
            or not _matches(_IDENTIFIER, self.workspace_handle)
            or not _integer(self.workspace_revision, 1_000_000, minimum=0)
            or not _integer(self.timeline_revision, 1_000_000, minimum=0)
            or not _matches(_IDEMPOTENCY_KEY, self.idempotency_key)
            or not _integer(self.timeout_ms, 900_000)
        ):
            raise RenderJobError("invalid_request")
        for field in fields(self):
            if field.name.endswith("_fingerprint") and not _matches(
                _FINGERPRINT, getattr(self, field.name)
            ):
                raise RenderJobError("invalid_request")

    def to_wire(self) -> dict[str, object]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def decode_render_job_request(value: object) -> AuthoringRenderJobRequestV1:
    """A closed internal wire boundary, not a public route or a source-claim decoder."""

    if type(value) is not dict:
        raise RenderJobError("invalid_request")
    wire = cast(dict[str, object], value)
    if set(wire) != {field.name for field in fields(AuthoringRenderJobRequestV1)}:
        raise RenderJobError("invalid_request")
    return AuthoringRenderJobRequestV1(
        schema=cast(str, wire["schema"]),
        workspace_handle=cast(str, wire["workspace_handle"]),
        workspace_revision=cast(int, wire["workspace_revision"]),
        timeline_revision=cast(int, wire["timeline_revision"]),
        snapshot_fingerprint=cast(str, wire["snapshot_fingerprint"]),
        plan_fingerprint=cast(str, wire["plan_fingerprint"]),
        source_manifest_fingerprint=cast(str, wire["source_manifest_fingerprint"]),
        font_facts_fingerprint=cast(str, wire["font_facts_fingerprint"]),
        font_package_fingerprint=cast(str, wire["font_package_fingerprint"]),
        renderer_profile_fingerprint=cast(str, wire["renderer_profile_fingerprint"]),
        currentness_fingerprint=cast(str, wire["currentness_fingerprint"]),
        output_profile_fingerprint=cast(str, wire["output_profile_fingerprint"]),
        idempotency_key=cast(str, wire["idempotency_key"]),
        timeout_ms=cast(int, wire["timeout_ms"]),
    )


def request_for_render_plan(
    plan: RenderPlanV1, *, idempotency_key: str, timeout_ms: int
) -> AuthoringRenderJobRequestV1:
    """Describe a plan, without asserting that its renderer is qualified or its sources live."""

    if type(plan) is not RenderPlanV1:
        raise RenderJobError("invalid_request")
    return AuthoringRenderJobRequestV1(
        schema=RENDER_JOB_REQUEST_SCHEMA,
        workspace_handle=plan.source_currentness_claim.workspace_handle,
        workspace_revision=plan.source_currentness_claim.workspace_revision,
        timeline_revision=plan.snapshot_revision,
        snapshot_fingerprint=plan.snapshot_fingerprint,
        plan_fingerprint=render_plan_fingerprint(plan),
        source_manifest_fingerprint=plan.source_manifest_fingerprint,
        font_facts_fingerprint=plan.font_facts_fingerprint,
        font_package_fingerprint=plan.font_package_fingerprint,
        renderer_profile_fingerprint=plan.renderer_capability_profile_fingerprint,
        currentness_fingerprint=canonical_fingerprint(plan.source_currentness_claim.to_wire()),
        output_profile_fingerprint=canonical_fingerprint(plan.output_profile.to_wire()),
        idempotency_key=idempotency_key,
        timeout_ms=timeout_ms,
    )


def render_plan_fingerprint(plan: RenderPlanV1) -> str:
    if type(plan) is not RenderPlanV1 or not 1 <= len(plan.resolved_operations) <= 256:
        raise RenderJobError("invalid_request")
    material = plan.to_wire()
    # CRITICAL: accepted plans can exceed generic canonical's1MB bound. Join each bounded
    # operation chunk's actual wire values, never just trust its supplied chunk fingerprint.
    material["resolved_operations"] = [
        {
            "claimed": chunk.chunk_fingerprint,
            "observed": canonical_fingerprint(chunk.fingerprint_material()),
        }
        for chunk in plan.resolved_operations
    ]
    return canonical_fingerprint({"schema": "h3.authoring.render_plan_join.v1", "plan": material})


def require_request_matches_plan(request: AuthoringRenderJobRequestV1, plan: RenderPlanV1) -> None:
    if type(request) is not AuthoringRenderJobRequestV1:
        raise RenderJobError("invalid_request")
    # CRITICAL: compare every join, not just the plan digest. Otherwise a correct digest can
    # carry a foreign workspace/revision or source claim into a later status/receipt consumer.
    expected = request_for_render_plan(
        plan, idempotency_key=request.idempotency_key, timeout_ms=request.timeout_ms
    )
    if request != expected:
        raise RenderJobError("plan_mismatch")


def semantic_render_fingerprint(plan: RenderPlanV1) -> str:
    """Pixel/audio input identity only; not a validator, job key, or execution authority.

    Revision, selection, lock and lease renewal may change provenance without changing media.
    Hash resolved operation values rather than a potentially stale caller-supplied chunk digest.
    """

    if type(plan) is not RenderPlanV1:
        raise RenderJobError("invalid_request")
    frames = [frame for chunk in plan.resolved_operations for frame in chunk.frames]
    if not 1 <= len(frames) <= 3600:
        raise RenderJobError("invalid_request")
    active_ids = {layer.asset_id for frame in frames for layer in frame.layers}
    active_ids.update(frame.audio_span.asset_id for frame in frames if frame.audio_span is not None)
    return canonical_fingerprint(
        {
            "schema": SEMANTIC_RENDER_SCHEMA,
            # CRITICAL: the accepted NLE has3600frames but generic canonical arrays stop
            # at256items. Fixed chunks preserve every ordered frame without weakening that cap.
            "frame_chunks": [
                canonical_fingerprint(
                    [
                        canonical_fingerprint(frame.to_wire())
                        for frame in frames[start : start + 128]
                    ]
                )
                for start in range(0, len(frames), 128)
            ],
            "sources": [
                {
                    "asset_id": source.asset_id,
                    "content": source.source_fingerprint,
                    "profile": source.source_profile_fingerprint,
                    "color": source.color_facts_fingerprint,
                    "width": source.width,
                    "height": source.height,
                }
                for source in sorted(plan.source_bindings, key=lambda row: row.asset_id)
                if source.asset_id in active_ids
            ],
            "font_package": plan.font_package_fingerprint,
            "font_facts": plan.font_facts_fingerprint,
            "renderer_profile": plan.renderer_capability_profile_fingerprint,
            "output": plan.output_profile.to_wire(),
            "emits_audio": plan.emits_audio_stream,
            # GUARD: a clip's audio adjustments are not in the resolved frames, yet they change
            # the audio a render produces, so they are part of this identity; without them two
            # renders with different gains would claim the same media. Present only when a clip
            # carries one, so a plan without adjustments keeps its identity.
            **(
                {"clip_audio": [row.to_wire() for row in plan.clip_audio]}
                if plan.clip_audio
                else {}
            ),
        }
    )


class RenderJobPhase(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    PREPARING = "preparing"
    RENDERING = "rendering"
    ENCODING = "encoding"
    MUXING = "muxing"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RenderJobFailure(str, Enum):
    CANCELLED = "cancelled"
    WORKSPACE_RELEASED = "workspace_released"
    SOURCE_REPLACED = "source_replaced"
    SOURCE_EXPIRED = "source_expired"
    SOURCE_UNAVAILABLE = "source_unavailable"
    FONT_CHANGED = "font_changed"
    DEADLINE = "deadline"
    RESOURCE_LIMIT = "resource_limit"
    PROCESS_FAILED = "process_failed"
    OUTPUT_INVALID = "output_invalid"
    RECEIPT_INVALID = "receipt_invalid"
    STORE_UNAVAILABLE = "store_unavailable"
    SERVICE_CLOSED = "service_closed"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"


_TERMINAL: Final = frozenset(
    {RenderJobPhase.SUCCEEDED, RenderJobPhase.FAILED, RenderJobPhase.CANCELLED}
)
_PHASE_RANK: Final = {phase: rank for rank, phase in enumerate(RenderJobPhase)}


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderJobState:
    job_id: str
    request_fingerprint: str
    version: int = 0
    phase: RenderJobPhase = RenderJobPhase.QUEUED
    progress_bp: int = 0
    failure: RenderJobFailure | None = None
    receipt_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if (
            not _matches(_JOB_ID, self.job_id)
            or not _matches(_FINGERPRINT, self.request_fingerprint)
            or not _integer(self.version, 1_000_000, minimum=0)
            or type(self.phase) is not RenderJobPhase
            or not _integer(self.progress_bp, 10_000, minimum=0)
            or (self.failure is not None and type(self.failure) is not RenderJobFailure)
            or (
                self.receipt_fingerprint is not None
                and not _matches(_FINGERPRINT, self.receipt_fingerprint)
            )
        ):
            raise RenderJobError("invalid_state")
        if self.phase is RenderJobPhase.SUCCEEDED:
            valid = (
                self.failure is None
                and self.receipt_fingerprint is not None
                and self.progress_bp == 10_000
            )
        elif self.phase is RenderJobPhase.FAILED:
            valid = (
                self.failure not in (None, RenderJobFailure.CANCELLED)
                and self.receipt_fingerprint is None
            )
        elif self.phase is RenderJobPhase.CANCELLED:
            valid = self.failure is RenderJobFailure.CANCELLED and self.receipt_fingerprint is None
        else:
            valid = (
                self.failure is None
                and self.receipt_fingerprint is None
                and self.progress_bp < 10_000
            )
        if not valid:
            raise RenderJobError("invalid_state")

    @classmethod
    def queued(cls, job_id: str, request_fingerprint: str) -> RenderJobState:
        return cls(job_id=job_id, request_fingerprint=request_fingerprint)

    @property
    def terminal(self) -> bool:
        return self.phase in _TERMINAL

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": RENDER_JOB_STATE_SCHEMA,
            "job_id": self.job_id,
            "request_fingerprint": self.request_fingerprint,
            "version": self.version,
            "phase": self.phase.value,
            "progress_bp": self.progress_bp,
            "failure": None if self.failure is None else self.failure.value,
            "receipt_fingerprint": self.receipt_fingerprint,
        }


def advance_render_job(
    state: RenderJobState,
    *,
    expected_version: int,
    phase: RenderJobPhase,
    progress_bp: int | None = None,
    failure: RenderJobFailure | None = None,
    receipt_fingerprint: str | None = None,
) -> RenderJobState:
    if type(state) is not RenderJobState or type(phase) is not RenderJobPhase:
        raise RenderJobError("invalid_transition")
    if type(expected_version) is not int or expected_version != state.version:
        raise RenderJobError("version_conflict")
    progress = state.progress_bp if progress_bp is None else progress_bp
    if phase is RenderJobPhase.SUCCEEDED and progress_bp is None:
        progress = 10_000
    if state.terminal:
        # CRITICAL: terminal replay is a no-op only when all established fields match. A late
        # renderer callback must never replace cancellation or bind a second output receipt.
        if (phase, progress, failure, receipt_fingerprint) == (
            state.phase,
            state.progress_bp,
            state.failure,
            state.receipt_fingerprint,
        ):
            return state
        raise RenderJobError("terminal_immutable")
    if (
        not _integer(progress, 10_000, minimum=state.progress_bp)
        or _PHASE_RANK[phase] < _PHASE_RANK[state.phase]
        or (phase is RenderJobPhase.SUCCEEDED and state.phase is not RenderJobPhase.VALIDATING)
    ):
        raise RenderJobError("invalid_transition")
    try:
        return replace(
            state,
            version=state.version + 1,
            phase=phase,
            progress_bp=progress,
            failure=failure,
            receipt_fingerprint=receipt_fingerprint,
        )
    except RenderJobError:
        raise RenderJobError("invalid_transition") from None
