"""Native ceilings and downgrade admission through the actual session transport."""

from collections.abc import Mapping

import pytest
from test_provider_connection import choose, connect, connection_state, listed

from comfyui_h3_context.adapters.prompt_model_transport import (
    ACTION_DEADLINE_SECONDS,
    PromptModelActionState,
    PromptModelTransportError,
    run_prompt_model_session,
)
from comfyui_h3_context.core.prompt_model_budget import (
    ContextProfileLadder,
    ContextProfileStep,
    PromptModelWorkload,
    plan_prompt_model_request,
)
from comfyui_h3_context.core.prompt_model_provider import (
    ModelChoice,
    ModelMetadata,
    PromptModelOutcomeId,
    admit_egress_destination,
    load_prompt_model_catalog,
)
from comfyui_h3_context.core.prompt_model_session import (
    LiveModelIdentity,
    PromptModelMessage,
    PromptModelRole,
    PromptModelSessionRequest,
)
from comfyui_h3_context.core.provider_settings import ProviderReadiness

MODEL = "native-text-model"


def request(facts: ModelMetadata) -> PromptModelSessionRequest:
    profile = load_prompt_model_catalog().require("openai.remote")
    decision = plan_prompt_model_request(
        capabilities=profile.capabilities,
        ladder=ContextProfileLadder((ContextProfileStep(32768, 1),)),
        workload=PromptModelWorkload(40, 0, 2, 0, 40, 2048),
        requested_step=0,
        available_memory_bytes=1,
    )
    assert decision.plan is not None
    return PromptModelSessionRequest(
        profile=profile,
        destination=admit_egress_destination(profile.family, profile.endpoint),
        plan=decision.plan,
        messages=(
            PromptModelMessage(PromptModelRole.SYSTEM, "Synthetic instruction."),
            PromptModelMessage(PromptModelRole.USER, "Synthetic content."),
        ),
        model=ModelChoice(profile.profile_id, MODEL, "sha256:" + "a" * 64, 1.0, facts),
        base_output_tokens=2048,
    )


def state() -> PromptModelActionState:
    result = PromptModelActionState()
    result.identity = LiveModelIdentity(MODEL, None)
    result.identity_key = ("openai.remote", MODEL)
    return result


class Exchange:
    def __init__(self, *, invalid_first: bool = False) -> None:
        self.invalid_first = invalid_first
        self.payloads: list[Mapping[str, object]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        assert method == "POST" and payload is not None
        self.payloads.append(payload)
        if self.invalid_first and len(self.payloads) == 1:
            raise PromptModelTransportError(
                PromptModelOutcomeId.PROVIDER_ERROR,
                "",
                http_status=400,
                error_kind="invalid_request_error",
            )
        return {
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Synthetic result."},
                }
            ],
        }


@pytest.mark.parametrize(
    "facts,expected",
    [
        (ModelMetadata(), 2048),
        (ModelMetadata(reasoning_mandatory=True), 8192),
        (ModelMetadata(reasoning_control_supported=False), 8192),
        (ModelMetadata(max_output_tokens=1000), 1000),
    ],
)
def test_actual_wire_respects_native_output_and_reasoning(
    facts: ModelMetadata, expected: int
) -> None:
    exchange = Exchange()
    result = run_prompt_model_session(request(facts), exchange, action_state=state())
    assert result.answer is not None
    assert exchange.payloads[0]["max_completion_tokens"] == expected


def test_downgrade_replans_and_cached_repair_keeps_original_action() -> None:
    exchange = Exchange(invalid_first=True)
    action = state()
    req = request(ModelMetadata())
    assert run_prompt_model_session(req, exchange, action_state=action).answer is not None
    assert [p["max_completion_tokens"] for p in exchange.payloads] == [2048, 8192]
    assert run_prompt_model_session(req, exchange, action_state=action).answer is not None
    assert exchange.payloads[-1]["max_completion_tokens"] == 8192
    assert action.transmissions == 3 and action.downgraded


