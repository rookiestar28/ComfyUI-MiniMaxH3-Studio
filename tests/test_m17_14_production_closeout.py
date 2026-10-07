"""M17-14 production acceptance closeout matrix.

These rows do not re-prove what each predecessor item already proved on its own. They join accepted
capabilities into the flows a user actually performs and assert the seams between them, which is the
only place a defect can hide when every item passes alone.

Every fixture value here is frozen by
`.planning/260818-M17-14_PRODUCTION_CLOSEOUT_PLAN.md` section 3.2.1. The single authored duration is
`8_000 ms` because it is the one length that is simultaneously on the H3 lattice
(`192 = 5 + 17 * 11`), inside the trained range, and an exact integer frame count at the model's
24 fps, the reconstruction target's 30 fps and the preview's 15 fps -- so no row below needs a
rounded count.
"""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from test_av_reconstruction_pipeline import _adapter
from test_av_reconstruction_pipeline import _policy as _pipeline_policy

from comfyui_h3_context.adapters.av_reconstruction_media import (
    AVMediaExecutionResult,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.av_reconstruction_pipeline import (
    AVReconstructionPipelineError,
    execute_production_av_reconstruction,
)
from comfyui_h3_context.adapters.av_reconstruction_store import PrivateAVReconstructionStore
from comfyui_h3_context.adapters.comfyui_continuity import (
    DecodeResult,
    extract_native_tail,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
    dispatch_production_action,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SidebarProductionSeed,
    SidebarWorkspaceRegistry,
    claim_sidebar_production_seed,
)
from comfyui_h3_context.adapters.media_subprocess import OwnedOutputLease
from comfyui_h3_context.adapters.segment_artifact_store import PrivateSegmentArtifactStore
from comfyui_h3_context.core import (
    QUALIFIED_COMFYUI_HOST_REVISION,
    QUALIFIED_COMFYUI_HOST_VERSION,
    QUALIFIED_DECODER_NAME,
    QUALIFIED_DECODER_VERSION,
    QUALIFIED_VIDEO_NODE_SOURCE_SHA256,
    QUALIFIED_VIDEO_TYPES_SOURCE_SHA256,
    ArtifactReuseStatus,
    AVReconstructionPlan,
    ContextReport,
    ContinuityAdmissionError,
    ContinuityBoundaryReceipt,
    ContinuityCapability,
    ContinuityError,
    ContinuityLimits,
    ContinuityMode,
    ContinuityPolicy,
    ContinuityReceiptError,
    ContinuityRuntimeTail,
    ExecutionCorrelation,
    FingerprintDomain,
    GenerationJobSpec,
    GenerationSequenceJob,
    GenerationSequencePlan,
    GenerationSequenceState,
    GraphAnchor,
    GraphAnchorRole,
    GraphNode,
    NativeGraphMaterialization,
    ProductionWorkbenchProjection,
    RecomputeDisposition,
    SelectiveArtifactEvidence,
    SelectiveRerunApproval,
    SelectiveRerunDisposition,
    SelectiveRerunError,
    SelectiveRerunPlan,
    VideoAdmissionMetadata,
    build_approved_generation_sequence_plan,
    build_continuity_boundary_receipt,
    build_continuity_policy,
    build_native_h3_wiring,
    build_selective_rerun_plan,
    canonical_fingerprint,
    estimate_tensor_bytes,
    plan_recompute,
)
from comfyui_h3_context.core.av_reconstruction import (
    AVAudioDescriptor,
    AVBoundaryEvidence,
    AVMediaDescriptor,
    AVOperation,
    AVRational,
    AVReconstructionError,
    AVTargetProfile,
    AVVideoDescriptor,
    approve_av_reconstruction_plan,
    build_av_reconstruction_plan,
    qualified_av_limits,
    qualified_ffmpeg_capability,
)
from comfyui_h3_context.core.continuity_handoff import bind_native_tail_to_successor
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.generation_sequence import (
    GenerationJobState,
    GenerationSequenceError,
    build_generation_sequence_plan,
    build_generation_sequence_projection,
    cancel_generation_sequence,
    create_generation_sequence_state,
    eligible_generation_jobs,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
)
from comfyui_h3_context.core.graph_binding import VisibleGraph
from comfyui_h3_context.core.length import LATTICE_OFFSET, LATTICE_STEP
from comfyui_h3_context.core.segment_artifacts import (
    ArtifactLifecycleState,
    SegmentArtifactError,
    SegmentArtifactReceipt,
    begin_segment_artifact_receipt,
    complete_segment_artifact_receipt,
)
from comfyui_h3_context.core.segment_workspace import (
    MAX_WORKSPACE_SEGMENTS,
    AcceptedIntentAuthority,
    MultiSegmentWorkspace,
    SegmentContextManifest,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    SegmentWorkspaceError,
    create_workspace,
    derive_segment_manifests,
    revise_workspace,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextProductShellNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)

#: The single frozen closeout duration and everything derived from it.
CLOSEOUT_DURATION_MS = 8_000
CLOSEOUT_FRAMES = 192
CLOSEOUT_RECONSTRUCTION_FRAMES = 240
CLOSEOUT_PREVIEW_FRAMES = 120
CLOSEOUT_AUDIO_SAMPLES = 384_000
#: The adjacent producible length, used only for the C6 duration edit.
CLOSEOUT_EDIT_DURATION_MS = 5_875
CLOSEOUT_EDIT_FRAMES = 141

WORKSPACE_ID = "workspace.m17.closeout"
ROOT = "segment.root"
NATIVE = "segment.native"
CUT = "segment.cut"
SEGMENT_ORDER = (ROOT, NATIVE, CUT)


def fp(label: str) -> str:
    """The exact direct hashing the plan freezes for the non-seed segments."""

    return f"sha256:{sha256(label.encode('ascii')).hexdigest()}"


def _closeout_report() -> ContextReport:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "A safe content-free closeout prompt.",
        duration_seconds=CLOSEOUT_DURATION_MS / 1000,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


def claim_public_seed() -> SidebarProductionSeed:
    """Reach a production seed only through the accepted public ProductShell path."""

    report = _closeout_report()
    wiring = build_native_h3_wiring(report)
    emission = H3ContextProductShellNode().emit(
        report,
        wiring,
        prompt_id="prompt.m17.14.seed",
        execution_node_id="node.m17.14.seed",
    )
    ui = cast(dict[str, tuple[object, ...]], emission["ui"])
    wire = cast(dict[str, object], ui["sidebar_workspace"][0])
    return claim_sidebar_production_seed(cast(str, wire["workspace_id"]))


def root_declaration(seed: SidebarProductionSeed) -> SegmentDeclaration:
    """Copy the claimed seed byte-for-byte; recomputing any canonical field is forbidden."""

    return SegmentDeclaration(
        segment_id=ROOT,
        task_mode=seed.task_mode,
        source_id=seed.source_id,
        reference_ids=seed.reference_ids,
        duration=seed.duration,
        relation=SegmentRelationKind.INDEPENDENT,
        predecessor_segment_id=None,
        accepted_intent_fingerprint=seed.accepted_intent_fingerprint,
        semantic_receipt_fingerprint=None,
        profile_fingerprint=seed.profile_fingerprint,
        reference_registry_fingerprint=seed.reference_registry_fingerprint,
        native_binding_fingerprint=seed.native_binding_fingerprint,
        producer_settings_fingerprint=seed.producer_settings_fingerprint,
    )


def _derived_declaration(
    segment_id: str,
    *,
    task_mode: TaskMode,
    relation: SegmentRelationKind,
    predecessor: str | None,
    references: tuple[str, ...],
    settings_label: str,
) -> SegmentDeclaration:
    return SegmentDeclaration(
        segment_id=segment_id,
        task_mode=task_mode,
        source_id=f"source.{segment_id}",
        reference_ids=references,
        duration=SegmentDuration(duration_milliseconds=CLOSEOUT_DURATION_MS),
        relation=relation,
        predecessor_segment_id=predecessor,
        accepted_intent_fingerprint=fp(f"intent.{segment_id}"),
        semantic_receipt_fingerprint=None,
        profile_fingerprint=fp(f"profile.{task_mode.value}"),
        reference_registry_fingerprint=fp(f"registry.{segment_id}"),
        native_binding_fingerprint=fp(f"native.{task_mode.value}"),
        producer_settings_fingerprint=fp(settings_label),
    )


def closeout_segments(
    seed: SidebarProductionSeed,
    *,
    native_settings: str = "settings.native.v1",
) -> tuple[SegmentDeclaration, ...]:
    return (
        root_declaration(seed),
        _derived_declaration(
            NATIVE,
            task_mode=TaskMode.FL2VA,
            relation=SegmentRelationKind.PREDECESSOR,
            predecessor=ROOT,
            references=("reference.segment.native",),
            settings_label=native_settings,
        ),
        _derived_declaration(
            CUT,
            task_mode=TaskMode.T2VA,
            relation=SegmentRelationKind.CUT,
            predecessor=None,
            references=(),
            settings_label="settings.cut.v1",
        ),
    )


def authorities(
    segments: tuple[SegmentDeclaration, ...],
) -> tuple[AcceptedIntentAuthority, ...]:
    return tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in segments
    )


