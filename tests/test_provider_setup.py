"""M15-06 operational provider setup, consent, preflight, and redaction tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator

from comfyui_h3_context.core.contracts import ProviderIdentity
from comfyui_h3_context.core.errors import (
    ContractValidationError,
    LocalAdapterCancelledError,
    LocalAdapterTimeoutError,
    SecurityPolicyError,
)
from comfyui_h3_context.core.local_adapters import LocalCancellationProbe
from comfyui_h3_context.core.model_manifest import (
    ModelProbeReport,
    ModelProbeStatus,
    OllamaModelObservation,
    OllamaServerObservation,
)
from comfyui_h3_context.core.provider_policy import (
    ProviderExecutionPolicy,
    ProviderPrivacyMode,
)
from comfyui_h3_context.core.provider_setup import (
    MAX_PROVIDER_SETUP_WIRE_BYTES,
    ProviderConnectivityObservation,
    ProviderConnectivityRequest,
    ProviderConsentAuthority,
    ProviderConsentStatus,
    ProviderDestinationClass,
    ProviderLocalBackend,
    ProviderPreflightStatus,
    ProviderTransferBoundary,
    admit_provider_execution,
    build_ollama_provider_preflight,
    build_provider_consent_authority,
    build_provider_setup,
    build_static_provider_preflight,
    decode_provider_setup_json,
    run_remote_provider_preflight,
)
from comfyui_h3_context.nodes import H3ContextProviderTransparencyNode

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(character: str) -> str:
    return "sha256:" + character * 64


def policy(
    provider: ProviderIdentity,
    *,
    privacy: ProviderPrivacyMode,
    offline: bool,
    network_allowed: bool,
    upload_consent: bool,
    credential_reference: str | None = None,
    fallback_provider: ProviderIdentity | None = None,
) -> ProviderExecutionPolicy:
    return ProviderExecutionPolicy(
        provider=provider,
        privacy_mode=privacy,
        offline=offline,
        network_allowed=network_allowed,
        upload_consent=upload_consent,
        credential_reference=credential_reference,
        fallback_provider=fallback_provider,
    )


class ProviderSetupContractTests(unittest.TestCase):
    def test_closed_route_backend_matrix_and_preferred_local_default(self) -> None:
        manual = build_provider_setup(
            policy(
                ProviderIdentity.MANUAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=True,
                network_allowed=False,
                upload_consent=False,
            )
        )
        local = build_provider_setup(
            policy(
                ProviderIdentity.LOCAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=True,
                network_allowed=False,
                upload_consent=False,
            )
        )
        ollama = build_provider_setup(
            policy(
                ProviderIdentity.LOCAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=False,
                network_allowed=True,
                upload_consent=False,
            ),
            local_backend=ProviderLocalBackend.OLLAMA,
        )
        remote = build_provider_setup(
            policy(
                ProviderIdentity.REMOTE_CUSTOM,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.remote_custom",
            )
        )

        self.assertIs(manual.local_backend, ProviderLocalBackend.NONE)
        self.assertIs(local.local_backend, ProviderLocalBackend.COMFYUI_NATIVE)
        self.assertIs(ollama.local_backend, ProviderLocalBackend.OLLAMA)
        self.assertIs(remote.local_backend, ProviderLocalBackend.NONE)
        self.assertIs(manual.destination, ProviderDestinationClass.NONE)
        self.assertIs(local.destination, ProviderDestinationClass.IN_PROCESS)
        self.assertIs(ollama.destination, ProviderDestinationClass.LOOPBACK_HTTP)
        self.assertIs(remote.destination, ProviderDestinationClass.INTERNET)
        self.assertIs(ollama.transfer_boundary, ProviderTransferBoundary.OLLAMA_PROCESS)
        self.assertIs(remote.transfer_boundary, ProviderTransferBoundary.REMOTE_UPLOAD)

        with self.assertRaises(ContractValidationError):
            build_provider_setup(
                policy(
                    ProviderIdentity.OFFICIAL_MINIMAX,
                    privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                    offline=False,
                    network_allowed=True,
                    upload_consent=True,
                    credential_reference="env.official_minimax",
                ),
                local_backend=ProviderLocalBackend.OLLAMA,
            )

    def test_setup_fingerprint_binds_route_revision_and_caller_selected_alternative(self) -> None:
        first = build_provider_setup(
            policy(
                ProviderIdentity.REMOTE_CUSTOM,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.remote_custom",
                fallback_provider=ProviderIdentity.LOCAL,
            ),
            revision=7,
        )
        second = build_provider_setup(
            policy(
                ProviderIdentity.LOCAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=True,
                network_allowed=False,
                upload_consent=False,
            ),
            revision=8,
        )
        self.assertNotEqual(first.setup_fingerprint, second.setup_fingerprint)
        self.assertEqual(first.fallback_policy, "caller_selected_only")
        self.assertEqual(first.fallback_provider, ProviderIdentity.LOCAL)
        self.assertNotEqual(first.provider, second.provider)

    def test_wire_contains_only_opaque_reference_and_public_projection_redacts_it(self) -> None:
        setup = build_provider_setup(
            policy(
                ProviderIdentity.OFFICIAL_MINIMAX,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.official_minimax",
            )
        )
        wire_text = json.dumps(setup.to_wire(), sort_keys=True)
        public_text = json.dumps(setup.to_public_dict(), sort_keys=True)
        self.assertIn("env.official_minimax", wire_text)
        self.assertNotIn("env.official_minimax", public_text)
        for forbidden in ("Bearer ", "api_key", "token=", "https://", "C:\\\\"):
            self.assertNotIn(forbidden, wire_text)

    def test_disclosure_names_ollama_process_transfer_and_preflight_requirement(self) -> None:
        setup = build_provider_setup(
            policy(
                ProviderIdentity.LOCAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=False,
                network_allowed=True,
                upload_consent=False,
            ),
            local_backend=ProviderLocalBackend.OLLAMA,
        )
        self.assertIn("backend=ollama", setup.disclosure)
        self.assertIn("media_transfer=ollama_process", setup.disclosure)
        self.assertIn("preflight=required", setup.disclosure)

    def test_consent_and_preflight_are_fingerprint_bound_and_failure_never_falls_back(self) -> None:
        setup = build_provider_setup(
            policy(
                ProviderIdentity.LOCAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=True,
                network_allowed=False,
                upload_consent=False,
            )
        )
        consent = build_provider_consent_authority(setup)
        ready = build_static_provider_preflight(
            setup, backend_available=True, backend_revision="comfyui-0.30.0-c44"
        )
        admit_provider_execution(setup, consent, ready)

        stale_setup = replace(setup, revision=setup.revision + 1)
        with self.assertRaises(SecurityPolicyError):
            admit_provider_execution(stale_setup, consent, ready)

        absent = build_static_provider_preflight(setup, backend_available=False)
        self.assertIs(absent.status, ProviderPreflightStatus.BACKEND_ABSENT)
        self.assertIsNone(absent.fallback_provider)
        with self.assertRaises(SecurityPolicyError):
            admit_provider_execution(setup, consent, absent)

        forged = ProviderConsentAuthority(
            setup.setup_fingerprint,
            setup.revision,
            ProviderConsentStatus.NOT_REQUIRED,
            False,
            False,
        )
        with self.assertRaises(SecurityPolicyError):
            admit_provider_execution(setup, forged, ready)

    def test_ollama_preflight_joins_setup_manifest_digest_and_server(self) -> None:
        setup = build_provider_setup(
            policy(
                ProviderIdentity.LOCAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=False,
                network_allowed=True,
                upload_consent=False,
            ),
            local_backend=ProviderLocalBackend.OLLAMA,
        )
        observation = OllamaModelObservation(
            name="qwen3-vl:8b",
            digest=fingerprint("d"),
            size_bytes=1024,
            family="qwen3",
            capabilities=("text_generation", "vision"),
        )
        report = ModelProbeReport(
            manifest_id="ollama.qwen3_vl_8b",
            status=ModelProbeStatus.SUPPORTED,
            observed_model=observation,
            server=OllamaServerObservation("0.9.0", cloud_enabled=False),
        )
        receipt = build_ollama_provider_preflight(
            setup,
            report,
            endpoint_fingerprint=fingerprint("e"),
            expected_manifest_id="ollama.qwen3_vl_8b",
            expected_model_digest=fingerprint("d"),
            expected_server_version="0.9.0",
        )
        self.assertIs(receipt.status, ProviderPreflightStatus.READY)
        self.assertEqual(receipt.model_digest, fingerprint("d"))
        self.assertFalse(receipt.media_transferred)
        self.assertFalse(receipt.model_executed)
        self.assertFalse(receipt.download_attempted)
        self.assertFalse(receipt.admin_attempted)

        mismatch = build_ollama_provider_preflight(
            setup,
            report,
            endpoint_fingerprint=fingerprint("e"),
            expected_manifest_id="ollama.qwen3_vl_8b",
            expected_model_digest=fingerprint("f"),
            expected_server_version="0.9.0",
        )
        self.assertIs(mismatch.status, ProviderPreflightStatus.DIGEST_MISMATCH)

    def test_remote_connectivity_preflight_preserves_terminal_states_and_zero_media(self) -> None:
        setup = build_provider_setup(
            policy(
                ProviderIdentity.OFFICIAL_MINIMAX,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.official_minimax",
            )
        )

        class Probe:
            def __init__(self, status: ProviderPreflightStatus) -> None:
                self.status = status
                self.requests: list[ProviderConnectivityRequest] = []

            def probe(
                self,
                request: ProviderConnectivityRequest,
                *,
                cancellation: LocalCancellationProbe | None = None,
            ) -> ProviderConnectivityObservation:
                del cancellation
                self.requests.append(request)
                return ProviderConnectivityObservation(
                    self.status,
                    backend_revision="minimax-api-v1",
                    diagnostics=()
                    if self.status is ProviderPreflightStatus.READY
                    else (self.status.value,),
                )

        for status in (
            ProviderPreflightStatus.READY,
            ProviderPreflightStatus.AUTHENTICATION,
            ProviderPreflightStatus.QUOTA,
            ProviderPreflightStatus.TRANSPORT,
        ):
            with self.subTest(status=status):
                probe = Probe(status)
                receipt = run_remote_provider_preflight(setup, probe)
                self.assertIs(receipt.status, status)
                self.assertTrue(receipt.network_attempted)
                self.assertFalse(receipt.media_transferred)
                self.assertFalse(receipt.model_executed)
                self.assertFalse(receipt.download_attempted)
                self.assertFalse(receipt.admin_attempted)
                self.assertIsNone(receipt.fallback_provider)
                self.assertEqual(probe.requests[0].credential_reference, "env.official_minimax")

    def test_remote_preflight_maps_timeout_and_cancel_without_error_text_leakage(self) -> None:
        setup = build_provider_setup(
            policy(
                ProviderIdentity.REMOTE_CUSTOM,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.remote_custom",
            )
        )

        class FailingProbe:
            def __init__(self, error: Exception) -> None:
                self.error = error

            def probe(
                self,
                request: ProviderConnectivityRequest,
                *,
                cancellation: LocalCancellationProbe | None = None,
            ) -> ProviderConnectivityObservation:
                del request, cancellation
                raise self.error

        for error, status in (
            (
                LocalAdapterTimeoutError("https://private.invalid?token=secret"),
                ProviderPreflightStatus.TIMEOUT,
            ),
            (
                LocalAdapterCancelledError("C:\\private\\media.png"),
                ProviderPreflightStatus.CANCELLED,
            ),
        ):
            with self.subTest(status=status):
                receipt = run_remote_provider_preflight(setup, FailingProbe(error))
                self.assertIs(receipt.status, status)
                encoded = json.dumps(receipt.to_public_dict(), sort_keys=True)
                self.assertNotIn("private.invalid", encoded)
                self.assertNotIn("media.png", encoded)
                self.assertNotIn("secret", encoded)

    def test_strict_decoder_rejects_duplicates_subclasses_non_finite_and_size(self) -> None:
        setup = build_provider_setup(
            policy(
                ProviderIdentity.MANUAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=True,
                network_allowed=False,
                upload_consent=False,
            )
        )
        encoded = json.dumps(setup.to_wire(), separators=(",", ":"), sort_keys=True)
        self.assertEqual(decode_provider_setup_json(encoded), setup)
        duplicate = encoded.replace('"revision":1', '"revision":1,"revision":1', 1)
        hostile = encoded.replace('"revision":1', '"revision":NaN', 1)

        class TextSubclass(str):
            pass

        class BytesSubclass(bytes):
            pass

        class BytearraySubclass(bytearray):
            pass

        for payload in (
            duplicate,
            hostile,
            TextSubclass(encoded),
            BytesSubclass(encoded.encode("utf-8")),
            BytearraySubclass(encoded.encode("utf-8")),
            " " * (MAX_PROVIDER_SETUP_WIRE_BYTES + 1),
            "[" * 2_000 + "0" + "]" * 2_000,
        ):
            with (
                self.subTest(payload_type=type(payload).__name__),
                self.assertRaises(ContractValidationError),
            ):
                decode_provider_setup_json(payload)
        with self.assertRaisesRegex(ContractValidationError, "resource depth"):
            decode_provider_setup_json("[" * 17 + "0" + "]" * 17)

    def test_wire_matches_closed_public_json_schema(self) -> None:
        setup = build_provider_setup(
            policy(
                ProviderIdentity.OFFICIAL_MINIMAX,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.official_minimax",
            )
        )
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "provider_setup_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(setup.to_wire())), [])

    def test_node_preserves_existing_outputs_and_appends_operational_setup(self) -> None:
        self.assertEqual(
            H3ContextProviderTransparencyNode.RETURN_NAMES[:3],
            ("transparency", "consent_notice", "disclosure"),
        )
        self.assertEqual(H3ContextProviderTransparencyNode.RETURN_NAMES[3], "provider_setup")
        optional = H3ContextProviderTransparencyNode.INPUT_TYPES()["optional"]
        self.assertEqual(optional["local_backend"][1]["default"], "none")
        result = H3ContextProviderTransparencyNode().describe()
        self.assertEqual(len(result), 4)
        self.assertIs(result[3].local_backend, ProviderLocalBackend.NONE)
        ollama = H3ContextProviderTransparencyNode().describe(
            provider="local",
            privacy_mode="local_only",
            offline=False,
            network_allowed=True,
            upload_consent=False,
            credential_reference="none",
            fallback_provider="none",
            local_backend="ollama",
        )
        self.assertIn("media_transfer=ollama_process", ollama[2])

        blocked = H3ContextProviderTransparencyNode().describe(
            provider="remote_custom",
            privacy_mode="explicit_remote",
            offline=True,
            network_allowed=False,
            upload_consent=False,
            credential_reference="none",
            fallback_provider="none",
        )
        self.assertFalse(blocked[0].policy_valid)
        self.assertFalse(blocked[3].policy_valid)
        self.assertIn("policy_valid=no", blocked[2])


if __name__ == "__main__":
    unittest.main()
