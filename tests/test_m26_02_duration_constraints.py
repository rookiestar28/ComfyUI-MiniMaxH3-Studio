"""Constraint-aware M26 production-duration partition regressions."""

from __future__ import annotations

from dataclasses import replace

import pytest

from comfyui_h3_context.core.production_duration import (
    MAX_CANDIDATE_BOUNDARIES,
    MAX_FORBIDDEN_CUT_INTERVALS,
    MAX_REQUIRED_CUTS,
    DurationPlanRefusalCodeV1,
    ProductionDurationError,
    ProductionDurationIntentV1,
    SegmentationPolicyV1,
    SegmentCutConstraintsV1,
    SegmentDurationPlanRefusalV1,
    SegmentDurationPlanV1,
    solve_segment_duration_plan,
)


def _parts(result: SegmentDurationPlanV1) -> tuple[int, ...]:
    return tuple(row.requested_seconds for row in result.allocations)


def _independent_legal_partitions(
    *,
    target: int,
    minimum: int,
    maximum: int,
    max_segments: int,
    required_cut_milliseconds: tuple[int, ...] = (),
    forbidden_cut_intervals_milliseconds: tuple[tuple[int, int], ...] = (),
) -> tuple[tuple[int, ...], ...]:
    """Small-domain oracle that enumerates values, not production DP states."""

    results: list[tuple[int, ...]] = []

    def visit(elapsed: int, parts: tuple[int, ...]) -> None:
        if elapsed == target:
            results.append(parts)
            return
        if len(parts) == max_segments:
            return
        for seconds in range(minimum, maximum + 1):
            next_elapsed = elapsed + seconds
            if next_elapsed > target:
                continue
            start_ms = elapsed * 1000
            end_ms = next_elapsed * 1000
            if any(start_ms < anchor < end_ms for anchor in required_cut_milliseconds):
                continue
            if next_elapsed < target and any(
                interval_start < end_ms < interval_end
                for interval_start, interval_end in forbidden_cut_intervals_milliseconds
            ):
                continue
            visit(next_elapsed, (*parts, seconds))

    visit(0, ())
    return tuple(results)


def _master_score(
    parts: tuple[int, ...], candidate_boundaries_seconds: tuple[int, ...]
) -> tuple[int, int, int, tuple[int, ...]]:
    elapsed = 0
    cuts: list[int] = []
    for seconds in parts[:-1]:
        elapsed += seconds
        cuts.append(elapsed)
    candidates = frozenset(candidate_boundaries_seconds)
    unaligned = sum(cut not in candidates for cut in cuts)
    preferred = sum(seconds in (5, 10, 12, 15) for seconds in parts)
    return -unaligned, -len(parts), preferred, parts


def test_candidate_cut_bound_is_independent_from_output_clip_bound() -> None:
    assert MAX_CANDIDATE_BOUNDARIES == 59
    all_internal_seconds = tuple(range(1, 60))
    intent = ProductionDurationIntentV1(
        target_seconds=60,
        candidate_boundaries_seconds=all_internal_seconds,
    )
    result = solve_segment_duration_plan(intent, constraints=SegmentCutConstraintsV1())
    assert type(result) is SegmentDurationPlanV1
    assert _parts(result) == (15, 15, 15, 15)
    assert len(result.allocations) == 4 <= intent.max_segments

    with pytest.raises(ProductionDurationError, match="intent_candidate_boundaries"):
        replace(intent, candidate_boundaries_seconds=(0, *all_internal_seconds))


def test_twenty_three_second_shots_do_not_overflow_or_truncate_candidate_cuts() -> None:
    shot_cuts = tuple(range(3, 60, 3))
    assert len(shot_cuts) == 19
    intent = ProductionDurationIntentV1(
        target_seconds=60,
        candidate_boundaries_seconds=shot_cuts,
    )
    result = solve_segment_duration_plan(intent, constraints=SegmentCutConstraintsV1())
    assert type(result) is SegmentDurationPlanV1
    assert _parts(result) == (15, 15, 15, 15)
    assert result.matched_candidate_boundaries_seconds == (15, 30, 45)


