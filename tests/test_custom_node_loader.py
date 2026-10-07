"""Regression tests for the ComfyUI directory-loader entrypoint."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import cast

import comfyui_h3_context as package

ROOT = Path(__file__).resolve().parents[1]


class CustomNodeLoaderTests(unittest.TestCase):
    def test_host_file_loader_executes_root_entrypoint_and_exports_mappings(self) -> None:
        entrypoint = ROOT / "__init__.py"
        self.assertTrue(entrypoint.is_file(), "custom_nodes directory entrypoint is missing")

        # Match reference/ComfyUI/nodes.py: ``module_path.replace(".", "_x_")`` for directories.
        module_name = str(entrypoint.parent).replace(".", "_x_")
        spec = importlib.util.spec_from_file_location(module_name, entrypoint)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertIsNotNone(spec.loader)
        assert spec.loader is not None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertIs(getattr(module, "NODE_CLASS_MAPPINGS", None), package.NODE_CLASS_MAPPINGS)
        self.assertIs(
            getattr(module, "NODE_DISPLAY_NAME_MAPPINGS", None),
            package.NODE_DISPLAY_NAME_MAPPINGS,
        )
        loaded_mappings = cast(dict[str, object], module.__dict__["NODE_CLASS_MAPPINGS"])
        self.assertEqual(set(loaded_mappings), set(package.NODE_CLASS_MAPPINGS))


if __name__ == "__main__":
    unittest.main()
