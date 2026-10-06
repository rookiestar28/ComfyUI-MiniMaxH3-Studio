"""OpenAI-compatible chat, privacy-preserving safe requests and final content."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from ..prompt_model_provider import PromptModelFamily, PromptModelOutcomeId
from ..remote_provider_policy import policy_for_profile, remote_output_tokens_from_plan
from .common import (
    DialectAnswer,
    DialectResponseError,
    RequestOptions,
    draft_schema,
    fail,
    final_text,
    response_model,
)

if TYPE_CHECKING:
    from ..prompt_model_session import PromptModelSessionRequest


def build(request: PromptModelSessionRequest, options: RequestOptions) -> dict[str, object]:
    chat: list[dict[str, object]] = []
    for index, item in enumerate(request.messages):
        if index == len(request.messages) - 1 and request.image_payloads:
            parts: list[dict[str, object]] = [{"type": "text", "text": item.text}]
            parts.extend(
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{value}"}}
                for value in request.image_payloads
            )
            chat.append({"role": item.role.value, "content": parts})
        else:
            chat.append({"role": item.role.value, "content": item.text})
    payload: dict[str, object] = {
        "model": request.model_id,
        "messages": chat,
        "stream": False,
    }
    if request.family is PromptModelFamily.LOOPBACK_SERVER:
        payload["max_tokens"] = request.plan.reserved_output_tokens
        return payload
    if request.image_payloads:
        fail("session_media_unsupported")
    policy = policy_for_profile(request.profile)
    payload["max_completion_tokens"] = remote_output_tokens_from_plan(
        policy, request.plan.reserved_output_tokens
    )
    if policy.store is not None:
        payload["store"] = policy.store
    if options.reasoning_control:
        payload["reasoning_effort"] = policy.reasoning_effort
    if options.structured_output:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "h3_prompt_model_draft",
                "strict": True,
                "schema": draft_schema(),
            },
        }
    return payload


def read(response: object, model_id: object = None, *, remote: bool = True) -> DialectAnswer:
    if not isinstance(response, Mapping):
        fail("response_shape")
    observed = response_model(response.get("model"), model_id, snapshot=True) if remote else ""
    choices = response.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        fail("response_shape")
    if remote and len(choices) != 1:
        fail("response_choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        fail("response_shape")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        fail("response_shape")
    if remote and (
        message.get("role") != "assistant"
        or type(choice.get("index")) is not int
        or choice.get("index") != 0
    ):
        fail("response_role")
    reason = choice.get("finish_reason", "stop")
    if not isinstance(reason, str) or reason not in {"stop", "length", "content_filter"}:
        fail("response_incomplete")
    if message.get("refusal") or reason == "content_filter":
        raise DialectResponseError(PromptModelOutcomeId.MODERATED, "response_refused")
    if remote and reason not in {"stop", "length"}:
        fail("response_incomplete")
    for forbidden in ("tool_calls", "function_call", "audio"):
        if message.get(forbidden) not in (None, "", [], {}):
            fail("response_unrequested_channel")
    text = final_text(message.get("content"), reason, truncated_reason="length")
    return DialectAnswer(text, observed, str(reason))
