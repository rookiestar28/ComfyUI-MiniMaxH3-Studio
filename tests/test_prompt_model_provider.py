"""M22-01 prompt-model provider contract and single egress chokepoint tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from historical_prompt_model_fixtures import load_historical_catalog as load_prompt_model_catalog

from comfyui_h3_context.core.contracts import ProviderIdentity, ValidationSeverity
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.prompt_model_provider import (
    PROMPT_MODEL_CATALOG_PATH,
    PROMPT_MODEL_CATALOG_V1_SCHEMA,
    PROMPT_MODEL_CATALOG_V2_SCHEMA,
    PROMPT_MODEL_CATALOG_V3_SCHEMA,
    PROMPT_MODEL_CATALOG_V4_SCHEMA,
    PROMPT_MODEL_FAMILY_MATRIX,
    PROMPT_MODEL_OUTCOME_IDS,
    PROMPT_MODEL_PROVIDER_SCHEMA,
    AdmittedDestination,
    EgressRejection,
    PromptModelCapabilities,
    PromptModelContractError,
    PromptModelCredentialRef,
    PromptModelCredentialSource,
    PromptModelDialect,
    PromptModelEgressError,
    PromptModelFamily,
    PromptModelMediaKind,
    PromptModelOutcome,
    PromptModelOutcomeId,
    PromptModelQualificationEvidence,
    PromptModelQualificationState,
    PromptModelRemediation,
    QualifiedIdentityObservation,
    RemotePromptModelQualificationEvidence,
    UntrustedProviderDetail,
    admit_egress_destination,
    admit_prompt_model_execution,
    build_prompt_model_capabilities,
    build_prompt_model_outcome,
    build_qualification_evidence,
    compute_endpoint_fingerprint,
    decode_prompt_model_catalog,
    match_qualification_identity,
    parse_exact_tags_row,
    parse_show_identity,
    route_for_family,
    stamp_readiness_evidence,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PROMPT_MODEL_CATALOG_V5_SCHEMA as PROMPT_MODEL_CATALOG_SCHEMA,
)
from comfyui_h3_context.core.prompt_model_provider import (
    load_prompt_model_catalog as load_current_prompt_model_catalog,
)
from comfyui_h3_context.core.provider_policy import ProviderExecutionPolicy, ProviderPrivacyMode
from comfyui_h3_context.core.provider_setup import (
    ProviderConsentStatus,
    ProviderDestinationClass,
    ProviderLocalBackend,
    ProviderTransferBoundary,
    build_provider_setup,
)

ROOT = Path(__file__).resolve().parents[1]

# Assembled from parts so no single literal here is a high-entropy string. These are the two shapes
# the scrubber targets -- an `sk-` prefixed identifier and a long unbroken alphanumeric run -- as a
# provider might echo them back inside an error body. The test is that neither survives.
SK_SHAPED_SAMPLE = "sk-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "012345"
LONG_RUN_SAMPLE = "abcdefghijklmnopqrstuvwxyz" + "012345"

LOCAL_FAMILIES = (
    PromptModelFamily.IN_PROCESS_GGUF,
    PromptModelFamily.LOOPBACK_SERVER,
    PromptModelFamily.OLLAMA,
)


def capability_mapping(family: PromptModelFamily, **overrides: object) -> dict[str, object]:
    managed = family is not PromptModelFamily.IN_PROCESS_GGUF
    remote = family in {
        PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        PromptModelFamily.REMOTE_ANTHROPIC,
    }
    values: dict[str, object] = {
        "family": family.value,
        "accepted_media": ["text"],
        "provider_managed_context": managed,
        "provider_managed_kv_cache": managed,
        "max_request_bytes": 262_144,
        "max_context_tokens": 32_768,
        "max_output_tokens": 4_096,
        "streaming": False,
        "local_only": not remote,
        "requires_credential": remote,
    }
    values.update(overrides)
    return values


class PromptModelCapabilityTests(unittest.TestCase):
    def test_every_family_has_a_declarable_capability_set(self) -> None:
        for family in PromptModelFamily:
            with self.subTest(family=family):
                declared = build_prompt_model_capabilities(capability_mapping(family))
                self.assertIsInstance(declared, PromptModelCapabilities)
                self.assertIs(declared.family, family)

    def test_under_declared_family_cannot_load(self) -> None:
        for missing in sorted(capability_mapping(PromptModelFamily.OLLAMA)):
            values = capability_mapping(PromptModelFamily.OLLAMA)
            values.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(PromptModelContractError) as caught:
                    build_prompt_model_capabilities(values)
                self.assertEqual(caught.exception.code, "capabilities_incomplete")

    def test_unknown_capability_key_is_refused(self) -> None:
        values = capability_mapping(PromptModelFamily.OLLAMA)
        values["temperature_default"] = 0
        with self.assertRaises(PromptModelContractError) as caught:
            build_prompt_model_capabilities(values)
        self.assertEqual(caught.exception.code, "capabilities_unknown_key")

    def test_text_media_is_mandatory(self) -> None:
        values = capability_mapping(PromptModelFamily.OLLAMA, accepted_media=["image"])
        with self.assertRaises(PromptModelContractError) as caught:
            build_prompt_model_capabilities(values)
        self.assertEqual(caught.exception.code, "capabilities_media")

    def test_media_kinds_are_closed(self) -> None:
        values = capability_mapping(PromptModelFamily.OLLAMA, accepted_media=["text", "hologram"])
        with self.assertRaises(PromptModelContractError):
            build_prompt_model_capabilities(values)
        self.assertEqual(
            {item.value for item in PromptModelMediaKind},
            {"text", "image", "audio", "video"},
        )

    def test_bounds_are_enforced(self) -> None:
        for field, value in (
            ("max_request_bytes", 0),
            ("max_request_bytes", 1 << 40),
            ("max_context_tokens", -1),
            ("max_output_tokens", 0),
            ("max_request_bytes", True),
        ):
            values = capability_mapping(PromptModelFamily.OLLAMA, **{field: value})
            with self.subTest(field=field, value=value):
                with self.assertRaises(PromptModelContractError):
                    build_prompt_model_capabilities(values)

    def test_in_process_family_cannot_claim_provider_managed_state(self) -> None:
        for field in ("provider_managed_context", "provider_managed_kv_cache"):
            values = capability_mapping(PromptModelFamily.IN_PROCESS_GGUF, **{field: True})
            with self.subTest(field=field):
                with self.assertRaises(PromptModelContractError) as caught:
                    build_prompt_model_capabilities(values)
                self.assertEqual(caught.exception.code, "capabilities_managed_state")

    def test_local_only_must_match_the_matrix(self) -> None:
        values = capability_mapping(PromptModelFamily.OLLAMA, local_only=False)
        with self.assertRaises(PromptModelContractError) as caught:
            build_prompt_model_capabilities(values)
        self.assertEqual(caught.exception.code, "capabilities_locality")

    def test_local_family_cannot_require_a_credential(self) -> None:
        for family in LOCAL_FAMILIES:
            values = capability_mapping(family, requires_credential=True)
            with self.subTest(family=family):
                with self.assertRaises(PromptModelContractError) as caught:
                    build_prompt_model_capabilities(values)
                self.assertEqual(caught.exception.code, "capabilities_credential")


class PromptModelMatrixTests(unittest.TestCase):
    def test_matrix_covers_every_family_exactly_once(self) -> None:
        self.assertEqual(set(PROMPT_MODEL_FAMILY_MATRIX), set(PromptModelFamily))

    def test_routes_reuse_the_accepted_provider_vocabulary(self) -> None:
        expected = {
            PromptModelFamily.IN_PROCESS_GGUF: (
                ProviderDestinationClass.IN_PROCESS,
                ProviderTransferBoundary.IN_PROCESS,
                False,
            ),
            PromptModelFamily.LOOPBACK_SERVER: (
                ProviderDestinationClass.LOOPBACK_HTTP,
                ProviderTransferBoundary.LOCAL_SERVER_PROCESS,
                False,
            ),
            PromptModelFamily.OLLAMA: (
                ProviderDestinationClass.LOOPBACK_HTTP,
                ProviderTransferBoundary.OLLAMA_PROCESS,
                False,
            ),
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE: (
                ProviderDestinationClass.INTERNET,
                ProviderTransferBoundary.REMOTE_UPLOAD,
                True,
            ),
        }
        for family, (destination, boundary, consent) in expected.items():
            route = route_for_family(family)
            with self.subTest(family=family):
                self.assertIs(route.destination, destination)
                self.assertIs(route.transfer_boundary, boundary)
                self.assertIs(route.consent_required, consent)
                self.assertTrue(route.preflight_required)

    def test_route_lookup_refuses_a_foreign_value(self) -> None:
        for value in ("ollama", None, ProviderIdentity.LOCAL):
            with self.subTest(value=value):
                with self.assertRaises(PromptModelContractError) as caught:
                    route_for_family(value)
                self.assertEqual(caught.exception.code, "unknown_family")

    def test_remote_family_requires_granted_consent(self) -> None:
        family = PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
        admit_prompt_model_execution(family, ProviderConsentStatus.GRANTED)
        for status in (ProviderConsentStatus.DENIED, ProviderConsentStatus.NOT_REQUIRED):
            with self.subTest(status=status):
                with self.assertRaises(PromptModelContractError) as caught:
                    admit_prompt_model_execution(family, status)
                self.assertEqual(caught.exception.code, "consent_required")

    def test_local_families_refuse_a_consent_claim(self) -> None:
        for family in LOCAL_FAMILIES:
            admit_prompt_model_execution(family, ProviderConsentStatus.NOT_REQUIRED)
            with self.subTest(family=family):
                with self.assertRaises(PromptModelContractError) as caught:
                    admit_prompt_model_execution(family, ProviderConsentStatus.GRANTED)
                self.assertEqual(caught.exception.code, "consent_not_applicable")

    def test_no_default_or_fallback_family_is_expressible(self) -> None:
        source = (ROOT / "comfyui_h3_context" / "core" / "prompt_model_provider.py").read_text(
            encoding="utf-8"
        )
        for token in ("DEFAULT_PROMPT_MODEL", "FALLBACK_FAMILY", "PREFERRED_FAMILY"):
            self.assertNotIn(token, source)


class EgressChokepointTests(unittest.TestCase):
    def test_loopback_endpoint_is_admitted(self) -> None:
        admitted = admit_egress_destination(
            PromptModelFamily.OLLAMA, "http://127.0.0.1:11434/api/generate"
        )
        self.assertIsInstance(admitted, AdmittedDestination)
        self.assertIs(admitted.family, PromptModelFamily.OLLAMA)
        self.assertIs(admitted.destination, ProviderDestinationClass.LOOPBACK_HTTP)
        self.assertEqual(admitted.host, "127.0.0.1")
        self.assertEqual(admitted.port, 11434)
        self.assertEqual(admitted.url, "http://127.0.0.1:11434/api/generate")

    def test_localhost_and_ipv6_loopback_are_admitted(self) -> None:
        for url in (
            "http://localhost:8080/v1/chat/completions",
            "http://[::1]:8080/v1/chat/completions",
        ):
            with self.subTest(url=url):
                admitted = admit_egress_destination(PromptModelFamily.LOOPBACK_SERVER, url)
                self.assertTrue(admitted.loopback)

    def test_remote_https_endpoint_is_admitted(self) -> None:
        admitted = admit_egress_destination(
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE, "https://api.example.com/v1"
        )
        self.assertIs(admitted.destination, ProviderDestinationClass.INTERNET)
        self.assertFalse(admitted.loopback)
        self.assertEqual(admitted.port, 443)

    def test_in_process_family_has_no_egress_at_all(self) -> None:
        with self.assertRaises(PromptModelEgressError) as caught:
            admit_egress_destination(PromptModelFamily.IN_PROCESS_GGUF, "http://127.0.0.1:8080/")
        self.assertIs(caught.exception.rejection, EgressRejection.IN_PROCESS_FAMILY)

    def test_every_rejection_class_is_reachable(self) -> None:
        cases = (
            (EgressRejection.MALFORMED, PromptModelFamily.OLLAMA, "http://[::1/api"),
            (EgressRejection.MALFORMED, PromptModelFamily.OLLAMA, " http://127.0.0.1/api"),
            (EgressRejection.MALFORMED, PromptModelFamily.OLLAMA, "http://127.0.0.1/a\npi"),
            (EgressRejection.SCHEME, PromptModelFamily.OLLAMA, "ftp://127.0.0.1/api"),
            (EgressRejection.SCHEME, PromptModelFamily.OLLAMA, "file:///etc/passwd"),
            (
                EgressRejection.EMBEDDED_CREDENTIAL,
                PromptModelFamily.OLLAMA,
                "http://user:secret@127.0.0.1:11434/api",  # pragma: allowlist secret
            ),
            (EgressRejection.QUERY, PromptModelFamily.OLLAMA, "http://127.0.0.1:11434/api?key=abc"),
            (EgressRejection.FRAGMENT, PromptModelFamily.OLLAMA, "http://127.0.0.1:11434/api#top"),
            (EgressRejection.HOST_MISSING, PromptModelFamily.OLLAMA, "http:///api"),
            (
                EgressRejection.PATH_TRAVERSAL,
                PromptModelFamily.OLLAMA,
                "http://127.0.0.1:11434/api/../../secret",
            ),
            (
                EgressRejection.PLAINTEXT_PUBLIC,
                PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
                "http://api.example.com/v1",
            ),
            (
                EgressRejection.METADATA_HOST,
                PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
                "https://metadata.google.internal/computeMetadata/v1",
            ),
            (
                EgressRejection.METADATA_HOST,
                PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
                "https://169.254.169.254/latest/meta-data",
            ),
            (
                EgressRejection.LINK_LOCAL,
                PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
                "https://169.254.10.9/v1",
            ),
            (
                EgressRejection.DESTINATION_CLASS,
                PromptModelFamily.OLLAMA,
                "http://192.168.1.10:11434/api",
            ),
            (
                EgressRejection.DESTINATION_CLASS,
                PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
                "https://127.0.0.1/v1",
            ),
            (EgressRejection.LENGTH, PromptModelFamily.OLLAMA, "http://127.0.0.1/" + "a" * 3000),
        )
        seen: set[EgressRejection] = set()
        for rejection, family, url in cases:
            with self.subTest(rejection=rejection, url=url[:48]):
                with self.assertRaises(PromptModelEgressError) as caught:
                    admit_egress_destination(family, url)
                self.assertIs(caught.exception.rejection, rejection)
                seen.add(rejection)
        seen.add(EgressRejection.IN_PROCESS_FAMILY)
        self.assertEqual(seen, set(EgressRejection))

    def test_private_lan_endpoint_is_admitted_for_the_remote_family(self) -> None:
        admitted = admit_egress_destination(
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE, "http://10.1.2.3:8000/v1"
        )
        self.assertIs(admitted.destination, ProviderDestinationClass.INTERNET)
        self.assertFalse(admitted.loopback)

    def test_admitted_destination_cannot_be_constructed_directly(self) -> None:
        with self.assertRaises(PromptModelContractError):
            AdmittedDestination(
                family=PromptModelFamily.OLLAMA,
                scheme="http",
                host="127.0.0.1",
                port=11434,
                path="/api",
                destination=ProviderDestinationClass.LOOPBACK_HTTP,
                loopback=True,
            )

    def test_admitted_destination_carries_no_credential_query_or_fragment(self) -> None:
        admitted = admit_egress_destination(
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE, "https://API.Example.COM/v1"
        )
        self.assertEqual(admitted.host, "api.example.com")
        wire = admitted.to_wire()
        self.assertNotIn("?", json.dumps(wire))
        self.assertNotIn("@", json.dumps(wire))
        self.assertEqual(
            set(wire),
            {"family", "scheme", "host", "port", "path", "destination", "loopback"},
        )

    def test_the_metadata_blocklist_is_defined_once(self) -> None:
        package = ROOT / "comfyui_h3_context"
        owners = sorted(
            path.relative_to(ROOT).as_posix()
            for path in package.rglob("*.py")
            if "169.254.169.254" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(owners, ["comfyui_h3_context/core/prompt_model_provider.py"])


class PromptModelOutcomeTests(unittest.TestCase):
    def test_outcome_registry_is_closed_and_matches_the_enum(self) -> None:
        self.assertEqual(
            PROMPT_MODEL_OUTCOME_IDS, frozenset(item.value for item in PromptModelOutcomeId)
        )
        for value in PROMPT_MODEL_OUTCOME_IDS:
            self.assertTrue(value.startswith("prompt_model."))

    def test_every_distinct_failure_class_is_a_separate_identifier(self) -> None:
        required = {
            "prompt_model.cancelled",
            "prompt_model.timeout",
            "prompt_model.authentication",
            "prompt_model.quota",
            "prompt_model.moderated",
            "prompt_model.unsupported_media",
            "prompt_model.egress_refused",
            "prompt_model.consent_required",
            "prompt_model.profile_not_qualified",
        }
        self.assertTrue(required.issubset(PROMPT_MODEL_OUTCOME_IDS))

    def test_outcome_is_renderable_without_any_provider_sentence(self) -> None:
        outcome = build_prompt_model_outcome(
            PromptModelOutcomeId.CONTEXT_EXCEEDED,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.REDUCE_REQUEST,
            parameters=(("requested_tokens", 40_000), ("limit_tokens", 32_768)),
        )
        wire = outcome.to_wire()
        self.assertEqual(wire["outcome_id"], "prompt_model.context_exceeded")
        self.assertEqual(wire["parameters"], {"requested_tokens": 40_000, "limit_tokens": 32_768})
        self.assertIsNone(wire["untrusted_provider_detail"])
        for field in wire:
            self.assertNotIn("message", field)

    def test_outcome_parameters_are_typed_and_bounded(self) -> None:
        with self.assertRaises(PromptModelContractError):
            build_prompt_model_outcome(
                PromptModelOutcomeId.TIMEOUT,
                severity=ValidationSeverity.ERROR,
                remediation=PromptModelRemediation.RETRY_LATER,
                parameters=(("payload", {"nested": 1}),),
            )
        with self.assertRaises(PromptModelContractError):
            build_prompt_model_outcome(
                PromptModelOutcomeId.TIMEOUT,
                severity=ValidationSeverity.ERROR,
                remediation=PromptModelRemediation.RETRY_LATER,
                parameters=tuple((f"k{index}", index) for index in range(33)),
            )

    def test_unknown_outcome_identifier_is_refused(self) -> None:
        with self.assertRaises(PromptModelContractError) as caught:
            build_prompt_model_outcome(
                "prompt_model.invented",
                severity=ValidationSeverity.ERROR,
                remediation=PromptModelRemediation.RETRY_LATER,
                parameters=(),
            )
        self.assertEqual(caught.exception.code, "unknown_outcome")


class UntrustedProviderDetailTests(unittest.TestCase):
    def test_provider_text_is_marked_untrusted_across_the_boundary(self) -> None:
        outcome = build_prompt_model_outcome(
            PromptModelOutcomeId.AUTHENTICATION,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.REVIEW_CREDENTIAL,
            parameters=(("http_status", 401),),
            provider_detail="Incorrect API key provided",
        )
        detail = outcome.to_wire()["untrusted_provider_detail"]
        assert isinstance(detail, dict)
        self.assertEqual(detail["trust"], "untrusted")
        self.assertEqual(detail["source"], "provider")
        self.assertIs(detail["renderable_as_product_copy"], False)
        self.assertEqual(detail["text"], "Incorrect API key provided")

    def test_provider_text_is_scrubbed_of_secrets_and_control_characters(self) -> None:
        detail = UntrustedProviderDetail.from_provider_text(
            f"bad key {SK_SHAPED_SAMPLE}\r\n\x07 Bearer {LONG_RUN_SAMPLE}"
        )
        self.assertNotIn(SK_SHAPED_SAMPLE, detail.text)
        self.assertNotIn(LONG_RUN_SAMPLE, detail.text)
        self.assertIn("[redacted]", detail.text)
        self.assertTrue(all(ord(char) >= 0x20 for char in detail.text))

    def test_provider_text_is_length_bounded(self) -> None:
        detail = UntrustedProviderDetail.from_provider_text("x" * 4096)
        self.assertLessEqual(len(detail.text), 512)


class PromptModelCredentialTests(unittest.TestCase):
    def test_only_session_scoped_credential_sources_exist(self) -> None:
        self.assertEqual(
            {item.value for item in PromptModelCredentialSource}, {"none", "session_only"}
        )

    def test_environment_or_file_indirection_is_refused(self) -> None:
        for source in ("environment", "file", "keyring", ""):
            with self.subTest(source=source):
                with self.assertRaises(PromptModelContractError) as caught:
                    PromptModelCredentialRef.from_wire({"source": source, "last_four": ""})
                self.assertEqual(caught.exception.code, "credential_source")

    def test_credential_reference_never_carries_a_secret(self) -> None:
        reference = PromptModelCredentialRef(
            source=PromptModelCredentialSource.SESSION_ONLY, last_four=""
        )
        wire = reference.to_wire()
        self.assertEqual(set(wire), {"source", "last_four"})
        self.assertEqual(wire["last_four"], "")
        self.assertNotIn("sk-", repr(reference))

    def test_credential_hint_must_remain_empty(self) -> None:
        for value in ("9f2a", "9f2a1", "9f2", "9f 2", "9f2\n"):
            with self.subTest(value=value):
                with self.assertRaises(PromptModelContractError):
                    PromptModelCredentialRef(
                        source=PromptModelCredentialSource.SESSION_ONLY, last_four=value
                    )

    def test_absent_credential_carries_no_hint(self) -> None:
        with self.assertRaises(PromptModelContractError) as caught:
            PromptModelCredentialRef(source=PromptModelCredentialSource.NONE, last_four="9f2a")
        self.assertEqual(caught.exception.code, "credential_hint")


class PromptModelCatalogTests(unittest.TestCase):
    def entry(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "profile_id": "ollama.qwen3_8.27b_bf16.local",
            "family": "ollama",
            "endpoint": "http://127.0.0.1:11434/api",
            "model_id": "qwen3.8:27b-bf16",
            "model_digest": "sha256:" + "1" * 64,
            "adapter_version": "1.0.0",
            "parser_version": "h3.prompt_model.draft_json.v1",
            "license_id": "Apache-2.0",
            "license_source": "ollama_show",
            "license_text_sha256": "sha256:" + "2" * 64,
            "capabilities": capability_mapping(PromptModelFamily.OLLAMA),
        }
        values.update(overrides)
        return values

    def encode(self, *entries: dict[str, object]) -> bytes:
        payload = {"schema": PROMPT_MODEL_CATALOG_V1_SCHEMA, "profiles": list(entries)}
        return json.dumps(payload).encode("utf-8")

    def test_shipped_v5_catalog_is_curated_and_expresses_no_default(self) -> None:
        catalog = load_prompt_model_catalog()
        self.assertEqual(catalog.schema, PROMPT_MODEL_CATALOG_SCHEMA)
        self.assertIsNone(catalog.default_profile_id)
        self.assertEqual(
            catalog.profile_ids,
            (
                "ollama.qwen3_8_27b.local",
                "openai.gpt_5_6_terra.remote",
                "gemini.gemini_3_7_flash.remote",
                "anthropic.claude_sonnet_4_6.remote",
            ),
        )
        self.assertEqual(
            tuple(profile.wire_dialect for profile in catalog.profiles),
            (
                PromptModelDialect.OLLAMA_CHAT,
                PromptModelDialect.OPENAI_CHAT_COMPLETIONS,
                PromptModelDialect.OPENAI_CHAT_COMPLETIONS,
                PromptModelDialect.ANTHROPIC_MESSAGES,
            ),
        )
        # M22-15: only rows with reduced evidence from an executed qualification are runnable.
        # M22-18 added the reachability basis and OpenAI qualified on it -- a real exchange the
        # service answered with a quota refusal, which proves the route without buying a completion.
        # M22-21 then qualified Gemini on the ordinary completion basis, after two earlier attempts
        # were answered 503 by an upstream that was simply unavailable at the time. Every shipped
        # row is now runnable, which is a statement about evidence and not about ambition: each one
        # names the exchange it was obtained from.
        states = {profile.profile_id: profile.qualification_state for profile in catalog.profiles}
        self.assertEqual(
            states,
            {
                "ollama.qwen3_8_27b.local": PromptModelQualificationState.QUALIFIED,
                "openai.gpt_5_6_terra.remote": PromptModelQualificationState.QUALIFIED,
                "gemini.gemini_3_7_flash.remote": PromptModelQualificationState.QUALIFIED,
                "anthropic.claude_sonnet_4_6.remote": PromptModelQualificationState.QUALIFIED,
            },
        )
        qualified = catalog.require("ollama.qwen3_8_27b.local")
        evidence = qualified.qualification_evidence
        assert isinstance(evidence, PromptModelQualificationEvidence)
        self.assertEqual(evidence.to_wire()["kind"], "local_ollama")
        self.assertEqual(evidence.model_id, qualified.model_id)
        self.assertEqual(evidence.model_digest, qualified.model_digest)
        self.assertIn("completion", evidence.required_capabilities)
        self.assertGreaterEqual(evidence.context_length, 8_192)
        anthropic = catalog.require("anthropic.claude_sonnet_4_6.remote")
        remote_evidence = anthropic.qualification_evidence
        assert isinstance(remote_evidence, RemotePromptModelQualificationEvidence)
        self.assertIs(remote_evidence.family, PromptModelFamily.REMOTE_ANTHROPIC)
        self.assertEqual(
            remote_evidence.qualification_sha256,
            "sha256:72f85d17f32575de8b12751ae8c45dc999bdb15a396d2b473f2ce2cbbde726ad",
        )
        self.assertTrue(
            all(
                profile.model_digest is None
                for profile in catalog.profiles
                if profile.qualification_state is PromptModelQualificationState.CATALOG_ONLY
            )
        )
        raw = json.loads(PROMPT_MODEL_CATALOG_PATH.read_bytes().decode("utf-8"))
        self.assertEqual(set(raw), {"schema", "default_profile_id", "profiles"})
        self.assertIsNone(raw["default_profile_id"])

    def test_legacy_empty_v1_catalog_remains_decodable(self) -> None:
        catalog = decode_prompt_model_catalog(
            b'{"schema":"h3.prompt_model.profiles.v1","profiles":[]}'
        )
        self.assertEqual(catalog.profiles, ())
        self.assertIsNone(catalog.default_profile_id)

    def test_accepted_v3_local_evidence_remains_decodable_after_v4_migration(self) -> None:
        legacy = ROOT / "governance" / "contracts" / "prompt_model_profiles_v4.json"
        payload = json.loads(legacy.read_text(encoding="utf-8"))
        payload["schema"] = PROMPT_MODEL_CATALOG_V3_SCHEMA
        local_evidence = payload["profiles"][0]["qualification_evidence"]
        assert isinstance(local_evidence, dict)
        local_evidence.pop("kind")

        catalog = decode_prompt_model_catalog(json.dumps(payload).encode("utf-8"))

        self.assertEqual(catalog.schema, PROMPT_MODEL_CATALOG_V3_SCHEMA)
        self.assertIsInstance(
            catalog.require("ollama.qwen3_8_27b.local").qualification_evidence,
            type(fixture_evidence()),
        )

    def test_catalog_entry_requires_the_full_pinning_discipline(self) -> None:
        entry = self.entry()
        catalog = decode_prompt_model_catalog(self.encode(entry))
        self.assertEqual(catalog.profile_ids, ("ollama.qwen3_8.27b_bf16.local",))
        for field in sorted(entry):
            broken = dict(entry)
            broken.pop(field)
            with self.subTest(missing=field):
                with self.assertRaises(PromptModelContractError):
                    decode_prompt_model_catalog(self.encode(broken))

    def test_catalog_refuses_a_duplicate_profile_identifier(self) -> None:
        with self.assertRaises(PromptModelContractError) as caught:
            decode_prompt_model_catalog(self.encode(self.entry(), self.entry()))
        self.assertEqual(caught.exception.code, "catalog_duplicate_profile")

    def test_catalog_endpoint_passes_through_the_single_chokepoint(self) -> None:
        with self.assertRaises(PromptModelEgressError) as caught:
            decode_prompt_model_catalog(
                self.encode(
                    self.entry(profile_id="loopback.sneaky", endpoint="http://169.254.169.254/api")
                )
            )
        self.assertIs(caught.exception.rejection, EgressRejection.METADATA_HOST)

    def test_catalog_rejects_a_foreign_schema_or_oversized_payload(self) -> None:
        with self.assertRaises(PromptModelContractError) as caught:
            decode_prompt_model_catalog(b'{"schema": "other", "profiles": []}')
        self.assertEqual(caught.exception.code, "catalog_schema")
        with self.assertRaises(PromptModelContractError) as caught:
            decode_prompt_model_catalog(b"x" * 70_000)
        self.assertEqual(caught.exception.code, "catalog_size")


class AcceptedProviderSetupPreservedTests(unittest.TestCase):
    @staticmethod
    def policy(provider: ProviderIdentity) -> ProviderExecutionPolicy:
        remote = provider in {ProviderIdentity.REMOTE_CUSTOM, ProviderIdentity.OFFICIAL_MINIMAX}
        offline = provider is ProviderIdentity.MANUAL
        return ProviderExecutionPolicy(
            provider=provider,
            privacy_mode=(
                ProviderPrivacyMode.EXPLICIT_REMOTE if remote else ProviderPrivacyMode.LOCAL_ONLY
            ),
            offline=offline,
            network_allowed=not offline,
            upload_consent=remote,
            credential_reference="reference" if remote else None,
        )

    def test_existing_setup_rows_keep_their_meaning(self) -> None:
        setup = build_provider_setup(
            self.policy(ProviderIdentity.LOCAL),
            local_backend=ProviderLocalBackend.OLLAMA,
        )
        self.assertIs(setup.destination, ProviderDestinationClass.LOOPBACK_HTTP)
        self.assertIs(setup.transfer_boundary, ProviderTransferBoundary.OLLAMA_PROCESS)
        self.assertTrue(setup.preflight_required)

    def test_the_new_transfer_boundary_is_not_reachable_from_the_accepted_matrix(self) -> None:
        reached = 0
        for identity in ProviderIdentity:
            for backend in ProviderLocalBackend:
                try:
                    setup = build_provider_setup(self.policy(identity), local_backend=backend)
                except ContractValidationError:
                    continue  # the pair is outside the accepted matrix, which is the point
                reached += 1
                with self.subTest(identity=identity, backend=backend):
                    self.assertIsNot(
                        setup.transfer_boundary, ProviderTransferBoundary.LOCAL_SERVER_PROCESS
                    )
        self.assertGreaterEqual(reached, 5)


class PromptModelSchemaTests(unittest.TestCase):
    def test_contract_schema_is_versioned(self) -> None:
        self.assertEqual(PROMPT_MODEL_PROVIDER_SCHEMA, "h3-context-prompt-model-provider/1")
        self.assertEqual(PROMPT_MODEL_CATALOG_V1_SCHEMA, "h3.prompt_model.profiles.v1")
        self.assertEqual(PROMPT_MODEL_CATALOG_V2_SCHEMA, "h3.prompt_model.profiles.v2")
        self.assertEqual(PROMPT_MODEL_CATALOG_V3_SCHEMA, "h3.prompt_model.profiles.v3")
        self.assertEqual(PROMPT_MODEL_CATALOG_V4_SCHEMA, "h3.prompt_model.profiles.v4")
        self.assertEqual(PROMPT_MODEL_CATALOG_SCHEMA, "h3.prompt_model.profiles.v5")

    def test_outcome_type_is_frozen(self) -> None:
        outcome = build_prompt_model_outcome(
            PromptModelOutcomeId.QUOTA,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.RETRY_LATER,
            parameters=(),
        )
        self.assertIsInstance(outcome, PromptModelOutcome)
        with self.assertRaises(AttributeError):
            outcome.severity = ValidationSeverity.INFO  # type: ignore[misc]


# --- M22-13: reading a local runtime strictly enough to call a model qualified ----------------

LICENSE_TEXT = "Fixture licence notice. Not the real one, and never stored beyond its digest."
LICENSE_SHA = "sha256:" + sha256(LICENSE_TEXT.encode("utf-8")).hexdigest()
#: Hex with letters in it, so a case check is a real check.
WIRE_DIGEST = "5a" * 32
EXACT_MODEL_ID = "fixture-model:27b-q4_K_M"
MODEL_SIZE_BYTES = 17_741_872_132


def tags_payload(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "name": EXACT_MODEL_ID,
        "model": EXACT_MODEL_ID,
        "digest": WIRE_DIGEST,
        "size": MODEL_SIZE_BYTES,
        "details": {"format": "gguf", "family": "qwen35"},
    }
    row.update(overrides)
    return {"models": [row]}


def show_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "license": LICENSE_TEXT,
        "capabilities": ["completion", "thinking", "tools", "vision"],
        "details": {
            "format": "gguf",
            "family": "qwen35",
            "parameter_size": "27.3B",
            "quantization_level": "Q4_K_M",
        },
        "model_info": {"qwen35.context_length": 262_144, "general.architecture": "qwen35"},
    }
    payload.update(overrides)
    return payload


def fixture_evidence(**overrides: object) -> PromptModelQualificationEvidence:
    arguments: dict[str, object] = {
        "model_id": EXACT_MODEL_ID,
        "digest": WIRE_DIGEST,
        "model_size_bytes": MODEL_SIZE_BYTES,
        "model_format": "gguf",
        "model_family": "qwen35",
        "parameter_size": "27.3B",
        "quantization_level": "Q4_K_M",
        "context_length": 262_144,
        "capabilities": ("completion", "thinking", "tools", "vision"),
        "license_text_sha256": LICENSE_SHA,
        "adapter_version": "1.0.0",
        "parser_version": "h3.prompt_model.draft_json.v1",
        "evidence_basis_id": "M22-13.fixture.1",
    }
    arguments.update(overrides)
    return build_qualification_evidence(**arguments)  # type: ignore[arg-type]


class ExactTagsReadingTests(unittest.TestCase):
    def test_the_wire_digest_arrives_bare_and_leaves_in_the_repository_domain(self) -> None:
        row = parse_exact_tags_row(tags_payload(), EXACT_MODEL_ID)
        self.assertEqual(row.model_digest, "sha256:" + WIRE_DIGEST)
        self.assertEqual(row.model_size_bytes, MODEL_SIZE_BYTES)

    def test_a_second_row_for_the_same_id_invalidates_the_listing(self) -> None:
        listing = tags_payload()
        rows = listing["models"]
        assert isinstance(rows, list)
        listing["models"] = rows * 2
        with self.assertRaises(PromptModelContractError) as caught:
            parse_exact_tags_row(listing, EXACT_MODEL_ID)
        self.assertEqual(str(caught.exception), "readiness_tags_ambiguous")

    def test_one_unreadable_row_refuses_the_whole_answer(self) -> None:
        """Even a row for a completely different model: a listing is read whole or not at all."""

        for label, broken in (
            ("not a mapping", 7),
            ("no name", {"model": "other", "digest": WIRE_DIGEST}),
            ("name disagrees with model", {"name": "other-a", "model": "other-b"}),
        ):
            with self.subTest(row=label):
                listing = tags_payload()
                rows = listing["models"]
                assert isinstance(rows, list)
                listing["models"] = [*rows, broken]
                with self.assertRaises(PromptModelContractError):
                    parse_exact_tags_row(listing, EXACT_MODEL_ID)

    def test_an_incomplete_row_for_the_selected_model_is_refused(self) -> None:
        for label, override in (
            ("no digest", {"digest": None}),
            ("prefixed digest", {"digest": "sha256:" + WIRE_DIGEST}),
            ("uppercase digest", {"digest": WIRE_DIGEST.upper()}),
            ("zero size", {"size": 0}),
            ("boolean size", {"size": True}),
            ("no details", {"details": None}),
        ):
            with self.subTest(row=label):
                with self.assertRaises(PromptModelContractError):
                    parse_exact_tags_row(tags_payload(**override), EXACT_MODEL_ID)

    def test_an_absent_model_is_its_own_refusal(self) -> None:
        with self.assertRaises(PromptModelContractError) as caught:
            parse_exact_tags_row(tags_payload(), "some-other-model")
        self.assertEqual(str(caught.exception), "readiness_tags_absent")


class ShowIdentityTests(unittest.TestCase):
    def test_the_licence_survives_only_as_a_digest(self) -> None:
        identity = parse_show_identity(show_payload())
        self.assertEqual(identity.license_text_sha256, LICENSE_SHA)
        self.assertNotIn(LICENSE_TEXT, repr(identity))
        self.assertEqual(identity.context_length, 262_144)
        # Sorted, so two runs of the same server can be compared byte for byte.
        self.assertEqual(identity.capabilities, ("completion", "thinking", "tools", "vision"))

    def test_the_context_length_is_read_from_the_declared_architecture_only(self) -> None:
        """A key belonging to another architecture must not answer for this one."""

        payload = show_payload(model_info={"llama.context_length": 262_144})
        with self.assertRaises(PromptModelContractError) as caught:
            parse_show_identity(payload)
        self.assertEqual(str(caught.exception), "readiness_show_context_length")

    def test_prose_bearing_and_unbounded_fields_are_refused(self) -> None:
        for label, payload in (
            ("no licence", show_payload(license="")),
            ("oversized licence", show_payload(license="x" * (1 << 20) + "x")),
            ("no capabilities", show_payload(capabilities=[])),
            ("capability is not a token", show_payload(capabilities=["Completion!"])),
            ("details missing a fact", show_payload(details={"format": "gguf"})),
            ("model_info absent", show_payload(model_info=None)),
        ):
            with self.subTest(payload=label):
                with self.assertRaises(PromptModelContractError):
                    parse_show_identity(payload)


class QualificationMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = fixture_evidence()
        self.tags = parse_exact_tags_row(tags_payload(), EXACT_MODEL_ID)
        self.show = parse_show_identity(show_payload())

    def test_the_qualified_identity_agrees_with_itself(self) -> None:
        self.assertIsNone(match_qualification_identity(self.evidence, self.tags, self.show))

    def test_a_larger_context_window_still_satisfies_the_qualified_floor(self) -> None:
        wider = parse_show_identity(show_payload(model_info={"qwen35.context_length": 1 << 20}))
        self.assertIsNone(match_qualification_identity(self.evidence, self.tags, wider))

    def test_each_kind_of_drift_keeps_its_own_outcome(self) -> None:
        replaced_weights = parse_exact_tags_row(tags_payload(digest="6b" * 32), EXACT_MODEL_ID)
        self.assertIs(
            match_qualification_identity(self.evidence, replaced_weights, self.show),
            PromptModelOutcomeId.DIGEST_MISMATCH,
        )
        resized = parse_exact_tags_row(tags_payload(size=MODEL_SIZE_BYTES - 1), EXACT_MODEL_ID)
        self.assertIs(
            match_qualification_identity(self.evidence, resized, self.show),
            PromptModelOutcomeId.DIGEST_MISMATCH,
        )
        for label, show in (
            ("shrunken window", show_payload(model_info={"qwen35.context_length": 8_192})),
            ("amended licence", show_payload(license=LICENSE_TEXT + " (amended)")),
            (
                "requantised",
                show_payload(
                    details={
                        "format": "gguf",
                        "family": "qwen35",
                        "parameter_size": "27.3B",
                        "quantization_level": "Q8_0",
                    }
                ),
            ),
            ("capability withdrawn", show_payload(capabilities=["completion", "tools"])),
            ("cannot complete", show_payload(capabilities=["embedding"])),
        ):
            with self.subTest(drift=label):
                self.assertIs(
                    match_qualification_identity(
                        self.evidence, self.tags, parse_show_identity(show)
                    ),
                    PromptModelOutcomeId.CAPABILITY_MISMATCH,
                )

    def test_a_different_model_under_the_same_evidence_is_missing_not_mismatched(self) -> None:
        other = fixture_evidence(model_id="another-model:8b")
        self.assertIs(
            match_qualification_identity(other, self.tags, self.show),
            PromptModelOutcomeId.MODEL_MISSING,
        )


class ReadinessEvidenceTests(unittest.TestCase):
    def observation(self, **overrides: object) -> QualifiedIdentityObservation:
        arguments: dict[str, object] = {
            "profile_id": "fixture.qualified.local",
            "model_id": EXACT_MODEL_ID,
            "model_digest": "sha256:" + WIRE_DIGEST,
            "qualification_sha256": "sha256:" + "7" * 64,
            "endpoint_sha256": compute_endpoint_fingerprint("http://127.0.0.1:11434"),
            "observed_at": 1234.5,
        }
        arguments.update(overrides)
        return QualifiedIdentityObservation(**arguments)  # type: ignore[arg-type]

    def test_the_endpoint_is_bound_by_fingerprint_and_never_carried(self) -> None:
        endpoint = "http://127.0.0.1:11434"
        fingerprint = compute_endpoint_fingerprint(endpoint)
        self.assertRegex(fingerprint, r"\Asha256:[0-9a-f]{64}\Z")
        self.assertNotEqual(fingerprint, compute_endpoint_fingerprint("http://127.0.0.1:11435"))
        stamped = stamp_readiness_evidence(
            self.observation(endpoint_sha256=fingerprint), provider_revision=4, authority_epoch=2
        )
        self.assertNotIn("11434", json.dumps(stamped.to_wire()))

    def test_the_projection_proves_that_something_was_observed_not_what_proved_it(self) -> None:
        wire = stamp_readiness_evidence(
            self.observation(), provider_revision=4, authority_epoch=2
        ).to_wire()
        self.assertEqual(
            set(wire),
            {"profile_id", "model_id", "observed_at", "provider_revision", "authority_epoch"},
        )

    def test_evidence_stops_matching_when_any_part_of_the_authority_moves(self) -> None:
        profile = load_prompt_model_catalog().profiles[0]
        endpoint = compute_endpoint_fingerprint(profile.endpoint)
        assert profile.model_digest is not None
        qualification = profile.qualification_evidence
        assert isinstance(qualification, PromptModelQualificationEvidence)
        stamped = stamp_readiness_evidence(
            self.observation(
                profile_id=profile.profile_id,
                model_id=profile.model_id,
                model_digest=profile.model_digest,
                qualification_sha256=qualification.show_identity_sha256,
                endpoint_sha256=endpoint,
            ),
            provider_revision=4,
            authority_epoch=2,
        )
        self.assertTrue(stamped.matches(profile, endpoint, 4, 2))
        self.assertFalse(stamped.matches(profile, endpoint, 5, 2))
        self.assertFalse(stamped.matches(profile, endpoint, 4, 3))
        self.assertFalse(
            stamped.matches(profile, compute_endpoint_fingerprint("http://127.0.0.1:1"), 4, 2)
        )
        self.assertFalse(stamped.matches(None, endpoint, 4, 2))
        assert profile.license_text_sha256 is not None
        changed_claim = replace(
            profile,
            qualification_evidence=fixture_evidence(
                model_id=profile.model_id,
                digest=profile.model_digest,
                license_text_sha256=profile.license_text_sha256,
                adapter_version=profile.adapter_version,
                parser_version=profile.parser_version,
                quantization_level="Q8_0",
            ),
        )
        self.assertFalse(stamped.matches(changed_claim, endpoint, 4, 2))

    def test_an_unusable_observation_time_or_counter_is_refused(self) -> None:
        for label, overrides in (
            ("negative", {"observed_at": -1.0}),
            ("not a number", {"observed_at": "1234.5"}),
            ("infinite", {"observed_at": float("inf")}),
            ("not a number at all", {"observed_at": float("nan")}),
        ):
            with self.subTest(observed_at=label):
                with self.assertRaises(PromptModelContractError):
                    self.observation(**overrides)
        for counters in ({"provider_revision": 0}, {"authority_epoch": -1}):
            with self.subTest(counters=counters):
                with self.assertRaises(PromptModelContractError):
                    stamp_readiness_evidence(
                        self.observation(),
                        **{"provider_revision": 1, "authority_epoch": 1, **counters},
                    )


class ShippedSurfaceReconciliationTests(unittest.TestCase):
    """M22-13 section 2.6: the shipped surfaces agree, and none of them over-claims."""

    CONTRACTS = Path(__file__).resolve().parents[1] / "governance" / "contracts"

    def test_every_surface_that_names_the_local_profile_names_the_catalog_one(self) -> None:
        # IMPORTANT: current schemas need v6 connection IDs; the sealed v5 decoder fixture
        # would falsely require retired model IDs and break current factory validation.
        expected = load_current_prompt_model_catalog().profile_ids
        for name, pointer in (
            ("native_mode_matrix_v1.schema.json", ("properties", "assisted_profiles")),
            ("source_drift_checkpoint_v2.schema.json", ("properties", "assisted_profiles")),
        ):
            with self.subTest(contract=name):
                document = json.loads((self.CONTRACTS / name).read_text(encoding="utf-8"))
                for key in pointer:
                    document = document[key]
                self.assertEqual(tuple(item["const"] for item in document["prefixItems"]), expected)

    def test_activating_the_prompt_model_did_not_claim_the_evaluation_lane_passed(self) -> None:
        """Two different qualifications, and only one of them was earned by this item.

        M22-13 qualifies one local prompt model for drafting. It says nothing about the M14
        evaluation lane, whose `product_scope` reports whether an assisted profile has been
        qualified through candidate receipts, ablations and a Pareto set. Those surfaces stay
        `MANUAL_ONLY_SCOPED`, and this test exists so that flipping one is a deliberate act.
        """

        for name, pointer in (
            ("native_mode_matrix_v1.schema.json", ("properties", "product_scope")),
            ("source_drift_checkpoint_v2.schema.json", ("properties", "product_scope")),
            ("product_shell_v1.schema.json", ("properties", "product_scope")),
            ("sidebar_workspace_v1.schema.json", ("properties", "product_scope")),
        ):
            with self.subTest(contract=name):
                document = json.loads((self.CONTRACTS / name).read_text(encoding="utf-8"))
                for key in pointer:
                    document = document[key]
                self.assertEqual(document["const"], "MANUAL_ONLY_SCOPED")

    def test_no_shipped_row_claims_qualification_without_carrying_its_proof(self) -> None:
        for candidate in load_prompt_model_catalog().profiles:
            with self.subTest(profile=candidate.profile_id):
                qualified = candidate.qualification_state is PromptModelQualificationState.QUALIFIED
                self.assertIs(qualified, candidate.qualification_evidence is not None)
                if qualified:
                    if candidate.family is PromptModelFamily.OLLAMA:
                        self.assertIsInstance(
                            candidate.qualification_evidence,
                            PromptModelQualificationEvidence,
                        )
                        self.assertFalse(candidate.capabilities.requires_credential)
                    else:
                        self.assertIsInstance(
                            candidate.qualification_evidence,
                            RemotePromptModelQualificationEvidence,
                        )
                        self.assertTrue(candidate.capabilities.requires_credential)


if __name__ == "__main__":
    unittest.main()
