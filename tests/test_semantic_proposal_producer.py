from __future__ import annotations

import hashlib
import json
import socket
import socketserver
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

import comfyui_h3_context.adapters.ollama_native as ollama_native
import comfyui_h3_context.core.semantic_proposal_producer as semantic_proposal_producer
import comfyui_h3_context.semantic_proposal_node as semantic_proposal_node
from comfyui_h3_context.adapters.ollama_native import (
    LoopbackOllamaTransport,
    SemanticOllamaExecutionError,
    _execute_semantic_ollama_test_only,
    _semantic_request,
    consume_semantic_ollama_execution_authority,
    execute_semantic_ollama,
)
from comfyui_h3_context.core import (
    IntentScene,
    ProviderIdentity,
    TaskMode,
    TimelineSegment,
    TimePoint,
    build_intent_graph,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.constrained_semantic_planning import (
    build_semantic_generation_request,
    semantic_proposal_json_schema,
)
from comfyui_h3_context.core.errors import LocalAdapterTimeoutError, ModelTransportError
from comfyui_h3_context.core.provider_policy import (
    ProviderExecutionPolicy,
    ProviderPrivacyMode,
)
from comfyui_h3_context.core.provider_setup import (
    ProviderLocalBackend,
    ProviderSetup,
    build_provider_setup,
)
from comfyui_h3_context.core.semantic_enrichment import SemanticEnrichmentStatus
from comfyui_h3_context.core.semantic_proposal_producer import (
    FIXED_OLLAMA_ENDPOINT,
    SemanticProposalProducerError,
    SemanticProposalProduct,
    SemanticProposalReviewAuthority,
    SemanticProviderCatalog,
    SemanticProviderProfile,
    SemanticSourceBundle,
    _assemble_semantic_proposal_product,
    _build_semantic_ollama_generation_request,
    _SemanticProposalAuthorityOwner,
    build_semantic_source_bundle,
    load_semantic_provider_catalog,
    ollama_native_digest_to_model_digest,
    parse_ollama_native_digest,
)
from comfyui_h3_context.core.semantic_proposal_transaction import (
    SemanticProposalTransactionState,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextNativeH3AdapterNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)
from comfyui_h3_context.semantic_proposal_node import (
    H3SemanticProposalNodeError,
    H3SemanticProposalProducerNode,
)


class _DerivedSemanticDeadlineOllamaTransport(ollama_native.SemanticDeadlineOllamaTransport):
    pass


def _authoritative_source(*, explicit_scene: bool = True) -> tuple[Any, Any]:
    raw = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "Preserve the exact declared intent.",
        duration_seconds=5.0,
    )[0]
    graph = None
    if explicit_scene:
        duration = TimePoint.from_text("5.166666666666667")
        result = build_intent_graph(
            effective_duration=duration,
            registry=raw.reference_registry,
            scenes=(IntentScene("scene_1", "A quiet studio."),),
            segments=(
                TimelineSegment(
                    "segment_1",
                    TimePoint.from_text("0"),
                    duration,
                    scene_id="scene_1",
                ),
            ),
        )
        assert result.graph is not None
        graph = result.graph
    plan = H3ContextPlanNode().build_plan(raw, intent_graph=graph)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    wiring = H3ContextNativeH3AdapterNode().adapt(report)[1]
    return report, wiring


def test_catalog_profile_is_exact_reviewed_local_authority() -> None:
    catalog = load_semantic_provider_catalog()

    assert catalog.schema == "h3.semantic.provider_profiles.v1"
    assert catalog.profile_ids == ("ollama.qwen3_8.27b_bf16.local",)
    profile = catalog.require("ollama.qwen3_8.27b_bf16.local")
    assert profile.endpoint == FIXED_OLLAMA_ENDPOINT
    assert profile.model_id == "qwen3.8:27b-bf16"
    assert profile.model_digest == (
        "sha256:1aa85dae8b2db4a2721ec3501ecbafc54f89f89e33ecb27f719becea4bd2fd01"
    )
    assert profile.server_version == "0.32.13"
    assert profile.embedding_length == 5120
    assert profile.license_id == "Apache-2.0"
    assert profile.raw_media_capability is False
    assert replace(
        profile,
        context_length=16_777_216,
        embedding_length=1_048_576,
    )
    for field_name, invalid in (
        ("context_length", 16_777_217),
        ("context_length", True),
        ("context_length", -1),
        ("embedding_length", 1_048_577),
        ("embedding_length", True),
        ("embedding_length", -1),
    ):
        with pytest.raises(SemanticProposalProducerError, match=f"catalog_{field_name}"):
            if field_name == "context_length":
                replace(profile, context_length=invalid)
            else:
                replace(profile, embedding_length=invalid)
    for cloud_id in ("glm-4.7:cloud", "gpt-oss:120b-cloud"):
        with pytest.raises(SemanticProposalProducerError, match="catalog_cloud_model"):
            replace(profile, model_id=cloud_id)


@pytest.mark.parametrize(
    "value",
    (
        "sha256:" + "a" * 64,
        "A" * 64,
        " " + "a" * 64,
        "a" * 64 + " ",
        "a" * 63,
        "a" * 65,
    ),
)
def test_native_digest_rejects_noncanonical_spelling(value: str) -> None:
    with pytest.raises(SemanticProposalProducerError, match="native_digest"):
        parse_ollama_native_digest(value)


def test_native_digest_converts_to_internal_domain_exactly_once() -> None:
    native = parse_ollama_native_digest("a" * 64)
    assert ollama_native_digest_to_model_digest(native) == "sha256:" + "a" * 64
    with pytest.raises(SemanticProposalProducerError, match="native_digest"):
        parse_ollama_native_digest("sha256:" + "a" * 64)


def test_source_bundle_requires_exact_report_wiring_and_explicit_targets() -> None:
    profile = load_semantic_provider_catalog().require("ollama.qwen3_8.27b_bf16.local")
    report, wiring = _authoritative_source()

    bundle = build_semantic_source_bundle(report, wiring, profile)

    assert bundle.workspace.revision == 1
    assert bundle.workspace.segments[0].segment_id.startswith("context_")
    assert bundle.baseline_plan is report.plan
    assert bundle.planning_request.timeline_plan.task_mode is TaskMode.T2VA
    assert bundle.planning_request.profile.model_digest == profile.model_digest
    assert bundle.planning_request.media_fingerprints == ()
    assert tuple(item.target_id for item in bundle.reduction_items) == ("scene_1",)

    with pytest.raises(SemanticProposalProducerError, match="wiring_authority"):
        build_semantic_source_bundle(report, replace(wiring), profile)

    skeleton_report, skeleton_wiring = _authoritative_source(explicit_scene=False)
    with pytest.raises(SemanticProposalProducerError, match="semantic_targets_missing"):
        build_semantic_source_bundle(skeleton_report, skeleton_wiring, profile)


def test_odd_frame_fl2va_keeps_half_frame_intent_cut_out_of_feasible_projection() -> None:
    from comfyui_h3_context.core import AssetRole, MediaKind, RawContextRequest, ReferenceAsset
    from comfyui_h3_context.core.registry import build_reference_registry

    profile = load_semantic_provider_catalog().require("ollama.qwen3_8.27b_bf16.local")
    registry = build_reference_registry(
        (
            ReferenceAsset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),
            ReferenceAsset("last", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2),
        )
    )
    raw = RawContextRequest(
        mode=TaskMode.FL2VA,
        user_intent="Preserve the exact odd-frame intent.",
        duration_seconds=5.875,
        reference_registry=registry,
    )
    duration = TimePoint.from_text("5.875")
    midpoint = TimePoint.from_text("2.9375")
    graph_result = build_intent_graph(
        effective_duration=duration,
        registry=registry,
        scenes=(
            IntentScene("scene_first", "The first-frame composition remains grounded."),
            IntentScene("scene_last", "The last-frame composition remains grounded."),
        ),
        segments=(
            TimelineSegment(
                "segment_first",
                TimePoint.from_text("0"),
                midpoint,
                scene_id="scene_first",
            ),
            TimelineSegment(
                "segment_last",
                midpoint,
                duration,
                scene_id="scene_last",
            ),
        ),
    )
    assert graph_result.graph is not None
    plan = H3ContextPlanNode().build_plan(raw, intent_graph=graph_result.graph)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    wiring = H3ContextNativeH3AdapterNode().adapt(report)[1]

    bundle = build_semantic_source_bundle(report, wiring, profile)

    assert report.request.effective_frame_count == 141
    assert report.plan.intent_graph.segments[0].end.seconds * Decimal(24) == Decimal("70.5")
    assert bundle.baseline_plan.intent_graph is report.plan.intent_graph
    assert len(bundle.planning_request.timeline_plan.shots) == 1
    shot = bundle.planning_request.timeline_plan.shots[0]
    assert shot.start == TimePoint.from_text("0")
    assert shot.end == report.plan.intent_graph.effective_duration
    assert tuple(
        cast(Any, anchor.kind).value for anchor in bundle.planning_request.timeline_plan.anchors
    ) == (
        "first_frame",
        "last_frame",
    )


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class _RedirectSocket:
    def __init__(self, target: tuple[str, int], original: socket.socket) -> None:
        self._target = target
        self._socket = original

    def connect_ex(self, _: tuple[str, int]) -> int:
        return self._socket.connect_ex(self._target)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._socket, name)