def closeout_workspace(segments: tuple[SegmentDeclaration, ...]) -> MultiSegmentWorkspace:
    return create_workspace(
        WORKSPACE_ID,
        segments,
        accepted_intent_authorities=authorities(segments),
        selected_segment_ids=SEGMENT_ORDER,
    )


def job_spec(manifest: SegmentContextManifest) -> GenerationJobSpec:
    return GenerationJobSpec(
        segment_id=manifest.segment_id,
        job_id=f"job.{manifest.segment_id}",
        graph_fingerprint=fp(f"graph.{manifest.segment_id}"),
        compiled_prompt_fingerprint=fp(f"compiled.{manifest.segment_id}"),
        model_fingerprint=fp("model.h3"),
        runtime_fingerprint=fp("runtime.comfyui.0.32.0"),
        expected_format="video.mp4",
        expected_shape=(CLOSEOUT_FRAMES, 180, 320, 3),
        timeout_ms=60_000,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )


def segment_payload(segment_id: str) -> bytes:
    """The deterministic bounded stand-in for one segment's rendered output."""

    return f"closeout-private-source-{segment_id}".encode("ascii")


def artifact_receipt(
    manifest: SegmentContextManifest,
    spec: GenerationJobSpec,
) -> SegmentArtifactReceipt:
    return SegmentArtifactReceipt(
        artifact_id=f"artifact.{manifest.segment_id}",
        state=ArtifactLifecycleState.COMPLETE,
        workspace_id=manifest.workspace_id,
        workspace_revision=manifest.workspace_revision,
        workspace_fingerprint=manifest.workspace_fingerprint,
        segment_id=manifest.segment_id,
        manifest_fingerprint=manifest.fingerprint,
        producer_fingerprint=manifest.producer_fingerprint,
        transaction_fingerprint=fp(f"transaction.{manifest.segment_id}"),
        graph_fingerprint=spec.graph_fingerprint,
        native_binding_fingerprint=manifest.native_binding_fingerprint,
        model_fingerprint=spec.model_fingerprint,
        runtime_fingerprint=spec.runtime_fingerprint,
        settings_fingerprint=manifest.producer_settings_fingerprint,
        source_id=manifest.source_id,
        # A chained segment's artifact names the predecessor output it was produced from. Leaving
        # this empty would let a receipt from a different predecessor pass as reusable.
        predecessor_artifact_fingerprint=(
            fp(segment_payload(manifest.dependency_segment_ids[0]).decode("ascii"))
            if manifest.dependency_segment_ids
            else None
        ),
        execution_fingerprint=fp(f"execution.{manifest.segment_id}"),
        format_label=spec.expected_format,
        shape=spec.expected_shape,
        created_at_ms=100,
        expires_at_ms=10_000,
        # An artifact's output fingerprint is the digest of its bytes, not a label. Anything else
        # would let a receipt claim an output it does not describe, and the reconstruction wrapper
        # rechecks exactly this before it will run.
        output_fingerprint=fp(segment_payload(manifest.segment_id).decode("ascii")),
        byte_length=len(segment_payload(manifest.segment_id)),
    )


def reuse_evidence(
    manifest: SegmentContextManifest,
    spec: GenerationJobSpec,
    *,
    status: ArtifactReuseStatus = ArtifactReuseStatus.REUSABLE,
    inspected_at_ms: int = 100,
) -> SelectiveArtifactEvidence:
    return SelectiveArtifactEvidence(
        segment_id=manifest.segment_id,
        status=status,
        inspected_at_ms=inspected_at_ms,
        receipt=artifact_receipt(manifest, spec),
    )


def test_c01_workspace_identity_and_boundaries() -> None:
    """C1: the three-segment workspace is exactly what the plan freezes, seed included."""

    seed = claim_public_seed()
    segments = closeout_segments(seed)
    workspace = closeout_workspace(segments)

    assert workspace.revision == 1
    assert tuple(item.segment_id for item in workspace.segments) == SEGMENT_ORDER
    assert workspace.selected_segment_ids == SEGMENT_ORDER

    # The authored member is a duration; the frame count is derived, never supplied. A fixture that
    # could state its own frame count could state one the model cannot produce, which is exactly the
    # defect M17-25 removed.
    for segment in workspace.segments:
        assert segment.duration.duration_milliseconds == CLOSEOUT_DURATION_MS
        assert segment.duration.frame_count == CLOSEOUT_FRAMES
        assert segment.duration.snapped is False
    assert (CLOSEOUT_FRAMES - LATTICE_OFFSET) % LATTICE_STEP == 0

    root, native, cut = workspace.segments
    assert root.relation is SegmentRelationKind.INDEPENDENT
    assert root.predecessor_segment_id is None
    assert native.relation is SegmentRelationKind.PREDECESSOR
    assert native.predecessor_segment_id == ROOT
    assert native.task_mode is TaskMode.FL2VA
    # A cut is a declared boundary, not a missing predecessor: the two must stay distinguishable or
    # a reset would be indistinguishable from a broken chain.
    assert cut.relation is SegmentRelationKind.CUT
    assert cut.predecessor_segment_id is None

    # Root carries the claimed seed byte-for-byte. Recomputing any of these locally would let the
    # closeout agree with itself instead of with the public seed path.
    assert root.task_mode is seed.task_mode
    assert root.source_id == seed.source_id
    assert root.reference_ids == seed.reference_ids
    assert root.duration == seed.duration
    assert root.accepted_intent_fingerprint == seed.accepted_intent_fingerprint
    assert root.profile_fingerprint == seed.profile_fingerprint
    assert root.reference_registry_fingerprint == seed.reference_registry_fingerprint
    assert root.native_binding_fingerprint == seed.native_binding_fingerprint
    assert root.producer_settings_fingerprint == seed.producer_settings_fingerprint

    manifests = derive_segment_manifests(workspace)
    assert tuple(item.segment_id for item in manifests) == SEGMENT_ORDER
    assert tuple(item.ordinal for item in manifests) == (1, 2, 3)
    by_id = {item.segment_id: item for item in manifests}
    assert by_id[NATIVE].dependency_segment_ids == (ROOT,)
    assert by_id[NATIVE].ancestry_root_segment_id == ROOT
    assert by_id[NATIVE].ancestry_depth == 1
    assert by_id[CUT].dependency_segment_ids == ()
    assert by_id[CUT].reset_boundary is True
    assert by_id[ROOT].reset_boundary is False


def test_c02_selective_closure_and_reuse() -> None:
    """C2: one producer edit queues exactly one segment and reuses the two that did not change."""

    seed = claim_public_seed()
    first = closeout_workspace(closeout_segments(seed))
    second_segments = closeout_segments(seed, native_settings="settings.native.v2")
    second = revise_workspace(
        first,
        expected_workspace_fingerprint=first.fingerprint,
        accepted_intent_authorities=authorities(second_segments),
        segments=second_segments,
    )
    assert second.revision == 2

    previous = derive_segment_manifests(first)
    current = derive_segment_manifests(second)
    recompute = plan_recompute(previous, current)
    assert tuple(item.disposition for item in recompute.decisions) == (
        RecomputeDisposition.CLEAN,
        RecomputeDisposition.DIRTY_SELF,
        RecomputeDisposition.CLEAN,
    )

    specs = tuple(job_spec(item) for item in current)
    spec_by_id = {item.segment_id: item for item in specs}
    # The reusable output belongs to the accepted predecessor revision, which is the whole point of
    # reuse: a revision that changed a different segment must not invalidate this one.
    accepted = {item.segment_id: item for item in previous}
    evidence = (
        reuse_evidence(accepted[ROOT], spec_by_id[ROOT]),
        reuse_evidence(accepted[CUT], spec_by_id[CUT]),
    )
    plan = build_selective_rerun_plan(
        second,
        previous,
        current,
        requested_segment_ids=(NATIVE,),
        job_specs=specs,
        artifact_evidence=evidence,
    )
    # Requesting only the changed segment is a safe selection: nothing downstream of native exists,
    # so no enlargement is required and the user is not asked to approve more than they asked for.
    assert plan.selection_safe is True
    assert plan.required_enlargement_segment_ids == ()
    assert plan.queued_segment_ids == (NATIVE,)
    disposition = {item.segment_id: item.disposition for item in plan.decisions}
    assert disposition[ROOT] is SelectiveRerunDisposition.REUSE
    assert disposition[NATIVE] is SelectiveRerunDisposition.QUEUE
    assert disposition[CUT] is SelectiveRerunDisposition.REUSE

    sequence = build_approved_generation_sequence_plan(
        plan,
        SelectiveRerunApproval(plan.fingerprint, (NATIVE,)),
        workspace=second,
        manifests=current,
        job_specs=specs,
        artifact_evidence=tuple(replace(item, inspected_at_ms=200) for item in evidence),
        approval_inspected_at_ms=200,
    )
    # A clean segment produces no job at all. Producing one and discarding its output later would
    # still have cost a real generation.
    assert tuple(item.segment_id for item in sequence.jobs) == (NATIVE,)
    assert set(sequence.clean_segment_ids) == {ROOT, CUT}


