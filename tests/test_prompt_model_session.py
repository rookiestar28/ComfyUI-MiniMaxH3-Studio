"""M22-03 local loopback prompt-model transport and session tests."""

from __future__ import annotations

import json
import threading
import unittest
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from comfyui_h3_context.adapters.ollama_native import (
    MAX_OLLAMA_REQUEST_BYTES,
    MAX_OLLAMA_RESPONSE_BYTES,
)
from comfyui_h3_context.adapters.prompt_model_transport import (
    FAMILY_PATHS,
    LOOPBACK_SERVER_PROMPT_PATHS,
    MAX_JSON_NESTING_DEPTH,
    MAX_PROMPT_MODEL_REQUEST_BYTES,
    MAX_PROMPT_MODEL_RESPONSE_BYTES,
    OLLAMA_PROMPT_PATHS,
    PROBEABLE_RUNTIMES,
    LoopbackJsonExchange,
    PromptModelTransportError,
    decode_bounded_json,
    decode_probe_output,
    probe_native_runtime,
    resolve_live_identity,
    run_prompt_model_session,
)
from comfyui_h3_context.core.prompt_model_budget import (
    ContextProfileLadder,
    ContextProfileStep,
    PromptModelWorkload,
    plan_prompt_model_request,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelCapabilities,
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcomeId,
    admit_egress_destination,
    build_prompt_model_capabilities,
)
from comfyui_h3_context.core.prompt_model_session import (
    LOCAL_TRANSPORT_FAMILIES,
    DiscoveryObservation,
    DiscoveryRejection,
    IdentityVerification,
    LiveModelIdentity,
    NativeProbeReport,
    NativeProbeStatus,
    PromptModelMessage,
    PromptModelRole,
    PromptModelSessionRequest,
    UnloadArbiter,
    UnloadDisposition,
    admit_session_request,
    build_request_payload,
    describe_discovery,
    parse_response_text,
    probe_outcome,
    verify_live_identity,
)

MODEL_ID = "qwen3.8:27b-bf16"
DIGEST = "sha256:" + "1" * 64
#: What Ollama actually puts on the wire: bare lowercase hex, no algorithm prefix. These
#: fixtures used to carry the repository spelling instead, which quietly made a correct host
#: impossible to model and hid a real digest-domain mismatch (M22-13).
WIRE_DIGEST = DIGEST.removeprefix("sha256:")
LICENSE_DIGEST = "sha256:" + "2" * 64
LADDER = ContextProfileLadder(
    steps=(
        ContextProfileStep(context_tokens=8_192, memory_bytes=6 * 1024**3),
        ContextProfileStep(context_tokens=16_384, memory_bytes=9 * 1024**3),
    )
)
ENDPOINTS = {
    PromptModelFamily.OLLAMA: "http://127.0.0.1:11434",
    PromptModelFamily.LOOPBACK_SERVER: "http://127.0.0.1:8080",
}


def capabilities(
    family: PromptModelFamily, *, media: tuple[str, ...] = ("text",)
) -> PromptModelCapabilities:
    return build_prompt_model_capabilities(
        {
            "family": family.value,
            "accepted_media": list(media),
            "provider_managed_context": True,
            "provider_managed_kv_cache": True,
            "max_request_bytes": 262_144,
            "max_context_tokens": 32_768,
            "max_output_tokens": 4_096,
            "streaming": False,
            "local_only": True,
            "requires_credential": False,
        }
    )


def profile(family: PromptModelFamily, **overrides: object) -> PromptModelProfile:
    values: dict[str, object] = {
        "profile_id": "local.pinned",
        "family": family,
        "endpoint": ENDPOINTS[family],
        "model_id": MODEL_ID,
        "model_digest": DIGEST,
        "adapter_version": "1.0.0",
        "parser_version": "h3.prompt_model.draft_json.v1",
        "license_id": "Apache-2.0",
        "license_source": "ollama_show",
        "license_text_sha256": LICENSE_DIGEST,
        "capabilities": capabilities(family),
    }
    values.update(overrides)
    return PromptModelProfile(**values)  # type: ignore[arg-type]


