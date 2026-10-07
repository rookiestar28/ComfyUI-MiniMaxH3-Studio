"""M4-02 explicit provider selection, policy, and credential-boundary tests."""

from __future__ import annotations

import json
import unittest

from comfyui_h3_context.core import (
    CURRENT_PROVIDER_PROTOCOL_VERSION,
    DEFAULT_PROVIDER_POLICY,
    CredentialRequirement,
    CredentialResolver,
    MediaKind,
    NetworkRequirement,
    PrivacyLocation,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderExecutionPolicy,
    ProviderIdentity,
    ProviderOutputContract,
    ProviderPrivacyMode,
    ResolvedCredential,
    ResourceLimits,
    TaskMode,
    resolve_provider_credential,
    select_provider_descriptor,
    validate_provider_policy,
)
from comfyui_h3_context.core.errors import ContractValidationError, SecurityPolicyError


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


def provider(
    identity: ProviderIdentity,
    *,
    privacy: PrivacyLocation,
    network: NetworkRequirement,
    credential: CredentialRequirement,
) -> ProviderDescriptor:
    return ProviderDescriptor(
        protocol_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
        identity=identity,
        provider_version="provider-v1",
        capabilities=ProviderCapabilities(
            supported_task_modes=frozenset({TaskMode.T2VA}),
            supported_media=frozenset({MediaKind.IMAGE}),
            privacy_location=privacy,
            network_requirement=network,
            credential_requirement=credential,
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


LOCAL = provider(
    ProviderIdentity.LOCAL,
    privacy=PrivacyLocation.LOCAL,
    network=NetworkRequirement.NONE,
    credential=CredentialRequirement.OPTIONAL,
)
REMOTE = provider(
    ProviderIdentity.REMOTE_CUSTOM,
    privacy=PrivacyLocation.REMOTE,
    network=NetworkRequirement.INTERNET,
    credential=CredentialRequirement.REQUIRED,
)


class StubResolver:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.references: list[str] = []

    def resolve(self, reference: str) -> ResolvedCredential:
        self.references.append(reference)
        if self.fail:
            raise RuntimeError("secret=do-not-leak")
        return ResolvedCredential(reference=reference, value="runtime-secret")


class ProviderPolicyTests(unittest.TestCase):
    def test_default_policy_is_manual_offline_and_wire_safe(self) -> None:
        self.assertEqual(DEFAULT_PROVIDER_POLICY.provider, ProviderIdentity.MANUAL)
        self.assertTrue(DEFAULT_PROVIDER_POLICY.offline)
        self.assertFalse(DEFAULT_PROVIDER_POLICY.network_allowed)
        wire = DEFAULT_PROVIDER_POLICY.to_wire()
        encoded = json.dumps(wire, sort_keys=True)
        self.assertEqual(wire["privacy_mode"], "local_only")
        self.assertNotIn("runtime-secret", encoded)
        self.assertNotIn("api_key", encoded.casefold())
        self.assertNotIn("path", wire)

    def test_all_provider_identities_are_distinguishable_and_selection_is_exact(self) -> None:
        policies = (
            ProviderExecutionPolicy(ProviderIdentity.MANUAL, ProviderPrivacyMode.LOCAL_ONLY),
            ProviderExecutionPolicy(ProviderIdentity.LOCAL, ProviderPrivacyMode.LOCAL_ONLY),
            ProviderExecutionPolicy(
                ProviderIdentity.REMOTE_CUSTOM,
                ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.remote_custom",
            ),
            ProviderExecutionPolicy(
                ProviderIdentity.OFFICIAL_MINIMAX,
                ProviderPrivacyMode.EXPLICIT_REMOTE,
                offline=False,
                network_allowed=True,
                upload_consent=True,
                credential_reference="env.official_minimax",
            ),
        )
        self.assertEqual({policy.provider for policy in policies}, set(ProviderIdentity))
        self.assertIs(select_provider_descriptor(policies[1], (LOCAL, REMOTE)), LOCAL)
        with self.assertRaises(SecurityPolicyError):
            select_provider_descriptor(policies[2], (LOCAL,))

    def test_remote_policy_requires_consent_network_and_explicit_privacy(self) -> None:
        invalid = ProviderExecutionPolicy(
            ProviderIdentity.REMOTE_CUSTOM,
            ProviderPrivacyMode.LOCAL_ONLY,
            offline=True,
            network_allowed=False,
            upload_consent=False,
            credential_reference="env.remote_custom",
        )
        codes = {diagnostic.code for diagnostic in validate_provider_policy(REMOTE, invalid)}
        self.assertEqual(
            codes,
            {
                "policy.privacy_mode_mismatch",
                "policy.network_required",
                "policy.upload_consent_required",
                "policy.offline_network_conflict",
            },
        )

    def test_default_local_policy_has_no_network_or_upload_side_effect(self) -> None:
        local_policy = ProviderExecutionPolicy(
            ProviderIdentity.LOCAL, ProviderPrivacyMode.LOCAL_ONLY
        )
        diagnostics = validate_provider_policy(LOCAL, local_policy)
        self.assertEqual(diagnostics, ())
        self.assertIs(select_provider_descriptor(local_policy, (LOCAL, REMOTE)), LOCAL)

    def test_wire_policy_rejects_secret_like_credential_references(self) -> None:
        for reference in ("api_token", "private/path", "signed_resource", "secret-key"):
            with self.subTest(reference=reference), self.assertRaises(SecurityPolicyError):
                ProviderExecutionPolicy(
                    ProviderIdentity.REMOTE_CUSTOM,
                    ProviderPrivacyMode.EXPLICIT_REMOTE,
                    offline=False,
                    network_allowed=True,
                    upload_consent=True,
                    credential_reference=reference,
                )

        policy = ProviderExecutionPolicy(
            ProviderIdentity.REMOTE_CUSTOM,
            ProviderPrivacyMode.EXPLICIT_REMOTE,
            offline=False,
            network_allowed=True,
            upload_consent=True,
            credential_reference="env.remote_custom",
        )
        self.assertEqual(policy.to_wire()["credential_reference"], "env.remote_custom")

    def test_runtime_credential_resolution_is_injected_and_not_serialized(self) -> None:
        policy = ProviderExecutionPolicy(
            ProviderIdentity.REMOTE_CUSTOM,
            ProviderPrivacyMode.EXPLICIT_REMOTE,
            offline=False,
            network_allowed=True,
            upload_consent=True,
            credential_reference="env.remote_custom",
        )
        resolver = StubResolver()
        self.assertIsInstance(resolver, CredentialResolver)
        resolved = resolve_provider_credential(policy, REMOTE, resolver)
        assert resolved is not None
        self.assertEqual(resolved.reference, "env.remote_custom")
        self.assertEqual(resolver.references, ["env.remote_custom"])
        self.assertNotIn("runtime-secret", json.dumps(policy.to_wire()))

        with self.assertRaises(SecurityPolicyError) as failure:
            resolve_provider_credential(policy, REMOTE, StubResolver(fail=True))
        self.assertNotIn("do-not-leak", str(failure.exception))

    def test_required_credentials_and_optional_credentials_fail_closed(self) -> None:
        missing = ProviderExecutionPolicy(
            ProviderIdentity.REMOTE_CUSTOM,
            ProviderPrivacyMode.EXPLICIT_REMOTE,
            offline=False,
            network_allowed=True,
            upload_consent=True,
        )
        with self.assertRaises(SecurityPolicyError):
            resolve_provider_credential(missing, REMOTE, StubResolver())

        optional = resolve_provider_credential(
            ProviderExecutionPolicy(ProviderIdentity.LOCAL, ProviderPrivacyMode.LOCAL_ONLY),
            LOCAL,
            None,
        )
        self.assertIsNone(optional)

    def test_fallback_is_metadata_only_and_never_implicitly_selected(self) -> None:
        policy = ProviderExecutionPolicy(
            ProviderIdentity.REMOTE_CUSTOM,
            ProviderPrivacyMode.EXPLICIT_REMOTE,
            offline=False,
            network_allowed=True,
            upload_consent=True,
            credential_reference="env.remote_custom",
            fallback_provider=ProviderIdentity.LOCAL,
        )
        selected = select_provider_descriptor(policy, (LOCAL, REMOTE))
        self.assertIs(selected, REMOTE)
        self.assertEqual(policy.to_wire()["fallback_provider"], "local")
        with self.assertRaises(SecurityPolicyError):
            ProviderExecutionPolicy(
                ProviderIdentity.LOCAL,
                ProviderPrivacyMode.LOCAL_ONLY,
                fallback_provider=ProviderIdentity.LOCAL,
            )

    def test_invalid_policy_types_fail_closed(self) -> None:
        with self.assertRaises(ContractValidationError):
            ProviderExecutionPolicy(
                ProviderIdentity.LOCAL,
                ProviderPrivacyMode.LOCAL_ONLY,
                offline="yes",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
