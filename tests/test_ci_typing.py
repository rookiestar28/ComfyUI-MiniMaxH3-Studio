"""Pinned typing must cover both platform stubs, not the maintainer's OS alone."""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MYPY_REV = "41e691678310dfd3833f7ab4e180ddb014310356"  # pragma: allowlist secret


def test_pinned_mypy_checks_both_supported_platform_views() -> None:
    config: dict[str, Any] = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    repository = next(
        row for row in config["repos"] if row["repo"].endswith("pre-commit/mirrors-mypy")
    )
    assert repository["rev"] == MYPY_REV
    hooks = repository["hooks"]
    assert len(hooks) == 2
    assert all(hook["id"] == "mypy" for hook in hooks)
    assert {tuple(hook["args"]) for hook in hooks} == {
        (
            "--ignore-missing-imports",
            "--scripts-are-modules",
            "--python-version",
            "3.10",
            "--platform",
            "linux",
        ),
        (
            "--ignore-missing-imports",
            "--scripts-are-modules",
            "--python-version",
            "3.10",
            "--platform",
            "win32",
        ),
    }
    assert hooks[0]["additional_dependencies"] == hooks[1]["additional_dependencies"]
    assert hooks[0]["files"] == hooks[1]["files"]
    assert hooks[1]["alias"] == "mypy-windows"
