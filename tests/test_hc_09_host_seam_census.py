from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.hc_09_host_seam_census import (
    HostSeamCensusError,
    scan_hermetic_host_double_uses,
    scan_host_seams,
    validate_tracked_census,
)

ROOT = Path(__file__).resolve().parents[1]
CENSUS = ROOT / "comfyui_h3_context" / "contracts" / "host_seam_census_v1.json"


class HostSeamSourceCensusTests(unittest.TestCase):
    def test_tracked_census_exactly_covers_detected_product_use_sites(self) -> None:
        summary = validate_tracked_census(ROOT, CENSUS)
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["seams"], 29)
        self.assertGreaterEqual(summary["source_paths"], 50)

    def test_scan_finds_host_members_without_executing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "frontend" / "src" / "host" / "sample.ts"
            path.parent.mkdir(parents=True)
            path.write_text(
                (
                    "api.fetchApi('/content-never-retained', {}); app.graphToPrompt(); "
                    "app.graph.getNodeById(1); app.graph.change(); "
                    "app.graph.setDirtyCanvas(true, true);"
                ),
                encoding="utf-8",
            )
            detected, unknown = scan_host_seams(root)
            self.assertEqual(
                detected["frontend.api.fetch_api"],
                {"frontend/src/host/sample.ts"},
            )
            self.assertEqual(
                detected["frontend.app.graph_to_prompt"],
                {"frontend/src/host/sample.ts"},
            )
            for seam_id in (
                "frontend.app.graph.change",
                "frontend.app.graph.get_node_by_id",
                "frontend.app.graph.set_dirty_canvas",
            ):
                self.assertEqual(detected[seam_id], {"frontend/src/host/sample.ts"})
            self.assertEqual(unknown, ())

    def test_unknown_host_member_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "frontend" / "src" / "entry.tsx"
            path.parent.mkdir(parents=True)
            path.write_text("app.unclassifiedHostMutation();", encoding="utf-8")
            _detected, unknown = scan_host_seams(root)
            self.assertEqual(
                unknown,
                ("frontend/src/entry.tsx:app.unclassifiedHostMutation",),
            )

    def test_stale_or_missing_declared_source_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "frontend" / "src" / "host" / "sample.ts"
            source.parent.mkdir(parents=True)
            source.write_text("api.fetchApi('/ignored', {});", encoding="utf-8")
            census = {
                "seams": [
                    {
                        "id": "frontend.api.fetch_api",
                        "source_paths": ["frontend/src/host/stale.ts"],
                    }
                ]
            }
            census_path = root / "census.json"
            census_path.write_text(json.dumps(census), encoding="utf-8")
            with self.assertRaises(HostSeamCensusError):
                validate_tracked_census(root, census_path, parse_contract=False)

    def test_hermetic_host_doubles_are_fixture_backed(self) -> None:
        self.assertEqual(scan_hermetic_host_double_uses(ROOT), ())

    def test_hand_authored_governed_test_host_shape_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test = root / "frontend" / "tests" / "sample.test.ts"
            test.parent.mkdir(parents=True)
            test.write_text(
                "const app = { graphToPrompt: () => ({}) };\n"
                "const api = { queuePrompt: () => ({}) };\n",
                encoding="utf-8",
            )
            self.assertEqual(
                scan_hermetic_host_double_uses(root),
                (
                    "frontend/tests/sample.test.ts:api.queuePrompt",
                    "frontend/tests/sample.test.ts:app.graphToPrompt",
                ),
            )

    def test_inline_host_constructor_shape_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test = root / "frontend" / "tests" / "sample.test.ts"
            test.parent.mkdir(parents=True)
            test.write_text(
                "createSidebarHost({ app: { graph: {} }, api: {} });\n"
                "createAppModeController({ graphToPrompt() {} }, {});\n",
                encoding="utf-8",
            )
            self.assertEqual(
                scan_hermetic_host_double_uses(root),
                (
                    "frontend/tests/sample.test.ts:createAppModeController.unbound",
                    "frontend/tests/sample.test.ts:createSidebarHost.unbound",
                ),
            )

    def test_hand_authored_backend_host_modules_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test = root / "tests" / "test_sample.py"
            test.parent.mkdir(parents=True)
            test.write_text(
                'server = ModuleType("server")\n'
                "server.PromptServer = object()\n"
                'nodes = ModuleType("nodes")\n'
                "nodes.NODE_CLASS_MAPPINGS = {}\n"
                'folders = ModuleType("folder_paths")\n'
                "folders.get_filename_list = lambda _kind: []\n",
                encoding="utf-8",
            )
            self.assertEqual(
                scan_hermetic_host_double_uses(root),
                (
                    "tests/test_sample.py:backend.member.NODE_CLASS_MAPPINGS",
                    "tests/test_sample.py:backend.member.PromptServer",
                    "tests/test_sample.py:backend.member.get_filename_list",
                    "tests/test_sample.py:backend.module.folder_paths",
                    "tests/test_sample.py:backend.module.nodes",
                    "tests/test_sample.py:backend.module.server",
                ),
            )


if __name__ == "__main__":
    unittest.main()
