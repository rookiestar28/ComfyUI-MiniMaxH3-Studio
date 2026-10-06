"""Chosen-model request options and measured budgets, independent of transport or prices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .prompt_model_budget import (
    OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS,
    OUTPUT_SAFETY_MARGIN_PERCENT,
    ContextProfileLadder,
    ContextProfileStep,
    PromptModelBudgetDecision,
    PromptModelWorkload,
    plan_prompt_model_request,
)
from .prompt_model_dialects.common import RequestOptions
from .prompt_model_provider import ModelMetadata, PromptModelDialect, PromptModelFamily

if TYPE_CHECKING:
    from .prompt_model_session import PromptModelSessionRequest


def options_for_metadata(
    request: PromptModelSessionRequest, options: RequestOptions
) -> RequestOptions:
    facts = None if request.model is None else request.model.metadata
    if facts is None:
        return options
    think_off = options.ollama_think_off
    if request.family is PromptModelFamily.OLLAMA:
        think_off = think_off and "thinking" in facts.capabilities
        if facts.reasoning_control_supported is False or facts.reasoning_mandatory is True:
            think_off = False
    return RequestOptions(
        structured_output=options.structured_output and facts.structured_output is not False,
        reasoning_control=options.reasoning_control
        and facts.reasoning_control_supported is not False
        and facts.reasoning_mandatory is not True,
        ollama_think_off=think_off,
    )


def _needs_headroom(request: PromptModelSessionRequest, options: RequestOptions) -> bool:
    facts = None if request.model is None else request.model.metadata
    if facts is not None and facts.reasoning_mandatory is False:
        return False
    if facts is not None and facts.reasoning_mandatory is True:
        return True
    if request.profile.wire_dialect is PromptModelDialect.ANTHROPIC_MESSAGES:
        return True
    return not (
        options.ollama_think_off
        if request.family is PromptModelFamily.OLLAMA
        else options.reasoning_control
    )


def chosen_model_budget(
    request: PromptModelSessionRequest, options: RequestOptions
) -> PromptModelBudgetDecision:
    facts = None if request.model is None else request.model.metadata
    facts = ModelMetadata() if facts is None else facts
    capabilities = request.capabilities
    context = min(
        capabilities.max_context_tokens,
        facts.max_input_tokens or capabilities.max_context_tokens,
        facts.context_length or capabilities.max_context_tokens,
        request.plan.context_tokens,
    )
    requested = request.plan.requested_output_tokens
    if request.base_output_tokens is not None:
        requested = request.base_output_tokens + (6144 if _needs_headroom(request, options) else 0)
    # IMPORTANT: a provider wire output limit and the planner's reserve are different facts.
    # Clamp both before admission; sending the reserve would exceed a discovered wire limit.
    reserve_ceiling = capabilities.max_output_tokens
    ratio = 100 + OUTPUT_SAFETY_MARGIN_PERCENT
    reserve_limited = min(
        reserve_ceiling - OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS,
        (reserve_ceiling * 100 + ratio - 1) // ratio,
    )
    while (
        reserve_limited > 0
        and reserve_limited
        + max(
            OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS,
            reserve_limited * OUTPUT_SAFETY_MARGIN_PERCENT // 100,
        )
        > reserve_ceiling
    ):
        reserve_limited -= 1
    requested = min(requested, facts.max_output_tokens or requested, reserve_limited)
    text = "".join(item.text for item in request.messages)
    return plan_prompt_model_request(
        capabilities=capabilities,
        ladder=ContextProfileLadder(
            steps=(ContextProfileStep(context_tokens=context, memory_bytes=1),)
        ),
        workload=PromptModelWorkload(
            characters=len(text),
            wide_characters=sum(ord(char) > 127 for char in text),
            messages=len(request.messages),
            visual_inputs=len(request.image_payloads),
            request_bytes=len(text.encode("utf-8"))
            + sum(len(value.encode("utf-8")) for value in request.image_payloads),
            requested_output_tokens=max(1, requested),
        ),
        requested_step=0,
        available_memory_bytes=1,
    )