class _SemanticRawServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def _semantic_raw_server(
    monkeypatch: pytest.MonkeyPatch,
    responses: Callable[[str], bytes],
) -> Iterator[list[bytes]]:
    requests: list[bytes] = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            received = bytearray()
            self.request.settimeout(1.0)
            while b"\r\n\r\n" not in received:
                block = self.request.recv(4096)
                if not block:
                    return
                received.extend(block)
            header, body = bytes(received).split(b"\r\n\r\n", 1)
            content_length = 0
            for line in header.split(b"\r\n")[1:]:
                if line.startswith(b"Content-Length: "):
                    content_length = int(line.split(b": ", 1)[1])
            while len(body) < content_length:
                body += self.request.recv(content_length - len(body))
            request = header + b"\r\n\r\n" + body
            requests.append(request)
            path = header.split(b"\r\n", 1)[0].split(b" ")[1].decode("ascii")
            self.request.sendall(responses(path))

    server = _SemanticRawServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_socket = socket.socket

    def redirected(*args: Any, **kwargs: Any) -> _RedirectSocket:
        target = cast(tuple[str, int], server.server_address)
        return _RedirectSocket(target, original_socket(*args, **kwargs))

    monkeypatch.setattr(socket, "socket", redirected)
    try:
        yield requests
    finally:
        monkeypatch.setattr(socket, "socket", original_socket)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


class _SemanticTransport:
    def __init__(
        self,
        bundle: SemanticSourceBundle,
        *,
        profile: SemanticProviderProfile | None = None,
        clock: _Clock | None = None,
        unknown_chat_member: bool = False,
        cleanup_resident_polls: int = 0,
        cleanup_poll_duration: float = 0.0,
        advance_request_clock: bool = True,
        typed_intents: bool = False,
    ) -> None:
        self.endpoint_fingerprint = LoopbackOllamaTransport().endpoint_fingerprint
        self.calls: list[tuple[str, str, Any, float]] = []
        self.clock = clock
        selected = bundle.profile if profile is None else profile
        assert isinstance(selected, SemanticProviderProfile)
        self.profile = selected
        self.chat_text = _semantic_document(bundle, typed_intents=typed_intents)
        self.unknown_chat_member = unknown_chat_member
        self.cleanup_resident_polls = cleanup_resident_polls
        self.cleanup_poll_duration = cleanup_poll_duration
        self.advance_request_clock = advance_request_clock
        self.ps_count = 0
        self.unload_called = False

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
        self.calls.append((method, path, payload, phase_timeout_seconds))
        if path == "/api/ps" and self.unload_called and self.cleanup_poll_duration > 0:
            if self.clock is None:
                raise AssertionError("cleanup_poll_duration requires an injected clock")
            duration = self.cleanup_poll_duration
            self.cleanup_poll_duration = 0.0
            self.clock.value += duration
            if duration >= phase_timeout_seconds:
                raise LocalAdapterTimeoutError("fixture_timeout")
        if self.clock is not None and self.advance_request_clock:
            cleanup_call = path == "/api/generate" or (path == "/api/ps" and self.unload_called)
            self.clock.value += 1.0 if cleanup_call else 19.0
        profile = self.profile
        if path == "/api/version":
            self.ps_count = 0
            self.unload_called = False
            return {"version": profile.server_version}
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": profile.model_id,
                        "model": profile.model_id,
                        "modified_at": "2026-08-16T00:00:00Z",
                        "size": profile.model_size_bytes,
                        "digest": profile.model_digest.removeprefix("sha256:"),
                        "details": _ollama_details(profile),
                        "capabilities": ["completion", "vision", "tools", "thinking"],
                    }
                ]
            }
        if path == "/api/show":
            return {
                "details": _ollama_details(profile),
                "capabilities": ["completion", "vision", "tools", "thinking"],
                "license": _FIXTURE_LICENSE,
                "model_info": {f"{profile.model_family}.context_length": profile.context_length},
            }
        if path == "/api/chat":
            result: dict[str, object] = {
                "model": profile.model_id,
                "created_at": "2026-08-16T00:00:00Z",
                "message": {"role": "assistant", "content": self.chat_text},
                "done": True,
                "done_reason": "stop",
            }
            if self.unknown_chat_member:
                result["unknown"] = True
            return result
        if path == "/api/generate":
            self.unload_called = True
            return {
                "model": profile.model_id,
                "created_at": "2026-08-16T00:00:00Z",
                "response": "",
                "done": True,
                "done_reason": "unload",
            }
        if path == "/api/ps":
            self.ps_count += 1
            cleanup_absent = self.unload_called and self.cleanup_resident_polls == 0
            if self.ps_count == 1 or cleanup_absent:
                return {"models": []}
            if self.unload_called and self.cleanup_resident_polls > 0:
                self.cleanup_resident_polls -= 1
            return {
                "models": [
                    {
                        "name": profile.model_id,
                        "model": profile.model_id,
                        # /api/ps size is loaded-memory size, not /api/tags package size.
                        "size": profile.model_size_bytes + 4096,
                        "digest": profile.model_digest.removeprefix("sha256:"),
                        "details": _ollama_details(profile),
                        "expires_at": "2026-08-16T00:00:30Z",
                        "size_vram": profile.model_size_bytes,
                        "context_length": profile.context_length,
                    }
                ]
            }
        raise AssertionError(path)


class _MutatingSemanticTransport(_SemanticTransport):
    def __init__(
        self,
        bundle: SemanticSourceBundle,
        mutate: Any,
        *,
        profile: SemanticProviderProfile | None = None,
    ) -> None:
        super().__init__(bundle, profile=profile)
        self._mutate = mutate

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
        result = super().request(
            method,
            path,
            payload,
            cancellation=cancellation,
            absolute_deadline=absolute_deadline,
            phase_timeout_seconds=phase_timeout_seconds,
        )
        return cast(Mapping[str, object], self._mutate(path, result, self.ps_count))


def _ollama_details(profile: SemanticProviderProfile) -> dict[str, object]:
    return {
        "parent_model": "",
        "format": profile.model_format,
        "family": profile.model_family,
        "families": [profile.model_family],
        "parameter_size": profile.parameter_size,
        "quantization_level": profile.quantization_level,
        "context_length": profile.context_length,
        "embedding_length": profile.embedding_length,
    }


_FIXTURE_LICENSE = "Apache-2.0 hermetic qualification fixture"


def _fixture_profile(bundle: SemanticSourceBundle) -> SemanticProviderProfile:
    profile = bundle.profile
    assert isinstance(profile, SemanticProviderProfile)
    license_hash = "sha256:" + hashlib.sha256(_FIXTURE_LICENSE.encode()).hexdigest()
    identity = canonical_fingerprint(
        {
            "model_id": profile.model_id,
            "details": {
                "family": profile.model_family,
                "format": profile.model_format,
                "parameter_size": profile.parameter_size,
                "quantization_level": profile.quantization_level,
            },
            "capabilities": ["completion", "thinking", "tools", "vision"],
            "license_text_sha256": license_hash,
        }
    )
    return replace(
        profile,
        license_text_sha256=license_hash,
        show_identity_sha256=identity,
    )