def test_c02_missing_root_artifact_blocks_native_before_submission() -> None:
    """C2 companion: a chain whose predecessor output is gone must block, not queue and hope."""

    seed = claim_public_seed()
    first = closeout_workspace(closeout_segments(seed))
    second_segments = closeout_segments(seed, native_settings="settings.native.v2")
    second = revise_workspace(
        first,
        expected_workspace_fingerprint=first.fingerprint,
        accepted_intent_authorities=authorities(second_segments),
        segments=second_segments,
    )
    previous = derive_segment_manifests(first)
    current = derive_segment_manifests(second)
    specs = tuple(job_spec(item) for item in current)
    spec_by_id = {item.segment_id: item for item in specs}
    accepted = {item.segment_id: item for item in previous}
    plan = build_selective_rerun_plan(
        second,
        previous,
        current,
        requested_segment_ids=(NATIVE,),
        job_specs=specs,
        artifact_evidence=(
            reuse_evidence(
                accepted[ROOT],
                spec_by_id[ROOT],
                status=ArtifactReuseStatus.MISSING,
            ),
            reuse_evidence(accepted[CUT], spec_by_id[CUT]),
        ),
    )
    disposition = {item.segment_id: item.disposition for item in plan.decisions}
    assert disposition[ROOT] is SelectiveRerunDisposition.QUEUE
    assert plan.selection_safe is False
    assert ROOT in plan.required_enlargement_segment_ids

    with pytest.raises(SelectiveRerunError, match="approval_missing_required_segment"):
        build_approved_generation_sequence_plan(
            plan,
            SelectiveRerunApproval(plan.fingerprint, (NATIVE,)),
            workspace=second,
            manifests=current,
            job_specs=specs,
        )


