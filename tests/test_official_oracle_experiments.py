"""M9-06 controlled differential experiment contract tests."""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import cast

from comfyui_h3_context.core import (
    OfficialOracleAbortCondition,
    OfficialOracleBudgetPolicy,
    OfficialOracleClaimCeiling,
    OfficialOracleDecision,
    OfficialOracleExecutionMode,
    OfficialOracleExecutionScope,
    OfficialOracleExperimentCase,
    OfficialOracleExperimentFactor,
    OfficialOracleExperimentHarness,
    OfficialOracleExperimentPairStatus,
    OfficialOracleExperimentReportStatus,
    OfficialOracleExperimentSample,
    OfficialOracleExperimentSampleStatus,
    OfficialOracleExperimentVariabilityStatus,
    OfficialOracleGovernancePolicy,
    OfficialOracleLaneDisposition,
    OfficialOracleLaneStatus,
    OfficialOracleMediaPrivacy,
    OfficialOracleRetentionPolicy,
    OfficialOracleReviewStatus,
    OfficialOracleSourceApplicability,
    OfficialOracleSourceCategory,
    OfficialOracleSourceLedger,
    OfficialOracleSourceRecord,
    OfficialOracleTermsDecision,
    ProviderExecutionPolicy,
    ProviderIdentity,
    ProviderPrivacyMode,
)
from comfyui_h3_context.core.errors import ContractValidationError, OfficialOracleGovernanceError
from comfyui_h3_context.core.official_oracle_experiments import OfficialOracleExperimentRun
from comfyui_h3_context.core.official_oracle_governance import OfficialOracleExecutionGate

FINGERPRINT_A = "sha256:" + "a" * 64
FINGERPRINT_B = "sha256:" + "b" * 64
FINGERPRINT_C = "sha256:" + "c" * 64


def source_ledger() -> OfficialOracleSourceLedger:
    rows = (
        ("general", OfficialOracleSourceCategory.GENERAL_TERMS),
        ("product", OfficialOracleSourceCategory.PRODUCT_TERMS),
        ("privacy", OfficialOracleSourceCategory.PRIVACY_POLICY),
        ("api", OfficialOracleSourceCategory.API_LIFECYCLE),
        ("deletion", OfficialOracleSourceCategory.DELETION),
        ("pricing", OfficialOracleSourceCategory.PRICING),
        ("account", OfficialOracleSourceCategory.ACCOUNT_ORDER),
        ("supplemental", OfficialOracleSourceCategory.SUPPLEMENTAL),
    )
    return OfficialOracleSourceLedger(
        ledger_id="experiment_ledger",
        ledger_revision="ledger.rev_1",
        reviewer_reference="reviewer.experiment",
        approver_reference="approver.experiment",
        jurisdiction="GLOBAL",
        sources=tuple(
            OfficialOracleSourceRecord(
                source_id=f"source.{source_id}",
                category=category,
                url=f"https://official.example.test/{source_id}",
                revision="retrieved-2026-08-07",
                effective_date="2026-08-07",
                retrieved_at="2026-08-07",
                sha256="d" * 64,
                authority="official",
                jurisdiction="GLOBAL",
                applicability=OfficialOracleSourceApplicability.NOT_APPLICABLE,
            )
            for source_id, category in rows
        ),
    )