def test_constraint_wire_is_closed_canonical_and_fingerprint_bound() -> None:
    constraints = SegmentCutConstraintsV1(
        required_cut_milliseconds=(12_000,),
        forbidden_cut_intervals_milliseconds=((12_500, 15_500),),
    )
    assert SegmentCutConstraintsV1.from_wire(constraints.to_wire()) == constraints
    assert constraints.fingerprint.startswith("sha256:")
    changed = replace(constraints, required_cut_milliseconds=(10_000,))
    assert changed.fingerprint != constraints.fingerprint

    bad_wire = constraints.to_wire()
    bad_wire["extra"] = True
    with pytest.raises(ProductionDurationError, match="cut_constraints_wire"):
        SegmentCutConstraintsV1.from_wire(bad_wire)


def test_constraint_collections_have_separate_storyboard_derived_bounds() -> None:
    assert MAX_REQUIRED_CUTS == 63
    assert MAX_FORBIDDEN_CUT_INTERVALS == 192
    required = tuple(range(1, MAX_REQUIRED_CUTS + 1))
    hard_shot_intervals = tuple((value, value + 1) for value in range(64))
    timed_semantic_intervals = tuple((value, value + 1) for value in range(64, 128))
    typed_timing_intervals = tuple((value, value + 1) for value in range(128, 192))
    intervals = (*hard_shot_intervals, *timed_semantic_intervals, *typed_timing_intervals)
    constraints = SegmentCutConstraintsV1(
        required_cut_milliseconds=required,
        forbidden_cut_intervals_milliseconds=intervals,
    )
    assert len(constraints.required_cut_milliseconds) == 63
    assert len(constraints.forbidden_cut_intervals_milliseconds) == 192

    with pytest.raises(ProductionDurationError, match="cut_constraints_required"):
        SegmentCutConstraintsV1(required_cut_milliseconds=(*required, 64))
    with pytest.raises(ProductionDurationError, match="cut_constraints_intervals"):
        SegmentCutConstraintsV1(forbidden_cut_intervals_milliseconds=(*intervals, (192, 193)))


@pytest.mark.parametrize(
    "constraints",
    [
        SegmentCutConstraintsV1(required_cut_milliseconds=(12_500,)),
        SegmentCutConstraintsV1(forbidden_cut_intervals_milliseconds=((12_500, 15_500),)),
        SegmentCutConstraintsV1(
            required_cut_milliseconds=(12_000,),
            forbidden_cut_intervals_milliseconds=((12_500, 15_500),),
        ),
    ],
)
def test_same_explicit_constraints_are_deterministic(
    constraints: SegmentCutConstraintsV1,
) -> None:
    intent = ProductionDurationIntentV1(target_seconds=20)
    first = solve_segment_duration_plan(intent, constraints=constraints)
    second = solve_segment_duration_plan(intent, constraints=constraints)
    assert first == second


def test_auto_search_finds_a_safe_alternative_before_optimization() -> None:
    intent = ProductionDurationIntentV1(target_seconds=20)
    constraints = SegmentCutConstraintsV1(forbidden_cut_intervals_milliseconds=((12_500, 15_500),))
    result = solve_segment_duration_plan(intent, constraints=constraints)
    assert type(result) is SegmentDurationPlanV1
    assert _parts(result) == (10, 10)
    assert all(
        not 12_500 < row.requested_end_seconds * 1000 < 15_500 for row in result.allocations[:-1]
    )


def test_explicit_constraints_use_unaligned_cuts_before_clip_count() -> None:
    intent = ProductionDurationIntentV1(
        target_seconds=30,
        candidate_boundaries_seconds=(10, 15, 20),
    )
    legacy = solve_segment_duration_plan(intent)
    constrained = solve_segment_duration_plan(intent, constraints=SegmentCutConstraintsV1())
    assert type(legacy) is SegmentDurationPlanV1
    assert type(constrained) is SegmentDurationPlanV1
    assert _parts(legacy) == (10, 5, 5, 10)
    assert _parts(constrained) == (15, 15)