def session_request(
    family: PromptModelFamily = PromptModelFamily.OLLAMA,
    *,
    images: tuple[str, ...] = (),
    media: tuple[str, ...] = ("text",),
) -> PromptModelSessionRequest:
    pinned = profile(family, capabilities=capabilities(family, media=media))
    destination = admit_egress_destination(family, ENDPOINTS[family])
    decision = plan_prompt_model_request(
        capabilities=pinned.capabilities,
        ladder=LADDER,
        workload=PromptModelWorkload(
            characters=1_200,
            wide_characters=0,
            messages=2,
            visual_inputs=len(images),
            request_bytes=4_096,
            requested_output_tokens=512,
        ),
        requested_step=0,
        available_memory_bytes=32 * 1024**3,
    )
    assert decision.plan is not None
    return PromptModelSessionRequest(
        profile=pinned,
        destination=destination,
        plan=decision.plan,
        messages=(
            PromptModelMessage(role=PromptModelRole.SYSTEM, text="be exact"),
            PromptModelMessage(role=PromptModelRole.USER, text="a cinematic street at dusk"),
        ),
        image_payloads=images,
    )


class FakeExchange:
    """Records every call so a test can prove a request was never issued."""

    def __init__(
        self,
        *,
        tags: object = None,
        chat: object = None,
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self._tags = tags
        self._chat = chat
        self._raises = raises

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        self.calls.append((method, path))
        if self._raises is not None and path in {"/api/chat", "/v1/chat/completions"}:
            raise self._raises
        if path in {"/api/tags", "/v1/models"}:
            if isinstance(self._tags, Exception):
                raise self._tags
            return self._tags if isinstance(self._tags, Mapping) else {}
        return self._chat if isinstance(self._chat, Mapping) else {}


def ollama_tags(*, digest: str | None = WIRE_DIGEST, name: str = MODEL_ID) -> dict[str, object]:
    entry: dict[str, object] = {"name": name}
    if digest is not None:
        entry["digest"] = digest
    return {"models": [entry]}


#: A completed answer from the exact model that was asked, with no reasoning trace: the shape
#: the M22-13 request explicitly demands. Every field here is load-bearing, and the tests
#: below drop one at a time to prove it. The message is named separately so the variants
#: can extend it without the whole answer collapsing to `dict[str, object]`.
OLLAMA_MESSAGE: dict[str, object] = {"role": "assistant", "content": "a quiet street, low sun"}
OLLAMA_CHAT: dict[str, object] = {
    "model": MODEL_ID,
    "done": True,
    "message": OLLAMA_MESSAGE,
}
SERVER_MODELS = {"data": [{"id": MODEL_ID}]}
SERVER_CHAT = {"choices": [{"message": {"role": "assistant", "content": "a quiet street"}}]}


class LoopbackOnlyTests(unittest.TestCase):
    def test_every_local_family_destination_is_loopback(self) -> None:
        for family in LOCAL_TRANSPORT_FAMILIES:
            with self.subTest(family=family):
                destination = admit_egress_destination(family, ENDPOINTS[family])
                self.assertTrue(destination.loopback)
                exchange = LoopbackJsonExchange(destination)
                self.assertEqual(exchange.allowed_paths, FAMILY_PATHS[family])

    def test_a_remote_destination_cannot_reach_this_path(self) -> None:
        remote = admit_egress_destination(
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE, "https://api.example.com/v1"
        )
        with self.assertRaises(PromptModelTransportError) as caught:
            LoopbackJsonExchange(remote)
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.CAPABILITY_MISMATCH)

    def test_the_transport_cannot_be_built_from_a_url_string(self) -> None:
        for value in (ENDPOINTS[PromptModelFamily.OLLAMA], None, 8080):
            with self.subTest(value=value):
                with self.assertRaises(PromptModelTransportError):
                    LoopbackJsonExchange(value)  # type: ignore[arg-type]

    def test_the_in_process_family_has_no_transport_here(self) -> None:
        self.assertNotIn(PromptModelFamily.IN_PROCESS_GGUF, LOCAL_TRANSPORT_FAMILIES)
        self.assertNotIn(PromptModelFamily.IN_PROCESS_GGUF, FAMILY_PATHS)

    def test_method_and_path_are_closed_per_family(self) -> None:
        exchange = LoopbackJsonExchange(
            admit_egress_destination(PromptModelFamily.OLLAMA, ENDPOINTS[PromptModelFamily.OLLAMA])
        )
        for method, path in (("PUT", "/api/chat"), ("GET", "/api/show"), ("POST", "/v1/models")):
            with self.subTest(method=method, path=path):
                with self.assertRaises(PromptModelTransportError):
                    exchange.request(method, path)

    def test_bounds_match_the_accepted_ollama_route(self) -> None:
        self.assertEqual(MAX_PROMPT_MODEL_REQUEST_BYTES, MAX_OLLAMA_REQUEST_BYTES)
        self.assertEqual(MAX_PROMPT_MODEL_RESPONSE_BYTES, MAX_OLLAMA_RESPONSE_BYTES)
        self.assertEqual(OLLAMA_PROMPT_PATHS, frozenset({"/api/tags", "/api/show", "/api/chat"}))
        # Routes that mutate the operator model store are absent by intent (M22-13). Naming
        # them here means adding one cannot pass as an oversight.
        for mutating in ("/api/pull", "/api/create", "/api/delete", "/api/copy", "/api/push"):
            self.assertNotIn(mutating, OLLAMA_PROMPT_PATHS)
        self.assertEqual(
            LOOPBACK_SERVER_PROMPT_PATHS, frozenset({"/v1/models", "/v1/chat/completions"})
        )


