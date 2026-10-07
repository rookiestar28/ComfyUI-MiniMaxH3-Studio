"""Hermetic provider-readiness adapter regressions."""

from __future__ import annotations

import subprocess as subprocess_module
import unittest
from dataclasses import replace
from hashlib import sha256
from unittest.mock import patch

from historical_prompt_model_fixtures import (
    load_historical_direct_call_catalog as load_prompt_model_catalog,
)

import comfyui_h3_context.adapters.provider_readiness as readiness_adapter
from comfyui_h3_context.adapters.prompt_model_transport import (
    PromptModelTransportError,
    resolve_pinned_address_bounded,
)
from comfyui_h3_context.adapters.provider_readiness import probe_provider_readiness
from comfyui_h3_context.core.prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelQualificationState,
    PromptModelRemediation,
    build_prompt_model_capabilities,
    build_qualification_evidence,
    compute_endpoint_fingerprint,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.provider_settings import (
    ProviderSettingsIntent,
    ProviderSettingsState,
    ReadinessObservation,
)
from comfyui_h3_context.core.remote_prompt_model import RuntimeCredential
from comfyui_h3_context.core.remote_provider_policy import policy_for_profile

MODEL_ID = "model-a"
DIGEST = "sha256:" + "3" * 64
#: Ollama publishes bare lowercase hex on `/api/tags`; the repository spells every digest it
#: stores with the algorithm prefix. Keeping both names here keeps the crossing visible in the
#: fixtures instead of letting a listing pretend to be already-normalised (M22-13).
WIRE_DIGEST = DIGEST.removeprefix("sha256:")


def profile(
    family: PromptModelFamily = PromptModelFamily.OLLAMA,
    *,
    endpoint: str = "http://127.0.0.1:11434",
) -> PromptModelProfile:
    remote = family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
    return PromptModelProfile(
        profile_id="remote.example" if remote else "ollama.local",
        family=family,
        endpoint=endpoint,
        model_id=MODEL_ID,
        model_digest=DIGEST,
        adapter_version="1.0.0",
        parser_version="h3.prompt_model.draft_json.v1",
        license_id="proprietary",
        license_source="provider_terms",
        license_text_sha256="sha256:" + "4" * 64,
        capabilities=build_prompt_model_capabilities(
            {
                "family": family.value,
                "accepted_media": ["text"],
                "provider_managed_context": True,
                "provider_managed_kv_cache": True,
                "max_request_bytes": 262_144,
                "max_context_tokens": 32_768,
                "max_output_tokens": 4_096,
                "streaming": False,
                "local_only": not remote,
                "requires_credential": remote,
            }
        ),
    )


