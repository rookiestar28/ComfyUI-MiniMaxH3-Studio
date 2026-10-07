"""M0-04 package and pure-core boundary tests.

These tests deliberately use only the Python standard library. M0-05 owns the later quality
toolchain; the package boundary must be verifiable before that tooling exists.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "comfyui_h3_context"
CORE_ROOT = PACKAGE_ROOT / "core"
GOVERNANCE_MODULES = (
    "architecture_inventory",
    "closeout_matrix",
    "cross_language_surface",
    "m19_closeout",
    "node_surface",
    "public_surface",
    "retirement_eligibility",
)


def run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    """Run a bounded isolated interpreter with only this repository on ``sys.path``."""

    return subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(script)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def import_blocker_script(body: str) -> str:
    root = str(ROOT).replace("\\", "\\\\").replace('"', '\\"')
    prefix = textwrap.dedent(
        f"""
        import importlib.abc
        import sys
        sys.path.insert(0, \"{root}\")

        blocked_roots = {{
            \"comfy_api\", \"comfy\", \"diffusers\", \"torch\", \"transformers\",
            \"httpx\", \"requests\", \"aiohttp\",
        }}

        class BlockedOptionalFinder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(
                    fullname == name or fullname.startswith(name + \".\")
                    for name in blocked_roots
                ):
                    raise ImportError(\"blocked optional import: \" + fullname)
                return None

        sys.meta_path.insert(0, BlockedOptionalFinder())
        """
    ).strip()
    return prefix + "\n" + textwrap.dedent(body).strip() + "\n"


class PackageBoundaryTests(unittest.TestCase):
    def assert_isolated_success(self, script: str) -> subprocess.CompletedProcess[str]:
        result = run_isolated(script)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"isolated probe failed\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        return result

    def test_project_metadata_has_h3_identity_and_no_host_runtime_dependency(self) -> None:
        metadata_path = ROOT / "pyproject.toml"
        self.assertTrue(metadata_path.is_file())
        metadata = metadata_path.read_text(encoding="utf-8")
        self.assertRegex(metadata, r"(?m)^name\s*=\s*['\"]minimax-h3-studio['\"]\s*$")
        self.assertRegex(metadata, r"(?m)^version\s*=\s*['\"]\d+\.\d+\.\d+['\"]\s*$")
        self.assertRegex(metadata, r"(?m)^requires-python\s*=\s*['\"]>=3\.10['\"]\s*$")
        self.assertRegex(
            metadata,
            r"(?m)^authors\s*=\s*\[\{\s*name\s*=\s*['\"]rookiestar28['\"]\s*\}\]\s*$",
        )
        self.assertRegex(metadata, r"(?m)^license\s*=\s*['\"]Apache-2\.0['\"]\s*$")
        self.assertRegex(
            metadata,
            r"(?m)^license-files\s*=\s*\[\s*['\"]LICENSE['\"]\s*,\s*['\"]NOTICE['\"]\s*\]\s*$",
        )
        self.assertRegex(
            metadata,
            r"(?ms)^\[project\]\n.*?^dependencies\s*=\s*\[\s*\]\s*$",
        )

    def test_root_and_core_import_without_optional_frameworks(self) -> None:
        self.assert_isolated_success(
            import_blocker_script(
                """
                import importlib
                import pkgutil
                import re

                package = importlib.import_module("comfyui_h3_context")
                core = importlib.import_module("comfyui_h3_context.core")
                modules = [core.__name__]
                modules.extend(
                    info.name for info in pkgutil.walk_packages(core.__path__, core.__name__ + ".")
                )
                for module_name in modules:
                    importlib.import_module(module_name)
                assert re.fullmatch(r"\\d+\\.\\d+\\.\\d+", package.__version__)
                print("core imports:", ",".join(sorted(set(modules))))
                """
            )
        )

    def test_core_symbols_import_in_test_process_for_coverage(self) -> None:
        from comfyui_h3_context import __version__
        from comfyui_h3_context.core import OptionalDependencyError

        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        self.assertTrue(issubclass(OptionalDependencyError, ImportError))

    def test_repository_governance_implementations_do_not_ship_in_the_runtime_package(self) -> None:
        governance_root = ROOT / "scripts" / "governance"
        for module in GOVERNANCE_MODULES:
            with self.subTest(module=module):
                self.assertFalse((CORE_ROOT / f"{module}.py").exists())
                self.assertTrue((governance_root / f"{module}.py").is_file())

    def test_adapter_failure_path_is_exercised_in_test_process(self) -> None:
        from comfyui_h3_context.core.errors import OptionalDependencyError

        with self.assertRaises(OptionalDependencyError) as context:
            import comfyui_h3_context.adapters.comfyui  # noqa: F401
        self.assertIn("comfy_api", str(context.exception))

    def test_static_core_imports_are_stdlib_or_package_local(self) -> None:
        self.assertTrue(CORE_ROOT.is_dir(), "core package must exist")
        stdlib = set(getattr(sys, "stdlib_module_names", ()))
        self.assertTrue(stdlib, "Python 3.10+ stdlib module inventory is required")
        modules = sorted(CORE_ROOT.rglob("*.py"))
        self.assertTrue(modules, "at least one pure-core module must exist")

        violations: list[str] = []
        for path in modules:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = [alias.name.split(".", 1)[0] for alias in node.names]
                    for name in imported:
                        if name not in stdlib and name != "comfyui_h3_context":
                            violations.append(f"{path}:{node.lineno}: import {name}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    name = (node.module or "").split(".", 1)[0]
                    if name not in stdlib and name != "comfyui_h3_context":
                        violations.append(f"{path}:{node.lineno}: from {name}")
        self.assertEqual(
            violations, [], "core import boundary violations: " + "; ".join(violations)
        )

    def test_explicit_comfyui_adapter_reports_missing_host_capability(self) -> None:
        result = run_isolated(
            import_blocker_script(
                """
                try:
                    import comfyui_h3_context.adapters.comfyui  # noqa: F401
                except Exception as error:
                    from comfyui_h3_context.core.errors import OptionalDependencyError
                    assert isinstance(error, OptionalDependencyError), type(error).__name__
                    message = str(error)
                    assert "comfy_api" in message
                    assert "supported ComfyUI" in message
                    print(message)
                else:
                    raise AssertionError("the adapter must fail when comfy_api is unavailable")
                """
            )
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"adapter boundary probe failed\nstdout={result.stdout}\nstderr={result.stderr}",
        )

    def test_root_import_does_not_load_optional_adapter(self) -> None:
        result = self.assert_isolated_success(
            import_blocker_script(
                """
                import sys
                import comfyui_h3_context
                assert not any(
                    name == "comfy_api" or name.startswith(("comfy_api.", "comfy.", "diffusers."))
                    for name in sys.modules
                )
                print("root import is host-independent")
                """
            )
        )
        self.assertIn("host-independent", result.stdout)


if __name__ == "__main__":
    unittest.main()
