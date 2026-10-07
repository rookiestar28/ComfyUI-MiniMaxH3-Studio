"""M10-06 public producer and task-mode reachability matrix tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from comfyui_h3_context.core.capability_manifest import build_default_binding_manifest
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.reachability import (
    REACHABILITY_SCHEMA,
    ReachabilityDisposition,
    ReachabilityError,
    ReachabilitySource,
    ReachabilityStage,
    build_default_reachability_manifest,
    require_task_mode,
    validate_binding_coverage,
)
from comfyui_h3_context.nodes import H3ReferenceRegistryNode

ROOT = Path(__file__).resolve().parents[1]


def test_host_media_inputs_use_an_external_host_owned_reachability_source() -> None:
    manifest = build_default_reachability_manifest()
    for port in ("image", "video", "audio"):
        entry = manifest.for_binding(
            "comfyui_h3_context.H3Context.MediaAdmissionProducer",
            port,
        )
        assert entry.producer is not None
        assert entry.producer.source is ReachabilitySource.HOST_INPUT
        assert entry.producer.node_id == "comfyui.host.UpstreamMedia"
        assert entry.producer.socket_name == port


class ReachabilityTests(unittest.TestCase):
    def test_schema_and_wire_fingerprint_are_deterministic(self) -> None:
        schema = json.loads(
            (ROOT / "governance/contracts/reachability_v1.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["properties"]["schema"]["const"], REACHABILITY_SCHEMA)
        first = build_default_reachability_manifest()
        second = build_default_reachability_manifest()
        self.assertEqual(first.to_wire(), second.to_wire())
        self.assertEqual(json.loads(json.dumps(first.to_wire())), first.to_wire())
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_static_task_mode_fixture_matches_the_matrix(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/m10_06_task_mode_matrix.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["fixture_status"], "static_model_free_contract")
        manifest = build_default_reachability_manifest()
        for mode_name, stages in fixture["task_modes"].items():
            mode = TaskMode(mode_name)
            for stage_name, expected in stages.items():
                stage = ReachabilityStage(stage_name)
                entry = next(
                    item
                    for item in manifest.entries
                    if item.stage is stage
                    and item.task_mode is mode
                    and item.binding_node_id is None
                )
                self.assertEqual(entry.disposition.value, expected)
        self.assertEqual(
            fixture["negative_failures"]["i2va.native_generation"],
            "unsupported_native_task_mode",
        )
        registry = H3ReferenceRegistryNode().build_registry(
            first_frame=object(),
            last_frame=object(),
            images=[object()],
            videos=[object(), object()],
            paired_audios=[object()],
            audios=[object()],
        )[0]
        self.assertEqual(
            [asset.asset_id for asset in registry.assets], fixture["role_connection_order"]
        )

    def test_every_public_input_has_one_explicit_disposition(self) -> None:
        bindings = build_default_binding_manifest()
        manifest = build_default_reachability_manifest(bindings)
        validate_binding_coverage(bindings, manifest)
        self.assertEqual(
            manifest.for_binding(
                "comfyui_h3_context.H3Context.ReferenceRegistry", "first_frame"
            ).disposition,
            ReachabilityDisposition.REACHABLE,
        )
        self.assertEqual(
            manifest.for_binding(
                "comfyui_h3_context.H3Context.ReferenceRegistry", "paired_audios"
            ).disposition,
            ReachabilityDisposition.REACHABLE,
        )
        self.assertEqual(
            manifest.for_binding(
                "comfyui_h3_context.H3Context.Request", "hard_constraints"
            ).disposition,
            ReachabilityDisposition.REACHABLE,
        )
        self.assertEqual(
            manifest.for_binding(
                "comfyui_h3_context.H3Context.OfficialContextIR", "media"
            ).failure_code,
            "context_ir_media_producer_unavailable",
        )

    def test_stage_matrices_are_separate_for_all_closed_task_modes(self) -> None:
        manifest = build_default_reachability_manifest()
        for mode in TaskMode:
            with self.subTest(stage="compiler", mode=mode):
                route = require_task_mode(manifest, ReachabilityStage.COMPILER, mode)
                self.assertIs(route.disposition, ReachabilityDisposition.REACHABLE)
            with self.subTest(stage="context_ir", mode=mode):
                entry = next(
                    item
                    for item in manifest.entries
                    if item.stage is ReachabilityStage.CONTEXT_IR
                    and item.task_mode is mode
                    and item.binding_node_id is None
                )
                self.assertIs(entry.disposition, ReachabilityDisposition.INJECTED_ONLY)
                self.assertFalse(entry.normal_use)

        for mode in (TaskMode.T2VA, TaskMode.FL2VA, TaskMode.REF2VA):
            with self.subTest(stage="native_generation", mode=mode):
                route = require_task_mode(manifest, ReachabilityStage.NATIVE_GENERATION, mode)
                self.assertIs(route.disposition, ReachabilityDisposition.REACHABLE)
        for mode in (TaskMode.I2VA, TaskMode.L2VA):
            with self.subTest(stage="native_generation", mode=mode):
                with self.assertRaises(ReachabilityError) as context:
                    require_task_mode(manifest, ReachabilityStage.NATIVE_GENERATION, mode)
                self.assertEqual(str(context.exception), "unsupported_native_task_mode")

    def test_new_public_producers_are_reachable_or_explicitly_unavailable(self) -> None:
        manifest = build_default_reachability_manifest()
        graph = manifest.for_binding("comfyui_h3_context.H3Context.Plan", "intent_graph")
        self.assertIs(graph.disposition, ReachabilityDisposition.REACHABLE)
        self.assertTrue(graph.normal_use)
        self.assertIsNotNone(graph.producer)
        timeline = manifest.for_binding("comfyui_h3_context.H3Context.FullReference", "timeline")
        self.assertIs(timeline.disposition, ReachabilityDisposition.EXPLICIT_UNAVAILABLE)
        self.assertEqual(timeline.failure_code, "perception_profile_unavailable")
        self.assertFalse(timeline.normal_use)
        self.assertIsNotNone(timeline.producer)
        expected_producers = {
            "evidence_graph": "comfyui_h3_context.H3Context.EvidenceFusionProducer",
            "evidence_report": "comfyui_h3_context.H3Context.EvidenceFusionProducer",
            "cross_reference_graph": "comfyui_h3_context.H3Context.CrossReferenceProducer",
            "cross_reference_report": "comfyui_h3_context.H3Context.CrossReferenceProducer",
            "directive_authority": "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
            "directive_report": "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
            "intent_report": "comfyui_h3_context.H3Context.IntentGraphProducer",
        }
        for port_name, producer_id in expected_producers.items():
            with self.subTest(port_name=port_name):
                entry = manifest.for_binding(
                    "comfyui_h3_context.H3Context.FullReferenceTimelineProducer", port_name
                )
                self.assertIs(entry.disposition, ReachabilityDisposition.REACHABLE)
                self.assertIsNotNone(entry.producer)
                assert entry.producer is not None
                self.assertEqual(entry.producer.node_id, producer_id)

    def test_unknown_or_non_normal_entry_fails_closed(self) -> None:
        manifest = build_default_reachability_manifest()
        timeline = manifest.for_binding("comfyui_h3_context.H3Context.FullReference", "timeline")
        with self.assertRaises(ReachabilityError):
            manifest.require_normal(timeline.entry_id)
        with self.assertRaises(ReachabilityError):
            manifest.for_binding("comfyui_h3_context.H3Context.Missing", "input")


if __name__ == "__main__":
    unittest.main()
