"""Small, real Git histories for public tests that cannot require maintainer objects."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def require_history(root: Path, commits: Sequence[str]) -> None:
    missing = [
        commit
        for commit in commits
        if subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=root,
            capture_output=True,
            check=False,
        ).returncode
    ]
    if missing:
        raise unittest.SkipTest(
            "NOT_RUN: exact historical objects unavailable: " + ", ".join(missing)
        )


@contextmanager
def git_history() -> Iterator[Path]:
    temporary = Path(__file__).resolve().parents[1] / ".tmp"
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="public-history-", dir=temporary) as directory:
        root = Path(directory)
        git(root, "init", "--quiet")
        yield root


def write_json(root: Path, relative: str, value: object) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value) + "\n", encoding="utf-8")


def commit(root: Path) -> str:
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "test: synthetic history",
    )
    return git(root, "rev-parse", "HEAD")
