"""M3-02 H3 Context Request V1 node adapter tests."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    ExactTextConstraint,
    ExactTextKind,
    HardConstraintSet,
    NodeSocketType,
    TaskMode,
    validate_request_controls,
)
from comfyui_h3_context.nodes import (
    REQUEST_NODE_ID,
    REQUEST_SOCKET_TYPE,
    TASK_MODE_CHOICES,
    H3ContextRequestNode,
    RequestNodeError,
)

ROOT = Path(__file__).resolve().parents[1]
NODE_MODULE = ROOT / "comfyui_h3_context" / "nodes.py"


class RequestNodeTests(unittest.TestCase):
    def test_v1_metadata_matches_the_frozen_contract(self) -> None:
        metadata = H3ContextRequestNode.INPUT_TYPES()
        self.assertEqual(H3ContextRequestNode.NODE_ID, REQUEST_NODE_ID)
        self.assertEqual(H3ContextRequestNode.RETURN_TYPES, (REQUEST_SOCKET_TYPE,))
        self.assertEqual(H3ContextRequestNode.RETURN_NAMES, ("request",))
        self.assertEqual(H3ContextRequestNode.FUNCTION, "build_request")
        self.assertEqual(H3ContextRequestNode.CATEGORY, "h3_context/contracts")
        self.assertEqual(
            tuple(metadata["required"]),
            ("task_mode", "user_intent"),
        )
        self.assertEqual(
            tuple(metadata["optional"]),
            ("duration_seconds", "hard_constraints"),
        )
        task_mode_type, task_mode_options = metadata["required"]["task_mode"]
        self.assertEqual(task_mode_type, "COMBO")
        self.assertEqual(task_mode_options["default"], TaskMode.T2VA.value)
        self.assertEqual(task_mode_options["options"], list(TASK_MODE_CHOICES))
        self.assertTrue(metadata["required"]["user_intent"][1]["multiline"])
        self.assertEqual(
            metadata["optional"]["hard_constraints"][0],
            NodeSocketType.H3_HARD_CONSTRAINTS.value,
        )
        self.assertNotIn("default", metadata["optional"]["duration_seconds"][1])
        # M17-25 / AC-M17-25-03: duration is the only authored length. A frame
        # count is derived from it, never offered back as a control.
        self.assertNotIn("frame_count", metadata["optional"])

    def test_each_visible_control_changes_the_raw_request_envelope(self) -> None:
        node = H3ContextRequestNode()
        for mode in TaskMode:
            with self.subTest(mode=mode):
                request = node.build_request(mode.value, "a red kite crosses the sky")[0]
                self.assertEqual(request.mode, mode)
                self.assertEqual(request.user_intent, "a red kite crosses the sky")

        by_seconds = node.build_request("t2va", "duration test", duration_seconds=5.25)[0]
        self.assertEqual(by_seconds.duration_seconds, 5.25)
        self.assertFalse(hasattr(by_seconds, "frame_count"))

        exact_text = ExactTextConstraint("dialogue_1", ExactTextKind.DIALOGUE, "  保留?!\n")
        constraints = HardConstraintSet((exact_text,))
        with_constraint = node.build_request(
            "t2va", "constraint test", hard_constraints=constraints
        )[0]
        self.assertIs(with_constraint.hard_constraints, constraints)
        self.assertEqual(with_constraint.hard_constraints.exact_texts[0].text, "  保留?!\n")

    def test_reference_modes_are_carried_without_fabricating_assets(self) -> None:
        node = H3ContextRequestNode()
        for mode in (TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA, TaskMode.REF2VA):
            request = node.build_request(mode.value, "reference mode")[0]
            self.assertEqual(request.mode, mode)
            self.assertEqual(request.assets, ())
            self.assertEqual(request.reference_registry.assets, ())

    def test_invalid_controls_raise_actionable_diagnostics(self) -> None:
        node = H3ContextRequestNode()
        invalid_cases = (
            ("unsupported_task_mode", {"task_mode": "bad-mode", "user_intent": "x"}),
            ("missing_user_intent", {"task_mode": "t2va", "user_intent": "   "}),
            (
                "invalid_user_intent",
                {"task_mode": "t2va", "user_intent": "bad\x00text"},
            ),
            (
                "duration_out_of_bounds",
                {"task_mode": "t2va", "user_intent": "x", "duration_seconds": 0.0},
            ),
        )
        for code, values in invalid_cases:
            with self.subTest(code=code):
                with self.assertRaises(RequestNodeError) as context:
                    node.build_request(**values)
                self.assertIn(code, str(context.exception))
                self.assertTrue(any(item.code == code for item in context.exception.diagnostics))

        with self.assertRaises(RequestNodeError) as context:
            node.build_request(
                "t2va",
                "x",
                hard_constraints=cast(HardConstraintSet, object()),
            )
        self.assertIn("invalid_hard_constraints", str(context.exception))

    def test_warnings_are_visible_but_do_not_emit_a_fake_failure(self) -> None:
        node = H3ContextRequestNode()
        request = node.build_request("t2va", "round up", duration_seconds=5.0)[0]
        self.assertEqual(request.duration_seconds, 5.0)
        self.assertTrue(
            any(item.code == "duration_snapped" for item in validate_request_controls(request))
        )
        self.assertIs(node.VALIDATE_INPUTS("t2va", "round up", duration_seconds=5.0), True)

    def test_validation_hook_handles_comfyui_omitted_required_controls(self) -> None:
        results = (
            H3ContextRequestNode.VALIDATE_INPUTS(),
            H3ContextRequestNode.VALIDATE_INPUTS(task_mode="t2va"),
            H3ContextRequestNode.VALIDATE_INPUTS(user_intent="intent"),
        )
        for result in results:
            with self.subTest(result=result):
                self.assertIsInstance(result, str)
                if isinstance(result, str):
                    self.assertIn("H3 Context Request controls are invalid", result)

    def test_workflow_api_payload_json_round_trips_without_rewriting_inputs(self) -> None:
        payload: dict[str, object] = {
            "1": {
                "class_type": REQUEST_NODE_ID,
                "inputs": {
                    "task_mode": "ref2va",
                    "user_intent": "keep the camera movement",
                    "duration_seconds": 5.167,
                },
            }
        }
        round_tripped = cast(dict[str, object], json.loads(json.dumps(payload)))
        node_payload = cast(dict[str, object], round_tripped["1"])
        self.assertEqual(node_payload["class_type"], REQUEST_NODE_ID)
        inputs = cast(dict[str, object], node_payload["inputs"])
        request = H3ContextRequestNode().build_request(
            cast(str, inputs["task_mode"]),
            cast(str, inputs["user_intent"]),
            duration_seconds=cast(float, inputs["duration_seconds"]),
        )[0]
        self.assertEqual(request.mode, TaskMode.REF2VA)
        self.assertEqual(request.user_intent, "keep the camera movement")
        self.assertEqual(request.duration_seconds, 5.167)
        self.assertNotIn("frame_count", inputs)

    def test_raw_request_wire_projection_is_json_safe_and_unchanged(self) -> None:
        request = H3ContextRequestNode().build_request(
            "t2va", "  exact intent?!\n", duration_seconds=5.25
        )[0]
        wire = request.to_wire()
        self.assertEqual(json.loads(json.dumps(wire)), wire)
        self.assertEqual(wire["mode"], "t2va")
        self.assertEqual(wire["user_intent"], "  exact intent?!\n")
        self.assertEqual(wire["duration_seconds"], 5.25)
        self.assertNotIn("frame_count", wire)

    def test_node_module_has_no_host_or_optional_runtime_imports(self) -> None:
        tree = ast.parse(NODE_MODULE.read_text(encoding="utf-8"), filename=str(NODE_MODULE))
        forbidden = {
            "aiohttp",
            "comfy",
            "comfy_api",
            "cv2",
            "diffusers",
            "httpx",
            "moviepy",
            "numpy",
            "requests",
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


if __name__ == "__main__":
    unittest.main()
