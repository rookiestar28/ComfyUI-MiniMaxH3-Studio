from __future__ import annotations

import pytest

import comfyui_h3_context.semantic_proposal_node as semantic_proposal_node
from comfyui_h3_context.nodes import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    SEMANTIC_PROPOSAL_PRODUCER_NODE_ID,
    SEMANTIC_PROPOSAL_REVIEW_AUTHORITY_SOCKET_TYPE,
)
from comfyui_h3_context.semantic_proposal_node import (
    H3SemanticProposalNodeError,
    H3SemanticProposalProducerNode,
)


def test_semantic_proposal_node_is_additive_independently_queueable_producer() -> None:
    node = H3SemanticProposalProducerNode

    assert NODE_CLASS_MAPPINGS[SEMANTIC_PROPOSAL_PRODUCER_NODE_ID] is node
    assert NODE_DISPLAY_NAME_MAPPINGS[SEMANTIC_PROPOSAL_PRODUCER_NODE_ID] == (
        "H3 Semantic Proposal Producer"
    )
    assert node.OUTPUT_NODE is True
    assert node.RETURN_TYPES == (SEMANTIC_PROPOSAL_REVIEW_AUTHORITY_SOCKET_TYPE,)
    inputs = node.INPUT_TYPES()
    assert tuple(inputs) == ("required", "optional", "hidden")
    assert tuple(inputs["required"]) == (
        "report",
        "wiring",
        "provider_setup",
        "ollama_profile",
    )
    assert "endpoint" not in inputs["required"]
    assert tuple(inputs["optional"]) == ("ollama_model",)
    assert inputs["optional"]["ollama_model"] == ("STRING", {"default": ""})
    assert tuple(inputs["hidden"]) == ("prompt_id", "execution_node_id")


def test_semantic_proposal_node_suppresses_unexpected_private_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_with_private_content(*_: object) -> None:
        raise RuntimeError("private prompt must not cross the node boundary")

    monkeypatch.setattr(
        semantic_proposal_node,
        "build_semantic_source_bundle",
        fail_with_private_content,
    )
    node = H3SemanticProposalProducerNode()

    with pytest.raises(H3SemanticProposalNodeError) as captured:
        node.produce(
            object(),
            object(),
            object(),
            "ollama.qwen3_8.27b_bf16.local",
            prompt_id="prompt.private",
            execution_node_id="node.private",
        )

    assert captured.value.code == "producer_execution_failed"
    assert captured.value.__cause__ is None
    assert "private prompt" not in repr(captured.value)
