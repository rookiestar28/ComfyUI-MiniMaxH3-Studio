"""Real Git projection/CAS and separated tooling/payload preparation contracts."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import tomli

from scripts import public_projection as projection_module
from scripts import registry_release_controller as controller
from scripts.public_projection import (
    PUBLICATION_FILES,
    ProjectionError,
    allowed,
    entries,
    git,
    project,
    snapshot,
)
from scripts.validate_comfy_registry_metadata import PUBLIC_IDENTITY_PATHS

ROOT = Path(__file__).resolve().parents[1]


def _commit(root: Path) -> str:
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "test: fixture")
    return git(root, "rev-parse", "HEAD").decode().strip()


def _fixture_version(root: Path, source: str) -> str:
    # IMPORTANT: copied real metadata must not be paired with a hardcoded release version.
    manifest = tomli.loads(git(root, "show", source + ":pyproject.toml").decode("utf-8"))
    return str(manifest["project"]["version"])


def _fixture(root: Path) -> tuple[str, str]:
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "README.md").write_text("Public fixture\n", encoding="utf-8")
    parent = _commit(root)
    for name in (
        "__init__.py",
        "registry_release_controller.py",
        "public_projection.py",
        "public_source_policy.py",
        "public_build_backend.py",
        "sdist_payload.py",
        "product_completeness.py",
        "registry_pack_entry.py",
        "registry_payload.py",
        "registry_publication_capsule.py",
        "registry_publish_guard.py",
        "validate_comfy_registry_metadata.py",
    ):
        target = root / "scripts" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "scripts" / name, target)
    for name in (
        ".gitignore",
        ".comfyignore",
        "pyproject.toml",
        "LICENSE",
        "NOTICE",
        "MANIFEST.in",
        "__init__.py",
        ".github/workflows/publish.yml",
        "assets/h3_context.svg",
        "comfyui_h3_context/__init__.py",
        "comfyui_h3_context/web/h3-context-sidebar.js",
        "frontend/buildMetadata.ts",
        "frontend/src/buildMetadata.ts",
        "requirements/registry-publish-py310-linux-x86_64.txt",
        ".pre-commit-config.yaml",
        ".github/workflows/ci.yml",
        "requirements/secret-scan-baseline.json",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/playwright.config.ts",
        "frontend/vitest.config.ts",
        "scripts/run_full_tests_windows.ps1",
        "scripts/run_full_tests_linux.sh",
        "tests/fixtures/m16_01_source_requalification.json",
        "compatibility/compatibility_matrix_v1.json",
        *PUBLIC_IDENTITY_PATHS,
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    shutil.copytree(
        ROOT / "comfyui_h3_context",
        root / "comfyui_h3_context",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    for name in ("docs", "examples", "subgraphs", "workflows"):
        target = root / name / "fixture.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Public fixture\n", encoding="utf-8")
    for name in ("workflows", "subgraphs"):
        shutil.copytree(ROOT / name, root / name, dirs_exist_ok=True)
    for name in (
        "scripts/fixture.py",
        ".github/workflows/fixture.yml",
        "frontend/src/__tests__/fixture.py",
        "comfyui_h3_context/tests/fixture.py",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Development fixture\n", encoding="utf-8")
    return parent, _commit(root)


@pytest.fixture
def repository() -> Any:
    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="public-release-test-", dir=scratch) as directory:
        root = Path(directory)
        parent, source = _fixture(root)
        yield root, parent, source


def test_projection_is_reproducible_and_preserves_index_head(repository: Any) -> None:
    root, parent, source = repository
    before = snapshot(root)
    first = project(root, source=source, parent=parent, output=root / ".tmp/one")
    second = project(root, source=source, parent=parent, output=root / ".tmp/two")
    assert first["public_tree"] == second["public_tree"]
    assert snapshot(root) == before
    selected = entries(root, str(first["public_commit"]))
    assert selected == {
        path: value for path, value in entries(root, source).items() if allowed(path)
    }
    assert "scripts/fixture.py" in selected
    assert "frontend/src/__tests__/fixture.py" in selected
    assert all(not path.startswith(".github/") or path in PUBLICATION_FILES for path in selected)


@pytest.mark.parametrize("kind", ["declared-root", "runtime-import", "workflow-helper"])
def test_required_dependency_cannot_be_silently_filtered(kind: str, repository: Any) -> None:
    root, parent, _ = repository
    if kind == "declared-root":
        metadata = root / "pyproject.toml"
        metadata.write_text(
            metadata.read_text(encoding="utf-8").replace(
                "includes = [", 'includes = ["runtime_assets",', 1
            ),
            encoding="utf-8",
        )
        target = root / "runtime_assets" / "required.json"
    elif kind == "runtime-import":
        with (root / "__init__.py").open("a", encoding="utf-8") as stream:
            stream.write("\nimport required_helper\n")
        target = root / "required_helper.py"
    else:
        workflow = root / ".github/workflows/publish.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                ".publish-venv/bin/comfy node pack",
                ".publish-venv/bin/python scripts/required_helper.py\n"
                "          .publish-venv/bin/comfy node pack",
            ),
            encoding="utf-8",
        )
        target = root / "scripts" / "required_helper.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")
    source = _commit(root)
    before = snapshot(root)
    with (
        patch.object(
            projection_module,
            "allowed",
            side_effect=lambda path: path != target.relative_to(root).as_posix() and allowed(path),
        ),
        pytest.raises(ProjectionError, match="missing required"),
    ):
        project(root, source=source, parent=parent, output=root / ".tmp/missing-required")
    assert snapshot(root) == before


def test_publication_package_initializer_cannot_be_omitted(repository: Any) -> None:
    root, parent, source = repository
    with (
        patch.object(
            projection_module,
            "allowed",
            side_effect=lambda path: path != "scripts/__init__.py" and allowed(path),
        ),
        pytest.raises(ProjectionError, match="missing required files: scripts/__init__.py"),
    ):
        project(root, source=source, parent=parent, output=root / ".tmp/missing-initializer")


def test_projection_clone_uses_only_tracked_inputs(repository: Any) -> None:
    root, parent, source = repository
    clone = root / ".tmp/clone"
    clone.parent.mkdir()
    completed = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(root), str(clone)],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0
    assert not (clone / ".planning").exists()
    assert (
        project(clone, source=source, parent=parent, output=clone / ".tmp/projection")["status"]
        == "PASS"
    )


def test_projected_publication_guard_runs_without_development_checkout(repository: Any) -> None:
    root, parent, source = repository
    receipt = project(
        root,
        source=source,
        parent=parent,
        output=root / ".tmp/publication",
        local_ref="publication",
    )
    public = root / ".tmp/public-clone"
    completed = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-local",
            "--no-hardlinks",
            "--branch",
            "publication",
            str(root),
            str(public),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0
    required = {
        "scripts/__init__.py",
        ".github/workflows/publish.yml",
        "scripts/registry_publish_guard.py",
        "scripts/registry_payload.py",
        "scripts/validate_comfy_registry_metadata.py",
        "scripts/public_projection.py",
        "scripts/product_completeness.py",
    }
    assert required <= set(entries(public, str(receipt["public_commit"])))
    assert (public / "scripts/registry_release_controller.py").is_file()
    assert not (public / ".github/workflows/fixture.yml").exists()
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(public / "scripts/registry_publish_guard.py"),
            "--event-name",
            "push",
            "--previous-ref",
            parent,
            "--current-ref",
            str(receipt["public_commit"]),
            "--expected-candidate",
            str(receipt["public_commit"]),
            "--github-output",
            str(public / ".tmp-output"),
        ],
        cwd=public,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "should_publish=true" in (public / ".tmp-output").read_text(encoding="utf-8")
    validated = subprocess.run(
        [
            sys.executable,
            "-B",
            str(public / "scripts/validate_comfy_registry_metadata.py"),
            "--require-finalized",
            "--source-root",
            str(public),
            "--public-projection",
        ],
        cwd=public,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr


@pytest.mark.parametrize(
    "path",
    [
        "docs/.PLANNING/private.md",
        "docs/ROADMAP.md",
        "comfyui_h3_context/Reference/private.py",
        "docs/.env",
        "../README.md",
        "scripts/.planning/fixture.py",
        ".github/workflows/fixture.yml",
        ".github/workflows/publish.yml/private.txt",
        "scripts/registry_payload.py/private.txt",
        "docs\\secret.md",
    ],
)
def test_private_or_unsafe_projection_paths_are_refused(path: str, repository: Any) -> None:
    if path == "docs/.env":
        root, parent, source = repository
        source_entries = entries(root, source)
        source_entries["docs/.env"] = source_entries["README.md"]
        # Synthetic tree admission proves refusal without ever tracking an ignored fixture file.
        original = projection_module.entries

        def candidate_entries(repo: Path, revision: str) -> Any:
            return source_entries if revision == source else original(repo, revision)

        with (
            patch.object(projection_module, "entries", side_effect=candidate_entries),
            patch.object(
                projection_module,
                "allowed",
                side_effect=lambda path: path == "docs/.env" or allowed(path),
            ),
            pytest.raises(ProjectionError, match="ignored"),
        ):
            project(root, source=source, parent=parent, output=root / ".tmp/refused")
    else:
        assert not allowed(path)


def test_projection_ref_is_cas_created_only_after_validation(repository: Any) -> None:
    root, parent, source = repository
    receipt = project(
        root, source=source, parent=parent, output=root / ".tmp/valid", local_ref="public"
    )
    assert git(root, "rev-parse", "refs/heads/public").decode().strip() == receipt["public_commit"]
    with pytest.raises(ProjectionError, match="already"):
        project(
            root, source=source, parent=parent, output=root / ".tmp/conflict", local_ref="public"
        )
    assert not (root / ".tmp/conflict").exists()
    with pytest.raises(ProjectionError, match="predecessor"):
        project(
            root, source=source, parent=source, output=root / ".tmp/private-parent", local_ref="bad"
        )
    assert not (root / ".tmp/private-parent").exists()


@pytest.mark.parametrize("change", ["tooling", "version", "dirty", "predecessor"])
def test_controller_wrong_identity_fails_before_packing(change: str, repository: Any) -> None:
    root, parent, source = repository
    if change == "dirty":
        (root / "README.md").write_text("Changed\n", encoding="utf-8")
    with patch.object(controller, "ROOT", root), patch.object(controller, "_run") as packed:
        with pytest.raises(ValueError):
            controller.prepare(
                tooling_sha=parent if change == "tooling" else source,
                source=source,
                parent=source if change == "predecessor" else parent,
                version="9.9.9" if change == "version" else _fixture_version(root, source),
                environment=root / ".tmp/missing-env",
                output=root / ".tmp/refused",
            )
        packed.assert_not_called()
    assert not (root / ".tmp/refused").exists()


def test_controller_transfers_one_exact_audited_archive(repository: Any) -> None:
    root, parent, source = repository
    environment = root / ".tmp/env"
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"owned environment fixture")
    packed = []
    actual_run = subprocess.run

    def probe(command: list[str], **kwargs: Any) -> Any:
        if command[0] == str(executable):
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return actual_run(command, **kwargs)

    def pack(command: list[str], *, cwd: Path, output: Path) -> None:
        packed.append(command)
        assert command[2] == str(root / "scripts/registry_pack_entry.py")
        assert (cwd / "scripts/registry_publish_guard.py").is_file()
        assert (cwd / ".github/workflows/publish.yml").is_file()
        assert (cwd / "scripts/registry_release_controller.py").is_file()
        with zipfile.ZipFile(cwd / "node.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            from scripts.product_completeness import checkout_required_paths

            for name in checkout_required_paths(cwd, entries(cwd, "HEAD")):
                archive.write(cwd / name, name)

    with (
        patch.object(controller, "ROOT", root),
        patch.object(controller, "_run", side_effect=pack),
        patch.object(subprocess, "run", side_effect=probe),
    ):
        receipt = controller.prepare(
            tooling_sha=source,
            source=source,
            parent=parent,
            version=_fixture_version(root, source),
            environment=environment,
            output=root / ".tmp/prepared",
        )
    assert len(packed) == receipt["pack_count"] == 1
    assert receipt["version"] == _fixture_version(root, source)
    assert receipt["tooling_commit"] != receipt["public_commit"]
    assert receipt["publication_performed"] is False
    capsule = root / ".tmp/prepared/publication-capsule.tgz"
    assert hashlib.sha256(capsule.read_bytes()).hexdigest() == receipt["capsule_sha256"]
    assert (root / ".tmp/prepared/payload/node.zip").read_bytes() == (
        root / ".tmp/prepared/verified-transfer/node.zip"
    ).read_bytes()


def test_controller_cli_is_prepare_only_and_secret_free() -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts/registry_release_controller.py"), "--help"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0 and b"--tooling-sha" in result.stdout
    assert b"--token" not in result.stdout and b"--publish" not in result.stdout
    with patch.dict(
        os.environ,
        {
            "REGISTRY_ACCESS_TOKEN": "fixture",  # pragma: allowlist secret
            "PROVIDER_API_KEY": "fixture",  # pragma: allowlist secret
        },
    ):
        assert "REGISTRY_ACCESS_TOKEN" not in controller._environment(ROOT / ".tmp")
        assert "PROVIDER_API_KEY" not in controller._environment(ROOT / ".tmp")


def test_untracked_tooling_cannot_borrow_a_clean_head_identity(repository: Any) -> None:
    root, parent, _source = repository
    git(root, "rm", "--cached", "scripts/registry_pack_entry.py")
    git(root, "commit", "--quiet", "-m", "test: remove tracked helper")
    source = git(root, "rev-parse", "HEAD").decode().strip()
    assert not git(root, "diff", "HEAD", "--name-only")
    with patch.object(controller, "ROOT", root), patch.object(controller, "_run") as packed:
        with pytest.raises(ValueError, match="not tracked"):
            controller.prepare(
                tooling_sha=source,
                source=source,
                parent=parent,
                version=_fixture_version(root, source),
                environment=root / ".tmp/missing-env",
                output=root / ".tmp/refused",
            )
        packed.assert_not_called()
