"""M10-08 manifest/visible-graph binding and explicit anchor tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from comfyui_h3_context.core import (
    GRAPH_BINDING_SCHEMA,
    GraphAnchor,
    GraphAnchorRole,
    GraphBindingError,
    GraphEdge,
    VisibleGraph,
    build_public_manifest,
    graph_to_manifest,
    manifest_to_graph,
)

ROOT = Path(__file__).resolve().parents[1]


class GraphBindingTests(unittest.TestCase):
    def test_schema_and_multiple_pipeline_round_trips_are_deterministic(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "graph_binding_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schema"]["const"], GRAPH_BINDING_SCHEMA)
        manifest = build_public_manifest()
        fixture_ids = (
            "workflow.m3_07.base",
            "workflow.m3_07.reference",
            "workflow.m6_07.full_reference",
        )
        graphs = tuple(manifest_to_graph(manifest, fixture_id) for fixture_id in fixture_ids)
        self.assertEqual(tuple(graph_to_manifest(graph, manifest) for graph in graphs), fixture_ids)
        self.assertEqual(
            graphs[0].fingerprint, manifest_to_graph(manifest, fixture_ids[0]).fingerprint
        )
        self.assertEqual(json.loads(json.dumps(graphs[1].to_wire())), graphs[1].to_wire())

    def test_explicit_anchor_roles_fail_closed_for_missing_duplicate_and_ambiguous(self) -> None:
        manifest = build_public_manifest()
        empty = manifest_to_graph(manifest, "workflow.m3_07.reference")
        with self.assertRaisesRegex(GraphBindingError, "missing_anchor:reference"):
            graph_to_manifest(empty, manifest, required_anchor_roles=(GraphAnchorRole.REFERENCE,))

        registry_instance = "workflow.m3_07.reference.n1"
        duplicate = manifest_to_graph(
            manifest,
            "workflow.m3_07.reference",
            anchors=(
                GraphAnchor(GraphAnchorRole.REFERENCE, registry_instance, "images", 1),
                GraphAnchor(GraphAnchorRole.REFERENCE, registry_instance, "images", 2),
            ),
        )
        with self.assertRaisesRegex(GraphBindingError, "duplicate_anchor:reference"):
            graph_to_manifest(
                duplicate, manifest, required_anchor_roles=(GraphAnchorRole.REFERENCE,)
            )

        ambiguous = manifest_to_graph(
            manifest,
            "workflow.m3_07.reference",
            anchors=(
                GraphAnchor(GraphAnchorRole.REFERENCE, registry_instance, "images", 1),
                GraphAnchor(GraphAnchorRole.REFERENCE, "workflow.m3_07.reference.n2", "request", 2),
            ),
        )
        with self.assertRaisesRegex(GraphBindingError, "ambiguous_anchor:reference"):
            graph_to_manifest(
                ambiguous, manifest, required_anchor_roles=(GraphAnchorRole.REFERENCE,)
            )

    def test_transparent_nested_subgraphs_round_trip_without_hidden_nodes(self) -> None:
        manifest = build_public_manifest()
        fixture_id = "workflow.m3_07.base"
        fixture = next(item for item in manifest.workflow_fixtures if item.fixture_id == fixture_id)
        child_ids = fixture.node_ids[:2]
        root_ids = fixture.node_ids[2:]
        child = manifest_to_graph(
            manifest,
            fixture_id,
            node_ids=child_ids,
            instance_prefix="nested.child",
        )
        root = manifest_to_graph(
            manifest,
            fixture_id,
            node_ids=root_ids,
            instance_prefix="nested.root",
            children=(child,),
            transparent=True,
        )
        self.assertEqual(graph_to_manifest(root, manifest), fixture_id)
        self.assertEqual(len(root.flatten_nodes()), len(fixture.node_ids))
        with self.assertRaises(GraphBindingError):
            manifest_to_graph(manifest, fixture_id, node_ids=root_ids, children=(child,))

    def test_graph_rejects_node_drift_and_undeclared_edges(self) -> None:
        manifest = build_public_manifest()
        with self.assertRaises(GraphBindingError):
            manifest_to_graph(
                manifest,
                "workflow.m3_07.base",
                node_ids=("comfyui_h3_context.Unknown.Node",),
            )

        graph = manifest_to_graph(manifest, "workflow.m3_07.base")
        with self.assertRaises(GraphBindingError):
            VisibleGraph(
                fixture_id=graph.fixture_id,
                task_modes=graph.task_modes,
                nodes=graph.nodes,
                edges=graph.edges
                + (
                    # The source/target instance IDs exist, but these ports do not.
                    GraphEdge(
                        graph.nodes[0].node_instance_id,
                        "not_a_port",
                        graph.nodes[1].node_instance_id,
                        "request",
                    ),
                ),
            )
