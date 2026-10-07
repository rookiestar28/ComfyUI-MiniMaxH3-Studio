"""Current semantic choices are native identities, never model-name defaults."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

import pytest
from test_semantic_proposal_producer import (
    _Clock,
    _fixture_profile,
    _provider_bundle,
    _provider_setup,
    _SemanticTransport,
)

import comfyui_h3_context.core.semantic_proposal_producer as producer
from comfyui_h3_context.adapters.ollama_native import (
    SemanticOllamaExecutionError,
    _execute_semantic_ollama_test_only,
    _semantic_chat_payload,
    execute_semantic_ollama,
    resolve_semantic_model_profile,
)
from comfyui_h3_context.core.model_manifest import ModelGenerationResult
from comfyui_h3_context.core.semantic_proposal_producer import (
    SEMANTIC_MODEL_MAX_BYTES,
    SemanticChosenModelProfile,
    SemanticProposalReviewAuthority,
    _build_semantic_ollama_generation_request,
    build_semantic_source_bundle,
    claim_semantic_proposal_review_authority,
    load_semantic_connection,
)
from comfyui_h3_context.semantic_proposal_node import (
    H3SemanticProposalNodeError,
    H3SemanticProposalProducerNode,
)


class NoRequests:
    calls: list[object]

    def __init__(self) -> None:
        self.calls = []

    def request(self, *args: object, **kwargs: object) -> object:
        self.calls.append(args)
        raise AssertionError("selection must fail before native contact")


def test_new_node_exposes_optional_choice_without_breaking_saved_required_inputs() -> None:
    inputs = H3SemanticProposalProducerNode.INPUT_TYPES()
    assert tuple(inputs["required"]) == ("report", "wiring", "provider_setup", "ollama_profile")
    declaration = inputs["required"]["ollama_profile"]
    assert isinstance(declaration, tuple)
    profiles, settings = declaration
    assert profiles == ("ollama.local", "ollama.qwen3_8.27b_bf16.local")
    assert settings == {"default": "ollama.local"}
    assert inputs["optional"]["ollama_model"] == ("STRING", {"default": ""})


def test_empty_choice_refuses_before_any_native_request() -> None:
    bundle = _provider_bundle()
    transport = NoRequests()
    node = H3SemanticProposalProducerNode(transport=transport)
    with pytest.raises(H3SemanticProposalNodeError, match="choose_local_model"):
        node.produce(
            bundle.report,
            bundle.wiring,
            _provider_setup(),
            "ollama.local",
            prompt_id="prompt.empty.choice",
            execution_node_id="node.empty.choice",
            ollama_model="",
        )
    assert not transport.calls


class NativeChoiceTransport(_SemanticTransport):
    def __init__(self, mode: str = "valid") -> None:
        bundle = _provider_bundle()
        profile = replace(
            _fixture_profile(bundle),
            model_id="selected:local",
            model_size_bytes=4 * 1024**3,
            quantization_level="Q4_K_M",
        )
        super().__init__(bundle, profile=profile, typed_intents=True)
        self.mode = mode
        self.tags_count = 0
        self.deadlines: list[tuple[str, float]] = []

    def request(
        self, method: str, path: str, payload: Mapping[str, object] | None = None, **kwargs: Any
    ) -> Mapping[str, object]:
        self.deadlines.append((path, kwargs["absolute_deadline"]))
        result = json.loads(json.dumps(super().request(method, path, payload, **kwargs)))
        if path == "/api/tags":
            self.tags_count += 1
            rows = result["models"]
            if self.mode == "missing":
                result["models"] = []
            elif self.mode == "duplicate":
                rows.append(dict(rows[0]))
            elif self.mode == "oversized":
                rows[0]["size"] = SEMANTIC_MODEL_MAX_BYTES + 1
            elif self.mode == "cloud":
                rows[0]["remote_host"] = "https://untrusted.invalid"
            elif self.mode == "drift" and self.tags_count > 1:
                rows[0]["digest"] = "e" * 64
            elif self.mode == "many":
                for index in range(70):
                    row = dict(rows[0])
                    row["name"] = row["model"] = f"other-{index}:local"
                    rows.append(row)
        if path == "/api/show":
            if self.mode == "embedding":
                result["capabilities"] = ["embedding"]
            elif self.mode == "unknown-context":
                result["model_info"] = {}
            elif self.mode == "minimal":
                result.pop("license")
                result["details"].pop("quantization_level")
                result["details"].pop("parameter_size")
            elif self.mode == "thinking-off":
                result["thinking"] = {"values": [False, True]}
            elif self.mode == "thinking-levels":
                result["thinking"] = {"values": ["low", "high"]}
        if path == "/api/chat":
            result["message"]["thinking"] = "PRIVATE_THINKING_SENTINEL"
            if self.mode == "cached-metric":
                result["prompt_eval_cached_count"] = 3
        return cast(Mapping[str, object], result)


def resolve(transport: NativeChoiceTransport) -> SemanticChosenModelProfile:
    return resolve_semantic_model_profile(
        transport,
        _provider_setup(),
        load_semantic_connection(),
        transport.profile.model_id,
        action_deadline=time.monotonic() + 120,
    )


@pytest.mark.parametrize(
    "mode,code,last",
    [
        ("missing", "model_missing", "/api/tags"),
        ("duplicate", "model_identity_invalid", "/api/tags"),
        ("cloud", "model_identity_invalid", "/api/tags"),
        ("oversized", "model_resource_limit", "/api/tags"),
        ("embedding", "capability_mismatch", "/api/show"),
        ("unknown-context", "model_metadata_missing", "/api/show"),
    ],
)
def test_invalid_native_choice_refuses_before_chat(mode: str, code: str, last: str) -> None:
    transport = NativeChoiceTransport(mode)
    with pytest.raises(SemanticOllamaExecutionError, match=code):
        resolve(transport)
    assert transport.calls[-1][1] == last
    assert not any(path == "/api/chat" for _, path, *_ in transport.calls)


@pytest.mark.parametrize(
    "mode", ["valid", "many", "minimal", "thinking-off", "thinking-levels", "cached-metric"]
)
def test_chosen_model_runs_exact_native_identity_with_typed_output_and_cleanup(mode: str) -> None:
    transport = NativeChoiceTransport(mode)
    bundle = _provider_bundle()
    node = H3SemanticProposalProducerNode(transport=transport)
    authority = node.produce(
        bundle.report,
        bundle.wiring,
        _provider_setup(),
        "ollama.local",
        prompt_id=f"prompt.choice.{mode}",
        execution_node_id=f"node.choice.{mode}",
        ollama_model=transport.profile.model_id,
    )[0]
    assert type(authority) is SemanticProposalReviewAuthority
    assert [call[1] for call in transport.calls[-2:]] == ["/api/generate", "/api/ps"]
    lineage_id = authority.to_public_dict()["lineage_id"]
    assert isinstance(lineage_id, str)
    entry = producer._PROCESS_AUTHORITY_OWNER._entries[lineage_id]
    assert entry.source is not None
    observed = entry.source.profile.to_wire()
    observed_metadata = observed["metadata"]
    assert isinstance(observed_metadata, dict)
    with claim_semantic_proposal_review_authority(authority) as claim:
        assert observed["model_id"] == transport.profile.model_id
        assert observed["model_digest"] == transport.profile.model_digest
        assert observed_metadata["family"] == transport.profile.model_family
        assert observed_metadata["quantization"] == (None if mode == "minimal" else "Q4_K_M")
        assert "PRIVATE_THINKING_SENTINEL" not in repr(claim.bundle)
        claim.abort()
    chat = next(call[2] for call in transport.calls if call[1] == "/api/chat")
    assert chat["model"] == "selected:local"
    assert chat["options"]["num_predict"] == 2048
    assert chat["options"]["num_ctx"] <= transport.profile.context_length
    assert (chat.get("think") is False) == (mode == "thinking-off")
    if mode != "thinking-off":
        assert "think" not in chat


def test_digest_drift_between_choice_and_generation_refuses_without_rebinding() -> None:
    transport = NativeChoiceTransport("drift")
    profile = resolve(transport)
    bundle = _provider_bundle()
    source = build_semantic_source_bundle(bundle.report, bundle.wiring, profile)
    with pytest.raises(SemanticOllamaExecutionError, match="model_identity_mismatch"):
        execute_semantic_ollama(
            transport,
            _provider_setup(),
            profile,
            _build_semantic_ollama_generation_request(source, typed_intents=True),
        )
    assert not any(path == "/api/chat" for _, path, *_ in transport.calls)


@pytest.mark.parametrize("value", [True, -1, "3", 1.5, {}])
def test_cached_token_metric_remains_a_nonnegative_integer(value: object) -> None:
    from comfyui_h3_context.adapters.ollama_native import _semantic_chat_result

    profile = resolve(NativeChoiceTransport())
    payload = {
        "model": profile.model_id,
        "created_at": "2026-10-06T00:00:00Z",
        "message": {"role": "assistant", "content": "{}"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_cached_count": value,
    }
    with pytest.raises(SemanticOllamaExecutionError, match="generation_metadata_invalid"):
        _semantic_chat_result(payload, profile)


def test_manifest_never_infers_quantization_or_license_and_context_refusal_is_before_chat() -> None:
    transport = NativeChoiceTransport("minimal")
    profile = resolve(transport)
    manifest = profile.model_manifest
    assert manifest.runtime.dtype == "auto"
    assert manifest.license == "native.license.unreported"
    assert manifest.runtime.limits.max_memory_bytes == SEMANTIC_MODEL_MAX_BYTES
    bundle = _provider_bundle()
    source = build_semantic_source_bundle(bundle.report, bundle.wiring, profile)
    request = _build_semantic_ollama_generation_request(source, typed_intents=True)
    bounded = replace(profile, metadata=replace(profile.metadata, context_length=8192))
    with pytest.raises(SemanticOllamaExecutionError, match="model_context_limit"):
        _semantic_chat_payload(bounded, replace(request, prompt="x" * 8192))


def test_current_generation_and_cleanup_deadlines_remain_inside_original_action() -> None:
    transport = NativeChoiceTransport()
    profile = resolve(transport)
    bundle = _provider_bundle()
    source = build_semantic_source_bundle(bundle.report, bundle.wiring, profile)
    transport.deadlines.clear()
    clock = _Clock()
    result = _execute_semantic_ollama_test_only(
        transport,
        _provider_setup(),
        profile,
        _build_semantic_ollama_generation_request(source, typed_intents=True),
        clock=clock,
        cancellation=None,
        action_deadline=187.0,
    )
    model_result = result.model_result
    assert isinstance(model_result, ModelGenerationResult)
    assert model_result.model_id == profile.model_id
    assert all(deadline <= 187.0 for _, deadline in transport.deadlines)
    assert next(deadline for path, deadline in transport.deadlines if path == "/api/chat") < 187.0


@pytest.mark.parametrize("value", ["not a model", "../outside", "\ud800", 23])
def test_malformed_model_refuses_before_contact(value: object) -> None:
    transport = NativeChoiceTransport()
    with pytest.raises(
        SemanticOllamaExecutionError, match="model_choice_invalid|choose_local_model"
    ):
        resolve_semantic_model_profile(
            transport,
            _provider_setup(),
            load_semantic_connection(),
            value,
            action_deadline=time.monotonic() + 120,
        )
    assert not transport.calls


def test_namespaced_native_model_id_is_an_identity_not_a_locator() -> None:
    transport = NativeChoiceTransport()
    transport.profile = replace(transport.profile, model_id="namespace/selected:local")
    profile = resolve(transport)
    assert profile.model_manifest.model_id == "namespace/selected:local"
    assert profile.model_manifest.runtime.dtype == "auto"


@pytest.mark.parametrize(
    "value", ["https://bad.invalid/model", "C:/private/model", "../weights", "bad\\path", "x" * 257]
)
def test_model_manifest_still_refuses_locator_and_oversized_names(value: str) -> None:
    from comfyui_h3_context.core.errors import ModelManifestError

    profile = resolve(NativeChoiceTransport())
    with pytest.raises(ModelManifestError):
        replace(profile.model_manifest, model_id=value)


def test_only_optional_free_string_socket_may_have_empty_default() -> None:
    from comfyui_h3_context.core.errors import NodeContractError
    from comfyui_h3_context.core.node_contracts import NodeSocket, NodeSocketType

    assert (
        NodeSocket("model", NodeSocketType.STRING, False, "", 0, 1, (), "exact choice").default
        == ""
    )
    with pytest.raises(NodeContractError):
        NodeSocket("model", NodeSocketType.STRING, True, "", 1, 1, (), "exact choice")
    with pytest.raises(NodeContractError):
        NodeSocket("model", NodeSocketType.STRING, False, "", 0, 1, ("selected",), "exact choice")
