"""M22-04 consented remote prompt-model provider tests."""

from __future__ import annotations

import copy
import gc
import json
import pickle
import unittest
from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError, replace
from datetime import date
from typing import Any

from historical_prompt_model_fixtures import (
    load_historical_direct_call_catalog as load_prompt_model_catalog,
)

from comfyui_h3_context.adapters.prompt_model_transport import (
    MAX_REMOTE_RESPONSE_BYTES,
    REMOTE_PROMPT_PATHS,
    PromptModelTransportError,
    RemoteExchangeMetrics,
    RemoteHttpsExchange,
    RemoteSessionResult,
    _resolve_pinned_address,
    run_remote_prompt_model_session,
)
from comfyui_h3_context.core.prompt_model_budget import (
    ContextProfileLadder,
    ContextProfileStep,
    PromptModelWorkload,
    plan_prompt_model_request,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PROMPT_MODEL_OUTCOME_IDS,
    EgressRejection,
    PromptModelCapabilities,
    PromptModelContractError,
    PromptModelEgressError,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelQualificationState,
    admit_egress_destination,
    build_prompt_model_capabilities,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.prompt_model_session import (
    PromptModelMessage,
    PromptModelRole,
    PromptModelSessionRequest,
)
from comfyui_h3_context.core.provider_setup import ProviderConsentStatus
from comfyui_h3_context.core.remote_prompt_model import (
    REMOTE_FAMILY,
    SAFE_UPSTREAM_ERROR_CODES,
    RemoteConsentLedger,
    RemoteConsentRecord,
    RemoteRefusal,
    RemoteTransmissionDecision,
    RemoteUsageReceipt,
    RuntimeCredential,
    UpstreamError,
    admit_remote_transmission,
    map_remote_outcome,
    remote_family_is_enabled_by_default,
    scrub_upstream_error,
)
from comfyui_h3_context.core.remote_provider_policy import (
    policy_for_profile,
)

REMOTE_PROFILE = next(
    item
    for item in load_prompt_model_catalog().profiles
    if item.profile_id == "openai.gpt_5_6_terra.remote"
)
POLICY = policy_for_profile(REMOTE_PROFILE)
TEST_ON_DATE = date(2026, 8, 23)
PROFILE_ID = REMOTE_PROFILE.profile_id
MODEL_ID = REMOTE_PROFILE.model_id
ENDPOINT = REMOTE_PROFILE.endpoint
# Assembled from parts so a secret scanner does not read the fixture as a real credential.
EMBEDDED_CREDENTIAL_ENDPOINT = "https://" + "user" + ":" + "pw" + "@api.example.com/v1"
SECRET = "sk-" + "T" * 40  # pragma: allowlist secret
LADDER = ContextProfileLadder(
    steps=(ContextProfileStep(context_tokens=16_384, memory_bytes=1024**3),)
)


def _run_on_test_date(*args: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("on_date", TEST_ON_DATE)
    return run_remote_prompt_model_session(*args, **kwargs)


def capabilities(*, media: tuple[str, ...] = ("text",)) -> PromptModelCapabilities:
    return build_prompt_model_capabilities(
        {
            "family": REMOTE_FAMILY.value,
            "accepted_media": list(media),
            "provider_managed_context": True,
            "provider_managed_kv_cache": True,
            "max_request_bytes": 262_144,
            "max_context_tokens": 32_768,
            "max_output_tokens": 4_096,
            "streaming": False,
            "local_only": False,
            "requires_credential": True,
        }
    )


def profile(*, capabilities: PromptModelCapabilities | None = None) -> PromptModelProfile:
    if capabilities is None:
        return REMOTE_PROFILE
    return replace(REMOTE_PROFILE, capabilities=capabilities)


def qualified_remote_profile() -> PromptModelProfile:
    from historical_prompt_model_fixtures import historical_remote_evidence

    evidence = historical_remote_evidence(REMOTE_PROFILE.profile_id)
    return replace(
        REMOTE_PROFILE,
        qualification_state=PromptModelQualificationState.QUALIFIED,
        qualification_evidence=evidence,
    )


def ollama_profile() -> PromptModelProfile:
    """A profile from another family, so the gate is proven keyed on the family, not on a name."""

    return replace(
        REMOTE_PROFILE,
        family=PromptModelFamily.OLLAMA,
        # A qualification belongs to the identity that earned it, so it cannot survive the family
        # being rewritten underneath it -- the profile refuses to be constructed at all. Dropping it
        # here keeps this helper about the one thing it is for: a row of the wrong family.
        qualification_state=PromptModelQualificationState.CATALOG_ONLY,
        qualification_evidence=None,
        endpoint="http://127.0.0.1:11434",
        capabilities=build_prompt_model_capabilities(
            {
                "family": "ollama",
                "accepted_media": ["text"],
                "provider_managed_context": True,
                "provider_managed_kv_cache": True,
                "max_request_bytes": 1024,
                "max_context_tokens": 1024,
                "max_output_tokens": 64,
                "streaming": False,
                "local_only": True,
                "requires_credential": False,
            }
        ),
    )


def session_request(
    *, images: tuple[str, ...] = (), media: tuple[str, ...] = ("text",)
) -> PromptModelSessionRequest:
    pinned = profile(capabilities=capabilities(media=media))
    destination = admit_egress_destination(REMOTE_FAMILY, ENDPOINT)
    decision = plan_prompt_model_request(
        capabilities=pinned.capabilities,
        ladder=LADDER,
        workload=PromptModelWorkload(
            characters=800,
            wide_characters=0,
            messages=2,
            visual_inputs=len(images),
            request_bytes=2_048,
            requested_output_tokens=128,
        ),
        requested_step=0,
        available_memory_bytes=8 * 1024**3,
    )
    assert decision.plan is not None
    return PromptModelSessionRequest(
        profile=pinned,
        destination=destination,
        plan=decision.plan,
        messages=(
            PromptModelMessage(role=PromptModelRole.SYSTEM, text="be exact"),
            PromptModelMessage(role=PromptModelRole.USER, text="a rainy alley at night"),
        ),
        image_payloads=images,
    )


def granted(**overrides: object) -> RemoteConsentRecord:
    values: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "status": ProviderConsentStatus.GRANTED,
        "network_permitted": True,
        "media_upload_consented": False,
    }
    values.update(overrides)
    return RemoteConsentRecord(**values)  # type: ignore[arg-type]


MODELS_BODY = {"data": [{"id": MODEL_ID}]}
CHAT_BODY = {
    "model": MODEL_ID,
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "wet asphalt, sodium light"},
        }
    ],
    "usage": {"prompt_tokens": 310, "completion_tokens": 42},
}


