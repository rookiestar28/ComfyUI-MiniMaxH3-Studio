"""Cross-layer assisted-authoring declarations and provider-free rollback guarantees."""

from __future__ import annotations

import http.client
import socket
import subprocess
import unittest
from unittest.mock import patch

from comfyui_h3_context.adapters.prompt_model_transport import (
    LoopbackJsonExchange,
    PromptModelTransportError,
    RemoteHttpsExchange,
)
from comfyui_h3_context.core.capability_manifest import CapabilityMaturity
from comfyui_h3_context.core.installation_profiles import load_installation_profiles
from comfyui_h3_context.core.prompt_model_provider import (
    AdmittedDestination,
    PromptModelCapabilities,
    PromptModelContractError,
    PromptModelFamily,
    build_prompt_model_capabilities,
    load_prompt_model_catalog,
)
from comfyui_h3_context.core.prompt_model_provider import (
    LegacyPromptModelProfile as PromptModelProfile,
)
from comfyui_h3_context.core.provider_settings import (
    ProviderReadiness,
    ProviderSettingsIntent,
    ProviderSettingsState,
)
from comfyui_h3_context.public_api import get_public_manifest_v2

EXPECTED_FAMILY_CAPABILITIES = {
    PromptModelFamily.IN_PROCESS_GGUF: (
        "provider.prompt_model_in_process_gguf",
        CapabilityMaturity.UNSUPPORTED,
    ),
    PromptModelFamily.LOOPBACK_SERVER: (
        "provider.prompt_model_loopback_server",
        CapabilityMaturity.INJECTED_ONLY,
    ),
    PromptModelFamily.OLLAMA: (
        "provider.prompt_model_ollama",
        CapabilityMaturity.INJECTED_ONLY,
    ),
    PromptModelFamily.REMOTE_OPENAI_COMPATIBLE: (
        "provider.prompt_model_remote_openai_compatible",
        CapabilityMaturity.INJECTED_ONLY,
    ),
    PromptModelFamily.REMOTE_ANTHROPIC: (
        "provider.prompt_model_remote_anthropic",
        CapabilityMaturity.INJECTED_ONLY,
    ),
}


def _capabilities(family: PromptModelFamily) -> PromptModelCapabilities:
    remote = family in {
        PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        PromptModelFamily.REMOTE_ANTHROPIC,
    }
    in_process = family is PromptModelFamily.IN_PROCESS_GGUF
    return build_prompt_model_capabilities(
        {
            "family": family.value,
            "accepted_media": ["text"],
            "provider_managed_context": not in_process,
            "provider_managed_kv_cache": not in_process,
            "max_request_bytes": 262_144,
            "max_context_tokens": 32_768,
            "max_output_tokens": 4_096,
            "streaming": False,
            "local_only": not remote,
            "requires_credential": remote,
        }
    )


def _profile(family: PromptModelFamily) -> PromptModelProfile:
    endpoints = {
        PromptModelFamily.IN_PROCESS_GGUF: "",
        PromptModelFamily.LOOPBACK_SERVER: "http://127.0.0.1:8000/v1",
        PromptModelFamily.OLLAMA: "http://127.0.0.1:11434",
        PromptModelFamily.REMOTE_OPENAI_COMPATIBLE: "https://api.example.com/v1",
        PromptModelFamily.REMOTE_ANTHROPIC: "https://api.anthropic.com",
    }
    return PromptModelProfile(
        profile_id=f"closeout.{family.value}",
        family=family,
        endpoint=endpoints[family],
        model_id="model-a",
        model_digest="sha256:" + "3" * 64,
        adapter_version="1.0.0",
        parser_version="h3.prompt_model.draft_json.v1",
        license_id="test-only",
        license_source="synthetic_fixture",
        license_text_sha256="sha256:" + "4" * 64,
        capabilities=_capabilities(family),
    )


