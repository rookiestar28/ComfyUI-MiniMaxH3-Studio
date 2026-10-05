"""Join retained managed child completions without inventing an aggregate execution.

The adapter supplies the exact original workspace and the child states/receipts it retained
at capture. This pure join does not execute jobs, mint receipts, or establish live ownership.
"""

from __future__ import annotations

from .canonical import canonical_fingerprint
from .generation_sequence import (
    GenerationJobRuntime,
    GenerationJobState,
    GenerationSequenceError,
    GenerationSequenceJob,
    GenerationSequencePlan,
    GenerationSequenceProjection,
    GenerationSequenceState,
    artifact_receipt_matches_job_contract,
    build_generation_sequence_projection,
)
from .pipeline_transaction import PipelineTransactionState
from .segment_artifacts import ArtifactLifecycleState, SegmentArtifactReceipt
from .segment_workspace import (
    MAX_WORKSPACE_SEGMENTS,
    MultiSegmentWorkspace,
    SegmentContextManifest,
    SegmentRelationKind,
    derive_segment_manifests,
)
from .ui_projection import ExecutionCorrelation


def _validate_child(
    workspace: MultiSegmentWorkspace,
    manifests: tuple[SegmentContextManifest, ...],
    child: GenerationSequenceState,
    receipt: SegmentArtifactReceipt,
    ordinal: int,
) -> tuple[GenerationSequenceJob, GenerationJobRuntime]:
    plan = child.plan
    manifest = manifests[ordinal]
    manifest_fingerprints = tuple(item.fingerprint for item in manifests)
    if (
        plan.source_workspace_authority is not workspace
        or plan.workspace_id != workspace.workspace_id
        or plan.workspace_revision != workspace.revision
        or plan.workspace_fingerprint != workspace.fingerprint
        or plan.manifest_fingerprints != manifest_fingerprints
        or plan.max_concurrency != 1
        or len(plan.jobs) != 1
        or len(child.runtimes) != 1
        or child.cancellation_requested
        or plan.clean_segment_ids
        != tuple(item.segment_id for item in manifests if item is not manifest)
    ):
        raise GenerationSequenceError("managed_publication_child_authority")
    job = plan.jobs[0]
    runtime = child.runtimes[0]
    transaction = runtime.transaction
    if (
        job.segment_id != manifest.segment_id
        or job.ordinal != manifest.ordinal
        or job.manifest_fingerprint != manifest.fingerprint
        or job.dependency_segment_ids
        or job.predecessor_artifact_fingerprint is not None
        or job.predecessor_receipt_fingerprint is not None
        or runtime.job_id != job.job_id
        or runtime.state is not GenerationJobState.SUCCEEDED
        or transaction is None
        or transaction.state is not PipelineTransactionState.SUCCEEDED
        or transaction.cancellation_requested
        or transaction.workspace_id != workspace.workspace_id
        or transaction.workspace_revision != workspace.revision
        or transaction.workspace_fingerprint != workspace.fingerprint
        or transaction.manifest_fingerprints != manifest_fingerprints
        or transaction.recompute_plan_fingerprint != plan.recompute_plan_fingerprint
        or transaction.dirty_segment_ids != (job.segment_id,)
        or transaction.compiled_prompt_fingerprint != job.compiled_prompt_fingerprint
        or transaction.queue_prompt_id is None
        or transaction.host_owner_id is None
        or transaction.result_fingerprint is None
    ):
        raise GenerationSequenceError("managed_publication_child_completion")
    # IMPORTANT: a receipt binds the real pre-success transaction. Keep the accepted
    # runtime/receipt pair; re-keying its transaction to the joined plan fabricates execution.
    if (
        receipt.state is not ArtifactLifecycleState.COMPLETE
        or receipt.workspace_id != workspace.workspace_id
        or receipt.workspace_revision != workspace.revision
        or receipt.workspace_fingerprint != workspace.fingerprint
        or receipt.segment_id != job.segment_id
        or receipt.manifest_fingerprint != job.manifest_fingerprint
        or receipt.producer_fingerprint != job.producer_fingerprint
        or receipt.native_binding_fingerprint != job.native_binding_fingerprint
        or receipt.model_fingerprint != job.model_fingerprint
        or receipt.runtime_fingerprint != job.runtime_fingerprint
        or receipt.settings_fingerprint != job.settings_fingerprint
        or receipt.source_id != job.source_id
        or receipt.graph_fingerprint != transaction.graph_fingerprint
        or receipt.predecessor_artifact_fingerprint is not None
        or not artifact_receipt_matches_job_contract(job, receipt)
        or receipt.output_fingerprint is None
        or runtime.artifact_receipt_fingerprint != receipt.fingerprint
        or runtime.artifact_output_fingerprint != receipt.output_fingerprint
    ):
        raise GenerationSequenceError("managed_publication_receipt_identity")
    return job, runtime


