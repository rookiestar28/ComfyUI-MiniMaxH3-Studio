"""Closed installation-profile and dependency-ownership contracts."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

import tomli

from comfyui_h3_context import __version__ as package_version
from comfyui_h3_context.core.installation_profiles import (
    MAX_INSTALLATION_PROFILE_WIRE_BYTES,
    InstallationProfileError,
    decode_installation_profiles_json,
    load_installation_profiles,
    qualify_installation_environment,
)

ROOT = Path(__file__).resolve().parents[1]
# The profile artifact stays in the package because the installed package opens it at run
# time; its schema has no runtime or bundle reader and moved with M23-55.
CONTRACT_ROOT = ROOT / "comfyui_h3_context" / "contracts"
GOVERNANCE_ROOT = ROOT / "governance" / "contracts"
EXPECTED_HOST_CAPABILITY = "comfyui-native-node-capabilities-v1"
EXPECTED_FRONTEND_CAPABILITY = "comfyui-frontend-extension-api-v1"


class InstallationProfileTests(unittest.TestCase):
    @staticmethod
    def _make_directory_link(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(
                [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=5,
            )
            if result.returncode != 0:
                raise OSError("could not create Windows junction fixture") from None

    def test_closed_profile_inventory_and_ownership(self) -> None:
        manifest = load_installation_profiles()
        self.assertEqual(manifest.schema, "h3-context-installation-profiles/1")
        self.assertEqual(manifest.version, 1)
        self.assertEqual(
            tuple(profile.profile_id for profile in manifest.profiles),
            (
                "core_manual",
                "comfyui_native",
                "external_ollama",
                "external_openai_compatible_loopback",
                "remote_openai_compatible_service",
                "remote_anthropic_service",
                "frontend_extension",
            ),
        )

        profiles = {profile.profile_id: profile for profile in manifest.profiles}
        self.assertEqual(profiles["core_manual"].owner, "package")
        self.assertEqual(
            profiles["core_manual"].distribution,
            f"minimax-h3-studio=={package_version}",
        )
        self.assertEqual(profiles["core_manual"].download_policy, "artifact_only")
        self.assertEqual(profiles["comfyui_native"].owner, "host")
        self.assertEqual(
            profiles["comfyui_native"].compatibility,
            (EXPECTED_HOST_CAPABILITY, EXPECTED_FRONTEND_CAPABILITY),
        )
        self.assertEqual(profiles["external_ollama"].owner, "user")
        self.assertEqual(profiles["external_ollama"].download_policy, "forbidden")
        self.assertEqual(profiles["external_ollama"].model_management, "external_only")
        self.assertEqual(profiles["frontend_extension"].owner, "package")
        self.assertEqual(
            profiles["frontend_extension"].runtime_entries,
            ("comfyui_h3_context/web/h3-context-sidebar.js",),
        )
        self.assertEqual(profiles["frontend_extension"].end_user_node_lifecycle, "forbidden")
        self.assertTrue(all(profile.fallback == "none" for profile in manifest.profiles))

    def test_manifest_is_canonical_and_matches_shipped_metadata(self) -> None:
        path = CONTRACT_ROOT / "installation_profiles_v1.json"
        payload = path.read_bytes()
        manifest = decode_installation_profiles_json(payload)
        self.assertEqual(manifest.to_wire_bytes(), payload)
        with (ROOT / "pyproject.toml").open("rb") as stream:
            pyproject = tomli.load(stream)
        self.assertEqual(pyproject["project"]["dependencies"], [])
        wire_text = payload.decode("utf-8")
        for forbidden in (
            "reference/",
            ".planning/",
            "node_modules",
            "safetensors",
            "ComfyUI-OpenClaw",
            "ComfyUI-Doctor",
        ):
            self.assertNotIn(forbidden, wire_text)

    def test_schema_is_closed_and_stably_identified(self) -> None:
        schema = json.loads(
            (GOVERNANCE_ROOT / "installation_profiles_v1.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["$id"],
            "h3-context://contracts/installation_profiles_v1.schema.json",
        )
        self.assertEqual(schema["title"], "ComfyUI-MiniMaxH3-Context Installation Profiles")
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["profile"]["additionalProperties"])

    def test_runtime_compatibility_validates_syntax_without_pinning_identity(self) -> None:
        supported = qualify_installation_environment(
            python_version="3.13.9",
            comfyui_version="0.32.0",
            frontend_version="1.48.7",
        )
        self.assertEqual(supported.status, "supported")
        self.assertEqual(supported.diagnostics, ())
        for python_version, comfyui_version, frontend_version in (
            ("3.10.0", "0.32.0", "1.48.7"),
            ("4.0.0", "0.33.0", "1.49.6"),
            ("3.13.9", "99.1.2", "20.0.0"),
        ):
            with self.subTest(comfyui_version=comfyui_version):
                result = qualify_installation_environment(
                    python_version=python_version,
                    comfyui_version=comfyui_version,
                    frontend_version=frontend_version,
                )
                self.assertEqual(result.status, "supported")
                self.assertEqual(result.diagnostics, ())
        hostile = (
            ("3.9.99", "0.32.0", "1.48.7", ("python_incompatible",)),
            ("3.13", "0.32.0", "1.48.7", ("invalid_python_version",)),
            ("3.13.9", "latest", "1.48.7", ("invalid_comfyui_version",)),
            ("3.13.9", "0.32.0", "latest", ("invalid_frontend_version",)),
            (
                "3.13.9",
                f"{'9' * 5_000}.0.0",
                "1.48.7",
                ("invalid_comfyui_version",),
            ),
        )
        for python, comfyui, frontend, diagnostics in hostile:
            with self.subTest(diagnostics=diagnostics):
                result = qualify_installation_environment(
                    python_version=python,
                    comfyui_version=comfyui,
                    frontend_version=frontend,
                )
                self.assertEqual(result.status, "incompatible")
                self.assertEqual(result.diagnostics, diagnostics)

    def test_strict_decoder_rejects_hostile_json_and_types(self) -> None:
        valid = (CONTRACT_ROOT / "installation_profiles_v1.json").read_bytes()
        text = valid.decode("utf-8")
        duplicate = text.replace(
            '"schema":"h3-context-installation-profiles/1"',
            '"schema":"h3-context-installation-profiles/1",'
            '"schema":"h3-context-installation-profiles/1"',
            1,
        )
        nested_duplicate = text.replace('"owner":"package"', '"owner":"package","owner":"user"', 1)
        hostile: tuple[object, ...] = (
            "",
            b"\xff",
            duplicate,
            nested_duplicate,
            text.replace('"version":1', '"version":NaN', 1),
            text.replace('"version":1', '"version":Infinity', 1),
            "[" * 40 + "0" + "]" * 40,
            b" " * (MAX_INSTALLATION_PROFILE_WIRE_BYTES + 1),
            1,
            None,
            memoryview(valid),
        )
        for payload in hostile:
            with (
                self.subTest(payload=type(payload).__name__),
                self.assertRaises(InstallationProfileError),
            ):
                decode_installation_profiles_json(payload)  # type: ignore[arg-type]

        for built_in in (str, bytes, bytearray):
            hostile_type = type(f"Hostile{built_in.__name__}", (built_in,), {})
            payload = hostile_type(text if built_in is str else valid)
            with (
                self.subTest(subclass=built_in.__name__),
                self.assertRaises(InstallationProfileError),
            ):
                decode_installation_profiles_json(payload)

    def test_exact_contract_rejects_unknown_or_mutated_members(self) -> None:
        wire = load_installation_profiles().to_wire()
        mutations = []
        unknown = dict(wire)
        unknown["unexpected"] = True
        mutations.append(unknown)
        wrong_schema = dict(wire)
        wrong_schema["schema"] = "h3-context-installation-profiles/2"
        mutations.append(wrong_schema)
        removed = dict(wire)
        removed.pop("profiles")
        mutations.append(removed)
        reordered = dict(wire)
        reordered["profiles"] = list(reversed(cast(list[object], reordered["profiles"])))
        mutations.append(reordered)
        for mutation in mutations:
            with self.subTest(keys=tuple(mutation)), self.assertRaises(InstallationProfileError):
                decode_installation_profiles_json(
                    json.dumps(mutation, ensure_ascii=False, separators=(",", ":"))
                )

    def test_manifest_loader_rejects_link_or_reparse_ancestor(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="h3-profile-link-", dir=ROOT / ".tmp") as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir()
            manifest = target / "installation_profiles_v1.json"
            manifest.write_bytes((CONTRACT_ROOT / "installation_profiles_v1.json").read_bytes())
            link = base / "link"
            self._make_directory_link(link, target)
            try:
                with self.assertRaisesRegex(InstallationProfileError, "link|reparse"):
                    load_installation_profiles(link / manifest.name)
            finally:
                link.unlink() if link.is_symlink() else link.rmdir()


if __name__ == "__main__":
    unittest.main()