class IdentityTests(unittest.TestCase):
    def test_a_matching_digest_is_the_strong_verification(self) -> None:
        decision = verify_live_identity(
            profile(PromptModelFamily.OLLAMA),
            LiveModelIdentity(model_id=MODEL_ID, digest=DIGEST),
        )
        self.assertIs(decision.verification, IdentityVerification.DIGEST_MATCHED)
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.OK)

    def test_a_swapped_digest_is_a_distinct_outcome(self) -> None:
        decision = verify_live_identity(
            profile(PromptModelFamily.OLLAMA),
            LiveModelIdentity(model_id=MODEL_ID, digest="sha256:" + "9" * 64),
        )
        self.assertIsNone(decision.verification)
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.DIGEST_MISMATCH)

    def test_a_missing_digest_is_recorded_as_unverified_not_claimed_as_matched(self) -> None:
        decision = verify_live_identity(
            profile(PromptModelFamily.LOOPBACK_SERVER),
            LiveModelIdentity(model_id=MODEL_ID, digest=None),
        )
        self.assertIs(decision.verification, IdentityVerification.DIGEST_UNVERIFIED)
        self.assertEqual(dict(decision.outcome.parameters), {"verification": "digest_unverified"})

    def test_a_different_model_is_never_substituted(self) -> None:
        decision = verify_live_identity(
            profile(PromptModelFamily.OLLAMA),
            LiveModelIdentity(model_id="some-other-model", digest=DIGEST),
        )
        self.assertIsNone(decision.verification)
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.MODEL_MISSING)

    def test_identity_is_resolved_live_for_each_family(self) -> None:
        ollama = FakeExchange(tags=ollama_tags())
        observed = resolve_live_identity(PromptModelFamily.OLLAMA, MODEL_ID, ollama)
        assert observed is not None
        # The wire form goes in and the repository form comes out: this is the one crossing point.
        self.assertEqual(observed.digest, DIGEST)
        self.assertEqual(ollama.calls, [("GET", "/api/tags")])

        server = FakeExchange(tags=SERVER_MODELS)
        observed = resolve_live_identity(PromptModelFamily.LOOPBACK_SERVER, MODEL_ID, server)
        assert observed is not None
        self.assertIsNone(observed.digest)
        self.assertEqual(server.calls, [("GET", "/v1/models")])

    def test_a_model_the_runtime_does_not_hold_stops_the_request(self) -> None:
        for family, listing in (
            (PromptModelFamily.OLLAMA, ollama_tags(name="something-else")),
            (PromptModelFamily.LOOPBACK_SERVER, {"data": [{"id": "something-else"}]}),
        ):
            with self.subTest(family=family):
                probe = FakeExchange(tags=listing)
                self.assertIsNone(resolve_live_identity(family, MODEL_ID, probe))
                exchange = FakeExchange(tags=listing, chat=OLLAMA_CHAT)
                result = run_prompt_model_session(session_request(family), exchange)
                self.assertIsNone(result.answer)
                self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.MODEL_MISSING)
                self.assertEqual(len(exchange.calls), 1)

    def test_a_swapped_model_stops_the_request_before_it_is_issued(self) -> None:
        exchange = FakeExchange(tags=ollama_tags(digest="7" * 64), chat=OLLAMA_CHAT)
        result = run_prompt_model_session(session_request(), exchange)
        self.assertIsNone(result.answer)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.DIGEST_MISMATCH)
        self.assertEqual(exchange.calls, [("GET", "/api/tags")])