def _semantic_document(bundle: SemanticSourceBundle, *, typed_intents: bool = False) -> str:
    request = bundle.planning_request
    payload = {
        "schema": "h3.constrained_semantic_planning.v1",
        "document_id": "proposal.document",
        "policy": "evidence_bounded",
        "task_mode": request.timeline_plan.task_mode.value,
        "effective_duration": request.timeline_plan.effective_duration.raw,
        "asset_ids": list(request.timeline_plan.reference_order),
        "reference_order": list(request.timeline_plan.reference_order),
        "timeline_fingerprint": request.timeline_plan.fingerprint,
        "reduction_fingerprint": request.reduction_plan.fingerprint,
        "preserved_exact_text": [item.to_wire() for item in request.protected_exact_text],
        "proposals": [
            {
                "schema": "h3.constrained_semantic_planning.v1",
                "proposal_id": "proposal.scene",
                "target_kind": "scene",
                "target_id": "scene_1",
                "claim": "Soft daylight clarifies the quiet studio.",
                "evidence_label": "source",
                "source_ids": [bundle.workspace.segments[0].segment_id],
                "confidence": "0.90",
                "rationale": "source-grounded reduction evidence",
            }
        ],
        "complete": True,
    }
    if typed_intents:
        payload["schema"] = "h3.constrained_semantic_planning.v2"
        for proposal in cast(list[dict[str, Any]], payload["proposals"]):
            proposal["schema"] = payload["schema"]
            proposal["intent"] = {
                "kind": proposal["target_kind"],
                "description": proposal.pop("claim"),
                "dialogue_bindings": [],
            }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _semantic_http_responses(
    bundle: SemanticSourceBundle,
    profile: SemanticProviderProfile,
    *,
    malformed_chat: bool = False,
) -> Callable[[str], bytes]:
    ps_calls = 0

    def response(path: str) -> bytes:
        nonlocal ps_calls
        if path == "/api/version":
            value: object = {"version": profile.server_version}
        elif path == "/api/tags":
            value = {
                "models": [
                    {
                        "name": profile.model_id,
                        "model": profile.model_id,
                        "modified_at": "2026-08-16T00:00:00Z",
                        "size": profile.model_size_bytes,
                        "digest": profile.model_digest.removeprefix("sha256:"),
                        "details": _ollama_details(profile),
                        "capabilities": ["completion", "vision", "tools", "thinking"],
                    }
                ]
            }
        elif path == "/api/show":
            value = {
                "details": _ollama_details(profile),
                "capabilities": ["completion", "vision", "tools", "thinking"],
                "license": _FIXTURE_LICENSE,
                "model_info": {f"{profile.model_family}.context_length": profile.context_length},
            }
        elif path == "/api/chat":
            if malformed_chat:
                return (
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 1\r\n\r\n{"
                )
            value = {
                "model": profile.model_id,
                "created_at": "2026-08-16T00:00:00Z",
                "message": {"role": "assistant", "content": _semantic_document(bundle)},
                "done": True,
                "done_reason": "stop",
            }
        elif path == "/api/generate":
            value = {
                "model": profile.model_id,
                "created_at": "2026-08-16T00:00:00Z",
                "response": "",
                "done": True,
                "done_reason": "unload",
            }
        elif path == "/api/ps":
            ps_calls += 1
            value = (
                {"models": []}
                if ps_calls in {1, 3}
                else {
                    "models": [
                        {
                            "name": profile.model_id,
                            "model": profile.model_id,
                            "size": profile.model_size_bytes + 4096,
                            "digest": profile.model_digest.removeprefix("sha256:"),
                            "details": _ollama_details(profile),
                            "expires_at": "2026-08-16T00:00:30Z",
                            "size_vram": profile.model_size_bytes,
                            "context_length": profile.context_length,
                        }
                    ]
                }
            )
        else:  # pragma: no cover - fixture misuse guard
            raise AssertionError(path)
        body = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\n\r\n"
            + body
        )

    return response


def _provider_setup() -> ProviderSetup:
    return build_provider_setup(
        ProviderExecutionPolicy(
            provider=ProviderIdentity.LOCAL,
            privacy_mode=ProviderPrivacyMode.LOCAL_ONLY,
            offline=False,
            network_allowed=True,
            upload_consent=False,
        ),
        local_backend=ProviderLocalBackend.OLLAMA,
    )


def _provider_bundle() -> SemanticSourceBundle:
    profile = load_semantic_provider_catalog().require("ollama.qwen3_8.27b_bf16.local")
    report, wiring = _authoritative_source()
    return build_semantic_source_bundle(report, wiring, profile)


def test_m17_request_bound_schema_is_private_closed_and_generic_compatible() -> None:
    bundle = _provider_bundle()
    generic = build_semantic_generation_request(bundle.planning_request)
    generic_schema = semantic_proposal_json_schema()
    generic_properties = cast(dict[str, Any], generic_schema["properties"])
    assert generic.structured_schema == generic_schema
    assert "items" not in generic_properties["proposals"]
    assert "minItems" not in generic_properties["proposals"]

    generic_fixture = json.loads(_semantic_document(bundle))
    generic_fixture["proposals"] = [{"missing": "generic hint admits this"}]
    assert list(Draft202012Validator(generic_schema).iter_errors(generic_fixture)) == []

    exact = _build_semantic_ollama_generation_request(bundle)
    assert exact.prompt == generic.prompt
    assert exact.to_public_dict() == generic.to_public_dict()
    schema = exact.structured_schema
    assert isinstance(schema, Mapping)
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    properties = cast(dict[str, Any], schema["properties"])
    required = cast(list[str], schema["required"])
    assert set(required) == set(properties)
    request = bundle.planning_request
    assert properties["policy"] == {"const": "evidence_bounded"}
    assert properties["task_mode"] == {"const": request.timeline_plan.task_mode.value}
    assert properties["effective_duration"] == {
        "const": request.timeline_plan.effective_duration.raw
    }
    assert properties["asset_ids"] == {"const": list(request.timeline_plan.reference_order)}
    assert properties["reference_order"] == {"const": list(request.timeline_plan.reference_order)}
    assert properties["timeline_fingerprint"] == {"const": request.timeline_plan.fingerprint}
    assert properties["reduction_fingerprint"] == {"const": request.reduction_plan.fingerprint}
    assert properties["preserved_exact_text"] == {
        "const": [item.to_wire() for item in request.protected_exact_text]
    }
    assert properties["complete"] == {"const": True}

    proposals = cast(dict[str, Any], properties["proposals"])
    assert proposals["minItems"] == 1
    assert proposals["maxItems"] == request.budget.max_proposals
    item_schema = cast(dict[str, Any], proposals["items"])
    expected_fields = {
        "schema",
        "proposal_id",
        "target_kind",
        "target_id",
        "claim",
        "evidence_label",
        "source_ids",
        "confidence",
        "rationale",
    }
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) == expected_fields
    assert set(item_schema["properties"]) == expected_fields
    assert item_schema["properties"]["source_ids"] == {
        "type": "array",
        "items": {"enum": sorted(request.allowed_source_ids)},
        "minItems": 1,
        "maxItems": 64,
        "uniqueItems": True,
    }
    assert item_schema["properties"]["confidence"] == {
        "type": "string",
        "pattern": r"^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$",
    }
    expected_pairs = {
        (target.target_kind, target.target_id)
        for target in bundle.reduction_items
        if target.target_id not in request.protected_target_ids
    }
    actual_pairs = {
        (
            choice["properties"]["target_kind"]["const"],
            choice["properties"]["target_id"]["const"],
        )
        for choice in item_schema["allOf"][0]["anyOf"]
    }
    assert actual_pairs == expected_pairs

    validator = Draft202012Validator(schema)
    valid = json.loads(_semantic_document(bundle))
    assert list(validator.iter_errors(valid)) == []
    mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
        lambda value: value["proposals"][0].pop("rationale"),
        lambda value: value["proposals"][0].update({"unknown": True}),
        lambda value: value["proposals"][0].update({"confidence": 0.9}),
        lambda value: value.update({"proposals": []}),
    )
    for mutate in mutations:
        invalid = json.loads(_semantic_document(bundle))
        mutate(invalid)
        assert list(validator.iter_errors(invalid))


def test_semantic_provider_lifecycle_is_exact_and_prompt_free_cleanup() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    transport = _SemanticTransport(bundle, profile=profile)
    generation = build_semantic_generation_request(bundle.planning_request)

    result = execute_semantic_ollama(transport, _provider_setup(), profile, generation)

    assert cast(Any, result.model_result).model_id == profile.model_id
    assert "cloud" not in result.provider_preflight.to_public_dict()
    assert [call[:2] for call in transport.calls] == [
        ("GET", "/api/version"),
        ("GET", "/api/tags"),
        ("POST", "/api/show"),
        ("GET", "/api/ps"),
        ("POST", "/api/chat"),
        ("GET", "/api/ps"),
        ("POST", "/api/generate"),
        ("GET", "/api/ps"),
    ]
    assert transport.calls[2][2] == {"model": profile.model_id}
    assert transport.calls[4][2] == ollama_native._semantic_chat_payload(profile, generation)
    assert transport.calls[4][2]["keep_alive"] == "30s"
    assert transport.calls[4][2]["think"] is False
    assert transport.calls[6][2] == {"model": bundle.profile.model_id, "keep_alive": 0}
    path_limits = {
        "/api/version": 30.0,
        "/api/tags": 30.0,
        "/api/show": 30.0,
        "/api/ps": 30.0,
        "/api/chat": 120.0,
        "/api/generate": 5.0,
    }
    assert all(0 < call[3] <= path_limits[call[1]] for call in transport.calls)


def test_no_module_reachable_real_execution_bypass_or_competing_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    setup = _provider_setup()
    transport = ollama_native.SemanticDeadlineOllamaTransport(profile.model_id)
    calls: list[str] = []

    def forbidden_request(*_: object, **__: object) -> Mapping[str, object]:
        calls.append("request")
        raise AssertionError("rejected competing execution contacted transport")

    monkeypatch.setattr(ollama_native.SemanticDeadlineOllamaTransport, "request", forbidden_request)
    assert not hasattr(ollama_native, "_execute_semantic_ollama")
    test_only = ollama_native._execute_semantic_ollama_test_only
    assert ollama_native._SEMANTIC_EXECUTION_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(SemanticOllamaExecutionError, match="test_transport_invalid"):
            test_only(
                transport,
                setup,
                profile,
                generation,
                cancellation=None,
                clock=time.monotonic,
            )
        with pytest.raises(SemanticOllamaExecutionError, match="provider_busy"):
            execute_semantic_ollama(transport, setup, profile, generation)
    finally:
        ollama_native._SEMANTIC_EXECUTION_LOCK.release()
    assert calls == []