def compose_completed_managed_generation(
    workspace: MultiSegmentWorkspace,
    child_states: tuple[GenerationSequenceState, ...],
    artifact_receipts: tuple[SegmentArtifactReceipt, ...],
    correlation: ExecutionCorrelation,
) -> GenerationSequenceProjection:
    """Publish a terminal union of actual independent children in original segment order.

    The joined plan identifies this publication only. Each retained transaction continues
    to identify its own executed child plan. No command is eligible from this terminal state.
    Live authority, receipt/store ownership and idempotent CAS remain adapter obligations.
    """

    if type(workspace) is not MultiSegmentWorkspace:
        raise GenerationSequenceError("managed_publication_workspace")
    if (
        type(child_states) is not tuple
        or not 1 <= len(child_states) <= MAX_WORKSPACE_SEGMENTS
        or len(child_states) != len(workspace.segments)
        or not all(type(item) is GenerationSequenceState for item in child_states)
        or type(artifact_receipts) is not tuple
        or len(artifact_receipts) != len(child_states)
        or not all(type(item) is SegmentArtifactReceipt for item in artifact_receipts)
    ):
        raise GenerationSequenceError("managed_publication_cardinality")
    manifests = derive_segment_manifests(workspace)
    if any(
        item.relation not in {SegmentRelationKind.INDEPENDENT, SegmentRelationKind.CUT}
        or item.dependency_segment_ids
        for item in manifests
    ):
        raise GenerationSequenceError("managed_publication_dependent_segment")
    pairs = tuple(
        _validate_child(workspace, manifests, child, receipt, ordinal)
        for ordinal, (child, receipt) in enumerate(
            zip(child_states, artifact_receipts, strict=True)
        )
    )
    transactions = tuple(runtime.transaction for _, runtime in pairs)
    if len({item.transaction_id for item in transactions if item is not None}) != len(pairs) or (
        len({item.queue_prompt_id for item in transactions if item is not None}) != len(pairs)
    ):
        raise GenerationSequenceError("managed_publication_duplicate_execution")
    join_fingerprint = canonical_fingerprint(
        {
            "kind": "completed_managed_generation_publication.v1",
            "workspace_fingerprint": workspace.fingerprint,
            "children": [
                {"plan": item.plan.fingerprint, "state": item.fingerprint} for item in child_states
            ],
            "receipts": [item.fingerprint for item in artifact_receipts],
        }
    )
    plan = GenerationSequencePlan(
        sequence_id=f"managed.completed.{join_fingerprint.removeprefix('sha256:')}",
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        recompute_plan_fingerprint=join_fingerprint,
        manifest_fingerprints=tuple(item.fingerprint for item in manifests),
        jobs=tuple(job for job, _ in pairs),
        clean_segment_ids=(),
        source_workspace_authority=workspace,
    )
    state = GenerationSequenceState(
        plan=plan,
        runtimes=tuple(runtime for _, runtime in pairs),
        observation_count=sum(item.observation_count for item in child_states),
    )
    return build_generation_sequence_projection(state, correlation)
