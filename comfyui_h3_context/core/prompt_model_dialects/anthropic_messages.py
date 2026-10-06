"""Anthropic system authority, structured output and final text blocks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from ..prompt_model_provider import PromptModelOutcomeId
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
    if request.image_payloads:
        fail("session_media_unsupported")
    # IMPORTANT: only one leading system turn is valid. Merging later system turns would change
    # conversation authority when translating to Anthropic's top-level system field.
    if [i for i, item in enumerate(request.messages) if item.role.value == "system"] != [0]:
        fail("session_system_message")
    chat = []
    for item in request.messages[1:]:
        if item.role.value not in {"user", "assistant"}:
            fail("session_message_role")
        chat.append({"role": item.role.value, "content": item.text})
    if not chat:
        fail("session_messages")
    payload: dict[str, object] = {
        "model": request.model_id,
        "system": request.messages[0].text,
        "messages": chat,
        "stream": False,
        "max_tokens": remote_output_tokens_from_plan(
            policy_for_profile(request.profile), request.plan.reserved_output_tokens
        ),
    }
    if options.structured_output:
        payload["output_config"] = {"format": {"type": "json_schema", "schema": draft_schema()}}
    return payload


def read(response: object, model_id: object = None) -> DialectAnswer:
    if not isinstance(response, Mapping) or response.get("type") != "message":
        fail("response_shape")
    if response.get("role") != "assistant":
        fail("response_role")
    observed = response_model(response.get("model"), model_id, snapshot=True)
    reason = response.get("stop_reason")
    if reason == "refusal":
        raise DialectResponseError(PromptModelOutcomeId.MODERATED, "response_refused")
    if not isinstance(reason, str) or reason not in {"end_turn", "max_tokens"}:
        fail("response_incomplete")
    content = response.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        fail("response_shape")
    text_blocks = []
    for block in content:
        if not isinstance(block, Mapping):
            fail("response_shape")
        kind = block.get("type")
        if isinstance(kind, str) and kind in {"thinking", "redacted_thinking"}:
            continue
        if block.get("type") != "text" or set(block) != {"type", "text"}:
            fail("response_unrequested_channel")
        text_blocks.append(block.get("text"))
    if len(text_blocks) > 1:
        fail("response_shape")
    text = final_text(text_blocks[0] if text_blocks else "", reason, truncated_reason="max_tokens")
    return DialectAnswer(text, observed, str(reason))