def test_derived_real_transport_is_rejected_by_test_seam_before_contact_or_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    transport = _DerivedSemanticDeadlineOllamaTransport(profile.model_id)
    calls: list[str] = []

    def forbidden_request(*_: object, **__: object) -> Mapping[str, object]:
        calls.append("request")
        raise AssertionError("derived real transport contacted provider")

    monkeypatch.setattr(ollama_native.SemanticDeadlineOllamaTransport, "request", forbidden_request)
    with pytest.raises(SemanticOllamaExecutionError, match="test_transport_invalid"):
        ollama_native._execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            profile,
            generation,
            cancellation=None,
            clock=time.monotonic,
        )
    assert calls == []


def test_derived_real_transport_obeys_public_capacity_before_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    transport = _DerivedSemanticDeadlineOllamaTransport(profile.model_id)
    calls: list[str] = []

    def forbidden_request(*_: object, **__: object) -> Mapping[str, object]:
        calls.append("request")
        raise AssertionError("capacity-rejected derived transport contacted provider")

    monkeypatch.setattr(ollama_native.SemanticDeadlineOllamaTransport, "request", forbidden_request)
    monkeypatch.setattr(
        ollama_native,
        "_SEMANTIC_EXECUTION_AUTHORITIES",
        {index: object() for index in range(ollama_native._MAX_SEMANTIC_EXECUTION_AUTHORITIES)},
    )
    with pytest.raises(SemanticOllamaExecutionError, match="execution_authority_capacity"):
        execute_semantic_ollama(transport, _provider_setup(), profile, generation)
    assert calls == []


def test_derived_real_transport_uses_public_real_chat_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    transport = _DerivedSemanticDeadlineOllamaTransport(profile.model_id)

    with _semantic_raw_server(
        monkeypatch,
        _semantic_http_responses(bundle, profile),
    ) as requests:
        execution = execute_semantic_ollama(
            transport,
            _provider_setup(),
            profile,
            generation,
        )
        assert consume_semantic_ollama_execution_authority(execution) is execution
        assert any(item.startswith(b"POST /api/chat ") for item in requests)


def test_execution_authority_capacity_has_no_real_capable_private_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    setup = _provider_setup()
    transport = ollama_native.SemanticDeadlineOllamaTransport(profile.model_id)
    calls: list[str] = []

    def forbidden_request(*_: object, **__: object) -> Mapping[str, object]:
        calls.append("request")
        raise AssertionError("capacity-rejected execution contacted transport")

    monkeypatch.setattr(ollama_native.SemanticDeadlineOllamaTransport, "request", forbidden_request)
    monkeypatch.setattr(
        ollama_native,
        "_SEMANTIC_EXECUTION_AUTHORITIES",
        {index: object() for index in range(ollama_native._MAX_SEMANTIC_EXECUTION_AUTHORITIES)},
    )
    with pytest.raises(SemanticOllamaExecutionError, match="execution_authority_capacity"):
        execute_semantic_ollama(transport, setup, profile, generation)
    with pytest.raises(SemanticOllamaExecutionError, match="test_transport_invalid"):
        ollama_native._execute_semantic_ollama_test_only(
            transport,
            setup,
            profile,
            generation,
            cancellation=None,
            clock=time.monotonic,
        )
    assert calls == []


def test_qualified_real_chat_lease_is_single_use_and_replay_is_preconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    transport = ollama_native.SemanticDeadlineOllamaTransport(profile.model_id)

    with _semantic_raw_server(
        monkeypatch,
        _semantic_http_responses(bundle, profile),
    ) as requests:
        execution = execute_semantic_ollama(transport, _provider_setup(), profile, generation)
        assert consume_semantic_ollama_execution_authority(execution) is execution
        chat_request = next(item for item in requests if item.startswith(b"POST /api/chat "))
        chat_payload = json.loads(chat_request.split(b"\r\n\r\n", 1)[1])
        assert chat_payload == ollama_native._semantic_chat_payload(profile, generation)
        contacted = len(requests)
        with pytest.raises(ModelTransportError, match="semantic_http_request"):
            transport.request(
                "POST",
                "/api/chat",
                chat_payload,
                absolute_deadline=time.monotonic() + 1.0,
                phase_timeout_seconds=1.0,
            )
        assert len(requests) == contacted


def test_real_chat_lease_is_cleared_after_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    transport = ollama_native.SemanticDeadlineOllamaTransport(profile.model_id)

    with _semantic_raw_server(
        monkeypatch,
        _semantic_http_responses(bundle, profile, malformed_chat=True),
    ):
        with pytest.raises(SemanticOllamaExecutionError, match="transport"):
            execute_semantic_ollama(transport, _provider_setup(), profile, generation)

    with _semantic_raw_server(
        monkeypatch,
        _semantic_http_responses(bundle, profile),
    ) as requests:
        execution = execute_semantic_ollama(transport, _provider_setup(), profile, generation)
        assert any(item.startswith(b"POST /api/chat ") for item in requests)
        assert consume_semantic_ollama_execution_authority(execution) is execution


def test_semantic_provider_execution_authority_rejects_copy_before_exact_consumption() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    transport = _SemanticTransport(bundle, profile=profile)
    execution = execute_semantic_ollama(
        transport,
        _provider_setup(),
        profile,
        build_semantic_generation_request(bundle.planning_request),
    )

    with pytest.raises(SemanticOllamaExecutionError, match="execution_authority"):
        consume_semantic_ollama_execution_authority(replace(execution))
    assert consume_semantic_ollama_execution_authority(execution) is execution
    with pytest.raises(SemanticOllamaExecutionError, match="execution_authority"):
        consume_semantic_ollama_execution_authority(execution)


def _equal_distinct_referent(value: object) -> object | None:
    candidate: object
    if type(value) is str:
        candidate = ("_" + value)[1:]
    elif type(value) is tuple and value:
        candidate = tuple([*value])
    elif type(value) is dict:
        candidate = dict(value)
    elif type(value) is int and not -5 <= value <= 256:
        candidate = int(str(value))
    elif type(value) is float:
        candidate = float(str(value))
    else:
        return None
    assert candidate == value
    assert candidate is not value
    return candidate


