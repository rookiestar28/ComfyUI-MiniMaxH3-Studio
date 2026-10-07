"""M4-01 provider protocol and capability contract tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from comfyui_h3_context.core import (
    CURRENT_PROVIDER_PROTOCOL_VERSION,
    CredentialRequirement,
    MediaKind,
    NetworkRequirement,
    PrivacyLocation,
    ProviderCapabilities,
    ProviderCapabilityRequest,
    ProviderDescriptor,
    ProviderIdentity,
    ProviderOutputContract,
    ProviderProtocol,
    ResourceLimits,
    TaskMode,
    validate_provider_capabilities,
)
from comfyui_h3_context.core.errors import ContractValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "governance" / "contracts" / "provider_protocol_v1.schema.json"


def limits() -> ResourceLimits:
    return ResourceLimits(
        max_bytes=2_000_000,
        max_duration_seconds=120.0,
        max_width=4096,
        max_height=4096,
        max_frames=3600,
        max_sample_rate=48_000,
        max_references=4,
        max_concurrency=1,
        max_memory_bytes=2**30,
        max_wall_time_seconds=180.0,
        max_retries=0,
        max_cache_ttl_seconds=0.0,
    )


def descriptor() -> ProviderDescriptor:
    return ProviderDescriptor(
        protocol_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
        identity=ProviderIdentity.LOCAL,
        provider_version="local-v1",
        capabilities=ProviderCapabilities(
            supported_task_modes=frozenset({TaskMode.T2VA, TaskMode.I2VA}),
            supported_media=frozenset({MediaKind.IMAGE}),
            privacy_location=PrivacyLocation.LOCAL,
            network_requirement=NetworkRequirement.NONE,
            credential_requirement=CredentialRequirement.NONE,
            limits=limits(),
            supports_determinism=True,
            supports_seed=True,
            supports_cancellation=True,
        ),
        output_contract=ProviderOutputContract(
            schema_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
            output_schema="h3.context.prompt.v1",
            required_fields=("prompt", "receipt"),
        ),
    )


class ProviderProtocolTests(unittest.TestCase):
    def test_descriptor_is_versioned_immutable_and_wire_safe(self) -> None:
        value = descriptor()
        with self.assertRaises(FrozenInstanceError):
            value.provider_version = "changed"  # type: ignore[misc]

        encoded = json.dumps(value.to_wire(), sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["protocol_version"], "1.0")
        self.assertEqual(decoded["identity"], "local")
        self.assertEqual(decoded["capabilities"]["supported_task_modes"], ["i2va", "t2va"])
        self.assertEqual(decoded["output_contract"]["required_fields"], ["prompt", "receipt"])
        self.assertNotIn("api_key", encoded.casefold())
        self.assertNotIn("secret", encoded.casefold())
        self.assertNotIn("token", encoded.casefold())
        self.assertNotIn("path", decoded)
        self.assertNotIn("fallback", decoded)

    def test_capability_protocol_is_structural_without_execution_or_fallback(self) -> None:
        provider_descriptor = descriptor()

        class StubProvider:
            descriptor = provider_descriptor

        self.assertIsInstance(StubProvider(), ProviderProtocol)
        self.assertFalse(hasattr(StubProvider(), "execute"))
        self.assertFalse(hasattr(provider_descriptor, "fallback"))

    def test_invalid_capability_declarations_fail_closed(self) -> None:
        with self.assertRaises(ContractValidationError):
            ProviderCapabilities(
                supported_task_modes=frozenset({TaskMode.T2VA}),
                supported_media=frozenset(),
                privacy_location=PrivacyLocation.REMOTE,
                network_requirement=NetworkRequirement.NONE,
                credential_requirement=CredentialRequirement.REQUIRED,
                limits=limits(),
                supports_determinism=False,
                supports_seed=True,
                supports_cancellation=False,
            )

        for field in ("url", "local_path", "credential", "api_token", "signed_resource"):
            with self.subTest(field=field), self.assertRaises(ContractValidationError):
                ProviderOutputContract(
                    schema_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
                    output_schema="h3.context.prompt.v1",
                    required_fields=(field,),
                )

        with self.assertRaises(ContractValidationError):
            ProviderCapabilities(
                supported_task_modes={TaskMode.T2VA},  # type: ignore[arg-type]
                supported_media=frozenset(),
                privacy_location=PrivacyLocation.LOCAL,
                network_requirement=NetworkRequirement.NONE,
                credential_requirement=CredentialRequirement.NONE,
                limits=limits(),
                supports_determinism=True,
                supports_seed=False,
                supports_cancellation=False,
            )

    def test_preflight_reports_every_unsupported_capability_without_fallback(self) -> None:
        request = ProviderCapabilityRequest(
            task_mode=TaskMode.FL2VA,
            media_kinds=(MediaKind.VIDEO,),
            reference_count=5,
            duration_seconds=121.0,
            deterministic_required=True,
            seed=7,
            cancellation_required=True,
            required_output_fields=("report",),
        )

        diagnostics = validate_provider_capabilities(descriptor(), request)
        codes = {diagnostic.code for diagnostic in diagnostics}
        self.assertEqual(
            codes,
            {
                "provider.unsupported_task_mode",
                "provider.unsupported_media_kind",
                "provider.reference_limit_exceeded",
                "provider.duration_limit_exceeded",
                "provider.output_field_unsupported",
            },
        )
        self.assertNotIn("provider.fallback", codes)
        self.assertTrue(all(diagnostic.severity.value == "fatal" for diagnostic in diagnostics))

    def test_preflight_rejects_seed_and_cancellation_when_capabilities_are_absent(self) -> None:
        limited = ProviderDescriptor(
            protocol_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
            identity=ProviderIdentity.REMOTE_CUSTOM,
            provider_version="remote-v1",
            capabilities=ProviderCapabilities(
                supported_task_modes=frozenset({TaskMode.T2VA}),
                supported_media=frozenset(),
                privacy_location=PrivacyLocation.REMOTE,
                network_requirement=NetworkRequirement.INTERNET,
                credential_requirement=CredentialRequirement.REQUIRED,
                limits=limits(),
                supports_determinism=False,
                supports_seed=False,
                supports_cancellation=False,
            ),
            output_contract=ProviderOutputContract(
                schema_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
                output_schema="h3.context.prompt.v1",
                required_fields=("prompt",),
            ),
        )
        diagnostics = validate_provider_capabilities(
            limited,
            ProviderCapabilityRequest(
                task_mode=TaskMode.T2VA,
                deterministic_required=True,
                seed=12,
                cancellation_required=True,
            ),
        )
        self.assertEqual(
            {diagnostic.code for diagnostic in diagnostics},
            {
                "provider.determinism_unsupported",
                "provider.seed_unsupported",
                "provider.cancellation_unsupported",
            },
        )

    def test_wire_schema_matches_protocol_vocabularies(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/provider_protocol_v1.schema.json",
        )
        self.assertEqual(schema["properties"]["protocol_version"]["const"], "1.0")
        self.assertEqual(
            set(schema["properties"]["identity"]["enum"]),
            {identity.value for identity in ProviderIdentity},
        )
        capabilities = schema["$defs"]["ProviderCapabilities"]["properties"]
        self.assertEqual(
            set(capabilities["privacy_location"]["enum"]),
            {location.value for location in PrivacyLocation},
        )
        self.assertEqual(
            set(capabilities["network_requirement"]["enum"]),
            {requirement.value for requirement in NetworkRequirement},
        )
        self.assertEqual(
            set(capabilities["credential_requirement"]["enum"]),
            {requirement.value for requirement in CredentialRequirement},
        )


if __name__ == "__main__":
    unittest.main()
