"""Real Context -> owned planning -> canonical materialization -> atomic Production import."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import pytest

from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.production_planning_service import (
    PRODUCTION_PLANNING_ACTION_SCHEMA,
    ProductionPlanningService,
    decode_production_planning_action,
    decode_production_planning_json,
)
from comfyui_h3_context.core.constraints import HardConstraintSet
from comfyui_h3_context.core.native_h3 import build_native_h3_wiring
from comfyui_h3_context.core.normalization import RawContextRequest
from comfyui_h3_context.core.production_storyboard import StoryboardShotV1
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.sidebar_workspace import SidebarWorkspaceProjection
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextValidatorNode,
)


def environment(
    *,
    ttl: int = 60,
    max_requests: int = 256,
    max_ledger: int = 256,
    constraints: HardConstraintSet | None = None,
) -> tuple[
    ProductionPlanningService,
    SidebarWorkspaceRegistry,
    ProductionWorkspaceRegistry,
    SidebarWorkspaceProjection,
    dict[str, Any],
    list[float],
]:
    clock = [10.0]
    raw = RawContextRequest(
        mode="t2va",
        user_intent="A blue sphere turns slowly.",
        duration_seconds=10.0,
        hard_constraints=constraints if constraints is not None else HardConstraintSet(),
    )
    plan = H3ContextPlanNode().build_plan(raw)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    assert report.is_successful and report.validation.is_valid
    sidebar = SidebarWorkspaceRegistry(clock=lambda: clock[0], ttl_seconds=ttl)
    source = sidebar.publish(
        report, build_native_h3_wiring(report), ExecutionCorrelation("owned", "node")
    )
    production = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        clock=lambda: clock[0],
        max_ledger_entries=max_ledger,
    )
    created = production.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "create",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": source.workspace_id},
        }
    ).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    service = ProductionPlanningService(
        sidebar_registry=sidebar,
        production_registry=production,
        clock=lambda: clock[0],
        max_requests=max_requests,
    )
    prepare: dict[str, Any] = {
        "workspace_handle": created.workspace_handle,
        "expected_workspace_revision": created.workspace_revision,
        "expected_workspace_fingerprint": created.workspace_fingerprint,
        "context_workspace_handle": source.workspace_id,
        "expected_report_revision": source.report_revision,
        "expected_report_fingerprint": source.report_fingerprint,
        "expected_planning_revision": 0,
        "target_seconds": 20,
        "policy": "fixed_10",
    }
    return service, sidebar, production, source, prepare, clock


def action(kind: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "schema": PRODUCTION_PLANNING_ACTION_SCHEMA,
        "request_id": request_id,
        "action": kind,
        "payload": payload,
    }


def test_absence_only_constraint_cannot_cross_owned_source_or_automatic_authority() -> None:
    from comfyui_h3_context.adapters.production_context_materializer import (
        CanonicalProductionMaterializer,
    )
    from comfyui_h3_context.core.constraints import (
        ContentScope,
        ForbiddenContent,
        HardConstraintSet,
    )
    from comfyui_h3_context.core.production_import import ProductionImportError

    environments = [
        environment(
            constraints=HardConstraintSet((ForbiddenContent("forbid", ContentScope.VISUAL, text),))
        )
        for text in ("red emblem", "green emblem")
    ]
    entries = []
    authorities = []
    for service, sidebar, production, _source, prepare, _clock in environments:
        _, proposed = prepared_proposal(service, prepare)
        entry = service._entries[prepare["workspace_handle"]]
        entries.append(entry)
        imported: dict[str, Any] = service.dispatch(
            action(
                "import_plan",
                {
                    **selectors(proposed),
                    "proposal_id": proposed["proposal"]["proposal_id"],
                },
                "import",
            )
        ).to_wire()
        authorities.append(
            production.claim_automatic_plan_authority(
                prepare["workspace_handle"],
                expected_workspace_revision=2,
                expected_workspace_fingerprint=imported["workspace_fingerprint"],
                expected_plan_fingerprint=imported["plan_fingerprint"],
            )
        )
        assert set(sidebar._entries) == {entry.source.workspace_id}
    left, right = entries
    assert left.source.report.prompt_document.text == right.source.report.prompt_document.text
    assert left.context.source_request_fingerprint != right.context.source_request_fingerprint
    assert left.context.fingerprint != right.context.fingerprint
    assert left.proposal is not None and right.proposal is not None
    assert left.proposal.fingerprint != right.proposal.fingerprint
    assert authorities[0].fingerprint != authorities[1].fingerprint
    # Receipt V1 is not the whole source authority: absence-only constraints must remain bound
    # by the retained V2 request/context/proposal even when the visible prompt is identical.
    for source_entry, foreign_entry in ((left, right), (right, left)):
        materializer = CanonicalProductionMaterializer(
            source_entry.source, workspace_registry=environments[0][1]
        )
        assert foreign_entry.proposal is not None
        with pytest.raises(ProductionImportError, match="segment_context_source_mismatch"):
            materializer(
                foreign_entry.context, foreign_entry.proposal, foreign_entry.proposal.segments[0]
            )


@pytest.mark.parametrize("shot_count", [16, 64])
def test_dense_reviewed_storyboard_reaches_real_sixty_second_import(shot_count: int) -> None:
    service, sidebar, production, source, prepare, _clock = environment()
    prepare["target_seconds"] = 60
    prepared: dict[str, Any] = service.dispatch(
        action("prepare_context", prepare, "prepare")
    ).to_wire()
    rows = [
        StoryboardShotV1(
            f"shot_{index + 1}",
            index + 1,
            index * 60_000 // shot_count,
            (index + 1) * 60_000 // shot_count,
            f"The blue sphere turns through pose {index + 1}.",
        ).to_wire()
        for index in range(shot_count)
    ]
    admitted: dict[str, Any] = service.dispatch(
        action(
            "admit_storyboard",
            {
                **selectors(prepared),
                "source_kind": "user_reviewed_typed_rows",
                "typed_rows": rows,
                "user_reviewed": True,
            },
            "admit",
        )
    ).to_wire()
    proposed: dict[str, Any] = service.dispatch(
        action(
            "propose",
            {
                **selectors(admitted),
                "admission_id": admitted["admission_id"],
            },
            "propose",
        )
    ).to_wire()
    assert proposed["source_duration_seconds"] == 10
    assert [row["duration_seconds"] for row in proposed["proposal"]["segments"]] == [10] * 6
    imported: dict[str, Any] = service.dispatch(
        action(
            "import_plan",
            {
                **selectors(proposed),
                "proposal_id": proposed["proposal"]["proposal_id"],
            },
            "import",
        )
    ).to_wire()
    assert len(imported["materialization_receipt_fingerprints"]) == 6
    assert imported["workspace_revision"] == 2
    assert set(sidebar._entries) == {source.workspace_id}
    authority = production.claim_automatic_plan_authority(
        prepare["workspace_handle"],
        expected_workspace_revision=2,
        expected_workspace_fingerprint=imported["workspace_fingerprint"],
        expected_plan_fingerprint=imported["plan_fingerprint"],
    )
    assert sum(segment.duration.requested_seconds for segment in authority.proposal.segments) == 60


def test_other_live_context_cannot_replace_the_production_seed() -> None:
    service, sidebar, production, source, prepare, _clock = environment()
    plan = H3ContextPlanNode().build_plan(
        RawContextRequest(
            mode="t2va",
            user_intent="A yellow cube rises.",
            duration_seconds=10.0,
        )
    )[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    foreign = sidebar.publish(
        report, build_native_h3_wiring(report), ExecutionCorrelation("other", "node")
    )
    crossed = {
        **prepare,
        "context_workspace_handle": foreign.workspace_id,
        "expected_report_revision": foreign.report_revision,
        "expected_report_fingerprint": foreign.report_fingerprint,
    }
    with pytest.raises(ProductionWorkbenchError, match="planning_source_mismatch"):
        service.dispatch(action("prepare_context", crossed, "crossed.prepare"))
    assert not service._entries and not service._requests
    assert production._entries[prepare["workspace_handle"]].workspace.revision == 1
    assert set(sidebar._entries) == {source.workspace_id, foreign.workspace_id}


def selectors(projection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "workspace_handle": projection["workspace_handle"],
        "expected_workspace_revision": projection["workspace_revision"],
        "expected_workspace_fingerprint": projection["workspace_fingerprint"],
        "planning_context_id": projection["planning_context_id"],
        "expected_planning_revision": projection["planning_revision"],
    }


def prepared_proposal(
    service: ProductionPlanningService,
    prepare: dict[str, Any],
    *,
    canonical: bool = False,
    reviewed_rows: tuple[StoryboardShotV1, ...] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prepared: dict[str, Any] = service.dispatch(
        action("prepare_context", prepare, "prepare")
    ).to_wire()
    if reviewed_rows is None:
        reviewed_rows = (
            StoryboardShotV1("reviewed_1", 1, 0, 10_000, "A blue sphere turns slowly."),
            StoryboardShotV1("reviewed_2", 2, 10_000, 20_000, "The same sphere stops."),
        )
    rows = [] if canonical else [row.to_wire() for row in reviewed_rows]
    admitted: dict[str, Any] = service.dispatch(
        action(
            "admit_storyboard",
            {
                **selectors(prepared),
                "source_kind": "canonical_optimized_prompt"
                if canonical
                else "user_reviewed_typed_rows",
                "typed_rows": rows,
                "user_reviewed": not canonical,
            },
            "admit",
        )
    ).to_wire()
    proposal: dict[str, Any] = service.dispatch(
        action(
            "propose", {**selectors(admitted), "admission_id": admitted["admission_id"]}, "propose"
        )
    ).to_wire()
    return prepared, proposal


@pytest.mark.parametrize("canonical", [False, True])
def test_real_product_import_and_lost_response_replay(canonical: bool) -> None:
    service, sidebar, production, source, prepare, clock = environment()
    prepared, proposed = prepared_proposal(service, prepare, canonical=canonical)
    assert prepared["source_duration_seconds"] == 10 and prepared["target_seconds"] == 20
    assert len(proposed["proposal"]["segments"]) == 2
    before = set(sidebar._entries)
    request = action(
        "import_plan",
        {**selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
        "import",
    )
    imported: dict[str, Any] = service.dispatch(request).to_wire()
    assert imported["workspace_revision"] == 2
    assert len(imported["materialization_receipt_fingerprints"]) == 2
    assert set(sidebar._entries) == before == {source.workspace_id}
    authority = production.claim_automatic_plan_authority(
        prepare["workspace_handle"],
        expected_workspace_revision=2,
        expected_workspace_fingerprint=imported["workspace_fingerprint"],
        expected_plan_fingerprint=imported["plan_fingerprint"],
    )
    assert len(authority.materialization_receipts) == 2
    clock[0] += 61
    assert service.dispatch(request).to_wire() == imported
    assert (
        source.workspace_id not in sidebar._entries
        or sidebar._entries[source.workspace_id].touched_at == 10.0
    )
    changed = deepcopy(request)
    changed["payload"]["proposal_id"] = "foreign"
    with pytest.raises(ProductionWorkbenchError, match="request_id_conflict"):
        service.dispatch(changed)


def test_source_expiry_and_report_edit_invalidate_new_import() -> None:
    service, sidebar, production, source, prepare, clock = environment()
    _, proposed = prepared_proposal(service, prepare)
    request = action(
        "import_plan",
        {**selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
        "import",
    )
    clock[0] += 61
    with pytest.raises(ProductionWorkbenchError):
        service.dispatch(request)
    workspace = production.claim_workspace_authority(
        prepare["workspace_handle"],
        expected_workspace_revision=1,
        expected_workspace_fingerprint=prepare["expected_workspace_fingerprint"],
    )
    assert workspace.revision == 1


def test_prepare_change_invalidates_old_admission_and_preserves_source_clock() -> None:
    service, sidebar, _, source, prepare, clock = environment()
    prepared, proposed = prepared_proposal(service, prepare)
    clock[0] += 5
    updated = {
        **prepare,
        "target_seconds": 30,
        "expected_planning_revision": proposed["planning_revision"],
    }
    current: dict[str, Any] = service.dispatch(
        action("prepare_context", updated, "prepare.new")
    ).to_wire()
    assert current["planning_context_id"] != prepared["planning_context_id"]
    assert current["admission_id"] is None and current["proposal"] is None
    assert sidebar._entries[source.workspace_id].touched_at == 10.0
    with pytest.raises(ProductionWorkbenchError, match="planning_stale"):
        service.dispatch(action("read_plan", selectors(proposed), "read.old"))


@pytest.mark.parametrize(
    "key,value",
    [
        ("materialize", "callable"),
        ("source_request_fingerprint", "sha256:" + "0" * 64),
        ("qualification", {}),
        ("report", {}),
    ],
)
def test_no_public_authority_injection(key: str, value: object) -> None:
    *_, prepare, _clock = environment()
    with pytest.raises(ProductionWorkbenchError):
        decode_production_planning_action(
            action("prepare_context", {**prepare, key: value}, "inject")
        )


def test_duplicate_json_and_unreviewed_rows_are_rejected() -> None:
    with pytest.raises(ProductionWorkbenchError):
        decode_production_planning_json(b'{"schema":"a","schema":"b"}')
    service, _, _, _, prepare, _ = environment()
    prepared: dict[str, Any] = service.dispatch(
        action("prepare_context", prepare, "prepare")
    ).to_wire()
    with pytest.raises(ProductionWorkbenchError):
        service.dispatch(
            action(
                "admit_storyboard",
                {
                    **selectors(prepared),
                    "source_kind": "user_reviewed_typed_rows",
                    "typed_rows": [],
                    "user_reviewed": False,
                },
                "unreviewed",
            )
        )


def test_request_capacity_refuses_before_import_materialization_or_revision_change() -> None:
    service, sidebar, production, source, prepare, _ = environment(max_requests=3)
    _, proposed = prepared_proposal(service, prepare)
    with pytest.raises(ProductionWorkbenchError, match="planning_request_capacity"):
        service.dispatch(
            action(
                "import_plan",
                {**selectors(proposed), "proposal_id": proposed["proposal"]["proposal_id"]},
                "import",
            )
        )
    assert set(sidebar._entries) == {source.workspace_id}
    assert (
        production.claim_workspace_authority(
            prepare["workspace_handle"],
            expected_workspace_revision=1,
            expected_workspace_fingerprint=prepare["expected_workspace_fingerprint"],
        ).revision
        == 1
    )
