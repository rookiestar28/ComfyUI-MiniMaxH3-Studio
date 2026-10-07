"""Hermetic execution and live-authorization guards for the M22-14 qualification tool."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from historical_prompt_model_fixtures import (
    historical_remote_evidence,
)
from historical_prompt_model_fixtures import (
    load_historical_direct_call_catalog as load_prompt_model_catalog,
)

from comfyui_h3_context.adapters.prompt_model_transport import RemoteExchangeMetrics
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_provider import (
    build_remote_qualification_evidence,
)
from comfyui_h3_context.core.remote_prompt_model import RemoteUsageReceipt
from comfyui_h3_context.core.remote_provider_policy import RemoteProviderPolicy, policy_for_profile

ROOT = Path(__file__).resolve().parents[1]


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "m22_14_remote_qualification",
        ROOT / "scripts" / "m22_14_remote_qualification.py",
    )
    if spec is None or spec.loader is None:  # pragma: no cover - repository invariant
        raise AssertionError("qualification tool could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


class DialectCandidateQualificationTests(unittest.TestCase):
    def test_explicit_candidate_can_complete_below_the_ceiling_without_borrowing_live_evidence(
        self,
    ) -> None:
        for profile_id in TOOL.PROFILE_IDS:
            profile = TOOL._profile(profile_id, dialect_revision=2)
            self.assertEqual(profile.adapter_version, "1.1.0")
            self.assertEqual(profile.parser_version, "h3.prompt_model.draft_json.v2")
            self.assertEqual(profile.max_calls_per_action, 4)
            self.assertIsNone(profile.qualification_evidence)
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "candidate.json"
                exit_code = TOOL.main(
                    [
                        "--mode",
                        "hermetic",
                        "--profile",
                        profile_id,
                        "--dialect-revision",
                        "2",
                        "--output",
                        str(output),
                    ]
                )
                self.assertEqual(exit_code, 0)
                result = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(result["mode"], "hermetic")
                self.assertEqual(result["max_transmissions"], 4)
                self.assertEqual(result["observed_transmissions"], 2)
                self.assertFalse(result["catalog_promotion_performed"])
                self.assertIsNone(result["repository_commit"])
            self.assertEqual(TOOL._profile(profile_id).max_calls_per_action, 2)


class _PricePolicyValidToday:
    @staticmethod
    def today() -> date:
        return date(2026, 8, 23)


class _PricePolicyExpiredToday:
    @staticmethod
    def today() -> date:
        return date(2026, 9, 23)


class _UsageOptionalExchange:
    def __init__(self, profile: PromptModelProfile, policy: RemoteProviderPolicy) -> None:
        self._inner = TOOL._HermeticExchange(profile, policy)

    @property
    def transmission_count(self) -> int:
        return int(self._inner.transmission_count)

    @property
    def metrics(self) -> RemoteExchangeMetrics | None:
        metrics = self._inner.metrics
        if not isinstance(metrics, RemoteExchangeMetrics):
            return None
        return replace(metrics, prompt_tokens=0, completion_tokens=0, usage_present=False)

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        response = dict(self._inner.request(method, path, payload, timeout_seconds=timeout_seconds))
        response.pop("usage", None)
        return response


def _recording_reader(reads: list[str], value: str) -> Callable[[str], str]:
    def read(prompt: str) -> str:
        reads.append(prompt)
        return value

    return read


class RemoteQualificationToolTests(unittest.TestCase):
    def test_hermetic_mode_passes_both_profiles_without_requesting_a_real_credential(self) -> None:
        for profile_id in TOOL.PROFILE_IDS:
            with self.subTest(profile_id=profile_id), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "receipt.json"
                credential_reads: list[str] = []
                with patch.object(TOOL, "date", _PricePolicyExpiredToday):
                    status = TOOL.main(
                        [
                            "--mode",
                            "hermetic",
                            "--profile",
                            profile_id,
                            "--output",
                            str(output),
                        ],
                        credential_reader=_recording_reader(credential_reads, "forbidden"),
                    )
                self.assertEqual(status, 0)
                self.assertEqual(credential_reads, [])
                evidence = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(evidence["status"], "PASS")
                self.assertEqual(evidence["mode"], "hermetic")
                self.assertEqual(evidence["observed_on"], "2026-09-23")
                self.assertIsNone(evidence["repository_commit"])
                self.assertIsNone(evidence["repository_tree"])
                self.assertEqual(evidence["observed_transmissions"], 2)
                self.assertTrue(evidence["receipt_valid"])
                self.assertFalse(evidence["catalog_promotion_performed"])
                # M22-18 AC-05. The hermetic exchange answers with a real fixture completion, so a
                # hermetic run must never be able to reach the weaker reachability basis.
                self.assertEqual(evidence["basis"], "completion")
                serialized = json.dumps(evidence)
                self.assertNotIn("fixture input", serialized)
                self.assertNotIn("quiet street", serialized)
                self.assertNotIn("hermetic-session-credential", serialized)

    def test_live_mode_refuses_before_a_credential_prompt_without_exact_authority(self) -> None:
        reads: list[str] = []
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(SystemExit):
            TOOL.main(
                [
                    "--mode",
                    "live",
                    "--profile",
                    TOOL.PROFILE_IDS[0],
                    "--output",
                    str(Path(directory) / "receipt.json"),
                ],
                credential_reader=_recording_reader(reads, "secret"),
            )
        self.assertEqual(reads, [])

    def test_live_mode_with_exact_flags_uses_the_injected_exchange_and_never_promotes(self) -> None:
        profile = next(
            item
            for item in load_prompt_model_catalog().profiles
            if item.profile_id == TOOL.PROFILE_IDS[0]
        )
        policy = policy_for_profile(profile)
        historical = historical_remote_evidence(profile.profile_id)
        reads: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            with patch.object(TOOL, "date", _PricePolicyValidToday):
                status = TOOL.main(
                    [
                        "--mode",
                        "live",
                        "--profile",
                        profile.profile_id,
                        "--output",
                        str(output),
                        "--authorize-policy-sha256",
                        policy.fingerprint,
                        "--authorize-max-transmissions",
                        "2",
                        "--authorize-max-cost-micro-usd",
                        str(historical.max_cost_micro_usd),
                        "--candidate-commit",
                        "b" * 40,
                        "--candidate-tree",
                        "c" * 40,
                    ],
                    exchange_factory=lambda selected, _credential: TOOL._HermeticExchange(
                        selected, policy_for_profile(selected)
                    ),
                    credential_reader=_recording_reader(reads, "session-secret"),
                )
            self.assertEqual(status, 0)
            self.assertEqual(len(reads), 1)
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(evidence["mode"], "live")
            self.assertEqual(evidence["policy_sha256"], policy.fingerprint)
            self.assertEqual(evidence["observed_transmissions"], policy.max_transmissions)
            self.assertEqual(evidence["repository_commit"], "b" * 40)
            self.assertEqual(evidence["repository_tree"], "c" * 40)
            self.assertTrue(evidence["receipt_valid"])
            self.assertFalse(evidence["catalog_promotion_performed"])
            self.assertNotIn("session-secret", json.dumps(evidence))
            self.assertNotIn("host", evidence["receipt"])
            self.assertNotIn("credential", evidence["receipt"])

    def test_expired_price_and_legacy_fee_flags_do_not_block_live_mode(self) -> None:
        for profile_id in TOOL.PROFILE_IDS:
            profile = TOOL._profile(profile_id)
            policy = policy_for_profile(profile)
            historical = historical_remote_evidence(profile.profile_id)
            for fee in (None, 0, -1, historical.max_cost_micro_usd + 1):
                with (
                    self.subTest(profile=profile_id, fee=fee),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    output = Path(directory) / "receipt.json"
                    reads: list[str] = []
                    arguments = [
                        "--mode",
                        "live",
                        "--profile",
                        profile_id,
                        "--output",
                        str(output),
                        "--authorize-policy-sha256",
                        policy.fingerprint,
                        "--authorize-max-transmissions",
                        str(policy.max_transmissions),
                        "--candidate-commit",
                        "b" * 40,
                        "--candidate-tree",
                        "c" * 40,
                    ]
                    if fee is not None:
                        arguments.extend(["--authorize-max-cost-micro-usd", str(fee)])
                    with patch.object(TOOL, "date", _PricePolicyExpiredToday):
                        status = TOOL.main(
                            arguments,
                            exchange_factory=lambda selected, _credential: TOOL._HermeticExchange(
                                selected, policy_for_profile(selected)
                            ),
                            credential_reader=_recording_reader(reads, profile_id),
                        )
                    self.assertEqual(status, 0)
                    self.assertEqual(len(reads), 1)
                    evidence = json.loads(output.read_text(encoding="utf-8"))
                    self.assertEqual(evidence["status"], "PASS")
                    self.assertEqual(evidence["observed_on"], "2026-09-23")
                    self.assertFalse(evidence["catalog_promotion_performed"])
                    claim = build_remote_qualification_evidence(
                        evidence,
                        qualification_sha256="sha256:" + "a" * 64,
                        family=profile.family,
                    )
                    self.assertEqual(claim.profile_id, profile_id)
                    self.assertEqual(claim.actual_cost_micro_usd, 0)

    def test_qualification_accepts_optional_usage_without_billing_facts(self) -> None:
        for profile_id in TOOL.PROFILE_IDS:
            with self.subTest(profile=profile_id), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "receipt.json"
                status = TOOL.main(
                    [
                        "--mode",
                        "hermetic",
                        "--profile",
                        profile_id,
                        "--output",
                        str(output),
                        "--authorize-max-cost-micro-usd",
                        "-1",
                    ],
                    exchange_factory=lambda selected, _credential: _UsageOptionalExchange(
                        selected, policy_for_profile(selected)
                    ),
                    credential_reader=lambda _prompt: self.fail("hermetic mode cannot read a key"),
                )
                self.assertEqual(status, 0)
                evidence = json.loads(output.read_text(encoding="utf-8"))
                receipt = evidence["receipt"]
                self.assertTrue(evidence["receipt_valid"])
                self.assertFalse(receipt["usage_present"])
                self.assertEqual(receipt["prompt_tokens"], 0)
                self.assertEqual(receipt["completion_tokens"], 0)
                self.assertEqual(receipt["price_basis_id"], "")
                self.assertEqual(receipt["maximum_cost_micro_usd"], 0)
                self.assertEqual(receipt["actual_cost_micro_usd"], 0)
                self.assertNotIn("host", receipt)
                self.assertNotIn("credential", receipt)
                self.assertFalse(evidence["catalog_promotion_performed"])

    def test_receipt_qualification_preserves_identity_and_transport_guards(self) -> None:
        for profile_id in TOOL.PROFILE_IDS:
            profile = TOOL._profile(profile_id)
            policy = policy_for_profile(profile)
            historical = historical_remote_evidence(profile.profile_id)
            receipt = RemoteUsageReceipt(
                profile_id=profile_id,
                host=TOOL.admit_egress_destination(profile.family, profile.endpoint).host,
                outcome_id=TOOL.PromptModelOutcomeId.OK,
                http_status=200,
                request_bytes=512,
                response_bytes=256,
                prompt_tokens=0,
                completion_tokens=0,
                duration_ms=1,
                provider_id=policy.provider_id,
                model_id=profile.model_id,
                policy_sha256=policy.fingerprint,
            )
            self.assertTrue(TOOL._receipt_is_qualification_evidence(receipt, profile, policy))
            self.assertTrue(
                TOOL._receipt_is_qualification_evidence(
                    replace(
                        receipt,
                        usage_present=True,
                        prompt_tokens=historical.max_input_tokens + 1,
                        completion_tokens=historical.max_output_tokens + 1,
                    ),
                    profile,
                    policy,
                )
            )
            for field, invalid in (
                ("profile_id", replace(receipt, profile_id="other-profile")),
                ("host", replace(receipt, host="example.com")),
                ("outcome_id", replace(receipt, outcome_id=TOOL.PromptModelOutcomeId.QUOTA)),
                ("http_status", replace(receipt, http_status=201)),
                ("request_bytes", replace(receipt, request_bytes=0)),
                ("response_bytes", replace(receipt, response_bytes=0)),
                ("provider_id", replace(receipt, provider_id="other-provider")),
                ("model_id", replace(receipt, model_id="other-model")),
                ("policy_sha256", replace(receipt, policy_sha256="sha256:" + "a" * 64)),
            ):
                with self.subTest(profile=profile_id, field=field):
                    self.assertFalse(
                        TOOL._receipt_is_qualification_evidence(invalid, profile, policy)
                    )

    def test_each_live_authority_guard_refuses_before_credentials_or_exchange(self) -> None:
        for profile_id in TOOL.PROFILE_IDS:
            profile = TOOL._profile(profile_id)
            policy = policy_for_profile(profile)
            authority = {
                "--mode": "live",
                "--profile": profile_id,
                "--output": "unused.json",
                "--authorize-policy-sha256": policy.fingerprint,
                "--authorize-max-transmissions": str(policy.max_transmissions),
                "--candidate-commit": "b" * 40,
                "--candidate-tree": "c" * 40,
            }
            for flag, invalid in (
                ("--authorize-policy-sha256", "sha256:" + "a" * 64),
                ("--authorize-max-transmissions", str(policy.max_transmissions + 1)),
                ("--candidate-commit", "invalid"),
                ("--candidate-tree", "invalid"),
            ):
                arguments = {**authority, flag: invalid}
                with self.subTest(profile=profile_id, flag=flag), self.assertRaises(SystemExit):
                    TOOL.main(
                        [part for pair in arguments.items() for part in pair],
                        credential_reader=lambda _prompt: self.fail("refusal cannot read a key"),
                        exchange_factory=lambda *_args: self.fail(
                            "refusal cannot construct exchange"
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