class SessionTests(unittest.TestCase):
    def test_a_completed_ollama_session_returns_a_verified_answer(self) -> None:
        exchange = FakeExchange(tags=ollama_tags(), chat=OLLAMA_CHAT)
        result = run_prompt_model_session(session_request(), exchange)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.OK)
        assert result.answer is not None
        self.assertEqual(result.answer.text, "a quiet street, low sun")
        self.assertIs(result.answer.verification, IdentityVerification.DIGEST_MATCHED)
        self.assertEqual(exchange.calls, [("GET", "/api/tags"), ("POST", "/api/chat")])

    def test_a_completed_server_session_records_the_weaker_verification(self) -> None:
        exchange = FakeExchange(tags=SERVER_MODELS, chat=SERVER_CHAT)
        result = run_prompt_model_session(
            session_request(PromptModelFamily.LOOPBACK_SERVER), exchange
        )
        assert result.answer is not None
        self.assertIs(result.answer.verification, IdentityVerification.DIGEST_UNVERIFIED)
        self.assertEqual(exchange.calls, [("GET", "/v1/models"), ("POST", "/v1/chat/completions")])

    def test_the_answer_receipt_carries_a_length_not_the_text(self) -> None:
        exchange = FakeExchange(tags=ollama_tags(), chat=OLLAMA_CHAT)
        result = run_prompt_model_session(session_request(), exchange)
        assert result.answer is not None
        wire = result.answer.to_wire()
        self.assertEqual(wire["characters"], len("a quiet street, low sun"))
        self.assertNotIn("a quiet street", json.dumps(wire))

    def test_media_is_carried_when_declared_and_refused_when_not(self) -> None:
        refused = admit_session_request(session_request(images=("aGVsbG8=",)))
        self.assertIs(refused.outcome_id, PromptModelOutcomeId.UNSUPPORTED_MEDIA)

        allowed = session_request(
            PromptModelFamily.LOOPBACK_SERVER, images=("aGVsbG8=",), media=("text", "image")
        )
        self.assertIs(admit_session_request(allowed).outcome_id, PromptModelOutcomeId.OK)
        payload = build_request_payload(allowed)
        messages = payload["messages"]
        assert isinstance(messages, list)
        self.assertEqual(messages[-1]["content"][1]["type"], "image_url")

    def test_the_local_lane_is_text_only_however_the_row_is_written(self) -> None:
        """M22-13: the curated local model reports `vision`, so a catalog row that declared image
        media must still not open a media path this lane never qualified."""

        declared = session_request(images=("aGVsbG8=",), media=("text", "image"))
        self.assertIs(
            admit_session_request(declared).outcome_id, PromptModelOutcomeId.UNSUPPORTED_MEDIA
        )
        with self.assertRaises(PromptModelContractError) as caught:
            build_request_payload(declared)
        self.assertEqual(str(caught.exception), "session_media_unsupported")

    def test_only_a_completed_reasoning_free_answer_from_the_exact_model_is_read(self) -> None:
        """M22-13: each of these is a property the request asked for, so missing it refuses."""

        for label, response in (
            ("unfinished", {**OLLAMA_CHAT, "done": False}),
            ("no done flag", {key: OLLAMA_CHAT[key] for key in ("model", "message")}),
            ("another model", {**OLLAMA_CHAT, "model": "some-other-model"}),
            (
                "tool call",
                {
                    **OLLAMA_CHAT,
                    "message": {
                        **OLLAMA_MESSAGE,
                        "tool_calls": [{"function": {"name": "read_file"}}],
                    },
                },
            ),
            (
                "image",
                {**OLLAMA_CHAT, "message": {**OLLAMA_MESSAGE, "images": ["aGVsbG8="]}},
            ),
        ):
            with self.subTest(response=label):
                exchange = FakeExchange(tags=ollama_tags(), chat=response)
                result = run_prompt_model_session(session_request(), exchange)
                self.assertIsNone(result.answer)
                self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)

        # An empty reasoning field is the server saying it did not reason, which is what was asked.
        exchange = FakeExchange(
            tags=ollama_tags(),
            chat={**OLLAMA_CHAT, "message": {**OLLAMA_MESSAGE, "thinking": ""}},
        )
        self.assertIsNotNone(run_prompt_model_session(session_request(), exchange).answer)

    def test_the_local_chat_body_switches_off_every_capability_it_did_not_qualify(self) -> None:
        """M22-13: `thinking`, `tools` and `vision` are all declared by the curated model, so the
        body says so explicitly rather than trusting the server's defaults, and it unloads after."""

        payload = build_request_payload(session_request())
        self.assertIs(payload["stream"], False)
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["keep_alive"], 0)
        self.assertNotIn("tools", payload)
        self.assertEqual(
            payload["format"],
            {
                "type": "object",
                "properties": {
                    "schema": {"type": "string", "enum": ["h3.prompt_model.draft_json.v1"]},
                    "prompt_text": {"type": "string"},
                },
                "required": ["schema", "prompt_text"],
                "additionalProperties": False,
            },
        )
        # It has to survive the transport's own encoder, not merely compare equal in Python.
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False)

    def test_the_server_payload_carries_media_as_content_parts(self) -> None:
        allowed = session_request(
            PromptModelFamily.LOOPBACK_SERVER, images=("aGVsbG8=",), media=("text", "image")
        )
        payload = build_request_payload(allowed)
        messages = payload["messages"]
        assert isinstance(messages, list)
        parts = messages[-1]["content"]
        assert isinstance(parts, list)
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[1]["type"], "image_url")

    def test_the_payload_carries_the_admitted_budget_not_a_guess(self) -> None:
        request = session_request()
        payload = build_request_payload(request)
        options = payload["options"]
        assert isinstance(options, dict)
        self.assertEqual(options["num_ctx"], request.plan.context_tokens)
        self.assertEqual(options["num_predict"], request.plan.reserved_output_tokens)

    def test_a_request_cannot_be_assembled_across_families(self) -> None:
        pinned = profile(PromptModelFamily.OLLAMA)
        destination = admit_egress_destination(
            PromptModelFamily.LOOPBACK_SERVER, ENDPOINTS[PromptModelFamily.LOOPBACK_SERVER]
        )
        plan = session_request().plan
        with self.assertRaises(PromptModelContractError) as caught:
            PromptModelSessionRequest(
                profile=pinned,
                destination=destination,
                plan=plan,
                messages=(PromptModelMessage(role=PromptModelRole.USER, text="hi"),),
            )
        self.assertEqual(caught.exception.code, "request_family_mismatch")

    def test_cancellation_is_checked_before_any_call_is_made(self) -> None:
        exchange = FakeExchange(tags=ollama_tags(), chat=OLLAMA_CHAT)
        result = run_prompt_model_session(session_request(), exchange, cancellation=lambda: True)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.CANCELLED)
        self.assertEqual(exchange.calls, [])

    def test_every_transport_failure_class_stays_distinct(self) -> None:
        cases = (
            PromptModelOutcomeId.TIMEOUT,
            PromptModelOutcomeId.BACKEND_ABSENT,
            PromptModelOutcomeId.QUOTA,
            PromptModelOutcomeId.AUTHENTICATION,
            PromptModelOutcomeId.REQUEST_TOO_LARGE,
            PromptModelOutcomeId.MALFORMED_RESPONSE,
            PromptModelOutcomeId.PROVIDER_ERROR,
        )
        seen: set[PromptModelOutcomeId] = set()
        for outcome_id in cases:
            with self.subTest(outcome=outcome_id):
                exchange = FakeExchange(
                    tags=ollama_tags(), raises=PromptModelTransportError(outcome_id, "detail")
                )
                result = run_prompt_model_session(session_request(), exchange)
                self.assertIsNone(result.answer)
                self.assertIs(result.outcome.outcome_id, outcome_id)
                self.assertNotIn(outcome_id, seen)
                seen.add(outcome_id)
        self.assertEqual(len(seen), len(cases))

    def test_provider_prose_does_not_escape_the_session_boundary(self) -> None:
        exchange = FakeExchange(
            tags=ollama_tags(),
            raises=PromptModelTransportError(
                PromptModelOutcomeId.PROVIDER_ERROR, "upstream said no"
            ),
        )
        result = run_prompt_model_session(session_request(), exchange)
        self.assertIsNone(result.outcome.to_wire()["untrusted_provider_detail"])
        self.assertNotIn("upstream said no", json.dumps(result.outcome.to_wire()))

    def test_a_malformed_response_shape_is_refused(self) -> None:
        for family, response in (
            (PromptModelFamily.OLLAMA, {"message": {"role": "assistant"}}),
            (PromptModelFamily.LOOPBACK_SERVER, {"choices": []}),
            (PromptModelFamily.OLLAMA, {"unexpected": 1}),
        ):
            with self.subTest(family=family):
                with self.assertRaises(PromptModelContractError):
                    parse_response_text(family, response)


