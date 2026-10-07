"""M26-01 pure whole-video duration and deterministic partition contracts."""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from comfyui_h3_context.core import length
from comfyui_h3_context.core.production_duration import (
    DEFAULT_PRODUCTION_DURATION_SECONDS,
    MAX_PRODUCTION_DURATION_SECONDS,
    MAX_PRODUCTION_SEGMENTS,
    DurationPlanRefusalCodeV1,
    ProductionDurationError,
    ProductionDurationIntentV1,
    SegmentationPolicyV1,
    SegmentDurationPlanRefusalV1,
    SegmentDurationPlanV1,
    solve_segment_duration_plan,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "comfyui_h3_context" / "core" / "production_duration.py"


def _assert_exact_plan(plan: SegmentDurationPlanV1, target_seconds: int) -> None:
    assert plan.requested_total_seconds == target_seconds
    assert plan.requested_total_milliseconds == target_seconds * 1000
    assert sum(row.requested_seconds for row in plan.allocations) == target_seconds
    assert len(plan.allocations) <= MAX_PRODUCTION_SEGMENTS
    assert [row.ordinal for row in plan.allocations] == list(range(1, len(plan.allocations) + 1))
    for row in plan.allocations:
        assert 4 <= row.requested_seconds <= 15
        resolved = length.resolve_milliseconds(row.requested_seconds * 1000)
        assert row.requested_milliseconds == resolved.requested_milliseconds
        assert row.frame_count == resolved.frame_count
        assert row.delivered_milliseconds == resolved.delivered_milliseconds
        assert row.snapped == resolved.snapped
    assert plan.sampled_total_frames == sum(row.frame_count for row in plan.allocations)
    assert plan.sampled_total_milliseconds == sum(
        row.delivered_milliseconds for row in plan.allocations
    )


def _architecture_violations(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    word in alias.name.lower() for word in ("comfyui", "adapter", "http", "llm")
                ):
                    violations.append(f"import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(word in module.lower() for word in ("comfyui", "adapter", "http", "llm")):
                violations.append(f"import:{module}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and any(
                    token in target.id.upper()
                    for token in ("FPS", "LATTICE_OFFSET", "LATTICE_STEP")
                ):
                    violations.append(f"duplicate:{target.id}")
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mult)
            and any(
                isinstance(side, ast.Constant) and side.value == 24
                for side in (node.left, node.right)
            )
        ):
            violations.append("private_frame_arithmetic")
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            violations.append("float_literal")
    return tuple(violations)


def test_default_intent_is_a_distinct_closed_60_second_planning_contract() -> None:
    intent = ProductionDurationIntentV1()
    assert intent.target_seconds == DEFAULT_PRODUCTION_DURATION_SECONDS == 60
    assert MAX_PRODUCTION_DURATION_SECONDS == 60
    assert intent.policy is SegmentationPolicyV1.AUTO_STORYBOARD
    assert intent.segment_min_seconds == 4
    assert intent.segment_max_seconds == 15
    assert intent.max_segments == MAX_PRODUCTION_SEGMENTS == 15
    assert ProductionDurationIntentV1.from_wire(intent.to_wire()) == intent


@pytest.mark.parametrize("target_seconds", range(4, 61))
def test_auto_mode_partitions_every_v1_total_exactly_and_deterministically(
    target_seconds: int,
) -> None:
    intent = ProductionDurationIntentV1(target_seconds=target_seconds)
    first = solve_segment_duration_plan(intent)
    second = solve_segment_duration_plan(intent)
    assert type(first) is SegmentDurationPlanV1
    assert second == first
    _assert_exact_plan(first, target_seconds)
    assert SegmentDurationPlanV1.from_wire(first.to_wire()) == first