def test_required_anchor_is_forced_and_fractional_anchor_is_not_rounded() -> None:
    intent = ProductionDurationIntentV1(target_seconds=20)
    forced = solve_segment_duration_plan(
        intent,
        constraints=SegmentCutConstraintsV1(required_cut_milliseconds=(12_000,)),
    )
    assert type(forced) is SegmentDurationPlanV1
    assert _parts(forced) == (12, 8)

    refused = solve_segment_duration_plan(
        intent,
        constraints=SegmentCutConstraintsV1(required_cut_milliseconds=(12_500,)),
    )
    assert type(refused) is SegmentDurationPlanRefusalV1
    assert refused.code is DurationPlanRefusalCodeV1.REQUIRED_CUT_UNREPRESENTABLE


def test_fixed_policy_never_moves_an_unsafe_selected_cut() -> None:
    constraints = SegmentCutConstraintsV1(forbidden_cut_intervals_milliseconds=((12_500, 15_500),))
    fixed_twelve = solve_segment_duration_plan(
        ProductionDurationIntentV1(target_seconds=20, policy=SegmentationPolicyV1.FIXED_12),
        constraints=constraints,
    )
    assert type(fixed_twelve) is SegmentDurationPlanV1
    assert _parts(fixed_twelve) == (12, 8)

    fixed_fifteen = solve_segment_duration_plan(
        ProductionDurationIntentV1(target_seconds=20, policy=SegmentationPolicyV1.FIXED_15),
        constraints=constraints,
    )
    assert type(fixed_fifteen) is SegmentDurationPlanRefusalV1
    assert fixed_fifteen.code is DurationPlanRefusalCodeV1.FIXED_CUT_CONFLICT


def test_strict_interior_is_forbidden_but_interval_endpoints_are_legal() -> None:
    intent = ProductionDurationIntentV1(target_seconds=20)
    endpoint = solve_segment_duration_plan(
        intent,
        constraints=SegmentCutConstraintsV1(
            required_cut_milliseconds=(12_000,),
            forbidden_cut_intervals_milliseconds=((12_000, 16_000),),
        ),
    )
    assert type(endpoint) is SegmentDurationPlanV1
    assert _parts(endpoint) == (12, 8)

    impossible = solve_segment_duration_plan(
        intent,
        constraints=SegmentCutConstraintsV1(forbidden_cut_intervals_milliseconds=((0, 20_000),)),
    )
    assert type(impossible) is SegmentDurationPlanRefusalV1
    assert impossible.code is DurationPlanRefusalCodeV1.NO_FEASIBLE_PARTITION


def test_touching_intervals_stay_separate_and_their_shared_endpoint_is_legal() -> None:
    constraints = SegmentCutConstraintsV1(
        required_cut_milliseconds=(12_000,),
        forbidden_cut_intervals_milliseconds=((8_000, 12_000), (12_000, 16_000)),
    )
    assert constraints.forbidden_cut_intervals_milliseconds == (
        (8_000, 12_000),
        (12_000, 16_000),
    )
    result = solve_segment_duration_plan(
        ProductionDurationIntentV1(target_seconds=20), constraints=constraints
    )
    assert type(result) is SegmentDurationPlanV1
    assert _parts(result) == (12, 8)


