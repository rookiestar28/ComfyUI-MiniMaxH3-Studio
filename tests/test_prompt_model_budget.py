"""M22-02 prompt-model runtime budget and admission planner tests."""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import unittest

from comfyui_h3_context.core.prompt_model_budget import (
    ESTIMATION_BASIS,
    MAX_LADDER_STEPS,
    OUTPUT_SAFETY_MARGIN_PERCENT,
    PROMPT_MODEL_BUDGET_SCHEMA,
    ContextProfileLadder,
    ContextProfileStep,
    PromptModelBudgetDecision,
    PromptModelBudgetPlan,
    PromptModelWorkload,
    admit_reasoning_channel,
    plan_prompt_model_request,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PROMPT_MODEL_OUTCOME_IDS,
    PromptModelCapabilities,
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelRemediation,
    build_prompt_model_capabilities,
)

SMALL = ContextProfileStep(context_tokens=8_192, memory_bytes=6 * 1024**3)
MEDIUM = ContextProfileStep(context_tokens=16_384, memory_bytes=9 * 1024**3)
LARGE = ContextProfileStep(context_tokens=24_576, memory_bytes=13 * 1024**3)
LADDER = ContextProfileLadder(steps=(SMALL, MEDIUM, LARGE))
PLENTY = 32 * 1024**3


def capabilities(
    family: PromptModelFamily = PromptModelFamily.OLLAMA, **overrides: object
) -> PromptModelCapabilities:
    managed = family is not PromptModelFamily.IN_PROCESS_GGUF
    values: dict[str, object] = {
        "family": family.value,
        "accepted_media": ["text", "image"],
        "provider_managed_context": managed,
        "provider_managed_kv_cache": managed,
        "max_request_bytes": 262_144,
        "max_context_tokens": 32_768,
        "max_output_tokens": 4_096,
        "streaming": False,
        "local_only": family is not PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        "requires_credential": family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
    }
    values.update(overrides)
    return build_prompt_model_capabilities(values)


def workload(**overrides: object) -> PromptModelWorkload:
    values: dict[str, object] = {
        "characters": 2_400,
        "wide_characters": 0,
        "messages": 4,
        "visual_inputs": 0,
        "request_bytes": 8_192,
        "requested_output_tokens": 1_024,
    }
    values.update(overrides)
    return PromptModelWorkload(**values)  # type: ignore[arg-type]


def plan(**overrides: object) -> PromptModelBudgetDecision:
    values: dict[str, object] = {
        "capabilities": capabilities(),
        "ladder": LADDER,
        "workload": workload(),
        "requested_step": 0,
        "available_memory_bytes": PLENTY,
        "requested_context_override": None,
    }
    values.update(overrides)
    return plan_prompt_model_request(**values)


class LadderTests(unittest.TestCase):
    def test_a_ladder_must_ascend_in_both_dimensions(self) -> None:
        for steps in (
            (MEDIUM, SMALL),
            (SMALL, SMALL),
            (
                ContextProfileStep(context_tokens=8_192, memory_bytes=9 * 1024**3),
                ContextProfileStep(context_tokens=16_384, memory_bytes=6 * 1024**3),
            ),
        ):
            with self.subTest(steps=[step.context_tokens for step in steps]):
                with self.assertRaises(PromptModelContractError) as caught:
                    ContextProfileLadder(steps=steps)
                self.assertEqual(caught.exception.code, "ladder_order")

    def test_a_ladder_is_bounded_and_non_empty(self) -> None:
        with self.assertRaises(PromptModelContractError):
            ContextProfileLadder(steps=())
        too_many = tuple(
            ContextProfileStep(
                context_tokens=1_024 * (index + 1), memory_bytes=1024**3 * (index + 1)
            )
            for index in range(MAX_LADDER_STEPS + 1)
        )
        with self.assertRaises(PromptModelContractError):
            ContextProfileLadder(steps=too_many)

    def test_step_values_are_bounded_positive_integers(self) -> None:
        for field, value in (
            ("context_tokens", 0),
            ("context_tokens", True),
            ("memory_bytes", -1),
            ("memory_bytes", 1 << 60),
        ):
            values: dict[str, object] = {"context_tokens": 8_192, "memory_bytes": 1024**3}
            values[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(PromptModelContractError):
                    ContextProfileStep(**values)  # type: ignore[arg-type]

    def test_a_ladder_may_not_exceed_the_declared_capability_ceiling(self) -> None:
        decision = plan(capabilities=capabilities(max_context_tokens=8_192))
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.CAPABILITY_MISMATCH)
        self.assertIsNone(decision.plan)