@pytest.mark.parametrize(
    ("policy", "expected_seconds"),
    [
        (SegmentationPolicyV1.FIXED_5, (5,) * 12),
        (SegmentationPolicyV1.FIXED_10, (10,) * 6),
        (SegmentationPolicyV1.FIXED_12, (12,) * 5),
        (SegmentationPolicyV1.FIXED_15, (15,) * 4),
    ],
)
def test_sixty_second_fixed_modes_have_the_exact_requested_counts(
    policy: SegmentationPolicyV1,
    expected_seconds: tuple[int, ...],
) -> None:
    result = solve_segment_duration_plan(
        ProductionDurationIntentV1(target_seconds=60, policy=policy)
    )
    assert type(result) is SegmentDurationPlanV1
    assert tuple(row.requested_seconds for row in result.allocations) == expected_seconds
    _assert_exact_plan(result, 60)


@pytest.mark.parametrize("target_seconds", range(4, 61))
@pytest.mark.parametrize(
    "policy",
    [
        SegmentationPolicyV1.FIXED_5,
        SegmentationPolicyV1.FIXED_10,
        SegmentationPolicyV1.FIXED_12,
        SegmentationPolicyV1.FIXED_15,
    ],
)
def test_every_fixed_input_is_an_exact_plan_or_stable_typed_refusal(
    target_seconds: int,
    policy: SegmentationPolicyV1,
) -> None:
    intent = ProductionDurationIntentV1(target_seconds=target_seconds, policy=policy)
    first = solve_segment_duration_plan(intent)
    second = solve_segment_duration_plan(intent)
    assert second == first
    if type(first) is SegmentDurationPlanV1:
        _assert_exact_plan(first, target_seconds)
        assert SegmentDurationPlanV1.from_wire(first.to_wire()) == first
    else:
        assert type(first) is SegmentDurationPlanRefusalV1
        assert first.recommended_policy is SegmentationPolicyV1.AUTO_STORYBOARD
        assert SegmentDurationPlanRefusalV1.from_wire(first.to_wire()) == first


def test_fixed_mode_uses_one_valid_final_remainder_without_padding() -> None:
    result = solve_segment_duration_plan(
        ProductionDurationIntentV1(
            target_seconds=14,
            policy=SegmentationPolicyV1.FIXED_10,
        )
    )
    assert type(result) is SegmentDurationPlanV1
    assert tuple(row.requested_seconds for row in result.allocations) == (10, 4)
    _assert_exact_plan(result, 14)


def test_impossible_fixed_remainder_is_a_stable_typed_refusal() -> None:
    intent = ProductionDurationIntentV1(
        target_seconds=11,
        policy=SegmentationPolicyV1.FIXED_5,
    )
    refusal = solve_segment_duration_plan(intent)
    assert type(refusal) is SegmentDurationPlanRefusalV1
    assert refusal.code is DurationPlanRefusalCodeV1.FIXED_REMAINDER_OUT_OF_DOMAIN
    assert refusal.recommended_policy is SegmentationPolicyV1.AUTO_STORYBOARD
    assert refusal.target_seconds == 11
    assert SegmentDurationPlanRefusalV1.from_wire(refusal.to_wire()) == refusal
    assert solve_segment_duration_plan(intent) == refusal


def test_auto_mode_prefers_reachable_storyboard_boundaries_before_segment_count() -> None:
    result = solve_segment_duration_plan(
        ProductionDurationIntentV1(
            target_seconds=30,
            candidate_boundaries_seconds=(10, 20),
        )
    )
    assert type(result) is SegmentDurationPlanV1
    assert tuple(row.requested_seconds for row in result.allocations) == (10, 10, 10)
    assert result.matched_candidate_boundaries_seconds == (10, 20)


@pytest.mark.parametrize(
    ("target_seconds", "expected"),
    [
        (17, (12, 5)),
        (16, (12, 4)),
    ],
)
def test_auto_mode_has_pinned_preference_and_stable_tie_breaks(
    target_seconds: int,
    expected: tuple[int, ...],
) -> None:
    result = solve_segment_duration_plan(ProductionDurationIntentV1(target_seconds=target_seconds))
    assert type(result) is SegmentDurationPlanV1
    assert tuple(row.requested_seconds for row in result.allocations) == expected


