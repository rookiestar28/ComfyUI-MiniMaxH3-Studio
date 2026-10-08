"""Cheap prerequisite checks refuse misleading local/hosted parity."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import history_fixture
import pytest
import yaml

from scripts import acceptance_baseline, ci_preflight

ROOT = Path(__file__).resolve().parents[1]


def test_current_workflow_prepares_each_real_consumer() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    ci_preflight.validate_workflow(workflow)


@pytest.mark.skipif(sys.platform == "win32", reason="the quality shell consumer runs on Linux")
def test_initial_push_shell_resolves_sha_and_still_refuses_policy_relaxation(
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        row
        for row in workflow["jobs"]["quality"]["steps"]
        if row.get("if") == "github.event_name == 'push'"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    shutil.copyfile(
        ROOT / "scripts/acceptance_baseline.py", tmp_path / "scripts/acceptance_baseline.py"
    )
    (tmp_path / "tests/sample_test.py").write_text(
        "def test_transition():\n    pass\n", encoding="utf-8"
    )
    consumer = tmp_path / "fixture_policy.py"
    consumer.write_text("LIMIT = 10\n", encoding="utf-8")
    payload = {
        "schema": "h3-context-acceptance-baseline/1",
        "criteria": [
            {
                "id": "fixture.transition",
                "path": "tests/sample_test.py",
                "runner": "pytest",
                "selector": "test_transition",
            }
        ],
        "policies": [
            {
                "consumer": "fixture_policy:LIMIT",
                "direction": "maximum",
                "id": "fixture.maximum",
                "value": 10,
            }
        ],
        "public_python_abi": [
            acceptance_baseline.describe_public_callable("GenerationSequencePlan")
        ],
    }
    manifest = tmp_path / "tests/acceptance_baseline.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    history_fixture.git(tmp_path, "init", "--quiet")
    base = history_fixture.commit(tmp_path)
    ref = "refs/remotes/origin/main"
    history_fixture.git(tmp_path, "update-ref", ref, base)
    env = history_fixture.git_environment()
    env.update(POLICY_BASE_SHA="0" * 40, POLICY_DEFAULT_REF=ref)
    env["PYTHONPATH"] = str(ROOT)  # The synthetic tree owns the real CLI; core ABI comes from here.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")

    def execute() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )

    initial = execute()
    assert initial.returncode == 0, initial.stdout + initial.stderr
    env["POLICY_BASE_SHA"] = base
    assert execute().returncode == 0
    original = manifest.read_bytes()
    payload = json.loads(original)
    policy = payload["policies"][0]
    policy["value"] += 1 if policy["direction"] == "minimum" else -1
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    consumer.write_text("LIMIT = 9\n", encoding="utf-8")
    stricter = history_fixture.commit(tmp_path)
    history_fixture.git(tmp_path, "update-ref", ref, stricter)
    manifest.write_bytes(original)
    consumer.write_text("LIMIT = 10\n", encoding="utf-8")
    env["POLICY_BASE_SHA"] = "0" * 40
    relaxed = execute()
    assert relaxed.returncode == 3, relaxed.stdout + relaxed.stderr
    assert "POLICY RELAXATION" in relaxed.stdout
    env["POLICY_DEFAULT_REF"] = "refs/remotes/origin/missing"
    assert execute().returncode != 0


@pytest.mark.parametrize("job", ["backend", "browser", "frontend"])
@pytest.mark.parametrize(
    "attribute,value",
    [("if", "false"), ("continue-on-error", True), ("working-directory", "other")],
)
def test_bootstrap_cannot_be_bypassed(job: str, attribute: str, value: object) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    install = next(
        row for row in workflow["jobs"][job]["steps"] if "--frozen-lockfile" in row.get("run", "")
    )
    install[attribute] = value
    with pytest.raises(ValueError, match="bootstrap"):
        ci_preflight.validate_workflow(workflow)


def test_missing_job_bound_is_refused() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    workflow["jobs"]["browser"].pop("timeout-minutes", None)
    with pytest.raises(ValueError, match="bootstrap"):
        ci_preflight.validate_workflow(workflow)


@pytest.mark.parametrize(
    "job,kind",
    [
        (job, kind)
        for job in ("backend", "conformance", "browser")
        for kind in ("install", "consumer", "activation")
        if (job, kind) != ("conformance", "activation")
    ],
)
def test_echoed_command_cannot_impersonate_bootstrap_or_execution(job: str, kind: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    needles = {
        "install": ".[dev,host-tests]",
        "consumer": {
            "backend": "--cov=comfyui",
            "conformance": "--no-cov",
            "browser": "scripts/browser_ci.py",
        }[job],
        "activation": "corepack prepare",
    }
    step = next(
        row for row in workflow["jobs"][job]["steps"] if needles[kind] in row.get("run", "")
    )
    step["run"] = 'echo "' + step["run"].replace('"', '\\"') + '"'
    with pytest.raises(ValueError, match="bootstrap"):
        ci_preflight.validate_workflow(workflow)


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("if", "false"),
        ("continue-on-error", True),
        ("defaults", {"run": {"working-directory": "other"}}),
    ],
)
def test_job_level_bootstrap_bypass_is_refused(attribute: str, value: object) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    workflow["jobs"]["backend"][attribute] = value
    with pytest.raises(ValueError, match="bootstrap"):
        ci_preflight.validate_workflow(workflow)


@pytest.mark.parametrize("defect", ["loss", "overlap", "duplicate", "empty"])
def test_backend_partition_refuses_population_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    conformance = [f"{name}::test_one" for name in ci_preflight.CONFORMANCE_FILES]
    rest = ["tests/test_nodes.py::test_regular"]
    full = rest + conformance
    if defect == "loss":
        rest = ["tests/test_nodes.py::test_different"]
    elif defect == "overlap":
        rest += conformance[:1]
    elif defect == "duplicate":
        full += full[:1]
    else:
        full = []
    outputs = iter(["\n".join(rows) for rows in (full, rest, conformance)])
    monkeypatch.setattr(ci_preflight, "checked", lambda *args, **kwargs: next(outputs))
    with pytest.raises(ValueError, match="backend"):
        ci_preflight.validate_backend_partition(tmp_path)


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
        assert kwargs["timeout"] == 600
        from tests.test_public_minimum_receipt import receipt

        receipt(tmp_path / ".tmp/ci-public-minimum.xml")

    monkeypatch.setattr(ci_preflight, "checked", command)
    monkeypatch.setattr(subprocess, "run", run)
    assert ci_preflight.main() == 0
    assert "scripts.nle_hardening_dependency_audit import tomllib" in calls[0][2]
    assert "tomllib.loads" in calls[0][2]
    assert calls[1] == ci_preflight.minimum_command(tmp_path)