class AdmissionTests(unittest.TestCase):
    def test_a_request_that_fits_is_admitted_with_a_readable_basis(self) -> None:
        decision = plan()
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.OK)
        self.assertIsInstance(decision.plan, PromptModelBudgetPlan)
        assert decision.plan is not None
        self.assertEqual(decision.plan.requested_step, 0)
        self.assertEqual(decision.plan.resolved_step, 0)
        self.assertEqual(decision.plan.context_tokens, 8_192)
        self.assertGreater(decision.plan.estimated_input_tokens, 0)
        self.assertEqual(
            decision.plan.reserved_output_tokens,
            1_024 + max(64, 1_024 * OUTPUT_SAFETY_MARGIN_PERCENT // 100),
        )
        self.assertEqual(
            decision.plan.estimated_input_tokens + decision.plan.reserved_output_tokens,
            decision.plan.required_tokens,
        )
        self.assertEqual(
            decision.plan.headroom_tokens,
            decision.plan.context_tokens - decision.plan.required_tokens,
        )
        self.assertEqual(decision.plan.basis, ESTIMATION_BASIS)

    def test_the_estimate_is_deterministic_and_monotonic(self) -> None:
        first = plan().plan
        second = plan().plan
        assert first is not None and second is not None
        self.assertEqual(first.to_wire(), second.to_wire())
        bigger = plan(workload=workload(characters=4_800)).plan
        assert bigger is not None
        self.assertGreater(bigger.estimated_input_tokens, first.estimated_input_tokens)

    def test_wide_characters_visual_inputs_and_messages_each_raise_the_estimate(self) -> None:
        base = plan().plan
        assert base is not None
        for override in (
            {"wide_characters": 1_200},
            {"visual_inputs": 1},
            {"messages": 12},
        ):
            with self.subTest(override=override):
                raised = plan(workload=workload(**override)).plan
                assert raised is not None
                self.assertGreater(raised.estimated_input_tokens, base.estimated_input_tokens)

    def test_a_plan_carries_no_prompt_content_and_no_locator(self) -> None:
        decision = plan()
        assert decision.plan is not None
        wire = json.dumps(decision.plan.to_wire())
        self.assertNotIn("http", wire)
        self.assertEqual(
            set(decision.plan.to_wire()),
            {
                "schema",
                "family",
                "requested_step",
                "resolved_step",
                "context_tokens",
                "estimated_input_tokens",
                "reserved_output_tokens",
                "required_tokens",
                "headroom_tokens",
                "memory_required_bytes",
                "memory_available_bytes",
                "basis",
            },
        )

    def test_the_workload_accepts_measurements_only(self) -> None:
        parameters = set(inspect.signature(PromptModelWorkload).parameters)
        self.assertEqual(
            parameters,
            {
                "characters",
                "wide_characters",
                "messages",
                "visual_inputs",
                "request_bytes",
                "requested_output_tokens",
            },
        )

    def test_a_plan_cannot_be_constructed_outside_the_planner(self) -> None:
        with self.assertRaises(PromptModelContractError):
            PromptModelBudgetPlan(
                family=PromptModelFamily.OLLAMA,
                requested_step=0,
                resolved_step=0,
                context_tokens=8_192,
                estimated_input_tokens=1_000,
                reserved_output_tokens=1_228,
                memory_required_bytes=SMALL.memory_bytes,
                memory_available_bytes=PLENTY,
            )


class EscalationTests(unittest.TestCase):
    def test_escalation_happens_only_upward_and_reports_both_steps(self) -> None:
        decision = plan(workload=workload(characters=30_000))
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.OK)
        assert decision.plan is not None
        self.assertEqual(decision.plan.requested_step, 0)
        self.assertEqual(decision.plan.resolved_step, 1)
        self.assertEqual(decision.plan.context_tokens, 16_384)

    def test_a_higher_requested_step_is_never_silently_lowered(self) -> None:
        decision = plan(requested_step=2)
        assert decision.plan is not None
        self.assertEqual(decision.plan.resolved_step, 2)
        self.assertEqual(decision.plan.context_tokens, 24_576)

    def test_a_request_beyond_the_top_of_the_ladder_fails_closed(self) -> None:
        decision = plan(workload=workload(characters=200_000, request_bytes=200_000))
        self.assertIsNone(decision.plan)
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.CONTEXT_EXCEEDED)
        self.assertIs(decision.outcome.remediation, PromptModelRemediation.REDUCE_REQUEST)
        parameters = dict(decision.outcome.parameters)
        self.assertIn("required_tokens", parameters)
        self.assertIn("ceiling_tokens", parameters)
        self.assertEqual(parameters["ceiling_tokens"], 24_576)
        required = parameters["required_tokens"]
        ceiling = parameters["ceiling_tokens"]
        assert isinstance(required, int) and isinstance(ceiling, int)
        self.assertGreater(required, ceiling)

    def test_an_unknown_requested_step_is_refused(self) -> None:
        for step in (-1, 3, True, "0"):
            with self.subTest(step=step):
                with self.assertRaises(PromptModelContractError):
                    plan(requested_step=step)