class DiscoveryTests(unittest.TestCase):
    def test_every_rejection_reason_is_reachable_and_ambiguity_is_never_resolved(self) -> None:
        observations = (
            DiscoveryObservation(identifier="pinned", root_present=True),
            DiscoveryObservation(identifier="absent-root", root_present=False),
            DiscoveryObservation(identifier="twice", root_present=True, duplicate_identifier=True),
            DiscoveryObservation(
                identifier="needs-projector",
                root_present=True,
                projector_expected=True,
                projector_present=False,
            ),
            DiscoveryObservation(identifier="not-in-catalog", root_present=True),
        )
        candidates = describe_discovery(observations, {"pinned"})
        reasons = {candidate.identifier: candidate.reason for candidate in candidates}
        self.assertEqual(reasons["pinned"], DiscoveryRejection.ADMITTED)
        self.assertEqual(reasons["absent-root"], DiscoveryRejection.MISSING_ROOT)
        self.assertEqual(reasons["twice"], DiscoveryRejection.AMBIGUOUS_FOLDER)
        self.assertEqual(reasons["needs-projector"], DiscoveryRejection.UNPAIRED_PROJECTOR)
        self.assertEqual(reasons["not-in-catalog"], DiscoveryRejection.UNPINNED)
        self.assertEqual(len(candidates), len(observations))

    def test_an_empty_scan_reports_no_candidate_rather_than_nothing(self) -> None:
        candidates = describe_discovery((), frozenset())
        self.assertEqual(len(candidates), 1)
        self.assertIs(candidates[0].reason, DiscoveryRejection.NO_CANDIDATE)

    def test_capability_is_never_inferred_from_a_name(self) -> None:
        suggestive = DiscoveryObservation(identifier="llava-vision-13b", root_present=True)
        plain = DiscoveryObservation(identifier="plain-13b", root_present=True)
        candidates = describe_discovery((suggestive, plain), frozenset())
        self.assertEqual(
            {candidate.reason for candidate in candidates}, {DiscoveryRejection.UNPINNED}
        )

    def test_discovery_never_admits_something_the_catalog_does_not_pin(self) -> None:
        candidates = describe_discovery(
            (DiscoveryObservation(identifier="unpinned", root_present=True),), frozenset()
        )
        self.assertFalse(any(candidate.admitted for candidate in candidates))


