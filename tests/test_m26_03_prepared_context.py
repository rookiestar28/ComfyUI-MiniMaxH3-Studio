"""M26-03 closed prepared Context read and real lower-consumer coverage."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import replace
from typing import NoReturn

import pytest
from test_m26_03_context_materializer import _proposal, _source
from test_m26_04_managed_sequence_service import (
    _authorization,
    _authorize_request,
    _fp,
    _ProductionRegistry,
    _qualification,
    _service,
    _started,
)

from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.managed_sequence_service import (
    MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA,
    MANAGED_PREPARED_CONTEXT_SCHEMA,
    AuthorizeManagedSequenceRequestV1,
    ManagedChildBindingRequestV1,
    ManagedSegmentMaterializationClaimV1,
    ManagedSegmentMaterializationClaimV2,
    ManagedSequenceMutationAuthorityV1,
    ManagedSequenceService,
    ManagedSequenceServiceError,
    ManagedSequenceStartResourceClaimV1,
    ManagedSequenceStartResourceRequestV1,
    PrepareManagedSequenceChildRequestV1,
    ReadManagedPreparedContextRequestV1,
    StartManagedSequenceRequestV1,
    build_managed_sequence_service,
    decode_managed_sequence_action_json,
    dispatch_decoded_managed_sequence_action,
)
from comfyui_h3_context.adapters.production_context_materializer import (
    PRODUCTION_CANONICAL_LOWERING_REASON,
    PRODUCTION_CANONICAL_LOWERING_SCHEMA,
    CanonicalProductionMaterializer,
    claim_canonical_production_materialization,
)
from comfyui_h3_context.adapters.production_planning_source import (
    ProductionPlanningSourceSnapshot,
)
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.managed_sequence import (
    ManagedModeQualificationV1,
    ManagedSequenceAuthorizationV1,
    ManagedSequenceV1,
)
from comfyui_h3_context.core.production_import import (
    ProductionImportRequestV1,
    derive_segment_context_materialization_receipt,
)
from comfyui_h3_context.core.production_storyboard import (
    ProductionPlanningContextV2,
    SegmentationProposalV2,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection


def _prepared_service(
    sidebar: SidebarWorkspaceRegistry,
    *,
    mode: TaskMode = TaskMode.T2VA,
) -> tuple[
    ManagedSequenceService,
    ReadManagedPreparedContextRequestV1,
    ProductionPlanningSourceSnapshot,
    ProductionPlanningContextV2,
    SegmentationProposalV2,
]:
    source = _source(sidebar, mode=mode)
    context, proposal = _proposal(source, parts=(8,))
    materializer = CanonicalProductionMaterializer(source, workspace_registry=sidebar)
    sample = materializer(context, proposal, proposal.segments[0])
    receipt = derive_segment_context_materialization_receipt(
        context, proposal, proposal.segments[0], sample
    )
    sample.release()
    source_authority = object()
    production = _ProductionRegistry(source_authority)

    def authority_builder(
        _source_authority: object,
        **values: object,
    ) -> ManagedSequenceAuthorizationV1:
        authority = _authorization(str(values["sequence_id"]), count=1)
        row = replace(
            authority.segments[0],
            segment_id=receipt.segment_id,
            task_mode=receipt.task_mode,
            duration_milliseconds=receipt.duration_milliseconds,
            frame_count=receipt.frame_count,
            materialization_receipt_fingerprint=receipt.fingerprint,
            local_prompt_fingerprint=receipt.local_prompt_fingerprint,
            intent_graph_fingerprint=receipt.intent_graph_fingerprint,
            profile_fingerprint=receipt.profile_fingerprint,
            capability_fingerprint=receipt.capability_fingerprint,
        )
        return replace(
            authority,
            host_capability_fingerprint=receipt.capability_fingerprint,
            segments=(row,),
        )

    def materialize_segment(
        _authority: object,
        segment_id: str,
    ) -> ManagedSegmentMaterializationClaimV2:
        assert _authority is source_authority
        assert segment_id == proposal.segments[0].segment_id
        raw = materializer(context, proposal, proposal.segments[0])
        current_receipt = derive_segment_context_materialization_receipt(
            context, proposal, proposal.segments[0], raw
        )
        return ManagedSegmentMaterializationClaimV2(
            context_workspace_handle=raw.context_workspace_handle,
            segment_id=segment_id,
            materialization_receipt_fingerprint=current_receipt.fingerprint,
            receipt=current_receipt,
            materialization=claim_canonical_production_materialization(raw),
            release=raw.release,
        )

    tokens = iter(("a" * 40, "b" * 40))
    service = ManagedSequenceService(
        production_registry=production,
        authorization_builder=authority_builder,
        materialize_segment=materialize_segment,
        reserve_start_resources=lambda request: ManagedSequenceStartResourceClaimV1(
            request_fingerprint=request.fingerprint,
            coordinator_reservation_fingerprint="sha256:" + "1" * 64,
            child_slot_reservation_fingerprint="sha256:" + "2" * 64,
            artifact_reservation_fingerprint="sha256:" + "3" * 64,
            consume=lambda _request_id, _rows: None,
            release=lambda: None,
        ),
        token_factory=lambda: next(tokens),
        clock=lambda: 100.0,
        epoch_clock_ms=lambda: 1_000_000,
    )
    authorized = service.authorize(
        replace(
            _authorize_request(),
            host_capability_fingerprint=receipt.capability_fingerprint,
        )
    )
    authority = authority_builder(source_authority, sequence_id=authorized.parent_sequence_id)
    started = service.start(
        StartManagedSequenceRequestV1(
            request_id="start.prepared.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=authorized.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        ),
        qualification=_qualification(authority),
    )
    prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.context.1",
            parent_sequence_id=started.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=started.authorization_fingerprint,
            segment_id=proposal.segments[0].segment_id,
            predecessor_terminal_fingerprint=None,
        )
    )
    assert prepared.execution is not None
    request = ReadManagedPreparedContextRequestV1(
        request_id="read.prepared.1",
        parent_sequence_id=prepared.parent_sequence_id,
        expected_revision=prepared.revision,
        authorization_fingerprint=prepared.authorization_fingerprint,
        segment_id=proposal.segments[0].segment_id,
        eligible_execution_fingerprint=prepared.execution.fingerprint,
        materialization_receipt_fingerprint=receipt.fingerprint,
        context_workspace_handle=str(prepared.context_workspace_handle),
    )
    return service, request, source, context, proposal


def test_distinct_prepared_context_decoder_is_closed_and_response_has_no_request_id() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    service, request, _source_snapshot, _context, _proposal_value = _prepared_service(sidebar)
    body = request.to_wire()
    body.pop("schema")
    body.pop("request_id")
    encoded = json.dumps(
        {
            "schema": MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA,
            "request_id": request.request_id,
            "action": "read_prepared_child_context",
            "payload": body,
        }
    ).encode()

    decoded = decode_managed_sequence_action_json(encoded)
    result = dispatch_decoded_managed_sequence_action(
        service,
        decoded,
        qualification_claimant=lambda _fingerprint: None,
    )
    wire = result.to_wire()

    assert wire["schema"] == MANAGED_PREPARED_CONTEXT_SCHEMA
    assert "request_id" not in wire
    assert wire["canonical_prompt"]
    assert wire["context_workspace_handle"] == request.context_workspace_handle
    assert wire["profile"] == {"name": "h3_base", "version": "1.0"}
    lowering = wire["canonical_lowering"]
    assert isinstance(lowering, dict)
    assert set(lowering) == {
        "schema",
        "base_report_fingerprint",
        "base_report_revision",
        "override_revision",
        "reason",
    }
    assert lowering["schema"] == PRODUCTION_CANONICAL_LOWERING_SCHEMA
    assert lowering["reason"] == PRODUCTION_CANONICAL_LOWERING_REASON
    assert lowering["override_revision"] == lowering["base_report_revision"] + 1
    assert lowering["base_report_fingerprint"] != wire["context_report_fingerprint"]
    with pytest.raises(ManagedSequenceServiceError, match="invalid_action_envelope"):
        decode_managed_sequence_action_json(
            json.dumps(
                {
                    "schema": MANAGED_PREPARED_CONTEXT_ACTION_SCHEMA,
                    "request_id": "read.prepared.extra",
                    "action": "read_prepared_child_context",
                    "payload": {**body, "unexpected": True},
                }
            ).encode()
        )


@pytest.mark.parametrize("leading", ("-", "_"))
def test_default_managed_nonce_prefix_preserves_urlsafe_entropy_and_real_read(
    monkeypatch: pytest.MonkeyPatch,
    leading: str,
) -> None:
    token = leading + "a" * 53
    monkeypatch.setattr(
        secrets,
        "token_urlsafe",
        lambda _size: token,
    )
    service = ManagedSequenceService(
        production_registry=_ProductionRegistry(),
        authorization_builder=lambda _source, **values: _authorization(
            str(values["sequence_id"]), count=1
        ),
        reserve_start_resources=lambda request: ManagedSequenceStartResourceClaimV1(
            request_fingerprint=request.fingerprint,
            coordinator_reservation_fingerprint=_fp("nonce.coordinator"),
            child_slot_reservation_fingerprint=_fp("nonce.child"),
            artifact_reservation_fingerprint=_fp("nonce.artifact"),
            consume=lambda _request_id, _rows: None,
            release=lambda: None,
        ),
        clock=lambda: 100.0,
        epoch_clock_ms=lambda: 1_000_000,
    )

    authorized = service.authorize(_authorize_request())
    authority = _authorization(authorized.parent_sequence_id, count=1)
    started = service.start(
        StartManagedSequenceRequestV1(
            request_id="start.nonce.1",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=authorized.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        ),
        qualification=_qualification(authority),
    )
    read = service.read_projection(
        started.parent_sequence_id,
        started.read_authority_fingerprint,
    )

    assert read.status == 200
    assert read.projection is not None
    assert authorized.parent_sequence_id.startswith("managed.read.")


def test_custom_managed_nonce_factory_still_refuses_invalid_identifier() -> None:
    service = ManagedSequenceService(
        production_registry=_ProductionRegistry(),
        authorization_builder=lambda _source, **values: _authorization(
            str(values["sequence_id"]), count=1
        ),
        token_factory=lambda: "_" + "a" * 39,
        clock=lambda: 100.0,
        epoch_clock_ms=lambda: 1_000_000,
    )

    with pytest.raises(ManagedSequenceServiceError, match="invalid_read_authority_nonce"):
        service.authorize(_authorize_request())


def test_prepared_read_joins_every_held_identity_and_legacy_claims_cannot_read() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    service, request, _source_snapshot, _context, _proposal_value = _prepared_service(sidebar)

    first = service.read_prepared_child_context(request)
    second = service.read_prepared_child_context(replace(request, request_id="read.prepared.2"))
    assert first == second
    changed_requests = (
        replace(request, authorization_fingerprint="sha256:" + "a" * 64),
        replace(request, eligible_execution_fingerprint="sha256:" + "b" * 64),
        replace(request, materialization_receipt_fingerprint="sha256:" + "c" * 64),
        replace(request, context_workspace_handle="ws_" + "d" * 32),
    )
    for changed_request in changed_requests:
        with pytest.raises(ManagedSequenceServiceError):
            service.read_prepared_child_context(changed_request)

    cancelled = service.cancel(
        ManagedSequenceMutationAuthorityV1(
            request_id="cancel.prepared.1",
            parent_sequence_id=request.parent_sequence_id,
            expected_revision=request.expected_revision,
            authorization_fingerprint=request.authorization_fingerprint,
        )
    )
    with pytest.raises(ManagedSequenceServiceError, match="prepared_context_unavailable"):
        service.read_prepared_child_context(
            replace(request, request_id="read.after.cancel", expected_revision=cancelled.revision)
        )
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.claim_production_seed(request.context_workspace_handle)

    legacy_service, _production = _service(
        materialize=lambda _authority, segment_id: ManagedSegmentMaterializationClaimV1(
            context_workspace_handle="ws_" + "e" * 32,
            segment_id=segment_id,
            materialization_receipt_fingerprint=_fp("materialization.1"),
            release=lambda: None,
        )
    )
    _authorized, _authority, legacy_started = _started(legacy_service)
    legacy_prepared = legacy_service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.legacy.1",
            parent_sequence_id=legacy_started.parent_sequence_id,
            expected_revision=legacy_started.revision,
            authorization_fingerprint=legacy_started.authorization_fingerprint,
            segment_id="segment.1",
            predecessor_terminal_fingerprint=None,
        )
    )
    assert legacy_prepared.execution is not None
    with pytest.raises(ManagedSequenceServiceError, match="prepared_context_unavailable"):
        legacy_service.read_prepared_child_context(
            ReadManagedPreparedContextRequestV1(
                request_id="read.legacy.1",
                parent_sequence_id=legacy_prepared.parent_sequence_id,
                expected_revision=legacy_prepared.revision,
                authorization_fingerprint=legacy_prepared.authorization_fingerprint,
                segment_id="segment.1",
                eligible_execution_fingerprint=legacy_prepared.execution.fingerprint,
                materialization_receipt_fingerprint=_fp("materialization.1"),
                context_workspace_handle="ws_" + "e" * 32,
            )
        )


def test_prepared_read_preserves_actual_full_reference_profile_and_ordered_rows() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    service, request, _source_snapshot, _context, _proposal_value = _prepared_service(
        sidebar,
        mode=TaskMode.REF2VA,
    )

    wire = service.read_prepared_child_context(request).to_wire()

    assert wire["task_mode"] == "ref2va"
    assert wire["profile"] == {"name": "h3_full_reference", "version": "1.0"}
    assert wire["ordered_references"] == [
        {
            "asset_id": "asset_ref",
            "kind": "image",
            "role": "reference",
            "connection_order": 1,
            "metadata": None,
            "paired_video_id": None,
        }
    ]
    assert wire["canonical_lowering"] is None


def test_ephemeral_sidebar_is_immutable_but_remains_a_nonrefreshing_production_seed() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(8,))
    claim = CanonicalProductionMaterializer(source, workspace_registry=sidebar)(
        context, proposal, proposal.segments[0]
    )
    snapshot = claim_canonical_production_materialization(claim)

    assert sidebar.claim_production_seed(claim.context_workspace_handle) == snapshot.seed
    assert (
        sidebar.claim_production_materialization_seed(
            claim.context_workspace_handle,
            expected_report_revision=snapshot.report_revision,
            expected_report_fingerprint=snapshot.report_fingerprint,
        )
        == snapshot.seed
    )
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.get(claim.context_workspace_handle)
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.claim_assisted_source(
            claim.context_workspace_handle,
            snapshot.report_revision,
            snapshot.report_fingerprint,
        )
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.claim_authoring_seed(claim.context_workspace_handle)
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.claim_authoring_workspace(claim.context_workspace_handle)
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.snapshot_production_planning_source(
            claim.context_workspace_handle,
            expected_report_revision=snapshot.report_revision,
            expected_report_fingerprint=snapshot.report_fingerprint,
        )
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.dispatch(
            {
                "schema": "h3.context.sidebar.action.v2",
                "workspace_id": claim.context_workspace_handle,
                "expected_revision": snapshot.report_revision,
                "expected_report_fingerprint": snapshot.report_fingerprint,
                "action": "stage_prompt",
                "payload": {"reason": "blocked", "prompt_text": "blocked"},
            }
        )
    snapshot.assert_current()
    claim.release()


def test_real_production_consumer_creates_one_segment_then_releases_both_authorities() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=8, ttl_seconds=60)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(8,))
    materializer = CanonicalProductionMaterializer(source, workspace_registry=sidebar)
    production = ProductionWorkspaceRegistry(seed_claim=sidebar.claim_production_seed)
    initial = production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.initial.prepared",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": source.workspace_id},
        }
    ).projection
    assert isinstance(initial, ProductionWorkbenchProjection)
    imported = production.import_automatic_plan(
        ProductionImportRequestV1(
            request_id="production.import.prepared",
            workspace_handle=initial.workspace_handle,
            workspace_id=initial.workspace_id,
            expected_workspace_revision=initial.workspace_revision,
            expected_workspace_fingerprint=initial.workspace_fingerprint,
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            proposal_fingerprint=proposal.fingerprint,
            planning_context_fingerprint=context.fingerprint,
            source_request_fingerprint=context.source_request_fingerprint,
            source_profile_fingerprint=context.source_profile_fingerprint,
        ),
        planning_context=context,
        proposal=proposal,
        materialize=materializer,
    )
    authority = production.claim_automatic_plan_authority(
        initial.workspace_handle,
        expected_workspace_revision=imported.projection.workspace_revision,
        expected_workspace_fingerprint=imported.projection.workspace_fingerprint,
        expected_plan_fingerprint=imported.projection.plan_fingerprint,
    )
    claim, receipt = production.materialize_automatic_plan_segment(
        authority, proposal.segments[0].segment_id
    )
    assert receipt == authority.materialization_receipts[0]

    child = production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.child.prepared",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": claim.context_workspace_handle},
        }
    ).projection
    assert isinstance(child, ProductionWorkbenchProjection)
    assert len(child.segments) == 1
    assert child.segments[0].task_mode == receipt.task_mode.value
    assert child.segments[0].duration_milliseconds == receipt.duration_milliseconds

    production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.child.release",
            "action": "release_workspace",
            "payload": {
                "workspace_handle": child.workspace_handle,
                "expected_workspace_revision": child.workspace_revision,
                "expected_workspace_fingerprint": child.workspace_fingerprint,
            },
        }
    )
    claim.release()
    with pytest.raises(KeyError, match="workspace is unavailable"):
        sidebar.claim_production_seed(claim.context_workspace_handle)

    class _Coordinator:
        lease_transition: Callable[[str, float], None]

        def reserve_managed_sequence_resources(
            self,
            request: ManagedSequenceStartResourceRequestV1,
        ) -> ManagedSequenceStartResourceClaimV1:
            return ManagedSequenceStartResourceClaimV1(
                request_fingerprint=request.fingerprint,
                coordinator_reservation_fingerprint="sha256:" + "4" * 64,
                child_slot_reservation_fingerprint="sha256:" + "5" * 64,
                artifact_reservation_fingerprint="sha256:" + "6" * 64,
                consume=lambda _request_id, _rows: None,
                release=lambda: None,
            )

        def bind_managed_sequence_child(
            self,
            _request: ManagedChildBindingRequestV1,
        ) -> NoReturn:
            raise AssertionError("M26-04 owns the qualified parent bind")

        # IMPORTANT (B-M2522-FIXTURE-01): the service composition refuses a coordinator missing
        # any owner seam, so this fake must track every seam `build_managed_sequence_service`
        # requires; completion publication is never reached by this start-only journey.
        def publish_completed_managed_sequence(
            self,
            _source_authority: object,
            _workspace_handle: str,
            _candidate: ManagedSequenceV1,
        ) -> NoReturn:
            raise AssertionError("M26-05 owns completed-sequence publication")

        def transition_managed_sequence_lease(
            self,
            _before: ManagedSequenceV1,
            _after: ManagedSequenceV1,
            _now: float,
        ) -> None:
            return None

        def bind_managed_sequence_lease_transition(
            self,
            callback: Callable[[str, float], None],
        ) -> None:
            self.lease_transition = callback

    service = build_managed_sequence_service(
        production_registry=production,
        coordinator=_Coordinator(),
    )
    compiler_fingerprint = "sha256:" + "7" * 64
    generation_fingerprint = "sha256:" + "8" * 64
    authorized = service.authorize(
        AuthorizeManagedSequenceRequestV1(
            request_id="authorize.actual.prepared",
            workspace_handle=initial.workspace_handle,
            expected_workspace_revision=imported.projection.workspace_revision,
            expected_workspace_fingerprint=imported.projection.workspace_fingerprint,
            expected_plan_fingerprint=imported.projection.plan_fingerprint,
            generation_plan_fingerprint=generation_fingerprint,
            compiler_fingerprint=compiler_fingerprint,
            host_capability_fingerprint=receipt.capability_fingerprint,
            explicit_intent="generate_approved_sequence",
        )
    )
    observed = time.monotonic()
    started = service.start(
        StartManagedSequenceRequestV1(
            request_id="start.actual.prepared",
            parent_sequence_id=authorized.parent_sequence_id,
            expected_revision=authorized.revision,
            authorization_fingerprint=authorized.authorization_fingerprint,
        ),
        qualification=ManagedModeQualificationV1(
            host_capability_fingerprint=receipt.capability_fingerprint,
            compiler_fingerprint=compiler_fingerprint,
            qualified_global_modes=tuple(TaskMode),
            qualified_materialization_receipts=tuple(
                row.fingerprint for row in authority.materialization_receipts
            ),
            observed_at=observed,
            expires_at=observed + 1_000.0,
        ),
    )
    managed_prepared = service.prepare_child(
        PrepareManagedSequenceChildRequestV1(
            request_id="prepare.actual.context",
            parent_sequence_id=started.parent_sequence_id,
            expected_revision=started.revision,
            authorization_fingerprint=started.authorization_fingerprint,
            segment_id=proposal.segments[0].segment_id,
            predecessor_terminal_fingerprint=None,
        )
    )
    assert managed_prepared.execution is not None
    prepared_projection = service.read_prepared_child_context(
        ReadManagedPreparedContextRequestV1(
            request_id="read.actual.context",
            parent_sequence_id=managed_prepared.parent_sequence_id,
            expected_revision=managed_prepared.revision,
            authorization_fingerprint=managed_prepared.authorization_fingerprint,
            segment_id=proposal.segments[0].segment_id,
            eligible_execution_fingerprint=managed_prepared.execution.fingerprint,
            materialization_receipt_fingerprint=receipt.fingerprint,
            context_workspace_handle=str(managed_prepared.context_workspace_handle),
        )
    )
    assert prepared_projection.canonical_prompt == proposal.segments[0].local_prompt
    service.cancel(
        ManagedSequenceMutationAuthorityV1(
            request_id="cancel.actual.prepared",
            parent_sequence_id=managed_prepared.parent_sequence_id,
            expected_revision=managed_prepared.revision,
            authorization_fingerprint=managed_prepared.authorization_fingerprint,
        )
    )
