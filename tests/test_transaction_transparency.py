from __future__ import annotations

from dataclasses import replace

import pytest

from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.pipeline_transaction import (
    PipelineTransaction,
    PipelineTransactionState,
)
from comfyui_h3_context.core.recompute_closure import (
    RecomputeDisposition,
    RecomputePlan,
    SegmentRecomputeDecision,
)
from comfyui_h3_context.core.transaction_transparency import (
    TransactionTransparencyError,
    build_transaction_transparency_projection,
)
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation


def _fp(character: str) -> str:
    return "sha256:" + character * 64


def _plan() -> RecomputePlan:
    return RecomputePlan(
        decisions=(
            SegmentRecomputeDecision(
                "segment.1",
                RecomputeDisposition.DIRTY_SELF,
                ("producer_fingerprint_changed",),
                ("segment.1",),
            ),
            SegmentRecomputeDecision(
                "segment.2",
                RecomputeDisposition.DIRTY_UPSTREAM,
                ("upstream_producer_changed",),
                ("segment.1",),
            ),
        ),
        mandatory_segment_ids=("segment.1", "segment.2"),
        requested_segment_ids=("segment.1", "segment.2"),
        missing_required_segment_ids=(),
        selection_safe=True,
        requires_full_recompute=False,
    )


def _transaction(
    state: PipelineTransactionState = PipelineTransactionState.PREPARED,
) -> PipelineTransaction:
    plan = _plan()
    fields: dict[str, object] = {
        "transaction_id": "transaction.1",
        "attempt": 1,
        "state": state,
        "workspace_id": "workspace.1",
        "workspace_revision": 2,
        "workspace_fingerprint": _fp("a"),
        "manifest_fingerprints": (_fp("b"), _fp("c")),
        "recompute_plan_fingerprint": canonical_fingerprint(plan.to_public_dict()),
        "dirty_segment_ids": plan.mandatory_segment_ids,
    }
    if state not in {PipelineTransactionState.NOOP_CLEAN, PipelineTransactionState.BLOCKED}:
        fields.update(graph_fingerprint=_fp("d"), compiled_prompt_fingerprint=_fp("e"))
    if state in {
        PipelineTransactionState.SUBMITTED,
        PipelineTransactionState.RUNNING,
        PipelineTransactionState.SUCCEEDED,
        PipelineTransactionState.FAILED,
        PipelineTransactionState.UNKNOWN_OWNERSHIP,
    }:
        fields["queue_prompt_id"] = "prompt.1"
    if state is PipelineTransactionState.RUNNING:
        fields["host_owner_id"] = "host.execution.1"
    if state in {PipelineTransactionState.SUCCEEDED, PipelineTransactionState.FAILED}:
        fields["result_fingerprint"] = _fp("f")
    return PipelineTransaction(**fields)  # type: ignore[arg-type]


def test_projection_is_deterministic_closed_and_content_free() -> None:
    correlation = ExecutionCorrelation("prompt.1", "17")
    first = build_transaction_transparency_projection(_plan(), _transaction(), correlation)
    second = build_transaction_transparency_projection(_plan(), _transaction(), correlation)

    assert first == second
    assert first.transaction.state is PipelineTransactionState.PREPARED
    assert first.actions.confirm_native_queue is True
    assert first.actions.inspect_queue_history is False
    assert first.actions.rerun is False
    assert [item.segment_id for item in first.decisions] == ["segment.1", "segment.2"]
    assert first.to_wire()["schema"] == "h3.context.transaction_transparency.v1"
    encoded = str(first.to_wire()).lower()
    for forbidden in ("user_intent", "prompt_text", "media_path", "credential", "http://"):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    ("state", "confirm", "history", "rerun", "guidance"),
    [
        (PipelineTransactionState.PREPARED, True, False, False, "review_before_native_queue"),
        (PipelineTransactionState.SUBMITTED, False, True, False, "inspect_queue_history"),
        (PipelineTransactionState.RUNNING, False, True, False, "inspect_queue_history"),
        (PipelineTransactionState.UNKNOWN_OWNERSHIP, False, True, False, "ownership_unknown"),
        (PipelineTransactionState.SUCCEEDED, False, True, False, "generation_complete"),
        (PipelineTransactionState.FAILED, False, True, True, "rerun_available"),
        (PipelineTransactionState.CANCELLED, False, False, True, "rerun_available"),
    ],
)
def test_actions_are_backend_derived_from_exact_transaction_state(
    state: PipelineTransactionState,
    confirm: bool,
    history: bool,
    rerun: bool,
    guidance: str,
) -> None:
    projection = build_transaction_transparency_projection(
        _plan(),
        _transaction(state),
        ExecutionCorrelation("prompt.1", "17"),
    )
    assert projection.actions.confirm_native_queue is confirm
    assert projection.actions.inspect_queue_history is history
    assert projection.actions.rerun is rerun
    assert projection.guidance.value == guidance


def test_authority_mismatch_fails_before_ui_projection() -> None:
    transaction = replace(
        _transaction(),
        recompute_plan_fingerprint=_fp("9"),
        transaction_fingerprint=None,
    )
    with pytest.raises(TransactionTransparencyError, match="recompute_authority_mismatch"):
        build_transaction_transparency_projection(
            _plan(),
            transaction,
            ExecutionCorrelation("prompt.1", "17"),
        )


def test_public_projection_rejects_direct_privacy_and_action_policy_forgery() -> None:
    projection = build_transaction_transparency_projection(
        _plan(),
        _transaction(),
        ExecutionCorrelation("prompt.1", "17"),
    )
    with pytest.raises(TransactionTransparencyError, match="workspace_id"):
        replace(projection, workspace_id=r"C:\private\workspace")
    with pytest.raises(TransactionTransparencyError, match="action_policy"):
        replace(
            projection,
            actions=replace(projection.actions, rerun=True),
        )