class AssistedAuthoringDeclarationTests(unittest.TestCase):
    def test_public_manifest_declares_every_family_without_overclaiming_live_evidence(self) -> None:
        registry = get_public_manifest_v2().capabilities
        for family, (capability_id, maturity) in EXPECTED_FAMILY_CAPABILITIES.items():
            with self.subTest(family=family.value):
                declaration = registry.get(capability_id)
                self.assertEqual(declaration.provider_family, family.value)
                self.assertIs(declaration.maturity, maturity)
                self.assertEqual(
                    declaration.requires_consent,
                    family
                    in {
                        PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
                        PromptModelFamily.REMOTE_ANTHROPIC,
                    },
                )

        self.assertIs(
            registry.get("authoring.assisted_draft").maturity,
            CapabilityMaturity.DETERMINISTIC,
        )
        self.assertIs(
            registry.get("provider.prompt_model_settings").maturity,
            CapabilityMaturity.DETERMINISTIC,
        )
        self.assertIs(
            registry.get("provider.prompt_model_readiness").maturity,
            CapabilityMaturity.INJECTED_ONLY,
        )

    def test_installation_inventory_names_external_owners_but_not_unavailable_gguf(self) -> None:
        profiles = {
            profile.profile_id: profile for profile in load_installation_profiles().profiles
        }
        self.assertIn("external_openai_compatible_loopback", profiles)
        self.assertIn("remote_openai_compatible_service", profiles)
        self.assertIn("remote_anthropic_service", profiles)
        self.assertNotIn("in_process_gguf", profiles)
        for profile_id in (
            "external_ollama",
            "external_openai_compatible_loopback",
            "remote_openai_compatible_service",
            "remote_anthropic_service",
        ):
            with self.subTest(profile_id=profile_id):
                profile = profiles[profile_id]
                self.assertFalse(profile.required)
                self.assertEqual(profile.download_policy, "forbidden")
                self.assertEqual(profile.fallback, "none")
                self.assertEqual(profile.runtime_entries, ())


class ProviderFreeDefaultTests(unittest.TestCase):
    def test_loading_every_default_closeout_surface_makes_no_observation_or_network_call(
        self,
    ) -> None:
        with (
            patch.object(socket, "getaddrinfo") as getaddrinfo,
            patch.object(socket, "create_connection") as create_connection,
            patch.object(http.client, "HTTPConnection") as http_connection,
            patch.object(http.client, "HTTPSConnection") as https_connection,
            patch.object(subprocess, "run") as run_process,
        ):
            catalog = load_prompt_model_catalog()
            projection = ProviderSettingsState.from_catalog().project()
            installation = load_installation_profiles()
            manifest = get_public_manifest_v2()

        self.assertEqual(len(catalog.profiles), 4)
        self.assertFalse(projection.catalog_empty)
        self.assertEqual(projection.selected_profile_id, "")
        self.assertIs(projection.readiness, ProviderReadiness.NOT_CONFIGURED)
        self.assertIsNone(projection.disclosure)
        self.assertEqual(
            projection.assisted_authoring.to_wire(),
            {
                "available": True,
                "selected": False,
                "ready": False,
                "authorized_for_this_action": False,
                "defaulted": False,
            },
        )
        self.assertGreater(len(installation.profiles), 4)
        self.assertGreater(len(manifest.capabilities.declarations), 0)
        for observed in (
            getaddrinfo,
            create_connection,
            http_connection,
            https_connection,
            run_process,
        ):
            self.assertFalse(observed.called, observed)

    def test_every_family_can_be_cleared_without_fallback_or_transport_construction(self) -> None:
        with (
            patch.object(socket, "getaddrinfo") as getaddrinfo,
            patch.object(socket, "create_connection") as create_connection,
            patch.object(http.client, "HTTPConnection") as http_connection,
            patch.object(http.client, "HTTPSConnection") as https_connection,
        ):
            for family in PromptModelFamily:
                with self.subTest(family=family.value):
                    profile = _profile(family)
                    state = ProviderSettingsState(profiles=(profile,))
                    selected = state.apply(
                        ProviderSettingsIntent.SELECT_PROFILE,
                        {"profile_id": profile.profile_id},
                    )
                    self.assertTrue(selected.accepted)
                    cleared = state.apply(ProviderSettingsIntent.CLEAR_SELECTION)
                    self.assertTrue(cleared.accepted)
                    projection = cleared.projection
                    self.assertEqual(projection.selected_profile_id, "")
                    self.assertIs(projection.readiness, ProviderReadiness.NOT_CONFIGURED)
                    self.assertIsNone(projection.disclosure)
                    self.assertIsNone(projection.consent)
                    self.assertIsNone(projection.diagnostic)

        for observed in (
            getaddrinfo,
            create_connection,
            http_connection,
            https_connection,
        ):
            self.assertFalse(observed.called, observed)

    def test_transport_constructors_refuse_raw_endpoints_and_destination_forgery(self) -> None:
        with self.assertRaises(PromptModelContractError):
            AdmittedDestination(
                family=PromptModelFamily.OLLAMA,
                scheme="http",
                host="127.0.0.1",
                port=11434,
                path="/",
                destination=object(),  # type: ignore[arg-type]
                loopback=True,
            )
        with self.assertRaises(PromptModelTransportError):
            LoopbackJsonExchange("http://127.0.0.1:11434")  # type: ignore[arg-type]
        with self.assertRaises(PromptModelTransportError):
            RemoteHttpsExchange(
                "https://api.example.com/v1",  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