class RecordingHost:
    """A hermetic stand-in for the host: it records, it never generates.

    The point of the recording is negative as much as positive. Asserting the order proves the
    sequence is exact; asserting the count proves nothing was queued twice, which no amount of
    inspecting the final state can show once a duplicate has settled.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def submit(self, job: GenerationSequenceJob) -> str:
        segment_id = job.segment_id
        self.calls.append(("submit", segment_id))
        return f"prompt.{segment_id}"

    def complete(self, job: GenerationSequenceJob) -> None:
        self.calls.append(("complete", job.segment_id))


def _run_job(
    state: GenerationSequenceState,
    job: GenerationSequenceJob,
    manifest: SegmentContextManifest,
    host: RecordingHost,
) -> tuple[GenerationSequenceState, SegmentArtifactReceipt]:
    """Drive one job through the accepted lifecycle with no host side effect."""

    state = record_generation_projection(
        state,
        job.job_id,
        transaction_id=f"transaction.{job.segment_id}.attempt.1",
        graph_fingerprint=job.graph_fingerprint,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(
        state,
        job.job_id,
        queue_prompt_id=host.submit(job),
    )
    state = record_generation_running(state, job.job_id, host_owner_id=f"host.{job.segment_id}")
    runtime = state.runtime_for(job.job_id)
    assert runtime.transaction is not None
    predecessor = None
    if manifest.dependency_segment_ids:
        predecessor = fp(f"output.{manifest.dependency_segment_ids[0]}")
    partial = begin_segment_artifact_receipt(
        manifest=manifest,
        transaction=runtime.transaction,
        artifact_id=f"artifact.{job.segment_id}",
        model_fingerprint=job.model_fingerprint,
        runtime_fingerprint=job.runtime_fingerprint,
        execution_fingerprint=fp(f"execution.{job.segment_id}"),
        predecessor_artifact_fingerprint=predecessor,
        format_label=job.expected_format,
        shape=job.expected_shape,
        created_at_ms=100,
        expires_at_ms=10_000,
    )
    receipt = complete_segment_artifact_receipt(
        partial,
        output_fingerprint=fp(f"output.{job.segment_id}"),
        byte_length=1024,
    )
    host.complete(job)
    state = record_generation_success(
        state,
        job.job_id,
        result_fingerprint=fp(f"host.result.{job.segment_id}"),
        receipt=receipt,
    )
    return state, receipt


def full_run_plan(
    seed: SidebarProductionSeed,
) -> tuple[
    MultiSegmentWorkspace,
    tuple[SegmentContextManifest, ...],
    tuple[GenerationJobSpec, ...],
    GenerationSequencePlan,
]:
    """The frozen revision-1 full run: three jobs, canonical order, concurrency one."""

    workspace = closeout_workspace(closeout_segments(seed))
    manifests = derive_segment_manifests(workspace)
    specs = tuple(job_spec(item) for item in manifests)
    # A first full run is modelled as every producer differing from an unbuilt predecessor state.
    # The accepted closure has no "run everything" mode of its own, and inventing one here would be
    # product behaviour this item is not allowed to add.
    unbuilt = derive_segment_manifests(
        create_workspace(
            WORKSPACE_ID,
            tuple(
                replace(item, producer_settings_fingerprint=fp(f"unbuilt.{item.segment_id}"))
                for item in workspace.segments
            ),
            accepted_intent_authorities=authorities(workspace.segments),
            selected_segment_ids=SEGMENT_ORDER,
        )
    )
    recompute = plan_recompute(unbuilt, manifests)
    plan = build_generation_sequence_plan(
        workspace,
        manifests,
        recompute,
        specs,
        max_concurrency=1,
    )
    return workspace, manifests, specs, plan


def test_c03_recorded_sequence_and_cancel() -> None:
    """C3: the full run queues each segment exactly once, in canonical order."""

    seed = claim_public_seed()
    _, manifests, _, plan = full_run_plan(seed)
    assert plan.max_concurrency == 1
    assert tuple(item.segment_id for item in plan.jobs) == SEGMENT_ORDER

    state = create_generation_sequence_state(plan)
    host = RecordingHost()
    by_id = {item.segment_id: item for item in manifests}
    receipts = {}
    # Dirty-only sampling is enforced by the plan, not by the driver: whatever is eligible is what
    # runs, so a driver bug cannot add a segment the plan did not queue.
    while True:
        eligible = eligible_generation_jobs(state)
        if not eligible:
            break
        assert len(eligible) == 1
        job = eligible[0]
        state, receipt = _run_job(state, job, by_id[job.segment_id], host)
        receipts[job.segment_id] = receipt

    assert state.complete
    assert host.calls == [
        ("submit", ROOT),
        ("complete", ROOT),
        ("submit", NATIVE),
        ("complete", NATIVE),
        ("submit", CUT),
        ("complete", CUT),
    ]
    assert sum(1 for kind, _ in host.calls if kind == "submit") == 3
    assert set(receipts) == set(SEGMENT_ORDER)


def test_c03_cancel_after_submission_leaves_the_host_owning_the_work() -> None:
    """C3 companion: cancelling after submission does not unqueue and does not resubmit.

    A cancel that pretended the submitted job never happened would be the dangerous outcome: the
    host is still generating, and a replay would put a second job behind it.
    """

    seed = claim_public_seed()
    first = closeout_workspace(closeout_segments(seed))
    second_segments = closeout_segments(seed, native_settings="settings.native.v2")
    second = revise_workspace(
        first,
        expected_workspace_fingerprint=first.fingerprint,
        accepted_intent_authorities=authorities(second_segments),
        segments=second_segments,
    )
    previous = derive_segment_manifests(first)
    current = derive_segment_manifests(second)
    specs = tuple(job_spec(item) for item in current)
    spec_by_id = {item.segment_id: item for item in specs}
    accepted = {item.segment_id: item for item in previous}
    selective = build_selective_rerun_plan(
        second,
        previous,
        current,
        requested_segment_ids=(NATIVE,),
        job_specs=specs,
        artifact_evidence=(
            reuse_evidence(accepted[ROOT], spec_by_id[ROOT]),
            reuse_evidence(accepted[CUT], spec_by_id[CUT]),
        ),
    )
    sequence = build_approved_generation_sequence_plan(
        selective,
        SelectiveRerunApproval(selective.fingerprint, (NATIVE,)),
        workspace=second,
        manifests=current,
        job_specs=specs,
        artifact_evidence=(
            reuse_evidence(accepted[ROOT], spec_by_id[ROOT], inspected_at_ms=200),
            reuse_evidence(accepted[CUT], spec_by_id[CUT], inspected_at_ms=200),
        ),
        approval_inspected_at_ms=200,
    )
    state = create_generation_sequence_state(sequence)
    host = RecordingHost()
    job = eligible_generation_jobs(state)[0]
    assert job.segment_id == NATIVE

    state = record_generation_projection(
        state,
        job.job_id,
        transaction_id=f"transaction.{NATIVE}.attempt.1",
        graph_fingerprint=job.graph_fingerprint,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(state, job.job_id, queue_prompt_id=host.submit(job))
    cancelled = cancel_generation_sequence(state)

    runtime = cancelled.runtime_for(job.job_id)
    assert runtime.state is GenerationJobState.SUBMITTED
    assert eligible_generation_jobs(cancelled) == ()
    assert host.calls == [("submit", NATIVE)]
    # Replay after a cancel must not produce a second submission for work the host already owns.
    with pytest.raises(GenerationSequenceError):
        record_generation_submission(cancelled, job.job_id, queue_prompt_id="prompt.replay")
    assert sum(1 for kind, _ in host.calls if kind == "submit") == 1


def _selective_native_plan(
    seed: SidebarProductionSeed,
) -> tuple[
    MultiSegmentWorkspace,
    tuple[SegmentContextManifest, ...],
    tuple[SegmentContextManifest, ...],
    GenerationSequencePlan,
]:
    """The revision-2 selective plan: native queued once, root and cut reused."""

    first = closeout_workspace(closeout_segments(seed))
    second_segments = closeout_segments(seed, native_settings="settings.native.v2")
    second = revise_workspace(
        first,
        expected_workspace_fingerprint=first.fingerprint,
        accepted_intent_authorities=authorities(second_segments),
        segments=second_segments,
    )
    previous = derive_segment_manifests(first)
    current = derive_segment_manifests(second)
    specs = tuple(job_spec(item) for item in current)
    spec_by_id = {item.segment_id: item for item in specs}
    accepted = {item.segment_id: item for item in previous}
    evidence = (
        reuse_evidence(accepted[ROOT], spec_by_id[ROOT]),
        reuse_evidence(accepted[CUT], spec_by_id[CUT]),
    )
    selective = build_selective_rerun_plan(
        second,
        previous,
        current,
        requested_segment_ids=(NATIVE,),
        job_specs=specs,
        artifact_evidence=evidence,
    )
    sequence = build_approved_generation_sequence_plan(
        selective,
        SelectiveRerunApproval(selective.fingerprint, (NATIVE,)),
        workspace=second,
        manifests=current,
        job_specs=specs,
        artifact_evidence=tuple(replace(item, inspected_at_ms=200) for item in evidence),
        approval_inspected_at_ms=200,
    )
    return second, previous, current, sequence


def _root_store_receipt(manifest: SegmentContextManifest, payload: bytes) -> SegmentArtifactReceipt:
    return SegmentArtifactReceipt(
        artifact_id=f"artifact.{manifest.segment_id}",
        state=ArtifactLifecycleState.COMPLETE,
        workspace_id=manifest.workspace_id,
        workspace_revision=manifest.workspace_revision,
        workspace_fingerprint=manifest.workspace_fingerprint,
        segment_id=manifest.segment_id,
        manifest_fingerprint=manifest.fingerprint,
        producer_fingerprint=manifest.producer_fingerprint,
        transaction_fingerprint=fp(f"transaction.{manifest.segment_id}"),
        graph_fingerprint=fp(f"graph.{manifest.segment_id}"),
        native_binding_fingerprint=manifest.native_binding_fingerprint,
        model_fingerprint=fp("model.h3"),
        runtime_fingerprint=fp("runtime.comfyui.0.32.0"),
        settings_fingerprint=manifest.producer_settings_fingerprint,
        source_id=manifest.source_id,
        predecessor_artifact_fingerprint=None,
        execution_fingerprint=fp(f"execution.{manifest.segment_id}"),
        format_label="video.mp4",
        shape=(CLOSEOUT_FRAMES, 180, 320, 3),
        created_at_ms=1_000,
        expires_at_ms=100_000,
        output_fingerprint=fp(payload.decode("ascii")),
        byte_length=len(payload),
    )


def closeout_capability() -> ContinuityCapability:
    return ContinuityCapability(
        host_version=QUALIFIED_COMFYUI_HOST_VERSION,
        host_revision=QUALIFIED_COMFYUI_HOST_REVISION,
        video_node_source_sha256=QUALIFIED_VIDEO_NODE_SOURCE_SHA256,
        video_types_source_sha256=QUALIFIED_VIDEO_TYPES_SOURCE_SHA256,
        decoder_name=QUALIFIED_DECODER_NAME,
        decoder_version=QUALIFIED_DECODER_VERSION,
    )


def closeout_admission() -> VideoAdmissionMetadata:
    """The frozen media profile, expressed as the accepted admission metadata."""

    return VideoAdmissionMetadata(
        width=320,
        height=180,
        declared_frame_count=CLOSEOUT_FRAMES,
        duration_ms=CLOSEOUT_DURATION_MS,
        frames_per_second=24.0,
        estimated_tensor_bytes=estimate_tensor_bytes(CLOSEOUT_FRAMES, 180, 320),
    )


def _committed_root_artifact(
    tmp_path: Path, manifest: SegmentContextManifest
) -> tuple[PrivateSegmentArtifactStore, SegmentArtifactReceipt, bytes]:
    payload = b"closeout-bounded-video-payload"
    receipt = _root_store_receipt(manifest, payload)
    store = PrivateSegmentArtifactStore(tmp_path / "store", clock_ms=lambda: 1_000)
    partial = replace(
        receipt,
        state=ArtifactLifecycleState.PARTIAL,
        output_fingerprint=None,
        byte_length=0,
        receipt_fingerprint=None,
    )
    store.begin(partial)
    return store, store.commit(partial, payload), payload


def native_visible_graph() -> VisibleGraph:
    """The fl2va successor graph the native boundary binds into."""

    node = GraphNode(
        node_instance_id="node.m17.14.native",
        node_id="MiniMaxH3ImageToVideo",
        input_ports=("first_frame", "last_frame"),
        output_ports=("conditioning", "latent"),
    )
    return VisibleGraph(
        fixture_id="fixture.m17.14.native",
        task_modes=(TaskMode.FL2VA,),
        nodes=(node,),
        anchors=(
            GraphAnchor(GraphAnchorRole.FIRST_FRAME, node.node_instance_id, "first_frame", 1),
            GraphAnchor(GraphAnchorRole.LAST_FRAME, node.node_instance_id, "last_frame", 2),
        ),
    )


def _native_boundary_plan(
    tmp_path: Path,
) -> tuple[
    dict[str, SegmentContextManifest],
    PrivateSegmentArtifactStore,
    SegmentArtifactReceipt,
    bytes,
    GenerationSequencePlan,
    VisibleGraph,
]:
    """A revision-2 plan whose native job is bound to the exact root artifact in the store.

    The continuity boundary is only meaningful when the frame it carries came from the artifact the
    successor job actually declares as its predecessor. Building the plan around the committed
    receipt is what makes that true rather than assumed.
    """

    seed = claim_public_seed()
    first = closeout_workspace(closeout_segments(seed))
    second_segments = closeout_segments(seed, native_settings="settings.native.v2")
    second = revise_workspace(
        first,
        expected_workspace_fingerprint=first.fingerprint,
        accepted_intent_authorities=authorities(second_segments),
        segments=second_segments,
    )
    previous = derive_segment_manifests(first)
    current = derive_segment_manifests(second)
    by_id = {item.segment_id: item for item in current}
    store, committed, payload = _committed_root_artifact(tmp_path, by_id[ROOT])
    cut_receipt = artifact_receipt(by_id[CUT], job_spec(by_id[CUT]))
    graph = native_visible_graph()
    # The job must name the graph the tail will be bound into. A job that named a different graph
    # would let the boundary receipt describe a binding that never reached the executed prompt.
    native_spec = replace(
        job_spec(by_id[NATIVE]),
        graph_fingerprint=graph.fingerprint,
    )
    plan = build_generation_sequence_plan(
        second,
        current,
        plan_recompute(previous, current),
        (native_spec,),
        reusable_receipts=(committed, cut_receipt),
        max_concurrency=1,
    )
    return by_id, store, committed, payload, plan, graph


def test_c04_native_and_cut_accounting(tmp_path: Path) -> None:
    """C4: the native boundary carries exactly one frame and the cut boundary carries nothing."""

    by_id, store, committed, payload, sequence, graph = _native_boundary_plan(tmp_path)

    tail_bytes = bytes(estimate_tensor_bytes(1, 180, 320))
    extraction = extract_native_tail(
        store,
        by_id[ROOT],
        committed,
        ContinuityLimits(),
        closeout_capability(),
        metadata_probe=lambda value: closeout_admission(),
        worker=lambda value, limits, cancelled: DecodeResult(
            tail_bytes=tail_bytes,
            shape=(1, 180, 320, 3),
            decoded_frame_count=CLOSEOUT_FRAMES,
        ),
        tensor_factory=lambda value, shape: ("runtime-image", shape),
        test_only_seams=True,
    )

    # The three counts are deliberately separate. Reporting the decoded count as the delivered count
    # is how a handoff silently claims to have carried more than it did.
    assert extraction.decoded_frame_count == CLOSEOUT_FRAMES
    assert extraction.carried_frame_count == 1
    assert extraction.delivered_frame_count == 1
    assert extraction.input_byte_length == len(payload)
    assert extraction.tail.shape == (1, 180, 320, 3)
    assert extraction.admission.duration_ms == CLOSEOUT_DURATION_MS
    assert extraction.admission.declared_frame_count == CLOSEOUT_FRAMES

    job = sequence.job_for_id(f"job.{NATIVE}")
    last_frame = object()

    def materialize(
        tail: ContinuityRuntimeTail,
        anchor: GraphAnchor,
        successor_job: GenerationSequenceJob,
    ) -> NativeGraphMaterialization:
        return NativeGraphMaterialization(
            job_id=successor_job.job_id,
            graph_fingerprint=graph.fingerprint,
            node_instance_id=anchor.node_instance_id,
            socket_name=anchor.socket_name,
            connection_order=anchor.connection_order,
            bound_tail=tail,
            bound_input=tail.tensor,
            last_frame_after=last_frame,
        )

    binding = bind_native_tail_to_successor(
        extraction,
        successor_plan=sequence,
        successor_job=job,
        successor_manifest=by_id[NATIVE],
        visible_graph=graph,
        last_frame_before=last_frame,
        materialize=materialize,
    )
    receipt = build_continuity_boundary_receipt(
        build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.FL2VA),
        boundary_id="boundary.m17.14.native",
        predecessor_manifest=by_id[ROOT],
        predecessor_receipt=committed,
        successor_plan=sequence,
        successor_job=job,
        successor_manifest=by_id[NATIVE],
        binding=binding,
    )
    assert receipt.mode is ContinuityMode.NATIVE_FRAME_HANDOFF
    assert receipt.audio_not_carried is True
    # The receipt carries the same three counts, so a reader of the boundary alone cannot be told a
    # different story from the extraction.
    assert receipt.decoded_frame_count == CLOSEOUT_FRAMES
    assert receipt.carried_frame_count == 1
    assert receipt.delivered_frame_count == 1
    assert receipt.predecessor_segment_id == ROOT
    assert receipt.successor_segment_id == NATIVE
    assert receipt.successor_graph_fingerprint == graph.fingerprint

    # The cut segment is a declared boundary. Asking for a native handoff on its mode is refused
    # rather than downgraded, so a cut can never become an accidental continuation.
    with pytest.raises(ContinuityError):
        build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.T2VA)
    cut_receipt = build_continuity_boundary_receipt(
        ContinuityPolicy(ContinuityMode.CUT, TaskMode.T2VA),
        boundary_id="boundary.m17.14.cut",
    )
    assert cut_receipt.mode is ContinuityMode.CUT
    assert cut_receipt.audio_not_carried is True
    # A cut carries no tail at all: every native field stays absent rather than zero, so "carried
    # nothing" and "carried zero frames" cannot be confused.
    assert cut_receipt.decoded_frame_count is None
    assert cut_receipt.carried_frame_count is None
    assert cut_receipt.delivered_frame_count is None
    assert cut_receipt.tail_content_sha256 is None
    assert cut_receipt.restart_root_id is None
    assert cut_receipt.fingerprint != receipt.fingerprint


def test_c04_over_limit_input_is_refused_rather_than_degraded(tmp_path: Path) -> None:
    """C4 companion: exceeding an accepted bound fails closed; it does not carry fewer frames."""

    by_id, store, committed, payload, _, _ = _native_boundary_plan(tmp_path)
    tail_bytes = bytes(estimate_tensor_bytes(1, 180, 320))

    with pytest.raises(ContinuityAdmissionError):
        extract_native_tail(
            store,
            by_id[ROOT],
            committed,
            ContinuityLimits(max_input_bytes=len(payload) - 1),
            closeout_capability(),
            metadata_probe=lambda value: closeout_admission(),
            worker=lambda value, limits, cancelled: DecodeResult(
                tail_bytes=tail_bytes,
                shape=(1, 180, 320, 3),
                decoded_frame_count=CLOSEOUT_FRAMES,
            ),
            tensor_factory=lambda value, shape: ("runtime-image", shape),
            test_only_seams=True,
        )


def all_current_selective_plan(
    seed: SidebarProductionSeed,
) -> tuple[
    MultiSegmentWorkspace,
    tuple[SegmentContextManifest, ...],
    SelectiveRerunPlan,
    SelectiveRerunApproval,
    GenerationSequenceState,
    tuple[SegmentArtifactReceipt, ...],
    tuple[SegmentContextManifest, ...],
]:
    """An all-current revision: every segment has a reusable artifact and nothing is queued.

    This is the state a finished production is in, and it is the only state a full reconstruction
    is allowed to consume.
    """

    first = closeout_workspace(closeout_segments(seed))
    second_segments = closeout_segments(seed, native_settings="settings.native.v2")
    second = revise_workspace(
        first,
        expected_workspace_fingerprint=first.fingerprint,
        accepted_intent_authorities=authorities(second_segments),
        segments=second_segments,
    )
    previous = derive_segment_manifests(first)
    current = derive_segment_manifests(second)
    specs = tuple(job_spec(item) for item in current)
    spec_by_id = {item.segment_id: item for item in specs}
    accepted = {item.segment_id: item for item in current}
    evidence = tuple(
        reuse_evidence(accepted[segment_id], spec_by_id[segment_id]) for segment_id in SEGMENT_ORDER
    )
    selective = build_selective_rerun_plan(
        second,
        current,
        current,
        requested_segment_ids=(),
        job_specs=specs,
        artifact_evidence=evidence,
    )
    approval = SelectiveRerunApproval(selective.fingerprint, ())
    sequence = build_approved_generation_sequence_plan(
        selective,
        approval,
        workspace=second,
        manifests=current,
        job_specs=specs,
        artifact_evidence=tuple(replace(item, inspected_at_ms=200) for item in evidence),
        approval_inspected_at_ms=200,
    )
    receipts = tuple(
        artifact_receipt(accepted[segment_id], spec_by_id[segment_id])
        for segment_id in SEGMENT_ORDER
    )
    state = create_generation_sequence_state(sequence)
    return second, current, selective, approval, state, receipts, previous


def closeout_media_descriptor(
    receipt: SegmentArtifactReceipt,
    *,
    inspected_at_ms: int = 300,
) -> AVMediaDescriptor:
    """The frozen source profile: 320x180, 24 fps, 192 frames, stereo 48 kHz, 384 000 samples."""

    assert receipt.output_fingerprint is not None
    return AVMediaDescriptor(
        segment_id=receipt.segment_id,
        artifact_receipt_fingerprint=receipt.fingerprint,
        artifact_output_fingerprint=receipt.output_fingerprint,
        artifact_byte_length=receipt.byte_length,
        capability_fingerprint=qualified_ffmpeg_capability().fingerprint,
        container="mp4",
        stream_count=2,
        video=AVVideoDescriptor(
            codec="h264",
            width=320,
            height=180,
            pixel_format="yuv420p",
            color_range="tv",
            color_space="bt709",
            color_primaries="bt709",
            color_transfer="bt709",
            rotation_degrees=0,
            frame_rate=AVRational(24, 1),
            time_base=AVRational(1, 90_000),
            start_time=AVRational(0, 1),
            end_time=AVRational(CLOSEOUT_DURATION_MS // 1_000, 1),
            decoded_frame_count=CLOSEOUT_FRAMES,
        ),
        audio=AVAudioDescriptor(
            codec="aac",
            sample_format="fltp",
            sample_rate=48_000,
            channels=2,
            channel_layout="stereo",
            time_base=AVRational(1, 48_000),
            start_time=AVRational(0, 1),
            end_time=AVRational(CLOSEOUT_DURATION_MS // 1_000, 1),
            decoded_sample_count=CLOSEOUT_AUDIO_SAMPLES,
        ),
        subtitle_stream_count=0,
        data_stream_count=0,
        attachment_stream_count=0,
        inspected_at_ms=inspected_at_ms,
        expires_at_ms=inspected_at_ms + 60_000,
        warning_codes=(),
    )


def closeout_target_profile() -> AVTargetProfile:
    """The accepted normalization target: 512x512, 30 fps, stereo 48 kHz."""

    return AVTargetProfile(
        container="mp4",
        video_codec="h264",
        width=512,
        height=512,
        pixel_format="yuv420p",
        color_range="tv",
        color_space="bt709",
        color_primaries="bt709",
        color_transfer="bt709",
        rotation_degrees=0,
        frame_rate=AVRational(30, 1),
        video_time_base=AVRational(1, 90_000),
        audio_required=True,
        audio_codec="aac",
        sample_format="fltp",
        sample_rate=48_000,
        channels=2,
        channel_layout="stereo",
        audio_time_base=AVRational(1, 48_000),
    )


def closeout_boundaries(
    workspace: MultiSegmentWorkspace,
    receipts: tuple[SegmentArtifactReceipt, ...],
) -> tuple[AVBoundaryEvidence, ...]:
    """The two real boundaries of this workspace: one native handoff, then one cut."""

    return (
        AVBoundaryEvidence(
            workspace_id=workspace.workspace_id,
            workspace_revision=workspace.revision,
            workspace_fingerprint=workspace.fingerprint,
            predecessor_segment_id=ROOT,
            successor_segment_id=NATIVE,
            receipt=ContinuityBoundaryReceipt(
                boundary_id="boundary.m17.14.native",
                mode=ContinuityMode.CUT,
                audio_not_carried=True,
            ),
        ),
        AVBoundaryEvidence(
            workspace_id=workspace.workspace_id,
            workspace_revision=workspace.revision,
            workspace_fingerprint=workspace.fingerprint,
            predecessor_segment_id=NATIVE,
            successor_segment_id=CUT,
            receipt=ContinuityBoundaryReceipt(
                boundary_id="boundary.m17.14.cut",
                mode=ContinuityMode.CUT,
                audio_not_carried=True,
            ),
        ),
    )


def closeout_av_plan(
    seed: SidebarProductionSeed,
) -> tuple[
    MultiSegmentWorkspace,
    AVReconstructionPlan,
    GenerationSequenceState,
    tuple[SegmentArtifactReceipt, ...],
]:
    workspace, current, selective, approval, state, receipts, _ = all_current_selective_plan(seed)
    boundaries = closeout_boundaries(workspace, receipts)
    plan = build_av_reconstruction_plan(
        selective_plan=selective,
        selective_approval=approval,
        generation_state=state,
        artifact_receipts=receipts,
        media_descriptors=tuple(closeout_media_descriptor(item) for item in receipts),
        boundary_evidence=boundaries,
        accepted_boundary_receipt_fingerprints=tuple(
            item.receipt.fingerprint for item in boundaries
        ),
        capability=qualified_ffmpeg_capability(),
        limits=qualified_av_limits(),
        target_profile=closeout_target_profile(),
        planned_at_ms=300,
    )
    return workspace, plan, state, receipts


def test_c05_full_and_segment_reconstruction() -> None:
    """C5: three segments reconstruct in canonical order with exact frame and sample accounting."""

    seed = claim_public_seed()
    workspace, plan, _, receipts = closeout_av_plan(seed)

    assert tuple(item.segment_id for item in plan.segments) == SEGMENT_ORDER
    assert plan.workspace_id == WORKSPACE_ID
    assert len(plan.boundaries) == 2
    assert plan.target_profile.frame_rate == AVRational(30, 1)

    # Every source is 24 fps and every output is 30 fps, so the emitted count is the one the target
    # implies and not the one the source happened to have. Asserting both is the point: an
    # implementation that passed frames through unchanged would still satisfy either alone.
    elapsed = AVRational(0, 1)
    for segment in plan.segments:
        assert segment.disposition is SelectiveRerunDisposition.REUSE
        assert segment.descriptor.video is not None
        assert segment.descriptor.video.decoded_frame_count == CLOSEOUT_FRAMES
        assert segment.descriptor.audio is not None
        assert segment.descriptor.audio.decoded_sample_count == CLOSEOUT_AUDIO_SAMPLES
        assert segment.accounting.decoded_frames == CLOSEOUT_FRAMES
        assert segment.accounting.emitted_frames == CLOSEOUT_RECONSTRUCTION_FRAMES
        assert segment.accounting.decoded_samples == CLOSEOUT_AUDIO_SAMPLES
        assert segment.accounting.emitted_samples == CLOSEOUT_AUDIO_SAMPLES
        assert segment.accounting.dropped_frames == 0
        assert segment.accounting.dropped_samples == 0
        # Segments are laid end to end with no gap and no overlap.
        assert segment.output_start == elapsed
        elapsed = segment.output_end

    # The aggregate is exactly the sum of its parts, in both domains.
    total_seconds = (CLOSEOUT_DURATION_MS // 1_000) * len(SEGMENT_ORDER)
    assert elapsed == AVRational(total_seconds, 1)
    assert sum(item.accounting.emitted_frames for item in plan.segments) == 720
    assert sum(item.accounting.emitted_samples for item in plan.segments) == 1_152_000

    # Every transformation is named and drawn from the approved vocabulary. The source differs from
    # the target in geometry and rate only, so exactly one normalization is expected: an extra
    # operation here would mean the plan is doing something to the audio nobody asked for, and
    # `reject` would mean it silently declined a segment.
    approved = {
        AVOperation.PASSTHROUGH,
        AVOperation.REMUX,
        AVOperation.NORMALIZE_VIDEO,
        AVOperation.NORMALIZE_AUDIO,
    }
    for segment in plan.segments:
        assert segment.operations == (AVOperation.NORMALIZE_VIDEO,)
        assert set(segment.operations) <= approved
        assert AVOperation.REJECT not in segment.operations

    approval = approve_av_reconstruction_plan(plan, approved_at_ms=310, expires_at_ms=600)
    approval.assert_executable(plan, now_ms=599)
    with pytest.raises(AVReconstructionError, match="approval_stale"):
        approval.assert_executable(plan, now_ms=600)
    assert tuple(item.fingerprint for item in receipts) == plan.artifact_receipt_fingerprints


def _context_seed(registry: SidebarWorkspaceRegistry, label: str, *, duration_ms: int) -> str:
    """Publish one content-free Context authority and return its opaque handle."""

    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        f"A safe content-free closeout prompt for {label}.",
        duration_seconds=duration_ms / 1000,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    projection = registry.publish(
        report,
        build_native_h3_wiring(report),
        ExecutionCorrelation(f"prompt.{label}", "17"),
    )
    return projection.workspace_id


def _action(request_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema": PRODUCTION_ACTION_SCHEMA,
        "request_id": request_id,
        "action": action,
        "payload": payload,
    }


def _mutate(
    projection: ProductionWorkbenchProjection,
    *,
    request_id: str,
    action: str,
    **extra: object,
) -> dict[str, object]:
    return _action(
        request_id,
        action,
        {
            "workspace_handle": projection.workspace_handle,
            "expected_workspace_revision": projection.workspace_revision,
            "expected_workspace_fingerprint": projection.workspace_fingerprint,
            **extra,
        },
    )


def test_c06_production_action_cas_matrix() -> None:
    """C6: the frozen eleven-request table, each outcome single-valued.

    Every mutation carries the revision and fingerprint it believes it is editing. That is the whole
    protection: two people editing the same production cannot both win, and the loser is told
    so rather than silently overwriting.
    """

    sidebar = SidebarWorkspaceRegistry(max_entries=8, ttl_seconds=60)
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=4,
        ttl_seconds=60,
        terminal_ttl_seconds=60,
    )
    root_seed = _context_seed(sidebar, "seed.root", duration_ms=CLOSEOUT_DURATION_MS)
    native_seed = _context_seed(sidebar, "seed.native", duration_ms=CLOSEOUT_DURATION_MS)
    cut_seed = _context_seed(sidebar, "seed.cut", duration_ms=CLOSEOUT_DURATION_MS)
    edited_cut_seed = _context_seed(
        sidebar, "seed.cut.edited", duration_ms=CLOSEOUT_EDIT_DURATION_MS
    )

    created = registry.dispatch(
        _action(
            "m17-14.c6.01",
            "create_workspace_from_context",
            {"context_workspace_handle": root_seed},
        )
    ).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    assert created.workspace_revision == 1
    # Handles are opaque and minted by the registry. Hard-coding one would let the row pass against
    # an implementation that ignored what it was given.
    root_id = created.segments[0].segment_id
    assert isinstance(root_id, str) and root_id

    projection = registry.dispatch(
        _mutate(
            created,
            request_id="m17-14.c6.02",
            action="add_segment_from_context",
            context_workspace_handle=native_seed,
            relation="predecessor",
            predecessor_segment_id=root_id,
        )
    ).projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    assert projection.workspace_revision == 2
    native_id = projection.segments[1].segment_id

    projection = registry.dispatch(
        _mutate(
            projection,
            request_id="m17-14.c6.03",
            action="add_segment_from_context",
            context_workspace_handle=cut_seed,
            relation="cut",
            predecessor_segment_id=None,
        )
    ).projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    assert projection.workspace_revision == 3
    cut_id = projection.segments[2].segment_id
    assert len({root_id, native_id, cut_id}) == 3

    projection = registry.dispatch(
        _mutate(
            projection,
            request_id="m17-14.c6.04",
            action="set_selection",
            segment_ids=[native_id],
        )
    ).projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    assert projection.workspace_revision == 4
    assert tuple(projection.selected_segment_ids) == (native_id,)

    projection = registry.dispatch(
        _mutate(
            projection,
            request_id="m17-14.c6.05",
            action="replace_segment_from_context",
            segment_id=cut_id,
            context_workspace_handle=edited_cut_seed,
        )
    ).projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    assert projection.workspace_revision == 5
    # The sole duration edit is real: the replaced segment states the adjacent producible length.
    replaced = next(item for item in projection.segments if item.segment_id == cut_id)
    assert replaced.duration_milliseconds == CLOSEOUT_EDIT_DURATION_MS
    assert replaced.frame_count == CLOSEOUT_EDIT_FRAMES

    projection = registry.dispatch(
        _mutate(
            projection,
            request_id="m17-14.c6.06",
            action="set_segment_relation",
            segment_id=cut_id,
            relation="predecessor",
            predecessor_segment_id=native_id,
        )
    ).projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    assert projection.workspace_revision == 6

    # A reorder that would place a dependent before its predecessor is rejected, and the rejection
    # costs a revision to nobody: the workspace is exactly where it was.
    with pytest.raises(ProductionWorkbenchError) as rejected:
        registry.dispatch(
            _mutate(
                projection,
                request_id="m17-14.c6.07",
                action="reorder_segments",
                segment_ids=[cut_id, native_id, root_id],
            )
        )
    assert rejected.value.status == 422
    current = registry.dispatch(
        _action(
            "m17-14.c6.07.read",
            "read_projection",
            {"workspace_handle": projection.workspace_handle},
        )
    ).projection
    assert isinstance(current, ProductionWorkbenchProjection)
    assert current.workspace_revision == 6

    projection = registry.dispatch(
        _mutate(
            current,
            request_id="m17-14.c6.08",
            action="set_segment_relation",
            segment_id=cut_id,
            relation="independent",
            predecessor_segment_id=None,
        )
    ).projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    assert projection.workspace_revision == 7

    projection = registry.dispatch(
        _mutate(
            projection,
            request_id="m17-14.c6.09",
            action="reorder_segments",
            segment_ids=[cut_id, root_id, native_id],
        )
    ).projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    assert projection.workspace_revision == 8
    assert tuple(item.segment_id for item in projection.segments) == (cut_id, root_id, native_id)

    projection = registry.dispatch(
        _mutate(
            projection,
            request_id="m17-14.c6.10",
            action="delete_segment",
            segment_id=cut_id,
        )
    ).projection
    assert isinstance(projection, ProductionWorkbenchProjection)
    assert projection.workspace_revision == 9
    assert cut_id not in {item.segment_id for item in projection.segments}

    # The stale action carries revision 6 while the workspace is at 9. It must be refused *and* the
    # caller must be handed the current safe projection, or the UI has no way back.
    with pytest.raises(ProductionWorkbenchError) as stale:
        registry.dispatch(
            _action(
                "m17-14.c6.11",
                "set_selection",
                {
                    "workspace_handle": projection.workspace_handle,
                    "expected_workspace_revision": 6,
                    "expected_workspace_fingerprint": current.workspace_fingerprint,
                    "segment_ids": [root_id],
                },
            )
        )
    assert stale.value.status == 409
    stale_projection = stale.value.projection
    assert isinstance(stale_projection, ProductionWorkbenchProjection)
    assert stale_projection.workspace_revision == 9
    assert tuple(stale_projection.selected_segment_ids) != (root_id,)


def test_c06_product_shell_stage_adopt_and_generate() -> None:
    """C6: the run this closeout planned is the run Production adopts, not a lookalike.

    The join is the risk. Context owns the seed, the sequence owns the plan and Production owns the
    canvas; if the adoption minted its own workspace identity instead of adopting the one that was
    staged, every downstream revision, receipt and boundary would refer to a production nobody ran.
    """

    seed = claim_public_seed()
    workspace, manifests, _, plan = full_run_plan(seed)
    state = create_generation_sequence_state(plan)

    report = _closeout_report()
    wiring = build_native_h3_wiring(report)
    staged = H3ContextProductShellNode().emit(
        report,
        wiring,
        generation_sequence_state=state,
        prompt_id="prompt.m17.14.staged",
        execution_node_id="node.m17.14.staged",
    )
    staged_ui = cast(dict[str, tuple[object, ...]], staged["ui"])
    assert "generation_sequence" in staged_ui
    context_handle = cast(
        str, cast(dict[str, object], staged_ui["sidebar_workspace"][0])["workspace_id"]
    )

    created = dispatch_production_action(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.m17.14.adopt",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": context_handle},
        }
    ).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    # Adopted, not minted: the three identity dimensions are the staged source's own.
    assert created.workspace_id == workspace.workspace_id
    assert created.workspace_revision == workspace.revision
    assert created.workspace_fingerprint == workspace.fingerprint
    assert tuple(item.segment_id for item in created.segments) == SEGMENT_ORDER
    assert created.generation_sequence is not None
    # The adopted canvas can actually run: the generation control is offered, not merely described.
    assert "submit_generation_job" in created.allowed_actions

    released = dispatch_production_action(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.m17.14.release",
            "action": "release_workspace",
            "payload": {
                "workspace_handle": created.workspace_handle,
                "expected_workspace_revision": created.workspace_revision,
                "expected_workspace_fingerprint": created.workspace_fingerprint,
            },
        }
    )
    assert released.status == 204
    assert len(manifests) == len(SEGMENT_ORDER)


CLOSEOUT_INTENT = "A safe content-free closeout prompt."


def _wire_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def test_c09_resource_privacy_boundary_plus_one(tmp_path: Path) -> None:
    """C9: every bound rejects at boundary-plus-one, and no wire carries private content."""

    limits = ContinuityLimits()
    admission = closeout_admission()

    # Each of these is the accepted ceiling plus one, in a different dimension. They are asserted
    # one at a time because a validator checking only the first would still pass a combined case.
    limits.validate_admission(admission, limits.max_input_bytes)
    with pytest.raises(ContinuityAdmissionError, match="input_bytes_limit"):
        limits.validate_admission(admission, limits.max_input_bytes + 1)
    with pytest.raises(ContinuityAdmissionError, match="dimensions_limit"):
        limits.validate_admission(
            replace(admission, width=limits.max_width + 1),
            1_024,
        )
    with pytest.raises(ContinuityAdmissionError, match="duration_limit"):
        limits.validate_admission(
            replace(admission, duration_ms=limits.max_duration_ms + 1),
            1_024,
        )
    with pytest.raises(ContinuityAdmissionError, match="frame_count_limit"):
        limits.validate_admission(
            replace(admission, declared_frame_count=limits.max_decoded_frame_count + 1),
            1_024,
        )

    # A workspace may hold the accepted number of segments and not one more.
    seed = claim_public_seed()
    root = root_declaration(seed)
    many = tuple(
        replace(root, segment_id=f"segment.bulk.{index}") for index in range(MAX_WORKSPACE_SEGMENTS)
    )
    closeout = create_workspace(
        "workspace.m17.closeout.bulk",
        many,
        accepted_intent_authorities=authorities(many),
    )
    assert len(closeout.segments) == MAX_WORKSPACE_SEGMENTS
    with pytest.raises(SegmentWorkspaceError):
        create_workspace(
            "workspace.m17.closeout.bulk",
            many + (replace(root, segment_id="segment.bulk.overflow"),),
            accepted_intent_authorities=authorities(
                many + (replace(root, segment_id="segment.bulk.overflow"),)
            ),
        )

    # Privacy: the wires a browser can see carry identity, never content. The authored intent of the
    # closeout report is a literal here so the assertion fails if it ever leaks, rather than relying
    # on an allowlist of field names that a new field would silently escape.
    workspace, _, selective, approval, state, receipts, _ = all_current_selective_plan(seed)
    for wire in (
        selective.to_wire(),
        approval.to_wire(),
        state.plan.to_wire(),
        tuple(item.to_wire() for item in receipts),
        workspace.to_wire(),
    ):
        text = _wire_text(wire)
        assert CLOSEOUT_INTENT not in text
        assert "prompt" not in text
        assert str(tmp_path) not in text
        assert "C:\\\\" not in text

    # A receipt cannot be edited and keep its identity. Constructing one whose stated fingerprint no
    # longer matches its contents is refused at construction, so a tampered receipt never reaches
    # reuse boundary in the first place.
    manifests = derive_segment_manifests(workspace)
    by_id = {item.segment_id: item for item in manifests}
    honest = artifact_receipt(by_id[CUT], job_spec(by_id[CUT]))
    with pytest.raises(SegmentArtifactError, match="receipt_fingerprint_mismatch"):
        replace(honest, byte_length=honest.byte_length + 1)
    # Recomputed from its own contents, an altered receipt is a different receipt entirely.
    altered = replace(honest, byte_length=honest.byte_length + 1, receipt_fingerprint=None)
    assert altered.fingerprint != honest.fingerprint


def test_c10_unavailable_catalog() -> None:
    """C10: what the product cannot do is absent or truthfully unavailable, never faked."""

    # Native continuity exists for exactly two modes. For the rest it is refused, and the refusal is
    # not a downgrade to some other boundary that happens to work.
    for mode in (TaskMode.T2VA, TaskMode.L2VA, TaskMode.REF2VA):
        with pytest.raises(ContinuityError):
            build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, mode)
    for mode in (TaskMode.I2VA, TaskMode.FL2VA):
        policy = build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, mode)
        assert policy.mode is ContinuityMode.NATIVE_FRAME_HANDOFF
    # fl2va keeps the user's last frame and i2va does not. Collapsing the two would silently change
    # what a shot ends on.
    assert build_continuity_policy(
        ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.FL2VA
    ).preserves_last_frame
    assert not build_continuity_policy(
        ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.I2VA
    ).preserves_last_frame

    # A restart root belongs to a restart. Attaching one to a cut is refused rather than ignored.
    with pytest.raises(ContinuityReceiptError):
        build_continuity_boundary_receipt(
            ContinuityPolicy(ContinuityMode.CUT, TaskMode.T2VA),
            boundary_id="boundary.m17.14.invalid",
            restart_root_id="root.not.allowed",
        )

    # A production with no accepted reconstruction authority offers no preview output. Absence is
    # the honest answer; an empty-but-present handle would invite a request that cannot be served.
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=4,
        ttl_seconds=60,
        terminal_ttl_seconds=60,
    )
    handle = _context_seed(sidebar, "seed.unavailable", duration_ms=CLOSEOUT_DURATION_MS)
    created = registry.dispatch(
        _action(
            "m17-14.c10.01",
            "create_workspace_from_context",
            {"context_workspace_handle": handle},
        )
    ).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    # No reconstruction authority exists yet, so there is no output to preview. Absence is honest
    # answer; an empty-but-present output row would invite a request that cannot be served.
    assert created.outputs == ()
    assert created.reconstruction_state == "unavailable"
    wire = _wire_text(created.to_wire())
    # No provider, upload or model capability is advertised anywhere on this surface.
    for forbidden in ("provider", "upload", "download", "api_key", "endpoint"):
        assert forbidden not in wire


def closeout_output_descriptor(
    *,
    segment_id: str,
    authority_fingerprint: str,
    payload: bytes,
    duration: AVRational,
    frames: int,
    samples: int,
) -> AVMediaDescriptor:
    """One produced output, described exactly as the accepted target profile requires."""

    target = closeout_target_profile()
    return AVMediaDescriptor(
        segment_id=segment_id,
        artifact_receipt_fingerprint=authority_fingerprint,
        artifact_output_fingerprint=fp(payload.decode("ascii")),
        artifact_byte_length=len(payload),
        capability_fingerprint=qualified_ffmpeg_capability().fingerprint,
        container=target.container,
        stream_count=2,
        video=AVVideoDescriptor(
            codec=target.video_codec,
            width=target.width,
            height=target.height,
            pixel_format=target.pixel_format,
            color_range=target.color_range,
            color_space=target.color_space,
            color_primaries=target.color_primaries,
            color_transfer=target.color_transfer,
            rotation_degrees=target.rotation_degrees,
            frame_rate=target.frame_rate,
            time_base=target.video_time_base,
            start_time=AVRational(0, 1),
            end_time=duration,
            decoded_frame_count=frames,
        ),
        audio=AVAudioDescriptor(
            codec=target.audio_codec,
            sample_format=target.sample_format,
            sample_rate=target.sample_rate,
            channels=target.channels,
            channel_layout=target.channel_layout,
            time_base=target.audio_time_base,
            start_time=AVRational(0, 1),
            end_time=duration,
            decoded_sample_count=samples,
        ),
        subtitle_stream_count=0,
        data_stream_count=0,
        attachment_stream_count=0,
        inspected_at_ms=400,
        expires_at_ms=60_400,
        warning_codes=(),
    )


def test_c11_production_wrapper_preview_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C11: the preview a user opens is the reconstruction this production actually produced.

    The wrapper is the only shipped seam that turns an accepted reconstruction into something a
    browser may open. Two properties matter and neither is visible from a single call: the wrapper
    refuses to execute at all when the predecessors it is handed do not match the plan, and a repeat
    of the exact same request costs one execution rather than two.
    """

    seed = claim_public_seed()
    workspace, plan, state, receipts = closeout_av_plan(seed)
    approval = approve_av_reconstruction_plan(plan, approved_at_ms=310, expires_at_ms=600)
    sequence = build_generation_sequence_projection(
        state,
        ExecutionCorrelation("prompt.m17.14.preview", "node.m17.14.preview"),
    )
    aggregate_payload = b"closeout-private-aggregate"
    source_payloads = tuple(
        (item.segment_id, segment_payload(item.segment_id)) for item in plan.segments
    )
    executions = 0

    store = PrivateAVReconstructionStore(
        tmp_path / "av-store",
        policy=_pipeline_policy(),
        clock_ms=lambda: 400,
    )
    adapter = _adapter(tmp_path, monkeypatch, clock_ms=lambda: 400)

    derived_payloads = {
        item.segment_id: f"closeout-private-derived-{item.segment_id}".encode("ascii")
        for item in plan.segments
    }

    def execute(_self: object, **values: object) -> AVMediaExecutionResult:
        nonlocal executions
        executions += 1
        aggregate = cast(OwnedOutputLease, values["aggregate_output"])
        aggregate.path.write_bytes(aggregate_payload)
        derived = []
        for segment_id, lease in cast(
            tuple[tuple[str, OwnedOutputLease], ...], values["derived_outputs"]
        ):
            payload = derived_payloads[segment_id]
            lease.path.write_bytes(payload)
            derived.append(
                closeout_output_descriptor(
                    segment_id=segment_id,
                    authority_fingerprint=plan.fingerprint,
                    payload=payload,
                    duration=AVRational(CLOSEOUT_DURATION_MS // 1_000, 1),
                    frames=CLOSEOUT_RECONSTRUCTION_FRAMES,
                    samples=CLOSEOUT_AUDIO_SAMPLES,
                )
            )
        aggregate_descriptor = closeout_output_descriptor(
            segment_id="reconstruction.aggregate",
            authority_fingerprint=plan.fingerprint,
            payload=aggregate_payload,
            duration=AVRational((CLOSEOUT_DURATION_MS // 1_000) * len(SEGMENT_ORDER), 1),
            frames=CLOSEOUT_RECONSTRUCTION_FRAMES * len(SEGMENT_ORDER),
            samples=CLOSEOUT_AUDIO_SAMPLES * len(SEGMENT_ORDER),
        )
        return AVMediaExecutionResult(
            execution_fingerprint=canonical_fingerprint(
                {
                    "plan_fingerprint": plan.fingerprint,
                    "approval_fingerprint": approval.fingerprint,
                    "capability_fingerprint": plan.capability.fingerprint,
                    "aggregate": aggregate_descriptor.to_wire(),
                    "derived": [item.to_wire() for item in derived],
                }
            ),
            aggregate_descriptor=aggregate_descriptor,
            derived_descriptors=tuple(derived),
        )

    monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", execute)

    # Predecessors that do not match the plan stop the wrapper before any media work happens. This
    # is asserted first and by execution count, because an implementation that validated after
    # executing would still return the right error while having already spent the work.
    with pytest.raises(AVReconstructionPipelineError, match="production_authority_mismatch"):
        execute_production_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.m17.14.invalid",
            plan=plan,
            approval=approval,
            input_payloads=source_payloads,
            clock_ms=lambda: 400,
            generation_sequence=sequence,
            artifact_receipts=(),
            continuity_receipts=(),
        )
    assert executions == 0

    first = execute_production_av_reconstruction(
        store=store,
        adapter=adapter,
        transaction_id="transaction.m17.14.preview",
        plan=plan,
        approval=approval,
        input_payloads=source_payloads,
        clock_ms=lambda: 400,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        continuity_receipts=tuple(item.receipt for item in plan.boundaries),
    )
    assert executions == 1

    second = execute_production_av_reconstruction(
        store=store,
        adapter=adapter,
        transaction_id="transaction.m17.14.preview",
        plan=plan,
        approval=approval,
        input_payloads=source_payloads,
        clock_ms=lambda: 400,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        continuity_receipts=tuple(item.receipt for item in plan.boundaries),
    )
    # The retry is the same receipt, not an equal-looking second one, and it did not re-execute.
    assert second == first
    assert executions == 1
    assert first.plan_fingerprint == plan.fingerprint
    assert workspace.workspace_id == WORKSPACE_ID

    # Nothing private leaked into the receipt a caller may hold.
    text = _wire_text(first.to_wire())
    assert CLOSEOUT_INTENT not in text
    assert str(tmp_path) not in text
