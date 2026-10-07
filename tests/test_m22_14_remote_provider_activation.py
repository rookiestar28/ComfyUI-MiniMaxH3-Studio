"""Hermetic M22-14 provider dialect, disclosure, cost and receipt fixtures."""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from historical_prompt_model_fixtures import (
    load_historical_direct_call_catalog as load_prompt_model_catalog,
)

from comfyui_h3_context.adapters.prompt_model_transport import (
    PromptModelTransportError,
    RemoteHttpsExchange,
    decode_bounded_json,
    run_remote_prompt_model_session,
)
from comfyui_h3_context.core.prompt_model_budget import (
    ContextProfileLadder,
    ContextProfileStep,
    PromptModelWorkload,
    plan_prompt_model_request,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcomeId,
    admit_egress_destination,
)
from comfyui_h3_context.core.prompt_model_session import (
    PromptModelMessage,
    PromptModelRole,
    PromptModelSessionRequest,
    parse_response_text,
)
from comfyui_h3_context.core.provider_settings import (
    ProviderSettingsIntent,
    ProviderSettingsState,
    ReadinessObservation,
)
from comfyui_h3_context.core.provider_setup import ProviderConsentStatus
from comfyui_h3_context.core.remote_prompt_model import RemoteConsentRecord, RuntimeCredential
from comfyui_h3_context.core.remote_provider_policy import (
    policy_for_profile,
)

CHECKED_ON = date(2026, 8, 23)
SECRET = "fixture-" + "T" * 40  # pragma: allowlist secret
LADDER = ContextProfileLadder(
    steps=(ContextProfileStep(context_tokens=16_384, memory_bytes=1024**3),)
)


def remote_profiles() -> tuple[PromptModelProfile, ...]:
    return tuple(
        profile
        for profile in load_prompt_model_catalog().profiles
        if profile.family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
    )


def request_for(
    profile: PromptModelProfile, *, characters: int = 120, output_tokens: int = 128
) -> PromptModelSessionRequest:
    decision = plan_prompt_model_request(
        capabilities=profile.capabilities,
        ladder=LADDER,
        workload=PromptModelWorkload(
            characters=characters,
            wide_characters=0,
            messages=2,
            visual_inputs=0,
            request_bytes=1_024,
            requested_output_tokens=output_tokens,
        ),
        requested_step=0,
        available_memory_bytes=8 * 1024**3,
    )
    if decision.plan is None:  # pragma: no cover - fixture construction invariant
        raise AssertionError("fixture plan was not admitted")
    return PromptModelSessionRequest(
        profile=profile,
        destination=admit_egress_destination(profile.family, profile.endpoint),
        plan=decision.plan,
        messages=(
            PromptModelMessage(role=PromptModelRole.SYSTEM, text="Return the contract."),
            PromptModelMessage(role=PromptModelRole.USER, text="fixture input"),
        ),
    )


@dataclass
class _Response:
    status: int
    body: bytes

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class _Connection:
    def __init__(self, response: _Response, calls: list[dict[str, object]]) -> None:
        self.response = response
        self.calls = calls

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "payload": None if body is None else json.loads(body),
                "headers": dict(headers or {}),
            }
        )

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        return None


