from __future__ import annotations

import ast
import copy
import json
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from enum import Enum
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator

from comfyui_h3_context.core import fidelity_scorecard as scorecard
from comfyui_h3_context.core import fixed_h3_generation as fixed_h3
from comfyui_h3_context.core import human_review, training_authorization, visual_benchmark
from comfyui_h3_context.core import perturbation_evaluation as perturbation
from comfyui_h3_context.core import semantic_graph_comparator as semantic
from comfyui_h3_context.core.fidelity_scorecard import (
    FROZEN_FIDELITY_SCORECARD_FINGERPRINT,
    ClaimDisposition,
    ClaimLevel,
    EvidenceDisposition,
    FidelityLane,
    FidelityScorecard,
    FidelityScorecardError,
    build_fidelity_scorecard,
    decode_fidelity_scorecard_json,
    validate_fidelity_scorecard_wire,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m14_08_fidelity_scorecard.json"
SCHEMA = ROOT / "governance" / "contracts" / "fidelity_scorecard_v1.schema.json"
GENERATOR = ROOT / "scripts" / "m14_08_fidelity_scorecard_fixture.py"


class _EqualityTrap(str):
    equality_calls = 0

    def __eq__(self, other: object) -> bool:
        del other
        type(self).equality_calls += 1
        raise AssertionError("attacker equality was invoked")

    def __ne__(self, other: object) -> bool:
        del other
        type(self).equality_calls += 1
        raise AssertionError("attacker inequality was invoked")

    __hash__ = str.__hash__


class FidelityScorecardContractTests(unittest.TestCase):
    record: FidelityScorecard
    wire: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        cls.record = build_fidelity_scorecard()
        cls.wire = cls.record.to_wire()

    def test_exact_predecessor_join_scope_and_fingerprint(self) -> None:
        self.assertEqual(self.record.schema, "h3.fidelity_scorecard.v1")
        self.assertEqual(self.record.scorecard_version, "1.0.0")
        self.assertEqual(self.record.record_date, "2026-08-09")
        self.assertEqual(self.record.product_scope, "MANUAL_ONLY_SCOPED")
        self.assertEqual(self.record.claim_cap, "MANUAL_DETERMINISTIC_LOCAL_ONLY")
        self.assertEqual(self.record.assisted_profile_ids, ())
        self.assertIsNone(self.record.composite_score)
        self.assertFalse(self.record.promotion_authorized)
        self.assertEqual(
            tuple(item.authority for item in self.record.predecessors),
            (
                "official_oracle_terminal",
                "perturbation_corpus",
                "semantic_evaluation",
                "local_qualification",
                "fixed_h3_terminal",
                "human_review_terminal",
                "training_authorization_terminal",
            ),
        )
        self.assertEqual(self.record.fingerprint, FROZEN_FIDELITY_SCORECARD_FINGERPRINT)
        self.assertRegex(self.record.fingerprint, r"^sha256:[0-9a-f]{64}$")

    def test_each_public_projection_revalidates_predecessors_exactly_once(self) -> None:
        original = scorecard._verify_predecessor_authority
        calls = 0

        def count_verification() -> None:
            nonlocal calls
            calls += 1

        try:
            scorecard._verify_predecessor_authority = count_verification
            for projection in (
                self.record.to_wire,
                lambda: self.record.fingerprint,
                self.record.to_wire_bytes,
            ):
                calls = 0
                projection()
                self.assertEqual(calls, 1)
        finally:
            scorecard._verify_predecessor_authority = original

    def _assert_all_authority_paths_reject(
        self,
        existing: FidelityScorecard,
        existing_wire: dict[str, object],
    ) -> None:
        for path, action in (
            ("existing.to_wire", existing.to_wire),
            ("existing.fingerprint", lambda: existing.fingerprint),
            ("existing.to_wire_bytes", existing.to_wire_bytes),
            ("fresh build", build_fidelity_scorecard),
            ("validator", lambda: validate_fidelity_scorecard_wire(existing_wire)),
        ):
            with self.subTest(path=path):
                with self.assertRaises(FidelityScorecardError):
                    action()

    def test_derived_cache_cannot_hide_transitive_predecessor_drift(self) -> None:
        attacks = (
            (visual_benchmark, "build_default_visual_benchmark_plan"),
            (fixed_h3, "evaluate_local_qualification"),
            (human_review, "build_fixed_h3_terminal_record"),
            (training_authorization, "build_human_review_terminal_record"),
        )
        for module, attribute in attacks:
            scorecard._verify_derived_predecessor_authority.cache_clear()
            existing = build_fidelity_scorecard()
            existing_wire = existing.to_wire()
            original = getattr(module, attribute)

            def drift(*_args: object, **_kwargs: object) -> object:
                raise RuntimeError("deep predecessor drift")

            try:
                setattr(module, attribute, drift)
                with self.subTest(attribute=attribute, cache="warm"):
                    self._assert_all_authority_paths_reject(existing, existing_wire)
                scorecard._verify_derived_predecessor_authority.cache_clear()
                with self.subTest(attribute=attribute, cache="cleared"):
                    with self.assertRaises(FidelityScorecardError):
                        existing.to_wire()
            finally:
                setattr(module, attribute, original)
                scorecard._verify_derived_predecessor_authority.cache_clear()

    def test_exact_perturbation_dimension_inventory_is_revalidated_on_use(self) -> None:
        scorecard._verify_derived_predecessor_authority.cache_clear()
        existing = build_fidelity_scorecard()
        existing_wire = existing.to_wire()
        dimension = next(iter(perturbation.PerturbationDimension))
        original_value = dimension.value
        try:
            object.__setattr__(dimension, "_value_", "forged_dimension")
            self._assert_all_authority_paths_reject(existing, existing_wire)
            scorecard._verify_derived_predecessor_authority.cache_clear()
            with self.assertRaises(FidelityScorecardError):
                existing.to_wire()
        finally:
            object.__setattr__(dimension, "_value_", original_value)
            scorecard._verify_derived_predecessor_authority.cache_clear()

        original_enum = perturbation.PerturbationDimension
        forged_enum = Enum(  # type: ignore[misc]
            "PerturbationDimension",
            {f"D{index}": f"forged_{index}" for index in range(27)},
        )
        try:
            perturbation.PerturbationDimension = forged_enum  # type: ignore[assignment,misc]
            self._assert_all_authority_paths_reject(existing, existing_wire)
            scorecard._verify_derived_predecessor_authority.cache_clear()
            with self.assertRaises(FidelityScorecardError):
                existing.to_wire()
        finally:
            perturbation.PerturbationDimension = original_enum  # type: ignore[misc]
            scorecard._verify_derived_predecessor_authority.cache_clear()
        self.assertEqual(build_fidelity_scorecard().to_wire(), existing_wire)

    def test_live_perturbation_authority_and_derived_inventory_drift_are_rejected(self) -> None:
        existing = build_fidelity_scorecard()
        existing_wire = existing.to_wire()
        attacks = (
            ("PERTURBATION_EVALUATION_SCHEMA", "forged.perturbation.schema"),
            ("PERTURBATION_CORPUS_VERSION", "9.9.9"),
            ("FROZEN_CORPUS_FINGERPRINT", "sha256:" + "f" * 64),
            ("FROZEN_SOURCE_CLUSTER_COUNT", 8),
            ("FROZEN_CASE_COUNT", 26),
        )
        for attribute, forged in attacks:
            original = getattr(perturbation, attribute)
            try:
                setattr(perturbation, attribute, forged)
                with self.subTest(attribute=attribute):
                    with self.assertRaises(FidelityScorecardError):
                        build_fidelity_scorecard()
                    with self.assertRaises(FidelityScorecardError):
                        validate_fidelity_scorecard_wire(existing_wire)
                    for projection in (
                        existing.to_wire,
                        lambda: existing.fingerprint,
                        existing.to_wire_bytes,
                    ):
                        with self.assertRaises(FidelityScorecardError):
                            projection()
            finally:
                setattr(perturbation, attribute, original)

    def test_live_semantic_authority_and_report_inputs_drift_are_rejected(self) -> None:
        existing = build_fidelity_scorecard()
        existing_wire = existing.to_wire()
        attacks = (
            ("SEMANTIC_EVALUATION_SCHEMA", "forged.semantic.schema"),
            ("FROZEN_EVALUATION_BUNDLE_FINGERPRINT", "sha256:" + "f" * 64),
            ("FROZEN_GUIDE_CLUSTER_COUNT", 1),
            ("FROZEN_GUIDE_SUPPORT", 5),
            ("FROZEN_P0_CLUSTER_COUNT", 7),
            ("FROZEN_P0_SUPPORT", 23),
            ("FROZEN_NON_P0_CLUSTER_COUNT", 9),
            ("FROZEN_NON_P0_SUPPORT", 39),
            ("FROZEN_NON_P0_POSITIVE_SUPPORT", 19),
            ("FROZEN_NON_P0_NEGATIVE_SUPPORT", 19),
            ("FROZEN_INTERVAL_METHOD", "forged_interval"),
            ("FROZEN_INTERVAL_Z", "1.000000000000000"),
            ("FROZEN_POINT_TARGET", "0.90000000"),
        )
        for attribute, forged in attacks:
            original = getattr(semantic, attribute)
            try:
                setattr(semantic, attribute, forged)
                with self.subTest(attribute=attribute):
                    with self.assertRaises(FidelityScorecardError):
                        build_fidelity_scorecard()
                    with self.assertRaises(FidelityScorecardError):
                        validate_fidelity_scorecard_wire(existing_wire)
                    for projection in (
                        existing.to_wire,
                        lambda: existing.fingerprint,
                        existing.to_wire_bytes,
                    ):
                        with self.assertRaises(FidelityScorecardError):
                            projection()
            finally:
                setattr(semantic, attribute, original)
        original_interval = semantic.wilson_interval
        try:
            semantic.wilson_interval = lambda _successes, _total: (  # type: ignore[assignment]
                "0.00000000",
                "1.00000000",
            )
            with self.assertRaises(FidelityScorecardError):
                build_fidelity_scorecard()
            with self.assertRaises(FidelityScorecardError):
                validate_fidelity_scorecard_wire(existing_wire)
            for projection in (
                existing.to_wire,
                lambda: existing.fingerprint,
                existing.to_wire_bytes,
            ):
                with self.assertRaises(FidelityScorecardError):
                    projection()
        finally:
            semantic.wilson_interval = original_interval

    def test_mode_inventory_is_exact_and_official_results_remain_missing(self) -> None:
        observed = {
            item.mode: (
                item.case_count,
                item.source_cluster_count,
                item.guide_case_count,
                item.p0_case_count,
                item.non_p0_case_count,
                item.failure_count,
                item.official_result_count,
                item.missing_official_result_count,
            )
            for item in self.record.modes
        }
        self.assertEqual(
            observed,
            {
                "t2va": (41, 11, 1, 0, 40, 0, 0, 41),
                "i2va": (1, 1, 1, 0, 0, 0, 0, 1),
                "fl2va": (1, 1, 1, 0, 0, 0, 0, 1),
                "ref2va": (27, 9, 3, 24, 0, 0, 0, 27),
            },
        )
        self.assertEqual(sum(item.case_count for item in self.record.modes), 70)
        self.assertEqual(sum(item.source_cluster_count for item in self.record.modes), 22)
        self.assertEqual(self.record.semantic_source_cluster_count, 20)
        self.assertEqual(sum(item.missing_official_result_count for item in self.record.modes), 70)

    def test_lane_inventory_is_closed_and_missing_evidence_is_not_imputed(self) -> None:
        self.assertEqual(
            tuple(item.lane for item in self.record.lanes),
            tuple(FidelityLane),
        )
        dispositions = {item.lane: item.disposition for item in self.record.lanes}
        self.assertEqual(dispositions[FidelityLane.STRUCTURAL], EvidenceDisposition.QUALIFIED)
        self.assertEqual(dispositions[FidelityLane.PLANNER], EvidenceDisposition.LOCAL_ONLY)
        self.assertEqual(dispositions[FidelityLane.SECURITY], EvidenceDisposition.QUALIFIED)
        self.assertEqual(dispositions[FidelityLane.PERCEPTION], EvidenceDisposition.MISSING)
        self.assertEqual(
            dispositions[FidelityLane.ORACLE], EvidenceDisposition.UNAVAILABLE_PROHIBITED
        )
        self.assertEqual(dispositions[FidelityLane.FIXED_H3], EvidenceDisposition.UNAVAILABLE)
        self.assertEqual(dispositions[FidelityLane.HUMAN], EvidenceDisposition.UNAVAILABLE)
        self.assertEqual(dispositions[FidelityLane.RESOURCE], EvidenceDisposition.MISSING)
        for item in self.record.lanes:
            if item.disposition in {
                EvidenceDisposition.MISSING,
                EvidenceDisposition.UNAVAILABLE,
                EvidenceDisposition.UNAVAILABLE_PROHIBITED,
                EvidenceDisposition.EXHAUSTED,
                EvidenceDisposition.DRIFT_SUSPENDED,
            }:
                self.assertEqual(item.metrics, ())
                self.assertIsNone(item.confidence_interval)
                self.assertIsNone(item.achieved_power)
                self.assertIsNone(item.service_window)
                self.assertIsNone(item.evidence_fingerprint)

    def test_deterministic_and_local_semantic_results_remain_separate(self) -> None:
        lanes = {item.lane: item for item in self.record.lanes}
        structural = {item.metric: item for item in lanes[FidelityLane.STRUCTURAL].metrics}
        self.assertEqual(structural["parse_schema"].passed_count, 70)
        self.assertEqual(structural["parse_schema"].total_count, 70)
        self.assertEqual(structural["hard_constraint_detection"].passed_count, 6)
        self.assertEqual(structural["hard_constraint_detection"].total_count, 6)
        self.assertEqual(structural["reference_role_order_ownership_detection"].passed_count, 12)
        self.assertEqual(structural["reference_role_order_ownership_detection"].total_count, 12)
        planner = {item.metric: item for item in lanes[FidelityLane.PLANNER].metrics}
        self.assertEqual(planner["guide_round_trip"].passed_count, 6)
        self.assertEqual(planner["guide_round_trip"].total_count, 6)
        self.assertEqual(planner["p0_mutation_detection"].passed_count, 24)
        self.assertEqual(planner["p0_mutation_detection"].total_count, 24)
        self.assertEqual(planner["semantic_precision"].passed_count, 20)
        self.assertEqual(planner["semantic_precision"].total_count, 20)
        self.assertEqual(planner["semantic_precision"].point_estimate, "1.00000000")
        self.assertEqual(
            planner["semantic_precision"].confidence_interval,
            ("0.83887484", "1.00000000"),
        )
        self.assertEqual(planner["semantic_recall"].passed_count, 20)
        self.assertEqual(planner["semantic_recall"].total_count, 20)
        self.assertEqual(
            planner["semantic_recall"].confidence_interval,
            ("0.83887484", "1.00000000"),
        )
        privacy = lanes[FidelityLane.SECURITY].metrics
        self.assertEqual(len(privacy), 1)
        self.assertEqual((privacy[0].passed_count, privacy[0].total_count), (1, 1))
        self.assertEqual(self.record.perturbation_source_cluster_count, 9)
        self.assertEqual(self.record.perturbation_case_count, 27)
        self.assertEqual(self.record.official_relation_state, "MISSING")

    def test_claim_ladder_is_non_equivalent_and_never_implies_higher_levels(self) -> None:
        self.assertEqual(tuple(item.level for item in self.record.claims), tuple(ClaimLevel))
        self.assertEqual(
            tuple(item.disposition for item in self.record.claims),
            (
                ClaimDisposition.SUPPORTED,
                ClaimDisposition.SUPPORTED_LOCAL_ONLY,
                ClaimDisposition.UNVERIFIABLE,
                ClaimDisposition.UNVERIFIABLE,
                ClaimDisposition.UNVERIFIABLE,
            ),
        )
        self.assertTrue(all(not item.implies_next for item in self.record.claims))
        self.assertTrue(all(not item.promotion_authorized for item in self.record.claims))
        self.assertIsNone(self.record.claims[-1].evidence_fingerprint)

    def test_oracle_thresholds_are_conditional_and_not_observed(self) -> None:
        protocol = self.record.statistical_protocol
        self.assertEqual(protocol.confidence_level_basis_points, 9500)
        self.assertEqual(protocol.interval_method, "wilson_two_sided_95")
        self.assertEqual(protocol.multiplicity_method, "holm_bonferroni")
        self.assertEqual(protocol.planned_power_floor_basis_points, 8000)
        self.assertTrue(protocol.failures_remain_in_denominators)
        self.assertTrue(protocol.pilot_required_for_change)
        self.assertFalse(protocol.threshold_change_after_holdout_allowed)
        self.assertGreaterEqual(protocol.repeatability_runs, 2)
        self.assertEqual(
            {item.metric: (item.direction, item.threshold) for item in self.record.oracle_targets},
            {
                "semantic_micro_f1_lower_95": ("minimum", "0.90000000"),
                "every_stratum_macro_f1_lower_95": ("minimum", "0.80000000"),
                "unsupported_fact_upper_95": ("maximum", "0.05000000"),
            },
        )
        for item in self.record.oracle_targets:
            self.assertEqual(item.applicability, "CONDITIONAL_NOT_EXECUTED")
            self.assertIsNone(item.observed_value)
            self.assertIsNone(item.observed_interval)
            self.assertIsNone(item.verdict)

    def test_fixed_h3_and_human_lanes_are_noncompensatory(self) -> None:
        lanes = {item.lane: item for item in self.record.lanes}
        for lane in (FidelityLane.FIXED_H3, FidelityLane.HUMAN):
            self.assertTrue(lanes[lane].noncompensatory)
            self.assertEqual(lanes[lane].metrics, ())
        predecessor_wires = cast(list[dict[str, object]], self.wire["predecessors"])
        self.assertIn("NO_FIXED_H3_GENERATION_EVIDENCE", predecessor_wires[4].values())
        self.assertIn("NO_HUMAN_QUALITY_CLAIM", predecessor_wires[5].values())

    def test_public_constructors_and_post_construction_mutation_cannot_forge(self) -> None:
        for cls in (
            scorecard.PredecessorReceipt,
            scorecard.ModeReceipt,
            scorecard.MetricReceipt,
            scorecard.LaneReceipt,
            scorecard.ClaimReceipt,
            scorecard.StatisticalProtocol,
            scorecard.OracleTarget,
            FidelityScorecard,
        ):
            with self.assertRaises(TypeError):
                cls()  # type: ignore[call-arg]
        record = copy.deepcopy(self.record)
        with self.assertRaises(FidelityScorecardError):
            replace(record, product_scope="ASSISTED_PROFILE_QUALIFIED")
        object.__setattr__(record, "product_scope", "ASSISTED_PROFILE_QUALIFIED")
        with self.assertRaises(FidelityScorecardError):
            record.to_wire()
        with self.assertRaises(FidelityScorecardError):
            _ = record.fingerprint

    def test_exact_type_guards_run_before_attacker_equality(self) -> None:
        _EqualityTrap.equality_calls = 0
        with self.assertRaises(FidelityScorecardError):
            scorecard.MetricReceipt(
                "semantic_precision",
                20,
                20,
                _EqualityTrap("1.00000000"),
                ("0.83887484", "1.00000000"),
            )
        self.assertEqual(_EqualityTrap.equality_calls, 0)

        value_attack = dict(self.wire)
        value_attack["schema"] = _EqualityTrap("h3.fidelity_scorecard.v1")
        with self.assertRaises(FidelityScorecardError):
            validate_fidelity_scorecard_wire(value_attack)
        self.assertEqual(_EqualityTrap.equality_calls, 0)

        key_attack = dict(self.wire)
        schema_value = key_attack.pop("schema")
        key_attack[_EqualityTrap("schema")] = schema_value
        with self.assertRaises(FidelityScorecardError):
            validate_fidelity_scorecard_wire(key_attack)
        self.assertEqual(_EqualityTrap.equality_calls, 0)

    def test_all_nested_projections_revalidate_and_instances_do_not_cross_contaminate(self) -> None:
        def assert_mutation_rejected(
            select: Callable[[FidelityScorecard], object],
            field_name: str,
            forged_value: object,
        ) -> None:
            candidate = copy.deepcopy(self.record)
            object.__setattr__(select(candidate), field_name, forged_value)
            with self.assertRaises(FidelityScorecardError):
                candidate.to_wire()
            with self.assertRaises(FidelityScorecardError):
                _ = candidate.fingerprint

        assert_mutation_rejected(lambda item: item.predecessors[0], "claim_cap", "FORGED_AUTHORITY")
        assert_mutation_rejected(lambda item: item.modes[0], "case_count", 999)
        assert_mutation_rejected(
            lambda item: item.lanes[0], "disposition", EvidenceDisposition.MISSING
        )
        assert_mutation_rejected(
            lambda item: item.lanes[0].metrics[0], "point_estimate", "0.00000000"
        )
        assert_mutation_rejected(
            lambda item: item.claims[0], "disposition", ClaimDisposition.UNVERIFIABLE
        )
        assert_mutation_rejected(lambda item: item.statistical_protocol, "repeatability_runs", 1)
        assert_mutation_rejected(
            lambda item: item.oracle_targets[0], "observed_value", "1.00000000"
        )

        first = copy.deepcopy(self.record)
        second = copy.deepcopy(self.record)
        expected = second.to_wire()
        object.__setattr__(first.lanes[0].metrics[0], "point_estimate", "0.00000000")
        with self.assertRaises(FidelityScorecardError):
            first.to_wire()
        self.assertEqual(second.to_wire(), expected)
        self.assertEqual(copy.deepcopy(self.record).to_wire(), expected)

    def test_every_public_dataclass_field_rejects_value_drift(self) -> None:
        def forged_value(value: object) -> object:
            if type(value) is bool:
                return not value
            if type(value) is int:
                return value + 1
            if type(value) is str:
                return value + ".forged"
            if type(value) is tuple:
                return value + ((value[0] if value else "forged"),)
            if type(value) is FidelityLane:
                return FidelityLane.PERCEPTION
            if type(value) is EvidenceDisposition:
                return EvidenceDisposition.MISSING
            if type(value) is ClaimLevel:
                return ClaimLevel.PRIVATE_INTERNAL_RECOVERY
            if type(value) is ClaimDisposition:
                return ClaimDisposition.UNVERIFIABLE
            if value is None:
                return "forged"
            return object()

        targets: tuple[tuple[str, Callable[[FidelityScorecard], object], tuple[str, ...]], ...] = (
            (
                "predecessor",
                lambda item: item.predecessors[0],
                tuple(field.name for field in fields(scorecard.PredecessorReceipt)),
            ),
            (
                "mode",
                lambda item: item.modes[0],
                tuple(field.name for field in fields(scorecard.ModeReceipt)),
            ),
            (
                "metric",
                lambda item: item.lanes[0].metrics[0],
                tuple(field.name for field in fields(scorecard.MetricReceipt)),
            ),
            (
                "lane",
                lambda item: item.lanes[0],
                tuple(field.name for field in fields(scorecard.LaneReceipt)),
            ),
            (
                "claim",
                lambda item: item.claims[0],
                tuple(field.name for field in fields(scorecard.ClaimReceipt)),
            ),
            (
                "protocol",
                lambda item: item.statistical_protocol,
                tuple(field.name for field in fields(scorecard.StatisticalProtocol)),
            ),
            (
                "oracle",
                lambda item: item.oracle_targets[0],
                tuple(field.name for field in fields(scorecard.OracleTarget)),
            ),
            (
                "root",
                lambda item: item,
                tuple(field.name for field in fields(FidelityScorecard)),
            ),
        )
        original = scorecard._verify_predecessor_authority
        try:
            # The predecessor chain has a dedicated adversarial matrix above; this exhaustive
            # value-object sweep isolates the 74 public dataclass fields it is intended to pin.
            scorecard._verify_predecessor_authority = lambda: None
            for target_name, select, field_names in targets:
                for field_name in field_names:
                    candidate = copy.deepcopy(self.record)
                    target = select(candidate)
                    object.__setattr__(
                        target, field_name, forged_value(getattr(target, field_name))
                    )
                    with self.subTest(target=target_name, field=field_name):
                        with self.assertRaises(FidelityScorecardError):
                            candidate.to_wire()
        finally:
            scorecard._verify_predecessor_authority = original

    def test_production_validator_rejects_semantic_drift_and_imputation(self) -> None:
        self.assertEqual(validate_fidelity_scorecard_wire(self.wire).to_wire(), self.wire)
        attacks: list[dict[str, object]] = []
        attacks.append({**self.wire, "product_scope": "ASSISTED_PROFILE_QUALIFIED"})
        attacks.append({**self.wire, "composite_score": "1.00000000"})
        attacks.append({**self.wire, "promotion_authorized": True})
        forged_lane = json.loads(json.dumps(self.wire))
        forged_lane["lanes"][1]["metrics"] = [
            {
                "metric": "forged_accuracy",
                "passed_count": "1",
                "total_count": "1",
                "point_estimate": "1.00000000",
                "confidence_interval": None,
            }
        ]
        attacks.append(forged_lane)
        forged_claim = json.loads(json.dumps(self.wire))
        forged_claim["claims"][2]["disposition"] = "SUPPORTED"
        forged_claim["claims"][2]["promotion_authorized"] = True
        attacks.append(forged_claim)
        forged_target = json.loads(json.dumps(self.wire))
        forged_target["oracle_targets"][0]["observed_value"] = "1.00000000"
        forged_target["oracle_targets"][0]["verdict"] = "PASS"
        attacks.append(forged_target)
        for attack in attacks:
            with self.subTest(attack=attack):
                with self.assertRaises(FidelityScorecardError):
                    validate_fidelity_scorecard_wire(attack)

    def test_decoder_rejects_duplicates_cycles_non_json_and_unsafe_content(self) -> None:
        encoded = json.dumps(self.wire, separators=(",", ":"))
        self.assertEqual(decode_fidelity_scorecard_json(encoded).to_wire(), self.wire)
        with self.assertRaises(FidelityScorecardError):
            decode_fidelity_scorecard_json('{"schema":"x","schema":"y"}')
        with self.assertRaises(FidelityScorecardError):
            decode_fidelity_scorecard_json('{"schema":"\\ud800"}')
        for payload in (
            '{"unexpected":0}',
            '{"unexpected":0.0}',
            '{"unexpected":1e-400}',
            '{"unexpected":8000.0000000000001}',
            '{"unexpected":NaN}',
            '{"unexpected":Infinity}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(FidelityScorecardError):
                    decode_fidelity_scorecard_json(payload)
        with self.assertRaises(FidelityScorecardError):
            decode_fidelity_scorecard_json(b"\xff")
        cycle: dict[str, object] = {}
        cycle["self"] = cycle
        with self.assertRaises(FidelityScorecardError):
            validate_fidelity_scorecard_wire(cycle)
        for mutation in (
            {**self.wire, "schema": 1},
            {**self.wire, "schema": "https://example.invalid/private"},
            {**self.wire, "limitations": ["C:\\private\\local"]},
            {**self.wire, "unexpected": True},
        ):
            with self.assertRaises(FidelityScorecardError):
                validate_fidelity_scorecard_wire(mutation)

    def test_fixture_schema_and_generator_are_exact(self) -> None:
        for path in (FIXTURE, SCHEMA, GENERATOR):
            self.assertTrue(path.is_file(), f"missing public artifact: {path}")
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(fixture, self.wire)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(fixture)), [])
        for mutation in (
            {**fixture, "unexpected": True},
            {**fixture, "product_scope": "ASSISTED_PROFILE_QUALIFIED"},
            {**fixture, "composite_score": "1.00000000"},
            {**fixture, "promotion_authorized": 0.0},
        ):
            self.assertNotEqual(list(validator.iter_errors(mutation)), [])
        count_drift = json.loads(json.dumps(fixture))
        count_drift["modes"][0]["case_count"] = "69"
        self.assertNotEqual(list(validator.iter_errors(count_drift)), [])

    def test_concurrent_and_round_trip_builds_are_isolated(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = tuple(pool.map(lambda _: build_fidelity_scorecard(), range(2)))
        first, second = records
        self.assertIsNot(first, second)
        self.assertIsNot(first.predecessors[0], second.predecessors[0])
        self.assertIsNot(first.modes[0], second.modes[0])
        self.assertIsNot(first.lanes[0], second.lanes[0])
        self.assertEqual(first.to_wire(), second.to_wire())
        self.assertEqual(len({id(item) for item in records}), 2)
        self.assertEqual(len({id(item.lanes[0]) for item in records}), 2)
        self.assertEqual({item.fingerprint for item in records}, {first.fingerprint})
        reloaded = decode_fidelity_scorecard_json(json.dumps(first.to_wire()))
        self.assertEqual(reloaded.to_wire(), first.to_wire())

    def test_admitted_wire_cache_cannot_replace_fresh_source_authority(self) -> None:
        original = scorecard._ADMITTED_FIDELITY_SCORECARD_WIRE_BYTES
        try:
            scorecard._ADMITTED_FIDELITY_SCORECARD_WIRE_BYTES = b"{}"
            with self.assertRaises(FidelityScorecardError):
                validate_fidelity_scorecard_wire({})
        finally:
            scorecard._ADMITTED_FIDELITY_SCORECARD_WIRE_BYTES = original
        self.assertEqual(scorecard._ADMITTED_FIDELITY_SCORECARD_WIRE_BYTES, original)

    def test_leaf_exports_are_narrow_and_no_execution_surface_exists(self) -> None:
        expected = {
            "ClaimDisposition",
            "ClaimLevel",
            "EvidenceDisposition",
            "FidelityLane",
            "FidelityScorecard",
            "FidelityScorecardError",
            "FROZEN_FIDELITY_SCORECARD_FINGERPRINT",
            "build_fidelity_scorecard",
            "validate_fidelity_scorecard_wire",
            "decode_fidelity_scorecard_json",
        }
        self.assertEqual(expected - set(scorecard.__all__), set())
        for symbol in expected:
            self.assertTrue(hasattr(scorecard, symbol), symbol)
        source = (ROOT / "comfyui_h3_context" / "core" / "fidelity_scorecard.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        forbidden_import_roots = {
            "aiohttp",
            "requests",
            "httpx",
            "torch",
            "numpy",
            "subprocess",
            "socket",
            "pathlib",
        }
        imported = {
            node.names[0].name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        }
        imported.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertEqual(imported & forbidden_import_roots, set())
        self.assertNotRegex(
            source,
            r"\b(train|fine_tune|provider_call|model_load|media_open|gpu_run|human_review_start)\s*\(",
        )

    def test_wire_has_no_number_leaves_or_internal_identifiers(self) -> None:
        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertIs(type(key), str)
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            else:
                self.assertNotIn(type(value), {int, float})

        walk(self.wire)
        encoded = json.dumps(self.wire, ensure_ascii=False)
        self.assertNotRegex(encoded, r"\b(?:M|RM|HC)[0-9]+-[0-9]+\b")
        self.assertNotRegex(encoded.casefold(), r"https?://|file:|[a-z]:\\|\.planning|reference/")
        self.assertLess(len(encoded.encode("utf-8")), 32_768)
        self.assertEqual(len(fields(FidelityScorecard)), 19)


if __name__ == "__main__":
    unittest.main()
