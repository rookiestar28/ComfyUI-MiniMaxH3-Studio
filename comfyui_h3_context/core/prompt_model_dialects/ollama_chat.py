"""Ollama non-streaming chat requests and final content only."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .common import DialectAnswer, RequestOptions, draft_schema, fail, final_text, response_model

if TYPE_CHECKING:
    from ..prompt_model_session import PromptModelSessionRequest


def build(request: PromptModelSessionRequest, options: RequestOptions) -> dict[str, object]:
    if request.image_payloads:
        fail("session_media_unsupported")
    payload: dict[str, object] = {
        "model": request.model_id,
        "messages": [{"role": item.role.value, "content": item.text} for item in request.messages],
        "stream": False,
        "keep_alive": 0,
        "options": {
            "num_ctx": request.plan.context_tokens,
            "num_predict": request.plan.requested_output_tokens
            if request.model is not None
            else request.plan.reserved_output_tokens,
        },
    }
    if options.structured_output:
        payload["format"] = draft_schema()
    if options.ollama_think_off:
        payload["think"] = False
    return payload


def read(response: object, model_id: object = None) -> DialectAnswer:
    if not isinstance(response, Mapping):
        fail("response_shape")
    if response.get("done") is not True:
        fail("response_incomplete")
    observed = response_model(response.get("model"), model_id)
    message = response.get("message")
    if not isinstance(message, Mapping):
        fail("response_shape")
    if message.get("tool_calls"):
        fail("response_tool_call")
    if message.get("images"):
        fail("response_media")
    reason = response.get("done_reason", "stop")
    if not isinstance(reason, str) or reason not in {"stop", "length"}:
        fail("response_incomplete")
    text = final_text(message.get("content"), reason, truncated_reason="length")
    return DialectAnswer(text, observed, str(reason))