class UnloadArbiterTests(unittest.TestCase):
    def test_an_unload_during_an_in_flight_request_is_deferred_and_reported(self) -> None:
        arbiter = UnloadArbiter()
        arbiter.begin_request(PromptModelFamily.OLLAMA)
        decision = arbiter.request_unload(PromptModelFamily.OLLAMA)
        self.assertIs(decision.disposition, UnloadDisposition.DEFERRED_IN_FLIGHT)
        self.assertEqual(decision.in_flight, 1)
        self.assertEqual(decision.to_wire()["disposition"], "deferred_in_flight")

    def test_an_unload_after_the_request_ends_is_performed(self) -> None:
        arbiter = UnloadArbiter()
        arbiter.begin_request(PromptModelFamily.OLLAMA)
        arbiter.end_request(PromptModelFamily.OLLAMA)
        decision = arbiter.request_unload(PromptModelFamily.OLLAMA)
        self.assertIs(decision.disposition, UnloadDisposition.PERFORMED)

    def test_a_family_that_was_never_loaded_is_reported_as_such(self) -> None:
        decision = UnloadArbiter().request_unload(PromptModelFamily.LOOPBACK_SERVER)
        self.assertIs(decision.disposition, UnloadDisposition.NOT_LOADED)

    def test_one_family_does_not_defer_another(self) -> None:
        arbiter = UnloadArbiter()
        arbiter.begin_request(PromptModelFamily.OLLAMA)
        arbiter.mark_loaded(PromptModelFamily.LOOPBACK_SERVER)
        decision = arbiter.request_unload(PromptModelFamily.LOOPBACK_SERVER)
        self.assertIs(decision.disposition, UnloadDisposition.PERFORMED)

    def test_an_unbalanced_end_is_a_contract_error(self) -> None:
        with self.assertRaises(PromptModelContractError):
            UnloadArbiter().end_request(PromptModelFamily.OLLAMA)


