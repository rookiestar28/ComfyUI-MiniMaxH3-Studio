"""Mechanical delivery-lane proposals from inert paths and explicit Git inputs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import change_lane


@pytest.mark.parametrize(
    "path",
    [
        "comfyui_h3_context/adapters/new_route.py",
        "comfyui_h3_context/contracts/new_contract.json",
        "governance/contracts/new_contract.json",
        "frontend/src/contracts/new_contract.ts",
        "comfyui_h3_context/core/prompt_model_dialects/new_model.py",
        "comfyui_h3_context/new_nodes.py",
        "comfyui_h3_context/core/new_provider.py",
        "comfyui_h3_context/core/workflow_migration_next.py",
        "comfyui_h3_context/core/node_contracts_next.py",
        "comfyui_h3_context/core/security.py",
        "comfyui_h3_context/core/durable_workspace_state.py",
        "comfyui_h3_context/core/private_storage_layout.py",
        "comfyui_h3_context/core/managed_run.py",
        "comfyui_h3_context/core/model_manifest.py",
        "comfyui_h3_context/core/semantic_graph_wire.py",
        "comfyui_h3_context/core/prompt_fidelity.py",
        "comfyui_h3_context/core/context_reporting.py",
        "comfyui_h3_context/core/public_api.py",
        "comfyui_h3_context/public_api.py",
        "scripts/browser_ci.py",
        "scripts/registry_next.py",
        "scripts/release_next.py",
        "scripts/run_full_tests_windows.ps1",
        "scripts/roadmap_registry.py",
        "scripts/public_consumer_compatibility.py",
    ],
)
def test_pinned_public_risk_surfaces_propose_heavy(path: str) -> None:
    result = change_lane.classify_paths([path])
    assert result["lane"] == "heavy"
    assert result["reasons"]


@pytest.mark.parametrize(
    "path",
    [
        "comfyui_h3_context/web/h3-context-sidebar.js",
        "comfyui_h3_context/core/__init__.py",
        "frontend/src/i18n/generatedBackendIdentities.ts",
        "tests/fixtures/canonical_cross_language_vectors.json",
        "public-source-inventory.json",
        "pyproject.toml",
        ".pre-commit-config.yaml",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "requirements/registry-publish-py310-linux-x86_64.txt",
    ],
)
def test_generated_and_dependency_metadata_never_propose_light(path: str) -> None:
    assert change_lane.classify_paths([path])["lane"] in {"standard", "heavy"}


def test_unique_endpoint_boundary_is_five_and_six() -> None:
    five = [f"frontend/src/components/Example{number}.tsx" for number in range(5)]
    assert change_lane.classify_paths([*five, five[0]])["lane"] == "light"
    assert change_lane.classify_paths([*five, "docs/example.md"])["lane"] == "standard"


@pytest.mark.parametrize("paths", [[], ["docs/example.md"]])
@pytest.mark.parametrize("declared_lane,marked", [("heavy", False), (None, True)])
def test_declared_or_marked_heavy_never_downgrades(
    paths: list[str],
    declared_lane: str | None,
    marked: bool,
) -> None:
    result = change_lane.classify_paths(paths, declared_lane=declared_lane, marked=marked)
    assert result["lane"] == "heavy"
    assert result["status"] == ("NO_CHANGE" if not paths else "PROPOSED")


def test_unmarked_empty_diff_reports_no_change() -> None:
    assert change_lane.classify_paths([])["status"] == "NO_CHANGE"


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Fixture Author")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "commit.gpgSign", "false")
    hooks = root / ".git/empty-hooks"
    hooks.mkdir()
    _git(root, "config", "core.hooksPath", str(hooks))
    files = {
        ".gitignore": "ignored/\n",
        "docs/old name.md": "Original inert rename content.\n",
        "docs/deleted.md": "To be deleted.\n",
        "docs/unstaged.md": "Original unstaged content.\n",
    }
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "test: create inert source fixture")
    return root, _git(root, "rev-parse", "HEAD")


def _cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(Path(change_lane.__file__).resolve()), *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_git_cli_reads_staged_unstaged_untracked_and_both_rename_endpoints(
    tmp_path: Path,
) -> None:
    root, base = _git_repo(tmp_path)
    target = root / "comfyui_h3_context/adapters/new_route.py"
    target.parent.mkdir(parents=True)
    _git(root, "mv", "docs/old name.md", str(target.relative_to(root)))
    _git(root, "rm", "docs/deleted.md")
    (root / "docs/unstaged.md").write_text("Changed locally.\n", encoding="utf-8")
    poison = root / "docs/poison.py"
    sentinel = root / "EXECUTED"
    poison.write_text("from pathlib import Path\nPath('EXECUTED').touch()\n", encoding="utf-8")
    ignored = root / "ignored/new.py"
    ignored.parent.mkdir()
    ignored.write_text("raise RuntimeError('must remain inert')\n", encoding="utf-8")
    before_index = (root / ".git/index").read_bytes()
    before_poison = poison.read_bytes()

    completed = _cli(root, "--base", base)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["base"] == base
    assert result["lane"] == "heavy"
    assert set(result["paths"]) == {
        "docs/old name.md",
        "comfyui_h3_context/adapters/new_route.py",
        "docs/deleted.md",
        "docs/unstaged.md",
        "docs/poison.py",
    }
    assert not sentinel.exists()
    assert poison.read_bytes() == before_poison
    assert (root / ".git/index").read_bytes() == before_index


def test_rename_out_of_a_heavy_surface_keeps_its_original_endpoint(tmp_path: Path) -> None:
    root, _base = _git_repo(tmp_path)
    old = root / "comfyui_h3_context/adapters/old.py"
    old.parent.mkdir(parents=True)
    old.write_text("Inert source.\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "test: add inert adapter fixture")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "mv", "comfyui_h3_context/adapters/old.py", "docs/renamed.py")

    completed = _cli(root, "--base", base)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["lane"] == "heavy"
    assert set(result["paths"]) == {"comfyui_h3_context/adapters/old.py", "docs/renamed.py"}


def test_base_commits_and_index_worktree_changes_are_all_visible(tmp_path: Path) -> None:
    root, base = _git_repo(tmp_path)
    (root / "docs/committed.md").write_text(
        "Change since the explicit baseline.\n", encoding="utf-8"
    )
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "test: extend inert fixture")
    (root / "docs/unstaged.md").write_text("Staged intermediate change.\n", encoding="utf-8")
    _git(root, "add", "docs/unstaged.md")
    (root / "docs/unstaged.md").write_text("Original unstaged content.\n", encoding="utf-8")

    completed = _cli(root, "--base", base)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["base"] == base
    assert set(result["paths"]) == {"docs/committed.md", "docs/unstaged.md"}


def test_tree_object_is_not_an_acceptable_commit_baseline(tmp_path: Path) -> None:
    root, _base = _git_repo(tmp_path)
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    assert _cli(root, "--base", tree).returncode != 0


@pytest.mark.parametrize("arguments", [[], ["--base", "missing-ref"], ["--base", "--help"]])
def test_cli_refuses_missing_or_non_commit_base(tmp_path: Path, arguments: list[str]) -> None:
    root, _base = _git_repo(tmp_path)
    before = (root / ".git/index").read_bytes()
    assert _cli(root, *arguments).returncode != 0
    assert (root / ".git/index").read_bytes() == before


def test_requested_metadata_is_required_and_a_valid_marked_item_survives_empty_diff(
    tmp_path: Path,
) -> None:
    # CRITICAL: tests/ is a typing package base. A tests.* import creates two module
    # identities for this fixture and aborts both mypy hooks.
    from test_roadmap_registry import _dict_list, _release_repo, _write_json

    root, base = _git_repo(tmp_path)
    absent = _cli(root, "--base", base, "--item", "RM-17")
    assert absent.returncode != 0
    fixture, index = _release_repo(tmp_path / "metadata")
    row = _dict_list(index["items"])[0]
    row["title"] = "[DANGEROUS] Synthetic governance invariant"
    row["lane"] = "light"
    _write_json(fixture / ".planning/roadmap/roadmap.json", index)

    completed = _cli(
        root,
        "--base",
        base,
        "--item",
        "RM-17",
        "--index",
        str(fixture / ".planning/roadmap/roadmap.json"),
    )

    # Metadata paths are repository-relative; an absolute external source is refused.
    assert completed.returncode != 0
    # Move only inert fixture metadata/plan/archive files into this synthetic repository.
    import shutil

    shutil.copytree(fixture / ".planning", root / ".planning")
    with (root / ".git/info/exclude").open("a", encoding="utf-8") as stream:
        stream.write(".planning/\n")
    completed = _cli(root, "--base", base, "--item", "RM-17")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "NO_CHANGE"
    assert result["lane"] == "heavy"

    row["title"] = "Synthetic unmarked item"
    _write_json(root / ".planning/roadmap/roadmap.json", index)
    plan = root / str(row["plan_path"])
    plan.write_text(
        "# Synthetic plan\n\n[IMPORTANT] A pinned compatibility invariant requires review.\n"
        "\n## Acceptance criteria\n\n- Preserve the invariant.\n",
        encoding="utf-8",
    )
    completed = _cli(root, "--base", base, "--item", "RM-17")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["lane"] == "heavy"