def terms() -> OfficialOracleTermsDecision:
    lanes = (
        OfficialOracleLaneDisposition(
            "official_live_oracle",
            OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
            OfficialOracleExecutionScope.OFFLINE_ONLY,
            OfficialOracleClaimCeiling.NO_CLAIM,
            OfficialOracleRetentionPolicy.NONE,
            "review.live-unavailable",
        ),
        OfficialOracleLaneDisposition(
            "official_mocked_contract",
            OfficialOracleLaneStatus.LIMITED,
            OfficialOracleExecutionScope.MOCKED_ONLY,
            OfficialOracleClaimCeiling.STRUCTURAL_ONLY,
            OfficialOracleRetentionPolicy.HASH_ONLY,
            "review.mocked-only",
        ),
        *tuple(
            OfficialOracleLaneDisposition(
                lane_id,
                OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
                OfficialOracleExecutionScope.OFFLINE_ONLY,
                OfficialOracleClaimCeiling.NO_CLAIM,
                OfficialOracleRetentionPolicy.NONE,
                f"review.{lane_id}",
            )
            for lane_id in (
                "recorded_oracle_fixture",
                "benchmark",
                "publication",
                "training_distillation",
            )
        ),
    )
    return OfficialOracleTermsDecision(
        review_id="experiment-review",
        source_ledger_id="experiment_ledger",
        terms_revision="terms.rev_1",
        reviewed_at="2026-08-07T00:00:00Z",
        valid_until="2026-08-08T00:00:00Z",
        reviewer_reference="reviewer.experiment",
        status=OfficialOracleReviewStatus.APPROVED_CONTROLLED_RESEARCH,
        source_references=tuple(
            f"source.{name}"
            for name in (
                "general",
                "product",
                "privacy",
                "api",
                "deletion",
                "pricing",
                "account",
                "supplemental",
            )
        ),
        lane_dispositions=lanes,
        api_use=OfficialOracleDecision.ALLOW,
        output_retention=OfficialOracleDecision.ALLOW,
        redistribution=OfficialOracleDecision.DENY,
        benchmarking=OfficialOracleDecision.DENY,
        publication=OfficialOracleDecision.DENY,
        automated_probing=OfficialOracleDecision.DENY,
        training_distillation=OfficialOracleDecision.DENY,
    )


def gate() -> OfficialOracleExecutionGate:
    policy = OfficialOracleGovernancePolicy(
        policy_revision="policy.rev_1",
        operator_consent=True,
        consent_reference="consent.experiment",
        consent_terms_revision="terms.rev_1",
        consent_policy_revision="policy.rev_1",
        media_privacy=OfficialOracleMediaPrivacy.NO_MEDIA,
        retention_policy=OfficialOracleRetentionPolicy.HASH_ONLY,
        deletion_reference="delete.experiment",
        abort_on=frozenset({OfficialOracleAbortCondition.CANCELLATION}),
        budget=OfficialOracleBudgetPolicy(
            max_calls=32,
            max_requests_per_window=32,
            window_seconds=60.0,
            max_spend_minor_units=0,
            currency="usd",
            max_output_tokens=1000,
        ),
    )
    provider = ProviderExecutionPolicy(
        provider=ProviderIdentity.OFFICIAL_MINIMAX,
        privacy_mode=ProviderPrivacyMode.EXPLICIT_REMOTE,
        offline=False,
        network_allowed=True,
        upload_consent=False,
        credential_reference="env.experiment",
    )
    return OfficialOracleExecutionGate(terms(), policy, source_ledger(), provider)


def case(
    factor: OfficialOracleExperimentFactor = OfficialOracleExperimentFactor.WORDING,
    *,
    replicates: int = 2,
    negative_control: bool = False,
) -> OfficialOracleExperimentCase:
    return OfficialOracleExperimentCase(
        case_id=f"case.{factor.value}",
        factor=factor,
        baseline_input_fingerprint=FINGERPRINT_A,
        variant_input_fingerprint=FINGERPRINT_B,
        changed_fields=(factor.value,),
        source_fingerprints=(FINGERPRINT_C,),
        hypothesis_ids=(f"hypothesis.{factor.value}",),
        replicates=replicates,
        negative_control=negative_control,
    )


class Executor:
    def __init__(self, outputs: Mapping[tuple[bool, int], str | None]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, bool, int]] = []

    def execute(
        self,
        experiment_case: OfficialOracleExperimentCase,
        *,
        variant: bool,
        replicate_index: int,
    ) -> OfficialOracleExperimentSample:
        self.calls.append((experiment_case.case_id, variant, replicate_index))
        output = self.outputs.get((variant, replicate_index))
        if output is None:
            return OfficialOracleExperimentSample(
                OfficialOracleExperimentSampleStatus.FAILED,
                output_fingerprint=None,
                diagnostic_code="executor.failed",
            )
        return OfficialOracleExperimentSample(
            OfficialOracleExperimentSampleStatus.RECORDED,
            output_fingerprint=output,
        )