class RefusalTests(unittest.TestCase):
    def test_every_refusal_is_typed_distinct_and_carries_a_remediation(self) -> None:
        cases = (
            (
                PromptModelOutcomeId.CONTEXT_EXCEEDED,
                {"workload": workload(characters=200_000, request_bytes=200_000)},
            ),
            (
                PromptModelOutcomeId.INSUFFICIENT_MEMORY,
                {"available_memory_bytes": 4 * 1024**3},
            ),
            (
                PromptModelOutcomeId.PROVIDER_MANAGED_SETTING,
                {"requested_context_override": 16_384},
            ),
            (
                PromptModelOutcomeId.REQUEST_TOO_LARGE,
                {"workload": workload(request_bytes=1_000_000)},
            ),
            (
                PromptModelOutcomeId.CAPABILITY_MISMATCH,
                {"workload": workload(requested_output_tokens=8_192)},
            ),
        )
        seen: set[PromptModelOutcomeId] = set()
        for expected, overrides in cases:
            with self.subTest(outcome=expected):
                decision = plan(**overrides)
                self.assertIsNone(decision.plan)
                self.assertIs(decision.outcome.outcome_id, expected)
                self.assertIsNot(decision.outcome.remediation, PromptModelRemediation.NONE)
                self.assertTrue(decision.outcome.parameters)
                self.assertNotIn(expected, seen)
                seen.add(expected)

    def test_insufficient_memory_names_both_figures(self) -> None:
        decision = plan(available_memory_bytes=4 * 1024**3)
        parameters = dict(decision.outcome.parameters)
        self.assertEqual(parameters["required_bytes"], SMALL.memory_bytes)
        self.assertEqual(parameters["available_bytes"], 4 * 1024**3)
        self.assertIs(decision.outcome.remediation, PromptModelRemediation.SELECT_MODEL)

    def test_memory_is_judged_against_the_step_that_actually_resolved(self) -> None:
        decision = plan(
            workload=workload(characters=30_000),
            available_memory_bytes=MEDIUM.memory_bytes - 1,
        )
        self.assertIsNone(decision.plan)
        parameters = dict(decision.outcome.parameters)
        self.assertEqual(parameters["required_bytes"], MEDIUM.memory_bytes)

    def test_a_provider_managed_setting_is_refused_not_ignored(self) -> None:
        decision = plan(requested_context_override=16_384)
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.PROVIDER_MANAGED_SETTING)
        self.assertIs(decision.outcome.remediation, PromptModelRemediation.CORRECT_ENDPOINT)
        admitted = plan(
            capabilities=capabilities(PromptModelFamily.IN_PROCESS_GGUF),
            requested_context_override=8_192,
        )
        self.assertIs(admitted.outcome.outcome_id, PromptModelOutcomeId.OK)

    def test_an_unavailable_estimate_blocks_admission_rather_than_defaulting(self) -> None:
        decision = plan(workload=None)
        self.assertIsNone(decision.plan)
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.ESTIMATE_UNAVAILABLE)
        self.assertIs(decision.outcome.remediation, PromptModelRemediation.NONE)

    def test_nothing_is_shortened_truncated_or_downgraded(self) -> None:
        for requested in (1, 512, 4_096):
            with self.subTest(requested=requested):
                decision = plan(workload=workload(requested_output_tokens=requested))
                assert decision.plan is not None
                self.assertGreaterEqual(decision.plan.reserved_output_tokens, requested)
                self.assertGreaterEqual(decision.plan.resolved_step, decision.plan.requested_step)

    def test_the_planner_contains_no_slicing_or_truncation_operation(self) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(plan_prompt_model_request)))
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, ast.Slice)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"min", "truncate"})