@pytest.mark.parametrize(
    "cohort",
    ("execution", "model_result", "provider_preflight"),
)
def test_execution_authority_rejects_every_equal_distinct_field_referent(
    cohort: str,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    field_names: set[str] | None = None
    exercised: set[str] = set()

    while field_names is None or exercised != field_names:
        execution = execute_semantic_ollama(
            _SemanticTransport(bundle, profile=profile),
            _provider_setup(),
            profile,
            build_semantic_generation_request(bundle.planning_request),
        )
        target: Any = (
            execution
            if cohort == "execution"
            else execution.model_result
            if cohort == "model_result"
            else execution.provider_preflight
        )
        field_names = {field.name for field in dataclass_fields(target)}
        candidate = next(
            (
                (field.name, replacement)
                for field in dataclass_fields(target)
                if field.name not in exercised
                and (replacement := _equal_distinct_referent(getattr(target, field.name)))
                is not None
            ),
            None,
        )
        if candidate is None:
            assert exercised == {
                field.name
                for field in dataclass_fields(target)
                if _equal_distinct_referent(getattr(target, field.name)) is not None
            }
            consume_semantic_ollama_execution_authority(execution)
            break
        field_name, replacement = candidate
        exercised.add(field_name)
        object.__setattr__(target, field_name, replacement)
        with pytest.raises(SemanticOllamaExecutionError, match="execution_authority"):
            consume_semantic_ollama_execution_authority(execution)


@pytest.mark.parametrize("cohort", ("model_result", "provider_preflight", "parsed_output"))
def test_execution_authority_rejects_equal_value_private_object_replacement(cohort: str) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    execution = execute_semantic_ollama(
        _SemanticTransport(bundle, profile=profile),
        _provider_setup(),
        profile,
        build_semantic_generation_request(bundle.planning_request),
    )
    if cohort == "model_result":
        object.__setattr__(
            execution,
            "model_result",
            replace(cast(Any, execution.model_result)),
        )
    elif cohort == "provider_preflight":
        object.__setattr__(
            execution,
            "provider_preflight",
            replace(execution.provider_preflight),
        )
    else:
        object.__setattr__(
            execution.model_result,
            "parsed_output",
            dict(cast(Any, execution.model_result).parsed_output),
        )

    with pytest.raises(SemanticOllamaExecutionError, match="execution_authority"):
        consume_semantic_ollama_execution_authority(execution)


def test_execution_authority_normalizes_current_content_inspection_failure() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    execution = execute_semantic_ollama(
        _SemanticTransport(bundle, profile=profile),
        _provider_setup(),
        profile,
        build_semantic_generation_request(bundle.planning_request),
    )
    object.__setattr__(execution.model_result, "parsed_output", {"private": object()})

    with pytest.raises(SemanticOllamaExecutionError, match="^execution_authority$") as captured:
        consume_semantic_ollama_execution_authority(execution)
    formatted = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert captured.value.__cause__ is None
    assert "CanonicalizationError" not in formatted
    assert "parsed_output.private" not in formatted


def test_semantic_provider_shared_deadline_shrinks_and_unknown_output_closes() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    clock = _Clock()
    transport = _SemanticTransport(bundle, profile=profile, clock=clock)

    _execute_semantic_ollama_test_only(
        transport,
        _provider_setup(),
        profile,
        generation,
        cancellation=None,
        clock=clock,
    )
    request_timeouts = [call[3] for call in transport.calls[:6]]
    assert request_timeouts[:4] == [30.0, 30.0, 30.0, 30.0]
    assert request_timeouts[4] == 44.0
    assert request_timeouts[5] == 25.0
    assert request_timeouts[-1] <= request_timeouts[0]

    rejected = _SemanticTransport(bundle, profile=profile, unknown_chat_member=True)
    with pytest.raises(SemanticOllamaExecutionError, match="chat_members"):
        execute_semantic_ollama(rejected, _provider_setup(), profile, generation)
    assert [call[1] for call in rejected.calls[-2:]] == ["/api/generate", "/api/ps"]


def test_async_cleanup_uses_exact_non_busy_schedule_until_absence() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    clock = _Clock()
    transport = _SemanticTransport(
        bundle,
        profile=profile,
        clock=clock,
        cleanup_resident_polls=3,
        advance_request_clock=False,
    )

    _execute_semantic_ollama_test_only(
        transport,
        _provider_setup(),
        profile,
        generation,
        cancellation=None,
        clock=clock,
        sleeper=clock.sleep,
    )

    assert clock.sleeps == [0.5, 0.5, 0.5]
    cleanup_start = next(
        index for index, call in enumerate(transport.calls) if call[:2] == ("POST", "/api/generate")
    )
    assert [call[1] for call in transport.calls[cleanup_start:]] == [
        "/api/generate",
        "/api/ps",
        "/api/ps",
        "/api/ps",
        "/api/ps",
    ]
    assert transport.calls[cleanup_start][3] == 5.0
    assert all(0 < call[3] <= 30.0 for call in transport.calls[cleanup_start + 1 :])


def test_async_cleanup_residency_waits_full_deadline_without_busy_polling() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    clock = _Clock()
    transport = _SemanticTransport(
        bundle,
        profile=profile,
        clock=clock,
        cleanup_resident_polls=-1,
        advance_request_clock=False,
    )

    with pytest.raises(SemanticOllamaExecutionError, match="cleanup_failed"):
        _execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            profile,
            generation,
            cancellation=None,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert sum(clock.sleeps) == 30.0
    assert set(clock.sleeps) == {0.5}
    cleanup_start = next(
        index for index, call in enumerate(transport.calls) if call[:2] == ("POST", "/api/generate")
    )
    cleanup_ps = [call for call in transport.calls[cleanup_start:] if call[1] == "/api/ps"]
    assert len(cleanup_ps) == 60
    assert cleanup_ps[0][3] == 30.0
    assert cleanup_ps[-1][3] == 0.5


def test_cleanup_ps_response_after_five_seconds_uses_shared_deadline() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    clock = _Clock()
    transport = _SemanticTransport(
        bundle,
        profile=profile,
        clock=clock,
        cleanup_poll_duration=6.0,
        advance_request_clock=False,
    )

    _execute_semantic_ollama_test_only(
        transport,
        _provider_setup(),
        profile,
        generation,
        cancellation=None,
        clock=clock,
        sleeper=clock.sleep,
    )

    cleanup_ps = [
        call for call in transport.calls if call[1] == "/api/ps" and transport.unload_called
    ]
    assert cleanup_ps[-1][3] == 30.0


@pytest.mark.parametrize("cleanup_poll_duration", (30.0, 30.001))
def test_cleanup_ps_at_or_beyond_shared_deadline_fails_closed(
    cleanup_poll_duration: float,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    clock = _Clock()
    transport = _SemanticTransport(
        bundle,
        profile=profile,
        clock=clock,
        cleanup_poll_duration=cleanup_poll_duration,
        advance_request_clock=False,
    )

    with pytest.raises(SemanticOllamaExecutionError, match="cleanup_failed"):
        _execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            profile,
            generation,
            cancellation=None,
            clock=clock,
            sleeper=clock.sleep,
        )

    cleanup_ps = [call for call in transport.calls if call[1] == "/api/ps"]
    assert cleanup_ps[-1][3] == 30.0


@pytest.mark.parametrize(
    ("expected_code", "mutate"),
    (
        (
            "native_digest_invalid",
            lambda path, value, _: (
                {
                    "models": [
                        {
                            **value["models"][0],
                            "digest": "sha256:" + value["models"][0]["digest"],
                        }
                    ]
                }
                if path == "/api/tags"
                else value
            ),
        ),
        (
            "model_identity_conflict",
            lambda path, value, _: (
                {"models": [value["models"][0], dict(value["models"][0])]}
                if path == "/api/tags"
                else value
            ),
        ),
        (
            "tags_model_members",
            lambda path, value, _: (
                {
                    "models": [
                        {key: item for key, item in value["models"][0].items() if key != "digest"}
                    ]
                }
                if path == "/api/tags"
                else value
            ),
        ),
        (
            "details_members",
            lambda path, value, _: (
                {
                    "models": [
                        {
                            **value["models"][0],
                            "details": {**value["models"][0]["details"], "unknown": 1},
                        }
                    ]
                }
                if path == "/api/tags"
                else value
            ),
        ),
        (
            "details_value",
            lambda path, value, _: (
                {
                    "models": [
                        {
                            **value["models"][0],
                            "details": {**value["models"][0]["details"], "context_length": 0},
                        }
                    ]
                }
                if path == "/api/tags"
                else value
            ),
        ),
        (
            "details_mismatch",
            lambda path, value, _: (
                {
                    "models": [
                        {
                            **value["models"][0],
                            "details": {
                                **value["models"][0]["details"],
                                "embedding_length": value["models"][0]["details"][
                                    "embedding_length"
                                ]
                                + 1,
                            },
                        }
                    ]
                }
                if path == "/api/tags"
                else value
            ),
        ),
        (
            "details_mismatch",
            lambda path, value, _: (
                {
                    **value,
                    "details": {
                        **value["details"],
                        "context_length": value["details"]["context_length"] + 1,
                    },
                }
                if path == "/api/show"
                else value
            ),
        ),
        (
            "details_mismatch",
            lambda path, value, ps_count: (
                {
                    "models": [
                        {
                            **value["models"][0],
                            "details": {
                                **value["models"][0]["details"],
                                "embedding_length": value["models"][0]["details"][
                                    "embedding_length"
                                ]
                                + 1,
                            },
                        }
                    ]
                }
                if path == "/api/ps" and ps_count == 2
                else value
            ),
        ),
        (
            "process_resource_invalid",
            lambda path, value, ps_count: (
                {
                    "models": [
                        {
                            **value["models"][0],
                            "size": True,
                        }
                    ]
                }
                if path == "/api/ps" and ps_count == 2
                else value
            ),
        ),
        (
            "capability_mismatch",
            lambda path, value, _: (
                {**value, "capabilities": ["vision", "tools", "thinking"]}
                if path == "/api/show"
                else value
            ),
        ),
        (
            "postflight_process_missing",
            lambda path, value, ps_count: (
                {"models": []} if path == "/api/ps" and ps_count == 2 else value
            ),
        ),
        (
            "cleanup_failed",
            lambda path, value, _: {**value, "done": False} if path == "/api/generate" else value,
        ),
    ),
)
def test_semantic_provider_adversarial_identity_and_cleanup_fail_closed(
    expected_code: str,
    mutate: Any,
) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    transport = _MutatingSemanticTransport(bundle, mutate, profile=profile)

    with pytest.raises(SemanticOllamaExecutionError, match=expected_code):
        execute_semantic_ollama(
            transport,
            _provider_setup(),
            profile,
            build_semantic_generation_request(bundle.planning_request),
        )

    if any(call[1] == "/api/chat" for call in transport.calls):
        assert any(call[1] == "/api/generate" for call in transport.calls)


def test_semantic_provider_rejects_invalid_cancellation_before_contact() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    transport = _SemanticTransport(bundle, profile=profile)

    with pytest.raises(SemanticOllamaExecutionError, match="cancellation_invalid"):
        execute_semantic_ollama(
            transport,
            _provider_setup(),
            profile,
            build_semantic_generation_request(bundle.planning_request),
            cancellation=cast(Any, object()),
        )

    assert transport.calls == []


def _qualified_product() -> tuple[SemanticSourceBundle, SemanticProposalProduct]:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    transport = _SemanticTransport(bundle, profile=profile)
    execution = execute_semantic_ollama(
        transport,
        _provider_setup(),
        profile,
        build_semantic_generation_request(bundle.planning_request),
    )
    exact = consume_semantic_ollama_execution_authority(execution)
    product = _assemble_semantic_proposal_product(
        bundle,
        exact.model_result,
        exact.capability_fingerprint,
    )
    return bundle, product


def test_qualified_output_closes_into_exact_m17_05_transaction() -> None:
    bundle, product = _qualified_product()

    assert product.planning_result.is_valid
    assert product.enrichment_result.status.value == "applied"
    assert product.transaction.state.value == "ready_for_review"
    assert product.transaction.attempt == 1
    assert product.transaction.revision == 1
    assert product.transaction.workspace_fingerprint == bundle.workspace.fingerprint
    assert product.transaction.candidate_graph != bundle.baseline_plan.intent_graph


def test_registered_node_executes_exact_route_and_retries_without_second_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_bundle = _provider_bundle()
    profile = _fixture_profile(catalog_bundle)
    source = build_semantic_source_bundle(
        catalog_bundle.report,
        catalog_bundle.wiring,
        profile,
    )
    transport = _SemanticTransport(source, profile=profile, typed_intents=True)
    monkeypatch.setattr(
        semantic_proposal_node,
        "load_semantic_provider_catalog",
        lambda: SemanticProviderCatalog("h3.semantic.provider_profiles.v1", (profile,)),
    )
    node = H3SemanticProposalProducerNode(transport=transport)

    authority = node.produce(
        source.report,
        source.wiring,
        _provider_setup(),
        profile.profile_id,
        prompt_id="prompt.node.success",
        execution_node_id="node.semantic.success",
    )[0]
    call_count = len(transport.calls)
    retry = node.produce(
        source.report,
        source.wiring,
        _provider_setup(),
        profile.profile_id,
        prompt_id="prompt.node.success",
        execution_node_id="node.semantic.success",
    )[0]

    assert type(authority) is SemanticProposalReviewAuthority
    assert retry is authority
    assert len(transport.calls) == call_count
    chat_call = next(call for call in transport.calls if call[1] == "/api/chat")
    assert (
        chat_call[2]["format"]
        == _build_semantic_ollama_generation_request(source, typed_intents=True).structured_schema
    )
    assert [call[1] for call in transport.calls[-2:]] == ["/api/generate", "/api/ps"]


def test_registered_node_closes_failed_provider_reservation_without_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_bundle = _provider_bundle()
    profile = _fixture_profile(catalog_bundle)
    source = build_semantic_source_bundle(
        catalog_bundle.report,
        catalog_bundle.wiring,
        profile,
    )
    transport = _SemanticTransport(source, profile=profile, unknown_chat_member=True)
    monkeypatch.setattr(
        semantic_proposal_node,
        "load_semantic_provider_catalog",
        lambda: SemanticProviderCatalog("h3.semantic.provider_profiles.v1", (profile,)),
    )
    node = H3SemanticProposalProducerNode(transport=transport)

    with pytest.raises(H3SemanticProposalNodeError, match="chat_members") as error:
        node.produce(
            source.report,
            source.wiring,
            _provider_setup(),
            profile.profile_id,
            prompt_id="prompt.node.failure",
            execution_node_id="node.semantic.failure",
        )

    assert error.value.code == "chat_members"
    assert "quiet studio" not in repr(error.value)
    assert [call[1] for call in transport.calls[-2:]] == ["/api/generate", "/api/ps"]


def test_review_authority_is_factory_only_exact_retry_and_one_time_claim() -> None:
    bundle, product = _qualified_product()
    setup = _provider_setup()
    owner = _SemanticProposalAuthorityOwner()
    reservation = owner.begin(bundle, setup, "prompt.1", "node.1")
    authority = owner.commit(reservation, product)

    assert type(authority) is SemanticProposalReviewAuthority
    assert "quiet studio" not in repr(authority)
    assert set(authority.to_public_dict()) == {
        "schema",
        "lineage_id",
        "expires_at_ms",
        "authority_fingerprint",
    }
    assert owner.begin(bundle, setup, "prompt.1", "node.1") is authority
    with pytest.raises(TypeError):
        SemanticProposalReviewAuthority()

    with owner.claim(authority) as claim:
        assert claim.bundle.report is bundle.report
        assert claim.bundle.workspace is bundle.workspace
        assert claim.bundle.transaction is product.transaction
        claim.abort()
    assert owner.begin(bundle, setup, "prompt.1", "node.1") is authority

    with owner.claim(authority) as claim:
        claim.commit()
    with pytest.raises(SemanticProposalProducerError, match="lineage_consumed"):
        owner.begin(bundle, setup, "prompt.1", "node.1")
    with pytest.raises(SemanticProposalProducerError, match="inactive"):
        with owner.claim(authority):
            pass


def test_review_authority_rejects_copy_mutation_conflict_expiry_and_capacity() -> None:
    bundle, product = _qualified_product()
    setup = _provider_setup()
    owner = _SemanticProposalAuthorityOwner()
    reservation = owner.begin(bundle, setup, "prompt.copy", "node.copy")
    authority = owner.commit(reservation, product)
    forged = object.__new__(SemanticProposalReviewAuthority)
    for field in ("_lineage_id", "_expires_at_ms", "_fingerprint"):
        object.__setattr__(forged, field, getattr(authority, field))
    with pytest.raises(SemanticProposalProducerError, match="review_authority"):
        with owner.claim(forged):
            pass
    object.__setattr__(authority, "_expires_at_ms", authority.expires_at_ms + 1)
    with pytest.raises(SemanticProposalProducerError, match="review_authority"):
        with owner.claim(authority):
            pass

    conflict_owner = _SemanticProposalAuthorityOwner()
    conflict_owner.begin(bundle, setup, "prompt.conflict", "node.conflict")
    with pytest.raises(SemanticProposalProducerError, match="identity_conflict"):
        conflict_owner.begin(
            bundle,
            replace(setup, revision=setup.revision + 1),
            "prompt.conflict",
            "node.conflict",
        )

    clock = _Clock()
    expiry_owner = _SemanticProposalAuthorityOwner(clock)
    expiry_reservation = expiry_owner.begin(bundle, setup, "prompt.expiry", "node.expiry")
    expiry_authority = expiry_owner.commit(expiry_reservation, product)
    clock.value += 301
    with pytest.raises(SemanticProposalProducerError, match="inactive"):
        with expiry_owner.claim(expiry_authority):
            pass
    with pytest.raises(SemanticProposalProducerError, match="lineage_expired"):
        expiry_owner.begin(bundle, setup, "prompt.expiry", "node.expiry")

    capacity_owner = _SemanticProposalAuthorityOwner()
    for index in range(64):
        reservation = capacity_owner.begin(
            bundle,
            setup,
            f"prompt.capacity.{index}",
            f"node.capacity.{index}",
        )
        capacity_owner.fail(reservation)
    with pytest.raises(SemanticProposalProducerError, match="lineage_capacity"):
        capacity_owner.begin(bundle, setup, "prompt.capacity.64", "node.capacity.64")


@pytest.mark.parametrize("field", ("text", "parsed_output", "output_fingerprint"))
def test_execution_authority_rejects_current_private_model_result_mutation(field: str) -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    execution = execute_semantic_ollama(
        _SemanticTransport(bundle, profile=profile),
        _provider_setup(),
        profile,
        build_semantic_generation_request(bundle.planning_request),
    )
    model_result = cast(Any, execution.model_result)
    if field == "text":
        mutated = model_result.text.replace("quiet studio", "noisy studio", 1)
        assert mutated != model_result.text
        assert len(mutated) == len(model_result.text)
        object.__setattr__(model_result, field, mutated)
    elif field == "parsed_output":
        object.__setattr__(
            model_result,
            field,
            {**model_result.parsed_output, "complete": False},
        )
    else:
        object.__setattr__(model_result, field, "sha256:" + ("0" * 64))

    with pytest.raises(SemanticOllamaExecutionError, match="execution_authority"):
        consume_semantic_ollama_execution_authority(execution)


class _Cancellation:
    def __init__(self) -> None:
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled


class _OutcomeTransport(_SemanticTransport):
    def __init__(
        self,
        bundle: SemanticSourceBundle,
        *,
        profile: SemanticProviderProfile,
        clock: _Clock,
        cancellation: _Cancellation,
        unknown_chat_member: bool = False,
        cleanup_failure: bool = False,
        cleanup_extra_seconds: float = 0.0,
        cancel_during_cleanup: bool = True,
    ) -> None:
        super().__init__(
            bundle,
            profile=profile,
            clock=clock,
            unknown_chat_member=unknown_chat_member,
        )
        self.cancellation_probe = cancellation
        self.cleanup_failure = cleanup_failure
        self.cleanup_extra_seconds = cleanup_extra_seconds
        self.cancel_during_cleanup = cancel_during_cleanup

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
        result = super().request(
            method,
            path,
            payload,
            cancellation=cancellation,
            absolute_deadline=absolute_deadline,
            phase_timeout_seconds=phase_timeout_seconds,
        )
        if path == "/api/generate":
            self.cancellation_probe.cancelled = self.cancel_during_cleanup
            if self.clock is None:  # pragma: no cover - fixture misuse guard
                raise AssertionError("outcome transport requires a clock")
            self.clock.value += self.cleanup_extra_seconds
            if self.cleanup_failure:
                return {**result, "done": False}
        return result


def test_cancellation_arriving_during_successful_cleanup_blocks_publication() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    clock = _Clock()
    cancellation = _Cancellation()
    transport = _OutcomeTransport(
        bundle,
        profile=profile,
        clock=clock,
        cancellation=cancellation,
    )

    with pytest.raises(SemanticOllamaExecutionError, match="cancelled"):
        _execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            profile,
            build_semantic_generation_request(bundle.planning_request),
            cancellation=cancellation,
            clock=clock,
        )


