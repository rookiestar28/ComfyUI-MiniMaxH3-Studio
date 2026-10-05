"""The frozen evaluation: cases, goldens, reports and the bundle that carries them.

This layer is where M14-01's terminal disposition is enforced -- the frozen cluster counts, support
totals, interval method and bundle fingerprint are checked here, not asserted in a test. A bundle
that does not match the frozen design is rejected at construction.

`wilson_interval` is computed in `Decimal` with an explicit rounding mode so the interval is
reproducible across platforms, which a float would not be.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from .semantic_graph_diff import SemanticComparison, compare_semantic_graphs
from .semantic_graph_model import SemanticPromptGraph, render_semantic_prompt
from .semantic_graph_primitives import (
    FROZEN_EVALUATION_BUNDLE_FINGERPRINT,
    FROZEN_GUIDE_CLUSTER_COUNT,
    FROZEN_GUIDE_SUPPORT,
    FROZEN_INTERVAL_METHOD,
    FROZEN_INTERVAL_Z,
    FROZEN_NON_P0_CLUSTER_COUNT,
    FROZEN_NON_P0_NEGATIVE_SUPPORT,
    FROZEN_NON_P0_POSITIVE_SUPPORT,
    FROZEN_NON_P0_SUPPORT,
    FROZEN_P0_CLUSTER_COUNT,
    FROZEN_P0_SUPPORT,
    FROZEN_POINT_TARGET,
    M14_01_TERMINAL_DISPOSITION_SHA256,
    OFFICIAL_BEHAVIOR_PROFILE_SCHEMA,
    SEMANTIC_EVALUATION_SCHEMA,
    BehaviorProfileDisposition,
    EvaluationPartition,
    SemanticDiffOutcome,
    SemanticGraphError,
    SemanticSourceKind,
    _enum,
    _fingerprint,
    _identifier,
)


@dataclass(frozen=True, slots=True)
class FrozenEvaluationDesign:
    guide_cluster_count: int = FROZEN_GUIDE_CLUSTER_COUNT
    guide_support: int = FROZEN_GUIDE_SUPPORT
    p0_cluster_count: int = FROZEN_P0_CLUSTER_COUNT
    p0_support: int = FROZEN_P0_SUPPORT
    non_p0_cluster_count: int = FROZEN_NON_P0_CLUSTER_COUNT
    non_p0_support: int = FROZEN_NON_P0_SUPPORT
    non_p0_positive_support: int = FROZEN_NON_P0_POSITIVE_SUPPORT
    non_p0_negative_support: int = FROZEN_NON_P0_NEGATIVE_SUPPORT
    interval_method: str = FROZEN_INTERVAL_METHOD
    interval_z: str = FROZEN_INTERVAL_Z
    point_target: str = FROZEN_POINT_TARGET

    def __post_init__(self) -> None:
        count_values = (
            self.guide_cluster_count,
            self.guide_support,
            self.p0_cluster_count,
            self.p0_support,
            self.non_p0_cluster_count,
            self.non_p0_support,
            self.non_p0_positive_support,
            self.non_p0_negative_support,
        )
        if not all(type(value) is int for value in count_values) or not all(
            type(value) is str
            for value in (self.interval_method, self.interval_z, self.point_target)
        ):
            raise SemanticGraphError("evaluation design values require exact primitive types")
        expected = (
            FROZEN_GUIDE_CLUSTER_COUNT,
            FROZEN_GUIDE_SUPPORT,
            FROZEN_P0_CLUSTER_COUNT,
            FROZEN_P0_SUPPORT,
            FROZEN_NON_P0_CLUSTER_COUNT,
            FROZEN_NON_P0_SUPPORT,
            FROZEN_NON_P0_POSITIVE_SUPPORT,
            FROZEN_NON_P0_NEGATIVE_SUPPORT,
            FROZEN_INTERVAL_METHOD,
            FROZEN_INTERVAL_Z,
            FROZEN_POINT_TARGET,
        )
        if (
            self.guide_cluster_count,
            self.guide_support,
            self.p0_cluster_count,
            self.p0_support,
            self.non_p0_cluster_count,
            self.non_p0_support,
            self.non_p0_positive_support,
            self.non_p0_negative_support,
            self.interval_method,
            self.interval_z,
            self.point_target,
        ) != expected:
            raise SemanticGraphError("evaluation design differs from the preregistered support")

    def to_wire(self) -> dict[str, object]:
        return {
            "guide_cluster_count": self.guide_cluster_count,
            "guide_support": self.guide_support,
            "p0_cluster_count": self.p0_cluster_count,
            "p0_support": self.p0_support,
            "non_p0_cluster_count": self.non_p0_cluster_count,
            "non_p0_support": self.non_p0_support,
            "non_p0_positive_support": self.non_p0_positive_support,
            "non_p0_negative_support": self.non_p0_negative_support,
            "interval_method": self.interval_method,
            "interval_z": self.interval_z,
            "point_target": self.point_target,
        }


@dataclass(frozen=True, slots=True)
class GuideGolden:
    case_id: str
    source_cluster: str
    graph: SemanticPromptGraph

    def __post_init__(self) -> None:
        _identifier(self.case_id, "guide case_id")
        _identifier(self.source_cluster, "guide source_cluster")
        if type(self.graph) is not SemanticPromptGraph:
            raise SemanticGraphError("guide golden graph is invalid")
        if self.graph.provenance.source_kind is not SemanticSourceKind.PUBLIC_GUIDE:
            raise SemanticGraphError("guide golden must remain public_guide evidence")
        if render_semantic_prompt(self.graph) != self.graph.source_text:
            raise SemanticGraphError("guide golden does not round-trip")

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "source_cluster": self.source_cluster,
            "graph": self.graph.to_wire(),
            "round_trip": True,
        }


@dataclass(frozen=True, slots=True)
class ComparatorEvaluationCase:
    case_id: str
    source_cluster: str
    seed: int
    partition: EvaluationPartition
    expected_outcome: SemanticDiffOutcome
    expected_material_difference: bool
    local_graph: SemanticPromptGraph
    reference_graph: SemanticPromptGraph
    comparison: SemanticComparison

    def __post_init__(self) -> None:
        _identifier(self.case_id, "evaluation case_id")
        _identifier(self.source_cluster, "evaluation source_cluster")
        if type(self.seed) is not int or not 0 <= self.seed <= 2_147_483_647:
            raise SemanticGraphError("evaluation seed must be a bounded integer")
        _enum(self.partition, EvaluationPartition, "evaluation partition")
        _enum(self.expected_outcome, SemanticDiffOutcome, "evaluation expected outcome")
        if type(self.expected_material_difference) is not bool:
            raise SemanticGraphError("evaluation material expectation must be bool")
        if (
            type(self.local_graph) is not SemanticPromptGraph
            or type(self.reference_graph) is not SemanticPromptGraph
        ):
            raise SemanticGraphError("evaluation graphs are invalid")
        if (
            self.local_graph.provenance.source_kind is not SemanticSourceKind.LOCAL
            or self.reference_graph.provenance.source_kind is not SemanticSourceKind.LOCAL
        ):
            raise SemanticGraphError("comparison cases require local-only support evidence")
        # CRITICAL: check exact type before equality; duck-typed equality can forge derived data.
        if type(self.comparison) is not SemanticComparison:
            raise SemanticGraphError("evaluation comparison is invalid")
        expected = compare_semantic_graphs(self.local_graph, self.reference_graph)
        if self.comparison != expected:
            raise SemanticGraphError("evaluation comparison is not comparator-derived")
        if self.comparison.primary_outcome is not self.expected_outcome:
            raise SemanticGraphError("evaluation outcome differs from the frozen expectation")
        if self.comparison.material_difference is not self.expected_material_difference:
            raise SemanticGraphError("evaluation material decision differs from expectation")
        if self.partition is EvaluationPartition.P0 and self.expected_outcome not in {
            SemanticDiffOutcome.HARD_MUTATION,
            SemanticDiffOutcome.ROLE,
            SemanticDiffOutcome.OWNERSHIP,
            SemanticDiffOutcome.ORDER,
            SemanticDiffOutcome.TEMPORAL,
            SemanticDiffOutcome.CONTRADICTION,
        }:
            raise SemanticGraphError("P0 case uses a non-P0 expected outcome")
        if self.partition is EvaluationPartition.NON_P0 and self.expected_outcome in {
            SemanticDiffOutcome.HARD_MUTATION,
            SemanticDiffOutcome.ROLE,
            SemanticDiffOutcome.OWNERSHIP,
            SemanticDiffOutcome.ORDER,
            SemanticDiffOutcome.TEMPORAL,
            SemanticDiffOutcome.UNSCORABLE,
        }:
            raise SemanticGraphError("non-P0 holdout contains a P0/unscorable outcome")

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "source_cluster": self.source_cluster,
            "seed": self.seed,
            "partition": self.partition.value,
            "expected_outcome": self.expected_outcome.value,
            "expected_material_difference": self.expected_material_difference,
            "local_graph": self.local_graph.to_wire(),
            "reference_graph": self.reference_graph.to_wire(),
            "comparison": self.comparison.to_wire(),
        }


def _decimal_token(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN), "f")


def wilson_interval(successes: int, total: int) -> tuple[str, str]:
    """Return the preregistered two-sided Wilson 95% interval."""

    if type(successes) is not int or type(total) is not int or not 0 <= successes <= total:
        raise SemanticGraphError("Wilson counts must be bounded integers")
    if total <= 0:
        raise SemanticGraphError("Wilson total must be positive")
    with localcontext() as context:
        context.prec = 50
        success = Decimal(successes)
        count = Decimal(total)
        z = Decimal(FROZEN_INTERVAL_Z)
        proportion = success / count
        z_squared = z * z
        denominator = Decimal(1) + z_squared / count
        center = (proportion + z_squared / (Decimal(2) * count)) / denominator
        margin = (
            z
            * (
                (proportion * (Decimal(1) - proportion) / count)
                + z_squared / (Decimal(4) * count * count)
            ).sqrt()
            / denominator
        )
        lower = max(Decimal(0), center - margin)
        upper = min(Decimal(1), center + margin)
        return _decimal_token(lower), _decimal_token(upper)


@dataclass(frozen=True, slots=True, init=False)
class ComparatorEvaluationReport:
    guide_passed: int
    guide_total: int
    p0_detected: int
    p0_total: int
    p0_outcome_counts: tuple[tuple[str, int], ...]
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: str
    recall: str
    precision_interval: tuple[str, str]
    recall_interval: tuple[str, str]
    qualified: bool

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        # CRITICAL: derived metrics must stay evaluator-owned; public init can forge qualification.
        raise SemanticGraphError("evaluation report construction is evaluator-owned")

    def __post_init__(self) -> None:
        for value, field in (
            (self.guide_passed, "guide_passed"),
            (self.guide_total, "guide_total"),
            (self.p0_detected, "p0_detected"),
            (self.p0_total, "p0_total"),
            (self.true_positive, "true_positive"),
            (self.false_positive, "false_positive"),
            (self.false_negative, "false_negative"),
            (self.true_negative, "true_negative"),
        ):
            if type(value) is not int or not 0 <= value <= FROZEN_NON_P0_SUPPORT:
                raise SemanticGraphError(f"{field} must be a bounded count")
        if type(self.p0_outcome_counts) is not tuple:
            raise SemanticGraphError("P0 outcome counts must be a tuple")
        if tuple(sorted(self.p0_outcome_counts)) != self.p0_outcome_counts:
            raise SemanticGraphError("P0 outcome counts must be unique and sorted")
        for name, count in self.p0_outcome_counts:
            SemanticDiffOutcome(name)
            if type(count) is not int or count <= 0:
                raise SemanticGraphError("P0 outcome count must be positive")
        for token in (self.precision, self.recall, *self.precision_interval, *self.recall_interval):
            if re.fullmatch(r"(?:0|1)\.[0-9]{8}", token) is None:
                raise SemanticGraphError("evaluation metric token is invalid")
        if type(self.qualified) is not bool:
            raise SemanticGraphError("evaluation qualification must be bool")

    def to_wire(self) -> dict[str, object]:
        return {
            "guide_passed": self.guide_passed,
            "guide_total": self.guide_total,
            "p0_detected": self.p0_detected,
            "p0_total": self.p0_total,
            "p0_outcome_counts": {key: value for key, value in self.p0_outcome_counts},
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "true_negative": self.true_negative,
            "precision": self.precision,
            "recall": self.recall,
            "precision_interval": list(self.precision_interval),
            "recall_interval": list(self.recall_interval),
            "qualified": self.qualified,
        }


def _derive_comparator_evaluation_report(
    guide_passed: int,
    guide_total: int,
    p0_detected: int,
    p0_total: int,
    p0_outcome_counts: tuple[tuple[str, int], ...],
    true_positive: int,
    false_positive: int,
    false_negative: int,
    true_negative: int,
    precision: str,
    recall: str,
    precision_interval: tuple[str, str],
    recall_interval: tuple[str, str],
    qualified: bool,
) -> ComparatorEvaluationReport:
    report = object.__new__(ComparatorEvaluationReport)
    for field, value in (
        ("guide_passed", guide_passed),
        ("guide_total", guide_total),
        ("p0_detected", p0_detected),
        ("p0_total", p0_total),
        ("p0_outcome_counts", p0_outcome_counts),
        ("true_positive", true_positive),
        ("false_positive", false_positive),
        ("false_negative", false_negative),
        ("true_negative", true_negative),
        ("precision", precision),
        ("recall", recall),
        ("precision_interval", precision_interval),
        ("recall_interval", recall_interval),
        ("qualified", qualified),
    ):
        object.__setattr__(report, field, value)
    report.__post_init__()
    return report


@dataclass(frozen=True, slots=True, init=False)
class OfficialBehaviorProfile:
    disposition: BehaviorProfileDisposition
    terminal_disposition_sha256: str
    local_qualification_fingerprint: str
    oracle_corpus_fingerprint: None = None
    oracle_comparison_count: None = None
    oracle_agreement: None = None
    oracle_variability: None = None
    oracle_latency: None = None
    oracle_provider_failures: None = None
    observed_official_rules: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = (
        "official.agreement_missing",
        "official.capture_missing",
        "official.relations_missing",
        "official.variability_missing",
    )
    limitations: tuple[str, ...] = (
        "no_official_agreement_claim",
        "structural_and_local_only",
    )

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        # CRITICAL: behavior profiles must stay bundle-owned; public init can forge evidence joins.
        raise SemanticGraphError("official behavior profile construction is bundle-owned")

    def __post_init__(self) -> None:
        if self.disposition is not BehaviorProfileDisposition.UNAVAILABLE:
            raise SemanticGraphError("official behavior profile must remain UNAVAILABLE")
        if self.terminal_disposition_sha256 != M14_01_TERMINAL_DISPOSITION_SHA256:
            raise SemanticGraphError("official behavior profile terminal evidence differs")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.local_qualification_fingerprint) is None:
            raise SemanticGraphError("local qualification fingerprint is invalid")
        oracle_fields = (
            self.oracle_corpus_fingerprint,
            self.oracle_comparison_count,
            self.oracle_agreement,
            self.oracle_variability,
            self.oracle_latency,
            self.oracle_provider_failures,
        )
        if any(value is not None for value in oracle_fields):
            raise SemanticGraphError("oracle comparison fields must remain missing")
        if self.observed_official_rules or self.hypotheses:
            raise SemanticGraphError("unavailable profile cannot claim official rules/hypotheses")
        expected_unknowns = (
            "official.agreement_missing",
            "official.capture_missing",
            "official.relations_missing",
            "official.variability_missing",
        )
        if self.unknowns != expected_unknowns:
            raise SemanticGraphError("official unknown inventory differs")
        if self.limitations != (
            "no_official_agreement_claim",
            "structural_and_local_only",
        ):
            raise SemanticGraphError("official limitation inventory differs")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": OFFICIAL_BEHAVIOR_PROFILE_SCHEMA,
            "disposition": self.disposition.value,
            "terminal_disposition_sha256": self.terminal_disposition_sha256,
            "local_qualification_fingerprint": self.local_qualification_fingerprint,
            "oracle_corpus_fingerprint": self.oracle_corpus_fingerprint,
            "oracle_comparison_count": self.oracle_comparison_count,
            "oracle_agreement": self.oracle_agreement,
            "oracle_variability": self.oracle_variability,
            "oracle_latency": self.oracle_latency,
            "oracle_provider_failures": self.oracle_provider_failures,
            "observed_official_rules": list(self.observed_official_rules),
            "hypotheses": list(self.hypotheses),
            "unknowns": list(self.unknowns),
            "limitations": list(self.limitations),
        }


def _derive_official_behavior_profile(
    local_qualification_fingerprint: str,
) -> OfficialBehaviorProfile:
    profile = object.__new__(OfficialBehaviorProfile)
    object.__setattr__(profile, "disposition", BehaviorProfileDisposition.UNAVAILABLE)
    object.__setattr__(profile, "terminal_disposition_sha256", M14_01_TERMINAL_DISPOSITION_SHA256)
    object.__setattr__(profile, "local_qualification_fingerprint", local_qualification_fingerprint)
    for field in (
        "oracle_corpus_fingerprint",
        "oracle_comparison_count",
        "oracle_agreement",
        "oracle_variability",
        "oracle_latency",
        "oracle_provider_failures",
    ):
        object.__setattr__(profile, field, None)
    object.__setattr__(profile, "observed_official_rules", ())
    object.__setattr__(profile, "hypotheses", ())
    object.__setattr__(
        profile,
        "unknowns",
        (
            "official.agreement_missing",
            "official.capture_missing",
            "official.relations_missing",
            "official.variability_missing",
        ),
    )
    object.__setattr__(
        profile,
        "limitations",
        (
            "no_official_agreement_claim",
            "structural_and_local_only",
        ),
    )
    profile.__post_init__()
    return profile


def _derive_evaluation_report(
    guide_goldens: tuple[GuideGolden, ...],
    p0_cases: tuple[ComparatorEvaluationCase, ...],
    non_p0_cases: tuple[ComparatorEvaluationCase, ...],
) -> ComparatorEvaluationReport:
    guide_passed = sum(
        render_semantic_prompt(item.graph) == item.graph.source_text for item in guide_goldens
    )
    p0_detected = sum(item.comparison.primary_outcome is item.expected_outcome for item in p0_cases)
    counts: dict[str, int] = {}
    for item in p0_cases:
        counts[item.expected_outcome.value] = counts.get(item.expected_outcome.value, 0) + 1
    true_positive = false_positive = false_negative = true_negative = 0
    for item in non_p0_cases:
        actual = item.comparison.material_difference
        expected = item.expected_material_difference
        if actual and expected:
            true_positive += 1
        elif actual and not expected:
            false_positive += 1
        elif not actual and expected:
            false_negative += 1
        else:
            true_negative += 1
    predicted_positive = true_positive + false_positive
    actual_positive = true_positive + false_negative
    if predicted_positive == 0 or actual_positive == 0:
        raise SemanticGraphError("evaluation precision/recall support must be positive")
    precision_decimal = Decimal(true_positive) / Decimal(predicted_positive)
    recall_decimal = Decimal(true_positive) / Decimal(actual_positive)
    precision = _decimal_token(precision_decimal)
    recall = _decimal_token(recall_decimal)
    qualified = (
        guide_passed == FROZEN_GUIDE_SUPPORT
        and p0_detected == FROZEN_P0_SUPPORT
        and precision_decimal >= Decimal(FROZEN_POINT_TARGET)
        and recall_decimal >= Decimal(FROZEN_POINT_TARGET)
    )
    return _derive_comparator_evaluation_report(
        guide_passed,
        len(guide_goldens),
        p0_detected,
        len(p0_cases),
        tuple(sorted(counts.items())),
        true_positive,
        false_positive,
        false_negative,
        true_negative,
        precision,
        recall,
        wilson_interval(true_positive, predicted_positive),
        wilson_interval(true_positive, actual_positive),
        qualified,
    )


def _bundle_payload(
    design: FrozenEvaluationDesign,
    guide_goldens: tuple[GuideGolden, ...],
    p0_cases: tuple[ComparatorEvaluationCase, ...],
    non_p0_cases: tuple[ComparatorEvaluationCase, ...],
    report: ComparatorEvaluationReport,
    official_profile: OfficialBehaviorProfile,
) -> dict[str, object]:
    return {
        "schema": SEMANTIC_EVALUATION_SCHEMA,
        "design": design.to_wire(),
        "guide_goldens": [item.to_wire() for item in guide_goldens],
        "p0_cases": [item.to_wire() for item in p0_cases],
        "non_p0_cases": [item.to_wire() for item in non_p0_cases],
        "report": report.to_wire(),
        "official_profile": official_profile.to_wire(),
    }


@dataclass(frozen=True, slots=True)
class SemanticEvaluationBundle:
    design: FrozenEvaluationDesign
    guide_goldens: tuple[GuideGolden, ...]
    p0_cases: tuple[ComparatorEvaluationCase, ...]
    non_p0_cases: tuple[ComparatorEvaluationCase, ...]
    report: ComparatorEvaluationReport
    official_profile: OfficialBehaviorProfile
    bundle_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.design) is not FrozenEvaluationDesign:
            raise SemanticGraphError("evaluation bundle design is invalid")
        if type(self.guide_goldens) is not tuple or len(self.guide_goldens) != FROZEN_GUIDE_SUPPORT:
            raise SemanticGraphError("guide support differs from the preregistered count")
        if type(self.p0_cases) is not tuple or len(self.p0_cases) != FROZEN_P0_SUPPORT:
            raise SemanticGraphError("P0 support differs from the preregistered count")
        if type(self.non_p0_cases) is not tuple or len(self.non_p0_cases) != FROZEN_NON_P0_SUPPORT:
            raise SemanticGraphError("non-P0 support differs from the preregistered count")
        if not all(type(item) is GuideGolden for item in self.guide_goldens):
            raise SemanticGraphError("guide support contains an invalid value")
        if not all(type(item) is ComparatorEvaluationCase for item in self.p0_cases):
            raise SemanticGraphError("P0 support contains an invalid value")
        if not all(type(item) is ComparatorEvaluationCase for item in self.non_p0_cases):
            raise SemanticGraphError("non-P0 support contains an invalid value")
        # CRITICAL: exact derived types must precede equality and to_wire dispatch.
        if (
            type(self.report) is not ComparatorEvaluationReport
            or type(self.official_profile) is not OfficialBehaviorProfile
        ):
            raise SemanticGraphError("evaluation bundle report or profile is invalid")
        if (
            type(self.bundle_fingerprint) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.bundle_fingerprint) is None
        ):
            raise SemanticGraphError("evaluation bundle fingerprint is invalid")
        if any(item.partition is not EvaluationPartition.P0 for item in self.p0_cases):
            raise SemanticGraphError("P0 support contains another partition")
        if any(item.partition is not EvaluationPartition.NON_P0 for item in self.non_p0_cases):
            raise SemanticGraphError("non-P0 support contains another partition")
        all_ids = [item.case_id for item in self.guide_goldens] + [
            item.case_id for item in self.p0_cases + self.non_p0_cases
        ]
        if len(set(all_ids)) != len(all_ids):
            raise SemanticGraphError("evaluation case IDs must be globally unique")
        guide_clusters = {item.source_cluster for item in self.guide_goldens}
        p0_clusters = {item.source_cluster for item in self.p0_cases}
        non_p0_clusters = {item.source_cluster for item in self.non_p0_cases}
        if len(guide_clusters) != FROZEN_GUIDE_CLUSTER_COUNT:
            raise SemanticGraphError("guide source-cluster count differs")
        if len(p0_clusters) != FROZEN_P0_CLUSTER_COUNT:
            raise SemanticGraphError("P0 source-cluster count differs")
        if len(non_p0_clusters) != FROZEN_NON_P0_CLUSTER_COUNT:
            raise SemanticGraphError("non-P0 source-cluster count differs")
        for values, support in (
            (self.guide_goldens, 3),
            (self.p0_cases, 3),
            (self.non_p0_cases, 4),
        ):
            cluster_support: dict[str, int] = {}
            for item in values:
                cluster_support[item.source_cluster] = (
                    cluster_support.get(item.source_cluster, 0) + 1
                )
            if set(cluster_support.values()) != {support}:
                raise SemanticGraphError("evaluation per-cluster support differs")
        p0_outcomes: dict[str, int] = {}
        for item in self.p0_cases:
            outcome = item.expected_outcome.value
            p0_outcomes[outcome] = p0_outcomes.get(outcome, 0) + 1
        if tuple(sorted(p0_outcomes.items())) != (
            ("contradiction", 2),
            ("hard_mutation", 6),
            ("order", 4),
            ("ownership", 4),
            ("role", 4),
            ("temporal", 4),
        ):
            raise SemanticGraphError("P0 outcome distribution differs from preregistration")
        holdout_balance: dict[str, list[int]] = {}
        for item in self.non_p0_cases:
            counts = holdout_balance.setdefault(item.source_cluster, [0, 0])
            counts[0 if item.expected_material_difference else 1] += 1
        if any(counts != [2, 2] for counts in holdout_balance.values()):
            raise SemanticGraphError("non-P0 holdout cluster balance differs")
        for values, expected_count in (
            (self.p0_cases, FROZEN_P0_CLUSTER_COUNT),
            (self.non_p0_cases, FROZEN_NON_P0_CLUSTER_COUNT),
        ):
            references_by_cluster: dict[str, set[str]] = {}
            for item in values:
                references_by_cluster.setdefault(item.source_cluster, set()).add(
                    item.reference_graph.provenance.source_fingerprint
                )
            if (
                any(len(fingerprints) != 1 for fingerprints in references_by_cluster.values())
                or len(
                    {next(iter(fingerprints)) for fingerprints in references_by_cluster.values()}
                )
                != expected_count
            ):
                raise SemanticGraphError("evaluation source clusters are not source-distinct")
        if tuple(sorted(item.seed for item in self.p0_cases)) != tuple(range(1001, 1025)):
            raise SemanticGraphError("P0 seed inventory differs from preregistration")
        if tuple(sorted(item.seed for item in self.non_p0_cases)) != tuple(range(2001, 2041)):
            raise SemanticGraphError("non-P0 seed inventory differs from preregistration")
        if not (
            guide_clusters.isdisjoint(p0_clusters)
            and guide_clusters.isdisjoint(non_p0_clusters)
            and p0_clusters.isdisjoint(non_p0_clusters)
        ):
            raise SemanticGraphError("evaluation source clusters must be partition-disjoint")
        positives = sum(item.expected_material_difference for item in self.non_p0_cases)
        if positives != FROZEN_NON_P0_POSITIVE_SUPPORT:
            raise SemanticGraphError("non-P0 positive support differs")
        if len(self.non_p0_cases) - positives != FROZEN_NON_P0_NEGATIVE_SUPPORT:
            raise SemanticGraphError("non-P0 negative support differs")
        expected_report = _derive_evaluation_report(
            self.guide_goldens, self.p0_cases, self.non_p0_cases
        )
        if self.report != expected_report:
            raise SemanticGraphError("evaluation report is not support-derived")
        report_fingerprint = _fingerprint(self.report.to_wire())
        expected_profile = _derive_official_behavior_profile(report_fingerprint)
        if self.official_profile != expected_profile:
            raise SemanticGraphError("official behavior profile is not report-derived")
        payload = _bundle_payload(
            self.design,
            self.guide_goldens,
            self.p0_cases,
            self.non_p0_cases,
            self.report,
            self.official_profile,
        )
        if self.bundle_fingerprint != _fingerprint(payload):
            raise SemanticGraphError("evaluation bundle fingerprint differs")
        if self.bundle_fingerprint != FROZEN_EVALUATION_BUNDLE_FINGERPRINT:
            raise SemanticGraphError("evaluation bundle differs from the frozen fingerprint")

    def to_wire(self) -> dict[str, object]:
        payload = _bundle_payload(
            self.design,
            self.guide_goldens,
            self.p0_cases,
            self.non_p0_cases,
            self.report,
            self.official_profile,
        )
        payload["bundle_fingerprint"] = self.bundle_fingerprint
        return payload


def build_semantic_evaluation_bundle(
    guide_goldens: tuple[GuideGolden, ...],
    p0_cases: tuple[ComparatorEvaluationCase, ...],
    non_p0_cases: tuple[ComparatorEvaluationCase, ...],
) -> SemanticEvaluationBundle:
    design = FrozenEvaluationDesign()
    report = _derive_evaluation_report(guide_goldens, p0_cases, non_p0_cases)
    profile = _derive_official_behavior_profile(_fingerprint(report.to_wire()))
    payload = _bundle_payload(design, guide_goldens, p0_cases, non_p0_cases, report, profile)
    return SemanticEvaluationBundle(
        design,
        guide_goldens,
        p0_cases,
        non_p0_cases,
        report,
        profile,
        _fingerprint(payload),
    )


def build_comparator_evaluation_case(
    case_id: str,
    source_cluster: str,
    seed: int,
    partition: EvaluationPartition,
    expected_outcome: SemanticDiffOutcome,
    expected_material_difference: bool,
    local_graph: SemanticPromptGraph,
    reference_graph: SemanticPromptGraph,
) -> ComparatorEvaluationCase:
    comparison = compare_semantic_graphs(local_graph, reference_graph)
    return ComparatorEvaluationCase(
        case_id,
        source_cluster,
        seed,
        partition,
        expected_outcome,
        expected_material_difference,
        local_graph,
        reference_graph,
        comparison,
    )
