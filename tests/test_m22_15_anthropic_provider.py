"""Hermetic M22-15 native Anthropic provider contract tests."""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from unittest.mock import patch

from historical_prompt_model_fixtures import (
    load_historical_direct_call_catalog as load_prompt_model_catalog,
)

from comfyui_h3_context.adapters.prompt_model_transport import (
    PromptModelTransportError,
    RemoteHttpsExchange,
    resolve_live_identity,
)
from comfyui_h3_context.core.prompt_model_budget import (
    ContextProfileLadder,
    ContextProfileStep,
    PromptModelWorkload,
    plan_prompt_model_request,
)
from comfyui_h3_context.core.prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    PromptModelContractError,
    PromptModelDialect,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelQualificationState,
    RemotePromptModelQualificationEvidence,
    admit_egress_destination,
    route_for_family,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PROMPT_MODEL_CATALOG_V5_SCHEMA as PROMPT_MODEL_CATALOG_SCHEMA,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_session import (
    TRANSPORT_FAMILIES,
    PromptModelMessage,
    PromptModelRole,
    PromptModelSessionRequest,
    build_request_payload,
    parse_response_text,
)
from comfyui_h3_context.core.provider_settings import describe_transmission
from comfyui_h3_context.core.remote_prompt_model import (
    REMOTE_FAMILIES,
    SAFE_UPSTREAM_ERROR_CODES,
    RuntimeCredential,
    map_remote_outcome,
    scrub_upstream_error,
)
from comfyui_h3_context.core.remote_provider_policy import (
    RemoteCredentialScheme,
    policy_for_profile,
    remote_provider_policies,
)

CHECKED_ON = date(2026, 8, 23)
SECRET = "fixture-" + "A" * 40  # pragma: allowlist secret
PROFILE_ID = "anthropic.claude_sonnet_4_6.remote"
MODEL_ID = "claude-sonnet-4-6"
ENDPOINT = "https://api.anthropic.com"
LADDER = ContextProfileLadder(
    steps=(ContextProfileStep(context_tokens=16_384, memory_bytes=1024**3),)
)


def anthropic_profile() -> PromptModelProfile:
    return load_prompt_model_catalog().require(PROFILE_ID)