def test_terminal_precedence_keeps_primary_over_cleanup_expiry_and_cancellation() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    clock = _Clock()
    cancellation = _Cancellation()
    transport = _OutcomeTransport(
        bundle,
        profile=profile,
        clock=clock,
        cancellation=cancellation,
        unknown_chat_member=True,
        cleanup_failure=True,
        cleanup_extra_seconds=30.0,
    )

    with pytest.raises(SemanticOllamaExecutionError, match="chat_members"):
        _execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            profile,
            build_semantic_generation_request(bundle.planning_request),
            cancellation=cancellation,
            clock=clock,
        )


def test_terminal_precedence_keeps_cleanup_failure_over_expiry_and_cancellation() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    clock = _Clock()
    cancellation = _Cancellation()
    transport = _OutcomeTransport(
        bundle,
        profile=profile,
        clock=clock,
        cancellation=cancellation,
        cleanup_failure=True,
        cleanup_extra_seconds=6.0,
    )

    with pytest.raises(SemanticOllamaExecutionError, match="cleanup_failed"):
        _execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            profile,
            build_semantic_generation_request(bundle.planning_request),
            cancellation=cancellation,
            clock=clock,
        )


def test_terminal_precedence_keeps_primary_over_outer_expiry() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    clock = _Clock()
    cancellation = _Cancellation()
    transport = _OutcomeTransport(
        bundle,
        profile=profile,
        clock=clock,
        cancellation=cancellation,
        unknown_chat_member=True,
        cleanup_extra_seconds=23.0,
        cancel_during_cleanup=False,
    )

    with pytest.raises(SemanticOllamaExecutionError, match="chat_members"):
        _execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            profile,
            build_semantic_generation_request(bundle.planning_request),
            cancellation=cancellation,
            clock=clock,
        )


