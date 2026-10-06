"""Thin ComfyUI node boundary for the private M17-16 producer authority."""

from __future__ import annotations

import time
from typing import cast

from .adapters.ollama_native import SemanticOllamaExecutionError, SemanticOllamaTransport
from .core.local_adapters import LocalCancellationProbe
from .core.model_manifest import ModelGenerationRequest, ModelGenerationResult
from .core.provider_setup import ProviderSetup
from .core.semantic_proposal_execution import (
    SemanticGenerationObservation,
    SemanticProposalGenerationError,
    run_semantic_proposal_generation,
)
from .core.semantic_proposal_producer import (
    SemanticExecutionProfile,
    SemanticProposalActionUsage,
    SemanticProposalProducerError,
    SemanticProposalReviewAuthority,
    _begin_semantic_proposal_authority,
    _commit_semantic_proposal_authority,
    _fail_semantic_proposal_authority,
    build_semantic_source_bundle,
    load_semantic_connection,
    load_semantic_provider_catalog,
    validate_semantic_source_authority,
)

SEMANTIC_PROPOSAL_PRODUCER_NODE_ID = "comfyui_h3_context.H3Context.SemanticProposalProducer"
SEMANTIC_PROPOSAL_PRODUCER_DISPLAY_NAME = "H3 Semantic Proposal Producer"
SEMANTIC_PROPOSAL_REVIEW_AUTHORITY_SOCKET_TYPE = "H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY"


