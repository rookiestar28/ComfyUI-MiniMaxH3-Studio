"""Focused current-admission and retained-execution version joins for M26-02."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_production_import import (
    _authorities,
    _legacy_context,
    _legacy_proposal,
    _m26_execute,
    _m26_ready_sequence,
    _m26_runtime,
    _m26_subject,
    _M26Clock,
    _Materializer,
    _production,
    _request,
)

from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkbenchError
from comfyui_h3_context.adapters.managed_sequence_service import (
    ManagedSequenceServiceError,
    build_managed_sequence_authorization,
)
from comfyui_h3_context.adapters.segment_artifact_store import PrivateSegmentArtifactStore
from comfyui_h3_context.core.production_duration import SegmentationPolicyV1
from comfyui_h3_context.core.production_import import (
    ProductionAutomaticPlanAuthorityV1,
    ProductionAutomaticPlanAuthorityV2,
    derive_legacy_segment_context_materialization_receipt,
)
from comfyui_h3_context.core.production_storyboard import (
    ProductionPlanningContextV1,
    ProductionPlanningContextV2,
    SegmentationProposalV1,
)
from comfyui_h3_context.core.segment_workspace import derive_segment_manifests


def _retained_legacy_authority(
    context: ProductionPlanningContextV2,
    current: ProductionAutomaticPlanAuthorityV2,
) -> tuple[
    ProductionPlanningContextV1,
    SegmentationProposalV1,
    ProductionAutomaticPlanAuthorityV1,
]:
    legacy_context = _legacy_context(context)
    legacy_proposal = _legacy_proposal(legacy_context, current.proposal)
    materializer = _Materializer(context)
    legacy_receipts = []
    for segment in legacy_proposal.segments:
        claim = materializer(
            legacy_context,  # type: ignore[arg-type]
            legacy_proposal,  # type: ignore[arg-type]
            segment,
        )
        try:
            legacy_receipts.append(
                derive_legacy_segment_context_materialization_receipt(
                    legacy_context,
                    legacy_proposal,
                    segment,
                    claim,
                )
            )
        finally:
            claim.release()
    receipts = tuple(legacy_receipts)
    declarations = tuple(
        replace(
            declaration,
            source_id=receipt.segment_context_authority_id,
            accepted_intent_fingerprint=receipt.intent_graph_fingerprint,
            profile_fingerprint=receipt.profile_fingerprint,
            reference_registry_fingerprint=receipt.reference_registry_fingerprint,
            native_binding_fingerprint=receipt.native_binding_fingerprint,
            producer_settings_fingerprint=receipt.producer_settings_fingerprint,
        )
        for declaration, receipt in zip(current.workspace.segments, receipts, strict=True)
    )
    workspace = replace(
        current.workspace,
        segments=declarations,
        workspace_fingerprint=None,
    )
    legacy = ProductionAutomaticPlanAuthorityV1(
        workspace=workspace,
        proposal=legacy_proposal,
        planning_context_fingerprint=legacy_context.fingerprint,
        source_request_fingerprint=legacy_context.source_request_fingerprint,
        source_profile_fingerprint=legacy_context.source_profile_fingerprint,
        materialization_receipts=receipts,
        cut_boundary_receipts=current.cut_boundary_receipts,
        manifest_fingerprints=tuple(row.fingerprint for row in derive_segment_manifests(workspace)),
        reconstruction_order=current.reconstruction_order,
        start_hold_codes=legacy_proposal.start_hold_codes,
    )
    return legacy_context, legacy_proposal, legacy


def test_new_import_refuses_legacy_semantics_before_materialization() -> None:
    context, _admission, proposal = _authorities()
    legacy_context = _legacy_context(context)
    legacy_proposal = _legacy_proposal(legacy_context, proposal)
    _sidebar, registry, created = _production(context)
    materializer = _Materializer(context)

    with pytest.raises(ProductionWorkbenchError) as raised:
        registry.import_automatic_plan(
            _request(created, context, proposal),
            planning_context=legacy_context,
            proposal=legacy_proposal,
            materialize=materializer,
        )
    assert raised.value.code == "automatic_plan_semantic_authority_stale"
    assert raised.value.status == 409
    assert materializer.calls == 0


def test_committed_request_replays_before_supplied_legacy_authority_gate() -> None:
    context, _admission, proposal = _authorities()
    legacy_context = _legacy_context(context)
    legacy_proposal = _legacy_proposal(legacy_context, proposal)
    _sidebar, registry, created = _production(context)
    request = _request(created, context, proposal)
    materializer = _Materializer(context)
    first = registry.import_automatic_plan(
        request,
        planning_context=context,
        proposal=proposal,
        materialize=materializer,
    )
    calls = materializer.calls

    replay = registry.import_automatic_plan(
        request,
        planning_context=legacy_context,
        proposal=legacy_proposal,
        materialize=materializer,
    )
    assert replay.replayed
    assert replay.projection == first.projection
    assert materializer.calls == calls


def test_new_managed_parent_refuses_retained_v1_automatic_authority() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    imported = registry.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=_Materializer(context),
    )
    current = registry.claim_automatic_plan_authority(
        created.workspace_handle,
        expected_workspace_revision=imported.projection.workspace_revision,
        expected_workspace_fingerprint=imported.projection.workspace_fingerprint,
        expected_plan_fingerprint=imported.projection.plan_fingerprint,
    )
    assert type(current) is ProductionAutomaticPlanAuthorityV2
    _legacy_context_value, _legacy_proposal_value, retained = _retained_legacy_authority(
        context,
        current,
    )

    with pytest.raises(ManagedSequenceServiceError) as raised:
        build_managed_sequence_authorization(
            retained,  # type: ignore[arg-type]
            sequence_id="parent.version.join",
            generation_plan_fingerprint="sha256:" + "1" * 64,
            compiler_fingerprint="sha256:" + "2" * 64,
            host_capability_fingerprint="sha256:" + "3" * 64,
        )
    assert raised.value.code == "automatic_plan_semantic_authority_stale"
    assert raised.value.status == 409


def test_retained_v1_authority_rematerializes_through_legacy_receipt_contract() -> None:
    context, _admission, proposal = _authorities()
    _sidebar, registry, created = _production(context)
    materializer = _Materializer(context)
    imported = registry.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=materializer,
    )
    current = registry.claim_automatic_plan_authority(
        created.workspace_handle,
        expected_workspace_revision=imported.projection.workspace_revision,
        expected_workspace_fingerprint=imported.projection.workspace_fingerprint,
        expected_plan_fingerprint=imported.projection.plan_fingerprint,
    )
    assert type(current) is ProductionAutomaticPlanAuthorityV2
    legacy_context, legacy_proposal, retained = _retained_legacy_authority(context, current)

    # Simulate an authority retained in the process across the V1-to-V2 code migration.
    entry = registry._entries[created.workspace_handle]
    assert entry.automatic_materializer is not None
    entry.workspace = retained.workspace
    entry.automatic_plan = retained
    entry.automatic_materializer.planning_context = legacy_context
    entry.automatic_materializer.proposal = legacy_proposal

    claim, receipt = registry.materialize_automatic_plan_segment(
        retained,
        legacy_proposal.segments[0].segment_id,
    )
    try:
        assert receipt == retained.materialization_receipts[0]
    finally:
        claim.release()


def test_retained_v1_authority_remains_assemblable(tmp_path: Path) -> None:
    context, _proposal, _registry, _projection, current, _sequence, _receipts = _m26_subject(
        SegmentationPolicyV1.FIXED_10
    )
    _legacy_context_value, _legacy_proposal_value, retained = _retained_legacy_authority(
        context,
        current,
    )
    sequence, receipts = _m26_ready_sequence(retained)  # type: ignore[arg-type]
    artifact_store = PrivateSegmentArtifactStore(
        tmp_path / "artifacts",
        clock_ms=lambda: 2_000,
    )
    runtime, _bridge = _m26_runtime(tmp_path, artifact_store)

    execution = _m26_execute(
        runtime=runtime,
        authority=retained,  # type: ignore[arg-type]
        sequence=sequence,
        receipts=receipts,
        clock=_M26Clock(),
    )

    assert execution.authorization.automatic_plan_fingerprint == retained.fingerprint
    assert execution.assembly_receipt.workspace_fingerprint == retained.workspace.fingerprint