class NativeProbeTests(unittest.TestCase):
    def test_an_unlisted_runtime_is_never_imported(self) -> None:
        report = probe_native_runtime("os")
        self.assertIs(report.status, NativeProbeStatus.UNAVAILABLE)
        self.assertEqual(PROBEABLE_RUNTIMES, frozenset({"llama_cpp"}))

    def test_a_missing_runtime_fails_closed_without_touching_this_process(self) -> None:
        report = probe_native_runtime("llama_cpp", timeout_seconds=30.0)
        self.assertIn(
            report.status,
            {NativeProbeStatus.READY, NativeProbeStatus.CRASHED, NativeProbeStatus.UNAVAILABLE},
        )
        if report.status is not NativeProbeStatus.READY:
            self.assertFalse(report.usable)
            self.assertIs(probe_outcome(report).outcome_id, PromptModelOutcomeId.BACKEND_ABSENT)

    def test_a_probe_timeout_is_its_own_status_and_blocks_the_family(self) -> None:
        import subprocess as subprocess_module

        with mock.patch(
            "comfyui_h3_context.adapters.prompt_model_transport.subprocess.run",
            side_effect=subprocess_module.TimeoutExpired(cmd="probe", timeout=1.0),
        ):
            report = probe_native_runtime("llama_cpp")
        self.assertIs(report.status, NativeProbeStatus.TIMED_OUT)
        outcome = probe_outcome(report)
        self.assertIs(outcome.outcome_id, PromptModelOutcomeId.BACKEND_ABSENT)
        self.assertEqual(dict(outcome.parameters), {"status": "timed_out"})

    def test_probe_output_is_a_closed_shape(self) -> None:
        ready = decode_probe_output(b'{"runtime_version": "0.3.2", "detected_backends": ["cuda"]}')
        self.assertIs(ready.status, NativeProbeStatus.READY)
        self.assertEqual(ready.detected_backends, ("cuda",))
        for raw in (
            b"not json",
            b'{"runtime_version": "0.3.2"}',
            b'{"runtime_version": "0.3.2", "detected_backends": ["cuda"], "extra": 1}',
            b'{"runtime_version": 3, "detected_backends": []}',
            b'{"runtime_version": "x", "detected_backends": "cuda"}',
            b'{"runtime_version": "x", "detected_backends": [""]}',
        ):
            with self.subTest(raw=raw[:32]):
                self.assertIs(decode_probe_output(raw).status, NativeProbeStatus.MALFORMED)

    def test_a_failed_probe_cannot_carry_a_capability_claim(self) -> None:
        with self.assertRaises(PromptModelContractError):
            NativeProbeReport(
                status=NativeProbeStatus.CRASHED,
                runtime_version="0.3.2",
                detected_backends=("cuda",),
            )

    def test_the_probe_environment_carries_no_arbitrary_variable(self) -> None:
        from comfyui_h3_context.adapters.prompt_model_transport import _probe_environment

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "x", "PATH": "/usr/bin"}, clear=True):
            environment = _probe_environment()
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(set(environment), {"PATH"})


