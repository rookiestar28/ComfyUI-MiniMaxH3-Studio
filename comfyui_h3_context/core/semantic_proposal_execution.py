"""Provider-neutral, two-generation semantic proposal orchestration."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from math import isfinite

from .constrained_semantic_planning import parse_semantic_proposal
from .model_manifest import MAX_MODEL_PROMPT_CHARS, ModelGenerationRequest, ModelGenerationResult
from .semantic_proposal_producer import (
    SemanticProposalActionUsage,
    SemanticProposalProducerError,
    SemanticProposalProduct,
    SemanticSourceBundle,
    _assemble_semantic_proposal_product,
    _build_semantic_ollama_generation_request,
)

SEMANTIC_ACTION_SECONDS = 120.0
_REPAIRABLE = frozenset(
    {
        "provider_output_invalid",
        "semantic_dialogue_binding",
        "semantic_enrichment_rejected",
        "semantic_enrichment_noop",
        "semantic_target_protected",
        "semantic_proposals_missing",
    }
)
_PATHS = {
    "policy_mismatch": "$.policy",
    "task_mode_mutation": "$.task_mode",
    "duration_mutation": "$.effective_duration",
    "asset_identity_mutation": "$.asset_ids",
    "reference_order_mutation": "$.reference_order",
    "timeline_fingerprint_mismatch": "$.timeline_fingerprint",
    "reduction_fingerprint_mismatch": "$.reduction_fingerprint",
    "exact_text_mutation": "$.preserved_exact_text",
    "duplicate_proposal_id": "$.proposals",
    "duplicate_proposal_target": "$.proposals",
    "unsupported_source": "$.proposals",
    "unsupported_target": "$.proposals",
    "hard_constraint_mutation": "$.proposals",
    "proposal_invalid": "$",
    "evidence_policy_conflict": "$.proposals",
    "dialogue_binding_mutation": "$.proposals",
}


class SemanticProposalGenerationError(SemanticProposalProducerError):
    def __init__(self, code: str, usage: SemanticProposalActionUsage) -> None:
        self.usage = usage
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SemanticGenerationObservation:
    model_result: ModelGenerationResult
    capability_fingerprint: str
    usage: SemanticProposalActionUsage

    def __post_init__(self) -> None:
        if (
            type(self.model_result) is not ModelGenerationResult
            or type(self.usage) is not SemanticProposalActionUsage
            or self.usage.generations != 1
        ):
            raise SemanticProposalProducerError("provider_execution_authority")


def _add_usage(
    left: SemanticProposalActionUsage, right: SemanticProposalActionUsage
) -> SemanticProposalActionUsage:
    return SemanticProposalActionUsage(
        left.generations + right.generations,
        left.request_bytes + right.request_bytes,
        left.response_bytes + right.response_bytes,
        left.prompt_tokens + right.prompt_tokens,
        left.completion_tokens + right.completion_tokens,
    )


def build_semantic_repair_request(
    source: SemanticSourceBundle, previous: ModelGenerationResult, code: str
) -> ModelGenerationRequest:
    parsed = parse_semantic_proposal(
        source.planning_request,
        previous.text,
        model_result=previous,
        accepted_exact_text=source.baseline_plan.hard_constraints.exact_texts,
    )
    diagnostics = [
        {
            "code": diagnostic.code if diagnostic.code in _PATHS else "proposal_invalid",
            "path": _PATHS.get(diagnostic.code, "$"),
        }
        for diagnostic in parsed.diagnostics[:64]
    ]
    if not diagnostics:
        diagnostics = [
            {
                "code": code if code in _REPAIRABLE else "provider_output_invalid",
                "path": "$.proposals",
            }
        ]
    # SECURITY: do not replay the first prompt or source-bound schema constants. The repair may
    # contain only the prior untrusted proposal and closed code/path diagnostics, never messages.
    payload = {"previous_proposal": previous.text, "diagnostics": diagnostics}
    prompt = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(prompt) > MAX_MODEL_PROMPT_CHARS:
        raise SemanticProposalProducerError("semantic_repair_request_too_large")
    first = _build_semantic_ollama_generation_request(source, typed_intents=True)
    return replace(first, prompt=prompt, structured_schema={"type": "object"})


def run_semantic_proposal_generation(
    source: SemanticSourceBundle,
    generate: Callable[[ModelGenerationRequest, float], SemanticGenerationObservation],
    *,
    cancellation: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    action_deadline: float | None = None,
) -> SemanticProposalProduct:
    deadline = clock() + SEMANTIC_ACTION_SECONDS
    if action_deadline is not None:
        if type(action_deadline) not in {float, int} or not isfinite(action_deadline):
            raise SemanticProposalProducerError("semantic_deadline_invalid")
        # IMPORTANT: model discovery has already spent this action's budget; never restart it.
        deadline = min(deadline, action_deadline)
    if not isfinite(deadline):
        raise SemanticProposalProducerError("semantic_deadline_invalid")
    request = _build_semantic_ollama_generation_request(source, typed_intents=True)
    total = SemanticProposalActionUsage()
    for round_index in range(2):
        if cancellation is not None and cancellation():
            raise SemanticProposalGenerationError("cancelled", total)
        if clock() >= deadline:
            raise SemanticProposalGenerationError("timeout", total)
        try:
            observation = generate(request, deadline)
        except SemanticProposalProducerError as exc:
            failed_usage = (
                exc.usage
                if isinstance(exc, SemanticProposalGenerationError)
                else SemanticProposalActionUsage()
            )
            # IMPORTANT: a later transport failure must not erase a completed first generation's
            # observed usage. Failure metrics grant no execution or review authority.
            raise SemanticProposalGenerationError(
                exc.code, _add_usage(total, failed_usage)
            ) from None
        if type(observation) is not SemanticGenerationObservation:
            raise SemanticProposalProducerError("provider_execution_authority")
        usage = observation.usage
        total = _add_usage(total, usage)
        if cancellation is not None and cancellation():
            raise SemanticProposalGenerationError("cancelled", total)
        if clock() >= deadline:
            raise SemanticProposalGenerationError("timeout", total)
        try:
            product = _assemble_semantic_proposal_product(
                source,
                observation.model_result,
                observation.capability_fingerprint,
                typed_intents=True,
            )
        except SemanticProposalProducerError as exc:
            if exc.code not in _REPAIRABLE or round_index == 1:
                raise SemanticProposalGenerationError(exc.code, total) from None
            try:
                request = build_semantic_repair_request(source, observation.model_result, exc.code)
            except SemanticProposalProducerError as repair_error:
                raise SemanticProposalGenerationError(repair_error.code, total) from None
        else:
            if cancellation is not None and cancellation():
                raise SemanticProposalGenerationError("cancelled", total)
            if clock() >= deadline:
                raise SemanticProposalGenerationError("timeout", total)
            return replace(product, usage=total)
    raise SemanticProposalProducerError("provider_output_invalid")
