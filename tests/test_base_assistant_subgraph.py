"""M7-01 Base assistant Subgraph UX and migration contract tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from comfyui_h3_context.core import (
    BASE_ASSISTANT_UX_SCHEMA,
    BaseAssistantControls,
    BaseAssistantMigrationError,
    canonical_fingerprint,
    fingerprint_context_report,
    migrate_base_assistant_widgets,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextNativeH3AdapterNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)

ROOT = Path(__file__).resolve().parents[1]
SUBGRAPH = ROOT / "subgraphs" / "H3 Context Assistant - Base.json"
MIGRATION_FIXTURE = ROOT / "tests" / "fixtures" / "m7_01_base_assistant_migration.json"


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"fixture root must be an object: {path}")
    return value


def _joined(value: object) -> str:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AssertionError("fingerprint must be split into string parts")
    return "".join(value)


def _canonical_outputs(controls: BaseAssistantControls) -> tuple[str, str]:
    request = H3ContextRequestNode().build_request(
        controls.task_mode,
        controls.user_intent,
        duration_seconds=controls.duration_milliseconds / 1000,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    prompt, report, document = H3ContextCompilerNode().compile(plan)
    _, validated_report = H3ContextValidatorNode().validate(plan, document)
    adapted_prompt, _ = H3ContextNativeH3AdapterNode().adapt(validated_report)
    if adapted_prompt != prompt or document.text != prompt:
        raise AssertionError("adapter/compiler prompt projection diverged")
    return adapted_prompt, fingerprint_context_report(report)


class BaseAssistantSubgraphTests(unittest.TestCase):
    def test_subgraph_exposes_only_high_value_inputs_and_safe_disclosure(self) -> None:
        fixture = _load(SUBGRAPH)
        self.assertEqual(fixture["version"], 0.4)
        self.assertEqual(fixture["h3_context_fixture"]["ux_schema"], BASE_ASSISTANT_UX_SCHEMA)
        self.assertEqual(
            [item["name"] for item in fixture["nodes"][0]["inputs"]],
            ["task_mode", "user_intent", "duration_seconds"],
        )
        definition = fixture["definitions"]["subgraphs"][0]
        self.assertEqual(
            [item["name"] for item in definition["inputs"]],
            ["task_mode", "user_intent", "duration_seconds"],
        )
        request = next(
            node
            for node in definition["nodes"]
            if node["type"] == "comfyui_h3_context.H3Context.Request"
        )
        self.assertEqual(
            [(item["name"], item["link"]) for item in request["inputs"][:3]],
            [("task_mode", 10), ("user_intent", 11), ("duration_seconds", 12)],
        )
        self.assertEqual(
            fixture["h3_context_fixture"]["execution_disclosure"],
            {
                "execution_route": "manual",
                "privacy_mode": "local_only",
                "network": "disabled",
                "media_transfer": "disabled",
                "credentials": "none",
            },
        )
        definition = fixture["definitions"]["subgraphs"][0]
        shell_nodes = [
            node
            for node in definition["nodes"]
            if node["type"] == "comfyui_h3_context.H3Context.ProductShell"
        ]
        self.assertEqual(len(shell_nodes), 1)
        shell = shell_nodes[0]
        native = next(
            node for node in definition["nodes"] if node["type"] == "MiniMaxH3ImageToVideo"
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

    def test_novice_projection_matches_canonical_outputs_byte_for_byte(self) -> None:
        fixture = _load(MIGRATION_FIXTURE)
        self.assertEqual(fixture["schema"], "h3-context-base-assistant-migration/1")
        controls = migrate_base_assistant_widgets(fixture["legacy"])
        prompt, report_fingerprint = _canonical_outputs(controls)
        expected = fixture["expected"]
        self.assertEqual(controls.to_wire(), expected["controls"])
        self.assertEqual(
            canonical_fingerprint(prompt), _joined(expected["prompt_fingerprint_parts"])
        )
        self.assertEqual(report_fingerprint, _joined(expected["report_fingerprint_parts"]))
        metadata = _load(SUBGRAPH)["h3_context_fixture"]
        self.assertEqual(
            metadata["output_fingerprint_projection"],
            {
                "prompt": expected["prompt_fingerprint_parts"],
                "report": expected["report_fingerprint_parts"],
            },
        )

    def test_migration_fixture_rejects_unknown_or_malformed_values(self) -> None:
        fixture = _load(MIGRATION_FIXTURE)
        legacy = dict(fixture["legacy"])
        for mutation in (
            {**legacy, "version": 99},
            {**legacy, "widgets_values": ["t2va", "", 120]},
            {**legacy, "widgets_values": ["unknown", "safe intent", 120]},
            {**legacy, "widgets_values": ["t2va", "safe intent", 4]},
            {**legacy, "unexpected": True},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(BaseAssistantMigrationError):
                    migrate_base_assistant_widgets(mutation)

    def test_expansion_metadata_is_pinned_to_the_canonical_base_fixture(self) -> None:
        subgraph = _load(SUBGRAPH)
        metadata = subgraph["h3_context_fixture"]
        workflow = _load(ROOT / "workflows" / "m15_09_assistant_base.json")
        self.assertEqual(metadata["expanded_fixture"], "workflows/m15_09_assistant_base.json")
        self.assertEqual(
            metadata["graph_fingerprint_parts"], workflow["expected"]["graph_fingerprint_parts"]
        )
        self.assertEqual(metadata["output_projection"], workflow["expected"]["output_projection"])
        self.assertEqual(
            metadata["input_projection"],
            [
                {"external": "task_mode", "internal": ["1", "task_mode"], "link": 10},
                {"external": "user_intent", "internal": ["1", "user_intent"], "link": 11},
                {"external": "duration_seconds", "internal": ["1", "duration_seconds"], "link": 12},
            ],
        )


if __name__ == "__main__":
    unittest.main()