def test_terminal_precedence_keeps_outer_timeout_over_cleanup_cancellation() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    clock = _Clock()
    cancellation = _Cancellation()
    transport = _OutcomeTransport(
        bundle,
        profile=profile,
        clock=clock,
        cancellation=cancellation,
        cleanup_extra_seconds=4.0,
    )

    with pytest.raises(SemanticOllamaExecutionError, match="timeout"):
        _execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            profile,
            build_semantic_generation_request(bundle.planning_request),
            cancellation=cancellation,
            clock=clock,
        )


class _AtDeadlineTransport:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

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
        del method, path, payload, cancellation, phase_timeout_seconds
        self.clock.value = absolute_deadline
        return {"version": "0.32.13"}


def test_semantic_request_rejects_result_observed_exactly_at_deadline() -> None:
    clock = _Clock()
    deadline = clock.value + 1.0

    with pytest.raises(SemanticOllamaExecutionError, match="timeout"):
        _semantic_request(
            cast(Any, _AtDeadlineTransport(clock)),
            "GET",
            "/api/version",
            None,
            cancellation=None,
            deadline=deadline,
            clock=clock,
        )


def test_outer_deadline_crossed_during_cleanup_blocks_final_publication() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    clock = _Clock()
    transport = _SemanticTransport(
        bundle,
        profile=profile,
        clock=clock,
        cleanup_poll_duration=4.0,
    )

    with pytest.raises(SemanticOllamaExecutionError, match="timeout"):
        _execute_semantic_ollama_test_only(
            transport,
            _provider_setup(),
            profile,
            build_semantic_generation_request(bundle.planning_request),
            cancellation=None,
            clock=clock,
        )


def _nested_json_object(depth: int) -> dict[str, object]:
    value: object = "leaf"
    for _ in range(depth):
        value = {"nested": value}
    return cast(dict[str, object], value)


def test_semantic_response_json_validator_closes_depth_members_items_and_unicode() -> None:
    validate = ollama_native._validate_semantic_response_json

    assert validate({"root": "value"}) == {"root": "value"}
    assert validate(_nested_json_object(8)) == _nested_json_object(8)
    assert validate({f"key_{index}": index for index in range(64)})
    assert validate({"items": list(range(64))})
    unicode_value = {"資料": "合法值😀"}
    assert validate(unicode_value) == unicode_value
    for invalid in (
        _nested_json_object(9),
        {f"key_{index}": index for index in range(65)},
        {"items": list(range(65))},
        {"invalid": "\ud800"},
    ):
        with pytest.raises(SemanticOllamaExecutionError, match="provider_output_invalid"):
            validate(invalid)


def _show_tensor_items(count: int) -> list[dict[str, object]]:
    return [
        {
            "name": f"blk.{index}.weight",
            "shape": [248_320, 2_048],
            "type": "F16",
        }
        for index in range(count)
    ]


def test_show_tensor_wire_has_one_endpoint_scoped_bounded_exception() -> None:
    validate = ollama_native._validate_semantic_response_json

    for count in (0, 866, 4_096):
        value = {"tensors": _show_tensor_items(count)}
        assert validate(value, path="/api/show") == value

    invalid_items: tuple[dict[str, object], ...] = (
        {"name": "tensor", "shape": [1]},
        {"name": "tensor", "shape": [1], "type": "F16", "extra": 1},
        {"name": "", "shape": [1], "type": "F16"},
        {"name": "n" * 129, "shape": [1], "type": "F16"},
        {"name": "\ud800", "shape": [1], "type": "F16"},
        {"name": "tensor", "shape": [1], "type": ""},
        {"name": "tensor", "shape": [1], "type": "t" * 33},
        {"name": "tensor", "shape": [], "type": "F16"},
        {"name": "tensor", "shape": [1] * 9, "type": "F16"},
        {"name": "tensor", "shape": [True], "type": "F16"},
        {"name": "tensor", "shape": [0], "type": "F16"},
        {"name": "tensor", "shape": [-1], "type": "F16"},
        {"name": "tensor", "shape": [16_777_217], "type": "F16"},
        {"name": "tensor", "shape": [[1]], "type": "F16"},
    )
    for item in invalid_items:
        with pytest.raises(SemanticOllamaExecutionError, match="provider_output_invalid"):
            validate({"tensors": [item]}, path="/api/show")

    for invalid in (
        {"tensors": _show_tensor_items(4_097)},
        {"nested": {"tensors": _show_tensor_items(65)}},
        {"other": list(range(65))},
    ):
        with pytest.raises(SemanticOllamaExecutionError, match="provider_output_invalid"):
            validate(invalid, path="/api/show")
    with pytest.raises(SemanticOllamaExecutionError, match="provider_output_invalid"):
        validate({"tensors": _show_tensor_items(65)}, path="/api/tags")


def test_show_tensor_wire_never_influences_execution_authority() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)
    generation = build_semantic_generation_request(bundle.planning_request)
    plain = execute_semantic_ollama(
        _SemanticTransport(bundle, profile=profile),
        _provider_setup(),
        profile,
        generation,
    )
    with_tensors = execute_semantic_ollama(
        _MutatingSemanticTransport(
            bundle,
            lambda path, value, _: (
                {**value, "tensors": _show_tensor_items(866)} if path == "/api/show" else value
            ),
            profile=profile,
        ),
        _provider_setup(),
        profile,
        generation,
    )

    assert with_tensors.qualification_fingerprint == plain.qualification_fingerprint
    assert with_tensors.capability_fingerprint == plain.capability_fingerprint
    assert (
        cast(Any, with_tensors.model_result).to_public_dict()
        == cast(Any, plain.model_result).to_public_dict()
    )
    assert "tensor" not in repr(with_tensors)


def test_semantic_response_envelope_limits_apply_before_endpoint_specific_parsing() -> None:
    bundle = _provider_bundle()
    profile = _fixture_profile(bundle)

    mutations = (
        lambda value: {
            **value,
            "model_info": {**value["model_info"], "deep": _nested_json_object(9)},
        },
        lambda value: {
            **value,
            "model_info": {f"extra_{index}": index for index in range(64)} | value["model_info"],
        },
        lambda value: {
            **value,
            "model_info": {**value["model_info"], "many": list(range(65))},
        },
        lambda value: {
            **value,
            "model_info": {**value["model_info"], "invalid": "\ud800"},
        },
    )
    for mutate in mutations:
        transport = _MutatingSemanticTransport(
            bundle,
            lambda path, value, _, mutate=mutate: mutate(value) if path == "/api/show" else value,
            profile=profile,
        )
        with pytest.raises(SemanticOllamaExecutionError, match="provider_output_invalid"):
            execute_semantic_ollama(
                transport,
                _provider_setup(),
                profile,
                build_semantic_generation_request(bundle.planning_request),
            )