class ListingExchange:
    def __init__(self, listing: object = None, failure: PromptModelOutcomeId | None = None) -> None:
        self.listing = listing
        self.failure = failure
        self.calls: list[tuple[str, str, object]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        self.calls.append((method, path, payload))
        if self.failure is not None:
            raise PromptModelTransportError(self.failure)
        return self.listing


class ProviderReadinessProbeTests(unittest.TestCase):
    def test_a_hanging_resolver_is_terminated_and_typed_as_timeout(self) -> None:
        with patch(
            "comfyui_h3_context.adapters.prompt_model_transport.subprocess.run",
            side_effect=subprocess_module.TimeoutExpired(cmd="resolver", timeout=0.01),
        ) as runner:
            with self.assertRaises(PromptModelTransportError) as caught:
                resolve_pinned_address_bounded(
                    "localhost", timeout_seconds=0.01, loopback_required=True
                )
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.TIMEOUT)
        self.assertEqual(runner.call_count, 1)

    def test_every_resolved_address_must_match_the_destination_policy(self) -> None:
        private_and_public = subprocess_module.CompletedProcess(
            args=["resolver"],
            returncode=0,
            stdout=b'{"addresses":["93.184.216.34","127.0.0.1"]}',
            stderr=b"",
        )
        with patch(
            "comfyui_h3_context.adapters.prompt_model_transport.subprocess.run",
            return_value=private_and_public,
        ):
            with self.assertRaises(PromptModelTransportError) as caught:
                resolve_pinned_address_bounded(
                    "api.example.com", timeout_seconds=1.0, loopback_required=False
                )
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.EGRESS_REFUSED)

        public_for_loopback = subprocess_module.CompletedProcess(
            args=["resolver"],
            returncode=0,
            stdout=b'{"addresses":["93.184.216.34"]}',
            stderr=b"",
        )
        with patch(
            "comfyui_h3_context.adapters.prompt_model_transport.subprocess.run",
            return_value=public_for_loopback,
        ):
            with self.assertRaises(PromptModelTransportError) as caught:
                resolve_pinned_address_bounded(
                    "localhost", timeout_seconds=1.0, loopback_required=True
                )
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.EGRESS_REFUSED)

    def test_production_exchanges_receive_the_short_probe_timeouts(self) -> None:
        local_exchange = ListingExchange({"models": [{"name": MODEL_ID, "digest": WIRE_DIGEST}]})
        with (
            patch.object(
                readiness_adapter, "resolve_pinned_address_bounded", return_value="127.0.0.1"
            ) as local_resolver,
            patch.object(
                readiness_adapter, "LoopbackJsonExchange", return_value=local_exchange
            ) as local_constructor,
        ):
            local = probe_provider_readiness(profile())
        self.assertTrue(local.reachable)
        self.assertGreater(local_constructor.call_args.kwargs["timeout_seconds"], 0)
        self.assertLessEqual(
            local_constructor.call_args.kwargs["timeout_seconds"],
            readiness_adapter.LOOPBACK_PROBE_TIMEOUT_SECONDS,
        )
        local_resolver.assert_called_once_with(
            "127.0.0.1",
            timeout_seconds=readiness_adapter.LOOPBACK_PROBE_TIMEOUT_SECONDS,
            loopback_required=True,
        )
        self.assertEqual(len(local_exchange.calls), 1)

        remote_profile = next(
            item
            for item in load_prompt_model_catalog().profiles
            if item.profile_id == "openai.gpt_5_6_terra.remote"
        )
        remote_exchange = ListingExchange({"data": [{"id": remote_profile.model_id}]})
        credential = RuntimeCredential("test-credential-" + "T" * 40)
        with (
            patch.object(
                readiness_adapter,
                "resolve_pinned_address_bounded",
                return_value="93.184.216.34",
            ) as remote_resolver,
            patch.object(
                readiness_adapter, "RemoteHttpsExchange", return_value=remote_exchange
            ) as remote_constructor,
        ):
            remote = probe_provider_readiness(remote_profile, credential)
        self.assertTrue(remote.reachable)
        self.assertIs(remote_constructor.call_args.args[1], credential)
        self.assertGreater(remote_constructor.call_args.kwargs["timeout_seconds"], 0)
        self.assertLessEqual(
            remote_constructor.call_args.kwargs["timeout_seconds"],
            readiness_adapter.REMOTE_PROBE_TIMEOUT_SECONDS,
        )
        remote_resolver.assert_called_once_with(
            "api.openai.com",
            timeout_seconds=readiness_adapter.REMOTE_PROBE_TIMEOUT_SECONDS,
            loopback_required=False,
        )
        self.assertEqual(len(remote_exchange.calls), 1)

    def test_one_listing_request_proves_identity_and_builds_the_census(self) -> None:
        exchange = ListingExchange(
            {
                "models": [
                    {"name": MODEL_ID, "digest": WIRE_DIGEST},
                    {"name": "other-model", "digest": "9" * 64},
                ]
            }
        )
        observation = probe_provider_readiness(
            profile(),
            pinned_identifiers=(MODEL_ID,),
            exchange_factory=lambda _destination, _credential: exchange,
        )
        self.assertTrue(observation.reachable)
        self.assertIsNone(observation.outcome)
        self.assertEqual(exchange.calls, [("GET", "/api/tags", None)])
        self.assertEqual(
            [(item.identifier, item.reason.value) for item in observation.candidates],
            [(MODEL_ID, "admitted"), ("other-model", "unpinned")],
        )

    def test_any_malformed_census_row_refuses_the_entire_answer(self) -> None:
        malformed_rows: tuple[object, ...] = (
            7,
            {},
            {"digest": WIRE_DIGEST},
            {"name": ""},
            {"name": "../invalid"},
        )
        for malformed in malformed_rows:
            with self.subTest(malformed=malformed):
                exchange = ListingExchange(
                    {
                        "models": [
                            {"name": MODEL_ID, "digest": WIRE_DIGEST},
                            malformed,
                        ]
                    }
                )
                observation = probe_provider_readiness(
                    profile(),
                    exchange_factory=lambda _destination, _credential, current=exchange: current,
                )
                self.assertTrue(observation.reachable)
                self.assertEqual(observation.candidates, ())
                assert observation.outcome is not None
                self.assertIs(
                    observation.outcome.outcome_id,
                    PromptModelOutcomeId.MALFORMED_RESPONSE,
                )

    def test_raw_census_limit_applies_before_malformed_rows_are_filtered(self) -> None:
        exchange = ListingExchange(
            {
                "models": [
                    {"name": MODEL_ID, "digest": WIRE_DIGEST},
                    *({} for _ in range(MAX_DISCOVERY_ROWS)),
                ]
            }
        )
        observation = probe_provider_readiness(
            profile(), exchange_factory=lambda _destination, _credential: exchange
        )
        self.assertTrue(observation.reachable)
        self.assertEqual(observation.candidates, ())
        assert observation.outcome is not None
        self.assertIs(
            observation.outcome.outcome_id,
            PromptModelOutcomeId.MALFORMED_RESPONSE,
        )

    def test_an_openai_shaped_listing_past_the_ceiling_also_answers(self) -> None:
        # M22-19. The Ollama case above has a twin here deliberately. This is the family the item
        # was written for -- a hosted catalogue is what overflowed the old ceiling -- so it is the
        # likelier target of a future "harden resolve_live_identity" change, and a trap that
        # misses the likely target is not a trap. An oversized listing is an answer either way.
        exchange = ListingExchange(
            {"data": [{"id": f"model-{index}"} for index in range(MAX_DISCOVERY_ROWS + 1)]}
        )
        observation = probe_provider_readiness(
            profile(PromptModelFamily.LOOPBACK_SERVER),
            exchange_factory=lambda _destination, _credential: exchange,
        )
        self.assertTrue(observation.reachable)
        self.assertEqual((), observation.candidates)
        assert observation.outcome is not None
        self.assertIs(observation.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)

    def test_a_missing_model_is_reachable_but_incompatible(self) -> None:
        exchange = ListingExchange({"models": [{"name": "other-model"}]})
        observation = probe_provider_readiness(
            profile(), exchange_factory=lambda _destination, _credential: exchange
        )
        self.assertTrue(observation.reachable)
        assert observation.outcome is not None
        self.assertIs(observation.outcome.outcome_id, PromptModelOutcomeId.MODEL_MISSING)
        self.assertIs(observation.outcome.remediation, PromptModelRemediation.SELECT_MODEL)
        self.assertEqual(len(exchange.calls), 1)

    def test_a_digest_mismatch_is_reachable_but_incompatible(self) -> None:
        exchange = ListingExchange({"models": [{"name": MODEL_ID, "digest": "9" * 64}]})
        observation = probe_provider_readiness(
            profile(), exchange_factory=lambda _destination, _credential: exchange
        )
        self.assertTrue(observation.reachable)
        assert observation.outcome is not None
        self.assertIs(observation.outcome.outcome_id, PromptModelOutcomeId.DIGEST_MISMATCH)
        self.assertIs(observation.outcome.remediation, PromptModelRemediation.SELECT_MODEL)

    def test_transport_identities_survive_without_provider_prose_or_retry(self) -> None:
        for outcome_id in (
            PromptModelOutcomeId.BACKEND_ABSENT,
            PromptModelOutcomeId.TIMEOUT,
            PromptModelOutcomeId.AUTHENTICATION,
            PromptModelOutcomeId.QUOTA,
            PromptModelOutcomeId.TRANSPORT,
            PromptModelOutcomeId.MALFORMED_RESPONSE,
            PromptModelOutcomeId.PROVIDER_ERROR,
            PromptModelOutcomeId.DESTINATION_UNRESOLVED,
            PromptModelOutcomeId.EGRESS_REFUSED,
        ):
            with self.subTest(outcome_id=outcome_id):
                exchange = ListingExchange(failure=outcome_id)
                observation = probe_provider_readiness(
                    profile(),
                    exchange_factory=lambda _destination, _credential, current=exchange: current,
                )
                self.assertFalse(observation.reachable)
                assert observation.outcome is not None
                self.assertIs(observation.outcome.outcome_id, outcome_id)
                self.assertEqual(observation.outcome.parameters, ())
                self.assertEqual(len(exchange.calls), 1)

    def test_an_unadmitted_endpoint_never_reaches_the_exchange_factory(self) -> None:
        calls = 0

        def factory(_destination: object, _credential: object) -> ListingExchange:
            nonlocal calls
            calls += 1
            return ListingExchange({"models": []})

        observation = probe_provider_readiness(
            profile(endpoint="http://192.0.2.1:11434"), exchange_factory=factory
        )
        self.assertEqual(calls, 0)
        self.assertFalse(observation.reachable)
        assert observation.outcome is not None
        self.assertIs(observation.outcome.outcome_id, PromptModelOutcomeId.EGRESS_REFUSED)

    def test_historical_remote_qualification_does_not_authorize_new_policy_io(self) -> None:
        base = load_prompt_model_catalog().require("openai.gpt_5_6_terra.remote")
        policy = policy_for_profile(base)
        from historical_prompt_model_fixtures import historical_remote_evidence

        evidence = historical_remote_evidence(base.profile_id)
        target = replace(
            base,
            qualification_state=PromptModelQualificationState.QUALIFIED,
            qualification_evidence=evidence,
        )
        exchange = ListingExchange({"data": [{"id": target.model_id}]})

        observation = probe_provider_readiness(
            target, exchange_factory=lambda _destination, _credential: exchange
        )

        self.assertTrue(observation.reachable)
        self.assertIsNotNone(observation.outcome)
        self.assertEqual(exchange.calls, [("GET", policy.discovery_route, None)])
        self.assertIsNone(observation.identity)


