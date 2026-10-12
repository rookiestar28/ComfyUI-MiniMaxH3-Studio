"""Real bare-remote admission of CI vectors, changing refs and post-write readback."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from test_release_push_guard import VERSION, git, guard, invoke, reader, ready, refuse
from test_release_push_guard import remote as remote


@pytest.mark.parametrize(
    "defect",
    [
        "required_failure",
        "missing",
        "duplicate",
        "extra",
        "wrong_sha",
        "wrong_run",
        "unfinished",
        "truncated",
        "boolean_count",
        "attempt2",
        "dispatch",
        "foreign",
        "fresh_attempt",
    ],
)
def test_bad_ci_proof_refuses_before_native_promotion(remote: Any, defect: str) -> None:
    _repo, _bare, _parent, candidate = remote
    ready(remote)
    original = reader(candidate)

    def api(path: str) -> dict[str, Any]:
        document = cast(dict[str, Any], original(path))
        if "jobs" in document:
            jobs = document["jobs"]
            if defect == "required_failure":
                jobs[0]["conclusion"] = "failure"
            elif defect == "missing":
                jobs.pop()
            elif defect == "duplicate":
                jobs[-1] = dict(jobs[0])
            elif defect == "extra":
                jobs.append({**jobs[0], "name": "Extra job"})
            elif defect == "wrong_sha":
                jobs[0]["head_sha"] = "b" * 40
            elif defect == "wrong_run":
                jobs[0]["run_id"] = 43
            elif defect == "unfinished":
                jobs[-1]["status"] = "in_progress"
            document["total_count"] = len(jobs)
            if defect == "truncated":
                document["total_count"] += 1
            elif defect == "boolean_count":
                document["total_count"] = True
        else:
            runs = document.get("workflow_runs", [document])
            for run in runs:
                if defect == "attempt2" or defect == "fresh_attempt" and path == "actions/runs/42":
                    run["run_attempt"] = 2
                elif defect == "dispatch":
                    run["event"] = "workflow_dispatch"
                elif defect == "foreign":
                    run["repository"] = {"full_name": "fixture/foreign"}
        return document

    refuse(remote, api=api)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_only_advisory_failure_allows_atomic_promotion(remote: Any, conclusion: str) -> None:
    _repo, bare, _parent, candidate = remote
    ready(remote)
    original = reader(candidate)

    def api(path: str) -> dict[str, Any]:
        document = cast(dict[str, Any], original(path))
        if "jobs" in document:
            next(
                job
                for job in document["jobs"]
                if job["name"] == "Hermetic browser (windows-latest)"
            )["conclusion"] = conclusion
        else:
            for run in document.get("workflow_runs", [document]):
                run["conclusion"] = conclusion
        return document

    proof, calls = invoke(remote, "promotion", api=api, boundary="controller")
    assert proof["run_conclusion"] == proof["windows_browser_conclusion"] == conclusion
    assert len(calls) == 1 and "--atomic" in calls[0]
    assert git(bare, "rev-parse", "refs/heads/main") == candidate


def test_live_rc_change_during_ci_observation_refuses_before_push(remote: Any) -> None:
    repo, bare, parent, candidate = remote
    ready(remote)
    before = git(bare, "show-ref")
    original = reader(candidate)
    changed = False
    pushes = []

    def api(path: str) -> dict[str, Any]:
        nonlocal changed
        if "workflows" in path and not changed:
            git(bare, "update-ref", f"refs/heads/release-candidate/{VERSION}", parent, candidate)
            changed = True
        return cast(dict[str, Any], original(path))

    def execute(argv: list[str], **kwargs: Any) -> Any:
        if "push" in argv:
            pushes.append(argv)
        return subprocess.run(argv, **kwargs)

    with pytest.raises(ValueError, match="LIVE_PUBLIC_REFS_CHANGED"):
        guard().push_release(
            root=repo,
            candidate=candidate,
            expected_version=VERSION,
            operation="promotion",
            receipt=repo / ".tmp/live-change.json",
            runner=execute,
            reader=api,
            expected_origin=bare.as_posix(),
        )
    assert changed and not pushes
    expected = before.replace(
        f"{candidate} refs/heads/release-candidate/{VERSION}",
        f"{parent} refs/heads/release-candidate/{VERSION}",
    )
    assert git(bare, "show-ref") == expected


def test_successful_native_push_with_failed_readback_records_the_write(remote: Any) -> None:
    repo, bare, _parent, candidate = remote
    pushed = False
    pushes = []

    def execute(argv: list[str], **kwargs: Any) -> Any:
        nonlocal pushed
        if "push" in argv:
            pushes.append(argv)
            result = subprocess.run(argv, **kwargs)
            pushed = result.returncode == 0
            return result
        if pushed and "ls-remote" in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"fixture readback unavailable")
        return subprocess.run(argv, **kwargs)

    target = repo / ".tmp/readback.json"
    with pytest.raises(ValueError, match="GIT_READ_FAILED"):
        guard().push_release(
            root=repo,
            candidate=candidate,
            expected_version=VERSION,
            operation="candidate",
            receipt=target,
            runner=execute,
            reader=reader(candidate, defect="empty"),
            expected_origin=bare.as_posix(),
        )
    proof = json.loads(target.read_text())
    assert pushed and len(pushes) == 1
    assert proof["native_push_exit"] == 0 and proof["status"] == "PUSHED_READBACK_FAILED"
    assert git(bare, "rev-parse", f"refs/heads/release-candidate/{VERSION}") == candidate


def test_guard_help_has_no_external_side_effects() -> None:
    path = Path(__file__).resolve().parents[1] / "scripts/release_push_guard.py"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(path), "--help"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0
    assert b"--operation" in result.stdout and b"--expected-version" in result.stdout
    assert b"--token" not in result.stdout and b"--force" not in result.stdout


def test_guard_help_does_not_load_version_parser() -> None:
    path = Path(__file__).resolve().parents[1] / "scripts/release_push_guard.py"
    program = """
import importlib.abc
import runpy
import sys

class BlockVersionParser(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "scripts.registry_publish_guard":
            raise ImportError("version parser must remain unused during help")

sys.meta_path.insert(0, BlockVersionParser())
sys.argv = [sys.argv[1], "--help"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(path)],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0
    assert b"--operation" in result.stdout
