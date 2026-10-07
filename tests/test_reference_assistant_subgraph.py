"""M7-02 Reference assistant Subgraph UX and migration contract tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from comfyui_h3_context.core import (
    REFERENCE_ASSISTANT_MIGRATION_SCHEMA,
    REFERENCE_ASSISTANT_UX_SCHEMA,
    ReferenceAssistantControls,
    ReferenceAssistantMigrationError,
    migrate_reference_assistant_widgets,
)

ROOT = Path(__file__).resolve().parents[1]
SUBGRAPH = ROOT / "subgraphs" / "H3 Context Assistant - Reference.json"
MIGRATION_FIXTURE = ROOT / "tests" / "fixtures" / "m7_02_reference_assistant_migration.json"


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"fixture root must be an object: {path}")
    return value


class ReferenceAssistantSubgraphTests(unittest.TestCase):
    def test_subgraph_exposes_request_and_ordered_media_boundaries(self) -> None:
        fixture = _load(SUBGRAPH)
        self.assertEqual(fixture["h3_context_fixture"]["ux_schema"], REFERENCE_ASSISTANT_UX_SCHEMA)
        expected_inputs = [
            "task_mode",
            "user_intent",
            "duration_seconds",
            "image_1",
            "image_2",
            "video_1",
            "audio_1",
        ]
        self.assertEqual([item["name"] for item in fixture["nodes"][0]["inputs"]], expected_inputs)
        definition = fixture["definitions"]["subgraphs"][0]
        self.assertEqual([item["name"] for item in definition["inputs"]], expected_inputs)
        request = next(
            node
            for node in definition["nodes"]
            if node["type"] == "comfyui_h3_context.H3Context.Request"
        )
        self.assertEqual(
            [(item["name"], item["link"]) for item in request["inputs"][:3]],
            [("task_mode", 15), ("user_intent", 16), ("duration_seconds", 17)],
        )
        registry = next(
            node
            for node in definition["nodes"]
            if node["type"] == "comfyui_h3_context.H3Context.ReferenceRegistry"
        )
        self.assertEqual(
            [(item["name"], item["link"]) for item in registry["inputs"]],
            [
                ("images.image0", 18),
                ("images.image1", 20),
                ("videos.video0", 22),
                ("audios.audio0", 24),
            ],
        )
        self.assertEqual(
            fixture["h3_context_fixture"]["asset_projection"],
            [
                {
                    "asset_id": "image_1",
                    "role": "reference",
                    "connection_order": 1,
                    "label": "<Picture 1>",
                },
                {
                    "asset_id": "image_2",
                    "role": "reference",
                    "connection_order": 2,
                    "label": "<Picture 2>",
                },
                {
                    "asset_id": "video_1",
                    "role": "reference",
                    "connection_order": 3,
                    "label": "<Video 1>",
                },
                {
                    "asset_id": "audio_1",
                    "role": "audio_source",
                    "connection_order": 4,
                    "label": "<Audio 1>",
                },
            ],
        )
        shell_nodes = [
            node
            for node in definition["nodes"]
            if node["type"] == "comfyui_h3_context.H3Context.ProductShell"
        ]
        self.assertEqual(len(shell_nodes), 1)
        shell = shell_nodes[0]
        native = next(
            node for node in definition["nodes"] if node["type"] == "MiniMaxH3ReferenceToVideo"
        )
        shell_prompt_link = next(
            link["id"]
            for link in definition["links"]
            if link["origin_id"] == shell["id"]
            and link["origin_slot"] == 0
            and link["target_id"] == native["id"]
        )
        self.assertEqual(
            next(item["link"] for item in native["inputs"] if item["name"] == "prompt"),
            shell_prompt_link,
        )
        self.assertEqual(
            [item["name"] for item in shell["inputs"]],
            ["report", "native_h3_wiring"],
        )

    def test_disclosure_is_explicit_and_directives_are_not_inert_controls(self) -> None:
        metadata = _load(SUBGRAPH)["h3_context_fixture"]
        self.assertEqual(
            metadata["execution_disclosure"],
            {
                "execution_route": "manual",
                "privacy_mode": "local_only",
                "network": "disabled",
                "media_transfer": "disabled",
                "credentials": "none",
            },
        )
        self.assertEqual(
            metadata["directive_disclosure"],
            {
                "schema": "h3.reference.directives.v1",
                "supported_actions": ["copy_event", "retain", "adapt", "exclude"],
                "execution": "not_inferred_or_executed",
                "owner": "typed_m6_directive_or_full_reference_timeline",
            },
        )
        self.assertGreaterEqual(len(metadata["limitations"]), 2)

    def test_minimal_and_complex_projections_match_canonical_fixtures(self) -> None:
        metadata = _load(SUBGRAPH)["h3_context_fixture"]["canonical_projections"]
        for name, workflow_name in (
            ("minimal", "m3_07_h3_context_reference.json"),
            ("complex", "m6_07_h3_context_full_reference.json"),
        ):
            with self.subTest(name=name):
                expected = _load(ROOT / "workflows" / workflow_name)["expected"]
                projection = metadata[name]
                self.assertEqual(projection["fixture"], f"workflows/{workflow_name}")
                self.assertEqual(
                    projection["graph_fingerprint_parts"],
                    expected["graph_fingerprint_parts"],
                )
                self.assertEqual(projection["output_projection"], expected["output_projection"])
                self.assertEqual(projection["direct_media_links"], expected["direct_media_links"])

    def test_legacy_reference_migration_is_strict(self) -> None:
        fixture = _load(MIGRATION_FIXTURE)
        self.assertEqual(fixture["schema"], REFERENCE_ASSISTANT_MIGRATION_SCHEMA)
        controls = migrate_reference_assistant_widgets(fixture["legacy"])
        self.assertIsInstance(controls, ReferenceAssistantControls)
        self.assertEqual(controls.to_wire(), fixture["expected"]["controls"])
        legacy = dict(fixture["legacy"])
        for mutation in (
            {**legacy, "version": 99},
            {**legacy, "widgets_values": ["t2va", "safe", 124]},
            {**legacy, "widgets_values": ["ref2va", "", 124]},
            {**legacy, "unexpected": True},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(ReferenceAssistantMigrationError):
                    migrate_reference_assistant_widgets(mutation)

    def test_expanded_fixture_is_the_versioned_product_shell_composition(self) -> None:
        metadata = _load(SUBGRAPH)["h3_context_fixture"]
        workflow = _load(ROOT / "workflows" / "m15_09_assistant_reference.json")
        self.assertEqual(metadata["expanded_fixture"], "workflows/m15_09_assistant_reference.json")
        self.assertEqual(metadata["output_projection"], workflow["expected"]["output_projection"])
        self.assertEqual(
            metadata["graph_fingerprint_parts"],
            workflow["expected"]["graph_fingerprint_parts"],
        )


if __name__ == "__main__":
    unittest.main()
