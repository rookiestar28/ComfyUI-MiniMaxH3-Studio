"""Synthetic Git commands must never inherit a foreign repository's index or worktree."""

from __future__ import annotations

import os
from pathlib import Path

import history_fixture
import pytest


def test_foreign_git_steering_cannot_mutate_foreign_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    history_fixture.git(foreign, "init", "--quiet")
    (foreign / "sentinel.txt").write_text("foreign bytes\n")
    before = history_fixture.commit(foreign)
    index = (foreign / ".git/index").read_bytes()
    for name, value in {
        "GIT_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(foreign / ".git/index"),
    }.items():
        monkeypatch.setenv(name, value)
    snapshot = dict(os.environ)
    with history_fixture.git_history() as owned:
        (owned / "owned.txt").write_text("owned bytes\n")
        candidate = history_fixture.commit(owned)
        assert candidate != before
        assert history_fixture.git(owned, "ls-files") == "owned.txt"
    assert dict(os.environ) == snapshot
    assert (foreign / ".git/index").read_bytes() == index
    assert history_fixture.git(foreign, "rev-parse", "HEAD") == before
