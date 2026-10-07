"""M9-07 frozen baseline and program-envelope contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path

from comfyui_h3_context.core import (
    BaselineEvidenceLayer,
    BaselineObservation,
    BaselineOutcome,
    BaselineReportStatus,
    ProgramEnvelope,
    ProgramEnvelopeCapability,
    ProgramEnvelopeCapabilityMaturity,
    ProgramEnvelopeDecision,
    ProgramEnvelopeGate,
    ProgramEnvelopeLaneLimits,
    ProgramEnvelopeLimits,
    ProgramEnvelopeReauthorizationTrigger,
    ProgramEnvelopeRegressionPriority,
    ProgramEnvelopeResource,
    ProgramEnvelopeStopRule,
    ReconstructionBaselineCase,
    ReconstructionBaselineRunner,
    ReconstructionErrorTaxonomyEntry,
    ReconstructionRegressionItem,
)
from comfyui_h3_context.core.errors import ContractValidationError, ProgramEnvelopeError

FINGERPRINT_A = "sha256:" + "a" * 64
FINGERPRINT_B = "sha256:" + "b" * 64
FINGERPRINT_C = "sha256:" + "c" * 64


def case(case_id: str = "case.1") -> ReconstructionBaselineCase:
    return ReconstructionBaselineCase(
        case_id=case_id,
        corpus_id="m9-03-synthetic-governance",
        partition_id="test",
        task_mode="fl2va",
        language="multilingual",
        risk_classes=("resource", "missing_media"),
        input_fingerprints=(FINGERPRINT_A,),
        source_fingerprints=(FINGERPRINT_C,),
    )


def envelope() -> ProgramEnvelope:
    return ProgramEnvelope(
        envelope_id="m9-07-envelope",
        revision="envelope.rev_1",
        effective_date="2026-08-07",
        expires_at="2026-08-14",
        limits=ProgramEnvelopeLimits(
            max_calls=2,
            max_spend_minor_units=0,
            currency="usd",
            max_compute_seconds=60,
            max_storage_bytes=1024,
            max_retention_days=7,
            max_reviewer_minutes=30,
            max_candidates=2,
            max_wall_seconds=120,
        ),
        lane_limits=(
            ProgramEnvelopeLaneLimits(
                lane_id="manual_baseline",
                max_calls=1,
                max_spend_minor_units=0,
                max_compute_seconds=60,
                max_wall_seconds=120,
            ),
        ),
        capabilities=(
            ProgramEnvelopeCapability(
                capability_id="manual.baseline",
                lane_id="manual_baseline",
                maturity=ProgramEnvelopeCapabilityMaturity.MANDATORY,
                rationale="deterministic structural baseline",
            ),
            ProgramEnvelopeCapability(
                capability_id="oracle.live",
                lane_id="official_live_oracle",
                maturity=ProgramEnvelopeCapabilityMaturity.UNSUPPORTED,
                rationale="reviewed lane unavailable",
            ),
        ),
        stop_rules=(
            ProgramEnvelopeStopRule(
                rule_id="stop.calls",
                resource=ProgramEnvelopeResource.CALLS,
                threshold=2,
                decision=ProgramEnvelopeDecision.STOP,
                rationale="no unreviewed expansion",
            ),
        ),
        marginal_value_rules=("marginal_value.review_before_expansion",),
        reauthorization_triggers=(
            ProgramEnvelopeReauthorizationTrigger(
                trigger_id="reauth.expiry",
                trigger_kind="date_expiry",
                due_date="2026-08-14",
                rationale="renew before the envelope expires",
            ),
        ),
    )


class Executor:
    def __init__(self, outcomes: Mapping[BaselineEvidenceLayer, BaselineObservation]) -> None:
        self.outcomes = outcomes
        self.calls: list[BaselineEvidenceLayer] = []

    def execute(
        self,
        baseline_case: ReconstructionBaselineCase,
        *,
        layer: BaselineEvidenceLayer,
    ) -> BaselineObservation:
        self.calls.append(layer)
        return self.outcomes[layer]


class ReconstructionBaselineTests(unittest.TestCase):
    def test_case_closes_all_evidence_layers_and_rejects_raw_metadata(self) -> None:
        item = case()
        self.assertEqual(item.required_layers, tuple(BaselineEvidenceLayer))
        self.assertEqual(item.to_public_dict()["schema"], "h3.reconstruction.baseline.v1")
        with self.assertRaises(ContractValidationError):
            ReconstructionBaselineCase(
                "case.url",
                "corpus",
                "test",
                "fl2va",
                "multilingual",
                ("resource",),
                ("https://private.invalid/media",),
                (FINGERPRINT_C,),
            )

    def test_runner_keeps_structural_pass_separate_from_missing_runtime_layers(self) -> None:
        outcomes: dict[BaselineEvidenceLayer, BaselineObservation] = {}
        for layer in BaselineEvidenceLayer:
            if layer is BaselineEvidenceLayer.STRUCTURAL:
                outcomes[layer] = BaselineObservation(
                    "case.1",
                    layer,
                    BaselineOutcome.PASSED,
                    "manual_baseline",
                    (FINGERPRINT_A,),
                    metrics=(("hard_constraints", "1"),),
                )
            else:
                outcomes[layer] = BaselineObservation(
                    "case.1",
                    layer,
                    BaselineOutcome.MISSING,
                    "unavailable_lane",
                    (FINGERPRINT_A,),
                    diagnostic_codes=("lane.unavailable",),
                )
        result = ReconstructionBaselineRunner().run(
            (case(),),
            Executor(outcomes),
            envelope=envelope(),
            baseline_id="baseline.1",
            baseline_revision="baseline.rev_1",
            as_of="2026-08-07",
        )
        self.assertEqual(result.report.status, BaselineReportStatus.PARTIAL)
        self.assertEqual(result.report.claim_ceiling, "structural_only")
        self.assertEqual(result.report.missing_observation_count, 7)
        self.assertEqual(result.report.layer_summary[0].passed, 1)
        self.assertEqual(result.report.layer_summary[1].missing, 1)
        self.assertEqual(len(result.next_gate.usage.lane_usage), 1)

    def test_failure_taxonomy_and_regressions_are_ordered_and_redacted(self) -> None:
        taxonomy = (
            ReconstructionErrorTaxonomyEntry(
                "lane.unavailable",
                BaselineEvidenceLayer.PERCEPTION,
                "missing",
                "selected perception lane is unavailable",
            ),
        )
        regression = (
            ReconstructionRegressionItem(
                "regression.perception",
                "lane.unavailable",
                BaselineEvidenceLayer.PERCEPTION,
                ProgramEnvelopeRegressionPriority.P0,
                "open",
                1,
                "missing perception blocks grounded claims",
                "qualify a bounded local adapter",
            ),
        )
        self.assertEqual(taxonomy[0].to_public_dict()["layer"], "perception")
        self.assertEqual(regression[0].priority, ProgramEnvelopeRegressionPriority.P0)

    def test_envelope_stops_lane_without_expanding_or_blocking_other_lane(self) -> None:
        first = ProgramEnvelopeGate(envelope()).admit(
            lane_id="manual_baseline",
            capability_id="manual.baseline",
            operation_id="op.1",
            as_of="2026-08-07",
            calls=1,
        )
        self.assertEqual(first.decision, ProgramEnvelopeDecision.CONTINUE)
        stopped = ProgramEnvelopeGate(envelope(), first.next_gate.usage).admit(
            lane_id="manual_baseline",
            capability_id="manual.baseline",
            operation_id="op.2",
            as_of="2026-08-07",
            calls=1,
        )
        self.assertEqual(stopped.decision, ProgramEnvelopeDecision.STOP)
        self.assertEqual(stopped.next_gate.usage.calls, 1)
        other = ProgramEnvelopeGate(envelope(), first.next_gate.usage).admit(
            lane_id="official_live_oracle",
            capability_id="oracle.live",
            operation_id="op.3",
            as_of="2026-08-07",
            calls=1,
        )
        self.assertEqual(other.decision, ProgramEnvelopeDecision.STOP)
        self.assertEqual(other.reason_code, "capability_unsupported")

    def test_envelope_expiry_and_invalid_usage_fail_closed(self) -> None:
        with self.assertRaises(ProgramEnvelopeError) as expired:
            ProgramEnvelopeGate(envelope()).admit(
                lane_id="manual_baseline",
                capability_id="manual.baseline",
                operation_id="op.expired",
                as_of="2026-08-14",
                calls=1,
            )
        self.assertEqual(expired.exception.code, "envelope_expired")
        with self.assertRaises(ContractValidationError):
            ProgramEnvelopeLimits(
                max_calls=1,
                max_spend_minor_units=0,
                currency="usd",
                max_compute_seconds=0,
                max_storage_bytes=0,
                max_retention_days=0,
                max_reviewer_minutes=0,
                max_candidates=0,
                max_wall_seconds=0,
            )

    def test_report_fingerprint_is_reproducible_and_public_projection_has_no_raw(self) -> None:
        missing = {
            layer: BaselineObservation(
                "case.1",
                layer,
                BaselineOutcome.MISSING,
                "unavailable_lane",
                (FINGERPRINT_A,),
                diagnostic_codes=("lane.unavailable",),
            )
            for layer in BaselineEvidenceLayer
        }
        first = ReconstructionBaselineRunner().run(
            (case(),),
            Executor(missing),
            envelope=envelope(),
            baseline_id="baseline.1",
            baseline_revision="baseline.rev_1",
            as_of="2026-08-07",
        )
        second = ReconstructionBaselineRunner().run(
            (case(),),
            Executor(missing),
            envelope=envelope(),
            baseline_id="baseline.1",
            baseline_revision="baseline.rev_1",
            as_of="2026-08-07",
        )
        self.assertEqual(first.report.fingerprint, second.report.fingerprint)
        encoded = json.dumps(first.report.to_public_dict(), sort_keys=True)
        self.assertNotIn("https://", encoded)
        self.assertNotIn("raw", encoded.casefold())

    def test_metadata_fixture_cli_reports_explicit_missing_layers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / "reconstruction_baseline.py"),
                "--baseline",
                str(root / "tests" / "fixtures" / "m9_07_baseline_envelope.json"),
                "--json",
            ],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["report_status"], "partial")
        self.assertEqual(payload["missing_observation_count"], 7)
        self.assertEqual(payload["claim_ceiling"], "structural_only")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
