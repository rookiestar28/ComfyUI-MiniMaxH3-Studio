"""Complete license/SBOM, build-input, and ownership inventory contracts."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import tomli

ROOT = Path(__file__).resolve().parents[1]
# IMPORTANT: move this pin only with the rebuilt bundle and supply-chain manifest; otherwise HEAD
# fails its own deterministic runtime-parity contract.
RUNTIME_BUNDLE_SHA256 = (
    "c2ff921e035a9f943862d1cf8ae7f36ff07950a052cd27c8d129590a58c416f6"  # pragma: allowlist secret
)
RUNTIME_BUNDLE_SIZE = 1883569


def supply_chain() -> ModuleType:
    return importlib.import_module("scripts.supply_chain_manifest")


class SupplyChainManifestTests(unittest.TestCase):
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

    def test_regular_file_reader_rejects_link_or_reparse_ancestor(self) -> None:
        module = supply_chain()
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="h3-supply-link-", dir=ROOT / ".tmp") as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            subject = target / "subject.json"
            subject.write_text("{}\n", encoding="utf-8")
            link = root / "link"
            self._make_directory_link(link, target)
            try:
                with (
                    patch.object(module, "ROOT", root),
                    self.assertRaisesRegex(module.SupplyChainError, "link|reparse"),
                ):
                    module._regular_file(link / subject.name, "probe")
            finally:
                link.unlink() if link.is_symlink() else link.rmdir()

    def test_frontend_license_inventory_covers_every_lock_package(self) -> None:
        module = supply_chain()
        locked = module.frontend_lock_packages(ROOT / "frontend" / "pnpm-lock.yaml")
        licenses = module.load_frontend_license_inventory()
        self.assertEqual(len(locked), 156)
        self.assertEqual(tuple((item["name"], item["version"]) for item in licenses), locked)
        self.assertTrue(all(item["license"] != "NOASSERTION" for item in licenses))
        self.assertEqual(
            {item["license"] for item in licenses},
            {
                "Apache-2.0",
                "BSD-2-Clause",
                "BSD-3-Clause",
                "BlueOak-1.0.0",
                "CC0-1.0",
                "ISC",
                "MIT",
                "MIT-0",
                "MPL-2.0",
            },
        )

    def test_spdx_inventory_is_complete_and_explicitly_owned(self) -> None:
        module = supply_chain()
        document = module.build_spdx_sbom()
        self.assertEqual(document["spdxVersion"], "SPDX-2.3")
        self.assertEqual(document["dataLicense"], "CC0-1.0")
        packages = document["packages"]
        project = next(package for package in packages if package["name"] == "minimax-h3-studio")
        with (ROOT / "pyproject.toml").open("rb") as stream:
            project_version = tomli.load(stream)["project"]["version"]
        self.assertEqual(project["versionInfo"], project_version)
        self.assertEqual(
            project["externalRefs"][0]["referenceLocator"],
            f"pkg:pypi/minimax-h3-studio@{project_version}",
        )
        self.assertTrue(document["documentNamespace"].endswith(f"/{project_version}"))
        self.assertEqual(project["licenseDeclared"], "Apache-2.0")
        self.assertEqual(project["licenseConcluded"], "Apache-2.0")
        host_frontend = next(
            package for package in packages if package["name"] == "comfyui-frontend-package"
        )
        self.assertEqual(host_frontend["primaryPackagePurpose"], "FRAMEWORK")
        self.assertEqual(
            host_frontend["comment"],
            "host_owned_qualification_observation_not_bundled",
        )
        frontend = [
            package
            for package in packages
            if any(
                reference.get("referenceType") == "purl"
                and reference.get("referenceLocator", "").startswith("pkg:npm/")
                for reference in package.get("externalRefs", [])
            )
        ]
        self.assertEqual(len(frontend), 156)
        self.assertTrue(all(package["licenseDeclared"] != "NOASSERTION" for package in packages))
        ownership = {package["comment"] for package in packages}
        self.assertTrue(
            {
                "bundled_project_runtime",
                "host_owned_qualification_observation_not_bundled",
                "host_owned_not_bundled",
                "external_user_managed_not_bundled",
                "development_build_only_not_bundled",
            }.issubset(ownership)
        )
        self.assertNotIn("reference/", json.dumps(document, ensure_ascii=False))
        self.assertNotIn("NOASSERTION license", json.dumps(document, ensure_ascii=False))

    def test_fresh_spdx_contains_the_verified_bundled_font_release(self) -> None:
        module = supply_chain()
        document = module.build_spdx_sbom()
        verified = module.load_packaged_font_manifest()
        manifest = json.loads(module.FONT_MANIFEST_PATH.read_text(encoding="utf-8"))
        asset = manifest["font_assets"][0]
        font = next(package for package in document["packages"] if package["name"] == "Noto Sans")

        self.assertEqual(font["versionInfo"], asset["release_id"])
        self.assertEqual(font["licenseDeclared"], verified.license_spdx_id)
        self.assertEqual(font["licenseConcluded"], "OFL-1.1")
        self.assertEqual(font["primaryPackagePurpose"], "LIBRARY")
        self.assertEqual(font["comment"], "bundled_font_runtime")
        self.assertEqual(
            font["externalRefs"][0]["referenceLocator"],
            f"pkg:github/notofonts/latin-greek-cyrillic@{asset['build_revision']}",
        )
        self.assertEqual(
            font["downloadLocation"],
            "https://github.com/notofonts/latin-greek-cyrillic",
        )

    def test_font_verification_failure_blocks_the_spdx_inventory(self) -> None:
        module = supply_chain()
        failure = module.AuthoringFontError(
            "font_artifact_hash_mismatch", "tampered packaged font fixture"
        )
        with (
            patch.object(module, "load_packaged_font_manifest", side_effect=failure),
            self.assertRaisesRegex(module.SupplyChainError, "font inventory verification failed"),
        ):
            module.build_spdx_sbom()

    def test_spdx_download_locations_match_each_package_ecosystem(self) -> None:
        document = supply_chain().build_spdx_sbom()
        for package in document["packages"]:
            purl = package["externalRefs"][0]["referenceLocator"]
            location = package["downloadLocation"]
            with self.subTest(purl=purl):
                if purl.startswith("pkg:npm/"):
                    self.assertTrue(location.startswith("https://registry.npmjs.org/"))
                elif purl.startswith("pkg:pypi/"):
                    self.assertTrue(location.startswith("https://pypi.org/project/"))
                elif purl.startswith("pkg:github/"):
                    self.assertTrue(location.startswith("https://github.com/"))
                else:
                    self.fail(f"unexpected package ecosystem: {purl}")

    def test_supply_chain_manifest_rebuild_matches_shipped_bytes(self) -> None:
        module = supply_chain()
        shipped = module.load_supply_chain_manifest()
        rebuilt = module.build_supply_chain_manifest()
        runtime_sha256 = "sha256:" + RUNTIME_BUNDLE_SHA256
        self.assertEqual(rebuilt, shipped)
        self.assertEqual(shipped["schema"], "h3-context-supply-chain/1")
        self.assertEqual(shipped["frontend_lock_package_count"], 156)
        self.assertEqual(
            shipped["runtime_entries"],
            [
                {
                    "path": "comfyui_h3_context/web/h3-context-sidebar.js",
                    "sha256": runtime_sha256,
                    "size": RUNTIME_BUNDLE_SIZE,
                }
            ],
        )
        paths = tuple(item["path"] for item in shipped["build_inputs"])
        self.assertEqual(paths, tuple(sorted(paths)))
        self.assertIn("frontend/pnpm-lock.yaml", paths)
        self.assertIn("frontend/src/entry.tsx", paths)
        self.assertIn("comfyui_h3_context/contracts/official_h3_assets_v2.json", paths)
        self.assertIn("comfyui_h3_context/contracts/host_seam_census_v1.json", paths)
        self.assertIn("comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json", paths)
        self.assertIn("LICENSE", paths)
        self.assertIn("NOTICE", paths)
        self.assertNotIn("frontend/tests", "\n".join(paths))
        self.assertNotIn("reference/", "\n".join(paths))

        schema = json.loads(
            (ROOT / "governance" / "contracts" / "supply_chain_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["$id"], "h3-context://contracts/supply_chain_v1.schema.json")
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["file"]["additionalProperties"])

    def test_supply_chain_decoder_rejects_duplicate_nonfinite_size_and_subclasses(self) -> None:
        module = supply_chain()
        payload = module.SUPPLY_CHAIN_PATH.read_bytes()
        text = payload.decode("utf-8")
        hostile = (
            b"\xff",
            text.replace('"schema":', '"schema":"duplicate","schema":', 1),
            text.replace('"version": 1', '"version": NaN', 1),
            b" " * (module.MAX_SUPPLY_CHAIN_WIRE_BYTES + 1),
            "[" * 40 + "0" + "]" * 40,
            None,
            memoryview(payload),
        )
        for value in hostile:
            with (
                self.subTest(kind=type(value).__name__),
                self.assertRaises(module.SupplyChainError),
            ):
                module.decode_supply_chain_json(value)
        for built_in in (str, bytes, bytearray):
            hostile_type = type(f"Hostile{built_in.__name__}", (built_in,), {})
            value = hostile_type(text if built_in is str else payload)
            with (
                self.subTest(subclass=built_in.__name__),
                self.assertRaises(module.SupplyChainError),
            ):
                module.decode_supply_chain_json(value)


if __name__ == "__main__":
    unittest.main()
