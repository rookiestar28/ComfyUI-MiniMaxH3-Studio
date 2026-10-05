"""Deterministic visible-native generation sequence authority.

This pure module decides which immutable segment job may be projected next and validates exact
host/artifact observations. It never imports ComfyUI, queues a graph, reads media, opens an
artifact, or lets a browser infer dependency, retry, or completion truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from enum import Enum

from .canonical import canonical_bytes, canonical_fingerprint
from .contracts import TaskMode
from .pipeline_transaction import (
    MAX_PIPELINE_TRANSACTION_ATTEMPTS,
    PipelineTransaction,
    PipelineTransactionState,
    cancel_pipeline_transaction,
    mark_pipeline_unknown_ownership,
    record_pipeline_result,
    record_pipeline_running,
    record_pipeline_submission,
)
from .recompute_closure import RecomputeDisposition, RecomputePlan
from .segment_artifacts import (
    MAX_SEGMENT_ARTIFACT_DIMENSION,
    MAX_SEGMENT_ARTIFACT_DIMENSIONS,
    ArtifactLifecycleState,
    SegmentArtifactReceipt,
)
from .segment_workspace import (
    MAX_WORKSPACE_SEGMENTS,
    MAX_WORKSPACE_WIRE_BYTES,
    MultiSegmentWorkspace,
    SegmentContextManifest,
    SegmentDuration,
    derive_segment_manifests,
)
from .ui_projection import ExecutionCorrelation

GENERATION_SEQUENCE_PLAN_SCHEMA = "h3.context.generation_sequence_plan.v1"
GENERATION_SEQUENCE_STATE_SCHEMA = "h3.context.generation_sequence_state.v1"
GENERATION_SEQUENCE_PROJECTION_SCHEMA = "h3.context.generation_sequence_projection.v1"
MAX_GENERATION_SEQUENCE_CONCURRENCY = 4
MAX_GENERATION_JOB_TIMEOUT_MILLISECONDS = 86_400_000
MAX_GENERATION_SEQUENCE_OBSERVATIONS = 4_096

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class GenerationSequenceError(ValueError):
    """Raised when generation scheduling or host truth cannot remain exact."""


class FingerprintDomain(str, Enum):
    """Which graph `graph_fingerprint` and `compiled_prompt_fingerprint` cover.

    `M17-20` `D6`. Before that item a job's graph identity covered the context
    subgraph alone, because that was the only thing App Mode materialized. It now
    covers the full output-producing graph -- sampler, decode and artifact sink
    included -- so the same segment yields a different fingerprint than it did
    before, and an observation carrying the older one is not a mismatch to
    reconcile but a different domain.

    The domain is therefore declared rather than inferred. `M18-02` separates
    fingerprint domains further; declaring it here means that item does not have
    to guess which fingerprints `M17-20` widened. It covers exactly the two
    fingerprints named above: `model_fingerprint` and `runtime_fingerprint` are
    untouched by this widening and are not in its scope.
    """

    CONTEXT_SUBGRAPH = "context_subgraph"
    OUTPUT_PRODUCING_GRAPH = "output_producing_graph"


class GenerationJobState(str, Enum):
    PLANNED = "planned"
    PROJECTED = "projected"
    SUBMITTED = "submitted"
    RUNNING = "running"
    OUTPUT_VERIFICATION_FAILED = "output_verification_failed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN_OWNERSHIP = "unknown_ownership"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise GenerationSequenceError(f"bounded_identifier:{field}")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise GenerationSequenceError(f"sha256_fingerprint:{field}")
    return value


def _optional_fingerprint(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _fingerprint(value, field)


def _positive_int(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise GenerationSequenceError(field)
    return value


def _fingerprints(values: object, field: str) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > MAX_WORKSPACE_SEGMENTS:
        raise GenerationSequenceError(field)
    result = tuple(_fingerprint(value, field) for value in values)
    if len(result) != len(set(result)):
        raise GenerationSequenceError(f"duplicate_{field}")
    return result


def _identifiers(values: object, field: str) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > MAX_WORKSPACE_SEGMENTS:
        raise GenerationSequenceError(field)
    result = tuple(_identifier(value, field) for value in values)
    if len(result) != len(set(result)):
        raise GenerationSequenceError(f"duplicate_{field}")
    return result


@dataclass(frozen=True, slots=True)
class GenerationJobSpec:
    """Caller-supplied content-free identity for one already-qualified visible graph route."""

    segment_id: str
    job_id: str
    graph_fingerprint: str
    compiled_prompt_fingerprint: str
    model_fingerprint: str
    runtime_fingerprint: str
    expected_format: str
    expected_shape: tuple[int, ...]
    timeout_ms: int
    fingerprint_domain: FingerprintDomain

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "job_spec.segment_id")
        _identifier(self.job_id, "job_spec.job_id")
        if type(self.fingerprint_domain) is not FingerprintDomain:
            raise GenerationSequenceError("job_spec.fingerprint_domain")
        _fingerprint(self.graph_fingerprint, "job_spec.graph")
        _fingerprint(self.compiled_prompt_fingerprint, "job_spec.compiled_prompt")
        _fingerprint(self.model_fingerprint, "job_spec.model")
        _fingerprint(self.runtime_fingerprint, "job_spec.runtime")
        _identifier(self.expected_format, "job_spec.expected_format")
        if (
            type(self.expected_shape) is not tuple
            or not 1 <= len(self.expected_shape) <= MAX_SEGMENT_ARTIFACT_DIMENSIONS
        ):
            raise GenerationSequenceError("job_shape")
        for shape_value in self.expected_shape:
            _positive_int(shape_value, "job_shape", MAX_SEGMENT_ARTIFACT_DIMENSION)
        _positive_int(
            self.timeout_ms,
            "job_timeout",
            MAX_GENERATION_JOB_TIMEOUT_MILLISECONDS,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "job_id": self.job_id,
            "graph_fingerprint": self.graph_fingerprint,
            "compiled_prompt_fingerprint": self.compiled_prompt_fingerprint,
            "model_fingerprint": self.model_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "expected_format": self.expected_format,
            "expected_shape": list(self.expected_shape),
            "timeout_ms": self.timeout_ms,
            "fingerprint_domain": self.fingerprint_domain.value,
        }


@dataclass(frozen=True, slots=True)
class GenerationSequenceJob:
    """One immutable dirty-segment command; it contains no prompt, media, locator, or credential."""

    job_id: str
    segment_id: str
    ordinal: int
    task_mode: TaskMode
    source_id: str
    reference_ids: tuple[str, ...]
    duration: SegmentDuration
    dependency_segment_ids: tuple[str, ...]
    disposition: RecomputeDisposition
    reason_codes: tuple[str, ...]
    triggering_segment_ids: tuple[str, ...]
    manifest_fingerprint: str
    producer_fingerprint: str
    native_binding_fingerprint: str
    settings_fingerprint: str
    graph_fingerprint: str
    compiled_prompt_fingerprint: str
    model_fingerprint: str
    runtime_fingerprint: str
    expected_format: str
    expected_shape: tuple[int, ...]
    timeout_ms: int
    fingerprint_domain: FingerprintDomain
    predecessor_receipt_fingerprint: str | None = None
    predecessor_artifact_fingerprint: str | None = None
    state: GenerationJobState = GenerationJobState.PLANNED

    def __post_init__(self) -> None:
        _identifier(self.job_id, "job.job_id")
        _identifier(self.segment_id, "job.segment_id")
        _positive_int(self.ordinal, "job.ordinal", MAX_WORKSPACE_SEGMENTS)
        if type(self.task_mode) is not TaskMode:
            raise GenerationSequenceError("job_task_mode")
        _identifier(self.source_id, "job.source_id")
        _identifiers(self.reference_ids, "job.reference_ids")
        if type(self.duration) is not SegmentDuration:
            raise GenerationSequenceError("job_duration")
        _identifiers(self.dependency_segment_ids, "job.dependencies")
        if (
            type(self.disposition) is not RecomputeDisposition
            or self.disposition is RecomputeDisposition.CLEAN
        ):
            raise GenerationSequenceError("job_disposition")
        _identifiers(self.reason_codes, "job.reason_codes")
        _identifiers(self.triggering_segment_ids, "job.triggering_segment_ids")
        for value, field in (
            (self.manifest_fingerprint, "job.manifest"),
            (self.producer_fingerprint, "job.producer"),
            (self.native_binding_fingerprint, "job.native_binding"),
            (self.settings_fingerprint, "job.settings"),
            (self.graph_fingerprint, "job.graph"),
            (self.compiled_prompt_fingerprint, "job.compiled_prompt"),
            (self.model_fingerprint, "job.model"),
            (self.runtime_fingerprint, "job.runtime"),
        ):
            _fingerprint(value, field)
        _identifier(self.expected_format, "job.expected_format")
        if type(self.fingerprint_domain) is not FingerprintDomain:
            raise GenerationSequenceError("job.fingerprint_domain")
        if type(self.expected_shape) is not tuple:
            raise GenerationSequenceError("job_shape")
        for shape_value in self.expected_shape:
            _positive_int(shape_value, "job_shape", MAX_SEGMENT_ARTIFACT_DIMENSION)
        _positive_int(self.timeout_ms, "job_timeout", MAX_GENERATION_JOB_TIMEOUT_MILLISECONDS)
        _optional_fingerprint(self.predecessor_receipt_fingerprint, "job.predecessor_receipt")
        _optional_fingerprint(self.predecessor_artifact_fingerprint, "job.predecessor_artifact")
        if (self.predecessor_receipt_fingerprint is None) != (
            self.predecessor_artifact_fingerprint is None
        ):
            raise GenerationSequenceError("predecessor_receipt_pair")
        if (
            type(self.state) is not GenerationJobState
            or self.state is not GenerationJobState.PLANNED
        ):
            raise GenerationSequenceError("planned_job_state")

    def to_wire(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "task_mode": self.task_mode.value,
            "source_id": self.source_id,
            "reference_ids": list(self.reference_ids),
            "duration": self.duration.to_wire(),
            "dependency_segment_ids": list(self.dependency_segment_ids),
            "disposition": self.disposition.value,
            "reason_codes": list(self.reason_codes),
            "triggering_segment_ids": list(self.triggering_segment_ids),
            "manifest_fingerprint": self.manifest_fingerprint,
            "producer_fingerprint": self.producer_fingerprint,
            "native_binding_fingerprint": self.native_binding_fingerprint,
            "settings_fingerprint": self.settings_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "compiled_prompt_fingerprint": self.compiled_prompt_fingerprint,
            "model_fingerprint": self.model_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "expected_format": self.expected_format,
            "expected_shape": list(self.expected_shape),
            "timeout_ms": self.timeout_ms,
            "fingerprint_domain": self.fingerprint_domain.value,
            "predecessor_receipt_fingerprint": self.predecessor_receipt_fingerprint,
            "predecessor_artifact_fingerprint": self.predecessor_artifact_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class GenerationSequencePlan:
    sequence_id: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    recompute_plan_fingerprint: str
    manifest_fingerprints: tuple[str, ...]
    jobs: tuple[GenerationSequenceJob, ...]
    clean_segment_ids: tuple[str, ...]
    max_concurrency: int = 1
    sequence_fingerprint: str | None = None
    schema: str = GENERATION_SEQUENCE_PLAN_SCHEMA
    source_workspace_authority: MultiSegmentWorkspace | None = dataclass_field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.schema != GENERATION_SEQUENCE_PLAN_SCHEMA:
            raise GenerationSequenceError("unsupported_sequence_plan_schema")
        _identifier(self.sequence_id, "sequence_id")
        _identifier(self.workspace_id, "workspace_id")
        _positive_int(self.workspace_revision, "workspace_revision", 1_000_000)
        _fingerprint(self.workspace_fingerprint, "workspace")
        _fingerprint(self.recompute_plan_fingerprint, "recompute_plan")
        _fingerprints(self.manifest_fingerprints, "manifest_fingerprints")
        if (
            type(self.jobs) is not tuple
            or len(self.jobs) > MAX_WORKSPACE_SEGMENTS
            or not all(type(item) is GenerationSequenceJob for item in self.jobs)
        ):
            raise GenerationSequenceError("sequence_jobs")
        job_ids = tuple(item.job_id for item in self.jobs)
        segment_ids = tuple(item.segment_id for item in self.jobs)
        if len(job_ids) != len(set(job_ids)) or len(segment_ids) != len(set(segment_ids)):
            raise GenerationSequenceError("duplicate_sequence_job")
        if tuple(item.ordinal for item in self.jobs) != tuple(
            sorted(item.ordinal for item in self.jobs)
        ):
            raise GenerationSequenceError("sequence_job_order")
        clean = _identifiers(self.clean_segment_ids, "clean_segment_ids")
        if set(clean) & set(segment_ids):
            raise GenerationSequenceError("clean_dirty_overlap")
        self._validate_source_workspace_authority(clean, segment_ids)
        _positive_int(
            self.max_concurrency,
            "max_concurrency",
            MAX_GENERATION_SEQUENCE_CONCURRENCY,
        )
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.sequence_fingerprint is None:
            object.__setattr__(self, "sequence_fingerprint", expected)
        elif self.sequence_fingerprint != expected:
            raise GenerationSequenceError("sequence_fingerprint_mismatch")
        if len(canonical_bytes(self.to_wire())) > MAX_WORKSPACE_WIRE_BYTES:
            raise GenerationSequenceError("sequence_wire_limit")

    def _validate_source_workspace_authority(
        self,
        clean_segment_ids: tuple[str, ...],
        job_segment_ids: tuple[str, ...],
    ) -> None:
        source = self.source_workspace_authority
        if source is None:
            return
        if type(source) is not MultiSegmentWorkspace:
            raise GenerationSequenceError("source_workspace_authority_type")
        manifests = derive_segment_manifests(source)
        source_segment_ids = tuple(item.segment_id for item in source.segments)
        dirty = set(job_segment_ids)
        expected_job_ids = tuple(item for item in source_segment_ids if item in dirty)
        expected_clean_ids = tuple(item for item in source_segment_ids if item not in dirty)
        if (
            source.workspace_id != self.workspace_id
            or source.revision != self.workspace_revision
            or source.fingerprint != self.workspace_fingerprint
            or tuple(item.fingerprint for item in manifests) != self.manifest_fingerprints
            or set(clean_segment_ids) | set(job_segment_ids) != set(source_segment_ids)
            or len(clean_segment_ids) + len(job_segment_ids) != len(source_segment_ids)
            or job_segment_ids != expected_job_ids
            or clean_segment_ids != expected_clean_ids
        ):
            raise GenerationSequenceError("source_workspace_authority_mismatch")
        manifest_by_id = {item.segment_id: item for item in manifests}
        for job in self.jobs:
            manifest = manifest_by_id.get(job.segment_id)
            if manifest is None or (
                job.ordinal != manifest.ordinal
                or job.task_mode is not manifest.task_mode
                or job.source_id != manifest.source_id
                or job.reference_ids != manifest.reference_ids
                or job.duration != manifest.duration
                or job.dependency_segment_ids
                != tuple(item for item in manifest.dependency_segment_ids if item in dirty)
                or job.manifest_fingerprint != manifest.fingerprint
                or job.producer_fingerprint != manifest.producer_fingerprint
                or job.native_binding_fingerprint != manifest.native_binding_fingerprint
                or job.settings_fingerprint != manifest.producer_settings_fingerprint
            ):
                raise GenerationSequenceError("source_workspace_authority_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.sequence_fingerprint is None:  # pragma: no cover
            raise GenerationSequenceError("sequence_fingerprint_uninitialized")
        return self.sequence_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "sequence_id": self.sequence_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "recompute_plan_fingerprint": self.recompute_plan_fingerprint,
            "manifest_fingerprints": list(self.manifest_fingerprints),
            "jobs": [item.to_wire() for item in self.jobs],
            "clean_segment_ids": list(self.clean_segment_ids),
            "max_concurrency": self.max_concurrency,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["sequence_fingerprint"] = self.fingerprint
        return value

    def to_public_dict(self) -> dict[str, object]:
        return self.to_wire()

    def job_for_segment(self, segment_id: str) -> GenerationSequenceJob:
        _identifier(segment_id, "segment_id")
        for job in self.jobs:
            if job.segment_id == segment_id:
                return job
        raise GenerationSequenceError("unknown_segment_job")

    def job_for_id(self, job_id: str) -> GenerationSequenceJob:
        _identifier(job_id, "job_id")
        for job in self.jobs:
            if job.job_id == job_id:
                return job
        raise GenerationSequenceError("unknown_job")


def _compatible_receipt(
    receipt: SegmentArtifactReceipt,
    manifest: SegmentContextManifest,
) -> bool:
    return (
        receipt.state is ArtifactLifecycleState.COMPLETE
        and receipt.workspace_id == manifest.workspace_id
        and receipt.segment_id == manifest.segment_id
        and receipt.producer_fingerprint == manifest.producer_fingerprint
        and receipt.native_binding_fingerprint == manifest.native_binding_fingerprint
        and receipt.settings_fingerprint == manifest.producer_settings_fingerprint
        and receipt.source_id == manifest.source_id
        and receipt.output_fingerprint is not None
    )


def build_generation_sequence_plan(
    workspace: MultiSegmentWorkspace,
    manifests: tuple[SegmentContextManifest, ...],
    recompute_plan: RecomputePlan,
    job_specs: tuple[GenerationJobSpec, ...],
    *,
    reusable_receipts: tuple[SegmentArtifactReceipt, ...] = (),
    max_concurrency: int = 1,
    sequence_id: str | None = None,
) -> GenerationSequencePlan:
    """Build an exact ordered job plan before any host-visible action occurs."""

    if type(workspace) is not MultiSegmentWorkspace:
        raise GenerationSequenceError("workspace_type")
    if type(manifests) is not tuple or not all(
        type(item) is SegmentContextManifest for item in manifests
    ):
        raise GenerationSequenceError("manifest_type")
    expected_manifests = derive_segment_manifests(workspace)
    if tuple(item.fingerprint for item in manifests) != tuple(
        item.fingerprint for item in expected_manifests
    ):
        raise GenerationSequenceError("manifest_workspace_mismatch")
    if type(recompute_plan) is not RecomputePlan:
        raise GenerationSequenceError("recompute_plan_type")
    if not recompute_plan.selection_safe:
        raise GenerationSequenceError("unsafe_recompute_selection")
    decision_ids = tuple(item.segment_id for item in recompute_plan.decisions)
    manifest_ids = tuple(item.segment_id for item in manifests)
    if decision_ids != manifest_ids:
        raise GenerationSequenceError("recompute_manifest_mismatch")
    if recompute_plan.requires_full_recompute or any(
        item.disposition
        in {
            RecomputeDisposition.BLOCKED_MISSING_PREDECESSOR,
            RecomputeDisposition.REQUIRES_FULL_RECOMPUTE,
        }
        for item in recompute_plan.decisions
    ):
        raise GenerationSequenceError("blocked_recompute_plan")
    _positive_int(max_concurrency, "max_concurrency", MAX_GENERATION_SEQUENCE_CONCURRENCY)
    if type(job_specs) is not tuple or not all(
        type(item) is GenerationJobSpec for item in job_specs
    ):
        raise GenerationSequenceError("job_specs")
    specs = {item.segment_id: item for item in job_specs}
    if len(specs) != len(job_specs):
        raise GenerationSequenceError("duplicate_job_spec")
    dirty_ids = tuple(recompute_plan.mandatory_segment_ids)
    if set(specs) != set(dirty_ids):
        raise GenerationSequenceError("job_spec_closure_mismatch")
    if type(reusable_receipts) is not tuple or not all(
        type(item) is SegmentArtifactReceipt for item in reusable_receipts
    ):
        raise GenerationSequenceError("reusable_receipts")
    receipts = {item.segment_id: item for item in reusable_receipts}
    if len(receipts) != len(reusable_receipts):
        raise GenerationSequenceError("duplicate_reusable_receipt")

    manifest_by_id = {item.segment_id: item for item in manifests}
    decision_by_id = {item.segment_id: item for item in recompute_plan.decisions}
    dirty = set(dirty_ids)
    jobs: list[GenerationSequenceJob] = []
    for manifest in manifests:
        if manifest.segment_id not in dirty:
            continue
        spec = specs[manifest.segment_id]
        predecessor_receipt: SegmentArtifactReceipt | None = None
        dirty_dependencies = tuple(
            item for item in manifest.dependency_segment_ids if item in dirty
        )
        for dependency in manifest.dependency_segment_ids:
            if dependency in dirty:
                continue
            predecessor_receipt = receipts.get(dependency)
            if predecessor_receipt is None:
                raise GenerationSequenceError("missing_predecessor_receipt")
            if not _compatible_receipt(predecessor_receipt, manifest_by_id[dependency]):
                raise GenerationSequenceError("incompatible_predecessor_receipt")
        decision = decision_by_id[manifest.segment_id]
        jobs.append(
            GenerationSequenceJob(
                job_id=spec.job_id,
                segment_id=manifest.segment_id,
                ordinal=manifest.ordinal,
                task_mode=manifest.task_mode,
                source_id=manifest.source_id,
                reference_ids=manifest.reference_ids,
                duration=manifest.duration,
                dependency_segment_ids=dirty_dependencies,
                disposition=decision.disposition,
                reason_codes=decision.reason_codes,
                triggering_segment_ids=decision.triggering_segment_ids,
                manifest_fingerprint=manifest.fingerprint,
                producer_fingerprint=manifest.producer_fingerprint,
                native_binding_fingerprint=manifest.native_binding_fingerprint,
                settings_fingerprint=manifest.producer_settings_fingerprint,
                graph_fingerprint=spec.graph_fingerprint,
                compiled_prompt_fingerprint=spec.compiled_prompt_fingerprint,
                model_fingerprint=spec.model_fingerprint,
                runtime_fingerprint=spec.runtime_fingerprint,
                expected_format=spec.expected_format,
                expected_shape=spec.expected_shape,
                timeout_ms=spec.timeout_ms,
                fingerprint_domain=spec.fingerprint_domain,
                predecessor_receipt_fingerprint=(
                    None if predecessor_receipt is None else predecessor_receipt.fingerprint
                ),
                predecessor_artifact_fingerprint=(
                    None if predecessor_receipt is None else predecessor_receipt.output_fingerprint
                ),
            )
        )
    clean_ids = tuple(
        item.segment_id
        for item in recompute_plan.decisions
        if item.disposition is RecomputeDisposition.CLEAN
    )
    resolved_sequence_id = (
        f"sequence.{workspace.revision}.{workspace.fingerprint.removeprefix('sha256:')[:32]}"
        if sequence_id is None
        else sequence_id
    )
    return GenerationSequencePlan(
        sequence_id=resolved_sequence_id,
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        recompute_plan_fingerprint=canonical_fingerprint(recompute_plan.to_public_dict()),
        manifest_fingerprints=tuple(item.fingerprint for item in manifests),
        jobs=tuple(jobs),
        clean_segment_ids=clean_ids,
        source_workspace_authority=workspace,
        max_concurrency=max_concurrency,
    )


@dataclass(frozen=True, slots=True)
class GenerationJobRuntime:
    job_id: str
    state: GenerationJobState = GenerationJobState.PLANNED
    attempt: int = 1
    transaction_id: str | None = None
    transaction: PipelineTransaction | None = None
    artifact_receipt_fingerprint: str | None = None
    artifact_output_fingerprint: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.job_id, "runtime.job_id")
        if type(self.state) is not GenerationJobState:
            raise GenerationSequenceError("runtime_state")
        _positive_int(self.attempt, "runtime_attempt", MAX_PIPELINE_TRANSACTION_ATTEMPTS)
        if self.transaction_id is not None:
            _identifier(self.transaction_id, "runtime.transaction_id")
        _optional_fingerprint(self.artifact_receipt_fingerprint, "runtime.artifact_receipt")
        _optional_fingerprint(self.artifact_output_fingerprint, "runtime.artifact_output")
        if self.failure_code is not None:
            _identifier(self.failure_code, "runtime.failure_code")
        if self.transaction is not None:
            if type(self.transaction) is not PipelineTransaction:
                raise GenerationSequenceError("runtime_transaction")
            if self.transaction.attempt != self.attempt:
                raise GenerationSequenceError("runtime_transaction_attempt")
            if self.transaction_id != self.transaction.transaction_id:
                raise GenerationSequenceError("runtime_transaction_id")
        self._validate_state_shape()

    def _validate_state_shape(self) -> None:
        expected_transaction_states = {
            GenerationJobState.PROJECTED: PipelineTransactionState.PREPARED,
            GenerationJobState.SUBMITTED: PipelineTransactionState.SUBMITTED,
            GenerationJobState.RUNNING: PipelineTransactionState.RUNNING,
            GenerationJobState.OUTPUT_VERIFICATION_FAILED: PipelineTransactionState.RUNNING,
            GenerationJobState.SUCCEEDED: PipelineTransactionState.SUCCEEDED,
            GenerationJobState.FAILED: PipelineTransactionState.FAILED,
            GenerationJobState.TIMED_OUT: PipelineTransactionState.FAILED,
            GenerationJobState.UNKNOWN_OWNERSHIP: PipelineTransactionState.UNKNOWN_OWNERSHIP,
        }
        expected = expected_transaction_states.get(self.state)
        if expected is not None:
            if self.transaction is None or self.transaction.state is not expected:
                raise GenerationSequenceError("runtime_transaction_state")
        elif self.state is GenerationJobState.PLANNED and self.transaction is not None:
            raise GenerationSequenceError("planned_runtime_transaction")
        elif self.state is GenerationJobState.CANCELLED and self.transaction is not None:
            if self.transaction.state is not PipelineTransactionState.CANCELLED:
                raise GenerationSequenceError("cancelled_runtime_transaction")
        if self.state is GenerationJobState.SUCCEEDED:
            if (
                self.artifact_receipt_fingerprint is None
                or self.artifact_output_fingerprint is None
            ):
                raise GenerationSequenceError("successful_runtime_receipt")
        elif (
            self.artifact_receipt_fingerprint is not None
            or self.artifact_output_fingerprint is not None
        ):
            raise GenerationSequenceError("non_success_runtime_receipt")
        if self.state in {
            GenerationJobState.OUTPUT_VERIFICATION_FAILED,
            GenerationJobState.FAILED,
            GenerationJobState.TIMED_OUT,
        }:
            if self.failure_code is None:
                raise GenerationSequenceError("failed_runtime_code")
        elif self.failure_code is not None:
            raise GenerationSequenceError("non_failed_runtime_code")

    def to_wire(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "attempt": self.attempt,
            "transaction_id": self.transaction_id,
            "transaction": None if self.transaction is None else self.transaction.to_public_dict(),
            "artifact_receipt_fingerprint": self.artifact_receipt_fingerprint,
            "artifact_output_fingerprint": self.artifact_output_fingerprint,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True, slots=True)
class GenerationSequenceState:
    plan: GenerationSequencePlan
    runtimes: tuple[GenerationJobRuntime, ...]
    cancellation_requested: bool = False
    observation_count: int = 0
    state_fingerprint: str | None = None
    schema: str = GENERATION_SEQUENCE_STATE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != GENERATION_SEQUENCE_STATE_SCHEMA:
            raise GenerationSequenceError("unsupported_sequence_state_schema")
        if type(self.plan) is not GenerationSequencePlan:
            raise GenerationSequenceError("state_plan")
        if type(self.runtimes) is not tuple or not all(
            type(item) is GenerationJobRuntime for item in self.runtimes
        ):
            raise GenerationSequenceError("state_runtimes")
        if tuple(item.job_id for item in self.runtimes) != tuple(
            item.job_id for item in self.plan.jobs
        ):
            raise GenerationSequenceError("state_job_order")
        if type(self.cancellation_requested) is not bool:
            raise GenerationSequenceError("state_cancellation")
        if type(self.observation_count) is not int or not (
            0 <= self.observation_count <= MAX_GENERATION_SEQUENCE_OBSERVATIONS
        ):
            raise GenerationSequenceError("observation_limit")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.state_fingerprint is None:
            object.__setattr__(self, "state_fingerprint", expected)
        elif self.state_fingerprint != expected:
            raise GenerationSequenceError("state_fingerprint_mismatch")

    @property
    def fingerprint(self) -> str:
        if self.state_fingerprint is None:  # pragma: no cover
            raise GenerationSequenceError("state_fingerprint_uninitialized")
        return self.state_fingerprint

    @property
    def complete(self) -> bool:
        return all(
            item.state in {GenerationJobState.SUCCEEDED, GenerationJobState.CANCELLED}
            for item in self.runtimes
        )

    def runtime_for(self, job_id: str) -> GenerationJobRuntime:
        _identifier(job_id, "job_id")
        for runtime in self.runtimes:
            if runtime.job_id == job_id:
                return runtime
        raise GenerationSequenceError("unknown_job")

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_fingerprint": self.plan.fingerprint,
            "runtimes": [item.to_wire() for item in self.runtimes],
            "cancellation_requested": self.cancellation_requested,
            "observation_count": self.observation_count,
        }

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["state_fingerprint"] = self.fingerprint
        return value

    def to_public_dict(self) -> dict[str, object]:
        return self.to_wire()


def create_generation_sequence_state(plan: GenerationSequencePlan) -> GenerationSequenceState:
    if type(plan) is not GenerationSequencePlan:
        raise GenerationSequenceError("plan_type")
    return GenerationSequenceState(
        plan=plan,
        runtimes=tuple(GenerationJobRuntime(job_id=item.job_id) for item in plan.jobs),
    )


def eligible_generation_jobs(state: GenerationSequenceState) -> tuple[GenerationSequenceJob, ...]:
    """Return only the stable-order jobs the host adapter may project now."""

    _verify_state(state)
    if state.cancellation_requested:
        return ()
    active_states = {
        GenerationJobState.PROJECTED,
        GenerationJobState.SUBMITTED,
        GenerationJobState.RUNNING,
        GenerationJobState.OUTPUT_VERIFICATION_FAILED,
        GenerationJobState.UNKNOWN_OWNERSHIP,
    }
    active = sum(item.state in active_states for item in state.runtimes)
    available = max(0, state.plan.max_concurrency - active)
    if available == 0:
        return ()
    runtime_by_job = {item.job_id: item for item in state.runtimes}
    job_by_segment = {item.segment_id: item for item in state.plan.jobs}
    eligible: list[GenerationSequenceJob] = []
    for job in state.plan.jobs:
        runtime = runtime_by_job[job.job_id]
        if runtime.state is not GenerationJobState.PLANNED:
            continue
        ready = True
        for segment_id in job.dependency_segment_ids:
            dependency_job = job_by_segment[segment_id]
            dependency = runtime_by_job[dependency_job.job_id]
            if (
                dependency.state is not GenerationJobState.SUCCEEDED
                or dependency.artifact_output_fingerprint is None
            ):
                ready = False
                break
        if ready:
            eligible.append(job)
            if len(eligible) == available:
                break
    return tuple(eligible)


@dataclass(frozen=True, slots=True)
class GenerationSequenceCommand:
    """One core-approved host command for an exact attempt."""

    job: GenerationSequenceJob
    attempt: int
    transaction_id: str

    def __post_init__(self) -> None:
        if type(self.job) is not GenerationSequenceJob:
            raise GenerationSequenceError("command_job")
        _positive_int(self.attempt, "command_attempt", MAX_PIPELINE_TRANSACTION_ATTEMPTS)
        _identifier(self.transaction_id, "command.transaction_id")

    @property
    def job_id(self) -> str:
        return self.job.job_id

    def to_wire(self) -> dict[str, object]:
        value = self.job.to_wire()
        value["attempt"] = self.attempt
        value["transaction_id"] = self.transaction_id
        return value


@dataclass(frozen=True, slots=True)
class GenerationSequenceProgress:
    """Content-free progress fact for one planned job."""

    job_id: str
    segment_id: str
    ordinal: int
    state: GenerationJobState
    attempt: int
    transaction_id: str | None
    queue_prompt_id: str | None
    host_owner_id: str | None
    artifact_receipt_fingerprint: str | None
    artifact_output_fingerprint: str | None
    failure_code: str | None

    def __post_init__(self) -> None:
        _identifier(self.job_id, "progress.job_id")
        _identifier(self.segment_id, "progress.segment_id")
        _positive_int(self.ordinal, "progress.ordinal", MAX_WORKSPACE_SEGMENTS)
        if type(self.state) is not GenerationJobState:
            raise GenerationSequenceError("progress_state")
        _positive_int(self.attempt, "progress.attempt", MAX_PIPELINE_TRANSACTION_ATTEMPTS)
        for value, field in (
            (self.transaction_id, "progress.transaction_id"),
            (self.queue_prompt_id, "progress.queue_prompt_id"),
            (self.host_owner_id, "progress.host_owner_id"),
            (self.failure_code, "progress.failure_code"),
        ):
            if value is not None:
                _identifier(value, field)
        _optional_fingerprint(
            self.artifact_receipt_fingerprint,
            "progress.artifact_receipt",
        )
        _optional_fingerprint(
            self.artifact_output_fingerprint,
            "progress.artifact_output",
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "state": self.state.value,
            "attempt": self.attempt,
            "transaction_id": self.transaction_id,
            "queue_prompt_id": self.queue_prompt_id,
            "host_owner_id": self.host_owner_id,
            "artifact_receipt_fingerprint": self.artifact_receipt_fingerprint,
            "artifact_output_fingerprint": self.artifact_output_fingerprint,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True, slots=True)
class GenerationSequenceProjection:
    """Closed ProductShell payload; eligibility remains backend-owned."""

    state: GenerationSequenceState
    progress: tuple[GenerationSequenceProgress, ...]
    eligible_commands: tuple[GenerationSequenceCommand, ...]
    correlation: ExecutionCorrelation
    schema: str = GENERATION_SEQUENCE_PROJECTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != GENERATION_SEQUENCE_PROJECTION_SCHEMA:
            raise GenerationSequenceError("unsupported_sequence_projection_schema")
        _verify_state(self.state)
        if type(self.correlation) is not ExecutionCorrelation:
            raise GenerationSequenceError("projection_correlation")
        if (
            type(self.progress) is not tuple
            or len(self.progress) != len(self.state.plan.jobs)
            or not all(type(item) is GenerationSequenceProgress for item in self.progress)
        ):
            raise GenerationSequenceError("projection_progress")
        if tuple(item.job_id for item in self.progress) != tuple(
            item.job_id for item in self.state.plan.jobs
        ):
            raise GenerationSequenceError("projection_progress_order")
        if (
            type(self.eligible_commands) is not tuple
            or len(self.eligible_commands) > self.state.plan.max_concurrency
            or not all(type(item) is GenerationSequenceCommand for item in self.eligible_commands)
        ):
            raise GenerationSequenceError("projection_commands")
        if tuple(item.job_id for item in self.eligible_commands) != tuple(
            item.job_id for item in eligible_generation_jobs(self.state)
        ):
            raise GenerationSequenceError("projection_command_eligibility")
        if len(canonical_bytes(self.to_wire())) > MAX_WORKSPACE_WIRE_BYTES:
            raise GenerationSequenceError("sequence_projection_wire_limit")

    def to_wire(self) -> dict[str, object]:
        plan = self.state.plan
        return {
            "schema": self.schema,
            "sequence_id": plan.sequence_id,
            "sequence_fingerprint": plan.fingerprint,
            "state_fingerprint": self.state.fingerprint,
            "workspace_id": plan.workspace_id,
            "workspace_revision": plan.workspace_revision,
            "workspace_fingerprint": plan.workspace_fingerprint,
            "max_concurrency": plan.max_concurrency,
            "cancellation_requested": self.state.cancellation_requested,
            "complete": self.state.complete,
            "correlation": self.correlation.to_wire(),
            "progress": [item.to_wire() for item in self.progress],
            "eligible_commands": [item.to_wire() for item in self.eligible_commands],
        }


def _command_transaction_id(
    state: GenerationSequenceState,
    runtime: GenerationJobRuntime,
) -> str:
    if runtime.transaction_id is not None:
        return runtime.transaction_id
    digest = canonical_fingerprint(
        {
            "sequence_fingerprint": state.plan.fingerprint,
            "state_job_id": runtime.job_id,
            "attempt": runtime.attempt,
        }
    ).removeprefix("sha256:")
    return f"generation.{digest[:40]}.a{runtime.attempt}"


def build_generation_sequence_projection(
    state: GenerationSequenceState,
    correlation: ExecutionCorrelation,
) -> GenerationSequenceProjection:
    """Project exact progress plus only the commands already admitted by the pure core."""

    _verify_state(state)
    if type(correlation) is not ExecutionCorrelation:
        raise GenerationSequenceError("projection_correlation")
    runtime_by_job = {item.job_id: item for item in state.runtimes}
    progress: list[GenerationSequenceProgress] = []
    for job, runtime in zip(state.plan.jobs, state.runtimes, strict=True):
        transaction = runtime.transaction
        progress.append(
            GenerationSequenceProgress(
                job_id=job.job_id,
                segment_id=job.segment_id,
                ordinal=job.ordinal,
                state=runtime.state,
                attempt=runtime.attempt,
                transaction_id=runtime.transaction_id,
                queue_prompt_id=(None if transaction is None else transaction.queue_prompt_id),
                host_owner_id=None if transaction is None else transaction.host_owner_id,
                artifact_receipt_fingerprint=runtime.artifact_receipt_fingerprint,
                artifact_output_fingerprint=runtime.artifact_output_fingerprint,
                failure_code=runtime.failure_code,
            )
        )
    commands = tuple(
        GenerationSequenceCommand(
            job=job,
            attempt=runtime_by_job[job.job_id].attempt,
            transaction_id=_command_transaction_id(state, runtime_by_job[job.job_id]),
        )
        for job in eligible_generation_jobs(state)
    )
    return GenerationSequenceProjection(
        state=state,
        progress=tuple(progress),
        eligible_commands=commands,
        correlation=correlation,
    )


def _replace_runtime(
    state: GenerationSequenceState,
    runtime: GenerationJobRuntime,
    *,
    cancellation_requested: bool | None = None,
) -> GenerationSequenceState:
    runtimes = tuple(runtime if item.job_id == runtime.job_id else item for item in state.runtimes)
    return GenerationSequenceState(
        plan=state.plan,
        runtimes=runtimes,
        cancellation_requested=(
            state.cancellation_requested
            if cancellation_requested is None
            else cancellation_requested
        ),
        observation_count=state.observation_count + 1,
    )


def record_generation_projection(
    state: GenerationSequenceState,
    job_id: str,
    *,
    transaction_id: str,
    graph_fingerprint: str,
    compiled_prompt_fingerprint: str,
    fingerprint_domain: FingerprintDomain,
) -> GenerationSequenceState:
    _verify_state(state)
    job = state.plan.job_for_id(job_id)
    runtime = state.runtime_for(job_id)
    if state.cancellation_requested or runtime.state is not GenerationJobState.PLANNED:
        raise GenerationSequenceError("job_transition")
    if job_id not in {item.job_id for item in eligible_generation_jobs(state)}:
        raise GenerationSequenceError("job_not_eligible")
    _identifier(transaction_id, "transaction_id")
    if runtime.transaction_id is not None and runtime.transaction_id != transaction_id:
        raise GenerationSequenceError("retry_transaction_id")
    # `M17-20` `D6`: compare the domain before any fingerprint value.
    if type(fingerprint_domain) is not FingerprintDomain:
        raise GenerationSequenceError("fingerprint_domain")
    if fingerprint_domain is not job.fingerprint_domain:
        raise GenerationSequenceError("fingerprint_domain_mismatch")
    # IMPORTANT: D12 makes the whole graph surroundings evidence only. The
    # compiled API prompt is the single-run execution identity that must match.
    if compiled_prompt_fingerprint != job.compiled_prompt_fingerprint:
        raise GenerationSequenceError("projection_identity_mismatch")
    transaction = PipelineTransaction(
        transaction_id=transaction_id,
        attempt=runtime.attempt,
        state=PipelineTransactionState.PREPARED,
        workspace_id=state.plan.workspace_id,
        workspace_revision=state.plan.workspace_revision,
        workspace_fingerprint=state.plan.workspace_fingerprint,
        manifest_fingerprints=state.plan.manifest_fingerprints,
        recompute_plan_fingerprint=state.plan.recompute_plan_fingerprint,
        dirty_segment_ids=(job.segment_id,),
        graph_fingerprint=graph_fingerprint,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
    )
    return _replace_runtime(
        state,
        GenerationJobRuntime(
            job_id=job_id,
            state=GenerationJobState.PROJECTED,
            attempt=runtime.attempt,
            transaction_id=transaction_id,
            transaction=transaction,
        ),
    )


def record_generation_submission(
    state: GenerationSequenceState,
    job_id: str,
    *,
    queue_prompt_id: str,
) -> GenerationSequenceState:
    runtime = _runtime_in_state(state, job_id, GenerationJobState.PROJECTED)
    transaction = _require_transaction(runtime)
    submitted = record_pipeline_submission(
        transaction,
        expected_transaction_fingerprint=transaction.fingerprint,
        queue_prompt_id=queue_prompt_id,
    )
    return _replace_runtime(
        state,
        replace(runtime, state=GenerationJobState.SUBMITTED, transaction=submitted),
    )


def record_generation_running(
    state: GenerationSequenceState,
    job_id: str,
    *,
    host_owner_id: str,
) -> GenerationSequenceState:
    runtime = _runtime_in_state(state, job_id, GenerationJobState.SUBMITTED)
    transaction = _require_transaction(runtime)
    running = record_pipeline_running(
        transaction,
        expected_transaction_fingerprint=transaction.fingerprint,
        host_owner_id=host_owner_id,
    )
    return _replace_runtime(
        state,
        replace(runtime, state=GenerationJobState.RUNNING, transaction=running),
    )


def _expected_predecessor_output(
    state: GenerationSequenceState,
    job: GenerationSequenceJob,
) -> str | None:
    if not job.dependency_segment_ids:
        return job.predecessor_artifact_fingerprint
    predecessor_job = state.plan.job_for_segment(job.dependency_segment_ids[0])
    predecessor = state.runtime_for(predecessor_job.job_id)
    if (
        predecessor.state is not GenerationJobState.SUCCEEDED
        or predecessor.artifact_output_fingerprint is None
    ):
        raise GenerationSequenceError("predecessor_not_complete")
    return predecessor.artifact_output_fingerprint


def artifact_receipt_matches_job_contract(
    job: GenerationSequenceJob | GenerationJobSpec,
    receipt: SegmentArtifactReceipt,
) -> bool:
    """Match exact jobs or the managed observed-video admission contract."""

    if job.expected_format != "observed_video":
        return receipt.format_label == job.expected_format and receipt.shape == job.expected_shape
    if len(job.expected_shape) != 1 or len(receipt.shape) != 4:
        return False
    frames, height, width, channels = receipt.shape
    return (
        receipt.format_label in {"mp4", "mkv", "webm"}
        and frames == job.expected_shape[0]
        and 1 <= frames <= 512
        and 64 <= height <= 8_192
        and 64 <= width <= 8_192
        and channels == 3
    )


def record_generation_success(
    state: GenerationSequenceState,
    job_id: str,
    *,
    result_fingerprint: str,
    receipt: SegmentArtifactReceipt,
) -> GenerationSequenceState:
    _verify_state(state)
    runtime = state.runtime_for(job_id)
    if runtime.state not in {
        GenerationJobState.RUNNING,
        GenerationJobState.OUTPUT_VERIFICATION_FAILED,
    }:
        raise GenerationSequenceError("job_transition")
    transaction = _require_transaction(runtime)
    job = state.plan.job_for_id(job_id)
    if (
        type(receipt) is not SegmentArtifactReceipt
        or receipt.state is not ArtifactLifecycleState.COMPLETE
    ):
        raise GenerationSequenceError("incomplete_artifact_receipt")
    expected_predecessor = _expected_predecessor_output(state, job)
    if not (
        receipt.workspace_id == state.plan.workspace_id
        and receipt.workspace_revision == state.plan.workspace_revision
        and receipt.workspace_fingerprint == state.plan.workspace_fingerprint
        and receipt.segment_id == job.segment_id
        and receipt.manifest_fingerprint == job.manifest_fingerprint
        and receipt.producer_fingerprint == job.producer_fingerprint
        and receipt.transaction_fingerprint == transaction.fingerprint
        and receipt.graph_fingerprint == transaction.graph_fingerprint
        and receipt.native_binding_fingerprint == job.native_binding_fingerprint
        and receipt.model_fingerprint == job.model_fingerprint
        and receipt.runtime_fingerprint == job.runtime_fingerprint
        and receipt.settings_fingerprint == job.settings_fingerprint
        and receipt.source_id == job.source_id
        and receipt.predecessor_artifact_fingerprint == expected_predecessor
        and artifact_receipt_matches_job_contract(job, receipt)
        and receipt.output_fingerprint is not None
    ):
        raise GenerationSequenceError("artifact_receipt_identity_mismatch")
    succeeded = record_pipeline_result(
        transaction,
        expected_transaction_fingerprint=transaction.fingerprint,
        result_fingerprint=result_fingerprint,
        succeeded=True,
    )
    return _replace_runtime(
        state,
        replace(
            runtime,
            state=GenerationJobState.SUCCEEDED,
            transaction=succeeded,
            artifact_receipt_fingerprint=receipt.fingerprint,
            artifact_output_fingerprint=receipt.output_fingerprint,
            failure_code=None,
        ),
    )


def record_output_verification_failure(
    state: GenerationSequenceState,
    job_id: str,
    *,
    failure_code: str,
) -> GenerationSequenceState:
    """Retain one host attempt while making its failed artifact closure explicit."""

    runtime = _runtime_in_state(state, job_id, GenerationJobState.RUNNING)
    _identifier(failure_code, "failure_code")
    # CRITICAL: output verification is not a model retry. Keep the exact running host
    # transaction so the same immutable artifact may advance monotonically to success.
    return _replace_runtime(
        state,
        replace(
            runtime,
            state=GenerationJobState.OUTPUT_VERIFICATION_FAILED,
            failure_code=failure_code,
        ),
    )


def record_generation_failure(
    state: GenerationSequenceState,
    job_id: str,
    *,
    result_fingerprint: str,
    failure_code: str,
) -> GenerationSequenceState:
    _verify_state(state)
    runtime = state.runtime_for(job_id)
    if runtime.state not in {
        GenerationJobState.SUBMITTED,
        GenerationJobState.RUNNING,
        GenerationJobState.OUTPUT_VERIFICATION_FAILED,
    }:
        raise GenerationSequenceError("job_transition")
    _identifier(failure_code, "failure_code")
    transaction = _require_transaction(runtime)
    failed = record_pipeline_result(
        transaction,
        expected_transaction_fingerprint=transaction.fingerprint,
        result_fingerprint=result_fingerprint,
        succeeded=False,
    )
    failed_state = (
        GenerationJobState.TIMED_OUT
        if failure_code == "host_timeout"
        else GenerationJobState.FAILED
    )
    return _replace_runtime(
        state,
        replace(
            runtime,
            state=failed_state,
            transaction=failed,
            failure_code=failure_code,
        ),
    )


def mark_generation_unknown_ownership(
    state: GenerationSequenceState,
    job_id: str,
) -> GenerationSequenceState:
    _verify_state(state)
    runtime = state.runtime_for(job_id)
    if runtime.state not in {GenerationJobState.SUBMITTED, GenerationJobState.RUNNING}:
        raise GenerationSequenceError("job_transition")
    transaction = _require_transaction(runtime)
    unknown = mark_pipeline_unknown_ownership(
        transaction,
        expected_transaction_fingerprint=transaction.fingerprint,
    )
    return _replace_runtime(
        state,
        replace(runtime, state=GenerationJobState.UNKNOWN_OWNERSHIP, transaction=unknown),
    )


def cancel_generation_sequence(state: GenerationSequenceState) -> GenerationSequenceState:
    _verify_state(state)
    if state.cancellation_requested:
        return state
    runtimes: list[GenerationJobRuntime] = []
    for runtime in state.runtimes:
        if runtime.state is GenerationJobState.PLANNED:
            runtimes.append(replace(runtime, state=GenerationJobState.CANCELLED))
        elif runtime.state is GenerationJobState.PROJECTED:
            transaction = _require_transaction(runtime)
            cancelled = cancel_pipeline_transaction(
                transaction,
                expected_transaction_fingerprint=transaction.fingerprint,
            )
            runtimes.append(
                replace(runtime, state=GenerationJobState.CANCELLED, transaction=cancelled)
            )
        elif runtime.state in {
            GenerationJobState.SUBMITTED,
            GenerationJobState.RUNNING,
            GenerationJobState.UNKNOWN_OWNERSHIP,
        }:
            transaction = _require_transaction(runtime)
            requested = cancel_pipeline_transaction(
                transaction,
                expected_transaction_fingerprint=transaction.fingerprint,
            )
            runtimes.append(replace(runtime, transaction=requested))
        else:
            runtimes.append(runtime)
    return GenerationSequenceState(
        plan=state.plan,
        runtimes=tuple(runtimes),
        cancellation_requested=True,
        observation_count=state.observation_count + 1,
    )


def retry_generation_job(
    state: GenerationSequenceState,
    job_id: str,
    *,
    transaction_id: str,
) -> GenerationSequenceState:
    _verify_state(state)
    runtime = state.runtime_for(job_id)
    if runtime.state is GenerationJobState.UNKNOWN_OWNERSHIP:
        raise GenerationSequenceError("ambiguous_host_ownership")
    if runtime.state not in {
        GenerationJobState.FAILED,
        GenerationJobState.TIMED_OUT,
        GenerationJobState.CANCELLED,
    }:
        raise GenerationSequenceError("job_transition")
    if runtime.attempt >= MAX_PIPELINE_TRANSACTION_ATTEMPTS:
        raise GenerationSequenceError("retry_limit")
    _identifier(transaction_id, "transaction_id")
    if transaction_id == runtime.transaction_id:
        raise GenerationSequenceError("retry_transaction_id")
    retried = GenerationJobRuntime(
        job_id=job_id,
        state=GenerationJobState.PLANNED,
        attempt=runtime.attempt + 1,
        transaction_id=transaction_id,
    )
    return _replace_runtime(state, retried, cancellation_requested=False)


_STATE_RANK = {
    GenerationJobState.PLANNED: 0,
    GenerationJobState.PROJECTED: 1,
    GenerationJobState.SUBMITTED: 2,
    GenerationJobState.RUNNING: 3,
    GenerationJobState.OUTPUT_VERIFICATION_FAILED: 4,
    GenerationJobState.UNKNOWN_OWNERSHIP: 4,
    GenerationJobState.SUCCEEDED: 5,
    GenerationJobState.FAILED: 5,
    GenerationJobState.TIMED_OUT: 5,
    GenerationJobState.CANCELLED: 5,
}


def reconcile_generation_sequence(
    current: GenerationSequenceState,
    incoming: GenerationSequenceState,
) -> GenerationSequenceState:
    """Accept only exact-authority forward observations after reload or reconnect."""

    _verify_state(current)
    _verify_state(incoming)
    if current.plan.fingerprint != incoming.plan.fingerprint:
        raise GenerationSequenceError("sequence_authority_mismatch")
    if current.fingerprint == incoming.fingerprint:
        return current
    for current_runtime, incoming_runtime in zip(
        current.runtimes,
        incoming.runtimes,
        strict=True,
    ):
        if current_runtime.job_id != incoming_runtime.job_id:
            raise GenerationSequenceError("sequence_authority_mismatch")
        if incoming_runtime.attempt == current_runtime.attempt:
            if _STATE_RANK[incoming_runtime.state] < _STATE_RANK[current_runtime.state]:
                raise GenerationSequenceError("stale_sequence_reconciliation")
            if (
                _STATE_RANK[incoming_runtime.state] == _STATE_RANK[current_runtime.state]
                and incoming_runtime != current_runtime
            ):
                current_transaction = current_runtime.transaction
                incoming_transaction = incoming_runtime.transaction
                cancellation_forward = (
                    current_transaction is not None
                    and incoming_transaction is not None
                    and not current_transaction.cancellation_requested
                    and incoming_transaction.cancellation_requested
                )
                if not cancellation_forward:
                    raise GenerationSequenceError("stale_sequence_reconciliation")
        elif not (
            incoming_runtime.attempt == current_runtime.attempt + 1
            and incoming_runtime.state is GenerationJobState.PLANNED
            and current_runtime.state
            in {
                GenerationJobState.FAILED,
                GenerationJobState.TIMED_OUT,
                GenerationJobState.CANCELLED,
            }
        ):
            raise GenerationSequenceError("stale_sequence_reconciliation")
    if current.cancellation_requested and not incoming.cancellation_requested:
        if not any(
            new.attempt == old.attempt + 1
            for old, new in zip(current.runtimes, incoming.runtimes, strict=True)
        ):
            raise GenerationSequenceError("stale_sequence_reconciliation")
    return incoming


def _verify_state(state: GenerationSequenceState) -> None:
    if type(state) is not GenerationSequenceState:
        raise GenerationSequenceError("state_type")
    if state.fingerprint != canonical_fingerprint(state._wire_without_fingerprint()):
        raise GenerationSequenceError("tampered_sequence_state")


def _runtime_in_state(
    state: GenerationSequenceState,
    job_id: str,
    expected: GenerationJobState,
) -> GenerationJobRuntime:
    _verify_state(state)
    runtime = state.runtime_for(job_id)
    if runtime.state is not expected:
        raise GenerationSequenceError("job_transition")
    return runtime


def _require_transaction(runtime: GenerationJobRuntime) -> PipelineTransaction:
    if runtime.transaction is None:
        raise GenerationSequenceError("missing_job_transaction")
    return runtime.transaction


__all__ = [
    "GENERATION_SEQUENCE_PLAN_SCHEMA",
    "GENERATION_SEQUENCE_PROJECTION_SCHEMA",
    "GENERATION_SEQUENCE_STATE_SCHEMA",
    "MAX_GENERATION_JOB_TIMEOUT_MILLISECONDS",
    "MAX_GENERATION_SEQUENCE_CONCURRENCY",
    "MAX_GENERATION_SEQUENCE_OBSERVATIONS",
    "GenerationJobRuntime",
    "FingerprintDomain",
    "GenerationJobSpec",
    "GenerationJobState",
    "GenerationSequenceError",
    "GenerationSequenceJob",
    "GenerationSequencePlan",
    "GenerationSequenceCommand",
    "GenerationSequenceProgress",
    "GenerationSequenceProjection",
    "GenerationSequenceState",
    "artifact_receipt_matches_job_contract",
    "build_generation_sequence_plan",
    "build_generation_sequence_projection",
    "cancel_generation_sequence",
    "create_generation_sequence_state",
    "eligible_generation_jobs",
    "mark_generation_unknown_ownership",
    "reconcile_generation_sequence",
    "record_generation_failure",
    "record_generation_projection",
    "record_generation_running",
    "record_generation_submission",
    "record_generation_success",
    "record_output_verification_failure",
    "retry_generation_job",
]