@pytest.mark.parametrize(
    "cohort",
    (
        "report",
        "wiring",
        "profile",
        "workspace",
        "baseline_plan",
        "planning_request",
        "reduction_items",
        "planning_result",
        "enrichment_result",
        "transaction",
        "candidate_graph",
    ),
)
def test_review_authority_revalidates_every_retained_current_content_cohort(cohort: str) -> None:
    bundle, product = _qualified_product()
    owner = _SemanticProposalAuthorityOwner()
    reservation = owner.begin(bundle, _provider_setup(), f"prompt.{cohort}", f"node.{cohort}")
    authority = owner.commit(reservation, product)
    if cohort == "report":
        object.__setattr__(bundle.report, "revision", bundle.report.revision + 1)
    elif cohort == "wiring":
        object.__setattr__(bundle.wiring, "prompt_fingerprint", "sha256:" + ("1" * 64))
    elif cohort == "profile":
        object.__setattr__(bundle, "profile", replace(bundle.profile, server_version="0.32.14"))
    elif cohort == "workspace":
        object.__setattr__(bundle.workspace, "revision", bundle.workspace.revision + 1)
    elif cohort == "baseline_plan":
        object.__setattr__(bundle.baseline_plan, "plan_id", bundle.baseline_plan.plan_id + ".drift")
    elif cohort == "planning_request":
        object.__setattr__(bundle.planning_request, "schema", "h3.semantic.drift")
    elif cohort == "reduction_items":
        object.__setattr__(bundle, "reduction_items", ())
    elif cohort == "planning_result":
        object.__setattr__(product.planning_result, "schema", "h3.semantic.drift")
    elif cohort == "enrichment_result":
        object.__setattr__(product.enrichment_result, "status", SemanticEnrichmentStatus.NOOP)
    elif cohort == "transaction":
        object.__setattr__(
            product.transaction,
            "state",
            SemanticProposalTransactionState.FAILED,
        )
    else:
        object.__setattr__(
            product.transaction,
            "candidate_graph",
            bundle.baseline_plan.intent_graph,
        )

    with pytest.raises(SemanticProposalProducerError, match="review_authority_content"):
        with owner.claim(authority):
            pass
    entry = owner._entries[authority.lineage_id]
    assert entry.state == "invalid"
    assert entry.source is None
    assert entry.product is None
    assert entry.handle is None
    assert entry.active_claim is None
    assert entry.reserved_bytes == 0


def test_active_claim_bundle_replacement_invalidates_once_without_second_abort() -> None:
    bundle, product = _qualified_product()
    owner = _SemanticProposalAuthorityOwner()
    reservation = owner.begin(bundle, _provider_setup(), "prompt.bundle", "node.bundle")
    authority = owner.commit(reservation, product)

    with owner.claim(authority) as claim:
        object.__setattr__(
            claim,
            "_bundle",
            replace(claim.bundle),
        )
        with pytest.raises(SemanticProposalProducerError, match="review_authority_content"):
            claim.commit()
        assert claim._finished is True

    entry = owner._entries[authority.lineage_id]
    assert entry.state == "invalid"
    assert entry.active_claim is None
    assert entry.reserved_bytes == 0
    with pytest.raises(SemanticProposalProducerError, match="lineage_invalid"):
        owner.begin(bundle, _provider_setup(), "prompt.bundle", "node.bundle")


def test_coordinated_same_handle_drift_invalidates_against_owner_snapshot() -> None:
    bundle, product = _qualified_product()
    owner = _SemanticProposalAuthorityOwner()
    reservation = owner.begin(bundle, _provider_setup(), "prompt.handle.drift", "node.handle.drift")
    authority = owner.commit(reservation, product)
    new_expiry = authority.expires_at_ms + 1
    object.__setattr__(authority, "_expires_at_ms", new_expiry)
    object.__setattr__(
        authority,
        "_fingerprint",
        canonical_fingerprint(
            {
                "schema": authority.to_public_dict()["schema"],
                "lineage_id": authority.lineage_id,
                "expires_at_ms": new_expiry,
            }
        ),
    )

    with pytest.raises(SemanticProposalProducerError, match="review_authority"):
        with owner.claim(authority):
            pass
    entry = owner._entries[authority.lineage_id]
    assert entry.state == "invalid"
    assert entry.source is None
    assert entry.product is None
    assert entry.reserved_bytes == 0


def test_copied_review_handle_cannot_invalidate_genuine_entry() -> None:
    bundle, product = _qualified_product()
    owner = _SemanticProposalAuthorityOwner()
    reservation = owner.begin(bundle, _provider_setup(), "prompt.handle", "node.handle")
    authority = owner.commit(reservation, product)
    copied = object.__new__(SemanticProposalReviewAuthority)
    for field in ("_lineage_id", "_expires_at_ms", "_fingerprint"):
        object.__setattr__(copied, field, getattr(authority, field))

    with pytest.raises(SemanticProposalProducerError, match="review_authority"):
        with owner.claim(copied):
            pass
    with owner.claim(authority) as claim:
        claim.abort()
    assert owner.begin(bundle, _provider_setup(), "prompt.handle", "node.handle") is authority


def test_mutated_same_review_handle_invalidates_its_genuine_entry() -> None:
    bundle, product = _qualified_product()
    owner = _SemanticProposalAuthorityOwner()
    reservation = owner.begin(bundle, _provider_setup(), "prompt.handle.same", "node.handle.same")
    authority = owner.commit(reservation, product)
    original_lineage_id = authority.lineage_id
    object.__setattr__(authority, "_lineage_id", original_lineage_id + ".drift")

    with pytest.raises(SemanticProposalProducerError, match="review_authority"):
        with owner.claim(authority):
            pass
    entry = owner._entries[original_lineage_id]
    assert entry.state == "invalid"
    assert entry.reserved_bytes == 0


def test_retained_state_accounting_uses_complete_named_wire_set_and_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, product = _qualified_product()
    owner = _SemanticProposalAuthorityOwner()
    reservation = owner.begin(bundle, _provider_setup(), "prompt.bytes", "node.bytes")
    entry = owner._entries[reservation.lineage_id]
    wires = (
        {"wire": bundle.report.to_wire(), "revision": bundle.report.revision},
        bundle.wiring.to_wire(),
        bundle.profile.to_wire(),
        bundle.workspace.to_wire(),
        bundle.baseline_plan.to_wire(),
        bundle.planning_request.to_wire(),
        [
            {
                "target_kind": item.target_kind,
                "target_id": item.target_id,
                "claim": item.claim,
            }
            for item in bundle.reduction_items
        ],
        product.planning_result.to_wire(),
        product.enrichment_result.to_wire(),
        product.transaction.to_public_dict(),
        product.transaction.candidate_graph.to_wire(),
        product.usage.to_public_dict(),
    )
    expected = 1_024 + sum(
        len(
            json.dumps(
                wire,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        for wire in wires
    )
    assert owner._encoded_size(entry, product) == expected

    monkeypatch.setattr(semantic_proposal_producer, "_MAX_LINEAGE_BYTES", expected)
    assert type(owner.commit(reservation, product)) is SemanticProposalReviewAuthority

    below_owner = _SemanticProposalAuthorityOwner()
    below_reservation = below_owner.begin(
        bundle,
        _provider_setup(),
        "prompt.bytes.low",
        "node.bytes.low",
    )
    monkeypatch.setattr(semantic_proposal_producer, "_MAX_LINEAGE_BYTES", expected - 1)
    with pytest.raises(SemanticProposalProducerError, match="authority_byte_limit"):
        below_owner.commit(below_reservation, product)


def test_invalid_tombstones_share_non_evicting_process_lifetime_capacity() -> None:
    bundle = _provider_bundle()
    setup = _provider_setup()
    owner = _SemanticProposalAuthorityOwner()
    for index in range(64):
        reservation = owner.begin(bundle, setup, f"prompt.invalid.{index}", f"node.invalid.{index}")
        owner._invalidate(owner._entries[reservation.lineage_id])
    assert {entry.state for entry in owner._entries.values()} == {"invalid"}
    assert all(entry.reserved_bytes == 0 for entry in owner._entries.values())
    with pytest.raises(SemanticProposalProducerError, match="lineage_capacity"):
        owner.begin(bundle, setup, "prompt.invalid.64", "node.invalid.64")
    assert len(owner._entries) == 64


def test_catalog_read_failure_suppresses_private_path_from_formatted_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "private-install" / "semantic-provider-catalog.json"
    monkeypatch.setattr(semantic_proposal_producer, "_CATALOG_PATH", private_path)

    with pytest.raises(SemanticProposalProducerError, match="catalog_unavailable") as captured:
        load_semantic_provider_catalog()

    formatted = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert str(private_path) not in formatted
