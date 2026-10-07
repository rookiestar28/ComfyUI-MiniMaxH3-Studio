"""M10-08 offline runtime-foundation integration fixture tests."""

from __future__ import annotations

import json
import unittest
from typing import cast

from scripts.m10_08_runtime_fixture import run


class RuntimeFoundationFixtureTests(unittest.TestCase):
    def test_model_free_and_native_lanes_close_the_full_redacted_path(self) -> None:
        summary = run()
        self.assertEqual(summary["schema"], "h3.m10_08.runtime.fixture.v1")

        media = cast(dict[str, object], summary["media_admission"])
        self.assertEqual(media["statuses"], ["admitted", "admitted"])
        self.assertTrue(media["fingerprints_present"])

        for lane_name in ("model_free", "comfyui_native"):
            lane = cast(dict[str, object], summary[lane_name])
            self.assertEqual(lane["observation_status"], "complete")
            self.assertEqual(lane["coordinator_status"], "complete")
            self.assertTrue(lane["coordinator_value_present"])
            pipeline = cast(dict[str, object], lane["pipeline"])
            self.assertEqual(pipeline["node_plan"], "complete")
            self.assertEqual(pipeline["rendered"], "rendered")
            self.assertEqual(pipeline["validated"], "passed")
            self.assertEqual(pipeline["validated_report"], "passed")
            self.assertTrue(pipeline["ui_has_result"])
            self.assertEqual(pipeline["native_node_id"], "MiniMaxH3ImageToVideo")
            self.assertEqual(pipeline["native_validation"], "passed")
            self.assertEqual(pipeline["native_binding_count"], 2)
            self.assertTrue(pipeline["native_prompt_matches"])
            self.assertTrue(pipeline["stale_report_rejected"])

        native = cast(dict[str, object], summary["comfyui_native"])
        self.assertEqual(native["clip_calls"], ["tokenize", "generate", "decode"] * 2)

        terminal = cast(dict[str, object], summary["terminal_variants"])
        self.assertEqual(terminal["cancelled"], "cancelled")
        self.assertTrue(terminal["cancelled_value_absent"])
        self.assertEqual(terminal["failed"], "failed")
        self.assertTrue(terminal["failed_value_absent"])
        self.assertTrue(terminal["reservations_released"])

    def test_graph_and_privacy_facts_are_explicit(self) -> None:
        summary = run()
        graph = cast(dict[str, object], summary["graph"])
        self.assertEqual(graph["base_round_trip"], "workflow.m3_07.base")
        self.assertEqual(graph["reference_round_trip"], "workflow.m3_07.reference")
        self.assertEqual(graph["nested_round_trip"], "workflow.m3_07.base")
        self.assertEqual(graph["nested_node_count"], 6)
        self.assertTrue(graph["missing_anchor_rejected"])
        self.assertTrue(graph["ambiguous_anchor_rejected"])
        privacy = cast(dict[str, object], summary["privacy"])
        self.assertTrue(privacy["portable_outputs_are_redacted"])
        self.assertTrue(privacy["no_live_host_or_network"])

        wire = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("h3-m10-08-", wire)
        self.assertNotIn("frame_0.png", wire)
        self.assertNotIn("blue_door", wire)
        self.assertNotIn("A blue door is visible", wire)


if __name__ == "__main__":
    unittest.main()
