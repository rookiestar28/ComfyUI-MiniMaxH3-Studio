"""M2-07 public pure-core API and executable-documentation tests."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import comfyui_h3_context.core as core

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "minimal_core_pipeline.py"


def run_example() -> subprocess.CompletedProcess[str]:
    script = textwrap.dedent(
        f"""
        import runpy
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        runpy.run_path({str(EXAMPLE)!r}, run_name="__main__")
        """
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


class PublicApiTests(unittest.TestCase):
    def test_gate_enforced_core_exports_exist(self) -> None:
        baseline = json.loads((ROOT / "tests" / "acceptance_baseline.json").read_text("utf-8"))
        required = {row["export"] for row in baseline["public_python_abi"]}
        self.assertEqual(len(required), 14)
        self.assertLessEqual(required, set(core.__all__))
        for name in sorted(required):
            self.assertTrue(hasattr(core, name), name)

    def test_example_executes_in_isolated_interpreter_and_is_deterministic(self) -> None:
        self.assertTrue(EXAMPLE.is_file())
        first = run_example()
        second = run_example()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertTrue(payload["lint_valid"])
        self.assertTrue(payload["parse_canonical"])
        self.assertRegex(payload["document_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("/mnt/", first.stdout)
        self.assertNotIn("credential", first.stdout.casefold())

    def test_example_has_no_optional_runtime_imports(self) -> None:
        module = ast.parse(EXAMPLE.read_text(encoding="utf-8"), filename=str(EXAMPLE))
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
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports)

    def test_example_does_not_contain_signed_or_private_resources(self) -> None:
        for path in (EXAMPLE,):
            text = path.read_text(encoding="utf-8").casefold()
            self.assertIsNone(re.search(r"https?://|authorization|bearer |api[_-]?key|sig=", text))


if __name__ == "__main__":
    unittest.main()
