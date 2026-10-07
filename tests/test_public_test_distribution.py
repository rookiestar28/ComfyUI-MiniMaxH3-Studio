"""Public developer artifacts must carry runnable test inputs, without private records."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts import product_completeness, public_projection, registry_payload


def _sources() -> dict[str, bytes]:
    return {
        "__init__.py": b"import comfyui_h3_context\n",
        "comfyui_h3_context/__init__.py": b"value = 1\n",
        "comfyui_h3_context/web/h3-context-sidebar.js": b"export {};\n",
        "pyproject.toml": (
            b'[tool.comfy]\nincludes = ["comfyui_h3_context"]\n'
            b"[tool.h3-context.development]\n"
            b'roots = ["tests", "frontend", "scripts", "governance", '
            b'"compatibility", "requirements"]\n'
            b'required = [".pre-commit-config.yaml", "requirements/secret-scan-baseline.json"]\n'
        ),
        ".pre-commit-config.yaml": b"repos: []\n",
        "tests/test_feature.py": b"def test_feature(): assert True\n",
        "tests/fixtures/example.json": b"{}\n",
        "frontend/tests/example.test.ts": b"export {};\n",
        "frontend/tests/fixtures/example.ts": b"export const value = 1;\n",
        "frontend/e2e/example.tsx": b"export {};\n",
        "frontend/vitest.config.ts": b"export {};\n",
        "frontend/playwright.config.ts": b"export {};\n",
        "frontend/package.json": b"{}\n",
        "frontend/pnpm-lock.yaml": b"lockfileVersion: '9.0'\n",
        "scripts/run_full_tests_windows.ps1": b"exit 0\n",
        "governance/contracts/example.json": b"{}\n",
        "compatibility/example.json": b"{}\n",
        "requirements/secret-scan-baseline.json": b"{}\n",
        "tests/TEST_SOP.md": b"maintainer-only process\n",
        "tests/.planning/private.json": b"private fixture\n",
        "frontend/node_modules/installed.js": b"dependency cache\n",
    }


def test_complete_developer_inputs_are_required_independently_of_projection() -> None:
    sources = _sources()
    required = product_completeness.required_paths(sources, sources.__getitem__)
    for path in (
        "tests/test_feature.py",
        "tests/fixtures/example.json",
        "frontend/tests/fixtures/example.ts",
        "frontend/vitest.config.ts",
        "frontend/playwright.config.ts",
        "frontend/pnpm-lock.yaml",
        "scripts/run_full_tests_windows.ps1",
        "governance/contracts/example.json",
        "compatibility/example.json",
        "requirements/secret-scan-baseline.json",
    ):
        assert path in required
        with pytest.raises(product_completeness.CompletenessError, match="missing required"):
            product_completeness.require_present(required, required - {path}, phase="developer")
    assert "tests/TEST_SOP.md" not in required
    assert "tests/.planning/private.json" not in required
    assert "frontend/node_modules/installed.js" not in required


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_feature.py",
        "frontend/tests/example.test.ts",
        "frontend/e2e/example.tsx",
        "frontend/vitest.config.ts",
        "frontend/playwright.config.ts",
        "scripts/run_full_tests_windows.ps1",
        "governance/contracts/example.json",
        "compatibility/example.json",
        ".pre-commit-config.yaml",
        "requirements/secret-scan-baseline.json",
    ],
)
def test_projection_admits_public_test_inputs(path: str) -> None:
    assert public_projection.allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "tests/TEST_SOP.md",
        "tests/.planning/private.json",
        "tests/reference/private.json",
        "tests/__pycache__/test_feature.pyc",
        "frontend/tests/node_modules/package.js",
        "scripts/AGENTS.md",
        "governance/ROADMAP.md",
        ".secrets.baseline",
    ],
)
def test_new_allowlists_cannot_override_private_or_cache_exclusions(path: str) -> None:
    assert not public_projection.allowed(path)


def test_missing_development_dependency_declaration_fails_closed() -> None:
    sources = _sources()
    del sources["requirements/secret-scan-baseline.json"]
    with pytest.raises(product_completeness.CompletenessError, match="missing required"):
        product_completeness.required_paths(sources, sources.__getitem__)


@pytest.mark.parametrize(
    "omitted",
    [
        "tests/test_feature.py",
        "frontend/tests/fixtures/example.ts",
        "frontend/vitest.config.ts",
        "frontend/playwright.config.ts",
        "scripts/run_full_tests_windows.ps1",
        "requirements/secret-scan-baseline.json",
    ],
)
def test_actual_archive_cannot_omit_developer_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    omitted: str,
) -> None:
    sources = _sources()
    for relative, payload in sources.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    monkeypatch.setattr(registry_payload, "_git_tracked_paths", lambda root: frozenset(sources))
    archive_path = tmp_path / "node.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for relative, payload in sources.items():
            if public_projection.allowed(relative) and relative != omitted:
                archive.writestr(relative, payload)
    with pytest.raises(registry_payload.RegistryPayloadError, match="missing required"):
        registry_payload.build_registry_payload_report(archive_path, source_root=tmp_path)


@pytest.mark.parametrize(
    "relative", ["docs/private.md", "tests/fixtures/private.json", "frontend/private.yaml"]
)
def test_public_text_exception_does_not_admit_private_records(relative: str) -> None:
    with pytest.raises(registry_payload.RegistryPayloadError, match="internal marker"):
        registry_payload._inspect_text(b".planning/private-record.json", relative)


@pytest.mark.parametrize("suffix", [".mp4", ".wav", ".bin"])
def test_binary_test_fixtures_are_not_exempt_from_privacy_checks(suffix: str) -> None:
    relative = "tests/fixtures/example" + suffix
    registry_payload._inspect_text(b"synthetic malformed fixture\xff", relative)
    for payload in (b"C:" + b"/Users/operator/private", b"?" + b"sig=private"):
        with pytest.raises(registry_payload.RegistryPayloadError, match="private content"):
            registry_payload._inspect_text(payload, relative)