def test_whole_video_intent_never_reaches_the_single_clip_length_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    original = length.resolve_milliseconds

    def observe(milliseconds: int) -> length.ResolvedLength:
        calls.append(milliseconds)
        return original(milliseconds)

    monkeypatch.setattr(length, "resolve_milliseconds", observe)
    intent = ProductionDurationIntentV1(
        target_seconds=60,
        policy=SegmentationPolicyV1.FIXED_15,
    )
    assert calls == []
    result = solve_segment_duration_plan(intent)
    assert type(result) is SegmentDurationPlanV1
    assert calls
    assert 60_000 not in calls
    assert all(4_000 <= value <= 15_000 for value in calls)


def test_contracts_reject_cross_field_wire_and_lattice_drift() -> None:
    plan = solve_segment_duration_plan(ProductionDurationIntentV1(target_seconds=60))
    assert type(plan) is SegmentDurationPlanV1
    bad_wire = deepcopy(plan.to_wire())
    allocations = bad_wire["allocations"]
    assert isinstance(allocations, list)
    assert isinstance(allocations[0], dict)
    allocations[0]["frame_count"] = allocations[0]["frame_count"] + 17
    with pytest.raises(ProductionDurationError, match="allocation_canonical_tuple"):
        SegmentDurationPlanV1.from_wire(bad_wire)
    with pytest.raises(ProductionDurationError, match="allocation_canonical_tuple"):
        replace(plan.allocations[0], frame_count=plan.allocations[0].frame_count + 17)


def test_plan_wire_rejects_allocations_outside_its_declared_segment_domain() -> None:
    plan = solve_segment_duration_plan(ProductionDurationIntentV1(target_seconds=16))
    assert type(plan) is SegmentDurationPlanV1
    assert tuple(row.requested_seconds for row in plan.allocations) == (12, 4)
    bad_wire = deepcopy(plan.to_wire())
    intent = bad_wire["intent"]
    assert isinstance(intent, dict)
    domain = intent["segment_domain"]
    assert isinstance(domain, dict)
    domain["minimum_seconds"] = 5
    with pytest.raises(ProductionDurationError, match="plan_allocation_domain"):
        SegmentDurationPlanV1.from_wire(bad_wire)


def test_refusal_rejects_a_fixed_only_code_under_auto_policy() -> None:
    refusal = solve_segment_duration_plan(
        ProductionDurationIntentV1(
            target_seconds=11,
            policy=SegmentationPolicyV1.FIXED_5,
        )
    )
    assert type(refusal) is SegmentDurationPlanRefusalV1
    with pytest.raises(ProductionDurationError, match="refusal_policy_code"):
        replace(refusal, policy=SegmentationPolicyV1.AUTO_STORYBOARD)


@pytest.mark.parametrize("bad_target", [True, 3, 61, 4.0])
def test_total_duration_bounds_fail_closed_without_float_coercion(bad_target: object) -> None:
    with pytest.raises(ProductionDurationError, match="intent_target_seconds"):
        ProductionDurationIntentV1(target_seconds=bad_target)  # type: ignore[arg-type]


def test_segment_limit_refuses_instead_of_truncating_or_padding() -> None:
    result = solve_segment_duration_plan(
        ProductionDurationIntentV1(target_seconds=60, max_segments=3)
    )
    assert type(result) is SegmentDurationPlanRefusalV1
    assert result.code is DurationPlanRefusalCodeV1.NO_EXACT_PARTITION


def test_module_is_pure_and_architecture_guard_detects_duplicate_lattice_logic() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert "resolve_duration_request" not in source
    assert "NativeH3Adapter" not in source
    assert _architecture_violations(source) == ()
    assert _architecture_violations(source + "\n_PRIVATE_FPS = 24\n") == ("duplicate:_PRIVATE_FPS",)
    assert "import:comfyui_h3_context.adapters" in _architecture_violations(
        source + "\nfrom comfyui_h3_context.adapters import NativeH3Adapter\n"
    )
    assert "private_frame_arithmetic" in _architecture_violations(
        source + "\n_noncanonical = seconds * 24\n"
    )
