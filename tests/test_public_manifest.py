"""M10-07 public-surface facade and manifest reconciliation tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path

import comfyui_h3_context.core.public_api as pure_public_api
from comfyui_h3_context import get_public_manifest
from comfyui_h3_context.core.errors import PublicManifestError
from comfyui_h3_context.core.public_manifest import (
    DEFAULT_WORKFLOW_FIXTURES,
    PUBLIC_MANIFEST_SCHEMA,
    NodeObjectInfo,
    PublicManifest,
    WorkflowFixtureRef,
    build_public_manifest,
)
from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from comfyui_h3_context.public_api import build_runtime_public_manifest, collect_node_object_info

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "governance" / "contracts" / "public_manifest_v1.schema.json"
PURE_FACADE = ROOT / "comfyui_h3_context" / "core" / "public_api.py"


class PublicManifestTests(unittest.TestCase):
    def test_schema_and_wire_are_versioned_json_safe(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema"]["const"], PUBLIC_MANIFEST_SCHEMA)
        manifest = build_public_manifest()
        wire = manifest.to_wire()
        self.assertEqual(wire["schema"], PUBLIC_MANIFEST_SCHEMA)
        self.assertEqual(json.loads(json.dumps(wire)), wire)
        self.assertEqual(manifest.fingerprint, manifest.fingerprint)
        self.assertEqual(
            manifest.fingerprint,
            build_public_manifest().fingerprint,
        )

    def test_runtime_facade_reconciles_all_registration_and_object_info_metadata(self) -> None:
        manifest = get_public_manifest()
        self.assertEqual(set(manifest.node_ids), set(NODE_CLASS_MAPPINGS))
        self.assertEqual(
            tuple(item.node_id for item in manifest.object_info),
            manifest.node_ids,
        )
        preview = next(item for item in manifest.object_info if item.node_id.endswith(".Preview"))
        self.assertEqual(preview.function, "emit")
        self.assertTrue(preview.output_node)
        self.assertEqual(
            tuple(item.node_id for item in manifest.object_info),
            tuple(item.node_id for item in manifest.contracts.definitions),
        )
        self.assertEqual(
            tuple(item[0] for item in manifest.bindings.display_names),
            manifest.node_ids,
        )
        self.assertEqual(len(manifest.workflow_fixtures), len(DEFAULT_WORKFLOW_FIXTURES))

    def test_pure_facade_has_no_optional_runtime_imports(self) -> None:
        tree = ast.parse(PURE_FACADE.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        )
        self.assertFalse(any(name.startswith(("comfy", "torch", "requests")) for name in imported))
        self.assertIn("build_public_manifest", pure_public_api.__all__)

    def test_runtime_object_info_is_derived_from_the_canonical_mapping(self) -> None:
        object_info = collect_node_object_info(NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS)
        self.assertEqual(len(object_info), len(NODE_CLASS_MAPPINGS))
        self.assertTrue(all(item.node_id in NODE_CLASS_MAPPINGS for item in object_info))

    def test_object_info_and_fixture_drift_fail_closed(self) -> None:
        manifest = build_public_manifest()
        broken_object_info = tuple(
            replace(item, category="h3_context/foreign")
            if item.node_id == manifest.node_ids[0]
            else item
            for item in manifest.object_info
        )
        with self.assertRaises(PublicManifestError):
            PublicManifest(
                contracts=manifest.contracts,
                bindings=manifest.bindings,
                reachability=manifest.reachability,
                object_info=broken_object_info,
                workflow_fixtures=manifest.workflow_fixtures,
            )

        broken_fixture = replace(
            manifest.workflow_fixtures[0],
            node_ids=("comfyui_h3_context.Unknown.Node",),
        )
        with self.assertRaises(PublicManifestError):
            PublicManifest(
                contracts=manifest.contracts,
                bindings=manifest.bindings,
                reachability=manifest.reachability,
                object_info=manifest.object_info,
                workflow_fixtures=(broken_fixture, *manifest.workflow_fixtures[1:]),
            )

    def test_runtime_registration_drift_fails_with_typed_error(self) -> None:
        incomplete = dict(NODE_CLASS_MAPPINGS)
        incomplete.pop(next(iter(incomplete)))
        with self.assertRaises(PublicManifestError):
            build_runtime_public_manifest(
                node_mapping=incomplete,
                display_names=NODE_DISPLAY_NAME_MAPPINGS,
            )

    def test_fixture_index_is_explicit_relative_and_mode_bound(self) -> None:
        self.assertEqual(
            {item.kind for item in DEFAULT_WORKFLOW_FIXTURES},
            {"workflow", "subgraph"},
        )
        self.assertTrue(all(not item.path.startswith("/") for item in DEFAULT_WORKFLOW_FIXTURES))
        self.assertTrue(all(item.task_modes for item in DEFAULT_WORKFLOW_FIXTURES))
        self.assertTrue(all(".." not in item.path.split("/") for item in DEFAULT_WORKFLOW_FIXTURES))

    def test_constructor_rejects_invalid_fixture_paths(self) -> None:
        with self.assertRaises(PublicManifestError):
            WorkflowFixtureRef(
                "fixture.invalid",
                "../private.json",
                "workflow",
                DEFAULT_WORKFLOW_FIXTURES[0].task_modes,
                DEFAULT_WORKFLOW_FIXTURES[0].node_ids,
            )

    def test_node_object_info_wire_round_trip_is_stable(self) -> None:
        manifest = get_public_manifest()
        item = manifest.object_info[0]
        self.assertEqual(json.loads(json.dumps(item.to_wire())), item.to_wire())
        self.assertIsInstance(item, NodeObjectInfo)