class H3SemanticProposalNodeError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class H3SemanticProposalProducerNode:
    """Produce one content-free handle to a process-local semantic review seed."""

    NODE_ID = SEMANTIC_PROPOSAL_PRODUCER_NODE_ID
    __h3_context_node_id__ = NODE_ID
    RETURN_TYPES = (SEMANTIC_PROPOSAL_REVIEW_AUTHORITY_SOCKET_TYPE,)
    RETURN_NAMES = ("review_authority",)
    FUNCTION = "produce"
    CATEGORY = "h3_context/semantic"
    DESCRIPTION = "Produces a qualified process-local semantic proposal review authority."
    OUTPUT_NODE = True

    def __init__(
        self,
        *,
        transport: object | None = None,
        cancellation_probe: object | None = None,
    ) -> None:
        # Test/host injection never supplies endpoint or model authority; the adapter still checks
        # the exact catalog profile and fixed-loopback endpoint fingerprint before generation.
        self._transport = transport
        self._cancellation_probe = cancellation_probe

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, object]]:
        profiles = (
            load_semantic_connection().profile_id,
            *load_semantic_provider_catalog().profile_ids,
        )
        return {
            "required": {
                "report": ("H3_CONTEXT_REPORT", {}),
                "wiring": ("H3_NATIVE_H3_WIRING", {}),
                "provider_setup": ("H3_PROVIDER_SETUP", {}),
                "ollama_profile": (profiles, {"default": profiles[0]}),
            },
            # IMPORTANT: optional preserves saved workflows that predate the model-choice field.
            "optional": {"ollama_model": ("STRING", {"default": ""})},
            # IMPORTANT: PROMPT is never an identity authority.  It only causes host injection;
            # produce() obtains the exact prompt/node IDs from the reviewed execution context.
            "hidden": {
                "prompt_id": "PROMPT",
                "execution_node_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def IS_CHANGED(cls, **_: object) -> float:
        # Provider qualification and one-time process authority must not be cache-replayed.
        return float("nan")

    def produce(
        self,
        report: object,
        wiring: object,
        provider_setup: object,
        ollama_profile: object,
        prompt_id: object | None = None,
        execution_node_id: object | None = None,
        ollama_model: object = "",
    ) -> tuple[object]:
        try:
            from .adapters.comfyui_execution import current_execution_correlation

            correlation = current_execution_correlation()
        except (ImportError, RuntimeError, TypeError, ValueError):
            correlation = None
        if correlation is not None:
            host_prompt_id, host_node_id = correlation
            if isinstance(execution_node_id, str) and execution_node_id != host_node_id:
                raise H3SemanticProposalNodeError("correlation_mismatch")
            prompt_id = host_prompt_id
            execution_node_id = host_node_id
        if type(prompt_id) is not str or type(execution_node_id) is not str:
            raise H3SemanticProposalNodeError("missing_correlation")
        try:
            from .adapters.ollama_native import (
                SemanticDeadlineOllamaTransport,
                resolve_semantic_model_profile,
            )

            action_deadline: float | None = None
            profile: SemanticExecutionProfile
            transport = self._transport
            probe = self._cancellation_probe
            if probe is not None and not isinstance(probe, LocalCancellationProbe):
                raise SemanticOllamaExecutionError("cancellation_invalid")
            if type(ollama_profile) is str and ollama_profile == "ollama.local":
                validate_semantic_source_authority(report, wiring)
                if type(ollama_model) is not str or not ollama_model:
                    raise SemanticOllamaExecutionError("choose_local_model")
                connection = load_semantic_connection()
                action_deadline = time.monotonic() + connection.max_action_seconds
                if transport is None:
                    transport = SemanticDeadlineOllamaTransport(ollama_model)
                profile = resolve_semantic_model_profile(
                    cast(SemanticOllamaTransport, transport),
                    cast(ProviderSetup, provider_setup),
                    connection,
                    ollama_model,
                    cancellation=probe,
                    action_deadline=action_deadline,
                )
            else:
                profile = load_semantic_provider_catalog().require(ollama_profile)
                if ollama_model != "" and ollama_model != profile.model_id:
                    raise SemanticOllamaExecutionError("model_choice_invalid")
            source = build_semantic_source_bundle(report, wiring, profile)
            pending = _begin_semantic_proposal_authority(
                source,
                provider_setup,
                prompt_id,
                execution_node_id,
            )
            if type(pending) is SemanticProposalReviewAuthority:
                return (pending,)
            try:
                # Kept local so the provider adapter remains dormant until this node is queued.
                from .adapters.ollama_native import (
                    SemanticDeadlineOllamaTransport,
                    consume_semantic_ollama_execution_authority,
                    execute_semantic_ollama,
                )

                if transport is None:
                    transport = SemanticDeadlineOllamaTransport(profile.model_id)

                def generate(
                    request: ModelGenerationRequest, deadline: float
                ) -> SemanticGenerationObservation:
                    try:
                        execution = execute_semantic_ollama(
                            cast(SemanticOllamaTransport, transport),
                            cast(ProviderSetup, provider_setup),
                            profile,
                            request,
                            cancellation=probe,
                            allow_invalid_json=True,
                            action_deadline=deadline,
                        )
                    except SemanticOllamaExecutionError as exc:
                        raise SemanticProposalGenerationError(
                            exc.code, SemanticProposalActionUsage()
                        ) from None
                    # CRITICAL: each generation, including repair, must consume its own adapter
                    # authority after cleanup. A copied result cannot authorize a final proposal.
                    exact = consume_semantic_ollama_execution_authority(execution)
                    return SemanticGenerationObservation(
                        cast(ModelGenerationResult, exact.model_result),
                        exact.capability_fingerprint,
                        SemanticProposalActionUsage(
                            1,
                            exact.request_bytes,
                            exact.response_bytes,
                            exact.prompt_tokens,
                            exact.completion_tokens,
                        ),
                    )

                product = run_semantic_proposal_generation(
                    source,
                    generate,
                    cancellation=None if probe is None else probe.is_cancelled,
                    action_deadline=action_deadline,
                )
                authority = _commit_semantic_proposal_authority(pending, product)
            except Exception as exc:
                # IMPORTANT: failed provider/output/transaction work must close its reservation and
                # expose only a closed outcome code, never the prompt, response, or source objects.
                try:
                    _fail_semantic_proposal_authority(pending)
                except SemanticProposalProducerError:
                    pass
                if isinstance(exc, SemanticProposalProducerError):
                    raise
                if isinstance(exc, SemanticOllamaExecutionError):
                    raise SemanticProposalProducerError(exc.code) from exc
                raise SemanticProposalProducerError("producer_execution_failed") from exc
        except SemanticProposalProducerError as exc:
            raise H3SemanticProposalNodeError(exc.code) from None
        except SemanticOllamaExecutionError as exc:
            raise H3SemanticProposalNodeError(exc.code) from None
        except Exception:
            # The host boundary must not serialize an unexpected source/provider exception chain.
            raise H3SemanticProposalNodeError("producer_execution_failed") from None
        return (authority,)


__all__ = [
    "H3SemanticProposalNodeError",
    "H3SemanticProposalProducerNode",
    "SEMANTIC_PROPOSAL_PRODUCER_DISPLAY_NAME",
    "SEMANTIC_PROPOSAL_PRODUCER_NODE_ID",
    "SEMANTIC_PROPOSAL_REVIEW_AUTHORITY_SOCKET_TYPE",
]
