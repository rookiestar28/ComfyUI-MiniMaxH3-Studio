from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.hc_09_host_seam_test_double as doubles

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "comfyui_h3_context" / "contracts" / "host_seam_shape_fixture_v1.json"


class BackendHostSeamTestDoubleTests(unittest.TestCase):
    def tearDown(self) -> None:
        doubles.reset_fixture_cache_for_test()

    def test_factories_create_only_the_recorded_backend_shapes(self) -> None:
        nodes = doubles.host_nodes_module("ExampleNode")
        self.assertEqual(set(nodes.NODE_CLASS_MAPPINGS), {"ExampleNode"})
        self.assertEqual(set(nodes.NODE_DISPLAY_NAME_MAPPINGS), {"ExampleNode"})

        folders = doubles.host_folder_paths_module(inventory={"vae": ["synthetic"]})
        self.assertEqual(folders.get_filename_list("vae"), ["synthetic"])

        routes = object()
        server = doubles.host_prompt_server_module(routes)
        self.assertIs(server.PromptServer.instance.routes, routes)
        unavailable = doubles.host_prompt_server_module(object(), available=False)
        self.assertFalse(hasattr(unavailable, "PromptServer"))

    def test_module_installation_restores_the_prior_process_state(self) -> None:
        prior = sys.modules.get("folder_paths")
        module = doubles.host_folder_paths_module(inventory={})
        with doubles.InstalledHostModules(folder_paths=module):
            self.assertIs(sys.modules["folder_paths"], module)
        if prior is None:
            self.assertNotIn("folder_paths", sys.modules)
        else:
            self.assertIs(sys.modules["folder_paths"], prior)

    def test_corrupted_tracked_shape_cannot_author_a_backend_double(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        corrupted = copy.deepcopy(fixture)
        row = next(
            value
            for value in corrupted["observations"]
            if value["seam_id"] == "backend.prompt_server.routes"
        )
        row["kind"] = "object"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps(corrupted), encoding="utf-8")
            with patch.object(doubles, "FIXTURE", path):
                doubles.reset_fixture_cache_for_test()
                with self.assertRaisesRegex(AssertionError, "fixture shape disagrees"):
                    doubles.host_prompt_server_module(object())


if __name__ == "__main__":
    unittest.main()
