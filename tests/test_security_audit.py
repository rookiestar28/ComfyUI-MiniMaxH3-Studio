"""M8-03 offline license, privacy, dependency, and release-supply-chain audit tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import comfyui_h3_context.adapters.ollama_native as ollama_native
from scripts.security_audit import (
    _audit_license,
    _audit_public_markers,
    audit_repository,
    audit_runtime_package,
    audit_workflow_text,
)

ROOT = Path(__file__).resolve().parents[1]


class SecurityAuditTests(unittest.TestCase):
    def test_public_marker_scan_classifies_mp4_fixture_without_skipping_its_bytes(self) -> None:
        header = b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative = "frontend/tests/fixtures/media/clip.mp4"
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(header + b"\xff\xe8\x00\x00")
            findings: list[str] = []
            _audit_public_markers(root, (relative,), findings)
            self.assertEqual(findings, [])
            for marker, expected in (
                (b"C:" + b"/Users/fixture", "concrete private path marker found"),
                (b"api_key=" + b"fixture-only-value", "secret-shaped value found"),
                (b"?" + b"sig=fixture", "signed URL query marker found"),
            ):
                with self.subTest(expected=expected):
                    path.write_bytes(header + b"\xff" + marker + b"\x00")
                    findings = []
                    _audit_public_markers(root, (relative,), findings)
                    self.assertEqual(findings, [f"{relative}: {expected}"])

    def test_public_marker_scan_binary_classification_requires_fixture_path_suffix_and_header(
        self,
    ) -> None:
        header = b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
        rows = (
            ("frontend/tests/fixtures/media/clip.MP4", header, True),
            ("frontend/tests/fixtures/media/clip.bin", header, False),
            ("frontend/tests/fixtures_extra/clip.mp4", header, False),
            ("frontend/src/clip.mp4", header, False),
            ("frontend/tests/fixtures/media/clip.ts", header, False),
            ("frontend/tests/fixtures/media/clip.mp4", b"not-media", False),
            ("frontend/tests/fixtures/media/clip.mp4", header[:8], False),
            ("frontend/tests/fixtures/media/clip.mp4", b"\x00\x00\x00\x00" + header[4:], False),
            ("frontend/tests/fixtures/media/clip.mp4", b"\x00\x00\x00\x01" + header[4:], False),
            ("frontend/tests/fixtures/media/clip.mp4", b"\x00\x00\x00\x11" + header[4:], False),
            ("frontend/tests/fixtures/media/clip.mp4", b"\x00\x00\x01\x00" + header[4:], False),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative, prefix, admitted in rows:
                with self.subTest(relative=relative, prefix=prefix):
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(prefix + b"\xff\xe8\x00\x00")
                    findings: list[str] = []
                    _audit_public_markers(root, (relative,), findings)
                    if admitted:
                        self.assertEqual(findings, [])
                    else:
                        self.assertEqual(len(findings), 1)
                        self.assertIn("cannot inspect public text", findings[0])

    def test_public_marker_scan_keeps_text_and_frontend_negative_files_in_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in (
                "frontend/tests/negative.test.ts",
                "frontend/tests/fixtures/media/text.mp4",
                "frontend/src/config.ts",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("?" + "sig=fixture", encoding="utf-8")
                findings: list[str] = []
                _audit_public_markers(root, (relative,), findings)
                self.assertEqual(findings, [f"{relative}: signed URL query marker found"])

    def test_license_audit_requires_notice_path_without_binding_its_prose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
            findings: list[str] = []
            _audit_license(root, findings)
            self.assertEqual(findings, ["NOTICE: required attribution file is missing"])

            (root / "NOTICE").write_text("replaceable attribution fixture\n", encoding="utf-8")
            findings = []
            _audit_license(root, findings)
            self.assertEqual(findings, [])

    def test_current_repository_passes_the_offline_audit(self) -> None:
        self.assertEqual(audit_repository(ROOT), ())

    def test_perception_import_exceptions_are_exact_and_do_not_open_neighbors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            adapters = package / "adapters"
            adapters.mkdir(parents=True)
            for filename, module in (
                ("perception_host.py", "subprocess"),
                ("perception_process.py", "ctypes"),
                ("perception_worker.py", "http.client"),
            ):
                path = adapters / filename
                path.write_text(f"import {module}\nimport socket\n", encoding="utf-8")
            (adapters / "neighbor.py").write_text(
                "import subprocess\nimport ctypes\nimport http.client\n", encoding="utf-8"
            )
            findings = audit_runtime_package(package)
        self.assertEqual(len(findings), 6)
        self.assertTrue(all("socket" in item or "neighbor.py" in item for item in findings))

    def test_lazy_export_globals_exception_is_function_and_shape_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            core = package / "core"
            core.mkdir(parents=True)
            path = core / "__init__.py"
            path.write_text(
                "def __getattr__(name):\n    return globals().setdefault(name, value)\n"
                "def __dir__():\n    return sorted(set(globals()))\n",
                encoding="utf-8",
            )
            self.assertEqual(audit_runtime_package(package), ())
            path.write_text(
                "def __getattr__(name):\n    return globals().pop(name)\n"
                "def unrelated():\n    return globals()\n",
                encoding="utf-8",
            )
            self.assertEqual(len(audit_runtime_package(package)), 2)
            (core / "neighbor.py").write_text(
                "def __dir__():\n    return set(globals())\n", encoding="utf-8"
            )
            self.assertEqual(len(audit_runtime_package(package)), 3)

    def test_runtime_audit_rejects_external_network_and_dynamic_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            package.mkdir()
            (package / "bad.py").write_text(
                "import requests\neval('unexpected')\n", encoding="utf-8"
            )
            findings = audit_runtime_package(package)
        self.assertTrue(any("forbidden runtime import requests" in finding for finding in findings))
        self.assertTrue(any("forbidden runtime call eval" in finding for finding in findings))

    def test_network_resolution_is_allowed_only_in_the_one_reviewed_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            adapters = package / "adapters"
            adapters.mkdir(parents=True)
            body = "import socket\n\n\ndef go():\n    return socket.getaddrinfo('h', None)\n"
            (adapters / "prompt_model_transport.py").write_text(body, encoding="utf-8")
            self.assertEqual(audit_runtime_package(package), ())

            (adapters / "ollama_native.py").write_text(body, encoding="utf-8")
            findings = audit_runtime_package(package)
        self.assertEqual(
            findings,
            ("adapters/ollama_native.py:5: forbidden network call socket.getaddrinfo",),
        )

    def test_tls_is_gated_like_every_other_transport_import(self) -> None:
        # The distinct review of 2026-08-20 found the `ssl` allowlist entry was a no-op because
        # `ssl` had never been forbidden. An allowlist entry that permits something already
        # permitted reads as review that never happened.
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            adapters = package / "adapters"
            core = package / "core"
            adapters.mkdir(parents=True)
            core.mkdir(parents=True)
            (adapters / "prompt_model_transport.py").write_text("import ssl\n", encoding="utf-8")
            self.assertEqual(audit_runtime_package(package), ())

            (core / "elsewhere.py").write_text("import ssl\n", encoding="utf-8")
            findings = audit_runtime_package(package)
        self.assertIn("core/elsewhere.py:1: forbidden runtime import ssl", findings)

    def test_runtime_ctypes_exceptions_are_exact_to_reviewed_windows_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            adapters = package / "adapters"
            adapters.mkdir(parents=True)
            for relative in (
                "segment_artifact_store.py",
                "executable_admission.py",
                "media_runtime_discovery_worker.py",
                "media_subprocess.py",
                "authoring_render_executor.py",
                "authoring_render_probe.py",
                "authoring_render_process.py",
            ):
                (adapters / relative).write_text("import ctypes\n", encoding="utf-8")
            self.assertEqual(audit_runtime_package(package), ())

            # The executable pin left av_reconstruction_media, and the resolver was granted only
            # a process boundary: neither may import ctypes.
            for relative in ("av_reconstruction_media.py", "media_runtime_resolution.py"):
                (adapters / relative).write_text("import ctypes\n", encoding="utf-8")
            (adapters / "unreviewed.py").write_text("import ctypes\n", encoding="utf-8")
            findings = audit_runtime_package(package)
        for relative in ("av_reconstruction_media.py", "media_runtime_resolution.py"):
            self.assertIn(f"adapters/{relative}:1: forbidden runtime import ctypes", findings)
        self.assertTrue(
            any(
                finding == "adapters/unreviewed.py:1: forbidden runtime import ctypes"
                for finding in findings
            )
        )

    def test_media_runtime_egress_is_confined_to_the_download_transport(self) -> None:
        body = (
            "import http.client\nimport socket\nimport ssl\n\n\n"
            "def go():\n    return socket.create_connection(('a', 443))\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            adapters = package / "adapters"
            adapters.mkdir(parents=True)
            (adapters / "media_runtime_download.py").write_text(body, encoding="utf-8")
            self.assertEqual(audit_runtime_package(package), ())

            # The installer and the setup routes handle untrusted archive bytes and client
            # requests; neither may open a connection or resolve a name on its own.
            for relative in ("media_runtime_installer.py", "comfyui_media_runtime_setup.py"):
                (adapters / relative).write_text(body, encoding="utf-8")
            (adapters / "media_runtime_download.py").write_text(
                "import socket\n\n\ndef go():\n    return socket.getaddrinfo('a', None)\n",
                encoding="utf-8",
            )
            findings = audit_runtime_package(package)
        for relative in ("media_runtime_installer.py", "comfyui_media_runtime_setup.py"):
            for line, module in enumerate(("http.client", "socket", "ssl"), start=1):
                self.assertIn(
                    f"adapters/{relative}:{line}: forbidden runtime import {module}", findings
                )
            self.assertIn(
                f"adapters/{relative}:7: forbidden network call socket.create_connection", findings
            )
        self.assertIn(
            "adapters/media_runtime_download.py:5: forbidden network call socket.getaddrinfo",
            findings,
        )

    def test_native_render_process_exception_never_grants_neighbor_or_network_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            adapters = package / "adapters"
            core = package / "core"
            adapters.mkdir(parents=True)
            core.mkdir()
            process = adapters / "authoring_render_process.py"
            process.write_text("import ctypes\nimport subprocess\n", encoding="utf-8")
            self.assertEqual(audit_runtime_package(package), ())

            process.write_text(
                "import ctypes\nimport subprocess\nimport socket\n", encoding="utf-8"
            )
            for name in (
                "authoring_render_executor.py",
                "authoring_render_probe.py",
                "neighbor.py",
            ):
                (adapters / name).write_text("import subprocess\n", encoding="utf-8")
            (core / "native.py").write_text("import ctypes\nimport subprocess\n", encoding="utf-8")
            findings = audit_runtime_package(package)
        self.assertEqual(
            set(findings),
            {
                "adapters/authoring_render_process.py:3: forbidden runtime import socket",
                "adapters/authoring_render_executor.py:1: forbidden runtime import subprocess",
                "adapters/authoring_render_probe.py:1: forbidden runtime import subprocess",
                "adapters/neighbor.py:1: forbidden runtime import subprocess",
                "core/native.py:1: forbidden runtime import ctypes",
                "core/native.py:2: forbidden runtime import subprocess",
            },
        )

    def test_runtime_socket_exception_is_exact_to_reviewed_ollama_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            adapters = package / "adapters"
            adapters.mkdir(parents=True)
            (adapters / "ollama_native.py").write_text("import socket\n", encoding="utf-8")
            self.assertEqual(audit_runtime_package(package), ())

            (adapters / "unreviewed.py").write_text("import socket\n", encoding="utf-8")
            findings = audit_runtime_package(package)
        self.assertIn(
            "adapters/unreviewed.py:1: forbidden runtime import socket",
            findings,
        )

    def test_sequence_coordinator_is_the_exact_reviewed_dynamic_media_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "comfyui_h3_context"
            adapters = package / "adapters"
            adapters.mkdir(parents=True)
            body = "import importlib\n\ndef inspect():\n    return importlib.import_module('av')\n"
            (adapters / "comfyui_sequence_coordinator.py").write_text(body, encoding="utf-8")
            self.assertEqual(audit_runtime_package(package), ())

            (adapters / "unreviewed.py").write_text(body, encoding="utf-8")
            findings = audit_runtime_package(package)
        self.assertIn(
            "adapters/unreviewed.py:4: dynamic import outside explicit local adapter",
            findings,
        )

    def test_semantic_real_execution_has_one_public_module_entrypoint(self) -> None:
        surface = ollama_native.__dict__
        self.assertNotIn("_execute_semantic_ollama", surface)
        self.assertIn("execute_semantic_ollama", ollama_native.__all__)
        self.assertNotIn("_execute_semantic_ollama_test_only", ollama_native.__all__)
        for forbidden in (
            "_SEMANTIC_EXECUTION_FACTORY_TOKEN",
            "_SEMANTIC_REAL_EXECUTION_TOKEN",
            "_SEMANTIC_CHAT_LEASE_TOKEN",
        ):
            self.assertNotIn(forbidden, surface)

    def test_workflow_audit_rejects_unpinned_actions_write_permissions_and_extra_secrets(
        self,
    ) -> None:
        findings = audit_workflow_text(
            """
            permissions:
              contents: write
            jobs:
              publish:
                steps:
                  - uses: example/action@main
                    with:
                      token: ${{ secrets.OTHER_TOKEN }}
            """
        )
        self.assertTrue(any("write" in finding for finding in findings))
        self.assertTrue(any("not pinned" in finding for finding in findings))
        self.assertTrue(any("REGISTRY_ACCESS_TOKEN" in finding for finding in findings))

    def test_workflow_audit_rejects_composite_or_missing_direct_cli_token_binding(self) -> None:
        findings = audit_workflow_text(
            """
            if: github.repository == 'rookiestar28/ComfyUI-MiniMaxH3-Studio'
            permissions:
              contents: read
            steps:
              - uses: Comfy-Org/publish-node-action@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
                with:
                  personal_access_token: ${{ secrets.REGISTRY_ACCESS_TOKEN }}
            """
        )
        self.assertTrue(any("composite publication action" in finding for finding in findings))
        self.assertTrue(any("direct publication CLI" in finding for finding in findings))

    def test_workflow_audit_rejects_missing_or_renamed_registry_environment(self) -> None:
        template = """
        if: github.repository == 'rookiestar28/ComfyUI-MiniMaxH3-Studio'
        permissions:
          contents: read
        {environment}
        steps:
          - uses: example/action@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
          - env:
              REGISTRY_ACCESS_TOKEN: ${{{{ secrets.REGISTRY_ACCESS_TOKEN }}}}
            run: .publish-venv/bin/comfy node publish --token "$REGISTRY_ACCESS_TOKEN"
        """
        for environment in (
            "",
            "environment: another-environment",
            "environment: registry-production\n        environment: registry-production",
        ):
            with self.subTest(environment=environment):
                findings = audit_workflow_text(template.format(environment=environment))
                self.assertTrue(
                    any("registry-production" in finding for finding in findings),
                    findings,
                )


if __name__ == "__main__":
    unittest.main()
