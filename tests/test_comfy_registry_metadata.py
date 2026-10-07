"""M8-02 Comfy Registry metadata and public payload contract tests."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import tomli

from scripts.validate_comfy_registry_metadata import (
    EXPECTED_AUTHOR,
    EXPECTED_CLASSIFIERS,
    EXPECTED_DISPLAY_NAME,
    EXPECTED_ICON_URL,
    EXPECTED_INCLUDES,
    EXPECTED_NAME,
    EXPECTED_PUBLISHER,
    EXPECTED_REPOSITORY,
    PUBLIC_IDENTITY_PATHS,
    validate_identity_text,
    validate_metadata,
)

ROOT = Path(__file__).resolve().parents[1]


def _normalized_public_text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


class ComfyRegistryMetadataTests(unittest.TestCase):
    def test_finalized_metadata_contract_is_valid(self) -> None:
        validate_metadata(require_finalized=True)

    def test_registry_fields_are_explicit_and_dependency_bounded(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as stream:
            document = tomli.load(stream)
        project = document["project"]
        comfy = document["tool"]["comfy"]
        self.assertEqual(EXPECTED_AUTHOR, "rookiestar28")
        self.assertEqual(EXPECTED_PUBLISHER, "rookiestar")
        self.assertNotEqual(EXPECTED_AUTHOR, EXPECTED_PUBLISHER)
        self.assertEqual(EXPECTED_NAME, "minimax-h3-studio")
        self.assertNotEqual(EXPECTED_NAME, EXPECTED_DISPLAY_NAME)
        self.assertEqual(project["name"], EXPECTED_NAME)
        self.assertRegex(project["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(project["authors"], [{"name": EXPECTED_AUTHOR}])
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(project["license-files"], ["LICENSE", "NOTICE"])
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(project["classifiers"], EXPECTED_CLASSIFIERS)
        self.assertEqual(project["urls"]["Repository"], EXPECTED_REPOSITORY)
        self.assertEqual(comfy["PublisherId"], EXPECTED_PUBLISHER)
        self.assertEqual(EXPECTED_DISPLAY_NAME, "ComfyUI-MiniMaxH3-Studio")
        self.assertEqual(comfy["DisplayName"], EXPECTED_DISPLAY_NAME)
        self.assertEqual(comfy["Icon"], EXPECTED_ICON_URL)
        self.assertNotIn("requires-comfyui", comfy)
        self.assertEqual(frozenset(comfy["includes"]), EXPECTED_INCLUDES)
        self.assertIn("NOTICE", comfy["includes"])

    def test_identity_scan_uses_no_prose_documents(self) -> None:
        prose_paths = {
            path
            for path in PUBLIC_IDENTITY_PATHS
            if Path(path).suffix.casefold() == ".md" or Path(path).name == "NOTICE"
        }
        self.assertEqual(prose_paths, set())

    def test_current_public_brand_rejects_superseded_spellings(self) -> None:
        human_brand_paths = (
            ROOT / "pyproject.toml",
            ROOT / "comfyui_h3_context" / "__init__.py",
            ROOT / "governance" / "contracts" / "capability_manifest_v1.schema.json",
            ROOT / "governance" / "contracts" / "contracts_v2.schema.json",
            ROOT / "governance" / "contracts" / "h3_context_report_v1.schema.json",
            ROOT / "governance" / "contracts" / "h3_context_v1.schema.json",
            ROOT / "governance" / "contracts" / "h3_prompt_profile_v1.schema.json",
        )
        for path in human_brand_paths:
            text = path.read_text(encoding="utf-8")
            # Keep internal package/schema labels stable when the public Registry name changes.
            expected_label = (
                EXPECTED_DISPLAY_NAME
                if path.name == "pyproject.toml"
                else "ComfyUI-MiniMaxH3-Context"
            )
            self.assertIn(expected_label, text, path.as_posix())
            self.assertNotIn("ComfyUI-MinimaxH3-Context", text, path.as_posix())
            self.assertNotIn("ComfyUI-H3-Context", text, path.as_posix())
            self.assertNotIn("ComfyUI H3 Context", text, path.as_posix())
            self.assertNotIn("ComfyUI-Minimax-Context", text, path.as_posix())
            self.assertNotIn("ComfyUI-Minimax_Context", text, path.as_posix())

        schema_ids = {
            "capability_manifest_v1.schema.json": (
                "h3-context://contracts/capability_manifest_v1.schema.json"
            ),
            "contracts_v2.schema.json": "h3-context://contracts/contracts_v2.schema.json",
            "h3_context_report_v1.schema.json": (
                "comfyui-h3-context://contracts/h3_context_report_v1.schema.json"
            ),
            "h3_context_v1.schema.json": (
                "comfyui-h3-context://contracts/h3_context_v1.schema.json"
            ),
            "h3_prompt_profile_v1.schema.json": (
                "comfyui-h3-context://contracts/h3_prompt_profile_v1.schema.json"
            ),
        }
        for filename, expected_id in schema_ids.items():
            document = json.loads(
                (ROOT / "governance" / "contracts" / filename).read_text(encoding="utf-8")
            )
            self.assertEqual(document["$id"], expected_id)

    def test_legacy_brand_is_allowed_only_for_exact_stable_schema_ids(self) -> None:
        stable_id = (
            "https://rookiestar28.github.io/ComfyUI-MinimaxH3-Context/"
            "contracts/sidebar_action_v1.schema.json"
        )
        validate_identity_text(
            "governance/contracts/sidebar_action_v1.schema.json",
            f'{{"$id": "{stable_id}"}}',
        )
        with self.assertRaisesRegex(ValueError, "superseded public identity"):
            validate_identity_text("README.md", f"Current project: {stable_id}")

    def test_project_license_metadata_is_apache_2_0(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as stream:
            project = tomli.load(stream)["project"]

        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(project["license-files"], ["LICENSE", "NOTICE"])

    def test_comfyignore_excludes_private_and_development_trees(self) -> None:
        text = (ROOT / ".comfyignore").read_text(encoding="utf-8")
        for excluded in (
            ".planning/",
            "reference/",
            "tests/TEST_SOP.md",
            "tests/E2E_TESTING_SOP.md",
            ".github/",
            ".venv/",
            "*.egg-info/",
            "*.safetensors",
            "*.mp4",
        ):
            self.assertIn(excluded, text)
        for public in ("README.md", "LICENSE", "NOTICE", "comfyui_h3_context", "workflows"):
            self.assertNotRegex(text, rf"(?m)^\s*{re.escape(public)}/?\s*$")
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertRegex(gitignore, r"(?m)^node\.zip$")

    def test_public_development_declares_root_build_inputs(self) -> None:
        # The actual tarball regression owns suffix/privacy behavior. Here require the
        # upstream developer declaration to name the root policy and manifest explicitly.
        document = tomli.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        required = document["tool"]["h3-context"]["development"]["required"]
        self.assertIn(".gitignore", required)
        self.assertIn("MANIFEST.in", required)

    def test_publication_workflow_is_owner_guarded_and_secret_scoped(self) -> None:
        text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        self.assertIn("github.repository == 'rookiestar28/ComfyUI-MiniMaxH3-Studio'", text)
        self.assertIn("REGISTRY_ACCESS_TOKEN", text)
        self.assertIn("scripts/validate_comfy_registry_metadata.py", text)
        self.assertIn("--require-finalized --source-root . --public-projection", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("fetch-depth: 0", text)
        self.assertIn("scripts/registry_publish_guard.py", text)
        self.assertIn("needs.preflight.outputs.should_publish == 'true'", text)
        self.assertEqual(text.count("environment: registry-production"), 1)
        self.assertIn("requirements/registry-publish-py310-linux-x86_64.txt", text)
        self.assertNotIn("publication-capsule", text)
        self.assertIn('.publish-venv/bin/comfy node publish --token "$REGISTRY_ACCESS_TOKEN"', text)
        self.assertNotIn("Comfy-Org/publish-node-action", text)
        self.assertNotIn("skip_checkout", text)
        self.assertNotRegex(text, r"(?m)^\s*uses: [^@]+@(?:v|main|master)\b")
        for action in (
            "actions/checkout",
            "actions/setup-python",
        ):
            self.assertRegex(text, rf"uses: {re.escape(action)}@[0-9a-f]{{40}}")
        self.assertNotIn("permissions:\n  contents: write", text)


if __name__ == "__main__":
    unittest.main()
