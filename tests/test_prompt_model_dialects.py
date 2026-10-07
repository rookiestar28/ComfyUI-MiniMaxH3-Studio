"""Provider compatibility, final-channel and shared action-budget regressions."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from historical_prompt_model_fixtures import (
    load_historical_direct_call_catalog as load_prompt_model_catalog,
)
from test_m22_14_remote_provider_activation import _Connection, _Response, request_for

from comfyui_h3_context.adapters.assisted_draft_execution import _decode_candidate
from comfyui_h3_context.adapters.prompt_model_transport import (
    PromptModelActionState,
    PromptModelTransportError,
    RemoteHttpsExchange,
    RemoteSessionResult,
    run_remote_prompt_model_session,
)
from comfyui_h3_context.core.assisted_draft import DraftModelExecutionError
from comfyui_h3_context.core.prompt_model_dialects import DialectResponseError, RequestOptions
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcomeId,
    admit_egress_destination,
)
from comfyui_h3_context.core.prompt_model_session import build_request_payload, read_response_answer
from comfyui_h3_context.core.provider_setup import ProviderConsentStatus
from comfyui_h3_context.core.remote_prompt_model import RemoteConsentRecord, RuntimeCredential
from comfyui_h3_context.core.remote_provider_policy import policy_for_profile

PROFILES = tuple(
    row for row in load_prompt_model_catalog().profiles if not row.capabilities.local_only
)
PRIVATE = "provider-message-must-never-escape"


def answer(
    profile: PromptModelProfile, text: str = "final draft", reason: str | None = None
) -> dict[str, Any]:
    if profile.family is PromptModelFamily.REMOTE_ANTHROPIC:
        return {
            "type": "message",
            "role": "assistant",
            "model": profile.model_id,
            "stop_reason": reason or "end_turn",
            "content": [{"type": "text", "text": text}],
        }
    return {
        "model": profile.model_id,
        "choices": [
            {
                "index": 0,
                "finish_reason": reason or "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
    }


class Exchange:
    def __init__(
        self,
        profile: PromptModelProfile,
        replies: Sequence[Mapping[str, object] | BaseException],
        *,
        on_send: Callable[[str], None] | None = None,
    ) -> None:
        self.profile = profile
        self.replies = list(replies)
        self.calls: list[tuple[str, str, Mapping[str, object] | None, float | None]] = []
        self.on_send = on_send

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        self.calls.append((method, path, payload, timeout_seconds))
        if self.on_send is not None:
            self.on_send(method)
        if method == "GET":
            return {"data": [{"id": self.profile.model_id}]}
        response = self.replies.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def run(
    profile: PromptModelProfile,
    exchange: Exchange,
    state: PromptModelActionState | None = None,
    cancellation: Callable[[], bool] | None = None,
    options: RequestOptions | None = None,
) -> RemoteSessionResult:
    return run_remote_prompt_model_session(
        request_for(profile),
        exchange,
        selected_profile_id=profile.profile_id,
        consent=RemoteConsentRecord(profile.profile_id, ProviderConsentStatus.GRANTED, True, False),
        credential=RuntimeCredential("fixture-" + "D" * 40),  # pragma: allowlist secret
        action_state=state,
        cancellation=cancellation,
        options=options,
    )


class DialectTests(unittest.TestCase):
    def test_raw_gemini_error_prose_or_other_members_cannot_authorize_downgrade(self) -> None:
        profile = next(
            row for row in PROFILES if policy_for_profile(row).provider_id == "google_gemini"
        )
        for error in (
            {"message": "INVALID_ARGUMENT"},
            {"type": "INVALID_ARGUMENT", "message": PRIVATE},
            {"status": "unknown", "message": PRIVATE},
            {"status": 400, "message": PRIVATE},
        ):
            with self.subTest(error=error):
                calls: list[dict[str, object]] = []
                raw = json.dumps({"error": error}).encode()

                def factory(
                    _host: str,
                    _port: int,
                    *,
                    timeout: float,
                    raw_body: bytes = raw,
                    recorded_calls: list[dict[str, object]] = calls,
                ) -> _Connection:
                    return _Connection(_Response(400, raw_body), recorded_calls)

                exchange = RemoteHttpsExchange(
                    admit_egress_destination(profile.family, profile.endpoint),
                    RuntimeCredential("fixture-" + "D" * 40),  # pragma: allowlist secret
                    policy=policy_for_profile(profile),
                    connection_factory=factory,
                )
                with self.assertRaises(PromptModelTransportError) as caught:
                    exchange.request("POST", policy_for_profile(profile).chat_route, {})
                self.assertEqual(caught.exception.error_kind, "")
                self.assertNotIn(PRIVATE, str(caught.exception))
                self.assertEqual(len(calls), 1)

    def test_raw_gemini_invalid_argument_performs_only_one_safe_downgrade(self) -> None:
        profile = next(
            row for row in PROFILES if policy_for_profile(row).provider_id == "google_gemini"
        )
        policy = replace(policy_for_profile(profile), max_transmissions=4)
        calls: list[dict[str, object]] = []
        responses = [
            _Response(200, json.dumps({"data": [{"id": profile.model_id}]}).encode()),
            _Response(
                400,
                json.dumps({"error": {"status": "INVALID_ARGUMENT", "message": PRIVATE}}).encode(),
            ),
            _Response(200, json.dumps(answer(profile)).encode()),
        ]

        def factory(_host: str, _port: int, *, timeout: float) -> _Connection:
            return _Connection(responses.pop(0), calls)

        exchange = RemoteHttpsExchange(
            admit_egress_destination(profile.family, profile.endpoint),
            RuntimeCredential("fixture-" + "D" * 40),  # pragma: allowlist secret
            policy=policy,
            connection_factory=factory,
        )
        action = PromptModelActionState()
        result = run_remote_prompt_model_session(
            request_for(profile),
            exchange,
            selected_profile_id=profile.profile_id,
            consent=RemoteConsentRecord(
                profile.profile_id, ProviderConsentStatus.GRANTED, True, False
            ),
            credential=RuntimeCredential("fixture-" + "D" * 40),  # pragma: allowlist secret
            action_state=action,
        )
        self.assertTrue(result.completed)
        self.assertTrue(action.downgraded)
        self.assertEqual([call["method"] for call in calls], ["GET", "POST", "POST"])
        self.assertNotIn(PRIVATE, json.dumps(result.outcome.to_wire()))
        safe_payload = calls[2]["payload"]
        self.assertEqual(
            safe_payload, build_request_payload(request_for(profile), RequestOptions.safe())
        )

    def test_full_and_safe_payloads_keep_privacy_and_output_limits(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile.profile_id):
                request = request_for(profile)
                full = build_request_payload(request)
                safe = build_request_payload(request, RequestOptions.safe())
                self.assertEqual(safe["model"], profile.model_id)
                self.assertFalse(safe["stream"])
                self.assertEqual(safe["messages"], full["messages"])
                self.assertTrue(set(safe) < set(full))
                self.assertFalse({"modalities", "n", "tool_choice"} & set(full))
                if profile.family is PromptModelFamily.REMOTE_ANTHROPIC:
                    self.assertEqual(safe["max_tokens"], full["max_tokens"])
                    self.assertEqual(safe["system"], full["system"])
                else:
                    self.assertEqual(safe["max_completion_tokens"], full["max_completion_tokens"])
                    if profile.provider_label == "OpenAI":
                        self.assertIs(safe["store"], False)

    def test_one_downgrade_is_shared_with_repair_and_learned_only_for_the_session(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile.profile_id):
                bad = PromptModelTransportError(
                    PromptModelOutcomeId.PROVIDER_ERROR,
                    PRIVATE,
                    http_status=400,
                    error_kind="INVALID_ARGUMENT"
                    if policy_for_profile(profile).provider_id == "google_gemini"
                    else "invalid_request_error",
                )
                exchange = Exchange(profile, [bad, answer(profile), answer(profile)])
                preferences: set[tuple[str, str]] = set()
                state = PromptModelActionState(safe_preferences=preferences)
                first = run(profile, exchange, state)
                second = run(profile, exchange, state)
                self.assertTrue(first.completed)
                self.assertTrue(second.completed)
                self.assertEqual(
                    [call[0] for call in exchange.calls], ["GET", "POST", "POST", "POST"]
                )
                self.assertTrue(state.downgraded)
                self.assertEqual(exchange.calls[2][2], exchange.calls[3][2])
                exhausted = run(profile, exchange, state)
                self.assertIs(exhausted.outcome.outcome_id, PromptModelOutcomeId.REQUEST_TOO_LARGE)
                self.assertEqual(len(exchange.calls), 4)
                next_action = Exchange(profile, [answer(profile)])
                result = run(
                    profile, next_action, PromptModelActionState(safe_preferences=preferences)
                )
                self.assertTrue(result.completed)
                self.assertEqual(next_action.calls[1][2], exchange.calls[2][2])
                isolated = Exchange(profile, [answer(profile)])
                self.assertTrue(run(profile, isolated).completed)
                self.assertNotEqual(isolated.calls[1][2], next_action.calls[1][2])

    def test_a_second_error_and_noneligible_errors_do_not_retry_or_leak(self) -> None:
        profile = PROFILES[0]
        for status, kind, options in (
            (401, "invalid_request_error", None),
            (404, "invalid_request_error", None),
            (429, "invalid_request_error", None),
            (400, "unknown", None),
            (400, "invalid_request_error", RequestOptions.safe()),
        ):
            with self.subTest(status=status, kind=kind, options=options):
                failure = PromptModelTransportError(
                    PromptModelOutcomeId.PROVIDER_ERROR,
                    PRIVATE,
                    http_status=status,
                    error_kind=kind,
                )
                exchange = Exchange(profile, [failure])
                result = run(profile, exchange, options=options)
                self.assertFalse(result.completed)
                self.assertEqual(len(exchange.calls), 2)
                self.assertNotIn(PRIVATE, json.dumps(result.outcome.to_wire()))
        failure = PromptModelTransportError(
            PromptModelOutcomeId.PROVIDER_ERROR,
            PRIVATE,
            http_status=400,
            error_kind="invalid_request_error",
        )
        exchange = Exchange(profile, [failure, failure])
        result = run(profile, exchange)
        self.assertFalse(result.completed)
        self.assertEqual(len(exchange.calls), 3)
        self.assertNotIn(PRIVATE, json.dumps(result.outcome.to_wire()))

    def test_deadline_and_cancellation_cover_identity_downgrade_and_cached_repair(self) -> None:
        profile = PROFILES[0]
        now = [0.0]
        state = PromptModelActionState(clock=lambda: now[0])

        def identity_spent(method: str) -> None:
            if method == "GET":
                now[0] = 121.0

        exchange = Exchange(profile, [], on_send=identity_spent)
        self.assertIs(
            run(profile, exchange, state).outcome.outcome_id, PromptModelOutcomeId.TIMEOUT
        )
        self.assertEqual(len(exchange.calls), 1)
        now[0] = 0.0
        state = PromptModelActionState(clock=lambda: now[0])

        def nearly_spent(method: str) -> None:
            now[0] = 110.0 if method == "GET" else 121.0

        bad = PromptModelTransportError(
            PromptModelOutcomeId.PROVIDER_ERROR, http_status=400, error_kind="invalid_request_error"
        )
        exchange = Exchange(profile, [bad], on_send=nearly_spent)
        self.assertIs(
            run(profile, exchange, state).outcome.outcome_id, PromptModelOutcomeId.TIMEOUT
        )
        self.assertEqual(len(exchange.calls), 2)
        self.assertEqual(exchange.calls[1][3], 10.0)
        cancelled = [False]
        exchange = Exchange(profile, [answer(profile)])
        state = PromptModelActionState()
        self.assertTrue(run(profile, exchange, state, lambda: cancelled[0]).completed)
        cancelled[0] = True
        self.assertIs(
            run(profile, exchange, state, lambda: cancelled[0]).outcome.outcome_id,
            PromptModelOutcomeId.CANCELLED,
        )
        self.assertEqual(len(exchange.calls), 2)

    def test_refusals_truncation_and_reasoning_only_have_typed_outcomes(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile.profile_id):
                is_anthropic = profile.family is PromptModelFamily.REMOTE_ANTHROPIC
                cases = [
                    (
                        answer(profile, "partial", "max_tokens" if is_anthropic else "length"),
                        PromptModelOutcomeId.DRAFT_TRUNCATED,
                    ),
                    (
                        answer(profile, "", "max_tokens" if is_anthropic else "length"),
                        PromptModelOutcomeId.TRUNCATED_REASONING,
                    ),
                    (
                        answer(profile, "", "refusal" if is_anthropic else "content_filter"),
                        PromptModelOutcomeId.MODERATED,
                    ),
                ]
                for response, expected in cases:
                    with self.assertRaises(DialectResponseError) as caught:
                        read_response_answer(profile.family, response, profile.model_id)
                    self.assertIs(caught.exception.outcome_id, expected)
                if is_anthropic:
                    response = answer(profile)
                    response["content"].insert(0, {"type": "thinking", "thinking": PRIVATE})
                    self.assertEqual(
                        read_response_answer(profile.family, response, profile.model_id).text,
                        "final draft",
                    )
                    response["content"].pop()
                    with self.assertRaises(DialectResponseError):
                        read_response_answer(profile.family, response, profile.model_id)
                else:
                    response = answer(profile)
                    response["choices"][0]["message"]["reasoning"] = PRIVATE
                    self.assertEqual(
                        read_response_answer(profile.family, response, profile.model_id).text,
                        "final draft",
                    )
                    response["choices"][0]["message"]["content"] = ""
                    with self.assertRaises(DialectResponseError):
                        read_response_answer(profile.family, response, profile.model_id)

    def test_snapshot_suffix_is_reported_but_a_different_model_is_refused(self) -> None:
        for profile in PROFILES:
            response = answer(profile)
            response["model"] = profile.model_id + "-2026-10-06"
            observed = read_response_answer(profile.family, response, profile.model_id)
            self.assertEqual(observed.observed_model_id, response["model"])
            response["model"] = "different-" + profile.model_id
            with self.assertRaises(PromptModelContractError):
                read_response_answer(profile.family, response, profile.model_id)

    def test_openai_choice_index_is_an_integer_not_a_boolean_or_coerced_number(self) -> None:
        for profile in PROFILES:
            if profile.family is PromptModelFamily.REMOTE_ANTHROPIC:
                continue
            indices: tuple[object, ...] = (False, 0.0, "0", None, [], {})
            for index in indices:
                with self.subTest(profile=profile.profile_id, index=index):
                    response = answer(profile)
                    response["choices"][0]["index"] = index
                    with self.assertRaises(PromptModelContractError):
                        read_response_answer(profile.family, response, profile.model_id)

    def test_exact_fenced_json_and_unicode_guards_do_not_extract_arbitrary_prose(self) -> None:
        content = json.dumps(
            {"schema": "h3.prompt_model.draft_json.v1", "prompt_text": "落葉與微風"}
        )
        for value in (content, "```json\n" + content + "\n```", "```\n" + content + "\n```"):
            self.assertEqual(_decode_candidate(value).text, "落葉與微風")
        for value in (
            "prefix " + content,
            "```json\n" + content + "\n``` suffix",
            '{"schema":"h3.prompt_model.draft_json.v1","prompt_text":"a","prompt_text":"b"}',
            '{"schema":"h3.prompt_model.draft_json.v1","prompt_text":"\\u0000"}',
            '{"schema":"h3.prompt_model.draft_json.v1","prompt_text":"\\ud800"}',
        ):
            with self.assertRaises(DraftModelExecutionError):
                _decode_candidate(value)
