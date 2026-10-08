"""Backend CI prepares the real cross-language and CPU perception prerequisites."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_preflight

ROOT = Path(__file__).resolve().parents[1]


def test_backend_prepares_exact_frontend_tools_before_pytest() -> None:
    workflow: dict[str, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["backend"]["steps"]
    frontend = workflow["jobs"]["frontend"]["steps"]
    node = next(i for i, step in enumerate(steps) if "actions/setup-node@" in step.get("uses", ""))
    pinned = next(step for step in frontend if "actions/setup-node@" in step.get("uses", ""))
    assert steps[node]["uses"] == pinned["uses"]
    assert steps[node]["with"] == pinned["with"]
    pnpm = next(
        i for i, step in enumerate(steps) if "corepack prepare pnpm@11.3.0" in step.get("run", "")
    )
    install = next(
        i
        for i, step in enumerate(steps)
        if "pnpm --dir frontend install --frozen-lockfile" in step.get("run", "")
    )
    tests = next(i for i, step in enumerate(steps) if "-m pytest" in step.get("run", ""))
    assert node < pnpm < install < tests


def test_backend_supplies_cpu_tensor_image_process_and_host_test_dependencies() -> None:
    workflow: dict[str, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["backend"]["steps"]
    commands = [step.get("run", "") for step in steps]
    assert any('-e ".[dev,host-tests]"' in command for command in commands)
    cpu = next(i for i, command in enumerate(commands) if "torch==2.14.0+cpu" in command)
    assert "--index-url https://download.pytorch.org/whl/cpu" in commands[cpu]
    assert "--extra-index-url" not in commands[cpu]
    process = next(
        i for i, command in enumerate(commands) if "Pillow==12.3.0 psutil==7.2.2" in command
    )
    tests = next(i for i, command in enumerate(commands) if "-m pytest" in command)
    assert cpu < tests and process < tests
    assert workflow["jobs"]["backend"]["steps"][tests]["run"].endswith("--cov-fail-under=75")


def test_public_minimum_contracts_fail_before_the_long_backend_suite() -> None:
    workflow: dict[str, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["backend"]["steps"]
    setup = next(step for step in steps if "actions/setup-python@" in step.get("uses", ""))
    assert setup["with"]["python-version"] == "3.10"
    install = next(i for i, step in enumerate(steps) if "--frozen-lockfile" in step.get("run", ""))
    minimum = next(
        i for i, step in enumerate(steps) if "scripts/ci_preflight.py" in step.get("run", "")
    )
    tests = next(i for i, step in enumerate(steps) if "-m pytest" in step.get("run", ""))
    assert install < minimum < tests
    assert steps[minimum]["run"] == "python scripts/ci_preflight.py --public-minimum"


@pytest.mark.parametrize("defect", ["missing", "before_install", "after_tests"])
def test_missing_or_late_public_minimum_check_is_refused(defect: str) -> None:
    workflow: dict[str, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["backend"]["steps"]
    minimum = next(
        step
        for step in steps
        if step.get("run") == "python scripts/ci_preflight.py --public-minimum"
    )
    steps.remove(minimum)
    if defect == "before_install":
        install = next(
            i for i, step in enumerate(steps) if "--frozen-lockfile" in step.get("run", "")
        )
        steps.insert(install, minimum)
    elif defect == "after_tests":
        tests = next(i for i, step in enumerate(steps) if "-m pytest" in step.get("run", ""))
        steps.insert(tests + 1, minimum)
    with pytest.raises(ValueError, match="bootstrap"):
        ci_preflight.validate_workflow(workflow)
