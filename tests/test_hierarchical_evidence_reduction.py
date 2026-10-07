from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path

import pytest

from comfyui_h3_context.core import (
    AssetRole,
    FeasibleAVTimelinePlan,
    FeasibleAVTimelineRequest,
    FrameGridPolicy,
    GraphAlternative,
    GraphConflict,
    GraphNodeKind,
    GraphObservation,
    GraphObservationKind,
    GraphSourceSpan,
    HierarchicalEvidenceReductionRequest,
    MediaKind,
    ReductionBudget,
    ReductionEvidence,
    ReductionLevel,
    ReductionModality,
    ReductionStatus,
    ReductionTarget,
    ReferenceAnchor,
    ReferenceAsset,
    TaskMode,
    TaskModeRetentionRequest,
    TimePoint,
    UnifiedEvidenceGraph,
    build_feasible_av_timeline,
    build_hierarchical_evidence_reduction,
    build_reference_registry,
    build_task_mode_retention_report,
    build_unified_evidence_graph,
)

ROOT = Path(__file__).resolve().parents[1]


def _context(*, ambiguity: bool = False) -> tuple[UnifiedEvidenceGraph, FeasibleAVTimelinePlan]:
    registry = build_reference_registry(
        (ReferenceAsset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),)
    )
    report = build_task_mode_retention_report(
        TaskModeRetentionRequest.from_user_mode(TaskMode.I2VA, registry)
    )
    timeline = build_feasible_av_timeline(
        FeasibleAVTimelineRequest(
            report,
            TimePoint.from_text("4"),
            FrameGridPolicy(24, 1),
            registry,
            anchors=(
                ReferenceAnchor("anchor.first", "first_frame", "first", TimePoint.from_text("0")),
            ),
        )
    )
    assert timeline.plan is not None
    span = GraphSourceSpan("first", "source.first", "sha256:" + "a" * 64, 0, 1000)
    observation = GraphObservation(
        "obs.first",
        "first",
        MediaKind.IMAGE,
        GraphObservationKind.SUBJECT,
        "a declared subject",
        span,
    )
    alternatives: tuple[GraphAlternative, ...] = ()
    if ambiguity:
        alternatives = (
            GraphAlternative(
                "alt.subject",
                GraphNodeKind.OBSERVATION,
                "obs.first",
                ("obs.first", "obs.other"),
                "two source-owned candidates remain unresolved",
            ),
        )
    graph = build_unified_evidence_graph(observations=(observation,), alternatives=alternatives)
    return graph, timeline.plan


def _items(*, protect: bool = False) -> tuple[ReductionEvidence, ...]:
    return (
        ReductionEvidence(
            "asset.summary",
            ReductionLevel.ASSET,
            ReductionModality.VISUAL,
            "asset appearance summary",
            ("first",),
            token_estimate=4,
            fidelity_weight=4,
            salience=Decimal("0.40"),
        ),
        ReductionEvidence(
            "shot.summary",
            ReductionLevel.SHOT,
            ReductionModality.VISUAL,
            "opening shot composition",
            ("shot.1", "first"),
            token_estimate=4,
            fidelity_weight=5,
            salience=Decimal("0.80"),
            hard=protect,
            constraint_ids=("constraint.dialogue",) if protect else (),
        ),
        ReductionEvidence(
            "segment.audio",
            ReductionLevel.SEGMENT,
            ReductionModality.AUDIO,
            "source audio remains separate",
            ("shot.1",),
            token_estimate=4,
            fidelity_weight=3,
            salience=Decimal("0.60"),
        ),
        ReductionEvidence(
            "modality.summary",
            ReductionLevel.MODALITY_SUMMARY,
            ReductionModality.MULTIMODAL,
            "visual and audio timing relation",
            ("obs.first", "shot.1"),
            token_estimate=3,
            fidelity_weight=3,
            salience=Decimal("0.70"),
        ),
        ReductionEvidence(
            "relation.cross",
            ReductionLevel.CROSS_MODAL_RELATION,
            ReductionModality.MULTIMODAL,
            "the source subject and target shot remain linked",
            ("obs.first", "shot.1"),
            token_estimate=2,
            fidelity_weight=2,
            salience=Decimal("0.90"),
        ),
    )


def _request(
    graph: UnifiedEvidenceGraph,
    plan: FeasibleAVTimelinePlan,
    items: tuple[ReductionEvidence, ...],
    *,
    budget: ReductionBudget | None = None,
    target: ReductionTarget | None = None,
) -> HierarchicalEvidenceReductionRequest:
    return HierarchicalEvidenceReductionRequest(
        graph,
        plan,
        items,
        target
        or ReductionTarget(
            "target.opening",
            focus_source_ids=("shot.1", "first"),
            required_item_ids=("shot.summary",),
        ),
        budget or ReductionBudget(4, 13, Decimal("0.50"), Decimal("0.50"), Decimal("0.50")),
    )