class ReasoningChannelTests(unittest.TestCase):
    def test_a_closed_channel_is_admitted(self) -> None:
        outcome = admit_reasoning_channel(opened=1, closed=1)
        self.assertIs(outcome.outcome_id, PromptModelOutcomeId.OK)
        self.assertIs(
            admit_reasoning_channel(opened=0, closed=0).outcome_id, PromptModelOutcomeId.OK
        )

    def test_an_unclosed_channel_is_a_typed_failure_not_a_result(self) -> None:
        outcome = admit_reasoning_channel(opened=1, closed=0)
        self.assertIs(outcome.outcome_id, PromptModelOutcomeId.TRUNCATED_REASONING)
        self.assertIs(outcome.remediation, PromptModelRemediation.RETRY_LATER)
        self.assertEqual(dict(outcome.parameters), {"opened": 1, "closed": 0})

    def test_an_impossible_channel_count_is_refused(self) -> None:
        for opened, closed in ((0, 1), (-1, 0), (1, 2)):
            with self.subTest(opened=opened, closed=closed):
                with self.assertRaises(PromptModelContractError):
                    admit_reasoning_channel(opened=opened, closed=closed)


class RegistryTests(unittest.TestCase):
    def test_the_new_outcomes_joined_the_single_closed_registry(self) -> None:
        for value in (
            "prompt_model.insufficient_memory",
            "prompt_model.provider_managed_setting",
            "prompt_model.truncated_reasoning",
            "prompt_model.estimate_unavailable",
        ):
            self.assertIn(value, PROMPT_MODEL_OUTCOME_IDS)
        self.assertEqual(
            PROMPT_MODEL_OUTCOME_IDS, frozenset(item.value for item in PromptModelOutcomeId)
        )

    def test_the_schema_is_versioned_and_the_basis_is_published(self) -> None:
        self.assertEqual(PROMPT_MODEL_BUDGET_SCHEMA, "h3-context-prompt-model-budget/1")
        self.assertEqual(
            set(ESTIMATION_BASIS),
            {
                "ascii_characters_per_token",
                "wide_character_tokens",
                "message_overhead_tokens",
                "template_overhead_tokens",
                "visual_input_tokens",
                "output_safety_margin_percent",
                "output_safety_margin_floor_tokens",
            },
        )
        for value in ESTIMATION_BASIS.values():
            self.assertIsInstance(value, int)
            self.assertGreater(value, 0)


if __name__ == "__main__":
    unittest.main()
