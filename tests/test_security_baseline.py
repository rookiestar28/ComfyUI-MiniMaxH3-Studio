"""M0-06 adversarial policy and no-side-effect security contract tests."""

from __future__ import annotations

import ast
import dataclasses
import tempfile
import unittest
from pathlib import Path

from comfyui_h3_context.core.security import (
    RedactedReceipt,
    RemoteExecutionPolicy,
    ResourceLimits,
    SecurityPolicyError,
    validate_local_path,
    validate_remote_url,
)

ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = ROOT / "comfyui_h3_context" / "core"


class SecurityBaselineTests(unittest.TestCase):
    def test_provider_privacy_and_upload_consent_are_explicit(self) -> None:
        local = RemoteExecutionPolicy(
            provider="manual",
            privacy_mode="local_only",
            upload_consent=False,
        )
        self.assertEqual(local.provider, "manual")

        with self.assertRaises(SecurityPolicyError):
            RemoteExecutionPolicy(
                provider="official_minimax",
                privacy_mode="local_only",
                upload_consent=False,
            )
        with self.assertRaises(SecurityPolicyError):
            RemoteExecutionPolicy(
                provider="official_minimax",
                privacy_mode="explicit_remote",
                upload_consent=False,
            )
        with self.assertRaises(SecurityPolicyError):
            RemoteExecutionPolicy(
                provider="manual",
                privacy_mode="explicit_remote",
                upload_consent=True,
            )
        with self.assertRaises(SecurityPolicyError):
            RemoteExecutionPolicy(
                provider="unregistered",  # type: ignore[arg-type]
                privacy_mode="local_only",
                upload_consent=False,
            )

        remote = RemoteExecutionPolicy(
            provider="remote_custom",
            privacy_mode="explicit_remote",
            upload_consent=True,
        )
        self.assertTrue(remote.upload_consent)

    def test_resource_limits_are_finite_and_positive_or_explicitly_disabled(self) -> None:
        limits = ResourceLimits(
            max_bytes=1024,
            max_duration_seconds=10.0,
            max_width=1920,
            max_height=1080,
            max_frames=97,
            max_sample_rate=48_000,
            max_references=4,
            max_concurrency=1,
            max_memory_bytes=2**30,
            max_wall_time_seconds=120.0,
            max_retries=0,
            max_cache_ttl_seconds=0.0,
        )
        self.assertEqual(limits.max_retries, 0)
        self.assertEqual(limits.max_cache_ttl_seconds, 0.0)

        fields = dataclasses.asdict(limits)
        for name, value in fields.items():
            invalid = 0.0 if isinstance(value, float) else 0
            with self.subTest(field=name):
                if name in {"max_retries", "max_cache_ttl_seconds"}:
                    continue
                with self.assertRaises(SecurityPolicyError):
                    ResourceLimits(**{**fields, name: invalid})

        with self.assertRaises(SecurityPolicyError):
            ResourceLimits(**{**fields, "max_duration_seconds": float("inf")})
        with self.assertRaises(SecurityPolicyError):
            ResourceLimits(**{**fields, "max_references": True})

    def test_remote_url_is_allowlist_first_and_network_free(self) -> None:
        accepted = validate_remote_url(
            "https://api.example.test/v2/task",
            allowed_hosts={"api.example.test"},
        )
        self.assertEqual(accepted.hostname, "api.example.test")
        self.assertEqual(accepted.path, "/v2/task")

        blocked = (
            "http://api.example.test/v2/task",
            "https://other.example.test/v2/task",
            "https://api.example.test@127.0.0.1/v2/task",
            "https://127.0.0.1/v2/task",
            "https://10.0.0.4/v2/task",
            "https://[::1]/v2/task",
            "https://api.example.test:bad/v2/task",
            "https://api.example.test/v2/task?token=secret",
            "https://api.example.test/v2/task#fragment",
            "https://api.example.test\\@other.example.test/v2/task",
        )
        for url in blocked:
            with self.subTest(url=url), self.assertRaises(SecurityPolicyError):
                validate_remote_url(url, allowed_hosts={"api.example.test"})

    def test_local_path_is_rooted_and_symlink_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe.txt"
            safe.write_text("safe", encoding="utf-8")
            self.assertEqual(validate_local_path("safe.txt", root), safe.resolve())

            outside = root.parent / "outside-h3-context.txt"
            outside.write_text("outside", encoding="utf-8")
            try:
                for candidate in ("../outside-h3-context.txt", outside):
                    with self.subTest(candidate=candidate), self.assertRaises(SecurityPolicyError):
                        validate_local_path(candidate, root)

                directory = root / "directory"
                directory.mkdir()
                with self.assertRaises(SecurityPolicyError):
                    validate_local_path(directory, root)

                link = root / "link.txt"
                try:
                    link.symlink_to(safe)
                except (OSError, NotImplementedError):
                    self.skipTest("symlinks are unavailable in this environment")
                with self.assertRaises(SecurityPolicyError):
                    validate_local_path(link, root)
                with self.assertRaises(SecurityPolicyError):
                    validate_local_path("link.txt", root)
            finally:
                outside.unlink(missing_ok=True)

    def test_receipt_has_only_bounded_public_fields(self) -> None:
        receipt = RedactedReceipt(
            provider="official_minimax",
            status="succeeded",
            task_id="task_123-abc",
            request_fingerprint="sha256:" + "a" * 64,
            output_fingerprint="sha256:" + "b" * 64,
        )
        self.assertEqual(
            set(receipt.to_public_dict()),
            {"provider", "status", "task_id", "request_fingerprint", "output_fingerprint"},
        )
        self.assertNotIn("credential", receipt.to_public_dict())
        self.assertNotIn("path", receipt.to_public_dict())
        self.assertNotIn("prompt", receipt.to_public_dict())
        for field in dataclasses.fields(receipt):
            self.assertNotIn(field.name, {"credential", "token", "secret", "path", "prompt"})

        with self.assertRaises(SecurityPolicyError):
            RedactedReceipt(
                provider="official_minimax",
                status="failed",
                task_id="private/path.txt",
            )
        with self.assertRaises(SecurityPolicyError):
            RedactedReceipt(
                provider="official_minimax",
                status="failed",
                request_fingerprint="not-a-fingerprint",
            )

    def test_core_security_module_has_no_network_or_media_imports(self) -> None:
        path = CORE_ROOT / "security.py"
        self.assertTrue(path.is_file())
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = {
            "aiohttp",
            "comfy",
            "comfy_api",
            "cv2",
            "diffusers",
            "httpx",
            "moviepy",
            "requests",
            "torch",
            "transformers",
        }
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imports.append((node.module or "").split(".", 1)[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports)
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("urlopen(", source)
        self.assertNotIn("requests.", source)
        self.assertNotIn("socket.", source)


if __name__ == "__main__":
    unittest.main()
