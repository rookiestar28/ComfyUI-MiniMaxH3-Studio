"""A semantic proposal action has one repair, bounded inputs and no direct render authority."""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import Any, cast

import pytest
from test_semantic_proposal_producer import (
    _Clock,
    _fixture_profile,
    _provider_bundle,
    _provider_setup,
    _semantic_document,
    _semantic_http_responses,
    _semantic_raw_server,
    _SemanticTransport,
)

import comfyui_h3_context.adapters.ollama_native as ollama_native
import comfyui_h3_context.core.semantic_proposal_producer as semantic_proposal_producer
import comfyui_h3_context.semantic_proposal_node as semantic_proposal_node
from comfyui_h3_context.adapters.ollama_native import (
    SemanticOllamaExecution,
    SemanticOllamaExecutionError,
    consume_semantic_ollama_execution_authority,
    execute_semantic_ollama,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.model_manifest import ModelGenerationRequest, ModelGenerationResult
from comfyui_h3_context.core.semantic_proposal_execution import (
    SemanticGenerationObservation,
    SemanticProposalGenerationError,
    build_semantic_repair_request,
    run_semantic_proposal_generation,
)
from comfyui_h3_context.core.semantic_proposal_producer import (
    SemanticProposalActionUsage,
    SemanticProposalProduct,
    SemanticProposalReviewAuthority,
    SemanticProviderCatalog,
    SemanticProviderProfile,
    SemanticSourceBundle,
    _build_semantic_ollama_generation_request,
    build_semantic_source_bundle,
    claim_semantic_proposal_review_authority,
)
from comfyui_h3_context.semantic_proposal_node import (
    H3SemanticProposalNodeError,
    H3SemanticProposalProducerNode,
)


def typed_document(source: SemanticSourceBundle) -> str:
    return _semantic_document(source, typed_intents=True)


def model_result(source: SemanticSourceBundle, text: str) -> ModelGenerationResult:
    return ModelGenerationResult(
        source.profile.model_manifest.backend_family,
        source.profile.model_id,
        source.profile.model_digest,
        canonical_fingerprint({"text": text}),
        {},
        text,
        source.profile.model_manifest.parser_path,
    )


class Generator:
    def __init__(self, source: SemanticSourceBundle, replies: list[str]) -> None:
        self.source = source
        self.replies = list(replies)
        self.requests: list[ModelGenerationRequest] = []
        self.deadlines: list[float] = []

    def __call__(
        self, request: ModelGenerationRequest, deadline: float
    ) -> SemanticGenerationObservation:
        self.requests.append(request)
        self.deadlines.append(deadline)
        return SemanticGenerationObservation(
            model_result(self.source, self.replies.pop(0)),
            "sha256:" + "9" * 64,
            SemanticProposalActionUsage(1, 101, 73, 11, 7),
        )


class RepairTransport(_SemanticTransport):
    def __init__(self, source: SemanticSourceBundle, replies: list[str]) -> None:
        super().__init__(source, typed_intents=True)
        self.replies = list(replies)
        self.chat_count = 0
        self.responses: list[Mapping[str, object]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        cancellation: Any = None,
        absolute_deadline: float,
        phase_timeout_seconds: float,
    ) -> Mapping[str, object]:
        if path == "/api/chat":
            self.chat_text = self.replies.pop(0)
            self.chat_count += 1
        result = super().request(
            method,
            path,
            payload,
            cancellation=cancellation,
            absolute_deadline=absolute_deadline,
            phase_timeout_seconds=phase_timeout_seconds,
        )
        if path == "/api/chat":
            result = {**result, "prompt_eval_count": 11, "eval_count": 7}
            self.responses.append(result)
        return result


def node_fixture(monkeypatch: pytest.MonkeyPatch) -> SemanticSourceBundle:
    baseline = _provider_bundle()
    profile = _fixture_profile(baseline)
    source = build_semantic_source_bundle(baseline.report, baseline.wiring, profile)
    monkeypatch.setattr(
        semantic_proposal_node,
        "load_semantic_provider_catalog",
        lambda: SemanticProviderCatalog("h3.semantic.provider_profiles.v1", (profile,)),
    )
    return source


def test_queued_repair_consumes_each_native_authority_and_only_commits_final_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = node_fixture(monkeypatch)
    transport = RepairTransport(source, ["{broken-first", typed_document(source)])
    executions: list[SemanticOllamaExecution] = []
    products: list[SemanticProposalProduct] = []
    consume = ollama_native.consume_semantic_ollama_execution_authority
    commit = semantic_proposal_producer._commit_semantic_proposal_authority

    def consumed(execution: object) -> SemanticOllamaExecution:
        exact = consume(execution)
        executions.append(exact)
        return exact

    def committed(
        reservation: object, product: SemanticProposalProduct
    ) -> SemanticProposalReviewAuthority:
        products.append(product)
        return commit(reservation, product)

    monkeypatch.setattr(ollama_native, "consume_semantic_ollama_execution_authority", consumed)
    monkeypatch.setattr(semantic_proposal_node, "_commit_semantic_proposal_authority", committed)
    node = H3SemanticProposalProducerNode(transport=transport)
    authority = node.produce(
        source.report,
        source.wiring,
        _provider_setup(),
        source.profile.profile_id,
        prompt_id="prompt.typed.repaired",
        execution_node_id="node.typed.repaired",
    )[0]
    assert type(authority) is SemanticProposalReviewAuthority
    assert len(products) == 1
    assert len(executions) == transport.chat_count == 2
    assert [call[1] for call in transport.calls].count("/api/generate") == 2
    for execution in executions:
        with pytest.raises(SemanticOllamaExecutionError, match="execution_authority"):
            consume(execution)
    usage = products[0].usage
    chat_calls = [call for call in transport.calls if call[1] == "/api/chat"]
    assert usage.generations == 2
    assert usage.request_bytes == sum(
        len(json.dumps(call[2], ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode())
        for call in chat_calls
    )
    assert usage.response_bytes == sum(
        len(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode())
        for value in transport.responses
    )
    assert usage.prompt_tokens == 22
    assert usage.completion_tokens == 14
    repair_body = cast(dict[str, Any], chat_calls[1][2])
    repair = json.loads(repair_body["messages"][0]["content"])
    assert set(repair) == {"previous_proposal", "diagnostics"}
    assert repair["previous_proposal"] == "{broken-first"
    assert all(set(item) == {"code", "path"} for item in repair["diagnostics"])
    assert "quiet studio" not in json.dumps(repair_body)
    assert repair_body["format"] == {"type": "object"}
    with claim_semantic_proposal_review_authority(authority) as claim:
        assert claim.bundle.transaction is products[0].transaction
        assert claim.bundle.transaction.state.value == "ready_for_review"
        claim.commit()


def test_queued_two_invalid_outputs_never_commit_or_send_a_third_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = node_fixture(monkeypatch)
    transport = RepairTransport(source, ["{broken-first", "{broken-second", typed_document(source)])
    node = H3SemanticProposalProducerNode(transport=transport)
    with pytest.raises(H3SemanticProposalNodeError, match="provider_output_invalid") as failed:
        node.produce(
            source.report,
            source.wiring,
            _provider_setup(),
            source.profile.profile_id,
            prompt_id="prompt.typed.refused",
            execution_node_id="node.typed.refused",
        )
    assert transport.chat_count == 2
    assert len(transport.replies) == 1
    assert [call[1] for call in transport.calls].count("/api/generate") == 2
    assert "broken-first" not in repr(failed.value)
    # A failed lineage stays terminal; host retries must not reset the same action's call budget.
    call_count = len(transport.calls)
    with pytest.raises(H3SemanticProposalNodeError, match="lineage_failed"):
        node.produce(
            source.report,
            source.wiring,
            _provider_setup(),
            source.profile.profile_id,
            prompt_id="prompt.typed.refused",
            execution_node_id="node.typed.refused",
        )
    assert len(transport.calls) == call_count
    # A newly queued action has a distinct host correlation and can run normally.
    result = node.produce(
        source.report,
        source.wiring,
        _provider_setup(),
        source.profile.profile_id,
        prompt_id="prompt.typed.new",
        execution_node_id="node.typed.new",
    )[0]
    assert type(result) is SemanticProposalReviewAuthority
    assert transport.chat_count == 3
    with claim_semantic_proposal_review_authority(result) as claim:
        claim.commit()


def test_native_invalid_json_requires_explicit_repair_opt_in_and_one_time_authority() -> None:
    source = node_fixture_for_native()
    for repair_enabled in (False, True):
        transport = RepairTransport(source, ["{broken-json"])
        request = _build_semantic_ollama_generation_request(source, typed_intents=True)
        if not repair_enabled:
            with pytest.raises(SemanticOllamaExecutionError, match="provider_output_invalid"):
                execute_semantic_ollama(transport, _provider_setup(), source.profile, request)
        else:
            execution = execute_semantic_ollama(
                transport, _provider_setup(), source.profile, request, allow_invalid_json=True
            )
            assert cast(ModelGenerationResult, execution.model_result).text == "{broken-json"
            assert execution.prompt_tokens == 11
            assert execution.completion_tokens == 7
            assert consume_semantic_ollama_execution_authority(execution) is execution
            with pytest.raises(SemanticOllamaExecutionError, match="execution_authority"):
                consume_semantic_ollama_execution_authority(execution)
        assert [call[1] for call in transport.calls[-2:]] == ["/api/generate", "/api/ps"]


def node_fixture_for_native() -> SemanticSourceBundle:
    baseline = _provider_bundle()
    profile = _fixture_profile(baseline)
    return build_semantic_source_bundle(baseline.report, baseline.wiring, profile)


def test_native_unsafe_final_text_refuses_with_closed_error_and_cleanup() -> None:
    source = node_fixture_for_native()
    transport = RepairTransport(source, ["unsafe\ud800value"])
    request = _build_semantic_ollama_generation_request(source, typed_intents=True)
    with pytest.raises(SemanticOllamaExecutionError, match="provider_output_invalid") as raised:
        execute_semantic_ollama(
            transport, _provider_setup(), source.profile, request, allow_invalid_json=True
        )
    assert "unsafe" not in str(raised.value)
    assert [call[1] for call in transport.calls[-2:]] == ["/api/generate", "/api/ps"]


def test_real_transport_counts_actual_http_bodies_before_cleanup_replaces_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = node_fixture_for_native()
    assert isinstance(source.profile, SemanticProviderProfile)
    default_response = _semantic_http_responses(source, source.profile)
    chat_response = {
        "model": source.profile.model_id,
        "created_at": "2026-10-06T00:00:00Z",
        "message": {"role": "assistant", "content": "{broken-json"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 11,
        "eval_count": 7,
    }
    # Whitespace makes actual wire bytes differ from a re-encoded canonical object.
    chat_body = json.dumps(chat_response, ensure_ascii=True, indent=2).encode("ascii")

    def responses(path: str) -> bytes:
        if path != "/api/chat":
            return default_response(path)
        return (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(chat_body)).encode("ascii")
            + b"\r\n\r\n"
            + chat_body
        )

    with _semantic_raw_server(monkeypatch, responses) as requests:
        transport = ollama_native.SemanticDeadlineOllamaTransport(source.profile.model_id)
        execution = execute_semantic_ollama(
            transport,
            _provider_setup(),
            source.profile,
            _build_semantic_ollama_generation_request(source, typed_intents=True),
            allow_invalid_json=True,
        )
        exact = consume_semantic_ollama_execution_authority(execution)
    chat_request = next(request for request in requests if request.startswith(b"POST /api/chat "))
    assert exact.request_bytes == len(chat_request.partition(b"\r\n\r\n")[2])
    assert exact.response_bytes == len(chat_body)
    assert exact.response_bytes != len(json.dumps(chat_response, separators=(",", ":")).encode())
    assert (exact.prompt_tokens, exact.completion_tokens) == (11, 7)
    assert requests[-2].startswith(b"POST /api/generate ")
    assert requests[-1].startswith(b"GET /api/ps ")


def test_review_authority_detects_mutated_action_usage_and_usage_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = node_fixture(monkeypatch)
    transport = RepairTransport(source, [typed_document(source)])
    products: list[SemanticProposalProduct] = []
    commit = semantic_proposal_producer._commit_semantic_proposal_authority

    def capture(
        reservation: object, product: SemanticProposalProduct
    ) -> SemanticProposalReviewAuthority:
        products.append(product)
        return commit(reservation, product)

    monkeypatch.setattr(semantic_proposal_node, "_commit_semantic_proposal_authority", capture)
    authority = H3SemanticProposalProducerNode(transport=transport).produce(
        source.report,
        source.wiring,
        _provider_setup(),
        source.profile.profile_id,
        prompt_id="prompt.typed.usage",
        execution_node_id="node.typed.usage",
    )[0]
    object.__setattr__(products[0].usage, "request_bytes", products[0].usage.request_bytes + 1)
    with pytest.raises(
        semantic_proposal_producer.SemanticProposalProducerError, match="review_authority_content"
    ):
        with claim_semantic_proposal_review_authority(authority):
            pass
    for invalid in (True, -1, 2**63):
        with pytest.raises(
            semantic_proposal_producer.SemanticProposalProducerError, match="semantic_usage_invalid"
        ):
            SemanticProposalActionUsage(1, invalid)


def test_native_shared_action_deadline_stops_preflight_before_chat_or_cleanup() -> None:
    source = node_fixture_for_native()
    clock = _Clock()
    transport = _SemanticTransport(source, clock=clock)
    with pytest.raises(SemanticOllamaExecutionError, match="timeout"):
        ollama_native._execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            source.profile,
            _build_semantic_ollama_generation_request(source, typed_intents=True),
            cancellation=None,
            clock=clock,
            allow_invalid_json=True,
            action_deadline=145.0,
        )
    assert all(call[1] not in {"/api/chat", "/api/generate"} for call in transport.calls)
    assert max(call[3] for call in transport.calls) <= 45.0


class SemanticProposalExecutionTests(unittest.TestCase):
    def test_transport_failure_keeps_previous_observed_usage_and_never_retries(self) -> None:
        source = _provider_bundle()
        generate = Generator(source, ["{broken-first"])
        calls = [0]

        def observe(
            request: ModelGenerationRequest, deadline: float
        ) -> SemanticGenerationObservation:
            calls[0] += 1
            if calls[0] == 2:
                raise SemanticProposalGenerationError("transport", SemanticProposalActionUsage())
            return generate(request, deadline)

        with self.assertRaises(SemanticProposalGenerationError) as failed:
            run_semantic_proposal_generation(source, observe, clock=lambda: 0.0)
        self.assertEqual(calls[0], 2)
        self.assertEqual(failed.exception.code, "transport")
        self.assertEqual(failed.exception.usage, SemanticProposalActionUsage(1, 101, 73, 11, 7))

    def test_valid_first_pass_and_repair_are_counted_and_only_final_product_is_returned(
        self,
    ) -> None:
        source = _provider_bundle()
        for replies, generations in (
            ([typed_document(source)], 1),
            (["{broken-json", typed_document(source)], 2),
        ):
            generate = Generator(source, replies)
            product = run_semantic_proposal_generation(source, generate, clock=lambda: 0.0)
            self.assertEqual(len(generate.requests), generations)
            self.assertEqual(product.usage.generations, generations)
            self.assertEqual(product.usage.request_bytes, 101 * generations)
            self.assertEqual(product.usage.response_bytes, 73 * generations)
            self.assertEqual(product.usage.prompt_tokens, 11 * generations)
            self.assertEqual(product.usage.completion_tokens, 7 * generations)
            self.assertEqual(generate.deadlines, [120.0] * generations)
            self.assertEqual(product.transaction.attempt, 1)

    def test_second_invalid_proposal_is_a_typed_refusal_without_a_third_generation(self) -> None:
        source = _provider_bundle()
        generate = Generator(source, ["{broken-first", "{broken-second", typed_document(source)])
        with self.assertRaises(SemanticProposalGenerationError) as raised:
            run_semantic_proposal_generation(source, generate, clock=lambda: 0.0)
        self.assertEqual(raised.exception.code, "provider_output_invalid")
        self.assertEqual(raised.exception.usage.generations, 2)
        self.assertEqual(len(generate.requests), 2)

    def test_repair_has_only_previous_output_and_closed_diagnostics_with_no_source_replay(
        self,
    ) -> None:
        source = _provider_bundle()
        request = build_semantic_repair_request(
            source, model_result(source, "{broken-output"), "private-diagnostic-message"
        )
        payload = json.loads(request.prompt)
        self.assertEqual(set(payload), {"previous_proposal", "diagnostics"})
        self.assertEqual(payload["previous_proposal"], "{broken-output")
        self.assertEqual(payload["diagnostics"], [{"code": "proposal_invalid", "path": "$"}])
        self.assertNotIn("private-diagnostic-message", request.prompt)
        self.assertNotIn("quiet studio", request.prompt)
        self.assertEqual(request.structured_schema, {"type": "object"})
        self.assertFalse(request.media_fingerprints)
        self.assertFalse(request.image_payloads)

    def test_oversized_repair_refuses_instead_of_truncating_previous_proposal(self) -> None:
        source = _provider_bundle()
        generate = Generator(source, ["x" * 32_767, typed_document(source)])
        with self.assertRaises(SemanticProposalGenerationError) as raised:
            run_semantic_proposal_generation(source, generate, clock=lambda: 0.0)
        self.assertEqual(raised.exception.code, "semantic_repair_request_too_large")
        self.assertEqual(raised.exception.usage.generations, 1)
        self.assertEqual(len(generate.requests), 1)

    def test_shared_cancellation_and_deadline_stop_before_repair(self) -> None:
        source = _provider_bundle()

        def case(cancel: bool) -> None:
            generate = Generator(source, ["{broken-first", typed_document(source)])
            elapsed = [0.0]
            cancelled = [False]

            def observe(
                request: ModelGenerationRequest, deadline: float
            ) -> SemanticGenerationObservation:
                result = generate(request, deadline)
                elapsed[0] = 121.0 if not cancel else 0.0
                cancelled[0] = cancel
                return result

            with self.assertRaises(SemanticProposalGenerationError) as raised:
                run_semantic_proposal_generation(
                    source, observe, clock=lambda: elapsed[0], cancellation=lambda: cancelled[0]
                )
            self.assertEqual(raised.exception.code, "cancelled" if cancel else "timeout")
            self.assertEqual(raised.exception.usage.generations, 1)
            self.assertEqual(len(generate.requests), 1)

        for cancel in (True, False):
            case(cancel)

    def test_whole_fence_is_allowed_but_prefix_and_trailing_prose_are_refused(self) -> None:
        source = _provider_bundle()
        good = "```json\n" + typed_document(source) + "\n```"
        product = run_semantic_proposal_generation(
            source, Generator(source, [good]), clock=lambda: 0.0
        )
        self.assertEqual(product.usage.generations, 1)
        for text in ("prefix " + good, good + " trailing prose"):
            with self.assertRaises(SemanticProposalGenerationError):
                run_semantic_proposal_generation(
                    source, Generator(source, [text, text]), clock=lambda: 0.0
                )
