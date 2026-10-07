"""M9-04 official-oracle terms, consent, retention, budget, and abort gates."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import cast

from comfyui_h3_context.core import (
    OfficialOracleAbortCondition,
    OfficialOracleAdmission,
    OfficialOracleBudgetPolicy,
    OfficialOracleClaimCeiling,
    OfficialOracleDecision,
    OfficialOracleExecutionGate,
    OfficialOracleExecutionMode,
    OfficialOracleExecutionScope,
    OfficialOracleGovernancePolicy,
    OfficialOracleLaneDisposition,
    OfficialOracleLaneStatus,
    OfficialOracleMediaPrivacy,
    OfficialOracleOperation,
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
from comfyui_h3_context.core.errors import OfficialOracleGovernanceError


def terms(**changes: object) -> OfficialOracleTermsDecision:
    values: dict[str, object] = {
        "review_id": "terms-review-2026-08-07",
        "source_ledger_id": "m9_04_source_ledger",
        "terms_revision": "terms.rev_1",
        "reviewed_at": "2026-08-07T00:00:00Z",
        "valid_until": "2026-08-08T00:00:00Z",
        "reviewer_reference": "operator.m9_04",
        "status": OfficialOracleReviewStatus.APPROVED_CONTROLLED_RESEARCH,
        "source_references": (
            "official.general-terms",
            "official.product-terms",
            "official.privacy",
            "official.api-lifecycle",
            "official.deletion",
            "official.pricing",
            "official.account-order",
            "official.supplemental",
        ),
        "lane_dispositions": (
            OfficialOracleLaneDisposition(
                "official_live_oracle",
                OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
                OfficialOracleExecutionScope.OFFLINE_ONLY,
                OfficialOracleClaimCeiling.NO_CLAIM,
                OfficialOracleRetentionPolicy.NONE,
                "review.unavailable",
            ),
            OfficialOracleLaneDisposition(
                "official_mocked_contract",
                OfficialOracleLaneStatus.LIMITED,
                OfficialOracleExecutionScope.MOCKED_ONLY,
                OfficialOracleClaimCeiling.STRUCTURAL_ONLY,
                OfficialOracleRetentionPolicy.HASH_ONLY,
                "review.mocked-only",
            ),
            OfficialOracleLaneDisposition(
                "recorded_oracle_fixture",
                OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
                OfficialOracleExecutionScope.OFFLINE_ONLY,
                OfficialOracleClaimCeiling.NO_CLAIM,
                OfficialOracleRetentionPolicy.NONE,
                "review.no-recordings",
            ),
            OfficialOracleLaneDisposition(
                "benchmark",
                OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
                OfficialOracleExecutionScope.OFFLINE_ONLY,
                OfficialOracleClaimCeiling.NO_CLAIM,
                OfficialOracleRetentionPolicy.NONE,
                "review.no-benchmark",
            ),
            OfficialOracleLaneDisposition(
                "publication",
                OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
                OfficialOracleExecutionScope.OFFLINE_ONLY,
                OfficialOracleClaimCeiling.NO_CLAIM,
                OfficialOracleRetentionPolicy.NONE,
                "review.no-publication",
            ),
            OfficialOracleLaneDisposition(
                "training_distillation",
                OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
                OfficialOracleExecutionScope.OFFLINE_ONLY,
                OfficialOracleClaimCeiling.NO_CLAIM,
                OfficialOracleRetentionPolicy.NONE,
                "review.no-training",
            ),
        ),
        "api_use": OfficialOracleDecision.ALLOW,
        "output_retention": OfficialOracleDecision.ALLOW,
        "redistribution": OfficialOracleDecision.DENY,
        "benchmarking": OfficialOracleDecision.ALLOW,
        "publication": OfficialOracleDecision.DENY,
        "automated_probing": OfficialOracleDecision.DENY,
        "training_distillation": OfficialOracleDecision.DENY,
    }
    values.update(changes)
    return OfficialOracleTermsDecision(**values)  # type: ignore[arg-type]


def source_ledger(**changes: object) -> OfficialOracleSourceLedger:
    values: dict[str, object] = {
        "ledger_id": "m9_04_source_ledger",
        "ledger_revision": "ledger.rev_1",
        "reviewer_reference": "reviewer.m9_04",
        "approver_reference": "approver.m9_04",
        "jurisdiction": "TW",
        "sources": tuple(
            OfficialOracleSourceRecord(
                source_id=source_id,
                category=category,
                url=f"https://official.example.test/{source_id}",
                revision="retrieved-2026-08-07",
                effective_date="2026-08-07",
                retrieved_at="2026-08-07",
                sha256="a" * 64,
                authority="official",
                jurisdiction="GLOBAL",
                applicability=applicability,
            )
            for source_id, category, applicability in (
                (
                    "official.general-terms",
                    OfficialOracleSourceCategory.GENERAL_TERMS,
                    OfficialOracleSourceApplicability.APPLICABLE,
                ),
                (
                    "official.product-terms",
                    OfficialOracleSourceCategory.PRODUCT_TERMS,
                    OfficialOracleSourceApplicability.APPLICABLE,
                ),
                (
                    "official.privacy",
                    OfficialOracleSourceCategory.PRIVACY_POLICY,
                    OfficialOracleSourceApplicability.APPLICABLE,
                ),
                (
                    "official.api-lifecycle",
                    OfficialOracleSourceCategory.API_LIFECYCLE,
                    OfficialOracleSourceApplicability.APPLICABLE,
                ),
                (
                    "official.deletion",
                    OfficialOracleSourceCategory.DELETION,
                    OfficialOracleSourceApplicability.APPLICABLE,
                ),
                (
                    "official.pricing",
                    OfficialOracleSourceCategory.PRICING,
                    OfficialOracleSourceApplicability.APPLICABLE,
                ),
                (
                    "official.account-order",
                    OfficialOracleSourceCategory.ACCOUNT_ORDER,
                    OfficialOracleSourceApplicability.NOT_APPLICABLE,
                ),
                (
                    "official.supplemental",
                    OfficialOracleSourceCategory.SUPPLEMENTAL,
                    OfficialOracleSourceApplicability.NOT_APPLICABLE,
                ),
            )
        ),
    }
    values.update(changes)
    return OfficialOracleSourceLedger(**values)  # type: ignore[arg-type]


def budget(**changes: object) -> OfficialOracleBudgetPolicy:
    values: dict[str, object] = {
        "max_calls": 2,
        "max_requests_per_window": 1,
        "window_seconds": 10.0,
        "max_spend_minor_units": 5,
        "currency": "usd",
        "max_output_tokens": 100,
    }
    values.update(changes)
    return OfficialOracleBudgetPolicy(**values)  # type: ignore[arg-type]


def policy(**changes: object) -> OfficialOracleGovernancePolicy:
    values: dict[str, object] = {
        "policy_revision": "policy.rev_1",
        "operator_consent": True,
        "consent_reference": "consent.2026_08_07",
        "consent_terms_revision": "terms.rev_1",
        "consent_policy_revision": "policy.rev_1",
        "media_privacy": OfficialOracleMediaPrivacy.USER_CONSENTED,
        "media_upload_consent": True,
        "retention_policy": OfficialOracleRetentionPolicy.HASH_ONLY,
        "deletion_reference": "local.delete-after-capture",
        "abort_on": frozenset(
            {
                OfficialOracleAbortCondition.AUTHENTICATION,
                OfficialOracleAbortCondition.QUOTA,
                OfficialOracleAbortCondition.MODERATION,
                OfficialOracleAbortCondition.TIMEOUT,
                OfficialOracleAbortCondition.CANCELLATION,
            }
        ),
        "budget": budget(),
    }
    values.update(changes)
    return OfficialOracleGovernancePolicy(**values)  # type: ignore[arg-type]


def provider_policy(**changes: object) -> ProviderExecutionPolicy:
    values: dict[str, object] = {
        "provider": ProviderIdentity.OFFICIAL_MINIMAX,
        "privacy_mode": ProviderPrivacyMode.EXPLICIT_REMOTE,
        "offline": False,
        "network_allowed": True,
        "upload_consent": True,
        "credential_reference": "env.official_minimax",
    }
    values.update(changes)
    return ProviderExecutionPolicy(**values)  # type: ignore[arg-type]


def build_gate(
    *,
    terms_value: OfficialOracleTermsDecision | None = None,
    policy_value: OfficialOracleGovernancePolicy | None = None,
    source_ledger_value: OfficialOracleSourceLedger | None = None,
    provider_policy_value: ProviderExecutionPolicy | None = None,
) -> OfficialOracleExecutionGate:
    return OfficialOracleExecutionGate(
        terms_value or terms(),
        policy_value or policy(),
        source_ledger_value or source_ledger(),
        provider_policy_value or provider_policy(),
    )


def admit_kwargs(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "lane_id": "official_mocked_contract",
        "execution_mode": OfficialOracleExecutionMode.MOCKED,
        "operation": OfficialOracleOperation.API_CALL,
        "now": "2026-08-07T01:00:00Z",
        "rate_clock": 0.0,
    }
    values.update(changes)
    return values


def admit_gate(
    execution_gate: OfficialOracleExecutionGate, **changes: object
) -> OfficialOracleAdmission:
    values = admit_kwargs(**changes)
    return execution_gate.admit(
        lane_id=cast(str, values["lane_id"]),
        execution_mode=cast(OfficialOracleExecutionMode, values["execution_mode"]),
        operation=cast(OfficialOracleOperation, values["operation"]),
        now=cast(str, values["now"]),
        rate_clock=cast(float, values["rate_clock"]),
        operation_id=cast(str, values.get("operation_id", "official-oracle")),
        media_present=cast(bool, values.get("media_present", False)),
        media_class=cast(OfficialOracleMediaPrivacy | None, values.get("media_class")),
        estimated_spend_minor_units=cast(int | None, values.get("estimated_spend_minor_units")),
        estimated_output_tokens=cast(int, values.get("estimated_output_tokens", 0)),
    )


class OfficialOracleGovernanceTests(unittest.TestCase):
    def test_source_ledger_resolves_all_categories_and_has_stable_fingerprint(self) -> None:
        ledger = source_ledger()
        resolved = ledger.resolve(terms().source_references)
        self.assertEqual(len(resolved), 8)
        self.assertEqual(len(ledger.fingerprint), 64)
        self.assertEqual(ledger.fingerprint, source_ledger().fingerprint)
        with self.assertRaises(OfficialOracleGovernanceError) as failure:
            ledger.resolve(("missing.source",))
        self.assertEqual(failure.exception.code, "source_unresolved")

    def test_terms_are_dated_source_attributed_and_wire_safe(self) -> None:
        decision = terms()
        wire = decision.to_public_dict()
        encoded = json.dumps(wire, sort_keys=True)
        self.assertEqual(wire["schema"], "h3.context.ir.governance.v1")
        self.assertEqual(wire["status"], "approved_controlled_research")
        self.assertEqual(wire["api_use"], "allow")
        source_refs = cast(list[object], wire["source_references"])
        self.assertEqual(len(source_refs), 8)
        self.assertNotIn("https://", encoded)
        self.assertNotIn("token", encoded.casefold())
        self.assertNotIn("password", encoded.casefold())

    def test_lane_map_is_explicit_and_live_lane_is_not_admitted_by_limited_fixture(self) -> None:
        limited = terms().disposition_for("official_mocked_contract")
        self.assertEqual(limited.status, OfficialOracleLaneStatus.LIMITED)
        self.assertEqual(limited.claim_ceiling, OfficialOracleClaimCeiling.STRUCTURAL_ONLY)
        live = terms().disposition_for("official_live_oracle")
        self.assertEqual(live.status, OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED)
        with self.assertRaises(OfficialOracleGovernanceError) as failure:
            admit_gate(
                build_gate(),
                lane_id="official_live_oracle",
                execution_mode=OfficialOracleExecutionMode.LIVE,
            )
        self.assertEqual(failure.exception.code, "lane_execution_not_permitted")

    def test_all_lane_statuses_have_deterministic_scope_and_claim_ceiling(self) -> None:
        self.assertTrue(
            OfficialOracleLaneDisposition(
                "lane.authorized",
                OfficialOracleLaneStatus.AUTHORIZED,
                OfficialOracleExecutionScope.LIVE_AND_MOCKED,
                OfficialOracleClaimCeiling.ORACLE_EVIDENCE,
                OfficialOracleRetentionPolicy.RETAIN_RESTRICTED,
                "review.authorized",
            ).allows(OfficialOracleExecutionMode.LIVE)
        )
        self.assertTrue(
            OfficialOracleLaneDisposition(
                "lane.limited",
                OfficialOracleLaneStatus.LIMITED,
                OfficialOracleExecutionScope.RECORDED_ONLY,
                OfficialOracleClaimCeiling.RECORDED_EVIDENCE,
                OfficialOracleRetentionPolicy.DELETE_AFTER_CAPTURE,
                "review.limited",
            ).allows(OfficialOracleExecutionMode.RECORDED)
        )
        for status in (
            OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
            OfficialOracleLaneStatus.EXHAUSTED_SUSPENDED,
            OfficialOracleLaneStatus.EXPIRED,
            OfficialOracleLaneStatus.REAUTHORIZATION_REQUIRED,
        ):
            disposition = OfficialOracleLaneDisposition(
                f"lane.{status.value}",
                status,
                OfficialOracleExecutionScope.OFFLINE_ONLY,
                OfficialOracleClaimCeiling.NO_CLAIM,
                OfficialOracleRetentionPolicy.NONE,
                f"review.{status.value}",
            )
            with self.subTest(status=status):
                self.assertTrue(disposition.allows(OfficialOracleExecutionMode.OFFLINE))
                self.assertFalse(disposition.allows(OfficialOracleExecutionMode.MOCKED))
                self.assertEqual(disposition.claim_ceiling, OfficialOracleClaimCeiling.NO_CLAIM)

    def test_consent_is_execution_scoped_and_revision_bound(self) -> None:
        with self.assertRaises(OfficialOracleGovernanceError) as missing:
            admit_gate(
                build_gate(policy_value=policy(operator_consent=True, consent_reference=None))
            )
        self.assertEqual(missing.exception.code, "consent_binding_required")
        with self.assertRaises(OfficialOracleGovernanceError) as mismatch:
            admit_gate(build_gate(policy_value=policy(consent_terms_revision="terms.old")))
        self.assertEqual(mismatch.exception.code, "consent_revision_mismatch")

    def test_unresolved_source_review_is_distinct_from_terms_denial(self) -> None:
        with self.assertRaises(OfficialOracleGovernanceError) as failure:
            admit_gate(
                build_gate(
                    source_ledger_value=source_ledger(unresolved_questions=("question.open",))
                )
            )
        self.assertEqual(failure.exception.code, "source_review_incomplete")

    def test_provider_policy_and_lane_retention_are_checked_before_admission(self) -> None:
        with self.assertRaises(OfficialOracleGovernanceError) as provider_failure:
            admit_gate(
                build_gate(
                    provider_policy_value=provider_policy(offline=True, network_allowed=False)
                )
            )
        self.assertEqual(provider_failure.exception.code, "provider_policy_rejected")

        with self.assertRaises(OfficialOracleGovernanceError) as retention_failure:
            admit_gate(
                build_gate(
                    policy_value=policy(
                        retention_policy=OfficialOracleRetentionPolicy.DELETE_AFTER_CAPTURE
                    )
                )
            )
        self.assertEqual(retention_failure.exception.code, "retention_disposition_mismatch")

    def test_public_gate_receipt_excludes_provider_credential_reference(self) -> None:
        wire = json.dumps(build_gate().to_public_dict(), sort_keys=True)
        self.assertNotIn("env.official_minimax", wire)
        self.assertNotIn("credential", wire.casefold())

    def test_abort_rules_are_executable_and_terminal(self) -> None:
        execution_gate = build_gate().abort_for(OfficialOracleAbortCondition.QUOTA)
        self.assertTrue(execution_gate.aborted)
        with self.assertRaises(OfficialOracleGovernanceError) as failure:
            admit_gate(execution_gate)
        self.assertEqual(failure.exception.code, "aborted")

    def test_negative_terms_cannot_call_a_transport(self) -> None:
        class Transport:
            calls = 0

            def create(self) -> None:
                self.calls += 1

        transport = Transport()
        with self.assertRaises(OfficialOracleGovernanceError):
            OfficialOracleExecutionGate(
                terms(status=OfficialOracleReviewStatus.DO_NOT_IMPLEMENT),
                policy(),
                source_ledger(),
                provider_policy(),
            ).admit(
                lane_id="official_mocked_contract",
                execution_mode=OfficialOracleExecutionMode.MOCKED,
                operation=OfficialOracleOperation.API_CALL,
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        self.assertEqual(transport.calls, 0)

    def test_unreviewed_or_denied_terms_never_admit(self) -> None:
        for status in (
            OfficialOracleReviewStatus.NOT_REVIEWED,
            OfficialOracleReviewStatus.DENIED,
            OfficialOracleReviewStatus.DO_NOT_IMPLEMENT,
        ):
            gate = OfficialOracleExecutionGate(
                terms(status=status), policy(), source_ledger(), provider_policy()
            )
            with (
                self.subTest(status=status),
                self.assertRaises(OfficialOracleGovernanceError) as failure,
            ):
                admit_gate(gate)
            self.assertEqual(failure.exception.code, "terms_not_approved")

    def test_media_requires_privacy_upload_consent_and_deletion(self) -> None:
        cases = (
            (policy(media_upload_consent=False), "media_consent_required"),
            (
                policy(
                    retention_policy=OfficialOracleRetentionPolicy.NONE, deletion_reference=None
                ),
                "deletion_policy_required",
            ),
            (
                policy(media_privacy=OfficialOracleMediaPrivacy.SYNTHETIC_ONLY),
                "media_privacy_mismatch",
            ),
        )
        for governance, code in cases:
            gate = OfficialOracleExecutionGate(
                terms(), governance, source_ledger(), provider_policy()
            )
            with (
                self.subTest(code=code),
                self.assertRaises(OfficialOracleGovernanceError) as failure,
            ):
                admit_gate(
                    gate,
                    media_present=True,
                    media_class=OfficialOracleMediaPrivacy.USER_CONSENTED,
                )
            self.assertEqual(failure.exception.code, code)

        with self.assertRaises(OfficialOracleGovernanceError) as provider_failure:
            admit_gate(
                build_gate(provider_policy_value=provider_policy(upload_consent=False)),
                media_present=True,
                media_class=OfficialOracleMediaPrivacy.USER_CONSENTED,
            )
        self.assertEqual(provider_failure.exception.code, "provider_media_consent_required")

    def test_budget_and_rate_caps_are_bounded_and_persistent(self) -> None:
        execution_gate = build_gate()
        first = admit_gate(
            execution_gate, estimated_spend_minor_units=2, estimated_output_tokens=10
        )
        self.assertEqual(first.usage.calls, 1)
        self.assertEqual(first.usage.spend_minor_units, 2)
        with self.assertRaises(OfficialOracleGovernanceError) as rate_failure:
            admit_gate(first.next_gate, now="2026-08-07T01:00:01Z", rate_clock=1.0)
        self.assertEqual(rate_failure.exception.code, "rate_limit")

        second = admit_gate(
            first.next_gate,
            now="2026-08-07T01:00:11Z",
            rate_clock=11.0,
            estimated_spend_minor_units=3,
            estimated_output_tokens=20,
        )
        self.assertEqual(second.usage.calls, 2)
        self.assertEqual(second.usage.spend_minor_units, 5)
        with self.assertRaises(OfficialOracleGovernanceError) as quota_failure:
            admit_gate(second.next_gate, now="2026-08-07T01:00:21Z", rate_clock=21.0)
        self.assertEqual(quota_failure.exception.code, "call_quota")

    def test_abort_is_terminal_and_does_not_reset_budget(self) -> None:
        execution_gate = build_gate().abort("operator_cancelled")
        with self.assertRaises(OfficialOracleGovernanceError) as failure:
            admit_gate(execution_gate)
        self.assertEqual(failure.exception.code, "aborted")
        self.assertEqual(execution_gate.to_public_dict()["abort_reason"], "operator_cancelled")

    def test_expiry_and_disallowed_operation_are_distinguishable(self) -> None:
        with self.assertRaises(OfficialOracleGovernanceError) as expired:
            admit_gate(build_gate(), now="2026-08-09T00:00:00Z")
        self.assertEqual(expired.exception.code, "terms_expired")

        with self.assertRaises(OfficialOracleGovernanceError) as denied:
            admit_gate(build_gate(), operation=OfficialOracleOperation.PUBLICATION)
        self.assertEqual(denied.exception.code, "operation_not_permitted")

    def test_live_admission_requires_an_explicit_spend_estimate(self) -> None:
        base_terms = terms()
        authorized_terms = replace(
            base_terms,
            lane_dispositions=(
                base_terms.lane_dispositions[0],
                OfficialOracleLaneDisposition(
                    "official_mocked_contract",
                    OfficialOracleLaneStatus.AUTHORIZED,
                    OfficialOracleExecutionScope.LIVE_AND_MOCKED,
                    OfficialOracleClaimCeiling.ORACLE_EVIDENCE,
                    OfficialOracleRetentionPolicy.RETAIN_RESTRICTED,
                    "review.authorized",
                ),
                *base_terms.lane_dispositions[2:],
            ),
        )
        gate = build_gate(
            terms_value=authorized_terms,
            policy_value=policy(retention_policy=OfficialOracleRetentionPolicy.RETAIN_RESTRICTED),
        )
        with self.assertRaises(OfficialOracleGovernanceError) as failure:
            admit_gate(gate, execution_mode=OfficialOracleExecutionMode.LIVE)
        self.assertEqual(failure.exception.code, "spend_estimate_required")
        self.assertEqual(gate.usage.calls, 0)

    def test_spend_and_output_token_overages_fail_without_consuming_budget(self) -> None:
        execution_gate = build_gate()
        with self.assertRaises(OfficialOracleGovernanceError) as spend:
            admit_gate(execution_gate, estimated_spend_minor_units=6)
        self.assertEqual(spend.exception.code, "spend_quota")
        usage = cast(dict[str, object], execution_gate.to_public_dict()["usage"])
        self.assertEqual(usage["calls"], 0)

        with self.assertRaises(OfficialOracleGovernanceError) as tokens:
            admit_gate(execution_gate, estimated_output_tokens=101)
        self.assertEqual(tokens.exception.code, "output_token_quota")
        usage = cast(dict[str, object], execution_gate.to_public_dict()["usage"])
        self.assertEqual(usage["calls"], 0)

    def test_secret_like_references_and_invalid_limits_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            terms(source_references=("https://private.example.test/terms",))
        with self.assertRaises(ValueError):
            policy(deletion_reference="secret-token")
        with self.assertRaises(ValueError):
            budget(max_calls=0)


if __name__ == "__main__":
    unittest.main()
