"""Thin ComfyUI node boundary for the private M17-16 producer authority."""

from __future__ import annotations

from typing import cast

from .adapters.ollama_native import SemanticOllamaExecutionError, SemanticOllamaTransport
from .core.local_adapters import LocalCancellationProbe
from .core.provider_setup import ProviderSetup
from .core.semantic_proposal_producer import (
    SemanticProposalProducerError,
    SemanticProposalReviewAuthority,
    _assemble_semantic_proposal_product,
    _begin_semantic_proposal_authority,
    _build_semantic_ollama_generation_request,
    _commit_semantic_proposal_authority,
    _fail_semantic_proposal_authority,
    build_semantic_source_bundle,
    load_semantic_provider_catalog,
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
        profiles = load_semantic_provider_catalog().profile_ids
        return {
            "required": {
                "report": ("H3_CONTEXT_REPORT", {}),
                "wiring": ("H3_NATIVE_H3_WIRING", {}),
                "provider_setup": ("H3_PROVIDER_SETUP", {}),
                "ollama_profile": (profiles, {"default": profiles[0]}),
            },
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
            catalog = load_semantic_provider_catalog()
            profile = catalog.require(ollama_profile)
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

                transport = self._transport
                if transport is None:
                    transport = SemanticDeadlineOllamaTransport(profile.model_id)
                execution = execute_semantic_ollama(
                    cast(SemanticOllamaTransport, transport),
                    cast(ProviderSetup, provider_setup),
                    profile,
                    _build_semantic_ollama_generation_request(source),
                    cancellation=cast(LocalCancellationProbe | None, self._cancellation_probe),
                )
                exact_execution = consume_semantic_ollama_execution_authority(execution)
                product = _assemble_semantic_proposal_product(
                    source,
                    exact_execution.model_result,
                    exact_execution.capability_fingerprint,
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