# --- M22-13: a qualified profile has to prove it, and proving it costs exactly two requests ----

QUALIFIED_LICENSE = "Fixture licence notice, present only so that it can be hashed away."
QUALIFIED_LICENSE_SHA = "sha256:" + sha256(QUALIFIED_LICENSE.encode("utf-8")).hexdigest()
QUALIFIED_WIRE_DIGEST = "5a" * 32
QUALIFIED_MODEL_ID = "fixture-model:27b-q4_K_M"
QUALIFIED_SIZE = 17_741_872_132

QUALIFIED_EVIDENCE = build_qualification_evidence(
    model_id=QUALIFIED_MODEL_ID,
    digest=QUALIFIED_WIRE_DIGEST,
    model_size_bytes=QUALIFIED_SIZE,
    model_format="gguf",
    model_family="qwen35",
    parameter_size="27.3B",
    quantization_level="Q4_K_M",
    context_length=262_144,
    capabilities=("completion", "thinking", "tools", "vision"),
    license_text_sha256=QUALIFIED_LICENSE_SHA,
    adapter_version="1.0.0",
    parser_version="h3.prompt_model.draft_json.v1",
    evidence_basis_id="M22-13.fixture.1",
)


def qualified_profile() -> PromptModelProfile:
    return replace(
        profile(),
        profile_id="fixture.qualified.local",
        model_id=QUALIFIED_MODEL_ID,
        model_digest=QUALIFIED_EVIDENCE.model_digest,
        license_text_sha256=QUALIFIED_LICENSE_SHA,
        qualification_state=PromptModelQualificationState.QUALIFIED,
        qualification_evidence=QUALIFIED_EVIDENCE,
    )