@pytest.mark.parametrize(
    ("target", "candidate_boundaries", "required", "forbidden"),
    [
        (16, (), (), ()),
        (17, (), (), ()),
        (20, (), (), ((12_500, 15_500),)),
        (20, (), (12_000,), ((12_500, 15_500),)),
        (24, (8, 12, 20), (), ((8_500, 11_500),)),
    ],
)
def test_solver_matches_an_independent_bounded_small_domain_oracle(
    target: int,
    candidate_boundaries: tuple[int, ...],
    required: tuple[int, ...],
    forbidden: tuple[tuple[int, int], ...],
) -> None:
    intent = ProductionDurationIntentV1(
        target_seconds=target,
        candidate_boundaries_seconds=candidate_boundaries,
    )
    constraints = SegmentCutConstraintsV1(
        required_cut_milliseconds=required,
        forbidden_cut_intervals_milliseconds=forbidden,
    )
    legal = _independent_legal_partitions(
        target=target,
        minimum=intent.segment_min_seconds,
        maximum=intent.segment_max_seconds,
        max_segments=intent.max_segments,
        required_cut_milliseconds=required,
        forbidden_cut_intervals_milliseconds=forbidden,
    )
    assert legal
    expected = max(legal, key=lambda parts: _master_score(parts, candidate_boundaries))
    result = solve_segment_duration_plan(intent, constraints=constraints)
    assert type(result) is SegmentDurationPlanV1
    assert _parts(result) == expected


def test_solver_exhaustively_matches_single_constraint_small_domain() -> None:
    for target in range(4, 25):
        candidate_boundaries = tuple(range(3, target, 3))
        scenarios = [SegmentCutConstraintsV1()]
        scenarios.extend(
            SegmentCutConstraintsV1(required_cut_milliseconds=(cut * 1000,))
            for cut in range(1, target)
        )
        scenarios.extend(
            SegmentCutConstraintsV1(
                forbidden_cut_intervals_milliseconds=((cut * 1000 - 500, cut * 1000 + 500),)
            )
            for cut in range(1, target)
        )
        for constraints in scenarios:
            legal = _independent_legal_partitions(
                target=target,
                minimum=4,
                maximum=15,
                max_segments=15,
                required_cut_milliseconds=constraints.required_cut_milliseconds,
                forbidden_cut_intervals_milliseconds=(
                    constraints.forbidden_cut_intervals_milliseconds
                ),
            )
            result = solve_segment_duration_plan(
                ProductionDurationIntentV1(
                    target_seconds=target,
                    candidate_boundaries_seconds=candidate_boundaries,
                ),
                constraints=constraints,
            )
            if not legal:
                assert type(result) is SegmentDurationPlanRefusalV1
                assert result.code is DurationPlanRefusalCodeV1.NO_FEASIBLE_PARTITION
                continue
            assert type(result) is SegmentDurationPlanV1
            assert _parts(result) == max(
                legal,
                key=lambda parts: _master_score(parts, candidate_boundaries),
            )


def test_none_preserves_the_pinned_legacy_numeric_tie_break() -> None:
    expected = {
        16: (12, 4),
        17: (12, 5),
        18: (13, 5),
        20: (15, 5),
        22: (12, 10),
        31: (15, 12, 4),
        46: (12, 12, 12, 10),
        60: (15, 15, 15, 15),
    }
    for target, parts in expected.items():
        result = solve_segment_duration_plan(ProductionDurationIntentV1(target_seconds=target))
        assert type(result) is SegmentDurationPlanV1
        assert _parts(result) == parts


def test_constraint_shape_and_solver_join_fail_closed() -> None:
    with pytest.raises(ProductionDurationError, match="cut_constraints_required"):
        SegmentCutConstraintsV1(required_cut_milliseconds=(12_000, 12_000))
    with pytest.raises(ProductionDurationError, match="cut_constraints_required"):
        SegmentCutConstraintsV1(required_cut_milliseconds=(True,))
    with pytest.raises(ProductionDurationError, match="cut_constraints_intervals"):
        SegmentCutConstraintsV1(forbidden_cut_intervals_milliseconds=((15_500, 12_500),))
    with pytest.raises(ProductionDurationError, match="cut_constraints_intervals"):
        SegmentCutConstraintsV1(
            forbidden_cut_intervals_milliseconds=((12_500, 15_500), (10_000, 11_000))
        )
    with pytest.raises(ProductionDurationError, match="solver_constraints"):
        solve_segment_duration_plan(
            ProductionDurationIntentV1(target_seconds=20),
            constraints=object(),  # type: ignore[arg-type]
        )