class StubHandler(BaseHTTPRequestHandler):
    """A loopback stub that answers exactly the two paths the Ollama family is allowed to use."""

    def log_message(self, *_: object) -> None:
        return

    def _send(self, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        if self.path == "/api/tags":
            self._send(ollama_tags())
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path == "/api/chat":
            self._send(OLLAMA_CHAT)
            return
        self.send_error(404)


class LoopbackStubServerTests(unittest.TestCase):
    """One end-to-end run over a real loopback socket, so the client is not only tested by proxy."""

    def test_a_full_session_completes_against_a_loopback_stub(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            destination = admit_egress_destination(
                PromptModelFamily.OLLAMA, f"http://127.0.0.1:{port}"
            )
            exchange = LoopbackJsonExchange(destination, timeout_seconds=10.0)
            request = session_request()
            result = run_prompt_model_session(request, exchange)
            self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.OK)
            assert result.answer is not None
            self.assertEqual(result.answer.text, "a quiet street, low sun")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_nothing_is_listening_is_a_distinct_outcome(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        port = server.server_address[1]
        server.server_close()
        destination = admit_egress_destination(PromptModelFamily.OLLAMA, f"http://127.0.0.1:{port}")
        exchange = LoopbackJsonExchange(destination, timeout_seconds=2.0)
        result = run_prompt_model_session(session_request(), exchange)
        self.assertIsNone(result.answer)
        # Which of refused/timeout/transport the host reports for a closed local port is a platform
        # detail; that it is one of the typed transport failures and never an answer is not.
        self.assertIn(
            result.outcome.outcome_id,
            {
                PromptModelOutcomeId.BACKEND_ABSENT,
                PromptModelOutcomeId.TRANSPORT,
                PromptModelOutcomeId.TIMEOUT,
            },
        )


class BoundedJsonDecodingTests(unittest.TestCase):
    """A response that would recurse the parser is refused before the parser sees it.

    `json.loads` raises `RecursionError` on a deeply nested document -- a `RuntimeError`, not a
    `ValueError` -- so it slips past every `JSONDecodeError` handler and escapes the typed-outcome
    contract entirely. A few hundred bytes is enough, well inside the response-size cap, so the
    size bound does not imply this one.
    """

    def test_the_size_bound_does_not_imply_the_depth_bound(self) -> None:
        payload = ("[" * 100_000).encode("utf-8")
        self.assertLess(len(payload), MAX_PROMPT_MODEL_RESPONSE_BYTES)
        with self.assertRaises(PromptModelTransportError) as caught:
            decode_bounded_json(payload)
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)
        self.assertEqual(caught.exception.detail, "json_depth")

    def test_the_shapes_this_lane_actually_receives_are_far_inside_the_bound(self) -> None:
        listing = json.dumps({"models": [{"name": "m", "details": {"format": "gguf"}}]}).encode(
            "utf-8"
        )
        decoded_listing = decode_bounded_json(listing)
        assert isinstance(decoded_listing, dict)
        self.assertEqual(decoded_listing["models"][0]["name"], "m")
        # The bound is generous: a document an order of magnitude deeper than anything this lane
        # receives still decodes, so the guard cannot start refusing real answers.
        at_the_limit = ("[" * MAX_JSON_NESTING_DEPTH + "]" * MAX_JSON_NESTING_DEPTH).encode("utf-8")
        decoded = decode_bounded_json(at_the_limit)
        for _ in range(MAX_JSON_NESTING_DEPTH - 1):
            assert isinstance(decoded, list)
            decoded = decoded[0]
        self.assertEqual(decoded, [])

        one_deeper = (
            "[" * (MAX_JSON_NESTING_DEPTH + 1) + "]" * (MAX_JSON_NESTING_DEPTH + 1)
        ).encode("utf-8")
        with self.assertRaises(PromptModelTransportError):
            decode_bounded_json(one_deeper)

    def test_brackets_inside_a_string_are_prose_not_structure(self) -> None:
        """A licence or a draft may contain any number of brackets; counting them would refuse
        perfectly valid answers."""

        payload = json.dumps({"license": "[" * 5_000}).encode("utf-8")
        decoded = decode_bounded_json(payload)
        assert isinstance(decoded, dict)
        self.assertEqual(len(decoded["license"]), 5_000)

    def test_a_deep_answer_reaches_the_caller_as_a_typed_outcome_not_a_crash(self) -> None:
        """The property that matters: no untyped exception escapes the session."""

        class DeepExchange:
            def request(
                self,
                method: str,
                path: str,
                payload: object = None,
                *,
                timeout_seconds: float | None = None,
            ) -> Mapping[str, object]:
                del method, path, payload, timeout_seconds
                decoded = decode_bounded_json(("[" * 100_000).encode("utf-8"))
                assert isinstance(decoded, Mapping)
                return decoded

        result = run_prompt_model_session(session_request(), DeepExchange())
        self.assertIsNone(result.answer)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)


if __name__ == "__main__":
    unittest.main()
