"""Adverse real-broker tests for M26-03 source and Production atomicity."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from copy import deepcopy
from typing import cast

import pytest

# IMPORTANT: discovery uses top-level test modules; a tests. prefix duplicates mypy identities.
from test_m26_03_planning_service import (
    action,
    environment,
    prepared_proposal,
    selectors,
)

from comfyui_h3_context.adapters import production_context_materializer
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionDispatchResult,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.production_planning_source import ProductionPlanningSourceSnapshot
from comfyui_h3_context.core.context_reporting import ContextReport
from comfyui_h3_context.core.native_h3 import NativeH3Wiring
from comfyui_h3_context.core.production_import import (
    MaterializeSegmentContext,
    ProductionAutomaticPlanDispatchResultV1,
    ProductionImportRequestV1,
    SegmentContextMaterializationClaim,
)
from comfyui_h3_context.core.production_storyboard import (
    ProductionPlanningContextV1,
    ProductionPlanningContextV2,
    ProposalSegmentV1,
    SegmentationProposalV1,
    SegmentationProposalV2,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.sidebar_workspace import SidebarWorkspaceProjection
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation


def _import_action(proposed: dict[str, object], request_id: str = "import") -> dict[str, object]:
    proposal = proposed["proposal"]
    assert type(proposal) is dict
    return action(
        "import_plan",
        {**selectors(proposed), "proposal_id": proposal["proposal_id"]},
        request_id,
    )


def _assert_target_unmodified(
    production: ProductionWorkspaceRegistry,
    prepare: dict[str, object],
) -> None:
    handle = cast(str, prepare["workspace_handle"])
    workspace = production.claim_workspace_authority(
        handle,
        expected_workspace_revision=1,
        expected_workspace_fingerprint=cast(str, prepare["expected_workspace_fingerprint"]),
    )
    assert workspace.revision == 1
    assert production._entries[handle].automatic_plan is None


def _stage_source(
    sidebar: SidebarWorkspaceRegistry,
    source: SidebarWorkspaceProjection,
) -> None:
    sidebar.dispatch(
        {
            "schema": "h3.context.sidebar.action.v2",
            "workspace_id": source.workspace_id,
            "expected_revision": source.report_revision,
            "expected_report_fingerprint": source.report_fingerprint,
            "action": "stage_prompt",
            "payload": {
                "reason": "Concurrent source edit",
                "prompt_text": source.prompt_text.replace("blue sphere", "teal sphere"),
            },
        }
    )


def test_source_edit_during_real_materialization_refuses_before_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sidebar, production, source, prepare, _clock = environment()
    _prepared, proposed = prepared_proposal(service, prepare)
    original = production_context_materializer.CanonicalProductionMaterializer
    changed = False

    class EditDuringMaterialization:
        def __init__(
            self,
            snapshot: ProductionPlanningSourceSnapshot,
            *,
            workspace_registry: SidebarWorkspaceRegistry,
        ) -> None:
            self._inner = original(snapshot, workspace_registry=workspace_registry)

        def __call__(
            self,
            context: ProductionPlanningContextV2,
            proposal: SegmentationProposalV2,
            segment: ProposalSegmentV1,
        ) -> SegmentContextMaterializationClaim:
            nonlocal changed
            claim = self._inner(context, proposal, segment)
            if not changed:
                changed = True
                _stage_source(sidebar, source)
            return claim

    monkeypatch.setattr(
        production_context_materializer,
        "CanonicalProductionMaterializer",
        EditDuringMaterialization,
    )

    with pytest.raises(ProductionWorkbenchError, match="planning_source_stale"):
        service.dispatch(_import_action(proposed))
    assert changed
    _assert_target_unmodified(production, prepare)
    assert set(sidebar._entries) == {source.workspace_id}


def test_second_ordinal_failure_releases_first_temporary_context_and_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sidebar, production, source, prepare, _clock = environment()
    _prepared, proposed = prepared_proposal(service, prepare)
    original_publish = sidebar.publish_production_materialization
    publication_count = 0

    def fail_second_publication(
        report: ContextReport,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
    ) -> SidebarWorkspaceProjection:
        nonlocal publication_count
        publication_count += 1
        if publication_count == 2:
            raise RuntimeError("fault injection")
        return original_publish(report, wiring, correlation)

    monkeypatch.setattr(sidebar, "publish_production_materialization", fail_second_publication)
    with pytest.raises(
        ProductionWorkbenchError,
        match="segment_context_materialization_failed",
    ):
        service.dispatch(_import_action(proposed))

    assert publication_count == 2
    assert set(sidebar._entries) == {source.workspace_id}
    _assert_target_unmodified(production, prepare)
    assert "import" not in production._ledger


def test_concurrent_import_cas_has_one_winner_and_one_closed_stale_refusal() -> None:
    service, sidebar, production, source, prepare, _clock = environment()
    _prepared, proposed = prepared_proposal(service, prepare)
    barrier = threading.Barrier(3)
    outcomes: list[object] = []
    outcome_lock = threading.Lock()

    def run(request_id: str) -> None:
        barrier.wait()
        try:
            outcome: object = service.dispatch(_import_action(proposed, request_id)).to_wire()
        except Exception as exc:  # The assertion below verifies the exact closed category.
            outcome = exc
        with outcome_lock:
            outcomes.append(outcome)

    workers = [threading.Thread(target=run, args=(f"import.{index}",)) for index in (1, 2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    successes = [cast(dict[str, object], row) for row in outcomes if type(row) is dict]
    failures = [row for row in outcomes if isinstance(row, ProductionWorkbenchError)]
    assert len(successes) == len(failures) == 1
    assert failures[0].code == "stale_workspace"
    assert successes[0]["workspace_revision"] == 2
    assert production._entries[prepare["workspace_handle"]].workspace.revision == 2
    assert set(sidebar._entries) == {source.workspace_id}


def test_last_production_ledger_slot_filled_during_materialization_refuses_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sidebar, production, source, prepare, _clock = environment(max_ledger=2)
    _prepared, proposed = prepared_proposal(service, prepare)
    original_publish = sidebar.publish_production_materialization
    fill_result: ProductionDispatchResult | None = None

    def fill_last_slot(
        report: ContextReport,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
    ) -> SidebarWorkspaceProjection:
        nonlocal fill_result
        if fill_result is None:
            fill_result = production.dispatch(
                {
                    "schema": PRODUCTION_ACTION_SCHEMA,
                    "request_id": "concurrent.create",
                    "action": "create_workspace_from_context",
                    "payload": {"context_workspace_handle": source.workspace_id},
                }
            )
        return original_publish(report, wiring, correlation)

    monkeypatch.setattr(sidebar, "publish_production_materialization", fill_last_slot)
    with pytest.raises(ProductionWorkbenchError, match="request_capacity"):
        service.dispatch(_import_action(proposed))

    assert fill_result is not None and fill_result.projection is not None
    assert len(production._ledger) == 2
    assert "import" not in production._ledger
    assert set(sidebar._entries) == {source.workspace_id}
    _assert_target_unmodified(production, prepare)


def test_lost_broker_response_after_production_commit_replays_without_materializing_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sidebar, production, source, prepare, _clock = environment()
    _prepared, proposed = prepared_proposal(service, prepare)
    original_import = production.import_automatic_plan
    lost = False
    committed_wire: dict[str, object] | None = None
    publication_count = 0
    original_publish = sidebar.publish_production_materialization

    def count_publication(
        report: ContextReport,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
    ) -> SidebarWorkspaceProjection:
        nonlocal publication_count
        publication_count += 1
        return original_publish(report, wiring, correlation)

    def lose_after_commit(
        request: ProductionImportRequestV1,
        *,
        planning_context: ProductionPlanningContextV1 | ProductionPlanningContextV2,
        proposal: SegmentationProposalV1 | SegmentationProposalV2,
        materialize: MaterializeSegmentContext,
        source_guard: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> ProductionAutomaticPlanDispatchResultV1:
        nonlocal committed_wire, lost
        result = original_import(
            request,
            planning_context=planning_context,
            proposal=proposal,
            materialize=materialize,
            source_guard=source_guard,
        )
        committed_wire = result.projection.to_wire()
        if not lost:
            lost = True
            raise RuntimeError("simulated lost response")
        return result

    monkeypatch.setattr(sidebar, "publish_production_materialization", count_publication)
    monkeypatch.setattr(production, "import_automatic_plan", lose_after_commit)
    request = _import_action(proposed)
    with pytest.raises(RuntimeError, match="simulated lost response"):
        service.dispatch(request)
    assert production._entries[prepare["workspace_handle"]].workspace.revision == 2
    assert publication_count == 2
    assert committed_wire is not None

    replay = service.dispatch(request).to_wire()
    assert replay == committed_wire
    assert publication_count == 2
    assert set(sidebar._entries) == {source.workspace_id}


def test_stale_proposal_cross_workspace_and_unknown_source_selectors_refuse_pre_effect() -> None:
    service, sidebar, production, source, prepare, _clock = environment()
    _prepared, proposed = prepared_proposal(service, prepare)

    stale = _import_action(proposed, "stale.proposal")
    cast(dict[str, object], stale["payload"])["proposal_id"] = "foreign_proposal"
    with pytest.raises(ProductionWorkbenchError, match="planning_proposal_stale"):
        service.dispatch(stale)
    _assert_target_unmodified(production, prepare)

    second = production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "create.second",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": source.workspace_id},
        }
    ).projection
    assert isinstance(second, ProductionWorkbenchProjection)
    prepare_second = {
        **prepare,
        "workspace_handle": second.workspace_handle,
        "expected_workspace_revision": second.workspace_revision,
        "expected_workspace_fingerprint": second.workspace_fingerprint,
    }
    planned_second = service.dispatch(
        action("prepare_context", prepare_second, "prepare.second")
    ).to_wire()
    crossed = _import_action(proposed, "crossed")
    cast(dict[str, object], crossed["payload"]).update(
        {
            "workspace_handle": planned_second["workspace_handle"],
            "expected_workspace_revision": planned_second["workspace_revision"],
            "expected_workspace_fingerprint": planned_second["workspace_fingerprint"],
        }
    )
    with pytest.raises(ProductionWorkbenchError, match="planning_stale"):
        service.dispatch(crossed)

    unknown = deepcopy(prepare)
    unknown["context_workspace_handle"] = "ws_" + "x" * 32
    unknown["expected_planning_revision"] = proposed["planning_revision"]
    with pytest.raises(ProductionWorkbenchError, match="planning_source_unavailable"):
        service.dispatch(action("prepare_context", unknown, "prepare.unknown"))
    assert set(sidebar._entries) == {source.workspace_id}
    _assert_target_unmodified(production, prepare)


def test_reads_do_not_consume_ledgers_or_renew_source_or_planning_expiry() -> None:
    service, sidebar, production, source, prepare, clock = environment(ttl=60)
    _prepared, proposed = prepared_proposal(service, prepare)
    handle = prepare["workspace_handle"]
    planning_expiry = service._entries[handle].expires_at
    source_touch = sidebar._entries[source.workspace_id].touched_at
    production_touch = production._entries[handle].touched_at
    ledger_size = len(production._ledger)
    replay_size = len(service._requests)

    clock[0] = 69.0
    for index in range(3):
        read = service.dispatch(action("read_plan", selectors(proposed), f"read.{index}")).to_wire()
        assert read["planning_context_id"] == proposed["planning_context_id"]
    assert len(production._ledger) == ledger_size
    assert len(service._requests) == replay_size
    assert service._entries[handle].expires_at == planning_expiry
    assert sidebar._entries[source.workspace_id].touched_at == source_touch
    assert production._entries[handle].touched_at == production_touch

    clock[0] = 70.0
    with pytest.raises(ProductionWorkbenchError, match="planning_unavailable"):
        service.dispatch(action("read_plan", selectors(proposed), "read.expired"))
    assert len(production._ledger) == ledger_size
