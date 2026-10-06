"""M22-02 prompt-model runtime budget and admission planner.

Nothing is sent to a prompt model until this module has said it fits. The planner resolves a context
step from a declared ladder, estimates the input cost, reserves the requested output plus a margin,
checks the memory the resolved step needs against what the caller declares is available, and then
either returns an admitted plan or refuses with a typed outcome that names the estimate, the ceiling
and the next action.

Two properties are structural rather than promised. The planner is given *measurements* -- character
counts, message and visual-input counts, byte size, requested output tokens -- and never the prompt
itself, so no budget artifact can carry prompt content because no parameter exists to carry it in.
And `PromptModelBudgetPlan` can only be produced here, so a transport that requires one cannot be
handed an unplanned request, in the same way `AdmittedDestination` cannot be handed an unadmitted
endpoint.

Nothing here shortens, truncates or downgrades a request. A request that does not fit is refused; a
request that fits only at a larger context step escalates upward and reports both steps. Estimation
is deterministic and dependency-free: a conservative bound is worth more than an accurate one that
would need a tokenizer at import time, and the constants that produced the number travel with the
plan so a reviewer can check the arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NoReturn

from .contracts import ValidationSeverity
from .prompt_model_provider import (
    PromptModelCapabilities,
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcome,
    PromptModelOutcomeId,
    PromptModelRemediation,
    build_prompt_model_outcome,
)

PROMPT_MODEL_BUDGET_SCHEMA = "h3-context-prompt-model-budget/1"

MAX_LADDER_STEPS = 8
MAX_MEMORY_BYTES = 1 << 48
MAX_CHARACTERS = 8_000_000
MAX_MESSAGES = 4_096
MAX_VISUAL_INPUTS = 64

ASCII_CHARACTERS_PER_TOKEN = 3
WIDE_CHARACTER_TOKENS = 2
MESSAGE_OVERHEAD_TOKENS = 8
TEMPLATE_OVERHEAD_TOKENS = 64
VISUAL_INPUT_TOKENS = 768
OUTPUT_SAFETY_MARGIN_PERCENT = 20
OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS = 64

# Published with every plan so the number can be checked rather than trusted. A wide character is
# charged two tokens and an ASCII run three characters to the token, both deliberately pessimistic:
# over-reserving costs headroom, under-reserving costs a request that fails at the provider.
ESTIMATION_BASIS: Mapping[str, int] = {
    "ascii_characters_per_token": ASCII_CHARACTERS_PER_TOKEN,
    "wide_character_tokens": WIDE_CHARACTER_TOKENS,
    "message_overhead_tokens": MESSAGE_OVERHEAD_TOKENS,
    "template_overhead_tokens": TEMPLATE_OVERHEAD_TOKENS,
    "visual_input_tokens": VISUAL_INPUT_TOKENS,
    "output_safety_margin_percent": OUTPUT_SAFETY_MARGIN_PERCENT,
    "output_safety_margin_floor_tokens": OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS,
}

_PLAN_TOKEN = object()


def _fail(code: str) -> NoReturn:
    raise PromptModelContractError(code)


def _count(value: object, maximum: int, code: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


@dataclass(frozen=True, slots=True)
class ContextProfileStep:
    """One rung of a declared context ladder and the memory that rung needs."""

    context_tokens: int
    memory_bytes: int

    def __post_init__(self) -> None:
        _count(self.context_tokens, 1 << 24, "step_context", minimum=1)
        _count(self.memory_bytes, MAX_MEMORY_BYTES, "step_memory", minimum=1)


@dataclass(frozen=True, slots=True)
class ContextProfileLadder:
    """The context steps a model declares, ascending. Escalation moves up this and nowhere else."""

    steps: tuple[ContextProfileStep, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.steps, tuple) or not self.steps:
            _fail("ladder_empty")
        if len(self.steps) > MAX_LADDER_STEPS:
            _fail("ladder_length")
        for step in self.steps:
            if not isinstance(step, ContextProfileStep):
                _fail("ladder_step")
        for lower, upper in zip(self.steps, self.steps[1:], strict=False):
            if upper.context_tokens <= lower.context_tokens:
                _fail("ladder_order")
            if upper.memory_bytes <= lower.memory_bytes:
                # A larger context that needs no more memory is a declaration error, and admitting
                # it would let the memory check pass for a step it was never measured against.
                _fail("ladder_order")

    @property
    def ceiling_tokens(self) -> int:
        return self.steps[-1].context_tokens


@dataclass(frozen=True, slots=True)
class PromptModelWorkload:
    """Measurements of a request. There is no field a prompt could be passed through."""

    characters: int
    wide_characters: int
    messages: int
    visual_inputs: int
    request_bytes: int
    requested_output_tokens: int

    def __post_init__(self) -> None:
        _count(self.characters, MAX_CHARACTERS, "workload_characters")
        _count(self.wide_characters, MAX_CHARACTERS, "workload_wide_characters")
        if self.wide_characters > self.characters:
            _fail("workload_wide_characters")
        _count(self.messages, MAX_MESSAGES, "workload_messages")
        _count(self.visual_inputs, MAX_VISUAL_INPUTS, "workload_visual_inputs")
        _count(self.request_bytes, 1 << 32, "workload_request_bytes")
        _count(self.requested_output_tokens, 1 << 24, "workload_output", minimum=1)

    @property
    def estimated_input_tokens(self) -> int:
        narrow = self.characters - self.wide_characters
        return (
            -(-narrow // ASCII_CHARACTERS_PER_TOKEN)
            + self.wide_characters * WIDE_CHARACTER_TOKENS
            + self.messages * MESSAGE_OVERHEAD_TOKENS
            + self.visual_inputs * VISUAL_INPUT_TOKENS
            + TEMPLATE_OVERHEAD_TOKENS
        )

    @property
    def reserved_output_tokens(self) -> int:
        margin = self.requested_output_tokens * OUTPUT_SAFETY_MARGIN_PERCENT // 100
        return self.requested_output_tokens + max(OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS, margin)


@dataclass(frozen=True, slots=True)
class PromptModelBudgetPlan:
    """An admitted plan. Only `plan_prompt_model_request` can produce one."""

    family: PromptModelFamily
    requested_step: int
    resolved_step: int
    context_tokens: int
    estimated_input_tokens: int
    reserved_output_tokens: int
    memory_required_bytes: int
    memory_available_bytes: int
    admission: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.admission is not _PLAN_TOKEN:
            _fail("plan_not_admitted")

    @property
    def required_tokens(self) -> int:
        return self.estimated_input_tokens + self.reserved_output_tokens

    @property
    def requested_output_tokens(self) -> int:
        reserved = self.reserved_output_tokens
        requested = min(
            reserved - OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS,
            (reserved * 100 + 100 + OUTPUT_SAFETY_MARGIN_PERCENT - 1)
            // (100 + OUTPUT_SAFETY_MARGIN_PERCENT),
        )
        if (
            requested < 1
            or requested
            + max(
                OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS,
                requested * OUTPUT_SAFETY_MARGIN_PERCENT // 100,
            )
            != reserved
        ):
            _fail("plan_output_reserve")
        return requested

    @property
    def headroom_tokens(self) -> int:
        return self.context_tokens - self.required_tokens

    @property
    def basis(self) -> Mapping[str, int]:
        return ESTIMATION_BASIS

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PROMPT_MODEL_BUDGET_SCHEMA,
            "family": self.family.value,
            "requested_step": self.requested_step,
            "resolved_step": self.resolved_step,
            "context_tokens": self.context_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "required_tokens": self.required_tokens,
            "headroom_tokens": self.headroom_tokens,
            "memory_required_bytes": self.memory_required_bytes,
            "memory_available_bytes": self.memory_available_bytes,
            "basis": dict(ESTIMATION_BASIS),
        }


@dataclass(frozen=True, slots=True)
class PromptModelBudgetDecision:
    """Every call yields an outcome; only an admitted one also yields a plan."""

    outcome: PromptModelOutcome
    plan: PromptModelBudgetPlan | None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, PromptModelOutcome):
            _fail("decision_outcome")
        admitted = self.outcome.outcome_id is PromptModelOutcomeId.OK
        if admitted is not isinstance(self.plan, PromptModelBudgetPlan):
            _fail("decision_plan")

    @property
    def admitted(self) -> bool:
        return self.plan is not None

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PROMPT_MODEL_BUDGET_SCHEMA,
            "outcome": self.outcome.to_wire(),
            "plan": None if self.plan is None else self.plan.to_wire(),
        }


def _refusal(
    outcome_id: PromptModelOutcomeId,
    remediation: PromptModelRemediation,
    parameters: tuple[tuple[str, str | int | bool], ...],
) -> PromptModelBudgetDecision:
    return PromptModelBudgetDecision(
        outcome=build_prompt_model_outcome(
            outcome_id,
            severity=ValidationSeverity.ERROR,
            remediation=remediation,
            parameters=parameters,
        ),
        plan=None,
    )


def plan_prompt_model_request(
    *,
    capabilities: object,
    ladder: object,
    workload: object,
    requested_step: object,
    available_memory_bytes: object,
    requested_context_override: object = None,
) -> PromptModelBudgetDecision:
    """Admit a request or refuse it. Never shortens, never downgrades, never guesses a default."""

    if not isinstance(capabilities, PromptModelCapabilities):
        _fail("plan_capabilities")
    if not isinstance(ladder, ContextProfileLadder):
        _fail("plan_ladder")
    if type(requested_step) is not int or not 0 <= requested_step < len(ladder.steps):
        _fail("plan_requested_step")
    memory_available = _count(available_memory_bytes, MAX_MEMORY_BYTES, "plan_memory")

    if not isinstance(workload, PromptModelWorkload):
        # An estimate that cannot be produced blocks admission; it never falls back to a default.
        return _refusal(
            PromptModelOutcomeId.ESTIMATE_UNAVAILABLE,
            PromptModelRemediation.NONE,
            (("reason", "workload_measurements_absent"),),
        )

    if requested_context_override is not None:
        if type(requested_context_override) is not int:
            _fail("plan_context_override")
        if capabilities.provider_managed_context:
            # The provider owns the context window here, so honouring the override is impossible and
            # accepting it silently would report a window the request will not actually get.
            return _refusal(
                PromptModelOutcomeId.PROVIDER_MANAGED_SETTING,
                PromptModelRemediation.CORRECT_ENDPOINT,
                (
                    ("setting", "context_tokens"),
                    ("requested_tokens", requested_context_override),
                ),
            )

    if ladder.ceiling_tokens > capabilities.max_context_tokens:
        return _refusal(
            PromptModelOutcomeId.CAPABILITY_MISMATCH,
            PromptModelRemediation.SELECT_MODEL,
            (
                ("ladder_ceiling_tokens", ladder.ceiling_tokens),
                ("declared_ceiling_tokens", capabilities.max_context_tokens),
            ),
        )

    if workload.request_bytes > capabilities.max_request_bytes:
        return _refusal(
            PromptModelOutcomeId.REQUEST_TOO_LARGE,
            PromptModelRemediation.REDUCE_REQUEST,
            (
                ("request_bytes", workload.request_bytes),
                ("limit_bytes", capabilities.max_request_bytes),
            ),
        )

    if workload.requested_output_tokens > capabilities.max_output_tokens:
        return _refusal(
            PromptModelOutcomeId.CAPABILITY_MISMATCH,
            PromptModelRemediation.REDUCE_REQUEST,
            (
                ("requested_output_tokens", workload.requested_output_tokens),
                ("limit_output_tokens", capabilities.max_output_tokens),
            ),
        )

    estimated_input = workload.estimated_input_tokens
    reserved_output = workload.reserved_output_tokens
    required = estimated_input + reserved_output

    resolved = None
    for index in range(requested_step, len(ladder.steps)):
        if ladder.steps[index].context_tokens >= required:
            resolved = index
            break
    if resolved is None:
        return _refusal(
            PromptModelOutcomeId.CONTEXT_EXCEEDED,
            PromptModelRemediation.REDUCE_REQUEST,
            (
                ("estimated_input_tokens", estimated_input),
                ("reserved_output_tokens", reserved_output),
                ("required_tokens", required),
                ("ceiling_tokens", ladder.ceiling_tokens),
                ("requested_step", requested_step),
            ),
        )

    step = ladder.steps[resolved]
    if step.memory_bytes > memory_available:
        return _refusal(
            PromptModelOutcomeId.INSUFFICIENT_MEMORY,
            PromptModelRemediation.SELECT_MODEL,
            (
                ("required_bytes", step.memory_bytes),
                ("available_bytes", memory_available),
                ("resolved_step", resolved),
            ),
        )

    return PromptModelBudgetDecision(
        outcome=build_prompt_model_outcome(
            PromptModelOutcomeId.OK,
            severity=ValidationSeverity.INFO,
            remediation=PromptModelRemediation.NONE,
            parameters=(
                ("resolved_step", resolved),
                ("required_tokens", required),
                ("context_tokens", step.context_tokens),
            ),
        ),
        plan=PromptModelBudgetPlan(
            family=capabilities.family,
            requested_step=requested_step,
            resolved_step=resolved,
            context_tokens=step.context_tokens,
            estimated_input_tokens=estimated_input,
            reserved_output_tokens=reserved_output,
            memory_required_bytes=step.memory_bytes,
            memory_available_bytes=memory_available,
            admission=_PLAN_TOKEN,
        ),
    )


def admit_reasoning_channel(*, opened: object, closed: object) -> PromptModelOutcome:
    """A reasoning channel that opened and never closed is a failure, not a shorter answer."""

    open_count = _count(opened, MAX_MESSAGES, "reasoning_opened")
    close_count = _count(closed, MAX_MESSAGES, "reasoning_closed")
    if close_count > open_count:
        _fail("reasoning_unbalanced")
    if close_count < open_count:
        return build_prompt_model_outcome(
            PromptModelOutcomeId.TRUNCATED_REASONING,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.RETRY_LATER,
            parameters=(("opened", open_count), ("closed", close_count)),
        )
    return build_prompt_model_outcome(
        PromptModelOutcomeId.OK,
        severity=ValidationSeverity.INFO,
        remediation=PromptModelRemediation.NONE,
        parameters=(("opened", open_count), ("closed", close_count)),
    )
