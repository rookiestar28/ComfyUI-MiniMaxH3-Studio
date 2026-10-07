"""M22-12 execution admission, cancellation and sanitized receipt contracts."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from dataclasses import replace

from historical_prompt_model_fixtures import load_historical_catalog as load_prompt_model_catalog

from comfyui_h3_context.core.assisted_draft import (
    DraftCandidate,
    DraftGuarantee,
    DraftModelExecutionError,
    RepairShape,
)
from comfyui_h3_context.core.assisted_draft_orchestration import (
    AssistedDraftExecutionResult,
    AssistedDraftModelBinding,
    AssistedDraftUsage,
    run_assisted_draft_orchestration,
)
from comfyui_h3_context.core.context_reporting import ContextPlan, PromptDocument
from comfyui_h3_context.core.contracts import TaskMode, ValidationSeverity
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelOutcomeId,
    PromptModelQualificationState,
    PromptModelRemediation,
    build_prompt_model_outcome,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
)


def _authority() -> tuple[ContextPlan, PromptDocument]:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "A paper kite crosses a quiet blue sky.",
        duration_seconds=6.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return plan, document


PLAN, TEMPLATE = _authority()
# M22-13: the shipped local row now arrives qualified, so the catalog-only fixture is derived by
# removing the qualification rather than by assuming the shipped default is unqualified. The state
# and its evidence come off together, because a profile cannot hold one without the other.
QUALIFIED = load_prompt_model_catalog().profiles[0]
CATALOG_ONLY = replace(
    QUALIFIED,
    qualification_state=PromptModelQualificationState.CATALOG_ONLY,
    qualification_evidence=None,
)


class _ScriptedModel:
    def __init__(
        self,
        *candidates: DraftCandidate,
        on_call: Callable[[int], None] | None = None,
    ) -> None:
        self._candidates = list(candidates)
        self.calls = 0
        self._on_call = on_call

    def __call__(self, instruction: str, *, shape: RepairShape | None) -> DraftCandidate:
        del instruction, shape
        self.calls += 1
        if self._on_call is not None:
            self._on_call(self.calls)
        return self._candidates.pop(0)


def _run(
    profile: PromptModelProfile,
    factory: Callable[[], AssistedDraftModelBinding],
    *,
    cancellation: Callable[[], bool] = lambda: False,
) -> AssistedDraftExecutionResult:
    return run_assisted_draft_orchestration(
        profile=profile,
        plan=PLAN,
        template=TEMPLATE,
        guarantee=DraftGuarantee(allowed_labels=(), required_labels=()),
        instruction="Improve clarity without changing constraints.",
        evidence_fingerprint="sha256:" + "a" * 64,
        provider_revision=7,
        session_generation="generation_0123456789abcdef",
        authority_epoch=11,
        model_factory=factory,
        cancellation=cancellation,
    )


class AssistedDraftAdmissionTests(unittest.TestCase):
    def test_catalog_only_refuses_before_the_exchange_factory_is_constructed(self) -> None:
        factory_calls = 0

        def factory() -> AssistedDraftModelBinding:
            nonlocal factory_calls
            factory_calls += 1
            raise AssertionError("catalog-only admission constructed an exchange")

        result = _run(CATALOG_ONLY, factory)
        self.assertEqual(factory_calls, 0)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.PROFILE_NOT_QUALIFIED)
        self.assertIsNone(result.draft)
        self.assertIsNone(result.receipt)

    def test_a_qualified_profile_produces_a_content_free_assisted_receipt(self) -> None:
        model = _ScriptedModel(DraftCandidate(TEMPLATE.text))

        def factory() -> AssistedDraftModelBinding:
            return AssistedDraftModelBinding(
                model=model,
                usage=lambda: AssistedDraftUsage(
                    requests=1,
                    request_bytes=321,
                    response_bytes=654,
                    prompt_tokens=12,
                    completion_tokens=34,
                    duration_ms=56,
                ),
            )

        result = _run(QUALIFIED, factory)
        self.assertTrue(result.usable)
        self.assertEqual(model.calls, 1)
        self.assertIsNotNone(result.receipt)
        wire = result.to_wire()
        receipt = wire["receipt"]
        assert isinstance(receipt, dict)
        encoded = json.dumps(wire, sort_keys=True)
        self.assertEqual(
            set(receipt),
            {
                "schema",
                "action_id",
                "profile_id",
                "provider_family",
                "model_id",
                "attempts",
                "outcome_id",
                "requests",
                "request_bytes",
                "response_bytes",
                "prompt_tokens",
                "completion_tokens",
                "duration_ms",
                "evidence_fingerprint",
                "provider_revision",
                "downgraded",
                "observed_model_id",
            },
        )
        for forbidden in (
            CATALOG_ONLY.endpoint,
            "host",
            "credential",
            "authorization",
            "prompt_text",
            "candidate_text",
        ):
            self.assertNotIn(forbidden, encoded.lower())

    def test_a_typed_binding_setup_failure_is_not_collapsed_to_transport(self) -> None:
        authentication = build_prompt_model_outcome(
            PromptModelOutcomeId.AUTHENTICATION,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.REVIEW_CREDENTIAL,
            parameters=(),
        )

        def factory() -> AssistedDraftModelBinding:
            raise DraftModelExecutionError(authentication)

        result = _run(QUALIFIED, factory)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.AUTHENTICATION)
        self.assertIsNone(result.draft)
        self.assertIsNone(result.receipt)

    def test_each_explicit_execution_has_a_distinct_opaque_action_identity(self) -> None:
        def factory() -> AssistedDraftModelBinding:
            return AssistedDraftModelBinding(
                model=_ScriptedModel(DraftCandidate(TEMPLATE.text)),
                usage=lambda: AssistedDraftUsage(requests=1),
            )

        first = _run(QUALIFIED, factory)
        second = _run(QUALIFIED, factory)
        assert first.receipt is not None and second.receipt is not None
        self.assertNotEqual(first.receipt.action_id, second.receipt.action_id)

    def test_cancellation_after_first_response_prevents_the_repair_request(self) -> None:
        cancelled = False

        def cancel_after_first(call: int) -> None:
            nonlocal cancelled
            if call == 1:
                cancelled = True

        dirty = replace(TEMPLATE, text=TEMPLATE.text + " contact sheet").text
        model = _ScriptedModel(
            DraftCandidate(dirty),
            DraftCandidate(TEMPLATE.text),
            on_call=cancel_after_first,
        )

        result = _run(
            QUALIFIED,
            lambda: AssistedDraftModelBinding(
                model=model,
                usage=lambda: AssistedDraftUsage(requests=model.calls),
            ),
            cancellation=lambda: cancelled,
        )
        self.assertEqual(model.calls, 1)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.CANCELLED)
        self.assertIsNone(result.draft)
        self.assertIsNotNone(result.receipt)
        assert result.receipt is not None
        self.assertEqual(result.receipt.requests, 1)


if __name__ == "__main__":
    unittest.main()