def run_result(value: object) -> OfficialOracleExperimentRun:
    return cast(OfficialOracleExperimentRun, value)


class OfficialOracleExperimentTests(unittest.TestCase):
    def test_one_factor_case_contract_is_closed_and_versioned(self) -> None:
        for factor in OfficialOracleExperimentFactor:
            item = case(factor, replicates=1)
            self.assertEqual(item.changed_fields, (factor.value,))
            self.assertEqual(item.to_public_dict()["schema"], "h3.context.ir.experiment.v1")
        with self.assertRaises(ContractValidationError):
            OfficialOracleExperimentCase(
                "case.bad",
                OfficialOracleExperimentFactor.WORDING,
                FINGERPRINT_A,
                FINGERPRINT_B,
                ("duration",),
                (FINGERPRINT_C,),
            )

    def test_mock_pair_and_replicate_agreement_are_measured_without_raw_output(self) -> None:
        executor = Executor(
            {
                (False, 1): FINGERPRINT_A,
                (False, 2): FINGERPRINT_A,
                (True, 1): FINGERPRINT_B,
                (True, 2): FINGERPRINT_B,
            }
        )
        result = run_result(
            OfficialOracleExperimentHarness(gate()).run(
                (case(),),
                executor,
                lane_id="official_mocked_contract",
                execution_mode=OfficialOracleExecutionMode.MOCKED,
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        )
        self.assertEqual(result.report.status, OfficialOracleExperimentReportStatus.COMPLETE)
        item = result.report.results[0]
        self.assertEqual(item.pair_status, OfficialOracleExperimentPairStatus.DIFFERENT)
        self.assertEqual(
            item.variability_status, OfficialOracleExperimentVariabilityStatus.MEASURED
        )
        self.assertEqual(item.baseline_agreement_ratio, "1")
        self.assertEqual(item.variant_agreement_ratio, "1")
        self.assertEqual(len(executor.calls), 4)
        encoded = json.dumps(result.report.to_public_dict(), sort_keys=True)
        self.assertNotIn("raw", encoded.casefold())
        self.assertNotIn("https://", encoded)

    def test_unavailable_live_lane_is_missing_and_executor_is_not_called(self) -> None:
        executor = Executor({(False, 1): FINGERPRINT_A, (True, 1): FINGERPRINT_B})
        result = run_result(
            OfficialOracleExperimentHarness(gate()).run(
                (case(replicates=1),),
                executor,
                lane_id="official_live_oracle",
                execution_mode=OfficialOracleExecutionMode.LIVE,
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        )
        self.assertEqual(result.report.status, OfficialOracleExperimentReportStatus.INCOMPLETE)
        self.assertEqual(
            result.report.results[0].pair_status, OfficialOracleExperimentPairStatus.MISSING
        )
        self.assertEqual(
            result.report.results[0].variability_status,
            OfficialOracleExperimentVariabilityStatus.MISSING,
        )
        self.assertEqual(executor.calls, [])
        self.assertEqual(result.report.claim_ceiling, OfficialOracleClaimCeiling.NO_CLAIM)

    def test_negative_control_violation_is_visible_and_missingness_is_not_zero(self) -> None:
        executor = Executor({(False, 1): FINGERPRINT_A, (True, 1): FINGERPRINT_B})
        result = run_result(
            OfficialOracleExperimentHarness(gate()).run(
                (case(negative_control=True, replicates=1),),
                executor,
                lane_id="official_mocked_contract",
                execution_mode=OfficialOracleExecutionMode.MOCKED,
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        )
        item = result.report.results[0]
        self.assertTrue(item.negative_control_violation)
        self.assertIn("negative_control.output_differs", item.diagnostic_codes)
        self.assertIsNone(item.baseline_agreement_ratio)

        missing_executor = Executor({(False, 1): FINGERPRINT_A})
        missing = run_result(
            OfficialOracleExperimentHarness(gate()).run(
                (case(replicates=2),),
                missing_executor,
                lane_id="official_mocked_contract",
                execution_mode=OfficialOracleExecutionMode.MOCKED,
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        )
        self.assertEqual(missing.report.status, OfficialOracleExperimentReportStatus.FAILED)
        self.assertEqual(
            missing.report.results[0].variability_status,
            OfficialOracleExperimentVariabilityStatus.MISSING,
        )

    def test_executor_failure_and_gate_denial_are_distinguishable(self) -> None:
        executor = Executor({})
        result = run_result(
            OfficialOracleExperimentHarness(gate()).run(
                (case(replicates=1),),
                executor,
                lane_id="official_mocked_contract",
                execution_mode=OfficialOracleExecutionMode.MOCKED,
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        )
        self.assertEqual(result.report.status, OfficialOracleExperimentReportStatus.FAILED)
        self.assertEqual(
            result.report.results[0].pair_status, OfficialOracleExperimentPairStatus.FAILED
        )

        with self.assertRaises(OfficialOracleGovernanceError) as denied:
            OfficialOracleExperimentHarness(gate()).run(
                (case(),),
                executor,
                lane_id="official_mocked_contract",
                execution_mode=OfficialOracleExecutionMode.MOCKED,
                now="2026-08-06T00:00:00Z",
                rate_clock=0.0,
            )
        self.assertEqual(denied.exception.code, "terms_not_active")

    def test_report_fingerprint_and_gate_progress_are_reproducible(self) -> None:
        outputs = {(False, 1): FINGERPRINT_A, (True, 1): FINGERPRINT_B}
        first = run_result(
            OfficialOracleExperimentHarness(gate()).run(
                (case(replicates=1),),
                Executor(outputs),
                lane_id="official_mocked_contract",
                execution_mode=OfficialOracleExecutionMode.MOCKED,
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        )
        second = run_result(
            OfficialOracleExperimentHarness(gate()).run(
                (case(replicates=1),),
                Executor(outputs),
                lane_id="official_mocked_contract",
                execution_mode=OfficialOracleExecutionMode.MOCKED,
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        )
        self.assertEqual(first.report.fingerprint, second.report.fingerprint)
        self.assertEqual(first.next_gate.usage.calls, 2)
        self.assertEqual(first.report.missing_observation_count, 0)

    def test_offline_local_relation_is_explicit_and_does_not_consume_api_budget(self) -> None:
        outputs = {(False, 1): FINGERPRINT_A, (True, 1): FINGERPRINT_A}
        result = run_result(
            OfficialOracleExperimentHarness(gate()).run(
                (case(replicates=1),),
                Executor(outputs),
                lane_id="official_live_oracle",
                execution_mode=OfficialOracleExecutionMode.OFFLINE,
                now="2026-08-06T00:00:00Z",
                rate_clock=0.0,
            )
        )
        self.assertEqual(result.report.status, OfficialOracleExperimentReportStatus.COMPLETE)
        self.assertEqual(result.report.claim_ceiling, OfficialOracleClaimCeiling.NO_CLAIM)
        self.assertEqual(result.next_gate.usage.calls, 0)
        self.assertEqual(
            result.report.results[0].pair_status, OfficialOracleExperimentPairStatus.MATCHED
        )

    def test_negative_control_may_reuse_the_same_input_fingerprint(self) -> None:
        item = OfficialOracleExperimentCase(
            "case.negative",
            OfficialOracleExperimentFactor.WORDING,
            FINGERPRINT_A,
            FINGERPRINT_A,
            (OfficialOracleExperimentFactor.WORDING.value,),
            (FINGERPRINT_C,),
            ("hypothesis.negative",),
            1,
            True,
        )
        self.assertTrue(item.negative_control)


if __name__ == "__main__":
    unittest.main()