def request_for(profile: PromptModelProfile | None = None) -> PromptModelSessionRequest:
    selected = anthropic_profile() if profile is None else profile
    decision = plan_prompt_model_request(
        capabilities=selected.capabilities,
        ladder=LADDER,
        workload=PromptModelWorkload(
            characters=120,
            wide_characters=0,
            messages=3,
            visual_inputs=0,
            request_bytes=1_024,
            requested_output_tokens=128,
        ),
        requested_step=0,
        available_memory_bytes=8 * 1024**3,
    )
    if decision.plan is None:  # pragma: no cover - fixture construction invariant
        raise AssertionError("fixture plan was not admitted")
    return PromptModelSessionRequest(
        profile=selected,
        destination=admit_egress_destination(selected.family, selected.endpoint),
        plan=decision.plan,
        messages=(
            PromptModelMessage(role=PromptModelRole.SYSTEM, text="Return the contract."),
            PromptModelMessage(role=PromptModelRole.USER, text="first turn"),
            PromptModelMessage(role=PromptModelRole.ASSISTANT, text="prior answer"),
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
    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def __call__(self, _host: str, _port: int, *, timeout: float) -> _Connection:
        del timeout
        status, payload = self.responses.pop(0)
        return _Connection(
            _Response(status, json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            self.calls,
        )


class AnthropicCatalogAndPolicyTests(unittest.TestCase):
    def test_v5_catalog_declares_one_exact_qualified_anthropic_profile(self) -> None:
        from historical_prompt_model_fixtures import load_historical_catalog

        catalog = load_historical_catalog()
        profile = catalog.require(PROFILE_ID)

        self.assertEqual(PROMPT_MODEL_CATALOG_SCHEMA, "h3.prompt_model.profiles.v5")
        self.assertEqual(catalog.schema, PROMPT_MODEL_CATALOG_SCHEMA)
        self.assertIs(profile.family, PromptModelFamily.REMOTE_ANTHROPIC)
        self.assertIs(profile.wire_dialect, PromptModelDialect.ANTHROPIC_MESSAGES)
        self.assertEqual(profile.endpoint, ENDPOINT)
        self.assertEqual(profile.discovery_routes, ("/v1/models",))
        self.assertEqual(profile.chat_route, "/v1/messages")
        self.assertEqual(profile.model_id, MODEL_ID)
        self.assertEqual(profile.model_revision, MODEL_ID)
        self.assertIs(profile.qualification_state, PromptModelQualificationState.QUALIFIED)
        evidence = profile.qualification_evidence
        self.assertIsInstance(evidence, RemotePromptModelQualificationEvidence)
        assert isinstance(evidence, RemotePromptModelQualificationEvidence)
        self.assertIs(evidence.family, PromptModelFamily.REMOTE_ANTHROPIC)
        self.assertEqual(
            evidence.qualification_sha256,
            "sha256:72f85d17f32575de8b12751ae8c45dc999bdb15a396d2b473f2ce2cbbde726ad",
        )
        self.assertEqual(
            evidence.repository_commit,
            "bf67ff1ad1e454452ec1da912a3b20026cd14c72",  # pragma: allowlist secret
        )
        self.assertEqual(
            evidence.repository_tree,
            "90d9bea824934221df1a0ece97d2e15d670df56f",  # pragma: allowlist secret
        )
        self.assertEqual(evidence.observed_transmissions, 2)
        self.assertEqual(evidence.prompt_tokens, 249)
        self.assertEqual(evidence.completion_tokens, 36)
        self.assertEqual(evidence.actual_cost_micro_usd, 1_287)
        self.assertNotIn("remote_activation_pending", profile.limitations)
        self.assertIn("retention_depends_on_provider_policy", profile.limitations)
        self.assertFalse(profile.capabilities.local_only)
        self.assertTrue(profile.capabilities.requires_credential)
        self.assertEqual(profile.max_calls_per_action, 2)
        self.assertIn(profile.family, REMOTE_FAMILIES)
        self.assertIn(profile.family, TRANSPORT_FAMILIES)

    def test_policy_binds_native_auth_version_routes_model_and_cost(self) -> None:
        profile = anthropic_profile()
        policy = policy_for_profile(profile)

        self.assertEqual(policy.provider_id, "anthropic")
        self.assertEqual(policy.origin, ENDPOINT)
        self.assertEqual(policy.discovery_route, "/v1/models")
        self.assertEqual(policy.chat_route, "/v1/messages")
        self.assertIs(policy.credential_scheme, RemoteCredentialScheme.X_API_KEY)
        self.assertEqual(policy.api_version, "2023-06-01")
        self.assertEqual(policy.extra_headers, ())
        self.assertFalse(hasattr(policy, "input_price_micro_usd_per_million"))
        self.assertFalse(hasattr(policy, "output_price_micro_usd_per_million"))
        self.assertEqual(policy.max_transmissions, 2)

    def test_an_anthropic_profile_cannot_borrow_openai_dialect_or_origin(self) -> None:
        profile = anthropic_profile()
        for drifted in (
            replace(profile, wire_dialect=PromptModelDialect.OPENAI_CHAT_COMPLETIONS),
            replace(profile, endpoint="https://api.openai.com"),
            replace(profile, chat_route="/v1/chat/completions"),
        ):
            with self.subTest(drift=drifted.to_wire()):
                with self.assertRaises(PromptModelContractError):
                    policy_for_profile(drifted)

    def test_an_anthropic_policy_cannot_be_recomposed_as_bearer_or_openai(self) -> None:
        policy = policy_for_profile(anthropic_profile())
        for changes in (
            {"credential_scheme": RemoteCredentialScheme.BEARER, "api_version": None},
            {"origin": "https://api.openai.com"},
            {"chat_route": "/v1/chat/completions"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(PromptModelContractError) as caught:
                    replace(policy, **changes)
                self.assertEqual(caught.exception.code, "remote_policy_protocol")

    def test_disclosure_reuses_remote_consent_and_names_anthropic_cost_policy(self) -> None:
        profile = anthropic_profile()
        disclosure = describe_transmission(
            route_for_family(profile.family),
            profile.capabilities,
            profile=profile,
        )
        self.assertTrue(disclosure.consent_required)
        self.assertTrue(disclosure.requires_credential)
        self.assertEqual(disclosure.provider_id, "anthropic")
        self.assertEqual(disclosure.retention_policy, "provider_policy")
        self.assertTrue(
            {"max_input_tokens", "max_output_tokens", "max_cost_micro_usd"}.isdisjoint(
                disclosure.to_wire()
            )
        )

    def test_remote_evidence_is_family_discriminated(self) -> None:
        profile = anthropic_profile()
        policy = policy_for_profile(profile)
        evidence = RemotePromptModelQualificationEvidence(
            provider_id=policy.provider_id,
            profile_id=profile.profile_id,
            model_id=profile.model_id,
            policy_version=policy.policy_version,
            policy_sha256=policy.fingerprint,
            price_basis_id="historical.fixture",
            qualified_on="2026-08-23",
            source_checked_on="2026-08-23",
            price_valid_through="2026-09-22",
            max_transmissions=2,
            observed_transmissions=2,
            max_input_tokens=512,
            max_output_tokens=128,
            max_cost_micro_usd=10_000,
            prompt_tokens=32,
            completion_tokens=16,
            actual_cost_micro_usd=336,
            qualification_sha256="sha256:" + "1" * 64,
            repository_commit="2" * 40,
            repository_tree="3" * 40,
            adapter_version=profile.adapter_version,
            parser_version=profile.parser_version,
            family=PromptModelFamily.REMOTE_ANTHROPIC,
        )
        self.assertEqual(evidence.to_wire()["kind"], "remote_anthropic")
        qualified = replace(
            profile,
            qualification_state=PromptModelQualificationState.QUALIFIED,
            qualification_evidence=evidence,
        )
        with self.assertRaises(PromptModelContractError):
            policy_for_profile(qualified)


class AnthropicMessageDialectTests(unittest.TestCase):
    def test_system_messages_and_structured_output_use_the_native_shape(self) -> None:
        payload = build_request_payload(request_for())

        self.assertEqual(payload["model"], MODEL_ID)
        self.assertEqual(payload["system"], "Return the contract.")
        self.assertEqual(
            payload["messages"],
            [
                {"role": "user", "content": "first turn"},
                {"role": "assistant", "content": "prior answer"},
                {"role": "user", "content": "fixture input"},
            ],
        )
        self.assertEqual(payload["max_tokens"], 128)
        self.assertIs(payload["stream"], False)
        output_config = payload["output_config"]
        self.assertIsInstance(output_config, Mapping)
        assert isinstance(output_config, Mapping)
        output_format = output_config["format"]
        self.assertIsInstance(output_format, Mapping)
        assert isinstance(output_format, Mapping)
        self.assertEqual(output_format["type"], "json_schema")
        schema = output_format["schema"]
        self.assertIsInstance(schema, Mapping)
        assert isinstance(schema, Mapping)
        self.assertEqual(schema["required"], ["schema", "prompt_text"])
        for forbidden in (
            "response_format",
            "max_completion_tokens",
            "tools",
            "tool_choice",
            "thinking",
            "cache_control",
            "modalities",
            "reasoning_effort",
        ):
            self.assertNotIn(forbidden, payload)

    def test_system_position_and_media_are_refused_before_transport(self) -> None:
        base = request_for()
        cases = (
            replace(
                base,
                messages=(
                    PromptModelMessage(role=PromptModelRole.USER, text="first"),
                    PromptModelMessage(role=PromptModelRole.SYSTEM, text="late"),
                ),
            ),
            replace(base, image_payloads=("aGVsbG8=",)),
        )
        for request in cases:
            with self.subTest(messages=request.messages, images=request.image_payloads):
                with self.assertRaises(PromptModelContractError):
                    build_request_payload(request)

    def test_response_requires_exact_model_finality_role_and_one_text_block(self) -> None:
        valid = {
            "type": "message",
            "role": "assistant",
            "model": MODEL_ID,
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "fixture result"}],
        }
        self.assertEqual(
            parse_response_text(PromptModelFamily.REMOTE_ANTHROPIC, valid, MODEL_ID),
            "fixture result",
        )
        cases = (
            {**valid, "type": "other"},
            {**valid, "role": "user"},
            {**valid, "model": "claude-wrong"},
            {**valid, "stop_reason": "max_tokens"},
            {**valid, "content": []},
            {**valid, "content": [{"type": "thinking", "thinking": "hidden"}]},
            {
                **valid,
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ],
            },
        )
        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(PromptModelContractError):
                    parse_response_text(PromptModelFamily.REMOTE_ANTHROPIC, response, MODEL_ID)

    def test_models_listing_uses_the_declared_route_and_exact_id(self) -> None:
        class Exchange:
            calls: list[tuple[str, str]] = []

            def request(self, method: str, path: str) -> Mapping[str, object]:
                self.calls.append((method, path))
                return {"data": [{"id": MODEL_ID, "display_name": "ignored"}]}

        exchange = Exchange()
        identity = resolve_live_identity(
            PromptModelFamily.REMOTE_ANTHROPIC,
            MODEL_ID,
            exchange,
            discovery_route="/v1/models",
        )
        self.assertIsNotNone(identity)
        self.assertEqual(exchange.calls, [("GET", "/v1/models")])

    def test_models_listing_refuses_ambiguous_malformed_or_unbounded_rows(self) -> None:
        class Exchange:
            def __init__(self, data: object) -> None:
                self.data = data

            def request(self, method: str, path: str) -> Mapping[str, object]:
                del method, path
                return {"data": self.data}

        for data in (
            [{"id": MODEL_ID}, {"id": MODEL_ID}],
            [{"id": MODEL_ID}, "malformed"],
            # M22-19. Derived, not literal: this row was `MAX_REMOTE_MODEL_ROWS + 1` back when
            # the transport carried its own 256-row ceiling. The refusal is the assertion; the
            # count must follow the one ceiling rather than pin a retired second one.
            [{"id": f"other-{index}"} for index in range(MAX_DISCOVERY_ROWS + 1)],
        ):
            with self.subTest(rows=len(data)):
                with self.assertRaises(PromptModelTransportError) as caught:
                    resolve_live_identity(
                        PromptModelFamily.REMOTE_ANTHROPIC,
                        MODEL_ID,
                        Exchange(data),
                        discovery_route="/v1/models",
                    )
                self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)


class AnthropicTransportBoundaryTests(unittest.TestCase):
    def exchange(
        self, responses: list[tuple[int, object]]
    ) -> tuple[RemoteHttpsExchange, _FixtureFactory]:
        profile = anthropic_profile()
        factory = _FixtureFactory(responses)
        return (
            RemoteHttpsExchange(
                admit_egress_destination(profile.family, profile.endpoint),
                RuntimeCredential(SECRET),
                policy=policy_for_profile(profile),
                connection_factory=factory,
            ),
            factory,
        )

    def test_api_key_and_version_headers_exist_only_at_egress(self) -> None:
        exchange, factory = self.exchange([(200, {"data": [{"id": MODEL_ID}], "has_more": False})])
        exchange.request("GET", "/v1/models")
        headers = factory.calls[0]["headers"]
        self.assertEqual(
            headers,
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": SECRET,
                "anthropic-version": "2023-06-01",
            },
        )
        self.assertIsInstance(headers, Mapping)
        assert isinstance(headers, Mapping)
        self.assertNotIn("Authorization", headers)
        self.assertNotIn(SECRET, repr(exchange))
        self.assertNotIn(SECRET, str(exchange.metrics))

    def test_the_production_exchange_uses_the_killable_bounded_resolver(self) -> None:
        profile = anthropic_profile()
        with (
            patch(
                "comfyui_h3_context.adapters.prompt_model_transport.resolve_pinned_address_bounded",
                side_effect=PromptModelTransportError(PromptModelOutcomeId.TIMEOUT, ""),
            ) as bounded,
            patch(
                "comfyui_h3_context.adapters.prompt_model_transport._resolve_pinned_address",
                side_effect=AssertionError("the unbounded resolver must not be reachable"),
            ) as unbounded,
        ):
            # CRITICAL: construct inside the patch. The legacy implementation captured its
            # resolver during __init__; constructing earlier would let this RED test reach DNS.
            exchange = RemoteHttpsExchange(
                admit_egress_destination(profile.family, profile.endpoint),
                RuntimeCredential(SECRET),
                policy=policy_for_profile(profile),
                timeout_seconds=0.25,
            )
            with self.assertRaises(PromptModelTransportError) as caught:
                exchange.request("GET", "/v1/models")

        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.TIMEOUT)
        bounded.assert_called_once_with(
            "api.anthropic.com", timeout_seconds=0.25, loopback_required=False
        )
        unbounded.assert_not_called()
        self.assertEqual(exchange.transmission_count, 0)

    def test_native_usage_includes_cache_input_counts_and_never_request_id(self) -> None:
        exchange, _factory = self.exchange(
            [
                (
                    200,
                    {
                        "id": "msg_fixture",
                        "type": "message",
                        "role": "assistant",
                        "model": MODEL_ID,
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "fixture"}],
                        "usage": {
                            "input_tokens": 50,
                            "cache_creation_input_tokens": 7,
                            "cache_read_input_tokens": 11,
                            "output_tokens": 13,
                        },
                    },
                )
            ]
        )
        exchange.request("POST", "/v1/messages", {"model": MODEL_ID})
        metrics = exchange.metrics
        if metrics is None:  # pragma: no cover - fixture invariant
            raise AssertionError("metrics missing")
        self.assertEqual(metrics.prompt_tokens, 68)
        self.assertEqual(metrics.completion_tokens, 13)
        self.assertTrue(metrics.usage_present)
        self.assertFalse(hasattr(metrics, "request_id"))

    def test_missing_usage_does_not_require_provider_billing_accounting(self) -> None:
        exchange, _factory = self.exchange([(200, {"model": MODEL_ID})])
        self.assertEqual(
            exchange.request("POST", "/v1/messages", {"model": MODEL_ID}), {"model": MODEL_ID}
        )
        assert exchange.metrics is not None
        self.assertFalse(exchange.metrics.usage_present)

    def test_malformed_native_usage_is_refused(self) -> None:
        for usage in (
            "invalid-usage-shape",
            {"input_tokens": -1, "output_tokens": 1},
            {"input_tokens": 1, "output_tokens": "1"},
            {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": -1},
        ):
            with self.subTest(usage=usage):
                exchange, _factory = self.exchange([(200, {"usage": usage})])
                with self.assertRaises(PromptModelTransportError) as caught:
                    exchange.request("POST", "/v1/messages", {"model": MODEL_ID})
                self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)

    def test_anthropic_error_types_are_closed_and_typed_without_request_id_authority(self) -> None:
        expected = {
            "authentication_error": PromptModelOutcomeId.AUTHENTICATION,
            "billing_error": PromptModelOutcomeId.QUOTA,
            "permission_error": PromptModelOutcomeId.PERMISSION_DENIED,
            "not_found_error": PromptModelOutcomeId.MODEL_MISSING,
            "request_too_large": PromptModelOutcomeId.REQUEST_TOO_LARGE,
            "rate_limit_error": PromptModelOutcomeId.RATE_LIMITED,
            "timeout_error": PromptModelOutcomeId.TIMEOUT,
            "overloaded_error": PromptModelOutcomeId.PROVIDER_ERROR,
        }
        self.assertTrue(set(expected).issubset(SAFE_UPSTREAM_ERROR_CODES))
        for code, outcome in expected.items():
            with self.subTest(code=code):
                error = scrub_upstream_error(
                    500,
                    {
                        "type": "error",
                        "error": {"type": code, "message": "untrusted fixture"},
                        "request_id": "req_must_not_survive",
                    },
                )
                self.assertEqual(error.code, code)
                self.assertIs(map_remote_outcome(error), outcome)
                self.assertNotIn("req_must_not_survive", str(error.to_wire()))

    def test_only_exact_native_routes_are_admitted(self) -> None:
        exchange, _factory = self.exchange([])
        for method, path in (
            ("GET", "/v1/chat/completions"),
            ("POST", "/v1/chat/completions"),
            ("GET", "/v1/messages"),
            ("POST", "/v1/models"),
        ):
            with self.subTest(method=method, path=path):
                with self.assertRaises(PromptModelTransportError):
                    exchange.request(method, path)

    def test_closed_policy_inventory_adds_only_anthropic(self) -> None:
        self.assertEqual(
            {(policy.provider_id, policy.profile_id) for policy in remote_provider_policies()},
            {
                ("openai", "openai.remote"),
                ("google_gemini", "gemini.remote"),
                ("anthropic", "anthropic.remote"),
            },
        )


if __name__ == "__main__":
    unittest.main()