class QualifiedExchange:
    """Answers tags and show the way a local runtime holding the qualified weights would."""

    def __init__(self, *, show: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self._show = show

    def show_payload(self) -> dict[str, object]:
        if self._show is not None:
            return self._show
        return {
            "license": QUALIFIED_LICENSE,
            "capabilities": ["completion", "thinking", "tools", "vision"],
            "details": {
                "format": "gguf",
                "family": "qwen35",
                "parameter_size": "27.3B",
                "quantization_level": "Q4_K_M",
            },
            "model_info": {"qwen35.context_length": 262_144},
        }

    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        del timeout_seconds
        self.calls.append((method, path, payload))
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": QUALIFIED_MODEL_ID,
                        "model": QUALIFIED_MODEL_ID,
                        "digest": QUALIFIED_WIRE_DIGEST,
                        "size": QUALIFIED_SIZE,
                        "details": {"format": "gguf", "family": "qwen35"},
                    }
                ]
            }
        if path == "/api/show":
            return self.show_payload()
        raise AssertionError(path)


class CrowdedQualifiedExchange(QualifiedExchange):
    """The same qualified runtime, with a lot of other models pulled alongside the pinned one.

    M22-19. `parse_exact_tags_row` carried its own 256-row ceiling on this listing, a third name
    for the bound `_census` already applies. While the census refused at 64 that ceiling could
    never decide anything; raising the census to 512 made it the live bound for the window it
    covers, so a user with this many pulled models was told a reachable, genuinely qualified
    provider had answered malformed.
    """

    def __init__(self, rows: int) -> None:
        super().__init__()
        self.rows = rows

    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        if path != "/api/tags":
            return super().request(method, path, payload, timeout_seconds=timeout_seconds)
        self.calls.append((method, path, payload))
        filler = [
            {
                "name": f"filler-{index}:latest",
                "model": f"filler-{index}:latest",
                "digest": "aa" * 32,
                "size": 1,
                "details": {"format": "gguf", "family": "filler"},
            }
            for index in range(self.rows - 1)
        ]
        pinned = {
            "name": QUALIFIED_MODEL_ID,
            "model": QUALIFIED_MODEL_ID,
            "digest": QUALIFIED_WIRE_DIGEST,
            "size": QUALIFIED_SIZE,
            "details": {"format": "gguf", "family": "qwen35"},
        }
        half = self.rows // 2
        return {"models": [*filler[:half], pinned, *filler[half:]]}