def test_reduction_is_target_directed_and_records_retained_and_dropped_evidence() -> None:
    graph, plan = _context()
    result = build_hierarchical_evidence_reduction(_request(graph, plan, _items()))
    assert result.status is ReductionStatus.COMPLETE
    assert result.plan is not None
    assert result.plan.receipt.output_item_count <= 4
    assert "shot.summary" in result.plan.receipt.retained_item_ids
    assert result.plan.receipt.dropped_item_ids
    assert result.plan.receipt.source_coverage >= Decimal("0.50")
    assert result.plan.receipt.target_coverage >= Decimal("0.50")
    assert result.plan.receipt.rationale
    assert result.plan.fingerprint == result.plan.fingerprint


def test_hard_constraint_cannot_be_dropped_and_budget_failure_is_fail_closed() -> None:
    graph, plan = _context()
    request = _request(
        graph,
        plan,
        _items(protect=True),
        budget=ReductionBudget(1, 1, Decimal("0"), Decimal("0")),
    )
    result = build_hierarchical_evidence_reduction(request)
    assert result.status is ReductionStatus.BLOCKED
    assert result.plan is None
    assert any(item.code == "protected_budget_exceeded" for item in result.diagnostics)
    assert result.receipt.protected_item_ids == ("shot.summary",)


def test_graph_ambiguity_must_be_represented_by_a_protected_item() -> None:
    graph, plan = _context(ambiguity=True)
    result = build_hierarchical_evidence_reduction(_request(graph, plan, _items()))
    assert result.status is ReductionStatus.BLOCKED
    assert any(item.code == "unrepresented_high_risk_evidence" for item in result.diagnostics)


def test_conflicting_graph_remains_conflicting_even_when_conflict_is_represented() -> None:
    graph, plan = _context()
    conflict = GraphConflict(
        "conflict.subject",
        GraphNodeKind.OBSERVATION,
        ("obs.first", "obs.other"),
        "two observations disagree",
    )
    graph = build_unified_evidence_graph(
        observations=graph.observations
        + (
            GraphObservation(
                "obs.other",
                "first",
                MediaKind.IMAGE,
                GraphObservationKind.OBJECT,
                "another candidate",
                GraphSourceSpan("first", "source.other", "sha256:" + "b" * 64, 0, 1000),
            ),
        ),
        conflicts=(conflict,),
    )
    items = _items() + (
        ReductionEvidence(
            "conflict.guard",
            ReductionLevel.MODALITY_SUMMARY,
            ReductionModality.MULTIMODAL,
            "conflict remains unresolved",
            ("obs.first",),
            token_estimate=2,
            conflict_ids=("conflict.subject",),
            high_risk=True,
        ),
    )
    result = build_hierarchical_evidence_reduction(_request(graph, plan, items))
    assert result.status is ReductionStatus.CONFLICTING
    assert result.plan is None
    assert any(item.code == "upstream_conflict" for item in result.diagnostics)


def test_dependency_closure_and_threshold_failure_are_visible() -> None:
    graph, plan = _context()
    items = _items() + (
        ReductionEvidence(
            "dependent.detail",
            ReductionLevel.SEGMENT,
            ReductionModality.TEXT,
            "dependent exact text",
            ("shot.1",),
            token_estimate=2,
            fidelity_weight=4,
            depends_on=("relation.cross",),
        ),
    )
    request = _request(
        graph,
        plan,
        items,
        budget=ReductionBudget(2, 5, Decimal("0.99"), Decimal("0.99")),
        target=ReductionTarget("target.strict", focus_source_ids=("anchor.first",)),
    )
    result = build_hierarchical_evidence_reduction(request)
    assert result.status is ReductionStatus.BLOCKED
    assert any(item.code == "target_source_uncovered" for item in result.diagnostics)


def test_reduction_is_immutable_and_deterministic() -> None:
    graph, plan = _context()
    request = _request(graph, plan, _items())
    first = build_hierarchical_evidence_reduction(request)
    second = build_hierarchical_evidence_reduction(request)
    assert first.to_wire() == second.to_wire()
    assert first.plan is not None and second.plan is not None
    assert first.plan.fingerprint == second.plan.fingerprint
    with pytest.raises(FrozenInstanceError):
        first.plan.status = ReductionStatus.BLOCKED  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        request.target = replace(request.target, target_id="other")  # type: ignore[misc]


def test_schema_fixture_and_wire_bound_are_frozen() -> None:
    schema = json.loads(
        (
            ROOT / "governance" / "contracts" / "hierarchical_evidence_reduction_v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["$id"] == (
        "comfyui-h3-context://contracts/hierarchical_evidence_reduction_v1.schema.json"
    )
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "m13_07_hierarchical_evidence_reduction.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["fixture_status"] == "static_hierarchical_reduction_contract"
    assert fixture["reduction"]["schema"] == "h3.hierarchical_evidence_reduction.v1"
    graph, plan = _context()
    result = build_hierarchical_evidence_reduction(_request(graph, plan, _items()))
    assert len(result.to_wire_bytes()) <= 262_144