class _FixtureFactory:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def __call__(self, _host: str, _port: int, *, timeout: float) -> _Connection:
        del timeout
        payload = self.responses.pop(0)
        return _Connection(
            _Response(200, json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            self.calls,
        )


# M22-17. What each live service was observed to answer with on its listing route, recorded from the
# retained M22-14 qualification artifacts and the providers' own published interfaces. This is
# deliberately NOT derived from the adapter's `_CATALOG_IDENTIFIER_PREFIXES`: a fixture that returns
# whatever the comparison happens to search for asserts the assumption under test instead of
# exercising it, and that is exactly how the Gemini catalog spelling reached a live run undetected.
_OBSERVED_CATALOG_IDENTIFIERS: Mapping[str, str] = {
    "openai.gpt_5_6_terra.remote": "{model_id}",
    "gemini.gemini_3_7_flash.remote": "models/{model_id}",
    "anthropic.claude_sonnet_4_6.remote": "{model_id}",
}


def _observed_catalog_identifier(profile: object) -> str:
    """The exact string this provider's listing route puts in a row's `id`."""

    template = _OBSERVED_CATALOG_IDENTIFIERS.get(profile.profile_id, "{model_id}")  # type: ignore[attr-defined]
    return template.format(model_id=profile.model_id)  # type: ignore[attr-defined]


class ExactDialectFixtureTests(unittest.TestCase):
    def test_each_curated_provider_uses_only_its_routes_headers_and_closed_payload(self) -> None:
        for profile in remote_profiles():
            with self.subTest(profile=profile.profile_id):
                policy = policy_for_profile(profile)
                factory = _FixtureFactory(
                    [
                        {"data": [{"id": _observed_catalog_identifier(profile)}]},
                        {
                            "model": profile.model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "finish_reason": "stop",
                                    "message": {
                                        "role": "assistant",
                                        "content": json.dumps(
                                            {
                                                "schema": "h3.prompt_model.draft_json.v1",
                                                "prompt_text": "fixture result",
                                            }
                                        ),
                                    },
                                }
                            ],
                            "usage": {"prompt_tokens": 96, "completion_tokens": 32},
                        },
                    ]
                )
                exchange = RemoteHttpsExchange(
                    admit_egress_destination(profile.family, profile.endpoint),
                    RuntimeCredential(SECRET),
                    policy=policy,
                    connection_factory=factory,
                )
                consent = RemoteConsentRecord(
                    profile_id=profile.profile_id,
                    status=ProviderConsentStatus.GRANTED,
                    network_permitted=True,
                    media_upload_consented=False,
                )
                result = run_remote_prompt_model_session(
                    request_for(profile),
                    exchange,
                    selected_profile_id=profile.profile_id,
                    consent=consent,
                    credential=RuntimeCredential(SECRET),
                    on_date=CHECKED_ON,
                )

                self.assertTrue(result.completed)
                self.assertEqual(
                    [(call["method"], call["path"]) for call in factory.calls],
                    [("GET", policy.discovery_route), ("POST", policy.chat_route)],
                )
                headers = factory.calls[1]["headers"]
                if not isinstance(headers, dict):  # pragma: no cover - fixture invariant
                    raise AssertionError("headers not recorded")
                self.assertEqual(headers["Authorization"], "Bearer " + SECRET)
                self.assertEqual(
                    headers.get("x-goog-api-client"),
                    dict(policy.extra_headers).get("x-goog-api-client"),
                )
                payload = factory.calls[1]["payload"]
                if not isinstance(payload, dict):  # pragma: no cover - fixture invariant
                    raise AssertionError("payload not recorded")
                self.assertEqual(payload["max_completion_tokens"], 128)
                self.assertNotIn("max_tokens", payload)
                self.assertEqual(payload["reasoning_effort"], policy.reasoning_effort)
                self.assertFalse({"tool_choice", "modalities", "n"} & set(payload))
                self.assertEqual(payload.get("store"), policy.store)
                self.assertTrue(payload["response_format"]["json_schema"]["strict"])
                if result.receipt is None:  # pragma: no cover - fixture invariant
                    raise AssertionError("receipt missing")
                self.assertEqual(result.receipt.policy_sha256, policy.fingerprint)
                self.assertTrue(result.receipt.usage_present)
                self.assertTrue(
                    {
                        "price_basis_id",
                        "actual_cost_micro_usd",
                        "maximum_cost_micro_usd",
                    }.isdisjoint(result.receipt.to_wire())
                )
                self.assertEqual(exchange.transmission_count, 2)
                with self.assertRaises(PromptModelTransportError) as caught:
                    exchange.request("GET", policy.discovery_route)
                self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.REQUEST_TOO_LARGE)
                self.assertEqual(len(factory.calls), policy.max_transmissions)

    def test_duplicate_json_keys_are_refused_at_the_provider_boundary(self) -> None:
        with self.assertRaises(PromptModelTransportError) as caught:
            decode_bounded_json(b'{"usage":{"prompt_tokens":1,"prompt_tokens":2}}')
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)

    def test_all_profiles_draft_beyond_old_fee_caps_with_obsolete_or_absent_cost_data(self) -> None:
        for profile in load_prompt_model_catalog().profiles:
            if not profile.profile_id.endswith(".remote"):
                continue
            policy = policy_for_profile(profile)
            for output_tokens in (512, 2_000):
                with self.subTest(profile=profile.profile_id, output_tokens=output_tokens):
                    calls: list[tuple[str, str]] = []
                    payloads: list[Mapping[str, object]] = []

                    class Exchange:
                        def __init__(
                            self,
                            selected: PromptModelProfile,
                            recorded_calls: list[tuple[str, str]],
                            recorded_payloads: list[Mapping[str, object]],
                        ) -> None:
                            self.profile = selected
                            self.calls = recorded_calls
                            self.payloads = recorded_payloads

                        def request(
                            self,
                            method: str,
                            path: str,
                            payload: Mapping[str, object] | None = None,
                            *,
                            timeout_seconds: float | None = None,
                        ) -> Mapping[str, object]:
                            profile = self.profile
                            calls = self.calls
                            payloads = self.payloads
                            calls.append((method, path))
                            if method == "GET":
                                identifier = (
                                    profile.model_id
                                    if profile.family is PromptModelFamily.REMOTE_ANTHROPIC
                                    else _observed_catalog_identifier(profile)
                                )
                                return {"data": [{"id": identifier}]}
                            assert payload is not None
                            payloads.append(payload)
                            text = json.dumps(
                                {
                                    "schema": "h3.prompt_model.draft_json.v1",
                                    "prompt_text": "fixture result",
                                }
                            )
                            if profile.family is PromptModelFamily.REMOTE_ANTHROPIC:
                                return {
                                    "type": "message",
                                    "role": "assistant",
                                    "model": profile.model_id,
                                    "stop_reason": "end_turn",
                                    "content": [{"type": "text", "text": text}],
                                }
                            return {
                                "model": profile.model_id,
                                "choices": [
                                    {
                                        "index": 0,
                                        "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": text},
                                    }
                                ],
                            }

                    request = request_for(profile, characters=6_000, output_tokens=output_tokens)
                    self.assertGreater(request.plan.estimated_input_tokens, 512)
                    result = run_remote_prompt_model_session(
                        request,
                        Exchange(profile, calls, payloads),
                        selected_profile_id=profile.profile_id,
                        consent=RemoteConsentRecord(
                            profile_id=profile.profile_id,
                            status=ProviderConsentStatus.GRANTED,
                            network_permitted=True,
                            media_upload_consented=False,
                        ),
                        credential=RuntimeCredential(SECRET),
                        on_date=date(2099, 1, 1),
                    )
                    self.assertTrue(result.completed, result.outcome.to_wire())
                    self.assertEqual(
                        calls, [("GET", policy.discovery_route), ("POST", policy.chat_route)]
                    )
                    key = (
                        "max_tokens"
                        if profile.family is PromptModelFamily.REMOTE_ANTHROPIC
                        else "max_completion_tokens"
                    )
                    self.assertEqual(payloads[0][key], output_tokens)
                    self.assertIsNotNone(result.receipt)
                    assert result.receipt is not None

    def test_missing_cost_authority_does_not_block_provider_io(self) -> None:
        profile = remote_profiles()[0]
        policy = policy_for_profile(profile)
        factory = _FixtureFactory(
            [
                {"data": [{"id": profile.model_id}]},
                {
                    "model": profile.model_id,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "fixture result"},
                        }
                    ],
                },
            ]
        )
        exchange = RemoteHttpsExchange(
            admit_egress_destination(profile.family, profile.endpoint),
            RuntimeCredential(SECRET),
            policy=policy,
            connection_factory=factory,
        )
        result = run_remote_prompt_model_session(
            request_for(profile),
            exchange,
            selected_profile_id=profile.profile_id,
            consent=RemoteConsentRecord(
                profile_id=profile.profile_id,
                status=ProviderConsentStatus.GRANTED,
                network_permitted=True,
                media_upload_consented=False,
            ),
            credential=RuntimeCredential(SECRET),
            on_date=date(2099, 1, 1),
        )

        self.assertTrue(result.completed)
        self.assertIsNotNone(result.receipt)
        self.assertEqual(len(factory.calls), 2)

    def test_remote_response_contract_rejects_unrequested_or_ambiguous_shapes(self) -> None:
        profile = remote_profiles()[0]
        valid_choice = {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "fixture result"},
        }
        cases = (
            (
                "response_model",
                {"model": "wrong-model", "choices": [valid_choice]},
            ),
            (
                "response_choices",
                {"model": profile.model_id, "choices": [valid_choice, valid_choice]},
            ),
            (
                "response_truncated",
                {
                    "model": profile.model_id,
                    "choices": [{**valid_choice, "finish_reason": "length"}],
                },
            ),
            (
                "response_role",
                {
                    "model": profile.model_id,
                    "choices": [
                        {**valid_choice, "message": {"role": "tool", "content": "fixture"}}
                    ],
                },
            ),
            (
                "response_refused",
                {
                    "model": profile.model_id,
                    "choices": [
                        {
                            **valid_choice,
                            "message": {
                                "role": "assistant",
                                "content": "fixture",
                                "refusal": "not served",
                            },
                        }
                    ],
                },
            ),
        )

        for expected_code, response in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(PromptModelContractError) as caught:
                    parse_response_text(profile.family, response, profile.model_id)
                self.assertEqual(caught.exception.code, expected_code)