def test_failed_downgrade_replan_cannot_issue_second_post() -> None:
    exchange = Exchange(invalid_first=True)
    result = run_prompt_model_session(
        request(ModelMetadata(max_input_tokens=5000)), exchange, action_state=state()
    )
    assert (
        result.answer is None and result.outcome.outcome_id is PromptModelOutcomeId.CONTEXT_EXCEEDED
    )
    assert len(exchange.payloads) == 1


def test_native_context_refusal_precedes_identity_and_post() -> None:
    exchange = Exchange()
    result = run_prompt_model_session(
        request(ModelMetadata(max_input_tokens=2000)),
        exchange,
        action_state=PromptModelActionState(),
    )
    assert (
        result.answer is None and result.outcome.outcome_id is PromptModelOutcomeId.CONTEXT_EXCEEDED
    )
    assert not exchange.payloads


def test_shared_deadline_is_120_seconds_and_expiry_never_sends() -> None:
    assert ACTION_DEADLINE_SECONDS == 120.0
    now = [0.0]
    action = state()
    action.clock = lambda: now[0]
    action.started = 0.0
    now[0] = 120.0
    exchange = Exchange()
    result = run_prompt_model_session(request(ModelMetadata()), exchange, action_state=action)
    assert result.outcome.outcome_id is PromptModelOutcomeId.TIMEOUT and not exchange.payloads


def test_native_options_omit_explicitly_unsupported_and_mandatory_controls() -> None:
    exchange = Exchange()
    result = run_prompt_model_session(
        request(ModelMetadata(structured_output=False, reasoning_mandatory=True)),
        exchange,
        action_state=state(),
    )
    assert result.answer is not None
    assert "response_format" not in exchange.payloads[0]
    assert "reasoning_effort" not in exchange.payloads[0]
    assert exchange.payloads[0]["max_completion_tokens"] == 8192


def test_fresh_identity_metadata_restricts_wire_before_chat() -> None:
    class FreshExchange(Exchange):
        def request(
            self,
            method: str,
            path: str,
            payload: Mapping[str, object] | None = None,
            *,
            timeout_seconds: float | None = None,
        ) -> Mapping[str, object]:
            if method == "GET":
                return {"data": [{"id": MODEL}]}
            return super().request(method, path, payload, timeout_seconds=timeout_seconds)

    exchange = FreshExchange()
    # Fresh unknown facts replace old advertised support instead of inheriting authority.
    req = request(ModelMetadata(structured_output=False))
    assert run_prompt_model_session(req, exchange).answer is not None
    assert "response_format" not in exchange.payloads[0]


def test_four_transmissions_remain_one_ceiling_across_repair() -> None:
    action = state()
    exchange = Exchange()
    req = request(ModelMetadata())
    for _ in range(4):
        assert run_prompt_model_session(req, exchange, action_state=action).answer is not None
    result = run_prompt_model_session(req, exchange, action_state=action)
    assert result.outcome.outcome_id is PromptModelOutcomeId.REQUEST_TOO_LARGE
    assert len(exchange.payloads) == 4


@pytest.mark.parametrize("bad_shape", [False, True])
def test_failed_reload_clears_previous_choice_without_provider_prose(bad_shape: bool) -> None:
    subject = connection_state()
    connect(subject)
    choose(subject)
    subject.recheck(listed)
    assert subject.project().readiness is ProviderReadiness.READY

    def broken(_profile: object, _credential: object) -> object:
        if bad_shape:
            return {"raw": "private provider detail"}
        raise RuntimeError("private provider detail")

    result = subject.recheck(broken)
    assert result.projection.readiness is not ProviderReadiness.READY
    assert subject.model_choice is None and not result.projection.candidates
    assert result.projection.diagnostic is not None
    assert "private provider detail" not in str(result.projection.to_wire())
