"""M15-03 production bundle, schema, loader, and package-inventory contracts."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import comfyui_h3_context
from comfyui_h3_context.core import PRODUCT_SHELL_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = ROOT / "comfyui_h3_context" / "web"


def test_product_shell_schema_and_loader_web_directory_are_exact() -> None:
    schema = json.loads(
        (ROOT / "governance" / "contracts" / "product_shell_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["properties"]["schema"]["const"] == PRODUCT_SHELL_SCHEMA
    assert schema["additionalProperties"] is False
    assert comfyui_h3_context.WEB_DIRECTORY == "web"

    spec = importlib.util.spec_from_file_location(
        "h3_context_loader_bundle_test", ROOT / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.WEB_DIRECTORY == "./comfyui_h3_context/web"


def test_shipping_web_inventory_is_one_local_content_free_esm() -> None:
    files = sorted(path.relative_to(BUNDLE_ROOT).as_posix() for path in BUNDLE_ROOT.rglob("*"))
    assert files == ["h3-context-sidebar.js"]
    bundle = (BUNDLE_ROOT / files[0]).read_text(encoding="utf-8")
    assert "sourceMappingURL" not in bundle
    assert "process.env" not in bundle
    assert "reference/" not in bundle.casefold()
    assert "node_modules" not in bundle.casefold()
    assert (
        re.search(r'(?:from\s*["\']|import\s*\(\s*["\'])https?://', bundle, re.IGNORECASE) is None
    )
    assert re.search(r"<script[^>]+src\s*=\s*[\"']https?://", bundle, re.IGNORECASE) is None
    imports = re.findall(r'^import\s+[^;\n]+\s+from\s*["\']([^"\']+)["\']', bundle, re.MULTILINE)
    assert set(imports) == {"../../scripts/app.js", "../../scripts/api.js"}


def _assert_shipping_allowlists(pyproject: dict[str, Any]) -> None:
    # CRITICAL: development metadata and source-distribution inputs are not wheel/Registry
    # selectors. Scanning all TOML rejects legitimate source paths without detecting payloads.
    tool = pyproject["tool"]
    setuptools = tool["setuptools"]
    assert setuptools["packages"]["find"]["where"] == ["."]
    assert setuptools["packages"]["find"]["include"] == ["comfyui_h3_context*"]
    data = setuptools["package-data"]
    assert set(data) == {"comfyui_h3_context"}
    assert "web/*.js" in data["comfyui_h3_context"]
    paths = [*tool["comfy"]["includes"], *data["comfyui_h3_context"]]
    for value in paths:
        assert "\\" not in value
        path = PurePosixPath(value)
        assert not path.is_absolute() and ".." not in path.parts
        assert not {"frontend", "reference", ".planning", "node_modules"} & set(path.parts)


def test_package_allowlists_include_only_the_built_web_entry() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    _assert_shipping_allowlists(pyproject)
    # The sanitized source snapshot includes complete sources; wheel data remains built JS.
    assert "graft comfyui_h3_context" in manifest


def test_development_paths_do_not_select_shipping_payloads() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "frontend/package.json" in pyproject["tool"]["h3-context"]["development"]["required"]
    candidate = copy.deepcopy(pyproject)
    candidate["tool"]["h3-context"]["development"]["required"].append("frontend/tests/local.ts")
    _assert_shipping_allowlists(candidate)


@pytest.mark.parametrize("root", ("frontend", "reference", ".planning", "node_modules"))
@pytest.mark.parametrize("selector", ("registry", "package-data"))
def test_forbidden_shipping_paths_are_rejected(root: str, selector: str) -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if selector == "registry":
        target = pyproject["tool"]["comfy"]["includes"]
    else:
        target = pyproject["tool"]["setuptools"]["package-data"]["comfyui_h3_context"]
    target.append(f"{root}/*")
    with pytest.raises(AssertionError):
        _assert_shipping_allowlists(pyproject)