SCRIPTED_METRICS = RemoteExchangeMetrics(
    http_status=200,
    request_bytes=128,
    response_bytes=256,
    prompt_tokens=310,
    completion_tokens=42,
    usage_present=True,
)


class FakeRemoteExchange:
    """Records every call so a test can prove nothing was transmitted."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._raises = raises
        self.metrics: Any = None

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        self.calls.append((method, path))
        if path == "/v1/models":
            return MODELS_BODY
        if self._raises is not None:
            raise self._raises
        self.metrics = SCRIPTED_METRICS
        return CHAT_BODY


class StubResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


class StubConnection:
    """Stands in for an HTTPS connection so no test contacts a real provider."""

    last_headers: dict[str, str] = {}

    def __init__(self, status: int = 200, body: object = None) -> None:
        self._status = status
        self._body = json.dumps(body if body is not None else CHAT_BODY).encode("utf-8")
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        StubConnection.last_headers = dict(headers or {})

    def getresponse(self) -> StubResponse:
        return StubResponse(self._status, self._body)

    def close(self) -> None:
        self.closed = True


def exchange_with(status: int = 200, body: object = None) -> RemoteHttpsExchange:
    return RemoteHttpsExchange(
        admit_egress_destination(REMOTE_FAMILY, ENDPOINT),
        RuntimeCredential(SECRET),
        policy=POLICY,
        connection_factory=lambda host, port, timeout: StubConnection(status, body),
    )


class DefaultDisabledTests(unittest.TestCase):
    def test_no_remote_family_is_enabled_or_pinned_by_default(self) -> None:
        self.assertFalse(remote_family_is_enabled_by_default())
        catalog = load_prompt_model_catalog()
        self.assertEqual(
            {profile.profile_id for profile in catalog.profiles},
            {
                "gemini.gemini_3_7_flash.remote",
                "ollama.qwen3_8_27b.local",
                "openai.gpt_5_6_terra.remote",
                "anthropic.claude_sonnet_4_6.remote",
            },
        )
        self.assertIsNone(catalog.default_profile_id)
        self.assertEqual(RemoteConsentLedger().granted_profile_ids, ())

    def test_a_consent_record_cannot_claim_the_decision_was_unnecessary(self) -> None:
        with self.assertRaises(PromptModelContractError) as caught:
            granted(status=ProviderConsentStatus.NOT_REQUIRED)
        self.assertEqual(caught.exception.code, "consent_status")


class ConsentGateTests(unittest.TestCase):
    def admit(self, **overrides: object) -> RemoteTransmissionDecision:
        values: dict[str, object] = {
            "profile": profile(),
            "selected_profile_id": PROFILE_ID,
            "consent": granted(),
            "credential": RuntimeCredential(SECRET),
            "carries_media": False,
        }
        values.update(overrides)
        return admit_remote_transmission(**values)

    def test_all_four_conditions_together_admit_a_transmission(self) -> None:
        decision = self.admit()
        self.assertTrue(decision.admitted)
        self.assertIs(decision.outcome.outcome_id, PromptModelOutcomeId.OK)

    def test_every_refusal_class_is_reachable_and_distinct(self) -> None:
        cases = (
            (RemoteRefusal.WRONG_FAMILY, {"profile": ollama_profile()}),
            (RemoteRefusal.NOT_SELECTED, {"selected_profile_id": "something.else"}),
            (RemoteRefusal.CONSENT_MISSING, {"consent": None}),
            (
                RemoteRefusal.CONSENT_REVOKED,
                {"consent": granted(status=ProviderConsentStatus.DENIED, network_permitted=False)},
            ),
            (RemoteRefusal.NETWORK_NOT_PERMITTED, {"consent": granted(network_permitted=False)}),
            (RemoteRefusal.UPLOAD_NOT_CONSENTED, {"carries_media": True}),
            (RemoteRefusal.CREDENTIAL_ABSENT, {"credential": None}),
        )
        seen: set[RemoteRefusal] = set()
        for refusal, overrides in cases:
            with self.subTest(refusal=refusal):
                decision = self.admit(**overrides)
                self.assertFalse(decision.admitted)
                self.assertIs(decision.refusal, refusal)
                self.assertNotIn(refusal, seen)
                seen.add(refusal)
        self.assertEqual(seen, set(RemoteRefusal))

    def test_media_is_admitted_only_with_upload_consent(self) -> None:
        self.assertFalse(self.admit(carries_media=True).admitted)
        self.assertTrue(
            self.admit(carries_media=True, consent=granted(media_upload_consented=True)).admitted
        )

    def test_consent_granted_for_another_profile_does_not_transfer(self) -> None:
        decision = self.admit(consent=granted(profile_id="remote.other"))
        self.assertIs(decision.refusal, RemoteRefusal.NOT_SELECTED)


class ConsentLedgerTests(unittest.TestCase):
    def test_a_revocation_takes_effect_on_the_next_transmission(self) -> None:
        ledger = RemoteConsentLedger()
        ledger.grant(
            PROFILE_ID,
            network_permitted=True,
            media_upload_consented=False,
        )
        request = session_request()
        exchange = FakeRemoteExchange()
        first = _run_on_test_date(
            request,
            exchange,
            selected_profile_id=PROFILE_ID,
            consent=ledger.record_for(PROFILE_ID),
            credential=RuntimeCredential(SECRET),
        )
        self.assertTrue(first.completed)

        ledger.revoke(PROFILE_ID)
        exchange_after = FakeRemoteExchange()
        second = _run_on_test_date(
            request,
            exchange_after,
            selected_profile_id=PROFILE_ID,
            consent=ledger.record_for(PROFILE_ID),
            credential=RuntimeCredential(SECRET),
        )
        self.assertFalse(second.completed)
        self.assertIs(second.outcome.outcome_id, PromptModelOutcomeId.CONSENT_REVOKED)
        self.assertEqual(exchange_after.calls, [])
        self.assertIsNone(second.receipt)

    def test_each_decision_increments_a_revision(self) -> None:
        ledger = RemoteConsentLedger()
        first = ledger.grant(
            PROFILE_ID,
            network_permitted=True,
            media_upload_consented=False,
        )
        revoked = ledger.revoke(PROFILE_ID)
        regranted = ledger.grant(
            PROFILE_ID,
            network_permitted=True,
            media_upload_consented=True,
        )
        self.assertEqual((first.revision, revoked.revision, regranted.revision), (1, 2, 3))
        self.assertEqual(ledger.granted_profile_ids, (PROFILE_ID,))

    def test_nothing_in_the_ledger_is_persisted(self) -> None:
        ledger = RemoteConsentLedger()
        ledger.grant(
            PROFILE_ID,
            network_permitted=True,
            media_upload_consented=True,
        )
        self.assertEqual(RemoteConsentLedger().granted_profile_ids, ())
        self.assertFalse(hasattr(ledger, "to_wire"))


class RuntimeCredentialTests(unittest.TestCase):
    def test_the_secret_never_becomes_data(self) -> None:
        credential = RuntimeCredential(SECRET)
        self.assertNotIn(SECRET, repr(credential))
        self.assertNotIn(SECRET, str(credential))
        self.assertNotIn(SECRET, f"{credential}")
        self.assertNotIn(SECRET, format(credential))
        operations: tuple[Callable[[], object], ...] = (
            lambda: pickle.dumps(credential),
            lambda: copy.copy(credential),
            lambda: copy.deepcopy(credential),
            lambda: hash(credential),
            lambda: credential == RuntimeCredential(SECRET),
            lambda: list(credential),
        )
        for index, operation in enumerate(operations):
            with self.subTest(operation=index):
                with self.assertRaises(PromptModelContractError):
                    operation()

    def test_the_documented_boundary_is_accidental_disclosure_not_introspection(self) -> None:
        # The distinct review of 2026-08-20 recovered the secret through `gc.get_referents`. That
        # is the same privilege at which the slot could simply be read, so the class does not claim
        # to defend it; this test pins the boundary as documented so the claim cannot quietly widen.
        credential = RuntimeCredential(SECRET)
        self.assertIn(SECRET, [item for item in gc.get_referents(credential)])
        self.assertIn("cannot be in this language", RuntimeCredential.__doc__ or "")
        self.assertNotIn("nothing that can be walked back", RuntimeCredential.__doc__ or "")

    def test_only_the_session_source_and_transport_exit_are_exposed(self) -> None:
        credential = RuntimeCredential(SECRET)
        self.assertFalse(hasattr(credential, "last_four"))
        self.assertEqual(credential.source.value, "session_only")
        self.assertTrue(credential.authorization_header().startswith("Bearer "))

    def test_a_malformed_secret_is_refused(self) -> None:
        for value in ("", "x" * 4_096, "with\nnewline", None, 42):
            with self.subTest(value=str(value)[:16]):
                with self.assertRaises(PromptModelContractError):
                    RuntimeCredential(value)  # type: ignore[arg-type]

    def test_no_credential_reaches_the_receipt_or_the_wire(self) -> None:
        receipt = RemoteUsageReceipt(
            profile_id=PROFILE_ID,
            host="api.example.com",
            outcome_id=PromptModelOutcomeId.OK,
            http_status=200,
            request_bytes=128,
            response_bytes=256,
            prompt_tokens=310,
            completion_tokens=42,
            duration_ms=180,
        )
        wire = json.dumps(receipt.to_wire())
        self.assertNotIn(SECRET, wire)
        self.assertNotIn(SECRET[:8], wire)
        self.assertNotIn(SECRET[-4:], wire)
        self.assertEqual(receipt.credential_last_four, "")

        with self.assertRaises(PromptModelContractError):
            RemoteUsageReceipt(
                profile_id=PROFILE_ID,
                host="api.example.com",
                outcome_id=PromptModelOutcomeId.OK,
                http_status=200,
                request_bytes=0,
                response_bytes=0,
                prompt_tokens=0,
                completion_tokens=0,
                duration_ms=0,
                credential_last_four=SECRET[-4:],
            )


class UpstreamErrorTests(unittest.TestCase):
    def test_only_the_declared_subset_survives_a_scrub(self) -> None:
        error = scrub_upstream_error(
            429,
            {
                "error": {
                    "code": "insufficient_quota",
                    "message": "You exceeded your current quota",
                    "param": "billing",
                    "internal_trace": "srv-42.internal.example.com/tokens/abc",
                }
            },
        )
        self.assertEqual(error.code, "insufficient_quota")
        wire = error.to_wire()
        self.assertEqual(set(wire), {"schema", "status", "code", "untrusted_provider_detail"})
        self.assertNotIn("srv-42", json.dumps(wire))
        self.assertNotIn("param", json.dumps(wire))

    def test_an_unrecognised_code_is_dropped_rather_than_forwarded(self) -> None:
        error = scrub_upstream_error(400, {"error": {"code": "some_new_code"}})
        self.assertIsNone(error.code)
        with self.assertRaises(PromptModelContractError):
            UpstreamError(status=400, code="some_new_code", detail=None)

    def test_upstream_text_is_marked_untrusted_and_scrubbed(self) -> None:
        error = scrub_upstream_error(
            401, {"error": {"message": "Incorrect API key provided: " + SECRET}}
        )
        assert error.detail is not None
        self.assertNotIn(SECRET, error.detail.text)
        detail_wire = error.to_wire()["untrusted_provider_detail"]
        assert isinstance(detail_wire, dict)
        self.assertEqual(detail_wire["trust"], "untrusted")
        self.assertIs(detail_wire["renderable_as_product_copy"], False)

    def test_quota_and_rate_limit_stay_separate_on_the_same_status(self) -> None:
        quota = scrub_upstream_error(429, {"error": {"code": "insufficient_quota"}})
        limited = scrub_upstream_error(429, {"error": {"code": "rate_limit_exceeded"}})
        bare = scrub_upstream_error(429, {})
        self.assertIs(map_remote_outcome(quota), PromptModelOutcomeId.QUOTA)
        self.assertIs(map_remote_outcome(limited), PromptModelOutcomeId.RATE_LIMITED)
        self.assertIs(map_remote_outcome(bare), PromptModelOutcomeId.RATE_LIMITED)

    def test_every_declared_status_maps_to_a_distinct_outcome(self) -> None:
        expected = {
            401: PromptModelOutcomeId.AUTHENTICATION,
            402: PromptModelOutcomeId.QUOTA,
            403: PromptModelOutcomeId.PERMISSION_DENIED,
            404: PromptModelOutcomeId.MODEL_MISSING,
            408: PromptModelOutcomeId.TIMEOUT,
            413: PromptModelOutcomeId.REQUEST_TOO_LARGE,
            415: PromptModelOutcomeId.UNSUPPORTED_MEDIA,
            500: PromptModelOutcomeId.PROVIDER_ERROR,
            302: PromptModelOutcomeId.REDIRECT_REFUSED,
        }
        for status, outcome_id in expected.items():
            with self.subTest(status=status):
                self.assertIs(map_remote_outcome(scrub_upstream_error(status, None)), outcome_id)

    def test_recognised_codes_override_a_generic_status(self) -> None:
        for code, outcome_id in (
            ("content_policy_violation", PromptModelOutcomeId.MODERATED),
            ("context_length_exceeded", PromptModelOutcomeId.CONTEXT_EXCEEDED),
            ("invalid_api_key", PromptModelOutcomeId.AUTHENTICATION),
            ("model_not_found", PromptModelOutcomeId.MODEL_MISSING),
        ):
            with self.subTest(code=code):
                error = scrub_upstream_error(400, {"error": {"code": code}})
                self.assertIs(map_remote_outcome(error), outcome_id)
        self.assertIn("content_policy_violation", SAFE_UPSTREAM_ERROR_CODES)


class RemoteChokepointTests(unittest.TestCase):
    """AC-M22-04-03, restated against the remote family rather than inherited from M22-01."""

    def test_the_chokepoint_refuses_each_class_for_a_remote_endpoint(self) -> None:
        cases = (
            (EgressRejection.PLAINTEXT_PUBLIC, "http://api.example.com/v1"),
            (EgressRejection.EMBEDDED_CREDENTIAL, EMBEDDED_CREDENTIAL_ENDPOINT),
            (EgressRejection.METADATA_HOST, "https://169.254.169.254/v1"),
            (EgressRejection.LINK_LOCAL, "https://169.254.10.20/v1"),
            (EgressRejection.DESTINATION_CLASS, "https://127.0.0.1/v1"),
            (EgressRejection.QUERY, "https://api.example.com/v1?key=abc"),
            (EgressRejection.FRAGMENT, "https://api.example.com/v1#part"),
            (EgressRejection.SCHEME, "ftp://api.example.com/v1"),
            (EgressRejection.PATH_TRAVERSAL, "https://api.example.com/v1/../admin"),
            (EgressRejection.MALFORMED, "https://api.example.com/v1 "),
        )
        seen: set[EgressRejection] = set()
        for rejection, endpoint in cases:
            with self.subTest(rejection=rejection):
                with self.assertRaises(PromptModelEgressError) as caught:
                    admit_egress_destination(REMOTE_FAMILY, endpoint)
                self.assertIs(caught.exception.rejection, rejection)
                seen.add(rejection)
        self.assertEqual(len(seen), len(cases))

    def test_no_transport_can_be_built_from_a_string(self) -> None:
        with self.assertRaises(PromptModelTransportError):
            RemoteHttpsExchange(ENDPOINT, RuntimeCredential(SECRET))  # type: ignore[arg-type]


class RemoteExchangeTests(unittest.TestCase):
    def test_a_non_remote_or_plaintext_destination_is_refused(self) -> None:
        loopback = admit_egress_destination(PromptModelFamily.OLLAMA, "http://127.0.0.1:11434")
        with self.assertRaises(PromptModelTransportError) as caught:
            RemoteHttpsExchange(loopback, RuntimeCredential(SECRET))
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.CAPABILITY_MISMATCH)
        with self.assertRaises(PromptModelTransportError):
            RemoteHttpsExchange(
                admit_egress_destination(REMOTE_FAMILY, "http://10.1.2.3:8000/v1"),
                RuntimeCredential(SECRET),
            )

    def test_a_missing_credential_is_refused_at_construction(self) -> None:
        with self.assertRaises(PromptModelTransportError) as caught:
            RemoteHttpsExchange(
                admit_egress_destination(REMOTE_FAMILY, ENDPOINT),
                None,  # type: ignore[arg-type]
            )
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.AUTHENTICATION)

    def test_method_and_path_are_closed(self) -> None:
        exchange = exchange_with()
        self.assertEqual(
            REMOTE_PROMPT_PATHS,
            frozenset(
                {
                    "/v1/models",
                    "/v1/chat/completions",
                    "/v1beta/models?pageSize=1000",
                    "/v1beta/openai/chat/completions",
                    "/v1/messages",
                    "/v1/models?limit=1000",
                }
            ),
        )
        for method, path in (("PUT", "/v1/chat/completions"), ("GET", "/api/tags")):
            with self.subTest(method=method, path=path):
                with self.assertRaises(PromptModelTransportError):
                    exchange.request(method, path)

    def test_the_authorization_header_carries_the_credential_and_nothing_else_does(self) -> None:
        exchange = exchange_with()
        body = exchange.request("POST", "/v1/chat/completions", {"model": MODEL_ID})
        self.assertIn("choices", body)
        self.assertEqual(StubConnection.last_headers["Authorization"], "Bearer " + SECRET)
        self.assertEqual(
            set(StubConnection.last_headers), {"Accept", "Content-Type", "Authorization"}
        )

    def test_a_redirect_is_refused_and_the_location_is_never_read(self) -> None:
        exchange = exchange_with(status=302, body={"location": "https://evil.example.com/v1"})
        with self.assertRaises(PromptModelTransportError) as caught:
            exchange.request("POST", "/v1/chat/completions", {"model": MODEL_ID})
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.REDIRECT_REFUSED)
        self.assertEqual(caught.exception.detail, "")

    def test_an_error_status_is_typed_and_its_body_is_scrubbed(self) -> None:
        exchange = exchange_with(
            status=429,
            body={"error": {"code": "insufficient_quota", "message": "no credit", "trace": "x"}},
        )
        with self.assertRaises(PromptModelTransportError) as caught:
            exchange.request("POST", "/v1/chat/completions", {"model": MODEL_ID})
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.QUOTA)
        self.assertEqual(caught.exception.detail, "")

    def test_usage_counts_are_recorded_for_the_receipt(self) -> None:
        exchange = exchange_with()
        exchange.request("POST", "/v1/chat/completions", {"model": MODEL_ID})
        metrics = exchange.metrics
        assert metrics is not None
        self.assertEqual(metrics.http_status, 200)
        self.assertEqual(metrics.prompt_tokens, 310)
        self.assertEqual(metrics.completion_tokens, 42)
        self.assertGreater(metrics.response_bytes, 0)

    def test_the_response_ceiling_is_bounded(self) -> None:
        self.assertEqual(MAX_REMOTE_RESPONSE_BYTES, 2_000_000)
        with self.assertRaises(PromptModelTransportError):
            RemoteHttpsExchange(
                admit_egress_destination(REMOTE_FAMILY, ENDPOINT),
                RuntimeCredential(SECRET),
                max_response_bytes=MAX_REMOTE_RESPONSE_BYTES + 1,
            )


class AddressPinningTests(unittest.TestCase):
    """No test here touches DNS: the resolver is injected, so the policy is what is under test."""

    @staticmethod
    def answering(*addresses: str) -> Callable[..., list[Any]]:
        return lambda *_args, **_kwargs: [(0, 0, 0, "", (address, 443)) for address in addresses]

    def test_every_address_a_name_resolves_to_must_be_globally_routable(self) -> None:
        for address in (
            "127.0.0.1",
            "10.0.0.5",
            "192.168.1.9",
            "169.254.169.254",
            "::1",
            "224.0.0.1",
        ):
            with self.subTest(address=address):
                with self.assertRaises(PromptModelTransportError) as caught:
                    _resolve_pinned_address("api.example.com", getaddrinfo=self.answering(address))
                self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.EGRESS_REFUSED)
                self.assertEqual(caught.exception.detail, "")

    def test_an_ipv6_literal_embedding_a_private_address_is_refused(self) -> None:
        # The distinct review of 2026-08-20 found these: `is_global` reads the deprecated
        # `::a.b.c.d` form as an ordinary global address because its high bits are zero, so the
        # embedded address has to be judged rather than the wrapper it arrived in.
        embedded = (
            "::7f00:1",
            "::a9fe:a9fe",
            "::c0a8:1",
            "::a00:1",
            "::ffff:169.254.169.254",
            "::ffff:10.0.0.1",
            "::ffff:127.0.0.1",
        )
        for address in embedded:
            with self.subTest(address=address):
                with self.assertRaises(PromptModelTransportError) as caught:
                    _resolve_pinned_address("api.example.com", getaddrinfo=self.answering(address))
                self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.EGRESS_REFUSED)

    def test_an_ipv6_literal_embedding_a_public_address_is_still_admitted(self) -> None:
        for address in ("::ffff:93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"):
            with self.subTest(address=address):
                self.assertEqual(
                    _resolve_pinned_address("api.example.com", getaddrinfo=self.answering(address)),
                    address,
                )

    def test_one_bad_address_among_good_ones_refuses_the_whole_name(self) -> None:
        with self.assertRaises(PromptModelTransportError) as caught:
            _resolve_pinned_address(
                "api.example.com", getaddrinfo=self.answering("93.184.216.34", "127.0.0.1")
            )
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.EGRESS_REFUSED)

    def test_an_unresolvable_name_is_its_own_outcome(self) -> None:
        def raise_oserror(*_args: object, **_kwargs: object) -> list[Any]:
            raise OSError("no such host")

        with self.assertRaises(PromptModelTransportError) as caught:
            _resolve_pinned_address("does-not-resolve.example", getaddrinfo=raise_oserror)
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.DESTINATION_UNRESOLVED)

    def test_an_empty_answer_is_unresolved_rather_than_admitted(self) -> None:
        with self.assertRaises(PromptModelTransportError) as caught:
            _resolve_pinned_address("api.example.com", getaddrinfo=self.answering())
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.DESTINATION_UNRESOLVED)

    def test_a_public_address_is_returned_for_pinning(self) -> None:
        self.assertEqual(
            _resolve_pinned_address("api.example.com", getaddrinfo=self.answering("93.184.216.34")),
            "93.184.216.34",
        )


class PerTransmissionConsentTests(unittest.TestCase):
    """The distinct review of 2026-08-20 found the gate ran once per session, not per request."""

    def test_a_revocation_between_the_identity_call_and_the_prompt_call_stops_the_prompt(
        self,
    ) -> None:
        ledger = RemoteConsentLedger()
        ledger.grant(
            PROFILE_ID,
            network_permitted=True,
            media_upload_consented=False,
        )

        class RevokingExchange(FakeRemoteExchange):
            """Revokes while the identity call is 'in flight', which is the real window."""

            def request(self, *args: object, **kwargs: object) -> Mapping[str, object]:
                body = super().request(*args, **kwargs)  # type: ignore[arg-type]
                ledger.revoke(PROFILE_ID)
                return body

        exchange = RevokingExchange()
        result = _run_on_test_date(
            session_request(),
            exchange,
            selected_profile_id=PROFILE_ID,
            consent=lambda: ledger.record_for(PROFILE_ID),
            credential=RuntimeCredential(SECRET),
        )
        self.assertFalse(result.completed)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.CONSENT_REVOKED)
        # The identity call happened; the prompt call did not.
        self.assertEqual(exchange.calls, [("GET", "/v1/models")])

    def test_the_prompt_call_is_gated_even_when_the_identity_call_was_admitted(self) -> None:
        seen: list[int] = []

        def consent() -> RemoteConsentRecord:
            seen.append(len(seen))
            return granted()

        exchange = FakeRemoteExchange()
        result = _run_on_test_date(
            session_request(),
            exchange,
            selected_profile_id=PROFILE_ID,
            consent=consent,
            credential=RuntimeCredential(SECRET),
        )
        self.assertTrue(result.completed)
        # Session entry, the identity call, and the prompt call: three reads, not one.
        self.assertEqual(len(seen), 3)
        self.assertEqual(exchange.calls, [("GET", "/v1/models"), ("POST", "/v1/chat/completions")])

    def test_reaffirming_network_consent_between_calls_preserves_permission(self) -> None:
        ledger = RemoteConsentLedger()
        ledger.grant(
            PROFILE_ID,
            network_permitted=True,
            media_upload_consented=False,
        )

        class ReaffirmingConsentExchange(FakeRemoteExchange):
            def request(self, *args: object, **kwargs: object) -> Mapping[str, object]:
                body = super().request(*args, **kwargs)  # type: ignore[arg-type]
                ledger.grant(
                    PROFILE_ID,
                    network_permitted=True,
                    media_upload_consented=False,
                )
                return body

        exchange = ReaffirmingConsentExchange()
        result = _run_on_test_date(
            session_request(),
            exchange,
            selected_profile_id=PROFILE_ID,
            consent=lambda: ledger.record_for(PROFILE_ID),
            credential=RuntimeCredential(SECRET),
            on_date=date(2026, 8, 23),
        )

        self.assertTrue(result.completed)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.OK)
        self.assertEqual(exchange.calls, [("GET", "/v1/models"), ("POST", "/v1/chat/completions")])

    def test_a_bare_record_is_still_accepted_and_still_gates(self) -> None:
        exchange = FakeRemoteExchange()
        result = _run_on_test_date(
            session_request(),
            exchange,
            selected_profile_id=PROFILE_ID,
            consent=granted(network_permitted=False),
            credential=RuntimeCredential(SECRET),
        )
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.NETWORK_NOT_PERMITTED)
        self.assertEqual(exchange.calls, [])


class RemoteSessionTests(unittest.TestCase):
    def test_historical_qualification_cannot_borrow_current_connection_authority(self) -> None:
        exchange = FakeRemoteExchange()
        with self.assertRaises(PromptModelContractError):
            policy_for_profile(qualified_remote_profile())
        self.assertEqual(exchange.calls, [])

    def test_a_consented_session_completes_and_receipts_content_free(self) -> None:
        exchange = FakeRemoteExchange()
        result = _run_on_test_date(
            session_request(),
            exchange,
            selected_profile_id=PROFILE_ID,
            consent=granted(),
            credential=RuntimeCredential(SECRET),
            clock=iter([1.0, 1.25]).__next__,
        )
        self.assertIsInstance(result, RemoteSessionResult)
        self.assertTrue(result.completed)
        assert result.answer is not None
        self.assertEqual(result.answer.text, "wet asphalt, sodium light")
        assert result.receipt is not None
        wire = json.dumps(result.receipt.to_wire())
        self.assertNotIn("rainy alley", wire)
        self.assertNotIn("wet asphalt", wire)
        self.assertNotIn(SECRET, wire)
        self.assertEqual(result.receipt.duration_ms, 250)
        self.assertEqual(result.receipt.prompt_tokens, 310)

    def test_counts_are_taken_only_from_the_declared_metrics_type(self) -> None:
        class ForeignMetrics:
            http_status = 200
            request_bytes = 99_999
            response_bytes = 99_999
            prompt_tokens = 99_999
            completion_tokens = 99_999

        class ForeignMetricsExchange(FakeRemoteExchange):
            def request(self, *args: object, **kwargs: object) -> Mapping[str, object]:
                body = super().request(*args, **kwargs)  # type: ignore[arg-type]
                self.metrics = ForeignMetrics()
                return body

        exchange = ForeignMetricsExchange()
        result = _run_on_test_date(
            session_request(),
            exchange,
            selected_profile_id=PROFILE_ID,
            consent=granted(),
            credential=RuntimeCredential(SECRET),
        )
        assert result.receipt is not None
        # A duck-typed object is not evidence of what was actually transferred.
        self.assertEqual(result.receipt.prompt_tokens, 0)
        self.assertEqual(result.receipt.response_bytes, 0)

    def test_a_refused_session_transmits_nothing_and_writes_no_receipt(self) -> None:
        for overrides in (
            {"consent": None},
            {"consent": granted(network_permitted=False)},
            {"selected_profile_id": "another.profile"},
            {"credential": None},
        ):
            with self.subTest(overrides=sorted(overrides)):
                exchange = FakeRemoteExchange()
                values: dict[str, object] = {
                    "selected_profile_id": PROFILE_ID,
                    "consent": granted(),
                    "credential": RuntimeCredential(SECRET),
                }
                values.update(overrides)
                result = _run_on_test_date(
                    session_request(),
                    exchange,
                    **values,
                )
                self.assertFalse(result.completed)
                self.assertIsNone(result.receipt)
                self.assertEqual(exchange.calls, [])

    def test_media_without_upload_consent_never_reaches_the_transport(self) -> None:
        exchange = FakeRemoteExchange()
        result = _run_on_test_date(
            session_request(images=("aGVsbG8=",), media=("text", "image")),
            exchange,
            selected_profile_id=PROFILE_ID,
            consent=granted(media_upload_consented=False),
            credential=RuntimeCredential(SECRET),
        )
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.UPLOAD_NOT_CONSENTED)
        self.assertEqual(exchange.calls, [])

    def test_a_typed_transport_failure_still_produces_a_receipt(self) -> None:
        exchange = FakeRemoteExchange(
            raises=PromptModelTransportError(PromptModelOutcomeId.RATE_LIMITED, "slow down")
        )
        result = _run_on_test_date(
            session_request(),
            exchange,
            selected_profile_id=PROFILE_ID,
            consent=granted(),
            credential=RuntimeCredential(SECRET),
            clock=iter([0.0, 0.1]).__next__,
        )
        self.assertFalse(result.completed)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.RATE_LIMITED)
        assert result.receipt is not None
        self.assertIs(result.receipt.outcome_id, PromptModelOutcomeId.RATE_LIMITED)
        self.assertEqual(result.receipt.credential_last_four, "")


class RegistryTests(unittest.TestCase):
    def test_the_remote_identifiers_joined_the_single_closed_registry(self) -> None:
        for value in (
            "prompt_model.consent_revoked",
            "prompt_model.network_not_permitted",
            "prompt_model.upload_not_consented",
            "prompt_model.payment_required",
            "prompt_model.permission_denied",
            "prompt_model.rate_limited",
            "prompt_model.redirect_refused",
            "prompt_model.destination_unresolved",
        ):
            self.assertIn(value, PROMPT_MODEL_OUTCOME_IDS)
        self.assertEqual(
            PROMPT_MODEL_OUTCOME_IDS, frozenset(item.value for item in PromptModelOutcomeId)
        )

    def test_a_receipt_is_frozen_and_bounded(self) -> None:
        receipt = RemoteUsageReceipt(
            profile_id=PROFILE_ID,
            host="api.example.com",
            outcome_id=PromptModelOutcomeId.OK,
            http_status=200,
            request_bytes=1,
            response_bytes=1,
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1,
        )
        with self.assertRaises(FrozenInstanceError):
            receipt.http_status = 500  # type: ignore[misc]
        with self.assertRaises(PromptModelContractError):
            RemoteUsageReceipt(
                profile_id=PROFILE_ID,
                host="api.example.com",
                outcome_id=PromptModelOutcomeId.OK,
                http_status=200,
                request_bytes=-1,
                response_bytes=1,
                prompt_tokens=1,
                completion_tokens=1,
                duration_ms=1,
            )


if __name__ == "__main__":
    unittest.main()
