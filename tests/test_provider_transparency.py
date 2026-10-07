"""M7-04 provider transparency and privacy-mode regressions."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from comfyui_h3_context.core import (
    ProviderCostPolicy,
    ProviderExecutionPolicy,
    ProviderFallbackPolicy,
    ProviderIdentity,
    ProviderPrivacyMode,
    ProviderRetentionPolicy,
    ProviderTransparency,
    build_provider_transparency,
)
from comfyui_h3_context.nodes import (
    PROVIDER_TRANSPARENCY_NODE_ID,
    H3ContextProviderTransparencyNode,
    ProviderTransparencyNodeError,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "comfyui_h3_context" / "core" / "provider_policy.py"
WORKFLOW = ROOT / "workflows" / "m7_04_h3_context_provider_transparency.json"


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


class ProviderTransparencyTests(unittest.TestCase):
    def test_all_provider_modes_have_explicit_non_secret_disclosure(self) -> None:
        values = (
            policy(
                ProviderIdentity.MANUAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=True,
                network_allowed=False,
                upload_consent=False,
            ),
            policy(
                ProviderIdentity.LOCAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=True,
                network_allowed=False,
                upload_consent=False,
            ),
            policy(
                ProviderIdentity.REMOTE_CUSTOM,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.remote_custom",
            ),
            policy(
                ProviderIdentity.OFFICIAL_MINIMAX,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.official_minimax",
            ),
        )
        for selected in values:
            with self.subTest(provider=selected.provider):
                transparency = build_provider_transparency(selected)
                self.assertIsInstance(transparency, ProviderTransparency)
                wire = transparency.to_wire()
                self.assertEqual(wire["provider"], selected.provider.value)
                self.assertEqual(wire["privacy_mode"], selected.privacy_mode.value)
                self.assertIn("network", wire)
                self.assertIn("media_upload", wire)
                self.assertIn("credential", wire)
                self.assertIn("retention", wire)
                self.assertIn("cost", wire)
                self.assertIn("fallback", wire)
                self.assertNotIn("env.remote_custom", json.dumps(wire))
                self.assertNotIn("env.official_minimax", json.dumps(wire))

    def test_remote_without_consent_is_visible_but_blocked(self) -> None:
        transparency = build_provider_transparency(
            policy(
                ProviderIdentity.REMOTE_CUSTOM,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=True,
                network_allowed=False,
                upload_consent=False,
            )
        )
        self.assertFalse(transparency.execution_allowed)
        self.assertEqual(
            {
                "policy.upload_consent_required",
                "policy.network_required",
                "policy.offline_network_conflict",
                "policy.credential_reference_required",
            },
            {item.code for item in transparency.diagnostics},
        )
        encoded = json.dumps(transparency.to_wire(), sort_keys=True)
        self.assertIn("provider_defined", encoded)
        self.assertIn("provider_pricing", encoded)
        self.assertIn("never_automatic", encoded)
        self.assertNotIn("fallback_selected", encoded)

    def test_fallback_is_disclosed_as_caller_selected_only(self) -> None:
        transparency = build_provider_transparency(
            policy(
                ProviderIdentity.REMOTE_CUSTOM,
                privacy=ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.remote_custom",
                fallback_provider=ProviderIdentity.LOCAL,
            )
        )
        self.assertEqual(transparency.fallback_policy, ProviderFallbackPolicy.CALLER_SELECTED_ONLY)
        self.assertEqual(transparency.fallback_provider, ProviderIdentity.LOCAL)
        self.assertIn("fallback=caller_selected_only", transparency.disclosure)
        self.assertIn("caller-selected", transparency.disclosure)

    def test_policy_and_node_inputs_are_explicit_and_invalid_types_fail_closed(self) -> None:
        required = H3ContextProviderTransparencyNode.INPUT_TYPES()["required"]
        self.assertEqual(
            tuple(required),
            (
                "provider",
                "privacy_mode",
                "offline",
                "network_allowed",
                "upload_consent",
                "credential_reference",
                "fallback_provider",
            ),
        )
        self.assertEqual(H3ContextProviderTransparencyNode.NODE_ID, PROVIDER_TRANSPARENCY_NODE_ID)
        with self.assertRaises(ProviderTransparencyNodeError):
            H3ContextProviderTransparencyNode().describe(
                "remote_custom",
                "explicit_remote",
                "yes",
                False,
                False,
                "",
                "none",
            )

    def test_enums_and_wire_shape_are_versioned_json_safe(self) -> None:
        transparency = build_provider_transparency(
            policy(
                ProviderIdentity.MANUAL,
                privacy=ProviderPrivacyMode.LOCAL_ONLY,
                offline=True,
                network_allowed=False,
                upload_consent=False,
            )
        )
        self.assertEqual(transparency.schema, "h3-context-provider-transparency/1")
        self.assertEqual(transparency.retention_policy, ProviderRetentionPolicy.NONE)
        self.assertEqual(transparency.cost_policy, ProviderCostPolicy.NONE)
        decoded = json.loads(json.dumps(transparency.to_wire(), ensure_ascii=False))
        self.assertEqual(decoded, transparency.to_wire())

    def test_module_has_no_optional_runtime_imports(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
        forbidden = {
            "aiohttp",
            "comfy",
            "comfy_api",
            "cv2",
            "diffusers",
            "httpx",
            "moviepy",
            "numpy",
            "requests",
            "torch",
            "transformers",
        }
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports)

    def test_fixture_is_pinned_and_contains_no_provider_secrets(self) -> None:
        fixture = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(fixture["fixture_id"], "m7-04-provider-transparency")
        self.assertEqual(fixture["prompt"]["4"]["inputs"]["provider"], "remote_custom")
        encoded = json.dumps(fixture, sort_keys=True).casefold()
        for marker in ("authorization", "bearer ", "api_key", "password", "secret", "sig="):
            self.assertNotIn(marker, encoded)


if __name__ == "__main__":
    unittest.main()
