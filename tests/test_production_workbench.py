"""M17-12 bounded Production workbench core and adapter tests."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from hashlib import sha256

import pytest

from comfyui_h3_context.adapters.comfyui_production_workspace import (
    MAX_PRODUCTION_ACTION_BYTES,
    PRODUCTION_ACTION_SCHEMA,
    ProductionAcceptedAuthorities,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
    _is_structural_successor,
    decode_production_action_json,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SidebarProductionSeed,
    SidebarWorkspaceRegistry,
)
from comfyui_h3_context.core import (
    AcceptedIntentAuthority,
    ArtifactLifecycleState,
    AVOperation,
    AVOutputKind,
    AVPublicationState,
    AVRational,
    AVReceiptOutput,
    AVReceiptSegmentResult,
    AVReconstructionReceipt,
    AVStreamAccounting,
    ContextReport,
    ContinuityBoundaryReceipt,
    ContinuityMode,
    ExecutionCorrelation,
    FingerprintDomain,
    GenerationJobSpec,
    GenerationSequenceProjection,
    MultiSegmentWorkspace,
    NativeH3Wiring,
    ProductionDeliveredVideoProjection,
    ProductionOutputProjection,
    ProductionSegmentProjection,
    ProductionWorkbenchProjection,
    ProductionWorkbenchProjectionError,
    SegmentArtifactReceipt,
    SegmentContextManifest,
    SegmentDeclaration,
    SegmentRelationKind,
    TaskMode,
    begin_segment_artifact_receipt,
    build_generation_sequence_plan,
    build_generation_sequence_projection,
    build_native_h3_wiring,
    build_production_workbench_projection,
    cancel_generation_sequence,
    complete_segment_artifact_receipt,
    create_generation_sequence_state,
    create_workspace,
    derive_segment_manifests,
    plan_recompute,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
    revise_workspace,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)


def test_delivered_geometry_requires_complete_artifact_and_stays_content_free() -> None:
    geometry = ProductionDeliveredVideoProjection(
        format_label="mkv",
        frame_count=124,
        width=768,
        height=512,
    )
    segment = ProductionSegmentProjection(
        segment_id="segment.delivered",
        ordinal=1,
        task_mode="i2va",
        duration_milliseconds=5167,
        delivered_milliseconds=5167,
        frame_count=124,
        snapped=False,
        relation="independent",
        predecessor_segment_id=None,
        boundary_kind="independent",
        artifact_state="complete",
        delivered_geometry=geometry,
    )

    assert segment.to_wire()["delivered_geometry"] == {
        "format": "mkv",
        "frame_count": 124,
        "width": 768,
        "height": 512,
    }
    with pytest.raises(ProductionWorkbenchProjectionError, match="premature"):
        replace(segment, artifact_state="partial")


def _authority(
    prompt: str = "A safe operator-owned prompt.",
) -> tuple[ContextReport, NativeH3Wiring, ExecutionCorrelation]:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        prompt,
        duration_seconds=5.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    return report, build_native_h3_wiring(report), ExecutionCorrelation("prompt-1", "17")


def _setup() -> tuple[SidebarWorkspaceRegistry, ProductionWorkspaceRegistry, str]:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    report, wiring, correlation = _authority()
    sidebar_projection = sidebar.publish(report, wiring, correlation)
    production = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=4,
        ttl_seconds=60,
        terminal_ttl_seconds=60,
    )
    return sidebar, production, sidebar_projection.workspace_id


def _create(seed_handle: str, request_id: str = "request.create") -> dict[str, object]:
    return {
        "schema": PRODUCTION_ACTION_SCHEMA,
        "request_id": request_id,
        "action": "create_workspace_from_context",
        "payload": {"context_workspace_handle": seed_handle},
    }


def _mutation(
    projection: ProductionWorkbenchProjection,
    *,
    request_id: str,
    action: str,
    extra: dict[str, object],
) -> dict[str, object]:
    return {
        "schema": PRODUCTION_ACTION_SCHEMA,
        "request_id": request_id,
        "action": action,
        "payload": {
            "workspace_handle": projection.workspace_handle,
            "expected_workspace_revision": projection.workspace_revision,
            "expected_workspace_fingerprint": projection.workspace_fingerprint,
            **extra,
        },
    }


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _fp(label: str) -> str:
    return f"sha256:{sha256(label.encode('ascii')).hexdigest()}"


@pytest.mark.parametrize("operation", ("admit", "current"))
def test_preview_registry_identity_lookup_fails_busy_without_waiting_on_host_thread(
    operation: str,
) -> None:
    def missing_seed(handle: str) -> SidebarProductionSeed:
        raise KeyError(handle)

    registry = ProductionWorkspaceRegistry(seed_claim=missing_seed)
    entered = threading.Event()
    release = threading.Event()

    def hold_registry_lock() -> None:
        with registry._lock:
            entered.set()
            assert release.wait(timeout=1.0)

    def invoke() -> object:
        if operation == "admit":
            return registry.admit_media_preview_source(
                workspace_handle="pw_" + "a" * 40,
                expected_workspace_revision=1,
                expected_workspace_fingerprint="sha256:" + "b" * 64,
                output_handle="out_" + "c" * 40,
            )
        return registry.media_preview_source_is_current(
            workspace_handle="pw_" + "a" * 40,
            expected_workspace_revision=1,
            expected_workspace_fingerprint="sha256:" + "b" * 64,
            source=object(),  # type: ignore[arg-type]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_registry_lock)
        assert entered.wait(timeout=1.0)
        contender = executor.submit(invoke)
        try:
            with pytest.raises(ProductionWorkbenchError) as error:
                contender.result(timeout=0.25)
            assert (error.value.code, error.value.status) == ("workspace_busy", 423)
        finally:
            release.set()
        holder.result(timeout=1.0)


def _sequence_for_registry(
    registry: ProductionWorkspaceRegistry,
    projection: ProductionWorkbenchProjection,
) -> GenerationSequenceProjection:
    workspace = registry._entries[projection.workspace_handle].workspace
    current_manifests = derive_segment_manifests(workspace)
    previous_segments = tuple(
        replace(
            item,
            producer_settings_fingerprint=_fp(f"previous.{item.segment_id}"),
        )
        for item in workspace.segments
    )
    previous = create_workspace(
        workspace.workspace_id,
        previous_segments,
        accepted_intent_authorities=tuple(
            AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
            for item in previous_segments
        ),
        selected_segment_ids=workspace.selected_segment_ids,
    )
    recompute = plan_recompute(derive_segment_manifests(previous), current_manifests)
    specs = tuple(
        GenerationJobSpec(
            segment_id=item.segment_id,
            job_id=f"job.{index}",
            graph_fingerprint=_fp(f"graph.{item.segment_id}"),
            compiled_prompt_fingerprint=_fp(f"prompt.{item.segment_id}"),
            model_fingerprint=_fp("model.h3"),
            runtime_fingerprint=_fp("runtime.comfyui"),
            expected_format="video.mp4",
            expected_shape=(124, 512, 512, 3),
            timeout_ms=60_000,
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )
        for index, item in enumerate(workspace.segments, start=1)
    )
    plan = build_generation_sequence_plan(workspace, current_manifests, recompute, specs)
    state = create_generation_sequence_state(plan)
    return build_generation_sequence_projection(
        state,
        ExecutionCorrelation("prompt.production", "node.production"),
    )


def _source_sequence_for_seed(
    seed: SidebarProductionSeed,
    *,
    workspace_id: str = "workspace.staged.source",
    segment_id: str = "segment.staged.source",
) -> tuple[MultiSegmentWorkspace, GenerationSequenceProjection]:
    declaration = SegmentDeclaration(
        segment_id=segment_id,
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
    source = create_workspace(
        workspace_id,
        (declaration,),
        accepted_intent_authorities=(
            AcceptedIntentAuthority(segment_id, seed.accepted_intent_fingerprint),
        ),
        selected_segment_ids=(segment_id,),
    )
    previous_declaration = replace(
        declaration,
        producer_settings_fingerprint=_fp(f"previous.{workspace_id}"),
    )
    previous = create_workspace(
        workspace_id,
        (previous_declaration,),
        accepted_intent_authorities=(
            AcceptedIntentAuthority(segment_id, seed.accepted_intent_fingerprint),
        ),
        selected_segment_ids=(segment_id,),
    )
    manifests = derive_segment_manifests(source)
    recompute = plan_recompute(derive_segment_manifests(previous), manifests)
    plan = build_generation_sequence_plan(
        source,
        manifests,
        recompute,
        (
            GenerationJobSpec(
                segment_id=segment_id,
                job_id=f"job.{segment_id}",
                graph_fingerprint=_fp(f"graph.{workspace_id}"),
                compiled_prompt_fingerprint=_fp(f"prompt.{workspace_id}"),
                model_fingerprint=_fp("model.h3"),
                runtime_fingerprint=_fp("runtime.comfyui"),
                expected_format="video.mp4",
                expected_shape=(124, 512, 512, 3),
                timeout_ms=60_000,
                fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
            ),
        ),
    )
    return source, build_generation_sequence_projection(
        create_generation_sequence_state(plan),
        ExecutionCorrelation("prompt.staged", "node.staged"),
    )


def _artifact_for_manifest(
    manifest: SegmentContextManifest,
    *,
    label: str,
) -> SegmentArtifactReceipt:
    return SegmentArtifactReceipt(
        artifact_id=f"artifact.{label}",
        state=ArtifactLifecycleState.COMPLETE,
        workspace_id=manifest.workspace_id,
        workspace_revision=manifest.workspace_revision,
        workspace_fingerprint=manifest.workspace_fingerprint,
        segment_id=manifest.segment_id,
        manifest_fingerprint=manifest.fingerprint,
        producer_fingerprint=manifest.producer_fingerprint,
        transaction_fingerprint=_fp(f"transaction.{label}"),
        graph_fingerprint=_fp(f"graph.{label}"),
        native_binding_fingerprint=manifest.native_binding_fingerprint,
        model_fingerprint=_fp(f"model.{label}"),
        runtime_fingerprint=_fp(f"runtime.{label}"),
        settings_fingerprint=manifest.producer_settings_fingerprint,
        source_id=manifest.source_id,
        predecessor_artifact_fingerprint=None,
        execution_fingerprint=_fp(f"execution.{label}"),
        format_label="video.mp4",
        shape=(30, 512, 512, 3),
        created_at_ms=100,
        expires_at_ms=10_000,
        output_fingerprint=_fp(f"output.{label}"),
        byte_length=8192,
    )


def test_registry_stages_and_atomically_adopts_exact_builder_source_once() -> None:
    sidebar, registry, seed_handle = _setup()
    seed = sidebar.claim_production_seed(seed_handle)
    source, sequence = _source_sequence_for_seed(seed)

    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None
    adopted = registry.dispatch(_create(seed_handle, "request.adopt.staged")).projection

    assert isinstance(adopted, ProductionWorkbenchProjection)
    assert adopted.workspace_id == source.workspace_id
    assert adopted.workspace_revision == source.revision
    assert adopted.workspace_fingerprint == source.fingerprint
    assert tuple(item.segment_id for item in adopted.segments) == tuple(
        item.segment_id for item in source.segments
    )
    assert adopted.generation_sequence is not None
    assert adopted.generation_sequence.state_fingerprint == sequence.state.fingerprint
    assert "submit_generation_job" in adopted.allowed_actions

    ordinary = registry.dispatch(_create(seed_handle, "request.after.consume")).projection
    assert isinstance(ordinary, ProductionWorkbenchProjection)
    assert ordinary.workspace_id != source.workspace_id


def test_staged_adoption_retains_project_editor_seed_after_context_loss() -> None:
    """M25-40 class sweep: the planned origin prepares its editor like manual and managed ones."""

    sidebar, registry, seed_handle = _setup()
    seed = sidebar.claim_production_seed(seed_handle)
    _source, sequence = _source_sequence_for_seed(seed)
    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None
    adopted = registry.dispatch(_create(seed_handle, "request.adopt.staged.seed")).projection
    assert isinstance(adopted, ProductionWorkbenchProjection)
    assert adopted.generation_sequence is not None

    def expired_context(_handle: str) -> SidebarProductionSeed:
        raise KeyError("original Context expired")

    registry._seed_claim = expired_context
    retained = registry.claim_authoring_seed_for_project(
        adopted.workspace_handle, adopted.workspace_id
    )
    assert retained.source_id == seed.source_id
    assert retained.task_mode is seed.task_mode
    assert retained.lineage_token is seed.lineage_token
    with pytest.raises(ProductionWorkbenchError) as foreign:
        registry.claim_authoring_seed_for_project(adopted.workspace_handle, "workspace.foreign")
    assert (foreign.value.status, foreign.value.code) == (409, "destination_mismatch")


def test_staged_publication_is_absolute_ttl_and_does_not_refresh() -> None:
    clock = _Clock()
    sidebar, _, seed_handle = _setup()
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=4,
        ttl_seconds=60,
        terminal_ttl_seconds=60,
        clock=clock,
    )
    seed = sidebar.claim_production_seed(seed_handle)
    source, sequence = _source_sequence_for_seed(seed)

    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None
    clock.advance(899)
    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None
    clock.advance(2)

    ordinary = registry.dispatch(_create(seed_handle, "request.expired.stage")).projection
    assert isinstance(ordinary, ProductionWorkbenchProjection)
    assert ordinary.workspace_id != source.workspace_id


def test_distinct_context_handle_cannot_stage_or_inherit_same_source_lineage() -> None:
    sidebar, registry, first_handle = _setup()
    report, wiring, correlation = _authority()
    second_handle = sidebar.publish(report, wiring, correlation).workspace_id
    first_seed = sidebar.claim_production_seed(first_handle)
    source, sequence = _source_sequence_for_seed(first_seed)

    assert registry.publish_generation_sequence_authority(sequence, first_handle) is None
    assert registry.publish_generation_sequence_authority(sequence, second_handle) is None

    second_created = registry.dispatch(
        _create(second_handle, "request.second.nonretained")
    ).projection
    assert isinstance(second_created, ProductionWorkbenchProjection)
    assert second_created.workspace_id != source.workspace_id

    first_created = registry.dispatch(_create(first_handle, "request.first.adopts")).projection
    assert isinstance(first_created, ProductionWorkbenchProjection)
    assert first_created.workspace_id == source.workspace_id


def test_concurrent_context_handles_produce_one_exact_adoption_without_fork() -> None:
    sidebar, registry, first_handle = _setup()
    report, wiring, correlation = _authority()
    second_handle = sidebar.publish(report, wiring, correlation).workspace_id
    source, sequence = _source_sequence_for_seed(sidebar.claim_production_seed(first_handle))

    with ThreadPoolExecutor(max_workers=2) as pool:
        publications = tuple(
            pool.map(
                lambda handle: registry.publish_generation_sequence_authority(sequence, handle),
                (first_handle, second_handle),
            )
        )
    assert publications == (None, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = tuple(
            pool.map(
                lambda pair: registry.dispatch(_create(pair[0], pair[1])).projection,
                (
                    (first_handle, "request.concurrent.context.first"),
                    (second_handle, "request.concurrent.context.second"),
                ),
            )
        )
    assert all(item is not None for item in created)
    assert sum(item.workspace_id == source.workspace_id for item in created if item) == 1
    assert len({item.workspace_handle for item in created if item}) == 2


def test_different_revision_same_source_id_is_nonretaining_for_second_handle() -> None:
    sidebar, registry, first_handle = _setup()
    report, wiring, correlation = _authority()
    second_handle = sidebar.publish(report, wiring, correlation).workspace_id
    seed = sidebar.claim_production_seed(first_handle)
    source, sequence = _source_sequence_for_seed(seed)
    revised = revise_workspace(
        source,
        expected_workspace_fingerprint=source.fingerprint,
        accepted_intent_authorities=(
            AcceptedIntentAuthority(
                source.segments[0].segment_id,
                source.segments[0].accepted_intent_fingerprint,
            ),
        ),
        selected_segment_ids=(),
    )
    manifests = derive_segment_manifests(revised)
    recompute = plan_recompute(derive_segment_manifests(source), manifests)
    specs = tuple(
        GenerationJobSpec(
            segment_id=segment_id,
            job_id=f"job.revised.{index}",
            graph_fingerprint=_fp(f"graph.revised.{index}"),
            compiled_prompt_fingerprint=_fp(f"prompt.revised.{index}"),
            model_fingerprint=_fp("model.revised"),
            runtime_fingerprint=_fp("runtime.revised"),
            expected_format="video.mp4",
            expected_shape=(124, 512, 512, 3),
            timeout_ms=60_000,
            fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
        )
        for index, segment_id in enumerate(recompute.mandatory_segment_ids, start=1)
    )
    revised_sequence = build_generation_sequence_projection(
        create_generation_sequence_state(
            build_generation_sequence_plan(revised, manifests, recompute, specs)
        ),
        ExecutionCorrelation("prompt.revised", "node.revised"),
    )

    assert registry.publish_generation_sequence_authority(sequence, first_handle) is None
    assert registry.publish_generation_sequence_authority(revised_sequence, second_handle) is None
    second_created = registry.dispatch(
        _create(second_handle, "request.revised.nonretained")
    ).projection
    assert isinstance(second_created, ProductionWorkbenchProjection)
    assert second_created.workspace_id != source.workspace_id
    first_created = registry.dispatch(_create(first_handle, "request.original.retained")).projection
    assert isinstance(first_created, ProductionWorkbenchProjection)
    assert first_created.workspace_id == source.workspace_id


def test_retained_stage_validation_failure_is_422_and_retry_keeps_exact_stage() -> None:
    sidebar, _, seed_handle = _setup()
    exact_seed = sidebar.claim_production_seed(seed_handle)
    claimed = {seed_handle: exact_seed}
    registry = ProductionWorkspaceRegistry(seed_claim=claimed.__getitem__)
    source, sequence = _source_sequence_for_seed(exact_seed)
    request = _create(seed_handle, "request.retained.invalid")

    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None
    claimed[seed_handle] = replace(
        exact_seed,
        producer_settings_fingerprint=_fp("changed.after.stage"),
    )
    with pytest.raises(ProductionWorkbenchError) as rejected:
        registry.dispatch(request)
    assert (rejected.value.status, rejected.value.code, rejected.value.projection) == (
        422,
        "action_rejected",
        None,
    )

    claimed[seed_handle] = exact_seed
    adopted = registry.dispatch(request).projection
    assert isinstance(adopted, ProductionWorkbenchProjection)
    assert adopted.workspace_id == source.workspace_id
    assert adopted.workspace_fingerprint == source.fingerprint


def test_retained_invalid_stage_precedes_ledger_capacity_and_survives_retry() -> None:
    clock = _Clock()
    sidebar = SidebarWorkspaceRegistry(max_entries=2, ttl_seconds=60, clock=clock)
    report, wiring, correlation = _authority()
    first_handle = sidebar.publish(report, wiring, correlation).workspace_id
    second_handle = sidebar.publish(report, wiring, correlation).workspace_id
    exact_second_seed = sidebar.claim_production_seed(second_handle)
    claimed = {
        first_handle: sidebar.claim_production_seed(first_handle),
        second_handle: exact_second_seed,
    }
    registry = ProductionWorkspaceRegistry(
        seed_claim=claimed.__getitem__,
        ttl_seconds=1,
        terminal_ttl_seconds=1,
        max_ledger_entries=1,
        clock=clock,
    )
    assert registry.dispatch(_create(first_handle, "request.ledger.owner")).status == 201
    source, sequence = _source_sequence_for_seed(exact_second_seed)
    assert registry.publish_generation_sequence_authority(sequence, second_handle) is None
    claimed[second_handle] = replace(
        exact_second_seed,
        producer_settings_fingerprint=_fp("changed.with.full.ledger"),
    )

    with pytest.raises(ProductionWorkbenchError) as rejected:
        registry.dispatch(_create(second_handle, "request.invalid.before.capacity"))
    assert (rejected.value.status, rejected.value.code, rejected.value.projection) == (
        422,
        "action_rejected",
        None,
    )

    claimed[second_handle] = exact_second_seed
    clock.advance(2)
    assert registry.publish_generation_sequence_authority(sequence, second_handle) is None
    clock.advance(2)
    assert registry.publish_generation_sequence_authority(sequence, second_handle) is None
    adopted = registry.dispatch(
        _create(second_handle, "request.invalid.before.capacity")
    ).projection
    assert isinstance(adopted, ProductionWorkbenchProjection)
    assert adopted.workspace_id == source.workspace_id
    assert adopted.workspace_fingerprint == source.fingerprint


def test_staged_entry_counts_against_combined_capacity_without_eviction() -> None:
    sidebar, _, first_handle = _setup()
    report, wiring, correlation = _authority()
    second_handle = sidebar.publish(report, wiring, correlation).workspace_id
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=1,
    )
    source, sequence = _source_sequence_for_seed(sidebar.claim_production_seed(first_handle))
    assert registry.publish_generation_sequence_authority(sequence, first_handle) is None

    with pytest.raises(ProductionWorkbenchError) as full:
        registry.dispatch(_create(second_handle, "request.stage.capacity"))
    assert (full.value.status, full.value.code) == (429, "workspace_capacity")

    adopted = registry.dispatch(_create(first_handle, "request.stage.capacity.adopt")).projection
    assert isinstance(adopted, ProductionWorkbenchProjection)
    assert adopted.workspace_id == source.workspace_id


def test_fresh_publication_after_release_is_required_before_readoption() -> None:
    sidebar, registry, seed_handle = _setup()
    source, sequence = _source_sequence_for_seed(sidebar.claim_production_seed(seed_handle))
    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None
    original_request = _create(seed_handle, "request.fresh.original")
    adopted = registry.dispatch(original_request).projection
    assert isinstance(adopted, ProductionWorkbenchProjection)
    assert adopted.workspace_id == source.workspace_id
    released = registry.dispatch(
        _mutation(
            adopted,
            request_id="request.fresh.release",
            action="release_workspace",
            extra={},
        )
    )
    assert released.status == 204

    ordinary = registry.dispatch(_create(seed_handle, "request.fresh.ordinary")).projection
    assert isinstance(ordinary, ProductionWorkbenchProjection)
    assert ordinary.workspace_id != source.workspace_id
    registry.dispatch(
        _mutation(
            ordinary,
            request_id="request.fresh.ordinary.release",
            action="release_workspace",
            extra={},
        )
    )

    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None
    with pytest.raises(ProductionWorkbenchError) as old_replay:
        registry.dispatch(original_request)
    assert (old_replay.value.status, old_replay.value.code) == (410, "workspace_gone")

    readopted = registry.dispatch(_create(seed_handle, "request.fresh.readopt")).projection
    assert isinstance(readopted, ProductionWorkbenchProjection)
    assert readopted.workspace_id == source.workspace_id
    assert readopted.workspace_fingerprint == source.fingerprint


def test_registry_retains_exact_sequence_authority_across_selection_only_revision() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, created)

    attached = registry.attach_accepted_authorities(
        created.workspace_handle,
        expected_workspace_revision=created.workspace_revision,
        expected_workspace_fingerprint=created.workspace_fingerprint,
        authorities=ProductionAcceptedAuthorities(generation_sequence=sequence),
    )

    assert attached.run_state == "ready"
    assert attached.run_total == 1
    assert attached.run_completed == 0
    assert attached.generation_sequence is not None
    assert attached.generation_sequence.sequence_id == sequence.state.plan.sequence_id
    assert attached.generation_sequence.sequence_fingerprint == sequence.state.plan.fingerprint
    assert attached.generation_sequence.state_fingerprint == sequence.state.fingerprint
    assert attached.generation_sequence.workspace_id == sequence.state.plan.workspace_id
    assert attached.generation_sequence.correlation == sequence.correlation
    assert attached.authority_versions == ("h3.context.generation_sequence_projection.v1",)
    assert attached.segments[0].closure_state == "dirty_self"
    assert attached.segments[0].job_state == "planned"
    assert "submit_generation_job" in attached.allowed_actions
    reread = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.read.authorities",
            "action": "read_projection",
            "payload": {"workspace_handle": attached.workspace_handle},
        }
    ).projection
    assert reread == attached

    mutated = registry.dispatch(
        _mutation(
            attached,
            request_id="request.select.authorities",
            action="set_selection",
            extra={"segment_ids": []},
        )
    ).projection
    assert isinstance(mutated, ProductionWorkbenchProjection)
    assert mutated.workspace_revision == attached.workspace_revision + 1
    assert mutated.workspace_fingerprint != attached.workspace_fingerprint
    assert mutated.run_state == "ready"
    assert mutated.run_total == 1
    assert mutated.run_completed == 0
    assert mutated.generation_sequence is not None
    assert mutated.generation_sequence.workspace_revision == attached.workspace_revision
    assert mutated.generation_sequence.workspace_fingerprint == attached.workspace_fingerprint
    assert mutated.authority_versions == ("h3.context.generation_sequence_projection.v1",)
    assert "submit_generation_job" in mutated.allowed_actions
    retained = registry._entries[mutated.workspace_handle]
    assert retained.accepted_authorities.generation_sequence is sequence
    assert retained.authority_bytes == (
        len(retained.workspace.to_wire_bytes()) + retained.accepted_authorities.wire_bytes()
    )

    summary = mutated.generation_sequence
    with pytest.raises(
        ProductionWorkbenchProjectionError,
        match="generation_sequence_workspace_mismatch",
    ):
        replace(mutated, generation_sequence=replace(summary, workspace_id="workspace.foreign"))
    with pytest.raises(
        ProductionWorkbenchProjectionError,
        match="generation_sequence_workspace_mismatch",
    ):
        replace(
            mutated,
            generation_sequence=replace(
                summary,
                workspace_revision=mutated.workspace_revision + 1,
            ),
        )
    with pytest.raises(
        ProductionWorkbenchProjectionError,
        match="generation_sequence_workspace_mismatch",
    ):
        replace(
            mutated,
            generation_sequence=replace(
                summary,
                workspace_revision=mutated.workspace_revision,
                workspace_fingerprint=_fp("same.revision.fork"),
            ),
        )

    structural = registry.dispatch(
        _mutation(
            mutated,
            request_id="request.relation.authorities",
            action="set_segment_relation",
            extra={
                "segment_id": mutated.segments[0].segment_id,
                "relation": "cut",
                "predecessor_segment_id": None,
            },
        )
    ).projection
    assert isinstance(structural, ProductionWorkbenchProjection)
    assert structural.run_state == "unavailable"
    assert structural.authority_versions == ()
    assert "submit_generation_job" not in structural.allowed_actions


def test_registry_internal_sequence_publisher_joins_exact_live_workspace() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, created)

    published = registry.publish_generation_sequence_authority(sequence, seed_handle)

    assert published is not None
    assert published.workspace_handle == created.workspace_handle
    assert published.generation_sequence is not None
    assert published.generation_sequence.state_fingerprint == sequence.state.fingerprint
    assert "submit_generation_job" in published.allowed_actions


def test_registry_internal_sequence_publisher_joins_selection_only_successor() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, created)
    selected = registry.dispatch(
        _mutation(
            created,
            request_id="request.select.before-sequence-publish",
            action="set_selection",
            extra={"segment_ids": []},
        )
    ).projection
    assert isinstance(selected, ProductionWorkbenchProjection)

    published = registry.publish_generation_sequence_authority(sequence, seed_handle)

    assert published is not None
    assert published.workspace_revision == selected.workspace_revision
    assert published.workspace_fingerprint == selected.workspace_fingerprint
    assert published.generation_sequence is not None
    assert published.generation_sequence.workspace_revision == created.workspace_revision
    assert published.generation_sequence.workspace_fingerprint == created.workspace_fingerprint
    assert published.run_state == "ready"
    assert "submit_generation_job" in published.allowed_actions

    structural = registry.dispatch(
        _mutation(
            published,
            request_id="request.relation.after-sequence-publish",
            action="set_segment_relation",
            extra={
                "segment_id": published.segments[0].segment_id,
                "relation": "cut",
                "predecessor_segment_id": None,
            },
        )
    ).projection
    assert isinstance(structural, ProductionWorkbenchProjection)
    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None


def test_structural_successor_is_data_derived_and_revision_monotonic() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    source = registry._entries[created.workspace_handle].workspace
    accepted = tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in source.segments
    )
    selection_successor = revise_workspace(
        source,
        expected_workspace_fingerprint=source.fingerprint,
        accepted_intent_authorities=accepted,
        selected_segment_ids=(),
    )
    same_revision_fork = create_workspace(
        source.workspace_id,
        source.segments,
        accepted_intent_authorities=accepted,
        selected_segment_ids=(),
    )
    structural_successor = revise_workspace(
        source,
        expected_workspace_fingerprint=source.fingerprint,
        accepted_intent_authorities=accepted,
        segments=(replace(source.segments[0], relation=SegmentRelationKind.CUT),),
    )

    assert _is_structural_successor(source, source)
    assert _is_structural_successor(selection_successor, source)
    assert not _is_structural_successor(source, selection_successor)
    assert not _is_structural_successor(same_revision_fork, source)
    assert not _is_structural_successor(structural_successor, source)


def test_source_less_historical_sequence_authority_remains_fail_closed() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, created)
    portable_plan = replace(sequence.state.plan, source_workspace_authority=None)
    portable_state = replace(sequence.state, plan=portable_plan)
    portable_sequence = build_generation_sequence_projection(
        portable_state,
        sequence.correlation,
    )
    selected = registry.dispatch(
        _mutation(
            created,
            request_id="request.select.before-portable-attach",
            action="set_selection",
            extra={"segment_ids": []},
        )
    ).projection
    assert isinstance(selected, ProductionWorkbenchProjection)

    with pytest.raises(ProductionWorkbenchError, match="accepted_authority_mismatch"):
        registry.attach_accepted_authorities(
            selected.workspace_handle,
            expected_workspace_revision=selected.workspace_revision,
            expected_workspace_fingerprint=selected.workspace_fingerprint,
            authorities=ProductionAcceptedAuthorities(generation_sequence=portable_sequence),
        )


@pytest.mark.parametrize(
    "action",
    (
        "add_segment_from_context",
        "replace_segment_from_context",
        "set_segment_relation",
        "delete_segment",
        "reorder_segments",
    ),
)
def test_structural_actions_drop_sequence_authority(action: str) -> None:
    sidebar, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    second_report, second_wiring, second_correlation = _authority(
        "A distinct second operator-owned prompt."
    )
    second_seed = sidebar.publish(
        second_report,
        second_wiring,
        second_correlation,
    ).workspace_id
    expanded = registry.dispatch(
        _mutation(
            created,
            request_id=f"request.structural.{action}.expand",
            action="add_segment_from_context",
            extra={
                "context_workspace_handle": second_seed,
                "relation": "independent",
                "predecessor_segment_id": None,
            },
        )
    ).projection
    assert isinstance(expanded, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, expanded)
    attached = registry.attach_accepted_authorities(
        expanded.workspace_handle,
        expected_workspace_revision=expanded.workspace_revision,
        expected_workspace_fingerprint=expanded.workspace_fingerprint,
        authorities=ProductionAcceptedAuthorities(generation_sequence=sequence),
    )
    first_id, second_id = (item.segment_id for item in attached.segments)

    if action in {"add_segment_from_context", "replace_segment_from_context"}:
        third_report, third_wiring, third_correlation = _authority(
            "A distinct third operator-owned prompt."
        )
        third_seed = sidebar.publish(
            third_report,
            third_wiring,
            third_correlation,
        ).workspace_id
    else:
        third_seed = None
    extras: dict[str, dict[str, object]] = {
        "add_segment_from_context": {
            "context_workspace_handle": third_seed,
            "relation": "independent",
            "predecessor_segment_id": None,
        },
        "replace_segment_from_context": {
            "context_workspace_handle": third_seed,
            "segment_id": first_id,
        },
        "set_segment_relation": {
            "segment_id": second_id,
            "relation": "cut",
            "predecessor_segment_id": None,
        },
        "delete_segment": {"segment_id": second_id},
        "reorder_segments": {"segment_ids": [second_id, first_id]},
    }

    mutated = registry.dispatch(
        _mutation(
            attached,
            request_id=f"request.structural.{action}.mutate",
            action=action,
            extra=extras[action],
        )
    ).projection

    assert isinstance(mutated, ProductionWorkbenchProjection)
    assert mutated.run_state == "unavailable"
    assert mutated.generation_sequence is None
    assert mutated.authority_versions == ()
    assert not registry._entries[mutated.workspace_handle].preview_sources


def test_registry_projects_requested_all_and_mixed_cancellation_as_cancelled() -> None:
    sidebar, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    second_report, second_wiring, second_correlation = _authority(
        "A distinct safe operator-owned prompt."
    )
    second_seed_handle = sidebar.publish(
        second_report,
        second_wiring,
        second_correlation,
    ).workspace_id
    expanded = registry.dispatch(
        _mutation(
            created,
            request_id="request.add.cancellation",
            action="add_segment_from_context",
            extra={
                "context_workspace_handle": second_seed_handle,
                "relation": "independent",
                "predecessor_segment_id": None,
            },
        )
    ).projection
    assert isinstance(expanded, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, expanded)
    first = sequence.state.plan.jobs[0]

    projected = record_generation_projection(
        sequence.state,
        first.job_id,
        transaction_id="transaction.production.cancellation",
        graph_fingerprint=first.graph_fingerprint,
        compiled_prompt_fingerprint=first.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    submitted = record_generation_submission(
        projected,
        first.job_id,
        queue_prompt_id="prompt.production.cancellation",
    )
    running = record_generation_running(
        submitted,
        first.job_id,
        host_owner_id="host.production.cancellation",
    )
    requested = build_generation_sequence_projection(
        cancel_generation_sequence(running),
        sequence.correlation,
    )
    all_cancelled = build_generation_sequence_projection(
        cancel_generation_sequence(sequence.state),
        sequence.correlation,
    )

    workspace = registry._entries[expanded.workspace_handle].workspace
    manifest = derive_segment_manifests(workspace)[0]
    transaction = running.runtime_for(first.job_id).transaction
    assert transaction is not None
    partial = begin_segment_artifact_receipt(
        manifest=manifest,
        transaction=transaction,
        artifact_id="artifact.production.cancel.mixed",
        model_fingerprint=first.model_fingerprint,
        runtime_fingerprint=first.runtime_fingerprint,
        execution_fingerprint=_fp("execution.production.cancel.mixed"),
        predecessor_artifact_fingerprint=None,
        format_label=first.expected_format,
        shape=first.expected_shape,
        created_at_ms=100,
        expires_at_ms=10_000,
    )
    receipt = complete_segment_artifact_receipt(
        partial,
        output_fingerprint=_fp("output.production.cancel.mixed"),
        byte_length=8192,
    )
    succeeded = record_generation_success(
        running,
        first.job_id,
        result_fingerprint=_fp("result.production.cancel.mixed"),
        receipt=receipt,
    )
    mixed = build_generation_sequence_projection(
        cancel_generation_sequence(succeeded),
        sequence.correlation,
    )

    for cancelled in (requested, all_cancelled, mixed):
        published = registry.publish_generation_sequence_authority(cancelled, seed_handle)
        assert published is not None
        assert published.run_state == "cancelled"
        assert "submit_generation_job" not in published.allowed_actions


def test_registry_rejects_cross_workspace_sequence_authority_without_mutation() -> None:
    _, registry, seed_handle = _setup()
    first = registry.dispatch(_create(seed_handle, "request.create.first")).projection
    second = registry.dispatch(_create(seed_handle, "request.create.second")).projection
    assert isinstance(first, ProductionWorkbenchProjection)
    assert isinstance(second, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, first)

    with pytest.raises(ProductionWorkbenchError) as rejected:
        registry.attach_accepted_authorities(
            second.workspace_handle,
            expected_workspace_revision=second.workspace_revision,
            expected_workspace_fingerprint=second.workspace_fingerprint,
            authorities=ProductionAcceptedAuthorities(generation_sequence=sequence),
        )
    assert rejected.value.code == "accepted_authority_mismatch"
    assert (
        registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.read.second",
                "action": "read_projection",
                "payload": {"workspace_handle": second.workspace_handle},
            }
        ).projection
        == second
    )


def test_registry_projects_exact_artifact_and_reconstruction_authority() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, created)
    workspace = registry._entries[created.workspace_handle].workspace
    manifest = derive_segment_manifests(workspace)[0]
    artifact = SegmentArtifactReceipt(
        artifact_id="artifact.production.1",
        state=ArtifactLifecycleState.COMPLETE,
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        segment_id=manifest.segment_id,
        manifest_fingerprint=manifest.fingerprint,
        producer_fingerprint=manifest.producer_fingerprint,
        transaction_fingerprint=_fp("transaction.production.1"),
        graph_fingerprint=_fp("graph.production.1"),
        native_binding_fingerprint=manifest.native_binding_fingerprint,
        model_fingerprint=_fp("model.production.1"),
        runtime_fingerprint=_fp("runtime.production.1"),
        settings_fingerprint=manifest.producer_settings_fingerprint,
        source_id=manifest.source_id,
        predecessor_artifact_fingerprint=None,
        execution_fingerprint=_fp("execution.production.1"),
        format_label="video.mp4",
        shape=(30, 512, 512, 3),
        created_at_ms=100,
        expires_at_ms=10_000,
        output_fingerprint=_fp("output.production.1"),
        byte_length=8192,
    )
    reconstruction = AVReconstructionReceipt(
        transaction_id="transaction.production.reconstruction",
        plan_fingerprint=_fp("plan.production"),
        approval_fingerprint=_fp("approval.production"),
        selective_plan_fingerprint=_fp("selective.production"),
        generation_state_fingerprint=sequence.state.fingerprint,
        artifact_receipt_fingerprints=(artifact.fingerprint,),
        boundary_receipt_fingerprints=(),
        capability_fingerprint=_fp("capability.production"),
        execution_fingerprint=_fp("execution.production"),
        segment_results=(
            AVReceiptSegmentResult(
                segment_id=manifest.segment_id,
                artifact_receipt_fingerprint=artifact.fingerprint,
                operations=(AVOperation.NORMALIZE_VIDEO,),
                accounting=AVStreamAccounting(
                    decoded_frames=30,
                    carried_frames=30,
                    dropped_frames=0,
                    generated_frames=0,
                    emitted_frames=30,
                    decoded_samples=48_000,
                    carried_samples=48_000,
                    dropped_samples=0,
                    inserted_samples=0,
                    emitted_samples=48_000,
                ),
                output_start=AVRational(0, 1),
                output_end=AVRational(1, 1),
                derived_output_handle="avout_fedcba9876543210fedcba9876543210",
            ),
        ),
        outputs=(
            AVReceiptOutput(
                handle="avout_0123456789abcdef0123456789abcdef",
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                byte_length=8192,
                content_fingerprint=_fp("reconstruction.output"),
                video_frame_count=30,
                audio_sample_count=48_000,
                duration=AVRational(1, 1),
            ),
            AVReceiptOutput(
                handle="avout_fedcba9876543210fedcba9876543210",
                kind=AVOutputKind.SEGMENT_EXPORT,
                byte_length=4096,
                content_fingerprint=_fp("reconstruction.segment.output"),
                video_frame_count=30,
                audio_sample_count=48_000,
                duration=AVRational(1, 1),
            ),
        ),
        publication_state=AVPublicationState.COMPLETE,
        completed_at_ms=500,
    )
    authorities = ProductionAcceptedAuthorities(
        generation_sequence=sequence,
        artifact_receipts=(artifact,),
        reconstruction_receipt=reconstruction,
    )
    attached = registry.attach_accepted_authorities(
        created.workspace_handle,
        expected_workspace_revision=created.workspace_revision,
        expected_workspace_fingerprint=created.workspace_fingerprint,
        authorities=authorities,
    )

    assert attached.segments[0].artifact_state == "complete"
    assert attached.reconstruction_state == "complete"
    assert tuple(output.state for output in attached.outputs) == ("ready", "ready")
    assert all(output.output_handle.startswith("out_") for output in attached.outputs)
    assert all("avout_" not in output.output_handle for output in attached.outputs)
    assert attached.outputs[0].segment_id is None
    assert attached.outputs[0].preview is False
    assert attached.outputs[1].segment_id == manifest.segment_id
    assert attached.outputs[1].preview is False
    assert attached.outputs[1].to_wire() == {
        "output_handle": attached.outputs[1].output_handle,
        "ordinal": 2,
        "state": "ready",
        "segment_id": manifest.segment_id,
        "preview": False,
    }
    assert registry._entries[created.workspace_handle].authority_bytes > len(
        workspace.to_wire_bytes()
    )

    selected = registry.dispatch(
        _mutation(
            attached,
            request_id="request.select.reconstruction-authorities",
            action="set_selection",
            extra={"segment_ids": []},
        )
    ).projection
    assert isinstance(selected, ProductionWorkbenchProjection)
    assert selected.run_state == attached.run_state
    assert selected.reconstruction_state == "complete"
    assert selected.outputs == attached.outputs
    assert selected.segments[0].artifact_state == "complete"
    assert selected.authority_versions == attached.authority_versions
    retained = registry._entries[selected.workspace_handle]
    assert retained.accepted_authorities is authorities
    assert (
        retained.authority_bytes
        == len(retained.workspace.to_wire_bytes()) + authorities.wire_bytes()
    )

    structural = registry.dispatch(
        _mutation(
            selected,
            request_id="request.relation.reconstruction-authorities",
            action="set_segment_relation",
            extra={
                "segment_id": selected.segments[0].segment_id,
                "relation": "cut",
                "predecessor_segment_id": None,
            },
        )
    ).projection
    assert isinstance(structural, ProductionWorkbenchProjection)
    assert structural.run_state == "unavailable"
    assert structural.reconstruction_state == "unavailable"
    assert structural.outputs == ()
    assert structural.authority_versions == ()


def test_registry_projects_ordered_continuity_authority() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    expanded = registry.dispatch(
        _mutation(
            created,
            request_id="request.add.continuity",
            action="add_segment_from_context",
            extra={
                "context_workspace_handle": seed_handle,
                "relation": "cut",
                "predecessor_segment_id": None,
            },
        )
    ).projection
    assert isinstance(expanded, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, expanded)
    attached = registry.attach_accepted_authorities(
        expanded.workspace_handle,
        expected_workspace_revision=expanded.workspace_revision,
        expected_workspace_fingerprint=expanded.workspace_fingerprint,
        authorities=ProductionAcceptedAuthorities(
            generation_sequence=sequence,
            continuity_receipts=(
                ContinuityBoundaryReceipt(
                    boundary_id="boundary.production.1",
                    mode=ContinuityMode.CUT,
                    audio_not_carried=True,
                ),
            ),
        ),
    )

    assert attached.segments[0].continuity_state == "unavailable"
    assert attached.segments[1].continuity_state == "cut"
    assert "h3.context.continuity_boundary_receipt.v1" in attached.authority_versions


def test_registry_preserves_caller_declared_restart_root() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    expanded = registry.dispatch(
        _mutation(
            created,
            request_id="request.add.restart",
            action="add_segment_from_context",
            extra={
                "context_workspace_handle": seed_handle,
                "relation": "reset",
                "predecessor_segment_id": None,
            },
        )
    ).projection
    assert isinstance(expanded, ProductionWorkbenchProjection)
    sequence = _sequence_for_registry(registry, expanded)

    attached = registry.attach_accepted_authorities(
        expanded.workspace_handle,
        expected_workspace_revision=expanded.workspace_revision,
        expected_workspace_fingerprint=expanded.workspace_fingerprint,
        authorities=ProductionAcceptedAuthorities(
            generation_sequence=sequence,
            continuity_receipts=(
                ContinuityBoundaryReceipt(
                    boundary_id="boundary.production.restart",
                    mode=ContinuityMode.RESTART,
                    audio_not_carried=True,
                    restart_root_id="root.new",
                ),
            ),
        ),
    )
    assert attached.segments[1].continuity_state == "restart"


def test_registry_binds_native_continuity_to_relation_artifact_job_and_graph() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle)).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    predecessor_id = created.segments[0].segment_id
    expanded = registry.dispatch(
        _mutation(
            created,
            request_id="request.add.native",
            action="add_segment_from_context",
            extra={
                "context_workspace_handle": seed_handle,
                "relation": "predecessor",
                "predecessor_segment_id": predecessor_id,
            },
        )
    ).projection
    assert isinstance(expanded, ProductionWorkbenchProjection)
    workspace = registry._entries[expanded.workspace_handle].workspace
    manifests = derive_segment_manifests(workspace)
    sequence = _sequence_for_registry(registry, expanded)
    predecessor = SegmentArtifactReceipt(
        artifact_id="artifact.production.predecessor",
        state=ArtifactLifecycleState.COMPLETE,
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        segment_id=manifests[0].segment_id,
        manifest_fingerprint=manifests[0].fingerprint,
        producer_fingerprint=manifests[0].producer_fingerprint,
        transaction_fingerprint=_fp("transaction.production.predecessor"),
        graph_fingerprint=_fp("graph.production.predecessor"),
        native_binding_fingerprint=manifests[0].native_binding_fingerprint,
        model_fingerprint=_fp("model.production.predecessor"),
        runtime_fingerprint=_fp("runtime.production.predecessor"),
        settings_fingerprint=manifests[0].producer_settings_fingerprint,
        source_id=manifests[0].source_id,
        predecessor_artifact_fingerprint=None,
        execution_fingerprint=_fp("execution.production.predecessor"),
        format_label="video.mp4",
        shape=(30, 512, 512, 3),
        created_at_ms=100,
        expires_at_ms=10_000,
        output_fingerprint=_fp("output.production.predecessor"),
        byte_length=8192,
    )
    successor_job = sequence.state.plan.job_for_segment(manifests[1].segment_id)
    native = ContinuityBoundaryReceipt(
        boundary_id="boundary.production.native",
        mode=ContinuityMode.NATIVE_FRAME_HANDOFF,
        audio_not_carried=True,
        predecessor_segment_id=manifests[0].segment_id,
        predecessor_receipt_fingerprint=predecessor.fingerprint,
        predecessor_output_fingerprint=predecessor.output_fingerprint,
        successor_segment_id=manifests[1].segment_id,
        successor_job_id=successor_job.job_id,
        successor_graph_fingerprint=successor_job.graph_fingerprint,
        first_frame_node_instance_id="node.first.frame",
        first_frame_socket_name="first_frame",
        first_frame_connection_order=1,
        host_version="1.0.0",
        host_revision="0" * 40,
        video_node_source_sha256="1" * 64,
        video_types_source_sha256="2" * 64,
        decoder_name="synthetic_decoder",
        decoder_version="1.0.0",
        decoded_frame_count=30,
        carried_frame_count=1,
        delivered_frame_count=1,
        tail_content_sha256="3" * 64,
    )

    invalid = (
        ContinuityBoundaryReceipt(
            boundary_id="boundary.production.cut",
            mode=ContinuityMode.CUT,
            audio_not_carried=True,
        ),
        ContinuityBoundaryReceipt(
            boundary_id="boundary.production.restart",
            mode=ContinuityMode.RESTART,
            audio_not_carried=True,
            restart_root_id=manifests[1].segment_id,
        ),
        replace(
            native,
            predecessor_receipt_fingerprint=_fp("foreign.receipt"),
            receipt_fingerprint=None,
        ),
        replace(native, successor_job_id="job.foreign", receipt_fingerprint=None),
        replace(
            native,
            successor_graph_fingerprint=_fp("foreign.graph"),
            receipt_fingerprint=None,
        ),
    )
    for receipt in invalid:
        with pytest.raises(ProductionWorkbenchError, match="accepted_authority_mismatch"):
            registry.attach_accepted_authorities(
                expanded.workspace_handle,
                expected_workspace_revision=expanded.workspace_revision,
                expected_workspace_fingerprint=expanded.workspace_fingerprint,
                authorities=ProductionAcceptedAuthorities(
                    generation_sequence=sequence,
                    artifact_receipts=(predecessor,),
                    continuity_receipts=(receipt,),
                ),
            )

    attached = registry.attach_accepted_authorities(
        expanded.workspace_handle,
        expected_workspace_revision=expanded.workspace_revision,
        expected_workspace_fingerprint=expanded.workspace_fingerprint,
        authorities=ProductionAcceptedAuthorities(
            generation_sequence=sequence,
            artifact_receipts=(predecessor,),
            continuity_receipts=(native,),
        ),
    )
    assert attached.segments[1].continuity_state == "native_frame_handoff"


def test_registry_binds_native_continuity_to_declared_nonadjacent_predecessor() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle, "request.native.declared.create")).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    first_id = created.segments[0].segment_id
    second = registry.dispatch(
        _mutation(
            created,
            request_id="request.native.declared.second",
            action="add_segment_from_context",
            extra={
                "context_workspace_handle": seed_handle,
                "relation": "independent",
                "predecessor_segment_id": None,
            },
        )
    ).projection
    assert isinstance(second, ProductionWorkbenchProjection)
    third = registry.dispatch(
        _mutation(
            second,
            request_id="request.native.declared.third",
            action="add_segment_from_context",
            extra={
                "context_workspace_handle": seed_handle,
                "relation": "predecessor",
                "predecessor_segment_id": first_id,
            },
        )
    ).projection
    assert isinstance(third, ProductionWorkbenchProjection)
    workspace = registry._entries[third.workspace_handle].workspace
    manifests = derive_segment_manifests(workspace)
    sequence = _sequence_for_registry(registry, third)
    predecessor = _artifact_for_manifest(manifests[0], label="native.declared.first")
    successor_job = sequence.state.plan.job_for_segment(manifests[2].segment_id)
    native = ContinuityBoundaryReceipt(
        boundary_id="boundary.production.native.declared",
        mode=ContinuityMode.NATIVE_FRAME_HANDOFF,
        audio_not_carried=True,
        predecessor_segment_id=manifests[0].segment_id,
        predecessor_receipt_fingerprint=predecessor.fingerprint,
        predecessor_output_fingerprint=predecessor.output_fingerprint,
        successor_segment_id=manifests[2].segment_id,
        successor_job_id=successor_job.job_id,
        successor_graph_fingerprint=successor_job.graph_fingerprint,
        first_frame_node_instance_id="node.first.frame",
        first_frame_socket_name="first_frame",
        first_frame_connection_order=1,
        host_version="1.0.0",
        host_revision="0" * 40,
        video_node_source_sha256="1" * 64,
        video_types_source_sha256="2" * 64,
        decoder_name="synthetic_decoder",
        decoder_version="1.0.0",
        decoded_frame_count=30,
        carried_frame_count=1,
        delivered_frame_count=1,
        tail_content_sha256="3" * 64,
    )
    boundaries = (
        ContinuityBoundaryReceipt(
            boundary_id="boundary.production.native.declared.cut",
            mode=ContinuityMode.CUT,
            audio_not_carried=True,
        ),
        native,
    )

    attached = registry.attach_accepted_authorities(
        third.workspace_handle,
        expected_workspace_revision=third.workspace_revision,
        expected_workspace_fingerprint=third.workspace_fingerprint,
        authorities=ProductionAcceptedAuthorities(
            generation_sequence=sequence,
            artifact_receipts=(predecessor,),
            continuity_receipts=boundaries,
        ),
    )
    assert attached.segments[2].continuity_state == "native_frame_handoff"

    adjacent = _artifact_for_manifest(manifests[1], label="native.declared.adjacent")
    with pytest.raises(ProductionWorkbenchError, match="accepted_authority_mismatch"):
        registry.attach_accepted_authorities(
            third.workspace_handle,
            expected_workspace_revision=third.workspace_revision,
            expected_workspace_fingerprint=third.workspace_fingerprint,
            authorities=ProductionAcceptedAuthorities(
                generation_sequence=sequence,
                artifact_receipts=(adjacent,),
                continuity_receipts=boundaries,
            ),
        )


def test_sidebar_seed_claim_is_reusable_typed_and_content_free() -> None:
    sidebar, _, handle = _setup()
    first = sidebar.claim_production_seed(handle)
    second = sidebar.claim_production_seed(handle)

    assert first == second
    wire = json.dumps(asdict(first), sort_keys=True, default=str).lower()
    assert "operator-owned prompt" not in wire
    assert "prompt" not in wire
    assert "http://" not in wire
    assert "\\" not in wire
    assert first.duration.frame_count == 124


def test_create_read_replay_stale_selection_and_release_lifecycle() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle))
    assert created.status == 201
    assert created.projection is not None
    initial = created.projection
    assert isinstance(initial, ProductionWorkbenchProjection)
    assert initial.schema == "h3.context.production_workbench.projection.v1"
    assert initial.workspace_revision == 1
    assert len(initial.segments) == 1
    assert initial.selected_segment_ids == (initial.segments[0].segment_id,)
    assert "set_selection" in initial.allowed_actions
    assert "preview_output" not in initial.allowed_actions

    retry = registry.dispatch(_create(seed_handle))
    assert retry.status == 200
    assert retry.projection == initial

    read = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.read",
            "action": "read_projection",
            "payload": {"workspace_handle": initial.workspace_handle},
        }
    )
    assert read.status == 200
    assert read.projection == initial

    selected = registry.dispatch(
        _mutation(
            initial,
            request_id="request.select",
            action="set_selection",
            extra={"segment_ids": []},
        )
    )
    assert selected.status == 200
    assert selected.projection is not None
    selected_projection = selected.projection
    assert isinstance(selected_projection, ProductionWorkbenchProjection)
    assert selected_projection.workspace_revision == 2
    assert selected_projection.selected_segment_ids == ()

    exact_retry = registry.dispatch(
        _mutation(
            initial,
            request_id="request.select",
            action="set_selection",
            extra={"segment_ids": []},
        )
    )
    assert exact_retry.status == 200
    assert exact_retry.projection == selected.projection

    successor = registry.dispatch(
        _mutation(
            selected_projection,
            request_id="request.select-successor",
            action="set_selection",
            extra={"segment_ids": [initial.segments[0].segment_id]},
        )
    )
    assert successor.projection is not None
    successor_projection = successor.projection
    assert isinstance(successor_projection, ProductionWorkbenchProjection)

    superseded = registry.dispatch(
        _mutation(
            initial,
            request_id="request.select",
            action="set_selection",
            extra={"segment_ids": []},
        )
    )
    assert superseded.status == 409
    assert superseded.code == "replay_superseded"
    assert superseded.projection == successor.projection

    with pytest.raises(ProductionWorkbenchError) as changed:
        registry.dispatch(
            _mutation(
                initial,
                request_id="request.select",
                action="set_selection",
                extra={"segment_ids": [initial.segments[0].segment_id]},
            )
        )
    assert changed.value.status == 409
    assert changed.value.projection is None

    release = registry.dispatch(
        _mutation(
            successor_projection,
            request_id="request.release",
            action="release_workspace",
            extra={},
        )
    )
    assert release.status == 204
    assert release.projection is None
    assert (
        registry.dispatch(
            _mutation(
                successor_projection,
                request_id="request.release",
                action="release_workspace",
                extra={},
            )
        ).status
        == 204
    )
    changed_release = _mutation(
        successor_projection,
        request_id="request.release",
        action="release_workspace",
        extra={},
    )
    changed_payload = changed_release["payload"]
    assert type(changed_payload) is dict
    changed_payload["expected_workspace_revision"] = 1
    with pytest.raises(ProductionWorkbenchError) as release_conflict:
        registry.dispatch(changed_release)
    assert release_conflict.value.status == 409
    assert release_conflict.value.projection is None
    with pytest.raises(ProductionWorkbenchError) as other_release:
        registry.dispatch(
            _mutation(
                successor_projection,
                request_id="request.release.other",
                action="release_workspace",
                extra={},
            )
        )
    assert other_release.value.status == 410

    with pytest.raises(ProductionWorkbenchError) as gone:
        registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.after-release",
                "action": "read_projection",
                "payload": {"workspace_handle": initial.workspace_handle},
            }
        )
    assert gone.value.status == 410


def test_complete_structural_action_matrix_preserves_dependency_rules() -> None:
    sidebar, registry, first_seed = _setup()
    report, wiring, correlation = _authority("A second safe operator-owned prompt.")
    second_seed = sidebar.publish(report, wiring, correlation).workspace_id
    current = registry.dispatch(_create(first_seed)).projection
    assert isinstance(current, ProductionWorkbenchProjection)

    added = registry.dispatch(
        _mutation(
            current,
            request_id="request.add",
            action="add_segment_from_context",
            extra={
                "context_workspace_handle": second_seed,
                "relation": "predecessor",
                "predecessor_segment_id": current.segments[0].segment_id,
            },
        )
    ).projection
    assert isinstance(added, ProductionWorkbenchProjection)
    assert len(added.segments) == 2
    first_id, second_id = (segment.segment_id for segment in added.segments)

    with pytest.raises(ProductionWorkbenchError) as invalid_reorder:
        registry.dispatch(
            _mutation(
                added,
                request_id="request.bad-order",
                action="reorder_segments",
                extra={"segment_ids": [second_id, first_id]},
            )
        )
    assert invalid_reorder.value.status == 422

    independent = registry.dispatch(
        _mutation(
            added,
            request_id="request.relation",
            action="set_segment_relation",
            extra={
                "segment_id": second_id,
                "relation": "independent",
                "predecessor_segment_id": None,
            },
        )
    ).projection
    assert isinstance(independent, ProductionWorkbenchProjection)
    reordered = registry.dispatch(
        _mutation(
            independent,
            request_id="request.order",
            action="reorder_segments",
            extra={"segment_ids": [second_id, first_id]},
        )
    ).projection
    assert isinstance(reordered, ProductionWorkbenchProjection)
    assert [item.segment_id for item in reordered.segments] == [second_id, first_id]

    replaced = registry.dispatch(
        _mutation(
            reordered,
            request_id="request.replace",
            action="replace_segment_from_context",
            extra={"segment_id": second_id, "context_workspace_handle": first_seed},
        )
    ).projection
    assert isinstance(replaced, ProductionWorkbenchProjection)
    assert replaced.segments[0].segment_id == second_id

    selected = registry.dispatch(
        _mutation(
            replaced,
            request_id="request.select-two",
            action="set_selection",
            extra={"segment_ids": [second_id, first_id]},
        )
    ).projection
    assert isinstance(selected, ProductionWorkbenchProjection)
    deleted = registry.dispatch(
        _mutation(
            selected,
            request_id="request.delete",
            action="delete_segment",
            extra={"segment_id": first_id},
        )
    ).projection
    assert isinstance(deleted, ProductionWorkbenchProjection)
    assert [item.segment_id for item in deleted.segments] == [second_id]
    assert deleted.selected_segment_ids == (second_id,)


def test_decoder_rejects_duplicates_unknown_fields_float_and_bounds() -> None:
    valid = _create("ws_0123456789abcdefghijklmnopqrstuv")
    encoded = json.dumps(valid).encode("utf-8")
    assert decode_production_action_json(encoded) == valid

    duplicate = encoded.replace(
        b'"schema": "h3.context.production_workbench.action.v1",',
        b'"schema": "h3.context.production_workbench.action.v1", "schema": "x",',
    )
    hostile = [
        duplicate,
        b"not-json",
        b'"scalar"',
        b'{"schema": NaN}',
        b"{}" + b" " * MAX_PRODUCTION_ACTION_BYTES,
    ]
    for value in hostile:
        with pytest.raises(ValueError):
            decode_production_action_json(value)

    with pytest.raises(ValueError):
        decode_production_action_json(bytearray(encoded))  # type: ignore[arg-type]

    unknown = {**valid, "unexpected": True}
    with pytest.raises(ValueError):
        decode_production_action_json(json.dumps(unknown).encode("utf-8"))


def test_exact_seeded_retries_do_not_depend_on_a_still_live_sidebar_seed() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    report, wiring, correlation = _authority()
    first_seed = sidebar.publish(report, wiring, correlation).workspace_id
    report, wiring, correlation = _authority("A second safe operator-owned prompt.")
    second_seed = sidebar.publish(report, wiring, correlation).workspace_id
    source_available = True

    def claim(handle: str):  # type: ignore[no-untyped-def]
        if not source_available:
            raise KeyError(handle)
        return sidebar.claim_production_seed(handle)

    registry = ProductionWorkspaceRegistry(seed_claim=claim)
    create_action = _create(first_seed)
    created = registry.dispatch(create_action)
    assert created.projection is not None
    created_projection = created.projection
    assert isinstance(created_projection, ProductionWorkbenchProjection)
    add_action = _mutation(
        created_projection,
        request_id="request.add.retry",
        action="add_segment_from_context",
        extra={
            "context_workspace_handle": second_seed,
            "relation": "independent",
            "predecessor_segment_id": None,
        },
    )
    added = registry.dispatch(add_action)
    assert added.projection is not None

    source_available = False
    create_retry = registry.dispatch(create_action)
    assert create_retry.status == 409
    assert create_retry.code == "replay_superseded"
    assert create_retry.projection == added.projection
    assert registry.dispatch(add_action).projection == added.projection

    with pytest.raises(ProductionWorkbenchError) as unavailable:
        registry.dispatch(_create(first_seed, request_id="request.new.seed-required"))
    assert unavailable.value.status == 404


def test_sliding_ttl_terminal_window_and_immediate_capacity_reclamation() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=2, ttl_seconds=60)
    report, wiring, correlation = _authority()
    seed = sidebar.publish(report, wiring, correlation).workspace_id
    clock = _Clock()
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=1,
        ttl_seconds=10,
        terminal_ttl_seconds=10,
        clock=clock,
    )
    first = registry.dispatch(_create(seed, "request.first")).projection
    assert isinstance(first, ProductionWorkbenchProjection)

    with pytest.raises(ProductionWorkbenchError) as full:
        registry.dispatch(_create(seed, "request.full"))
    assert full.value.status == 429

    clock.advance(9)
    read_action = {
        "schema": PRODUCTION_ACTION_SCHEMA,
        "request_id": "request.read.ttl",
        "action": "read_projection",
        "payload": {"workspace_handle": first.workspace_handle},
    }
    assert registry.dispatch(read_action).status == 200
    clock.advance(9)
    assert registry.dispatch(read_action).status == 200
    clock.advance(11)
    with pytest.raises(ProductionWorkbenchError) as expired:
        registry.dispatch(read_action)
    assert expired.value.status == 410

    second = registry.dispatch(_create(seed, "request.second")).projection
    assert isinstance(second, ProductionWorkbenchProjection)
    release_action = _mutation(
        second,
        request_id="request.release.second",
        action="release_workspace",
        extra={},
    )
    assert registry.dispatch(release_action).status == 204
    assert registry.dispatch(release_action).status == 204
    third = registry.dispatch(_create(seed, "request.third")).projection
    assert isinstance(third, ProductionWorkbenchProjection)

    clock.advance(10)
    with pytest.raises(ProductionWorkbenchError) as forgotten:
        registry.dispatch(read_action)
    assert forgotten.value.status == 404


def test_ledger_capacity_never_evicts_live_replay_and_still_allows_read_release() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=2, ttl_seconds=60)
    report, wiring, correlation = _authority()
    seed = sidebar.publish(report, wiring, correlation).workspace_id
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_ledger_entries=1,
    )
    create_action = _create(seed)
    created = registry.dispatch(create_action).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    mutation = _mutation(
        created,
        request_id="request.ledger.full",
        action="set_selection",
        extra={"segment_ids": []},
    )
    with pytest.raises(ProductionWorkbenchError) as full:
        registry.dispatch(mutation)
    assert full.value.status == 429
    assert registry.dispatch(create_action).projection == created
    assert (
        registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.read.allowed",
                "action": "read_projection",
                "payload": {"workspace_handle": created.workspace_handle},
            }
        ).projection
        == created
    )
    assert (
        registry.dispatch(
            _mutation(
                created,
                request_id="request.release.allowed",
                action="release_workspace",
                extra={},
            )
        ).status
        == 204
    )


def test_tombstone_count_bound_evicts_only_oldest_terminal_authority() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=2, ttl_seconds=60)
    report, wiring, correlation = _authority()
    seed = sidebar.publish(report, wiring, correlation).workspace_id
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=1,
        max_tombstones=1,
    )
    first = registry.dispatch(_create(seed, "request.tombstone.first")).projection
    assert isinstance(first, ProductionWorkbenchProjection)
    assert (
        registry.dispatch(
            _mutation(
                first,
                request_id="request.tombstone.release-first",
                action="release_workspace",
                extra={},
            )
        ).status
        == 204
    )
    second = registry.dispatch(_create(seed, "request.tombstone.second")).projection
    assert isinstance(second, ProductionWorkbenchProjection)
    assert (
        registry.dispatch(
            _mutation(
                second,
                request_id="request.tombstone.release-second",
                action="release_workspace",
                extra={},
            )
        ).status
        == 204
    )

    for handle, expected_status in (
        (first.workspace_handle, 404),
        (second.workspace_handle, 410),
    ):
        with pytest.raises(ProductionWorkbenchError) as terminal:
            registry.dispatch(
                {
                    "schema": PRODUCTION_ACTION_SCHEMA,
                    "request_id": f"request.read.{expected_status}",
                    "action": "read_projection",
                    "payload": {"workspace_handle": handle},
                }
            )
        assert terminal.value.status == expected_status


def test_request_byte_and_array_boundaries_are_exact() -> None:
    valid = json.dumps(_create("ws_0123456789abcdefghijklmnopqrstuv")).encode("utf-8")
    padded = valid + b" " * (MAX_PRODUCTION_ACTION_BYTES - len(valid))
    assert decode_production_action_json(padded)["action"] == "create_workspace_from_context"
    with pytest.raises(ValueError):
        decode_production_action_json(padded + b" ")

    base_payload = {
        "workspace_handle": "pw_0123456789abcdefghijklmnopqrstuv",  # pragma: allowlist secret
        "expected_workspace_revision": 1,
        "expected_workspace_fingerprint": "sha256:" + "a" * 64,
    }
    for count, accepted in ((64, True), (65, False)):
        action = {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": f"request.array.{count}",
            "action": "reorder_segments",
            "payload": {
                **base_payload,
                "segment_ids": [f"segment.{index}" for index in range(count)],
            },
        }
        encoded = json.dumps(action).encode("utf-8")
        if accepted:
            assert decode_production_action_json(encoded) == action
        else:
            with pytest.raises(ValueError):
                decode_production_action_json(encoded)


def test_segment_and_output_boundaries_remain_content_free() -> None:
    sidebar, _, seed_handle = _setup()
    seed = sidebar.claim_production_seed(seed_handle)
    segments = tuple(
        SegmentDeclaration(
            segment_id=f"segment.{index}",
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
        for index in range(64)
    )
    workspace = create_workspace(
        "workspace.boundary",
        segments,
        accepted_intent_authorities=tuple(
            AcceptedIntentAuthority(
                segment.segment_id,
                segment.accepted_intent_fingerprint,
            )
            for segment in segments
        ),
        selected_segment_ids=tuple(segment.segment_id for segment in segments),
    )
    outputs = tuple(
        ProductionOutputProjection(
            output_handle=f"out_{index:016d}",
            ordinal=index + 1,
            state="ready",
            segment_id=None if index == 0 else segments[index - 1].segment_id,
        )
        for index in range(65)
    )
    projection = build_production_workbench_projection(
        workspace,
        workspace_handle="pw_" + "p" * 43,
        reconstruction_state="complete",
        outputs=outputs,
    )
    assert len(projection.segments) == 64
    assert len(projection.outputs) == 65
    assert "add_segment_from_context" not in projection.allowed_actions
    public = json.dumps(projection.to_wire(), sort_keys=True).lower()
    for forbidden in ("prompt", "provider", "http://", "https://", "\\"):
        assert forbidden not in public

    with pytest.raises(ProductionWorkbenchProjectionError):
        replace(
            projection,
            outputs=outputs
            + (
                ProductionOutputProjection(
                    output_handle="out_" + "z" * 16,
                    ordinal=65,
                    state="ready",
                ),
            ),
        )


def test_output_rows_bind_preview_capability_to_one_known_segment() -> None:
    _, registry, seed_handle = _setup()
    created = registry.dispatch(_create(seed_handle, "request.output.contract")).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    workspace = registry._entries[created.workspace_handle].workspace
    segment_id = workspace.segments[0].segment_id
    aggregate = ProductionOutputProjection(
        output_handle="out_" + "a" * 24,
        ordinal=1,
        state="ready",
        segment_id=None,
        preview=False,
    )
    segment = ProductionOutputProjection(
        output_handle="out_" + "b" * 24,
        ordinal=2,
        state="ready",
        segment_id=segment_id,
        preview=True,
    )

    projection = build_production_workbench_projection(
        workspace,
        workspace_handle=created.workspace_handle,
        reconstruction_state="complete",
        outputs=(aggregate, segment),
        preview_available=True,
    )

    assert projection.outputs[0].to_wire()["segment_id"] is None
    assert projection.outputs[1].to_wire()["segment_id"] == segment_id
    assert projection.outputs[1].to_wire()["preview"] is True
    assert "preview_output" in projection.allowed_actions
    public = json.dumps(projection.to_wire(), sort_keys=True).lower()
    for forbidden in ("locator", "private", "http://", "https://", "\\"):
        assert forbidden not in public

    with pytest.raises(ProductionWorkbenchProjectionError, match="preview"):
        replace(segment, state="pending")
    with pytest.raises(ProductionWorkbenchProjectionError, match="segment"):
        build_production_workbench_projection(
            workspace,
            workspace_handle=created.workspace_handle,
            reconstruction_state="complete",
            outputs=(aggregate, replace(segment, segment_id="segment.unknown")),
            preview_available=True,
        )
    with pytest.raises(ProductionWorkbenchProjectionError, match="preview"):
        build_production_workbench_projection(
            workspace,
            workspace_handle=created.workspace_handle,
            reconstruction_state="complete",
            outputs=(aggregate, segment),
            preview_available=False,
        )
    with pytest.raises(ProductionWorkbenchProjectionError, match="aggregate"):
        build_production_workbench_projection(
            workspace,
            workspace_handle=created.workspace_handle,
            reconstruction_state="complete",
            outputs=(replace(aggregate, segment_id=segment_id), segment),
            preview_available=True,
        )


def test_automatic_assembly_retains_original_import_capability() -> None:
    from comfyui_h3_context.core.production_workbench import ProductionAssemblyProjection

    _, registry, seed = _setup()
    created = registry.dispatch(_create(seed, "request.assembled.original")).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    workspace = registry._entries[created.workspace_handle].workspace
    original = ProductionOutputProjection(
        output_handle="out_" + "o" * 24,
        ordinal=1,
        state="ready",
        segment_id=workspace.segments[0].segment_id,
    )
    aggregate = ProductionOutputProjection(
        output_handle="out_" + "a" * 24, ordinal=2, state="ready"
    )
    assembly = ProductionAssemblyProjection(
        state="succeeded",
        completed=1,
        total=1,
        capability_fingerprint="sha256:" + "b" * 64,
        managed_sequence_fingerprint="sha256:" + "c" * 64,
        artifact_receipt_fingerprints=("sha256:" + "d" * 64,),
        assembly_job_id="assembly.original",
        authorization_fingerprint="sha256:" + "e" * 64,
        receipt_fingerprint="sha256:" + "f" * 64,
        failure_code=None,
    )
    projection = build_production_workbench_projection(
        workspace,
        workspace_handle=created.workspace_handle,
        reconstruction_state="complete",
        outputs=(original, aggregate),
        authority_versions=("h3.context.m26.production_assembly_receipt.v1",),
        assembly=assembly,
        authoring_import_available=True,
    )
    assert "import_production_outputs_to_authoring" in projection.allowed_actions
    assert projection.outputs[0] is original
    assert projection.outputs[1].segment_id is None


@pytest.mark.parametrize("invalid_subject", ["legacy", "aggregate_only", "missing_version"])
def test_projection_rejects_import_without_current_automatic_originals(
    invalid_subject: str,
) -> None:
    from comfyui_h3_context.core.production_workbench import ProductionAssemblyProjection

    _, registry, seed = _setup()
    created = registry.dispatch(_create(seed, "request.import.guard")).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    original = ProductionOutputProjection(
        output_handle="out_" + "o" * 24,
        ordinal=1,
        state="ready",
        segment_id=created.segments[0].segment_id,
    )
    aggregate = ProductionOutputProjection(
        output_handle="out_" + "a" * 24, ordinal=2, state="ready"
    )
    assembly = ProductionAssemblyProjection(
        state="succeeded",
        completed=1,
        total=1,
        capability_fingerprint="sha256:" + "b" * 64,
        managed_sequence_fingerprint="sha256:" + "c" * 64,
        artifact_receipt_fingerprints=("sha256:" + "d" * 64,),
        assembly_job_id="assembly.original",
        authorization_fingerprint="sha256:" + "e" * 64,
        receipt_fingerprint="sha256:" + "f" * 64,
        failure_code=None,
    )
    with pytest.raises(ProductionWorkbenchProjectionError, match="authoring_import"):
        replace(
            created,
            reconstruction_state="complete",
            outputs=(replace(aggregate, ordinal=1),)
            if invalid_subject == "aggregate_only"
            else (original, aggregate),
            assembly=ProductionAssemblyProjection() if invalid_subject == "legacy" else assembly,
            authority_versions=()
            if invalid_subject == "missing_version"
            else ("h3.context.m26.production_assembly_receipt.v1",),
            allowed_actions=created.allowed_actions + ("import_production_outputs_to_authoring",),
        )


def test_live_entry_capacity_boundary_plus_one_does_not_evict() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=1, ttl_seconds=60)
    report, wiring, correlation = _authority()
    seed = sidebar.publish(report, wiring, correlation).workspace_id
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=16,
    )
    projections = [
        registry.dispatch(_create(seed, f"request.capacity.{index}")).projection
        for index in range(16)
    ]
    assert all(projection is not None for projection in projections)
    with pytest.raises(ProductionWorkbenchError) as full:
        registry.dispatch(_create(seed, "request.capacity.overflow"))
    assert full.value.status == 429
    first = projections[0]
    assert isinstance(first, ProductionWorkbenchProjection)
    read = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.capacity.read",
            "action": "read_projection",
            "payload": {"workspace_handle": first.workspace_handle},
        }
    )
    assert read.projection == first


def test_stale_cas_returns_current_projection_without_publishing() -> None:
    _, registry, seed = _setup()
    initial = registry.dispatch(_create(seed)).projection
    assert isinstance(initial, ProductionWorkbenchProjection)
    current = registry.dispatch(
        _mutation(
            initial,
            request_id="request.current",
            action="set_selection",
            extra={"segment_ids": []},
        )
    ).projection
    assert isinstance(current, ProductionWorkbenchProjection)
    with pytest.raises(ProductionWorkbenchError) as stale:
        registry.dispatch(
            _mutation(
                initial,
                request_id="request.stale.new-id",
                action="set_selection",
                extra={"segment_ids": [initial.segments[0].segment_id]},
            )
        )
    assert stale.value.status == 409
    assert stale.value.projection == current
    read = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.stale.read",
            "action": "read_projection",
            "payload": {"workspace_handle": initial.workspace_handle},
        }
    )
    assert read.projection == current


def test_release_and_mutation_race_has_one_deterministic_winner() -> None:
    _, registry, seed = _setup()
    initial = registry.dispatch(_create(seed)).projection
    assert isinstance(initial, ProductionWorkbenchProjection)
    release = _mutation(
        initial,
        request_id="request.race.release",
        action="release_workspace",
        extra={},
    )
    mutation = _mutation(
        initial,
        request_id="request.race.mutation",
        action="set_selection",
        extra={"segment_ids": []},
    )
    barrier = threading.Barrier(2)

    def dispatch(action: dict[str, object]) -> tuple[str, int]:
        barrier.wait()
        try:
            return "result", registry.dispatch(action).status
        except ProductionWorkbenchError as exc:
            return "error", exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(dispatch, action) for action in (release, mutation)]
        outcomes = {future.result() for future in futures}
    assert outcomes in (
        {("result", 204), ("error", 410)},
        {("result", 200), ("error", 409)},
    )
