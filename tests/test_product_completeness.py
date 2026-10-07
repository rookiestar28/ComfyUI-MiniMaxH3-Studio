"""Missing-member, dependency closure and isolated artifact-reader contracts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.product_completeness import (
    CompletenessError,
    require_present,
    required_paths,
    smoke_checkout,
)

ROOT = Path(__file__).resolve().parents[1]


def _sources() -> dict[str, bytes]:
    return {
        "pyproject.toml": b'[tool.comfy]\nincludes = ["comfyui_h3_context"]\n',
        "__init__.py": b"import comfyui_h3_context\n",
        "comfyui_h3_context/__init__.py": b"from .core.reader import value\n",
        "comfyui_h3_context/core/__init__.py": b"# package\n",
        "comfyui_h3_context/core/reader.py": b"value = 1\n",
        "comfyui_h3_context/contracts/resource.json": b"{}\n",
        "comfyui_h3_context/web/extra.wasm": b"wasm fixture",
        "comfyui_h3_context/tests/fixture.py": b"raise RuntimeError('development only')\n",
    }


def test_runtime_inventory_requires_future_extensions_but_excludes_development() -> None:
    sources = _sources()
    required = required_paths(sources, sources.__getitem__)
    assert required == frozenset(sources) - {"comfyui_h3_context/tests/fixture.py"}
    require_present(required, required, phase="fixture")
    for omitted in (
        "comfyui_h3_context/contracts/resource.json",
        "comfyui_h3_context/web/extra.wasm",
    ):
        with pytest.raises(CompletenessError, match="missing required"):
            require_present(required, required - {omitted}, phase="fixture")


def test_runtime_dependency_on_excluded_development_module_is_required_and_refused() -> None:
    sources = _sources()
    sources["comfyui_h3_context/core/reader.py"] = b"from ..tests.fixture import value\n"
    sources["comfyui_h3_context/tests/__init__.py"] = b"# synthetic package\n"
    required = required_paths(sources, sources.__getitem__)
    assert "comfyui_h3_context/tests/fixture.py" in required
    with pytest.raises(CompletenessError, match="missing required"):
        require_present(
            required, {path for path in sources if "/tests/" not in path}, phase="fixture"
        )


def test_missing_internal_import_is_not_treated_as_an_external_dependency() -> None:
    sources = _sources()
    del sources["comfyui_h3_context/core/reader.py"]
    with pytest.raises(CompletenessError, match="missing required local module"):
        required_paths(sources, sources.__getitem__)


def test_local_import_closure_requires_root_helper_and_package_initializers() -> None:
    sources = _sources()
    sources["comfyui_h3_context/core/reader.py"] = b"import runtime_helper.reader\n"
    sources["runtime_helper/reader.py"] = b"value = 1\n"
    sources["runtime_helper/__init__.py"] = b"# package\n"
    required = required_paths(sources, sources.__getitem__)
    assert {"runtime_helper/reader.py", "runtime_helper/__init__.py"} <= required


def test_workflow_dependency_closure_is_separate_from_zip_requirements() -> None:
    sources = _sources()
    sources[".github/workflows/publish.yml"] = b"run: python scripts/new_helper.py\n"
    sources["scripts/new_helper.py"] = b"from scripts.helper import check\n"
    sources["scripts/helper.py"] = b"def check(): pass\n"
    assert "scripts/helper.py" not in required_paths(sources, sources.__getitem__)
    assert {"scripts/new_helper.py", "scripts/helper.py"} <= required_paths(
        sources, sources.__getitem__, publication=True
    )


@pytest.mark.parametrize("declaration", ["../private", "/private", "missing_assets"])
def test_invalid_or_empty_runtime_declaration_fails(declaration: str) -> None:
    sources = _sources()
    sources["pyproject.toml"] = f'[tool.comfy]\nincludes = ["{declaration}"]\n'.encode()
    with pytest.raises(CompletenessError):
        required_paths(sources, sources.__getitem__)


@pytest.fixture
def isolated_product(tmp_path: Path) -> Path:
    shutil.copytree(
        ROOT / "comfyui_h3_context",
        tmp_path / "comfyui_h3_context",
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    shutil.copyfile(ROOT / "__init__.py", tmp_path / "__init__.py")
    for name in ("workflows", "subgraphs"):
        shutil.copytree(ROOT / name, tmp_path / name)
    return tmp_path


def test_real_product_registration_and_resource_readers_pass_in_isolation(
    isolated_product: Path,
) -> None:
    report = smoke_checkout(isolated_product)
    assert report["status"] == "PASS"
    assert report["registered_count"] > 0
    assert {"registration", "packaged_fonts", "prompt_catalogs", "official_assets"} <= set(
        report["checks"]
    )
    assert not list(isolated_product.rglob("*.pyc"))


@pytest.mark.parametrize(
    "missing",
    [
        "comfyui_h3_context/registration.py",
        "comfyui_h3_context/contracts/prompt_model_profiles_v6.json",
        "comfyui_h3_context/fonts/NotoSans-Regular.ttf",
        "comfyui_h3_context/web/h3-context-sidebar.js",
        "workflows/m3_07_h3_context_base.json",
    ],
)
def test_isolated_artifact_cannot_borrow_missing_files_from_development_checkout(
    isolated_product: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    (isolated_product / missing).unlink()
    with pytest.raises(CompletenessError, match="isolated product smoke failed"):
        smoke_checkout(isolated_product)


@pytest.mark.parametrize(
    "operation",
    [
        "from pathlib import Path; Path('unexpected.txt').write_text('fixture')",
        "import socket; socket.socket()",
        "import subprocess; subprocess.run(['unexpected-command'])",
    ],
)
def test_artifact_smoke_refuses_write_network_and_process_side_effects(
    isolated_product: Path, operation: str
) -> None:
    with (isolated_product / "__init__.py").open("a", encoding="utf-8") as stream:
        stream.write("\n" + operation + "\n")
    with pytest.raises(CompletenessError, match="isolated product smoke failed"):
        smoke_checkout(isolated_product)
    assert not (isolated_product / "unexpected.txt").exists()