class SettingsCostAuthorityTests(unittest.TestCase):
    def state(self, observed: date) -> ProviderSettingsState:
        return ProviderSettingsState(
            profiles=load_prompt_model_catalog().profiles,
            _today=lambda: observed,
        )

    def test_network_consent_does_not_acknowledge_repository_fee_limits(self) -> None:
        profile = remote_profiles()[0]
        state = self.state(CHECKED_ON)
        state.apply(ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": profile.profile_id})
        disclosure = state.project().disclosure
        if disclosure is None:  # pragma: no cover - fixture invariant
            raise AssertionError("disclosure missing")
        self.assertEqual(disclosure.provider_id, "openai")
        granted = state.apply(ProviderSettingsIntent.GRANT_CONSENT, {"network_permitted": True})
        self.assertTrue(granted.accepted)
        consent = granted.projection.consent
        if consent is None:  # pragma: no cover - fixture invariant
            raise AssertionError("consent missing")

    def test_all_three_profiles_grant_network_consent_after_old_price_expiry(self) -> None:
        for profile in load_prompt_model_catalog().profiles:
            if not profile.profile_id.endswith(".remote"):
                continue
            for observed in (
                date(2026, 9, 22),
                date(2026, 9, 23),
                date(2026, 10, 1),
                date(2099, 1, 1),
            ):
                with self.subTest(profile=profile.profile_id, observed=observed):
                    state = self.state(observed)
                    state.apply(
                        ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": profile.profile_id}
                    )
                    granted = state.apply(
                        ProviderSettingsIntent.GRANT_CONSENT, {"network_permitted": True}
                    )
                    self.assertTrue(granted.accepted)
                    self.assertIsNotNone(granted.projection.consent)
                    assert granted.projection.consent is not None
                    state.apply(ProviderSettingsIntent.SUBMIT_CREDENTIAL, {"credential": SECRET})
                    state.apply(ProviderSettingsIntent.GRANT_CONSENT, {"network_permitted": True})
                    calls: list[str] = []

                    def probe(
                        selected: PromptModelProfile, _credential: object, _calls: list[str] = calls
                    ) -> ReadinessObservation:
                        _calls.append(selected.profile_id)
                        return ReadinessObservation(reachable=True)

                    checked = state.recheck(probe)
                    self.assertEqual(calls, [profile.profile_id])
                    self.assertIsNone(checked.projection.diagnostic)

    def test_readiness_census_is_independent_of_expired_price_data(self) -> None:
        profile = remote_profiles()[0]
        observed = [CHECKED_ON]
        state = ProviderSettingsState(
            profiles=load_prompt_model_catalog().profiles,
            _today=lambda: observed[0],
        )
        state.apply(ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": profile.profile_id})
        state.apply(ProviderSettingsIntent.SUBMIT_CREDENTIAL, {"credential": SECRET})
        granted = state.apply(ProviderSettingsIntent.GRANT_CONSENT, {"network_permitted": True})
        self.assertTrue(granted.accepted)
        observed[0] = date(2026, 9, 23)

        calls: list[str] = []

        def probe(selected: PromptModelProfile, _credential: object) -> ReadinessObservation:
            calls.append(selected.profile_id)
            return ReadinessObservation(reachable=True)

        result = state.recheck(probe)
        self.assertEqual(calls, [profile.profile_id])
        self.assertIsNone(result.projection.diagnostic)


if __name__ == "__main__":
    unittest.main()
