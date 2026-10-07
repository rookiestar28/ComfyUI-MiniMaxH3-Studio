"""M22-18. A confirmed live connection is acceptance; a completed generation is not required.

The user's standard of 2026-08-23 is that a confirmed connection qualifies a remote provider and no
API top-up is planned. Under the previous contract a live run on an account without credit produced
no evidence at all, so it promoted nothing and wasted a credential.

The shapes here are taken from the retained M22-14 live artifacts rather than invented. The OpenAI
run answered `prompt_model.quota` on HTTP 429 after spending both transmissions, with 726 request
bytes and 283 response bytes: authenticated, routed to the pinned model, refused on balance. The
Gemini retry answered HTTP 200 with an 8339-byte catalogue and never sent a content request at all,
which is why `request_bytes: 0` and one observed transmission must never qualify.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from historical_prompt_model_fixtures import historical_policy_facts as policy_for_profile
from historical_prompt_model_fixtures import load_historical_catalog as load_prompt_model_catalog

from comfyui_h3_context.core.prompt_model_provider import (
    REMOTE_QUALIFICATION_RECEIPT_SCHEMA,
    REMOTE_QUALIFICATION_RESULT_SCHEMA,
    PromptModelContractError,
    PromptModelOutcomeId,
    RemotePromptModelQualificationEvidence,
    RemoteQualificationBasis,
    _decode_remote_qualification_evidence,
    build_remote_qualification_evidence,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)

EVIDENCE_SHA = "sha256:" + "d" * 64
COMMIT_OID = "e" * 40
TREE_OID = "f" * 40


def remote_profile() -> PromptModelProfile:
    return load_prompt_model_catalog().require("openai.gpt_5_6_terra.remote")


def quota_result(**overrides: object) -> dict[str, object]:
    """The artifact the tool now emits for the retained OpenAI run: reached, answered, unbilled."""

    profile = remote_profile()
    policy = policy_for_profile(profile)
    body: dict[str, object] = {
        "schema": REMOTE_QUALIFICATION_RECEIPT_SCHEMA,
        "profile_id": profile.profile_id,
        "outcome_id": PromptModelOutcomeId.QUOTA.value,
        "http_status": 429,
        # The reach proof, in both directions. These are the retained values.
        "request_bytes": 726,
        "response_bytes": 283,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "duration_ms": 2924,
        "provider_id": policy.provider_id,
        "model_id": profile.model_id,
        "policy_sha256": policy.fingerprint,
        "price_basis_id": policy.price_basis_id,
        # Authorized before the request, so it exists even though nothing was billed.
        "maximum_cost_micro_usd": 1822,
        "actual_cost_micro_usd": 0,
        "usage_present": False,
    }
    patch = overrides.pop("receipt_fields", None)
    if isinstance(patch, dict):
        body.update(patch)
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
        "observed_on": policy.price_checked_on.isoformat(),
        "repository_commit": COMMIT_OID,
        "repository_tree": TREE_OID,
        "max_transmissions": policy.max_transmissions,
        "observed_transmissions": policy.max_transmissions,
        "max_input_tokens": policy.max_input_tokens,
        "max_output_tokens": policy.max_output_tokens,
        "max_cost_micro_usd": policy.max_cost_micro_usd,
        "outcome_id": PromptModelOutcomeId.QUOTA.value,
        # A quota run produced no draft, so neither of these can be true. The builder requires
        # both directions, so an artifact cannot claim a completion it did not get.
        "schema_valid": False,
        "receipt_valid": False,
        "receipt": body,
        "catalog_promotion_performed": False,
        "basis": RemoteQualificationBasis.REACHABILITY.value,
    }
    values.update(overrides)
    return values


def build(values: dict[str, object]) -> RemotePromptModelQualificationEvidence:
    return build_remote_qualification_evidence(values, qualification_sha256=EVIDENCE_SHA)


class ReachabilityBasisTests(unittest.TestCase):
    def test_a_quota_refusal_after_a_real_request_qualifies_on_reachability(self) -> None:
        evidence = build(quota_result())
        self.assertIs(evidence.basis, RemoteQualificationBasis.REACHABILITY)
        # Zero is the true observation here, not a missing one. The basis says which.
        self.assertEqual(0, evidence.prompt_tokens)
        self.assertEqual(0, evidence.completion_tokens)
        self.assertEqual(0, evidence.actual_cost_micro_usd)
        self.assertEqual("reachability", evidence.to_wire()["basis"])

    def test_only_a_reached_content_route_qualifies(self) -> None:
        # AC-03. The retained Gemini shape, both halves of it: a run that stopped at discovery
        # spent one transmission and sent no content request. Either alone is disqualifying.
        with self.assertRaises(PromptModelContractError):
            build(quota_result(observed_transmissions=1))
        with self.assertRaises(PromptModelContractError):
            build(quota_result(receipt_fields={"request_bytes": 0}))
        with self.assertRaises(PromptModelContractError):
            build(quota_result(receipt_fields={"response_bytes": 0}))

    def test_no_other_outcome_can_carry_a_basis(self) -> None:
        # AC-01, asserted over the whole vocabulary rather than a chosen sample, so a future
        # outcome identifier cannot quietly become qualifying by being added to the enum.
        admitted = {PromptModelOutcomeId.OK, PromptModelOutcomeId.QUOTA}
        for outcome in PromptModelOutcomeId:
            if outcome in admitted:
                continue
            with self.subTest(outcome=outcome.value):
                with self.assertRaises(PromptModelContractError):
                    build(
                        quota_result(
                            outcome_id=outcome.value,
                            receipt_fields={"outcome_id": outcome.value},
                            basis=RemoteQualificationBasis.REACHABILITY.value,
                        )
                    )

    def test_authentication_and_model_missing_are_refused_by_name(self) -> None:
        # The two exclusions worth naming: a rejected credential proves nothing about this profile,
        # and a provider that does not hold the pinned model must never be promoted as holding it.
        for outcome in (PromptModelOutcomeId.AUTHENTICATION, PromptModelOutcomeId.MODEL_MISSING):
            with self.subTest(outcome=outcome.value):
                with self.assertRaisesRegex(PromptModelContractError, "basis"):
                    build(
                        quota_result(
                            outcome_id=outcome.value, receipt_fields={"outcome_id": outcome.value}
                        )
                    )

    def test_the_label_cannot_outrank_the_answer(self) -> None:
        with self.assertRaisesRegex(PromptModelContractError, "basis"):
            build(quota_result(basis=RemoteQualificationBasis.COMPLETION.value))

    def test_a_quota_run_may_not_claim_it_produced_a_draft(self) -> None:
        for key in ("schema_valid", "receipt_valid"):
            claimed: dict[str, object] = {key: True}
            with self.subTest(key=key):
                with self.assertRaises(PromptModelContractError):
                    build(quota_result(**claimed))

    def test_a_failed_run_still_qualifies_on_neither_basis(self) -> None:
        with self.assertRaises(PromptModelContractError):
            build(quota_result(status="FAIL"))


class CompletionBasisIsUnchangedTests(unittest.TestCase):
    """Completion identity stays strict; optional usage is not a billing qualification gate."""

    @staticmethod
    def _completion(**receipt: object) -> dict[str, object]:
        body: dict[str, object] = {
            "outcome_id": PromptModelOutcomeId.OK.value,
            "http_status": 200,
            "prompt_tokens": 96,
            "completion_tokens": 32,
            "actual_cost_micro_usd": 312,
            "usage_present": True,
        }
        body.update(receipt)
        return quota_result(
            outcome_id=PromptModelOutcomeId.OK.value,
            schema_valid=True,
            receipt_valid=True,
            basis=RemoteQualificationBasis.COMPLETION.value,
            receipt_fields=body,
        )

    def test_a_completion_still_builds_and_says_so(self) -> None:
        evidence = build(self._completion())
        self.assertIs(evidence.basis, RemoteQualificationBasis.COMPLETION)
        self.assertEqual(96, evidence.prompt_tokens)

    def test_a_completion_can_carry_zero_optional_usage_or_cost(self) -> None:
        for key in ("prompt_tokens", "completion_tokens", "actual_cost_micro_usd"):
            with self.subTest(key=key):
                evidence = build(self._completion(**{key: 0}))
                self.assertIs(evidence.basis, RemoteQualificationBasis.COMPLETION)
                self.assertEqual(getattr(evidence, key), 0)

    def test_a_completion_keeps_transport_and_usage_consistency(self) -> None:
        evidence = build(
            self._completion(
                usage_present=False,
                prompt_tokens=0,
                completion_tokens=0,
                actual_cost_micro_usd=0,
            )
        )
        self.assertIs(evidence.basis, RemoteQualificationBasis.COMPLETION)
        with self.assertRaises(PromptModelContractError):
            build(self._completion(usage_present=False))
        with self.assertRaises(PromptModelContractError):
            build(self._completion(http_status=429))


class OlderEvidenceStillDecodesTests(unittest.TestCase):
    """AC-04. The key set predating this item could only ever describe a completion."""

    def test_the_shipped_catalog_round_trips(self) -> None:
        catalog = load_prompt_model_catalog()
        anthropic = catalog.require("anthropic.claude_sonnet_4_6.remote")
        evidence = anthropic.qualification_evidence
        assert isinstance(evidence, RemotePromptModelQualificationEvidence)
        # The shipped entry names its basis rather than relying on absence being read as one.
        self.assertIs(evidence.basis, RemoteQualificationBasis.COMPLETION)
        self.assertEqual("completion", evidence.to_wire()["basis"])
        self.assertGreaterEqual(evidence.actual_cost_micro_usd, 1)

    def test_a_result_without_the_key_is_accepted_as_a_completion(self) -> None:
        values = CompletionBasisIsUnchangedTests._completion()
        del values["basis"]
        evidence = build(values)
        self.assertIs(evidence.basis, RemoteQualificationBasis.COMPLETION)


class RejudgingARetainedObservationTests(unittest.TestCase):
    """A recorded observation is a fact about the day it was made; only the verdict was stale.

    M22-18. The retained M22-14 OpenAI artifact already contains everything the reachability basis
    needs. It says FAIL only because the tool that wrote it knew one bar. Re-running the provider
    would observe the same thing and spend a credential to do it, so the rule is re-applied to the
    retained observation instead. The rule itself is shared with the live path, not copied.
    """

    @staticmethod
    def _tool() -> ModuleType:
        import importlib.util

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "m22_14_remote_qualification", root / "scripts" / "m22_14_remote_qualification.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_retained_quota_observation_rejudges_to_reachability(self) -> None:
        tool = self._tool()
        retained = {**quota_result(), "status": "FAIL"}
        del retained["basis"]
        verdict = tool.rejudge(retained)
        self.assertEqual("PASS", verdict["status"])
        self.assertEqual("reachability", verdict["basis"])
        # Every recorded observation is carried through untouched; only the verdict is recomputed.
        for key in ("outcome_id", "observed_transmissions", "receipt", "observed_on"):
            self.assertEqual(retained[key], verdict[key])

    def test_a_discovery_only_observation_rejudges_to_nothing(self) -> None:
        tool = self._tool()
        verdict = tool.rejudge(
            {
                **quota_result(
                    outcome_id=PromptModelOutcomeId.MODEL_MISSING.value,
                    observed_transmissions=1,
                    receipt_fields={
                        "outcome_id": PromptModelOutcomeId.MODEL_MISSING.value,
                        "http_status": 200,
                        "request_bytes": 0,
                        "response_bytes": 8339,
                    },
                ),
                "status": "FAIL",
            }
        )
        self.assertEqual("FAIL", verdict["status"])
        self.assertIsNone(verdict["basis"])

    def test_a_hermetic_observation_may_not_be_rejudged(self) -> None:
        tool = self._tool()
        with self.assertRaises(SystemExit):
            tool.rejudge({**quota_result(), "mode": "hermetic"})

    def test_the_rejudged_verdict_still_has_to_satisfy_the_builder(self) -> None:
        tool = self._tool()
        verdict = tool.rejudge({**quota_result(), "status": "FAIL"})
        evidence = build(dict(verdict))
        self.assertIs(evidence.basis, RemoteQualificationBasis.REACHABILITY)


class ReviewFindingsTests(unittest.TestCase):
    """Cases written for a distinct review that found each of these gaps by mutation."""

    def test_a_quota_result_may_not_qualify_by_omitting_the_label(self) -> None:
        # P1-A. The key sets are versioned, and absence means the artifact predates the reachability
        # basis, so it could only ever have described a completion. Reading absence as "whatever the
        # outcome implies" would let a quota result obtain the weaker classification without ever
        # asserting it -- and would make the two versioned key sets disagree with each other.
        values = quota_result()
        del values["basis"]
        with self.assertRaisesRegex(PromptModelContractError, "basis"):
            build(values)

    def test_a_completion_result_without_the_label_is_still_accepted(self) -> None:
        # The other half: absence is not fatal, it is read as the only thing it could have meant.
        values = CompletionBasisIsUnchangedTests._completion()
        del values["basis"]
        self.assertIs(build(values).basis, RemoteQualificationBasis.COMPLETION)

    def test_reachability_cannot_claim_unobserved_usage_but_fee_metadata_is_inert(self) -> None:
        # A quota response has no completion/usage here. Its legacy fee metadata makes no claim
        # about provider billing and cannot alter the transport qualification basis.
        for key, value in (
            ("prompt_tokens", 1),
            ("completion_tokens", 1),
            ("prompt_tokens", 512),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(PromptModelContractError):
                    build(quota_result(receipt_fields={key: value}))
        for value in (0, 1, 10000, 10001):
            with self.subTest(legacy_cost=value):
                evidence = build(quota_result(receipt_fields={"actual_cost_micro_usd": value}))
                self.assertIs(evidence.basis, RemoteQualificationBasis.REACHABILITY)
                self.assertEqual(evidence.actual_cost_micro_usd, value)

    def test_the_receipt_outcome_must_agree_on_the_reachability_side_too(self) -> None:
        # The reviewer reverted this check to fire only on the completion side and nothing failed.
        # A receipt naming a different outcome, or a string that is no outcome at all, is refused.
        for spelling in ("prompt_model.ok", "totally-not-a-real-outcome-id", ""):
            with self.subTest(spelling=spelling):
                with self.assertRaises(PromptModelContractError):
                    build(quota_result(receipt_fields={"outcome_id": spelling}))

    def test_the_catalog_decoder_reads_absence_as_completion_not_reachability(self) -> None:
        # Legacy absence still means completion; accounting values cannot choose a basis.
        catalog = load_prompt_model_catalog()
        wire = catalog.require("anthropic.claude_sonnet_4_6.remote").qualification_evidence
        assert isinstance(wire, RemotePromptModelQualificationEvidence)
        older = dict(wire.to_wire())
        del older["basis"]
        decoded = _decode_remote_qualification_evidence(older)
        assert decoded is not None
        self.assertIs(decoded.basis, RemoteQualificationBasis.COMPLETION)

    def test_decoder_keeps_reachability_usage_guards_and_ignores_legacy_fees(self) -> None:
        # Exercise the constructor through the decoder, independently of the builder's guards.
        catalog = load_prompt_model_catalog()
        wire = catalog.require("openai.gpt_5_6_terra.remote").qualification_evidence
        assert isinstance(wire, RemotePromptModelQualificationEvidence)
        self.assertIs(wire.basis, RemoteQualificationBasis.REACHABILITY)
        for key, value in (
            ("prompt_tokens", 1),
            ("prompt_tokens", wire.max_input_tokens),
            ("completion_tokens", 1),
            ("completion_tokens", wire.max_output_tokens),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(PromptModelContractError):
                    _decode_remote_qualification_evidence({**wire.to_wire(), key: value})
        for value in (0, 1, wire.max_cost_micro_usd + 1):
            with self.subTest(legacy_cost=value):
                decoded = _decode_remote_qualification_evidence(
                    {
                        **wire.to_wire(),
                        "actual_cost_micro_usd": value,
                    }
                )
                assert isinstance(decoded, RemotePromptModelQualificationEvidence)
                self.assertIs(decoded.basis, RemoteQualificationBasis.REACHABILITY)
                self.assertEqual(decoded.actual_cost_micro_usd, value)

    def test_an_unknown_basis_string_is_refused_by_the_decoder(self) -> None:
        catalog = load_prompt_model_catalog()
        wire = catalog.require("anthropic.claude_sonnet_4_6.remote").qualification_evidence
        assert isinstance(wire, RemotePromptModelQualificationEvidence)
        for spelling in ("reachable", "", None, True):
            with self.subTest(spelling=spelling):
                with self.assertRaises(PromptModelContractError):
                    _decode_remote_qualification_evidence({**wire.to_wire(), "basis": spelling})


class TheSharedRuleTests(unittest.TestCase):
    """P2-C and P2-D. The script-side rule is the one a human reads the verdict from."""

    @staticmethod
    def _rule() -> Callable[..., str | None]:
        rule: Callable[..., str | None] = RejudgingARetainedObservationTests._tool().basis_for
        return rule

    def test_a_run_that_stopped_short_of_the_content_route_earns_nothing(self) -> None:
        # Deleting this guard survived the whole suite. It is the same invariant the dataclass
        # enforces, deliberately restated here so a verdict is never reported that the gate behind
        # it would refuse.
        rule = self._rule()
        self.assertIsNone(
            rule(
                outcome_id="prompt_model.quota",
                schema_valid=False,
                receipt_valid=False,
                transmissions=1,
                max_transmissions=2,
                receipt={"request_bytes": 726, "response_bytes": 283},
            )
        )

    def test_the_transmission_count_must_be_an_integer(self) -> None:
        # A hand-edited artifact carrying 2.0 satisfied a loose comparison and was reported PASS,
        # while `build_remote_qualification_evidence` still refused it. The two must agree.
        rule = self._rule()
        for value in (2.0, True, "2", None):
            with self.subTest(value=value):
                self.assertIsNone(
                    rule(
                        outcome_id="prompt_model.quota",
                        schema_valid=False,
                        receipt_valid=False,
                        transmissions=value,
                        max_transmissions=2,
                        receipt={"request_bytes": 726, "response_bytes": 283},
                    )
                )


class RejudgeCommandLineTests(unittest.TestCase):
    """P1-B. Every guard on the mode a person actually invokes was invisible to the suite."""

    def setUp(self) -> None:
        self.tool = RejudgingARetainedObservationTests._tool()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "retained.json"
        retained = {**quota_result(), "status": "FAIL"}
        del retained["basis"]
        self.source.write_text(json.dumps(retained), encoding="utf-8")
        self.output = self.root / "rejudged.json"

    def _argv(self, *extra: str) -> list[str]:
        return [
            "--mode",
            "rejudge",
            "--profile",
            "openai.gpt_5_6_terra.remote",
            "--output",
            str(self.output),
            "--source",
            str(self.source),
            *extra,
        ]

    def test_the_happy_path_writes_a_passing_verdict(self) -> None:
        self.assertEqual(0, self.tool.main(self._argv()))
        written = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual("PASS", written["status"])
        self.assertEqual("reachability", written["basis"])

    def test_live_authorization_is_refused_because_nothing_is_authorized(self) -> None:
        # Re-judging contacts no provider. Accepting live authorization here would let the mode
        # look like a live run in a command history while never touching the network.
        for flag, value in (
            ("--authorize-policy-sha256", "sha256:" + "0" * 64),
            ("--authorize-max-transmissions", "2"),
            ("--candidate-commit", "0" * 40),
            ("--candidate-tree", "0" * 40),
        ):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                self.tool.main(self._argv(flag, value))

    def test_legacy_fee_argument_is_inert_in_rejudge_mode(self) -> None:
        self.assertEqual(0, self.tool.main(self._argv("--authorize-max-cost-micro-usd", "-1")))
        written = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(written["mode"], "live")
        self.assertEqual(written["status"], "PASS")

    def test_an_artifact_from_another_profile_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.tool.main(
                [
                    "--mode",
                    "rejudge",
                    "--profile",
                    "gemini.gemini_3_7_flash.remote",
                    "--output",
                    str(self.output),
                    "--source",
                    str(self.source),
                ]
            )

    def test_rejudge_without_a_source_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.tool.main(
                [
                    "--mode",
                    "rejudge",
                    "--profile",
                    "openai.gpt_5_6_terra.remote",
                    "--output",
                    str(self.output),
                ]
            )

    def test_a_credential_reader_is_never_consulted(self) -> None:
        reads: list[str] = []

        def reader(prompt: str) -> str:
            reads.append(prompt)
            return "forbidden"

        self.assertEqual(0, self.tool.main(self._argv(), credential_reader=reader))
        self.assertEqual([], reads)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
