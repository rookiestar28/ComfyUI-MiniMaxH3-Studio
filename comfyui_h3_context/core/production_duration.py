"""Pure whole-video duration intent and deterministic H3 segment partitioning.

The whole-video value is planning data, not a single-clip duration. This module makes that boundary
structural: only validated per-segment allocations are resolved through the shared length authority.
It performs no I/O and owns no Production workspace, prompt, host, queue, or provider behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import cast

from . import length as _length
from .canonical import canonical_fingerprint

PRODUCTION_DURATION_INTENT_SCHEMA = "h3.context.production_duration_intent.v1"
SEGMENT_CUT_CONSTRAINTS_SCHEMA = "h3.context.segment_cut_constraints.v1"
SEGMENT_DURATION_ALLOCATION_SCHEMA = "h3.context.segment_duration_allocation.v1"
SEGMENT_DURATION_PLAN_SCHEMA = "h3.context.segment_duration_plan.v1"
SEGMENT_DURATION_PLAN_REFUSAL_SCHEMA = "h3.context.segment_duration_plan_refusal.v1"

MIN_PRODUCTION_DURATION_SECONDS = 4
MAX_PRODUCTION_DURATION_SECONDS = 60
DEFAULT_PRODUCTION_DURATION_SECONDS = 60
MIN_H3_SEGMENT_SECONDS = 4
MAX_H3_SEGMENT_SECONDS = 15
MAX_PRODUCTION_SEGMENTS = 15
# IMPORTANT: input shot cuts and output clip cuts have different ceilings. Rejoining this bound to
# MAX_PRODUCTION_SEGMENTS rejects valid dense storyboards before the 15-output solver can run.
MAX_CANDIDATE_BOUNDARIES = MAX_PRODUCTION_DURATION_SECONDS - 1
MAX_REQUIRED_CUTS = 63
# One valid authority may carry 64 hard-shot spans, 64 immutable timed-semantic spans, and 64
# typed timing-constraint ranges. These remain separate because touching endpoints are legal cuts.
MAX_FORBIDDEN_CUT_INTERVALS = 192
PREFERRED_AUTO_SEGMENT_SECONDS = (5, 10, 12, 15)


class ProductionDurationError(ValueError):
    """A duration intent, plan, allocation, or wire value is not closed and canonical."""


class SegmentationPolicyV1(str, Enum):
    AUTO_STORYBOARD = "auto_storyboard"
    FIXED_5 = "fixed_5"
    FIXED_10 = "fixed_10"
    FIXED_12 = "fixed_12"
    FIXED_15 = "fixed_15"


class DurationPlanRefusalCodeV1(str, Enum):
    FIXED_SEGMENT_OUT_OF_DOMAIN = "fixed_segment_out_of_domain"
    FIXED_REMAINDER_OUT_OF_DOMAIN = "fixed_remainder_out_of_domain"
    SEGMENT_LIMIT_EXCEEDED = "segment_limit_exceeded"
    NO_EXACT_PARTITION = "no_exact_partition"
    REQUIRED_CUT_UNREPRESENTABLE = "required_cut_unrepresentable"
    NO_FEASIBLE_PARTITION = "no_feasible_partition"
    FIXED_CUT_CONFLICT = "fixed_cut_conflict"


_FIXED_SECONDS = {
    SegmentationPolicyV1.FIXED_5: 5,
    SegmentationPolicyV1.FIXED_10: 10,
    SegmentationPolicyV1.FIXED_12: 12,
    SegmentationPolicyV1.FIXED_15: 15,
}
_AUTO_REFUSAL_CODES = frozenset(
    {
        DurationPlanRefusalCodeV1.NO_EXACT_PARTITION,
        DurationPlanRefusalCodeV1.REQUIRED_CUT_UNREPRESENTABLE,
        DurationPlanRefusalCodeV1.NO_FEASIBLE_PARTITION,
    }
)
_FIXED_REFUSAL_CODES = frozenset(
    {
        DurationPlanRefusalCodeV1.FIXED_SEGMENT_OUT_OF_DOMAIN,
        DurationPlanRefusalCodeV1.FIXED_REMAINDER_OUT_OF_DOMAIN,
        DurationPlanRefusalCodeV1.SEGMENT_LIMIT_EXCEEDED,
        DurationPlanRefusalCodeV1.REQUIRED_CUT_UNREPRESENTABLE,
        DurationPlanRefusalCodeV1.FIXED_CUT_CONFLICT,
    }
)


def _closed_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProductionDurationError(field)
    return value


def _closed_dict(value: object, field: str, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise ProductionDurationError(field)
    wire = cast(dict[str, object], value)
    if frozenset(wire) != keys:
        raise ProductionDurationError(field)
    return wire


def _policy(value: object, field: str) -> SegmentationPolicyV1:
    try:
        policy = SegmentationPolicyV1(value)
    except (TypeError, ValueError) as exc:
        raise ProductionDurationError(field) from exc
    if type(value) is not str:
        raise ProductionDurationError(field)
    return policy


@dataclass(frozen=True, slots=True)
class ProductionDurationIntentV1:
    """A bounded whole-video planning intent that cannot masquerade as one H3 clip."""

    target_seconds: int = DEFAULT_PRODUCTION_DURATION_SECONDS
    policy: SegmentationPolicyV1 = SegmentationPolicyV1.AUTO_STORYBOARD
    segment_min_seconds: int = MIN_H3_SEGMENT_SECONDS
    segment_max_seconds: int = MAX_H3_SEGMENT_SECONDS
    max_segments: int = MAX_PRODUCTION_SEGMENTS
    candidate_boundaries_seconds: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _closed_int(
            self.target_seconds,
            "intent_target_seconds",
            MIN_PRODUCTION_DURATION_SECONDS,
            MAX_PRODUCTION_DURATION_SECONDS,
        )
        if type(self.policy) is not SegmentationPolicyV1:
            raise ProductionDurationError("intent_policy")
        minimum = _closed_int(
            self.segment_min_seconds,
            "intent_segment_min_seconds",
            MIN_H3_SEGMENT_SECONDS,
            MAX_H3_SEGMENT_SECONDS,
        )
        maximum = _closed_int(
            self.segment_max_seconds,
            "intent_segment_max_seconds",
            MIN_H3_SEGMENT_SECONDS,
            MAX_H3_SEGMENT_SECONDS,
        )
        if minimum > maximum:
            raise ProductionDurationError("intent_segment_domain")
        _closed_int(self.max_segments, "intent_max_segments", 1, MAX_PRODUCTION_SEGMENTS)
        if (
            type(self.candidate_boundaries_seconds) is not tuple
            or len(self.candidate_boundaries_seconds) > MAX_CANDIDATE_BOUNDARIES
        ):
            raise ProductionDurationError("intent_candidate_boundaries")
        boundaries = tuple(
            _closed_int(value, "intent_candidate_boundaries", 1, self.target_seconds - 1)
            for value in self.candidate_boundaries_seconds
        )
        if boundaries != tuple(sorted(set(boundaries))):
            raise ProductionDurationError("intent_candidate_boundaries")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PRODUCTION_DURATION_INTENT_SCHEMA,
            "target_seconds": self.target_seconds,
            "policy": self.policy.value,
            "segment_domain": {
                "minimum_seconds": self.segment_min_seconds,
                "maximum_seconds": self.segment_max_seconds,
            },
            "max_segments": self.max_segments,
            "candidate_boundaries_seconds": list(self.candidate_boundaries_seconds),
        }

    @classmethod
    def from_wire(cls, value: object) -> ProductionDurationIntentV1:
        wire = _closed_dict(
            value,
            "intent_wire",
            frozenset(
                {
                    "schema",
                    "target_seconds",
                    "policy",
                    "segment_domain",
                    "max_segments",
                    "candidate_boundaries_seconds",
                }
            ),
        )
        if wire["schema"] != PRODUCTION_DURATION_INTENT_SCHEMA:
            raise ProductionDurationError("intent_schema")
        domain = _closed_dict(
            wire["segment_domain"],
            "intent_segment_domain_wire",
            frozenset({"minimum_seconds", "maximum_seconds"}),
        )
        boundaries = wire["candidate_boundaries_seconds"]
        if type(boundaries) is not list:
            raise ProductionDurationError("intent_candidate_boundaries_wire")
        return cls(
            target_seconds=cast(int, wire["target_seconds"]),
            policy=_policy(wire["policy"], "intent_policy"),
            segment_min_seconds=cast(int, domain["minimum_seconds"]),
            segment_max_seconds=cast(int, domain["maximum_seconds"]),
            max_segments=cast(int, wire["max_segments"]),
            candidate_boundaries_seconds=tuple(cast(list[int], boundaries)),
        )


@dataclass(frozen=True, slots=True)
class SegmentCutConstraintsV1:
    """Closed millisecond feasibility authority kept separate from numeric v1 intent."""

    required_cut_milliseconds: tuple[int, ...] = ()
    forbidden_cut_intervals_milliseconds: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        required = self.required_cut_milliseconds
        if type(required) is not tuple or len(required) > MAX_REQUIRED_CUTS:
            raise ProductionDurationError("cut_constraints_required")
        checked_required = tuple(
            _closed_int(
                value,
                "cut_constraints_required",
                1,
                MAX_PRODUCTION_DURATION_SECONDS * 1000 - 1,
            )
            for value in required
        )
        if required != tuple(sorted(set(checked_required))):
            raise ProductionDurationError("cut_constraints_required")

        intervals = self.forbidden_cut_intervals_milliseconds
        if type(intervals) is not tuple or len(intervals) > MAX_FORBIDDEN_CUT_INTERVALS:
            raise ProductionDurationError("cut_constraints_intervals")
        checked_intervals: list[tuple[int, int]] = []
        for interval in intervals:
            if type(interval) is not tuple or len(interval) != 2:
                raise ProductionDurationError("cut_constraints_intervals")
            start = _closed_int(
                interval[0],
                "cut_constraints_intervals",
                0,
                MAX_PRODUCTION_DURATION_SECONDS * 1000 - 1,
            )
            end = _closed_int(
                interval[1],
                "cut_constraints_intervals",
                1,
                MAX_PRODUCTION_DURATION_SECONDS * 1000,
            )
            if start >= end:
                raise ProductionDurationError("cut_constraints_intervals")
            checked_intervals.append((start, end))
        checked_tuple = tuple(checked_intervals)
        if intervals != tuple(sorted(set(checked_tuple))):
            raise ProductionDurationError("cut_constraints_intervals")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": SEGMENT_CUT_CONSTRAINTS_SCHEMA,
            "required_cut_milliseconds": list(self.required_cut_milliseconds),
            "forbidden_cut_intervals_milliseconds": [
                list(interval) for interval in self.forbidden_cut_intervals_milliseconds
            ],
        }

    @classmethod
    def from_wire(cls, value: object) -> SegmentCutConstraintsV1:
        wire = _closed_dict(
            value,
            "cut_constraints_wire",
            frozenset(
                {
                    "schema",
                    "required_cut_milliseconds",
                    "forbidden_cut_intervals_milliseconds",
                }
            ),
        )
        if wire["schema"] != SEGMENT_CUT_CONSTRAINTS_SCHEMA:
            raise ProductionDurationError("cut_constraints_schema")
        required = wire["required_cut_milliseconds"]
        intervals = wire["forbidden_cut_intervals_milliseconds"]
        if type(required) is not list or type(intervals) is not list:
            raise ProductionDurationError("cut_constraints_sequence_wire")
        converted_intervals: list[tuple[int, int]] = []
        for interval in cast(list[object], intervals):
            if type(interval) is not list or len(interval) != 2:
                raise ProductionDurationError("cut_constraints_intervals_wire")
            converted_intervals.append((cast(list[int], interval)[0], cast(list[int], interval)[1]))
        return cls(
            required_cut_milliseconds=tuple(cast(list[int], required)),
            forbidden_cut_intervals_milliseconds=tuple(converted_intervals),
        )


@dataclass(frozen=True, slots=True)
class SegmentDurationAllocationV1:
    """One exact 4..15-second allocation and its canonical sampled H3 length."""

    ordinal: int
    requested_start_seconds: int
    requested_end_seconds: int
    requested_seconds: int
    requested_milliseconds: int
    frame_count: int
    delivered_milliseconds: int
    snapped: bool

    def __post_init__(self) -> None:
        _closed_int(self.ordinal, "allocation_ordinal", 1, MAX_PRODUCTION_SEGMENTS)
        _closed_int(
            self.requested_start_seconds,
            "allocation_start_seconds",
            0,
            MAX_PRODUCTION_DURATION_SECONDS - MIN_H3_SEGMENT_SECONDS,
        )
        _closed_int(
            self.requested_end_seconds,
            "allocation_end_seconds",
            MIN_H3_SEGMENT_SECONDS,
            MAX_PRODUCTION_DURATION_SECONDS,
        )
        seconds = _closed_int(
            self.requested_seconds,
            "allocation_requested_seconds",
            MIN_H3_SEGMENT_SECONDS,
            MAX_H3_SEGMENT_SECONDS,
        )
        if self.requested_end_seconds - self.requested_start_seconds != seconds:
            raise ProductionDurationError("allocation_span")
        if self.requested_milliseconds != seconds * 1000:
            raise ProductionDurationError("allocation_requested_milliseconds")
        # CRITICAL: resolve only the already-partitioned clip value; passing the total here would
        # collapse whole-video planning back into the single-clip authority and reject 60 seconds.
        resolved = _length.resolve_milliseconds(self.requested_milliseconds)
        if (
            self.requested_milliseconds != resolved.requested_milliseconds
            or self.frame_count != resolved.frame_count
            or self.delivered_milliseconds != resolved.delivered_milliseconds
            or type(self.snapped) is not bool
            or self.snapped != resolved.snapped
        ):
            raise ProductionDurationError("allocation_canonical_tuple")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": SEGMENT_DURATION_ALLOCATION_SCHEMA,
            "ordinal": self.ordinal,
            "requested_start_seconds": self.requested_start_seconds,
            "requested_end_seconds": self.requested_end_seconds,
            "requested_seconds": self.requested_seconds,
            "requested_milliseconds": self.requested_milliseconds,
            "frame_count": self.frame_count,
            "delivered_milliseconds": self.delivered_milliseconds,
            "snapped": self.snapped,
        }

    @classmethod
    def from_wire(cls, value: object) -> SegmentDurationAllocationV1:
        keys = frozenset(
            {
                "schema",
                "ordinal",
                "requested_start_seconds",
                "requested_end_seconds",
                "requested_seconds",
                "requested_milliseconds",
                "frame_count",
                "delivered_milliseconds",
                "snapped",
            }
        )
        wire = _closed_dict(value, "allocation_wire", keys)
        if wire["schema"] != SEGMENT_DURATION_ALLOCATION_SCHEMA:
            raise ProductionDurationError("allocation_schema")
        return cls(
            ordinal=cast(int, wire["ordinal"]),
            requested_start_seconds=cast(int, wire["requested_start_seconds"]),
            requested_end_seconds=cast(int, wire["requested_end_seconds"]),
            requested_seconds=cast(int, wire["requested_seconds"]),
            requested_milliseconds=cast(int, wire["requested_milliseconds"]),
            frame_count=cast(int, wire["frame_count"]),
            delivered_milliseconds=cast(int, wire["delivered_milliseconds"]),
            snapped=cast(bool, wire["snapped"]),
        )


@dataclass(frozen=True, slots=True)
class SegmentDurationPlanV1:
    """An immutable exact-total plan with requested and sampled totals kept distinct."""

    intent: ProductionDurationIntentV1
    allocations: tuple[SegmentDurationAllocationV1, ...]
    requested_total_seconds: int
    requested_total_milliseconds: int
    sampled_total_frames: int
    sampled_total_milliseconds: int
    matched_candidate_boundaries_seconds: tuple[int, ...]

    def __post_init__(self) -> None:
        if type(self.intent) is not ProductionDurationIntentV1:
            raise ProductionDurationError("plan_intent")
        if (
            type(self.allocations) is not tuple
            or not self.allocations
            or len(self.allocations) > self.intent.max_segments
        ):
            raise ProductionDurationError("plan_allocations")
        cursor = 0
        for ordinal, allocation in enumerate(self.allocations, start=1):
            if type(allocation) is not SegmentDurationAllocationV1:
                raise ProductionDurationError("plan_allocation_type")
            # CRITICAL: revalidate the nested allocation against this intent's narrower domain;
            # validating only the global 4..15 range lets a recomposed wire contradict its plan.
            if not (
                self.intent.segment_min_seconds
                <= allocation.requested_seconds
                <= self.intent.segment_max_seconds
            ):
                raise ProductionDurationError("plan_allocation_domain")
            if allocation.ordinal != ordinal or allocation.requested_start_seconds != cursor:
                raise ProductionDurationError("plan_allocation_order")
            cursor = allocation.requested_end_seconds
        if cursor != self.intent.target_seconds:
            raise ProductionDurationError("plan_requested_total")
        if self.requested_total_seconds != self.intent.target_seconds:
            raise ProductionDurationError("plan_requested_total_seconds")
        if self.requested_total_milliseconds != self.intent.target_seconds * 1000:
            raise ProductionDurationError("plan_requested_total_milliseconds")
        if self.sampled_total_frames != sum(row.frame_count for row in self.allocations):
            raise ProductionDurationError("plan_sampled_total_frames")
        if self.sampled_total_milliseconds != sum(
            row.delivered_milliseconds for row in self.allocations
        ):
            raise ProductionDurationError("plan_sampled_total_milliseconds")
        expected_boundaries = tuple(
            row.requested_end_seconds
            for row in self.allocations[:-1]
            if row.requested_end_seconds in self.intent.candidate_boundaries_seconds
        )
        if self.matched_candidate_boundaries_seconds != expected_boundaries:
            raise ProductionDurationError("plan_matched_candidate_boundaries")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": SEGMENT_DURATION_PLAN_SCHEMA,
            "intent": self.intent.to_wire(),
            "allocations": [row.to_wire() for row in self.allocations],
            "requested_total_seconds": self.requested_total_seconds,
            "requested_total_milliseconds": self.requested_total_milliseconds,
            "sampled_total_frames": self.sampled_total_frames,
            "sampled_total_milliseconds": self.sampled_total_milliseconds,
            "matched_candidate_boundaries_seconds": list(self.matched_candidate_boundaries_seconds),
        }

    @classmethod
    def from_wire(cls, value: object) -> SegmentDurationPlanV1:
        wire = _closed_dict(
            value,
            "plan_wire",
            frozenset(
                {
                    "schema",
                    "intent",
                    "allocations",
                    "requested_total_seconds",
                    "requested_total_milliseconds",
                    "sampled_total_frames",
                    "sampled_total_milliseconds",
                    "matched_candidate_boundaries_seconds",
                }
            ),
        )
        if wire["schema"] != SEGMENT_DURATION_PLAN_SCHEMA:
            raise ProductionDurationError("plan_schema")
        allocations = wire["allocations"]
        boundaries = wire["matched_candidate_boundaries_seconds"]
        if type(allocations) is not list or type(boundaries) is not list:
            raise ProductionDurationError("plan_sequence_wire")
        return cls(
            intent=ProductionDurationIntentV1.from_wire(wire["intent"]),
            allocations=tuple(
                SegmentDurationAllocationV1.from_wire(row)
                for row in cast(list[object], allocations)
            ),
            requested_total_seconds=cast(int, wire["requested_total_seconds"]),
            requested_total_milliseconds=cast(int, wire["requested_total_milliseconds"]),
            sampled_total_frames=cast(int, wire["sampled_total_frames"]),
            sampled_total_milliseconds=cast(int, wire["sampled_total_milliseconds"]),
            matched_candidate_boundaries_seconds=tuple(cast(list[int], boundaries)),
        )


@dataclass(frozen=True, slots=True)
class SegmentDurationPlanRefusalV1:
    """A stable, serializable refusal that never substitutes a different requested total."""

    target_seconds: int
    policy: SegmentationPolicyV1
    code: DurationPlanRefusalCodeV1
    recommended_policy: SegmentationPolicyV1 = SegmentationPolicyV1.AUTO_STORYBOARD

    def __post_init__(self) -> None:
        _closed_int(
            self.target_seconds,
            "refusal_target_seconds",
            MIN_PRODUCTION_DURATION_SECONDS,
            MAX_PRODUCTION_DURATION_SECONDS,
        )
        if type(self.policy) is not SegmentationPolicyV1:
            raise ProductionDurationError("refusal_policy")
        if type(self.code) is not DurationPlanRefusalCodeV1:
            raise ProductionDurationError("refusal_code")
        # IMPORTANT: policy and refusal code are one closed decision; accepting a recomposed
        # fixed-only code under automatic mode would make downstream recovery branch incorrectly.
        allowed_codes = (
            _AUTO_REFUSAL_CODES
            if self.policy is SegmentationPolicyV1.AUTO_STORYBOARD
            else _FIXED_REFUSAL_CODES
        )
        if self.code not in allowed_codes:
            raise ProductionDurationError("refusal_policy_code")
        if self.recommended_policy is not SegmentationPolicyV1.AUTO_STORYBOARD:
            raise ProductionDurationError("refusal_recommended_policy")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": SEGMENT_DURATION_PLAN_REFUSAL_SCHEMA,
            "target_seconds": self.target_seconds,
            "policy": self.policy.value,
            "code": self.code.value,
            "recommended_policy": self.recommended_policy.value,
        }

    @classmethod
    def from_wire(cls, value: object) -> SegmentDurationPlanRefusalV1:
        wire = _closed_dict(
            value,
            "refusal_wire",
            frozenset({"schema", "target_seconds", "policy", "code", "recommended_policy"}),
        )
        if wire["schema"] != SEGMENT_DURATION_PLAN_REFUSAL_SCHEMA:
            raise ProductionDurationError("refusal_schema")
        try:
            code = DurationPlanRefusalCodeV1(wire["code"])
        except (TypeError, ValueError) as exc:
            raise ProductionDurationError("refusal_code") from exc
        return cls(
            target_seconds=cast(int, wire["target_seconds"]),
            policy=_policy(wire["policy"], "refusal_policy"),
            code=code,
            recommended_policy=_policy(wire["recommended_policy"], "refusal_recommended_policy"),
        )


def _automatic_partition(intent: ProductionDurationIntentV1) -> tuple[int, ...] | None:
    """Legacy bounded DP with its accepted boundary, clip, preference and larger-first order."""

    target = intent.target_seconds
    boundaries = frozenset(intent.candidate_boundaries_seconds)
    best: dict[tuple[int, int], tuple[int, ...]] = {(0, 0): ()}
    for elapsed in range(target + 1):
        for count in range(intent.max_segments):
            current = best.get((elapsed, count))
            if current is None:
                continue
            for seconds in range(intent.segment_min_seconds, intent.segment_max_seconds + 1):
                next_elapsed = elapsed + seconds
                if next_elapsed > target:
                    continue
                candidate = current + (seconds,)
                key = (next_elapsed, count + 1)
                incumbent = best.get(key)
                if incumbent is None or _partial_score(candidate, boundaries) > _partial_score(
                    incumbent, boundaries
                ):
                    best[key] = candidate
    completed = [parts for (elapsed, _count), parts in best.items() if elapsed == target and parts]
    if not completed:
        return None
    return max(completed, key=lambda parts: _final_score(parts, boundaries))


def _partial_score(
    parts: tuple[int, ...], boundaries: frozenset[int]
) -> tuple[int, int, tuple[int, ...]]:
    elapsed = 0
    matches = 0
    for seconds in parts:
        elapsed += seconds
        matches += int(elapsed in boundaries)
    preferred = sum(seconds in PREFERRED_AUTO_SEGMENT_SECONDS for seconds in parts)
    return matches, preferred, parts


def _final_score(
    parts: tuple[int, ...], boundaries: frozenset[int]
) -> tuple[int, int, int, tuple[int, ...]]:
    matches, preferred, stable = _partial_score(parts, boundaries)
    return matches, -len(parts), preferred, stable


def _transition_respects_constraints(
    elapsed_seconds: int,
    next_elapsed_seconds: int,
    target_seconds: int,
    constraints: SegmentCutConstraintsV1,
) -> bool:
    start_milliseconds = elapsed_seconds * 1000
    end_milliseconds = next_elapsed_seconds * 1000
    # CRITICAL: required anchors and hard spans are feasibility, not scoring. Moving this check
    # after path selection recreates false refusal when a different legal partition exists.
    if any(
        start_milliseconds < anchor < end_milliseconds
        for anchor in constraints.required_cut_milliseconds
    ):
        return False
    return next_elapsed_seconds == target_seconds or not any(
        interval_start < end_milliseconds < interval_end
        for interval_start, interval_end in constraints.forbidden_cut_intervals_milliseconds
    )


def _master_partial_score(
    parts: tuple[int, ...], boundaries: frozenset[int]
) -> tuple[int, int, tuple[int, ...]]:
    elapsed = 0
    unaligned = 0
    for seconds in parts:
        elapsed += seconds
        unaligned += int(elapsed not in boundaries)
    preferred = sum(seconds in PREFERRED_AUTO_SEGMENT_SECONDS for seconds in parts)
    return -unaligned, preferred, parts


def _master_final_score(
    parts: tuple[int, ...], boundaries: frozenset[int]
) -> tuple[int, int, int, tuple[int, ...]]:
    elapsed = 0
    unaligned = 0
    for seconds in parts[:-1]:
        elapsed += seconds
        unaligned += int(elapsed not in boundaries)
    preferred = sum(seconds in PREFERRED_AUTO_SEGMENT_SECONDS for seconds in parts)
    # The final tuple deliberately retains the accepted larger-first lexicographic tie-break.
    return -unaligned, -len(parts), preferred, parts


def _constraint_aware_automatic_partition(
    intent: ProductionDurationIntentV1, constraints: SegmentCutConstraintsV1
) -> tuple[int, ...] | None:
    """Bounded feasible-first DP using the M26 master optimization order."""

    target = intent.target_seconds
    boundaries = frozenset(intent.candidate_boundaries_seconds)
    best: dict[tuple[int, int], tuple[int, ...]] = {(0, 0): ()}
    for elapsed in range(target + 1):
        for count in range(intent.max_segments):
            current = best.get((elapsed, count))
            if current is None:
                continue
            for seconds in range(intent.segment_min_seconds, intent.segment_max_seconds + 1):
                next_elapsed = elapsed + seconds
                if next_elapsed > target or not _transition_respects_constraints(
                    elapsed, next_elapsed, target, constraints
                ):
                    continue
                candidate = current + (seconds,)
                key = (next_elapsed, count + 1)
                incumbent = best.get(key)
                if incumbent is None or _master_partial_score(
                    candidate, boundaries
                ) > _master_partial_score(incumbent, boundaries):
                    best[key] = candidate
    completed = [parts for (elapsed, _count), parts in best.items() if elapsed == target and parts]
    if not completed:
        return None
    return max(completed, key=lambda parts: _master_final_score(parts, boundaries))


def _required_cut_conflict(
    intent: ProductionDurationIntentV1, constraints: SegmentCutConstraintsV1
) -> DurationPlanRefusalCodeV1 | None:
    target_milliseconds = intent.target_seconds * 1000
    for anchor in constraints.required_cut_milliseconds:
        if anchor >= target_milliseconds:
            return (
                DurationPlanRefusalCodeV1.NO_FEASIBLE_PARTITION
                if intent.policy is SegmentationPolicyV1.AUTO_STORYBOARD
                else DurationPlanRefusalCodeV1.FIXED_CUT_CONFLICT
            )
        if anchor % 1000:
            return DurationPlanRefusalCodeV1.REQUIRED_CUT_UNREPRESENTABLE
    return None


def _partition_respects_constraints(
    parts: tuple[int, ...], intent: ProductionDurationIntentV1, constraints: SegmentCutConstraintsV1
) -> bool:
    elapsed = 0
    for seconds in parts:
        next_elapsed = elapsed + seconds
        if not _transition_respects_constraints(
            elapsed, next_elapsed, intent.target_seconds, constraints
        ):
            return False
        elapsed = next_elapsed
    return True


def _fixed_partition(
    intent: ProductionDurationIntentV1,
) -> tuple[tuple[int, ...] | None, DurationPlanRefusalCodeV1 | None]:
    fixed_seconds = _FIXED_SECONDS[intent.policy]
    quotient, remainder = divmod(intent.target_seconds, fixed_seconds)
    if quotient and not intent.segment_min_seconds <= fixed_seconds <= intent.segment_max_seconds:
        return None, DurationPlanRefusalCodeV1.FIXED_SEGMENT_OUT_OF_DOMAIN
    parts = (fixed_seconds,) * quotient
    if remainder:
        if not intent.segment_min_seconds <= remainder <= intent.segment_max_seconds:
            return None, DurationPlanRefusalCodeV1.FIXED_REMAINDER_OUT_OF_DOMAIN
        parts += (remainder,)
    if not parts:
        return None, DurationPlanRefusalCodeV1.NO_EXACT_PARTITION
    if len(parts) > intent.max_segments:
        return None, DurationPlanRefusalCodeV1.SEGMENT_LIMIT_EXCEEDED
    return parts, None


def _refusal(
    intent: ProductionDurationIntentV1, code: DurationPlanRefusalCodeV1
) -> SegmentDurationPlanRefusalV1:
    return SegmentDurationPlanRefusalV1(
        target_seconds=intent.target_seconds,
        policy=intent.policy,
        code=code,
    )


def solve_segment_duration_plan(
    intent: ProductionDurationIntentV1,
    *,
    constraints: SegmentCutConstraintsV1 | None = None,
) -> SegmentDurationPlanV1 | SegmentDurationPlanRefusalV1:
    """Return one exact requested-total plan or one typed refusal, with no side effects."""

    if type(intent) is not ProductionDurationIntentV1:
        raise ProductionDurationError("solver_intent")
    if constraints is not None and type(constraints) is not SegmentCutConstraintsV1:
        raise ProductionDurationError("solver_constraints")
    if constraints is not None:
        conflict = _required_cut_conflict(intent, constraints)
        if conflict is not None:
            return _refusal(intent, conflict)
    if intent.policy is SegmentationPolicyV1.AUTO_STORYBOARD:
        # IMPORTANT: None is the legacy v1 numeric call. An explicit constraints object opts into
        # feasible-first M26 scoring, including when its two bounded collections are empty.
        parts = (
            _automatic_partition(intent)
            if constraints is None
            else _constraint_aware_automatic_partition(intent, constraints)
        )
        if parts is None:
            return _refusal(
                intent,
                DurationPlanRefusalCodeV1.NO_EXACT_PARTITION
                if constraints is None
                else DurationPlanRefusalCodeV1.NO_FEASIBLE_PARTITION,
            )
    else:
        parts, refusal_code = _fixed_partition(intent)
        if refusal_code is not None:
            return _refusal(intent, refusal_code)
        if parts is None:
            raise ProductionDurationError("fixed_partition_internal")
        if constraints is not None and not _partition_respects_constraints(
            parts, intent, constraints
        ):
            return _refusal(intent, DurationPlanRefusalCodeV1.FIXED_CUT_CONFLICT)

    allocations: list[SegmentDurationAllocationV1] = []
    cursor = 0
    for ordinal, seconds in enumerate(parts, start=1):
        requested_milliseconds = seconds * 1000
        resolved = _length.resolve_milliseconds(requested_milliseconds)
        allocation = SegmentDurationAllocationV1(
            ordinal=ordinal,
            requested_start_seconds=cursor,
            requested_end_seconds=cursor + seconds,
            requested_seconds=seconds,
            requested_milliseconds=requested_milliseconds,
            frame_count=resolved.frame_count,
            delivered_milliseconds=resolved.delivered_milliseconds,
            snapped=resolved.snapped,
        )
        allocations.append(allocation)
        cursor += seconds
    allocation_tuple = tuple(allocations)
    matched_boundaries = tuple(
        row.requested_end_seconds
        for row in allocation_tuple[:-1]
        if row.requested_end_seconds in intent.candidate_boundaries_seconds
    )
    return SegmentDurationPlanV1(
        intent=intent,
        allocations=allocation_tuple,
        requested_total_seconds=intent.target_seconds,
        requested_total_milliseconds=intent.target_seconds * 1000,
        sampled_total_frames=sum(row.frame_count for row in allocation_tuple),
        sampled_total_milliseconds=sum(row.delivered_milliseconds for row in allocation_tuple),
        matched_candidate_boundaries_seconds=matched_boundaries,
    )


__all__ = [
    "DEFAULT_PRODUCTION_DURATION_SECONDS",
    "MAX_CANDIDATE_BOUNDARIES",
    "MAX_FORBIDDEN_CUT_INTERVALS",
    "MAX_H3_SEGMENT_SECONDS",
    "MAX_PRODUCTION_DURATION_SECONDS",
    "MAX_PRODUCTION_SEGMENTS",
    "MAX_REQUIRED_CUTS",
    "MIN_H3_SEGMENT_SECONDS",
    "MIN_PRODUCTION_DURATION_SECONDS",
    "PREFERRED_AUTO_SEGMENT_SECONDS",
    "PRODUCTION_DURATION_INTENT_SCHEMA",
    "SEGMENT_CUT_CONSTRAINTS_SCHEMA",
    "SEGMENT_DURATION_ALLOCATION_SCHEMA",
    "SEGMENT_DURATION_PLAN_REFUSAL_SCHEMA",
    "SEGMENT_DURATION_PLAN_SCHEMA",
    "DurationPlanRefusalCodeV1",
    "ProductionDurationError",
    "ProductionDurationIntentV1",
    "SegmentationPolicyV1",
    "SegmentCutConstraintsV1",
    "SegmentDurationAllocationV1",
    "SegmentDurationPlanRefusalV1",
    "SegmentDurationPlanV1",
    "solve_segment_duration_plan",
]