class QualifiedReadinessTests(unittest.TestCase):
    def probe(self, exchange: QualifiedExchange, target: object = None) -> ReadinessObservation:
        return probe_provider_readiness(
            qualified_profile() if target is None else target,
            exchange_factory=lambda _destination, _credential: exchange,
        )

    def test_a_crowded_tag_list_still_proves_the_identity_it_holds(self) -> None:
        # Past the retired 256-row ceiling and inside the one that replaced it. Both requests must
        # still be spent: a refusal here never reached /api/show at all.
        for rows in (257, MAX_DISCOVERY_ROWS):
            with self.subTest(rows=rows):
                exchange = CrowdedQualifiedExchange(rows)
                observation = self.probe(exchange)
                self.assertTrue(observation.reachable)
                self.assertIsNone(observation.outcome)
                self.assertIsNotNone(observation.identity)
                self.assertEqual(rows, len(observation.candidates))
                self.assertEqual(
                    ["/api/tags", "/api/show"],
                    [path for _method, path, _payload in exchange.calls],
                )

    def test_a_tag_list_past_the_one_ceiling_answers_rather_than_disappears(self) -> None:
        # SECURITY: an oversized listing is still an answer. It must stay `reachable`, because
        # reporting a provider that replied as unreachable is the wrong diagnostic for the user
        # and hides a malformed provider behind a network-shaped one.
        exchange = CrowdedQualifiedExchange(MAX_DISCOVERY_ROWS + 1)
        observation = self.probe(exchange)
        self.assertTrue(observation.reachable)
        assert observation.outcome is not None
        self.assertIs(observation.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)
        self.assertIsNone(observation.identity)
        self.assertEqual((), observation.candidates)

    def test_readiness_costs_tags_then_show_and_nothing_else(self) -> None:
        exchange = QualifiedExchange()
        observation = self.probe(exchange)
        self.assertTrue(observation.reachable)
        self.assertIsNone(observation.outcome)
        self.assertEqual(
            exchange.calls,
            [
                ("GET", "/api/tags", None),
                ("POST", "/api/show", {"model": QUALIFIED_MODEL_ID, "verbose": False}),
            ],
        )

    def test_the_observation_names_what_it_proved(self) -> None:
        observation = self.probe(QualifiedExchange())
        identity = observation.identity
        assert identity is not None
        self.assertEqual(identity.profile_id, "fixture.qualified.local")
        self.assertEqual(identity.model_digest, QUALIFIED_EVIDENCE.model_digest)
        self.assertEqual(identity.qualification_sha256, QUALIFIED_EVIDENCE.show_identity_sha256)
        self.assertEqual(
            identity.endpoint_sha256, compute_endpoint_fingerprint(qualified_profile().endpoint)
        )
        self.assertGreaterEqual(identity.observed_at, 0.0)

    def test_a_profile_nobody_qualified_spends_no_second_request(self) -> None:
        """The catalog-only path is unchanged: reachable, no identity, one request."""

        exchange = QualifiedExchange()
        unqualified = replace(
            qualified_profile(),
            qualification_state=PromptModelQualificationState.CATALOG_ONLY,
            qualification_evidence=None,
        )
        observation = self.probe(exchange, unqualified)
        self.assertIsNone(observation.outcome)
        self.assertIsNone(observation.identity)
        self.assertEqual([path for _method, path, _payload in exchange.calls], ["/api/tags"])

    def test_drift_leaves_the_provider_reachable_and_unproved(self) -> None:
        for label, show in (
            (
                "shrunken window",
                {
                    **QualifiedExchange().show_payload(),
                    "model_info": {"qwen35.context_length": 8_192},
                },
            ),
            (
                "amended licence",
                {**QualifiedExchange().show_payload(), "license": QUALIFIED_LICENSE + " (v2)"},
            ),
            (
                "cannot complete",
                {**QualifiedExchange().show_payload(), "capabilities": ["embedding"]},
            ),
        ):
            with self.subTest(drift=label):
                observation = self.probe(QualifiedExchange(show=show))
                self.assertTrue(observation.reachable)
                self.assertIsNone(observation.identity)
                assert observation.outcome is not None
                self.assertIs(
                    observation.outcome.outcome_id, PromptModelOutcomeId.CAPABILITY_MISMATCH
                )

    def test_an_unreadable_show_response_is_not_authority_for_anything(self) -> None:
        observation = self.probe(QualifiedExchange(show={"license": QUALIFIED_LICENSE}))
        self.assertTrue(observation.reachable)
        self.assertIsNone(observation.identity)
        assert observation.outcome is not None
        self.assertIs(observation.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)

    def test_the_session_stamps_its_own_counters_onto_what_the_probe_saw(self) -> None:
        profile_under_test = qualified_profile()
        state = ProviderSettingsState(profiles=(profile_under_test,))

        def recheck(exchange: QualifiedExchange) -> None:
            state.apply(
                ProviderSettingsIntent.RECHECK_READINESS,
                {},
                readiness_probe=lambda _profile, _credential: self.probe(exchange),
            )

        state.apply(
            ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": profile_under_test.profile_id}
        )
        # A model may only be selected out of an admitted census, so the first recheck is what
        # makes the second selection possible at all.
        recheck(QualifiedExchange())
        selected = state.apply(
            ProviderSettingsIntent.SELECT_MODEL, {"model_id": QUALIFIED_MODEL_ID}
        )
        self.assertTrue(selected.accepted)
        # Selecting the model discarded the earlier proof: it was collected before this selection.
        self.assertIsNone(state.readiness_evidence)

        recheck(QualifiedExchange())
        evidence = state.readiness_evidence
        assert evidence is not None
        self.assertEqual(evidence.provider_revision, state.revision)
        self.assertEqual(evidence.authority_epoch, state.authority_epoch)
        endpoint = compute_endpoint_fingerprint(profile_under_test.endpoint)
        self.assertTrue(
            evidence.matches(profile_under_test, endpoint, state.revision, state.authority_epoch)
        )

        # Clearing the model moves the revision and drops the proof, so nothing is left holding a
        # statement about a selection that no longer exists.
        state.apply(ProviderSettingsIntent.CLEAR_MODEL, {})
        self.assertIsNone(state.readiness_evidence)
        self.assertFalse(
            evidence.matches(profile_under_test, endpoint, state.revision, state.authority_epoch)
        )

    def test_a_recheck_that_proves_nothing_replaces_the_proof_it_had(self) -> None:
        profile_under_test = qualified_profile()
        state = ProviderSettingsState(profiles=(profile_under_test,))

        def recheck(exchange: QualifiedExchange) -> None:
            state.apply(
                ProviderSettingsIntent.RECHECK_READINESS,
                {},
                readiness_probe=lambda _profile, _credential: self.probe(exchange),
            )

        state.apply(
            ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": profile_under_test.profile_id}
        )
        recheck(QualifiedExchange())
        state.apply(ProviderSettingsIntent.SELECT_MODEL, {"model_id": QUALIFIED_MODEL_ID})
        recheck(QualifiedExchange())
        self.assertIsNotNone(state.readiness_evidence)

        # The operator re-pulled the tag and the weights changed underneath the session.
        replaced = QualifiedExchange(
            show={**QualifiedExchange().show_payload(), "capabilities": ["embedding"]}
        )
        recheck(replaced)
        self.assertIsNone(state.readiness_evidence)


if __name__ == "__main__":
    unittest.main()
