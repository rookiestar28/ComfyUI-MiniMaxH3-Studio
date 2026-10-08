"""Cheap prerequisite checks refuse misleading local/hosted parity."""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_preflight

ROOT = Path(__file__).resolve().parents[1]


def test_current_workflow_prepares_each_real_consumer() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    ci_preflight.validate_workflow(workflow)


@pytest.mark.parametrize("defect", ["python", "extras", "node", "frozen", "pnpm"])
def test_missing_browser_bootstrap_is_refused(defect: str) -> None:
    workflow: dict[str, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = copy.deepcopy(workflow["jobs"]["browser"]["steps"])
    needles = {
        "python": "actions/setup-python@",
        "extras": ".[dev,host-tests]",
        "node": "actions/setup-node@",
        "frozen": "--frozen-lockfile",
        "pnpm": "corepack prepare pnpm@11.3.0",
    }
    workflow["jobs"]["browser"]["steps"] = [
        step for step in steps if needles[defect] not in str(step)
    ]
    with pytest.raises(ValueError, match="bootstrap"):
        ci_preflight.validate_workflow(workflow)


def test_fixture_interpreter_is_current_os_local_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / (".venv" if sys.platform == "win32" else ".venv-wsl")
    executable = prefix / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.touch()
    (prefix / "pyvenv.cfg").write_text("home = base\n")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    assert ci_preflight.fixture_python(tmp_path, environ={}) == executable
    assert (
        ci_preflight.fixture_python(tmp_path, environ={"H3_CONTEXT_E2E_PYTHON": str(executable)})
        == executable
    )
    for wrong in (str(tmp_path / "python"), sys.executable, "python"):
        with pytest.raises(ValueError, match="project-local"):
            ci_preflight.fixture_python(tmp_path, environ={"H3_CONTEXT_E2E_PYTHON": wrong})


def test_missing_and_foreign_prefix_never_fall_back(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="project-local"):
        ci_preflight.fixture_python(tmp_path, environ={})


@pytest.mark.parametrize("version", [(3, 9), (3, 11), (3, 13)])
def test_public_minimum_requires_actual_python310(version: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="Python 3.10"):
        ci_preflight.require_minimum_python(version)


def test_supported_local_is_not_claimed_as_minimum_runtime() -> None:
    ci_preflight.require_minimum_python((3, 10))
    command = ci_preflight.minimum_command(ROOT)
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert "tests/test_native_t2va_structure.py" in command
    assert "tests/test_closeout_matrix.py::SyntheticHistoryTests" in command
    assert "--cov" not in " ".join(command)


@pytest.mark.parametrize("defect", ["history", "private", "ignored", "dirty", "untracked"])
def test_public_check_refuses_non_public_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    def git(args: list[str], root: Path, **kwargs: Any) -> str:
        assert root == tmp_path
        if "--show-toplevel" in args:
            return str(tmp_path)
        if "rev-list" in args:
            return "2" if defect == "history" else "1"
        if "-ci" in args:
            return "tests/private.log" if defect == "ignored" else ""
        if "--others" in args:
            return "new.py" if defect == "untracked" else ""
        if "diff" in args:
            return "pyproject.toml" if defect == "dirty" else ""
        return ".planning/private.md\0" if defect == "private" else "pyproject.toml\0"

    monkeypatch.setattr(ci_preflight, "checked", git)
    with pytest.raises(ValueError, match="(public|single-commit)"):
        ci_preflight.validate_public_checkout(tmp_path)


def test_public_git_failure_is_not_empty_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*args: Any, **kwargs: Any) -> str:
        raise OSError("git unavailable")

    monkeypatch.setattr(ci_preflight, "checked", failed)
    with pytest.raises(OSError, match="git unavailable"):
        ci_preflight.validate_public_checkout(tmp_path)


def test_local_import_failure_stops_before_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_preflight, "fixture_python", lambda root: Path(sys.executable))

    def failed(args: list[str], root: Path, **kwargs: Any) -> str:
        if args[0] == "node":
            return "24.13.1"
        if "--version" in args:
            return "11.3.0"
        raise OSError("missing aiohttp")

    monkeypatch.setattr(ci_preflight, "checked", failed)
    with pytest.raises(OSError, match="missing aiohttp"):
        ci_preflight.runtime_preflight(ROOT)


@pytest.mark.parametrize("node,pnpm", [("22.1.0", "11.3.0"), ("24.13.1", "11.4.0")])
def test_runtime_selection_is_checked_not_inferred_from_workflow(
    monkeypatch: pytest.MonkeyPatch, node: str, pnpm: str
) -> None:
    monkeypatch.setattr(ci_preflight, "fixture_python", lambda root: Path(sys.executable))
    monkeypatch.setattr(
        ci_preflight, "checked", lambda args, root, **kwargs: node if args[0] == "node" else pnpm
    )
    with pytest.raises(ValueError, match="supported Node 24 and exact"):
        ci_preflight.runtime_preflight(ROOT)


def test_public_minimum_dispatches_original_toml_reader_and_bounded_real_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(ci_preflight, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["ci_preflight.py", "--public-minimum"])
    monkeypatch.setattr(ci_preflight, "require_minimum_python", lambda version: None)
    monkeypatch.setattr(ci_preflight, "validate_public_checkout", lambda root: None)

    def command(args: list[str], root: Path, **kwargs: Any) -> str:
        calls.append(args)
        return ""

    def run(args: list[str], **kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(ci_preflight, "checked", command)
    monkeypatch.setattr(subprocess, "run", run)
    assert ci_preflight.main() == 0
    assert "scripts.nle_hardening_dependency_audit import tomllib" in calls[0][2]
    assert "tomllib.loads" in calls[0][2]
    assert calls[1] == ci_preflight.minimum_command(tmp_path)
