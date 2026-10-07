"""M22-14 typed, content-free evidence for later remote catalog promotion."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from historical_prompt_model_fixtures import historical_policy_facts as policy_for_profile
from historical_prompt_model_fixtures import load_historical_catalog as load_prompt_model_catalog

from comfyui_h3_context.core.prompt_model_provider import (
    PROMPT_MODEL_CATALOG_V3_SCHEMA,
    REMOTE_QUALIFICATION_RESULT_SCHEMA,
    PromptModelContractError,
    PromptModelQualificationState,
    RemotePromptModelQualificationEvidence,
    build_remote_qualification_evidence,
    decode_prompt_model_catalog,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PROMPT_MODEL_CATALOG_V5_SCHEMA as PROMPT_MODEL_CATALOG_SCHEMA,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.remote_provider_policy import (
    policy_for_profile as active_policy_for_profile,
)

EVIDENCE_SHA = "sha256:" + "a" * 64
COMMIT_OID = "b" * 40
TREE_OID = "c" * 40


def remote_profile() -> PromptModelProfile:
    return load_prompt_model_catalog().require("openai.gpt_5_6_terra.remote")


def live_result(**overrides: object) -> dict[str, object]:
    profile = remote_profile()
    policy = policy_for_profile(profile)
    values: dict[str, object] = {
        "schema": REMOTE_QUALIFICATION_RESULT_SCHEMA,
        "mode": "live",
        "status": "PASS",
        "provider_id": policy.provider_id,
        "profile_id": profile.profile_id,
        "model_id": profile.model_id,
        "adapter_version": profile.adapter_version,
        "parser_version": profile.parser_version,
        "policy_version": policy.policy_version,
        "policy_sha256": policy.fingerprint,
        "price_basis_id": policy.price_basis_id,
        "price_checked_on": policy.price_checked_on.isoformat(),
        "price_valid_through": policy.price_valid_through.isoformat(),
        "observed_on": "2026-08-23",
        "repository_commit": COMMIT_OID,
        "repository_tree": TREE_OID,
        "max_transmissions": policy.max_transmissions,
        "observed_transmissions": policy.max_transmissions,
        "max_input_tokens": policy.max_input_tokens,
        "max_output_tokens": policy.max_output_tokens,
        "max_cost_micro_usd": policy.max_cost_micro_usd,
        "outcome_id": "prompt_model.ok",
        "schema_valid": True,
        "receipt_valid": True,
        "receipt": {
            "schema": "h3.remote.prompt_model.qualification_receipt.v1",
            "profile_id": profile.profile_id,
            "outcome_id": "prompt_model.ok",
            "http_status": 200,
            "request_bytes": 512,
            "response_bytes": 256,
            "prompt_tokens": 96,
            "completion_tokens": 32,
            "duration_ms": 1,
            "provider_id": policy.provider_id,
            "model_id": profile.model_id,
            "policy_sha256": policy.fingerprint,
            "price_basis_id": policy.price_basis_id,
            "maximum_cost_micro_usd": 512,
            "actual_cost_micro_usd": 312,
            "usage_present": True,
        },
        "catalog_promotion_performed": False,
    }
    values.update(overrides)
    return values


def receipt_with_extra_key() -> dict[str, object]:
    receipt = live_result()["receipt"]
    assert isinstance(receipt, dict)
    return {**receipt, "host": "api.example.test"}


class RemoteQualificationEvidenceTests(unittest.TestCase):
    def test_current_receipts_reduce_and_decode_after_legacy_price_expiry(self) -> None:
        catalog = load_prompt_model_catalog()
        for profile_id in (
            "openai.gpt_5_6_terra.remote",
            "gemini.gemini_3_7_flash.remote",
            "anthropic.claude_sonnet_4_6.remote",
        ):
            profile = catalog.require(profile_id)
            policy = policy_for_profile(profile)
            values = live_result(
                provider_id=policy.provider_id,
                profile_id=profile.profile_id,
                model_id=policy.model_id,
                policy_version=policy.policy_version,
                policy_sha256=policy.fingerprint,
                price_basis_id=policy.price_basis_id,
                price_checked_on=policy.price_checked_on.isoformat(),
                price_valid_through=policy.price_valid_through.isoformat(),
                observed_on="2030-01-01",
                adapter_version=profile.adapter_version,
                parser_version=profile.parser_version,
            )
            receipt = values["receipt"]
            assert isinstance(receipt, dict)
            receipt.update(
                provider_id=policy.provider_id,
                profile_id=profile.profile_id,
                model_id=policy.model_id,
                policy_sha256=policy.fingerprint,
                price_basis_id="",
                maximum_cost_micro_usd=0,
                actual_cost_micro_usd=0,
                usage_present=False,
                prompt_tokens=0,
                completion_tokens=0,
            )
            with self.subTest(profile=profile.profile_id):
                evidence = build_remote_qualification_evidence(
                    values,
                    qualification_sha256=EVIDENCE_SHA,
                    family=profile.family,
                )
                self.assertEqual(evidence.qualified_on, "2030-01-01")
                self.assertEqual(evidence.actual_cost_micro_usd, 0)
                self.assertEqual(evidence.prompt_tokens, 0)
                self.assertEqual(evidence.completion_tokens, 0)
                qualified = replace(
                    profile,
                    qualification_state=PromptModelQualificationState.QUALIFIED,
                    qualification_evidence=evidence,
                )
                with self.assertRaises(PromptModelContractError):
                    active_policy_for_profile(qualified)
                decoded = decode_prompt_model_catalog(
                    json.dumps(
                        {
                            "schema": PROMPT_MODEL_CATALOG_SCHEMA,
                            "default_profile_id": None,
                            "profiles": [qualified.to_wire()],
                        }
                    ).encode("utf-8")
                )
                self.assertEqual(decoded.profiles[0].qualification_evidence, evidence)
                self.assertTrue(
                    set(evidence.to_wire()).isdisjoint(
                        {
                            "host",
                            "prompt",
                            "prompt_text",
                            "response",
                            "credential",
                            "request_id",
                        }
                    )
                )

    def test_legacy_price_and_usage_caps_do_not_admit_or_reject_evidence(self) -> None:
        values = live_result(observed_on="2020-01-01")
        receipt = values["receipt"]
        assert isinstance(receipt, dict)
        policy = policy_for_profile(remote_profile())
        receipt.update(
            price_basis_id="",
            maximum_cost_micro_usd=0,
            actual_cost_micro_usd=policy.max_cost_micro_usd + 1,
            prompt_tokens=policy.max_input_tokens + 1,
            completion_tokens=policy.max_output_tokens + 1,
        )
        evidence = build_remote_qualification_evidence(values, qualification_sha256=EVIDENCE_SHA)
        self.assertEqual(evidence.qualified_on, "2020-01-01")
        self.assertEqual(evidence.actual_cost_micro_usd, policy.max_cost_micro_usd + 1)
        self.assertEqual(evidence.prompt_tokens, policy.max_input_tokens + 1)
        self.assertEqual(evidence.completion_tokens, policy.max_output_tokens + 1)

    def test_current_receipt_schema_and_transport_guards_remain_strict(self) -> None:
        for field, invalid in (
            ("usage_present", 1),
            ("prompt_tokens", True),
            ("actual_cost_micro_usd", True),
            ("maximum_cost_micro_usd", -1),
            ("profile_id", "other-profile"),
            ("provider_id", "other-provider"),
            ("policy_sha256", EVIDENCE_SHA),
            ("request_bytes", 0),
            ("response_bytes", 0),
            ("http_status", 201),
            ("outcome_id", "prompt_model.quota"),
        ):
            values = live_result()
            receipt = values["receipt"]
            assert isinstance(receipt, dict)
            receipt[field] = invalid
            with self.subTest(field=field), self.assertRaises(PromptModelContractError):
                build_remote_qualification_evidence(values, qualification_sha256=EVIDENCE_SHA)

    def test_live_pass_reduces_to_a_strict_content_free_catalog_claim(self) -> None:
        evidence = build_remote_qualification_evidence(
            live_result(), qualification_sha256=EVIDENCE_SHA
        )

        self.assertIsInstance(evidence, RemotePromptModelQualificationEvidence)
        wire = evidence.to_wire()
        self.assertEqual(wire["kind"], "remote_openai_compatible")
        self.assertEqual(wire["qualification_sha256"], EVIDENCE_SHA)
        self.assertEqual(wire["repository_commit"], COMMIT_OID)
        self.assertEqual(wire["repository_tree"], TREE_OID)
        self.assertTrue(
            set(wire).isdisjoint(
                {"prompt", "prompt_text", "response", "credential", "request_id", "host"}
            )
        )

        profile = replace(
            remote_profile(),
            qualification_state=PromptModelQualificationState.QUALIFIED,
            qualification_evidence=evidence,
        )
        with self.assertRaises(PromptModelContractError):
            active_policy_for_profile(profile)

    def test_non_live_non_pass_or_structurally_drifted_results_cannot_mint_evidence(self) -> None:
        cases = (
            live_result(mode="hermetic"),
            live_result(status="FAIL"),
            live_result(observed_transmissions=1),
            live_result(max_cost_micro_usd="10000"),
            live_result(receipt_valid=False),
            live_result(receipt=receipt_with_extra_key()),
            {**live_result(), "unexpected": True},
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(PromptModelContractError):
                build_remote_qualification_evidence(payload, qualification_sha256=EVIDENCE_SHA)

    def test_four_send_ceiling_accepts_observed_identity_and_completion_without_fabricating_sends(
        self,
    ) -> None:
        for observed in (2, 3, 4):
            evidence = build_remote_qualification_evidence(
                live_result(max_transmissions=4, observed_transmissions=observed),
                qualification_sha256=EVIDENCE_SHA,
            )
            self.assertEqual(evidence.max_transmissions, 4)
            self.assertEqual(evidence.observed_transmissions, observed)
        for observed in (False, 1, 5):
            with self.assertRaises(PromptModelContractError):
                build_remote_qualification_evidence(
                    live_result(max_transmissions=4, observed_transmissions=observed),
                    qualification_sha256=EVIDENCE_SHA,
                )

    def test_v4_decodes_remote_evidence_while_v3_cannot_claim_it(self) -> None:
        evidence = build_remote_qualification_evidence(
            live_result(), qualification_sha256=EVIDENCE_SHA
        )
        row = remote_profile().to_wire()
        row["qualification_state"] = "qualified"
        row["qualification_evidence"] = evidence.to_wire()
        payload = {
            "schema": PROMPT_MODEL_CATALOG_SCHEMA,
            "default_profile_id": None,
            "profiles": [row],
        }
        decoded = decode_prompt_model_catalog(json.dumps(payload).encode("utf-8"))
        self.assertIsInstance(
            decoded.profiles[0].qualification_evidence,
            RemotePromptModelQualificationEvidence,
        )

        payload["schema"] = PROMPT_MODEL_CATALOG_V3_SCHEMA
        with self.assertRaises(PromptModelContractError):
            decode_prompt_model_catalog(json.dumps(payload).encode("utf-8"))

    def test_current_policy_rejects_a_validly_shaped_but_stale_evidence_fingerprint(self) -> None:
        evidence = build_remote_qualification_evidence(
            live_result(), qualification_sha256=EVIDENCE_SHA
        )
        stale = replace(evidence, policy_sha256="sha256:" + "d" * 64)
        profile = replace(
            remote_profile(),
            qualification_state=PromptModelQualificationState.QUALIFIED,
            qualification_evidence=stale,
        )
        with self.assertRaises(PromptModelContractError) as caught:
            active_policy_for_profile(profile)
        self.assertEqual(caught.exception.code, "remote_policy_qualification")


if __name__ == "__main__":
    unittest.main()
