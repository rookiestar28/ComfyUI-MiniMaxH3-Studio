"""M4-05 media transfer, consent, credential, SSRF, and redaction regressions."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from comfyui_h3_context.core import (
    CURRENT_PROVIDER_PROTOCOL_VERSION,
    OFFICIAL_CONTEXT_IR_DESCRIPTOR,
    CredentialRequirement,
    MediaKind,
    MediaSource,
    MediaSourceKind,
    MediaTransferPolicy,
    MediaTransferTimeouts,
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
    ValidatedMediaSource,
    build_provider_consent_notice,
    validate_media_sources,
    validate_remote_url,
)
from comfyui_h3_context.core.errors import SecurityPolicyError

ROOT = Path(__file__).resolve().parents[1]


def transfer_policy(root: Path) -> MediaTransferPolicy:
    return MediaTransferPolicy(
        allowed_hosts=("media.example.test",),
        allowed_url_path_prefixes=("/assets/",),
        allowed_local_roots=(root,),
        max_bytes=1_000_000,
        max_duration_seconds=15.0,
        max_references=3,
        timeouts=MediaTransferTimeouts(
            connect_seconds=2.0,
            read_seconds=8.0,
            total_seconds=12.0,
        ),
    )


def image_url(url: str = "https://media.example.test/assets/frame.png") -> MediaSource:
    return MediaSource(
        source_kind=MediaSourceKind.REMOTE_URL,
        locator=url,
        media_kind=MediaKind.IMAGE,
        media_type="image/png",
        size_bytes=1024,
    )


def image_path(path: Path) -> MediaSource:
    return MediaSource(
        source_kind=MediaSourceKind.LOCAL_PATH,
        locator=path,
        media_kind=MediaKind.IMAGE,
        media_type="image/png",
        size_bytes=1024,
    )


def official_policy(*, consent: bool = True) -> ProviderExecutionPolicy:
    return ProviderExecutionPolicy(
        provider=ProviderIdentity.OFFICIAL_MINIMAX,
        privacy_mode=ProviderPrivacyMode.EXPLICIT_REMOTE,
        offline=False,
        network_allowed=True,
        upload_consent=consent,
        credential_reference="env.official_minimax",
    )


class MediaSecurityTests(unittest.TestCase):
    def test_timeout_and_transfer_policy_require_finite_explicit_bounds(self) -> None:
        for values in (
            (0, 8.0, 12.0),
            (2.0, float("inf"), 12.0),
            (2.0, 8.0, 1.0),
        ):
            with self.subTest(values=values), self.assertRaises(SecurityPolicyError):
                MediaTransferTimeouts(*values)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(SecurityPolicyError):
                MediaTransferPolicy(
                    allowed_hosts=(),
                    allowed_url_path_prefixes=(),
                    allowed_local_roots=(),
                    max_bytes=0,
                    max_duration_seconds=15.0,
                    max_references=1,
                    timeouts=MediaTransferTimeouts(1.0, 1.0, 1.0),
                )
            valid = transfer_policy(root)
            self.assertEqual(valid.max_references, 3)
            self.assertEqual(valid.timeouts.total_seconds, 12.0)

    def test_remote_url_and_path_allowlists_are_exact_and_network_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = transfer_policy(Path(temporary))
            validated = validate_media_sources((image_url(),), policy)
            self.assertIsInstance(validated[0], ValidatedMediaSource)
            self.assertEqual(validated[0].source_kind, MediaSourceKind.REMOTE_URL)
            public = validated[0].to_public_dict()
            self.assertNotIn("locator", public)
            self.assertNotIn("media.example.test", json.dumps(public))

            blocked = (
                "http://media.example.test/assets/frame.png",
                "https://other.example.test/assets/frame.png",
                "https://media.example.test/private/frame.png",
                "https://media.example.test/assets/frame.png?token=secret",
                "https://media.example.test/assets/frame.png#fragment",
                "https://media.example.test@127.0.0.1/assets/frame.png",
                "https://127.0.0.1/assets/frame.png",
                "https://media.example.test/assets/%2e%2e/private.png",
                "https://media.example.test/assets/../private.png",
            )
            for url in blocked:
                with self.subTest(url=url), self.assertRaises(SecurityPolicyError):
                    validate_media_sources((image_url(url),), policy)

            with self.assertRaises(SecurityPolicyError):
                validate_remote_url(
                    "https://media.example.test/assets/../private.png",
                    allowed_hosts=policy.allowed_hosts,
                    allowed_path_prefixes=policy.allowed_url_path_prefixes,
                )

    def test_local_sources_require_existing_regular_non_symlink_files_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe.png"
            safe.write_bytes(b"safe")
            policy = transfer_policy(root)
            validated = validate_media_sources((image_path(safe),), policy)
            self.assertEqual(validated[0].source_kind, MediaSourceKind.LOCAL_PATH)
            self.assertNotIn(str(safe), repr(validated[0]))
            self.assertNotIn(str(safe), json.dumps(validated[0].to_public_dict()))

            for candidate in (
                root / "missing.png",
                root.parent / "outside.png",
                root / ".." / root.name / ".." / "outside.png",
            ):
                with self.subTest(candidate=candidate), self.assertRaises(SecurityPolicyError):
                    validate_media_sources((image_path(candidate),), policy)

            directory = root / "directory"
            directory.mkdir()
            with self.assertRaises(SecurityPolicyError):
                validate_media_sources((image_path(directory),), policy)

            link = root / "link.png"
            try:
                link.symlink_to(safe)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable in this environment")
            with self.assertRaises(SecurityPolicyError):
                validate_media_sources((image_path(link),), policy)

            nested = root / "nested"
            nested.mkdir()
            nested_link = nested / "link"
            nested_link.symlink_to(root)
            with self.assertRaises(SecurityPolicyError):
                validate_media_sources((image_path(nested_link / "safe.png"),), policy)

    def test_size_duration_and_reference_count_limits_fail_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = transfer_policy(Path(temporary))
            oversized = MediaSource(
                MediaSourceKind.REMOTE_URL,
                "https://media.example.test/assets/large.mp4",
                MediaKind.VIDEO,
                "video/mp4",
                1_000_001,
                duration_seconds=5.0,
            )
            too_long = MediaSource(
                MediaSourceKind.REMOTE_URL,
                "https://media.example.test/assets/long.mp4",
                MediaKind.VIDEO,
                "video/mp4",
                1024,
                duration_seconds=15.1,
            )
            for source in (oversized, too_long):
                with self.subTest(source=source.media_type), self.assertRaises(SecurityPolicyError):
                    validate_media_sources((source,), policy)

            sources = (image_url("https://media.example.test/assets/a.png"),) * 4
            with self.assertRaises(SecurityPolicyError):
                validate_media_sources(sources, policy)

            with self.assertRaises(SecurityPolicyError):
                MediaSource(
                    MediaSourceKind.REMOTE_URL,
                    "https://media.example.test/assets/missing.mp3",
                    MediaKind.AUDIO,
                    "audio/mpeg",
                    1024,
                )

    def test_consent_notice_is_visible_but_never_contains_credentials_or_locators(self) -> None:
        notice = build_provider_consent_notice(OFFICIAL_CONTEXT_IR_DESCRIPTOR, official_policy())
        public = notice.to_public_dict()
        encoded = json.dumps(public, sort_keys=True)
        self.assertTrue(notice.requires_explicit_consent)
        self.assertIn("official_minimax", notice.message)
        self.assertIn("upload", notice.message.casefold())
        self.assertIn("runtime", notice.message.casefold())
        self.assertNotIn("env.official_minimax", encoded)
        self.assertNotIn("secret", encoded.casefold())
        self.assertNotIn("https://", encoded)
        self.assertNotIn("/", encoded)

        no_consent = build_provider_consent_notice(
            OFFICIAL_CONTEXT_IR_DESCRIPTOR, official_policy(consent=False)
        )
        self.assertFalse(no_consent.consent_granted)

    def test_credentials_and_receipt_like_public_values_are_runtime_only(self) -> None:
        credential = ResolvedCredential("env.official_minimax", "runtime-secret-value")
        self.assertNotIn("runtime-secret-value", repr(credential))
        self.assertNotIn("runtime-secret-value", str(credential))
        self.assertNotIn("runtime-secret-value", json.dumps(credential.to_public_dict()))
        wire = official_policy().to_wire()
        self.assertNotIn("runtime-secret-value", json.dumps(wire))
        self.assertNotIn("Authorization", json.dumps(wire))

    def test_security_module_has_no_network_or_media_runtime_dependency(self) -> None:
        path = ROOT / "comfyui_h3_context" / "core" / "security.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = {
            "aiohttp",
            "cv2",
            "httpx",
            "moviepy",
            "requests",
            "socket",
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


class ConsentDescriptorFixtureTests(unittest.TestCase):
    def test_consent_notice_rejects_invalid_descriptor_types(self) -> None:
        with self.assertRaises(ValueError):
            build_provider_consent_notice(
                object(),  # type: ignore[arg-type]
                official_policy(),
            )

    def test_consent_notice_is_non_secret_for_a_local_provider(self) -> None:
        limits = ResourceLimits(
            max_bytes=1024,
            max_duration_seconds=5.0,
            max_width=256,
            max_height=256,
            max_frames=1,
            max_sample_rate=8000,
            max_references=1,
            max_concurrency=1,
            max_memory_bytes=1024,
            max_wall_time_seconds=5.0,
            max_retries=0,
            max_cache_ttl_seconds=0.0,
        )
        descriptor = ProviderDescriptor(
            protocol_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
            identity=ProviderIdentity.LOCAL,
            provider_version="local-v1",
            capabilities=ProviderCapabilities(
                supported_task_modes=frozenset(),
                supported_media=frozenset(),
                privacy_location=PrivacyLocation.LOCAL,
                network_requirement=NetworkRequirement.NONE,
                credential_requirement=CredentialRequirement.NONE,
                limits=limits,
                supports_determinism=True,
                supports_seed=True,
                supports_cancellation=True,
            ),
            output_contract=ProviderOutputContract(
                schema_version=CURRENT_PROVIDER_PROTOCOL_VERSION,
                output_schema="local.output.v1",
                required_fields=("prompt",),
            ),
        )
        policy = ProviderExecutionPolicy(ProviderIdentity.LOCAL, ProviderPrivacyMode.LOCAL_ONLY)
        notice = build_provider_consent_notice(descriptor, policy)
        self.assertFalse(notice.requires_explicit_consent)
        self.assertFalse(notice.consent_granted)
        self.assertNotIn("upload", notice.message.casefold())


if __name__ == "__main__":
    unittest.main()
