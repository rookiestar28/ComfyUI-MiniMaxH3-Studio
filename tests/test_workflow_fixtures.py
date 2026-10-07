"""M3-07 canonical workflow and convenience-subgraph contract tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from comfyui_h3_context.core import (
    NATIVE_H3_HOST_REVISION,
    NATIVE_H3_SOURCE_BLOB,
    canonical_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / "workflows"
SUBGRAPH_ROOT = ROOT / "subgraphs"
WORKFLOW_FILES = {
    "base": WORKFLOW_ROOT / "m3_07_h3_context_base.json",
    "reference": WORKFLOW_ROOT / "m3_07_h3_context_reference.json",
    "full_reference": WORKFLOW_ROOT / "m6_07_h3_context_full_reference.json",
}
ASSISTANT_WORKFLOW_FILES = {
    "base": WORKFLOW_ROOT / "m15_09_assistant_base.json",
    "reference": WORKFLOW_ROOT / "m15_09_assistant_reference.json",
}
SUBGRAPH_FILES = {
    "base": SUBGRAPH_ROOT / "H3 Context Assistant - Base.json",
    "reference": SUBGRAPH_ROOT / "H3 Context Assistant - Reference.json",
}
ALLOWED_NODE_TYPES = {
    "LoadImage",
    "LoadVideo",
    "LoadAudio",
    "GetVideoComponents",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
    "comfyui_h3_context.H3Context.Compiler",
    "comfyui_h3_context.H3Context.NativeH3Adapter",
    "comfyui_h3_context.H3Context.ProductShell",
    "comfyui_h3_context.H3Context.Plan",
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    "comfyui_h3_context.H3Context.Request",
    "comfyui_h3_context.H3Context.Validator",
    "comfyui_h3_context.H3Context.Preview",
    "comfyui_h3_context.H3Context.AuditOverride",
    "comfyui_h3_context.H3Context.ProviderTransparency",
    "comfyui_h3_context.H3Context.Reliability",
}
PIPELINE_NODE_TYPES = (
    "comfyui_h3_context.H3Context.Request",
    "comfyui_h3_context.H3Context.Plan",
    "comfyui_h3_context.H3Context.Compiler",
    "comfyui_h3_context.H3Context.Validator",
    "comfyui_h3_context.H3Context.NativeH3Adapter",
)
STAGE_NODE_TYPES = PIPELINE_NODE_TYPES + (
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    "comfyui_h3_context.H3Context.Preview",
)
PRIVATE_MARKERS = (
    "/home/",
    "/mnt/",
    "c:\\",
    "../",
    "http://",
    "https://",
    "authorization",
    "bearer ",
    "api_key",
    "password",
    "secret",
    "sig=",
    "token=",
    "private_logic",
    "prompt_template",
    "media_bytes",
    "upload",
)


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"fixture root must be an object: {path}")
    return value


def _joined(parts: object) -> str:
    if not isinstance(parts, list) or not all(isinstance(item, str) for item in parts):
        raise AssertionError("split fingerprint must be a string list")
    return "".join(parts)


def _workflow_projection(workflow: dict[str, Any]) -> dict[str, Any]:
    prompt = workflow.get("prompt")
    if not isinstance(prompt, dict):
        raise AssertionError("workflow prompt must be an object")
    return {
        "node_types": [
            value.get("class_type")
            for _, value in sorted(prompt.items(), key=lambda item: int(item[0]))
        ],
        "output_projection": workflow["expected"]["output_projection"],
        "direct_media_links": workflow["expected"]["direct_media_links"],
    }


class WorkflowFixtureTests(unittest.TestCase):
    def test_m15_03_product_shell_migration_is_append_only_and_reversible(self) -> None:
        for mode in ("base", "reference"):
            with self.subTest(mode=mode):
                fixture = _load(WORKFLOW_ROOT / f"m15_03_product_shell_{mode}.json")
                prompt = fixture["prompt"]
                expected = fixture["expected"]
                shell_id = expected["output_projection"]["terminal_node"]
                native_id = expected["output_projection"]["native_node"]
                shell = prompt[shell_id]
                self.assertEqual(shell["class_type"], "comfyui_h3_context.H3Context.ProductShell")
                self.assertEqual(expected["output_projection"]["prompt"], [shell_id, 0])
                self.assertEqual(prompt[native_id]["inputs"]["prompt"], [shell_id, 0])
                self.assertEqual(
                    shell["inputs"],
                    {
                        "report": expected["output_projection"]["report"],
                        "native_h3_wiring": [
                            expected["output_projection"]["native_report"][0],
                            1,
                        ],
                    },
                )
                self.assertEqual(expected["migration_source"], expected["rollback_fixture"])
                self.assertEqual(
                    _joined(expected["graph_fingerprint_parts"]),
                    canonical_fingerprint(prompt),
                )
                if mode == "reference":
                    self.assertEqual(prompt[native_id]["inputs"]["ref_image_size"], "match")
                    self.assertTrue(expected["dynamic_reference_parent_visible"])
                    self.assertEqual(prompt[native_id]["class_type"], "MiniMaxH3ReferenceToVideo")
                    self.assertEqual(
                        expected["direct_media_links"],
                        [
                            {
                                "source": "2",
                                "source_output": 0,
                                "target": native_id,
                                "target_input": "ref_images",
                                "target_path": "ref_images.ref_image_0",
                                "asset_id": "image_1",
                                "presentation_label": "<Picture 1>",
                                "presentation_ordinal": 1,
                            },
                            {
                                "source": "3",
                                "source_output": 0,
                                "target": native_id,
                                "target_input": "ref_images",
                                "target_path": "ref_images.ref_image_1",
                                "asset_id": "image_2",
                                "presentation_label": "<Picture 2>",
                                "presentation_ordinal": 2,
                            },
                        ],
                    )

    def test_product_shell_subgraph_is_one_transparent_node_only(self) -> None:
        fixture = _load(SUBGRAPH_ROOT / "H3 Product Shell Boundary.json")
        definition = fixture["definitions"]["subgraphs"][0]
        self.assertEqual(
            [node["type"] for node in definition["nodes"]],
            ["comfyui_h3_context.H3Context.ProductShell"],
        )
        self.assertEqual(
            [item["type"] for item in definition["inputs"]],
            ["H3_CONTEXT_REPORT", "H3_NATIVE_H3_WIRING"],
        )
        self.assertEqual(
            [item["type"] for item in definition["outputs"]],
            ["STRING", "H3_PRODUCT_SHELL"],
        )
        self.assertEqual(definition["h3_context"]["dynamic_reference_boundary"], "parent_visible")
        serialized = json.dumps(fixture, sort_keys=True).casefold()
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, serialized, marker)

    def test_m15_02_scenario_workflows_are_public_input_only(self) -> None:
        scenarios = ("minimal", "complex", "ambiguous", "adversarial")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                path = WORKFLOW_ROOT / f"m15_02_downstream_{scenario}.json"
                self.assertTrue(path.is_file(), path)
                fixture = _load(path)
                self.assertEqual(fixture["schema"], "h3-context-workflow-fixture/1")
                self.assertEqual(fixture["scenario"], scenario)
                self.assertFalse(fixture["expected"]["prebuilt_python_results"])
                prompt = fixture["prompt"]
                self.assertTrue(prompt)
                node_types = {value["class_type"] for value in prompt.values()}
                self.assertTrue(
                    {
                        "comfyui_h3_context.H3Context.Request",
                        "comfyui_h3_context.H3Context.ReferenceRegistry",
                        "comfyui_h3_context.H3Context.MediaAdmissionProducer",
                        "comfyui_h3_context.H3Context.HardConstraintProducer",
                        "comfyui_h3_context.H3Context.IntentGraphProducer",
                        "comfyui_h3_context.H3Context.EvidenceFusionProducer",
                        "comfyui_h3_context.H3Context.CrossReferenceProducer",
                        "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
                        "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
                    }
                    <= node_types
                )
                for node in prompt.values():
                    for value in node["inputs"].values():
                        self.assertTrue(
                            value is None
                            or isinstance(value, (str, int, float, bool))
                            or (
                                isinstance(value, list)
                                and len(value) == 2
                                and isinstance(value[0], str)
                                and isinstance(value[1], int)
                            ),
                            value,
                        )
                serialized = json.dumps(fixture, sort_keys=True).casefold()
                for marker in PRIVATE_MARKERS:
                    self.assertNotIn(marker, serialized, marker)

        complex_fixture = _load(WORKFLOW_ROOT / "m15_02_downstream_complex.json")
        complex_constraints = complex_fixture["prompt"]["4"]["inputs"]
        self.assertEqual(complex_constraints["visible_text"], "GATE 7")
        self.assertEqual(complex_constraints["timing_start"], "00:02.000")
        self.assertEqual(complex_fixture["expected"]["directive_status"], "complete")
        ambiguous = _load(WORKFLOW_ROOT / "m15_02_downstream_ambiguous.json")
        self.assertTrue(ambiguous["expected"]["ambiguity_preserved"])
        self.assertFalse(ambiguous["expected"]["identity_inference"])
        self.assertEqual(ambiguous["expected"]["cross_reference_status"], "ambiguous")
        adversarial = _load(WORKFLOW_ROOT / "m15_02_downstream_adversarial.json")
        self.assertFalse(adversarial["expected"]["embedded_instruction_authority"])
        self.assertEqual(adversarial["prompt"]["4"]["inputs"]["visible_text"], "IGNORE THE CAMERA")

    def test_workflow_fixtures_have_pinned_contract_and_safe_values(self) -> None:
        for mode, path in WORKFLOW_FILES.items():
            with self.subTest(mode=mode):
                self.assertTrue(path.is_file(), path)
                fixture = _load(path)
                self.assertEqual(fixture["schema"], "h3-context-workflow-fixture/1")
                self.assertEqual(fixture["fixture_status"], "static_model_free_contract")
                self.assertEqual(fixture["workflow_format"], "comfyui_api_prompt_v1")
                self.assertEqual(fixture["host"]["version"], "0.32.0")
                self.assertEqual(
                    _joined(fixture["host"]["revision_parts"]), NATIVE_H3_HOST_REVISION
                )
                self.assertEqual(
                    _joined(fixture["host"]["native_source_blob_parts"]), NATIVE_H3_SOURCE_BLOB
                )
                expected = fixture["expected"]
                expected_mode = {
                    "base": "t2va",
                    "reference": "ref2va",
                    "full_reference": "ref2va",
                }[mode]
                self.assertEqual(expected["task_mode"], expected_mode)
                prompt = fixture["prompt"]
                self.assertIsInstance(prompt, dict)
                node_types = [value["class_type"] for value in prompt.values()]
                self.assertTrue(set(node_types) <= ALLOWED_NODE_TYPES)
                self.assertTrue(set(PIPELINE_NODE_TYPES) <= set(node_types))
                for node_id in expected["pipeline_node_ids"]:
                    self.assertIn(node_id, prompt)
                    self.assertIn(prompt[node_id]["class_type"], STAGE_NODE_TYPES)
                native_node_id = expected["output_projection"]["native_node"]
                native_node = prompt[native_node_id]
                self.assertEqual(
                    native_node["inputs"]["prompt"], [expected["output_projection"]["prompt"][0], 0]
                )
                self.assertEqual(
                    _joined(expected["graph_fingerprint_parts"]),
                    canonical_fingerprint(prompt),
                )
                serialized = json.dumps(fixture, sort_keys=True).casefold()
                for marker in PRIVATE_MARKERS:
                    self.assertNotIn(marker, serialized, marker)

    def test_base_and_reference_workflows_preserve_direct_media_semantics(self) -> None:
        base = _load(WORKFLOW_FILES["base"])
        reference = _load(WORKFLOW_FILES["reference"])
        self.assertEqual(reference["prompt"]["9"]["inputs"]["ref_image_size"], "match")
        self.assertEqual(base["expected"]["direct_media_links"], [])
        self.assertEqual(
            reference["expected"]["direct_media_links"],
            [
                {
                    "source": "2",
                    "source_output": 0,
                    "target": "9",
                    "target_input": "ref_images",
                },
                {
                    "source": "3",
                    "source_output": 0,
                    "target": "9",
                    "target_input": "ref_images",
                },
            ],
        )
        self.assertEqual(base["expected"]["task_mode"], "t2va")
        self.assertEqual(reference["expected"]["task_mode"], "ref2va")
        self.assertEqual(
            base["expected"]["output_projection"]["native_prompt"],
            ["6", "prompt"],
        )
        self.assertEqual(
            reference["expected"]["output_projection"]["native_prompt"],
            ["9", "prompt"],
        )
        full_reference = _load(WORKFLOW_FILES["full_reference"])
        self.assertEqual(
            full_reference["expected"]["output_projection"]["native_prompt"],
            ["12", "prompt"],
        )
        self.assertEqual(
            full_reference["expected"]["direct_media_links"],
            [
                {"source": "2", "source_output": 0, "target": "12", "target_input": "ref_images"},
                {"source": "3", "source_output": 0, "target": "12", "target_input": "ref_images"},
                {"source": "6", "source_output": 0, "target": "12", "target_input": "ref_videos"},
                {"source": "5", "source_output": 0, "target": "12", "target_input": "ref_audios"},
            ],
        )

    def test_audit_override_workflow_pins_safe_revalidation_projection(self) -> None:
        fixture = _load(WORKFLOW_ROOT / "m7_03_h3_context_audit_override.json")
        self.assertEqual(fixture["fixture_id"], "m7-03-audit-override")
        self.assertEqual(fixture["host"]["version"], "0.32.0")
        self.assertEqual(_joined(fixture["host"]["revision_parts"]), NATIVE_H3_HOST_REVISION)
        self.assertEqual(
            _joined(fixture["host"]["native_source_blob_parts"]), NATIVE_H3_SOURCE_BLOB
        )
        prompt = fixture["prompt"]
        expected = fixture["expected"]
        self.assertTrue({"1", "2", "3", "4", "5", "6", "7", "8"}.issubset(prompt))
        self.assertTrue(set(value["class_type"] for value in prompt.values()) <= ALLOWED_NODE_TYPES)
        self.assertEqual(expected["task_mode"], "t2va")
        self.assertEqual(expected["output_projection"]["prompt"], ["7", 0])
        self.assertEqual(expected["output_projection"]["report"], ["5", 1])
        self.assertEqual(prompt["6"]["inputs"]["report"], ["5", 1])
        self.assertEqual(prompt["7"]["inputs"]["report"], ["5", 1])
        self.assertEqual(
            _joined(expected["graph_fingerprint_parts"]), canonical_fingerprint(prompt)
        )
        encoded = json.dumps(fixture, sort_keys=True).casefold()
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, encoded, marker)

    def test_provider_transparency_workflow_pins_blocked_remote_projection(self) -> None:
        fixture = _load(WORKFLOW_ROOT / "m7_04_h3_context_provider_transparency.json")
        self.assertEqual(fixture["fixture_id"], "m7-04-provider-transparency")
        self.assertEqual(fixture["host"]["version"], "0.32.0")
        self.assertEqual(_joined(fixture["host"]["revision_parts"]), NATIVE_H3_HOST_REVISION)
        self.assertEqual(
            _joined(fixture["host"]["native_source_blob_parts"]), NATIVE_H3_SOURCE_BLOB
        )
        prompt = fixture["prompt"]
        expected = fixture["expected"]
        self.assertEqual(prompt["4"]["inputs"]["provider"], "remote_custom")
        self.assertEqual(prompt["4"]["inputs"]["upload_consent"], False)
        self.assertEqual(expected["transparency_projection"]["execution_allowed"], False)
        self.assertEqual(prompt["6"]["inputs"]["report"], ["5", 1])
        self.assertEqual(prompt["7"]["inputs"]["report"], ["5", 1])
        self.assertTrue(set(value["class_type"] for value in prompt.values()) <= ALLOWED_NODE_TYPES)
        self.assertEqual(
            _joined(expected["graph_fingerprint_parts"]), canonical_fingerprint(prompt)
        )
        encoded = json.dumps(fixture, sort_keys=True).casefold()
        for marker in ("authorization", "bearer ", "api_key", "password", "secret", "sig="):
            self.assertNotIn(marker, encoded, marker)

    def test_reliability_workflow_pins_cancelled_incompatible_projection(self) -> None:
        fixture = _load(WORKFLOW_ROOT / "m7_05_h3_context_reliability.json")
        self.assertEqual(fixture["fixture_id"], "m7-05-reliability")
        self.assertEqual(fixture["host"]["version"], "0.32.0")
        self.assertEqual(_joined(fixture["host"]["revision_parts"]), NATIVE_H3_HOST_REVISION)
        prompt = fixture["prompt"]
        expected = fixture["expected"]
        self.assertEqual(prompt["4"]["inputs"]["cancel_requested"], True)
        self.assertEqual(prompt["4"]["inputs"]["checkpoint_status"], "incompatible")
        self.assertEqual(expected["reliability_projection"]["action"], "cancelled")
        self.assertEqual(prompt["6"]["inputs"]["report"], ["5", 1])
        self.assertEqual(prompt["7"]["inputs"]["report"], ["5", 1])
        self.assertTrue(set(value["class_type"] for value in prompt.values()) <= ALLOWED_NODE_TYPES)
        self.assertEqual(
            _joined(expected["graph_fingerprint_parts"]), canonical_fingerprint(prompt)
        )
        encoded = json.dumps(fixture, sort_keys=True).casefold()
        for marker in ("authorization", "bearer ", "api_key", "password", "secret", "sig="):
            self.assertNotIn(marker, encoded, marker)

    def test_subgraphs_are_transparent_expansions_of_canonical_fixtures(self) -> None:
        for mode, path in SUBGRAPH_FILES.items():
            with self.subTest(mode=mode):
                self.assertTrue(path.is_file(), path)
                subgraph = _load(path)
                self.assertEqual(subgraph["version"], 0.4)
                self.assertIn("definitions", subgraph)
                definitions = subgraph["definitions"]["subgraphs"]
                self.assertIsInstance(definitions, list)
                self.assertEqual(len(definitions), 1)
                definition = definitions[0]
                self.assertEqual(subgraph["nodes"][0]["type"], definition["id"])
                self.assertEqual(definition["name"], subgraph["h3_context_fixture"]["name"])
                fixture = _load(WORKFLOW_FILES[mode])
                assistant_fixture = _load(ASSISTANT_WORKFLOW_FILES[mode])
                metadata = subgraph["h3_context_fixture"]
                if mode == "reference" and "canonical_projections" in metadata:
                    minimal = metadata["canonical_projections"]["minimal"]
                    self.assertEqual(
                        minimal["fixture"], WORKFLOW_FILES[mode].relative_to(ROOT).as_posix()
                    )
                    self.assertEqual(
                        _joined(minimal["graph_fingerprint_parts"]),
                        _joined(fixture["expected"]["graph_fingerprint_parts"]),
                    )
                    self.assertEqual(
                        minimal["output_projection"], fixture["expected"]["output_projection"]
                    )
                    self.assertEqual(
                        minimal["direct_media_links"], fixture["expected"]["direct_media_links"]
                    )
                    complex_fixture = _load(WORKFLOW_FILES["full_reference"])
                    complex_projection = metadata["canonical_projections"]["complex"]
                    self.assertEqual(
                        complex_projection["fixture"],
                        WORKFLOW_FILES["full_reference"].relative_to(ROOT).as_posix(),
                    )
                    self.assertEqual(
                        complex_projection["output_projection"],
                        complex_fixture["expected"]["output_projection"],
                    )
                    self.assertEqual(
                        complex_projection["direct_media_links"],
                        complex_fixture["expected"]["direct_media_links"],
                    )
                    self.assertEqual(
                        metadata["expanded_fixture"],
                        ASSISTANT_WORKFLOW_FILES[mode].relative_to(ROOT).as_posix(),
                    )
                    self.assertEqual(
                        metadata["output_projection"],
                        assistant_fixture["expected"]["output_projection"],
                    )
                    self.assertEqual(
                        _joined(metadata["graph_fingerprint_parts"]),
                        _joined(assistant_fixture["expected"]["graph_fingerprint_parts"]),
                    )
                else:
                    self.assertEqual(
                        metadata["expanded_fixture"],
                        ASSISTANT_WORKFLOW_FILES[mode].relative_to(ROOT).as_posix(),
                    )
                    self.assertEqual(
                        _joined(metadata["graph_fingerprint_parts"]),
                        _joined(assistant_fixture["expected"]["graph_fingerprint_parts"]),
                    )
                    self.assertEqual(
                        metadata["output_projection"],
                        assistant_fixture["expected"]["output_projection"],
                    )
                    self.assertEqual(
                        metadata["direct_media_links"], fixture["expected"]["direct_media_links"]
                    )
                internal_types = [node["type"] for node in definition["nodes"]]
                self.assertEqual(internal_types, metadata["expanded_node_types"])
                self.assertTrue(set(internal_types) <= ALLOWED_NODE_TYPES)
                self.assertTrue(set(PIPELINE_NODE_TYPES) <= set(internal_types))
                serialized = json.dumps(subgraph, sort_keys=True).casefold()
                for marker in PRIVATE_MARKERS:
                    self.assertNotIn(marker, serialized, marker)

    def test_subgraph_internal_nodes_do_not_duplicate_prompt_logic(self) -> None:
        forbidden_keys = {"prompt_template", "private_logic", "media_bytes", "provider", "upload"}
        for path in SUBGRAPH_FILES.values():
            with self.subTest(path=path.name):
                fixture = _load(path)
                definition = fixture["definitions"]["subgraphs"][0]
                keys = {
                    key for node in definition["nodes"] for key in node if key in forbidden_keys
                }
                self.assertEqual(keys, set())
                serialized = json.dumps(definition, sort_keys=True).casefold()
                for marker in ("prompt_template", "private_logic", "media_bytes", "provider"):
                    self.assertNotIn(marker, serialized, marker)


if __name__ == "__main__":
    unittest.main()
