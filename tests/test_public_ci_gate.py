"""Publication requires one original run, seven required jobs and one terminal advisory job."""

from __future__ import annotations

import copy
from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import public_ci_gate as gate

SHA = "a" * 40
ADVISORY = "Hermetic browser (windows-latest)"
REQUIRED = sorted(set(gate.EXPECTED_JOBS) - {ADVISORY})


def inventory(*subjects: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": len(subjects), "workflow_runs": list(subjects)}


def run(conclusion: str = "success") -> dict[str, Any]:
    return {
        "id": 42,
        "run_attempt": 1,
        "head_sha": SHA,
        "head_branch": "release-candidate/check",
        "event": "push",
        "path": gate.WORKFLOW_PATH,
        "status": "completed",
        "conclusion": conclusion,
        "repository": {"full_name": gate.REPOSITORY},
        "head_repository": {"full_name": gate.REPOSITORY},
    }


def jobs(subject: dict[str, Any], **conclusions: str) -> dict[str, Any]:
    rows = [
        {
            "name": name,
            "run_id": subject["id"],
            "head_sha": subject["head_sha"],
            "status": "completed",
            "conclusion": conclusions.get(name, "success"),
        }
        for name in gate.EXPECTED_JOBS
    ]
    return {"total_count": len(rows), "jobs": rows}


def admit(runs: list[dict[str, Any]], **conclusions: str) -> str:
    subject = gate.approved_run(inventory(*runs), SHA)
    return gate.validate_jobs(jobs(subject, **conclusions), subject)


def test_exact_green_candidate_and_every_job_are_required() -> None:
    assert admit([run()]) == "success"


def test_only_the_windows_browser_job_is_advisory() -> None:
    assert gate.ADVISORY_JOBS == frozenset({ADVISORY})
    assert len(REQUIRED) == 7


@pytest.mark.parametrize("advisory", ["failure", "cancelled"])
@pytest.mark.parametrize("run_conclusion", ["failure", "cancelled"])
def test_advisory_windows_browser_failure_does_not_block(
    advisory: str, run_conclusion: str
) -> None:
    assert admit([run(run_conclusion)], **{ADVISORY: advisory}) == advisory


@pytest.mark.parametrize("required", REQUIRED)
@pytest.mark.parametrize("advisory", ["success", "failure"])
def test_every_required_job_still_blocks(required: str, advisory: str) -> None:
    with pytest.raises(ValueError):
        admit([run("failure")], **{required: "failure", ADVISORY: advisory})


@pytest.mark.parametrize("defect", ["missing", "duplicate", "in_progress"])
def test_advisory_job_must_be_present_once_and_terminal(defect: str) -> None:
    subject = run("failure")
    payload = jobs(subject, **{ADVISORY: "failure"})
    rows = payload["jobs"]
    index = next(i for i, row in enumerate(rows) if row["name"] == ADVISORY)
    if defect == "missing":
        rows.pop(index)
        payload["total_count"] -= 1
    elif defect == "duplicate":
        rows[0] = copy.deepcopy(rows[index])
    else:
        rows[index]["status"] = "in_progress"
        rows[index]["conclusion"] = None
    with pytest.raises(ValueError):
        gate.validate_jobs(payload, subject)


def test_run_conclusion_must_agree_with_its_jobs() -> None:
    # A green run cannot hide a failed advisory job, and a red run needs a job that failed.
    with pytest.raises(ValueError):
        admit([run("success")], **{ADVISORY: "failure"})
    with pytest.raises(ValueError):
        admit([run("failure")])


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("event", "pull_request"),
        ("head_branch", "main"),
        ("path", ".github/workflows/other.yml"),
        ("status", "in_progress"),
        ("conclusion", None),
        ("conclusion", "timed_out"),
        ("conclusion", "startup_failure"),
        ("conclusion", "action_required"),
        ("run_attempt", 0),
        ("head_repository", {"full_name": "foreign/repository"}),
        ("repository", {"full_name": "foreign/repository"}),
    ],
)
def test_wrong_or_unfinished_subject_is_refused(field: str, value: object) -> None:
    subject = run()
    subject[field] = value
    with pytest.raises(ValueError):
        gate.approved_run(inventory(subject), SHA)


def test_new_failure_cannot_fall_back_to_old_success() -> None:
    newer = {**run("failure"), "id": 43}
    with pytest.raises(ValueError):
        gate.approved_run(inventory(run(), newer), SHA)


@pytest.mark.parametrize(
    "defect",
    [
        "attempt2",
        "second_green",
        "failed_first",
        "dispatch",
        "pr",
        "other_branch",
        "truncated",
        "missing_count",
        "boolean_count",
    ],
)
def test_same_sha_retries_and_incomplete_inventory_never_authorize_publication(defect: str) -> None:
    first = run()
    payload = inventory(first)
    if defect == "attempt2":
        first["run_attempt"] = 2
    elif defect in {"second_green", "failed_first", "dispatch", "pr", "other_branch"}:
        second = {**run(), "id": 43}
        if defect == "failed_first":
            first["conclusion"] = "failure"
        elif defect == "dispatch":
            second["event"] = "workflow_dispatch"
            second["head_branch"] = "dev"
        elif defect == "pr":
            second["event"] = "pull_request"
            second["head_branch"] = "feature"
        elif defect == "other_branch":
            second["head_branch"] = "main"
        payload = inventory(first, second)
    elif defect == "truncated":
        payload["total_count"] = 2
    elif defect == "missing_count":
        del payload["total_count"]
    elif defect == "boolean_count":
        payload["total_count"] = True
    with pytest.raises(ValueError):
        gate.approved_run(payload, SHA)


def test_ci_runs_on_rc_once_and_main_or_tag_promotion_does_not_start_ci() -> None:
    workflow = yaml.load(
        (Path(__file__).resolve().parents[1] / gate.WORKFLOW_PATH).read_text(),
        Loader=yaml.BaseLoader,  # noqa: S506 - strings only; preserves the workflow's on key
    )
    events = workflow["on"]
    push = events["push"]
    patterns = push["branches"]

    def admitted(branch: str) -> bool:
        return any(fnmatchcase(branch, pattern) for pattern in patterns)

    assert admitted("dev")
    assert admitted("release-candidate/1.2.0")
    assert not admitted("main")
    assert not admitted("feature/work")
    assert "tags" not in push and "tags-ignore" not in push
    assert "pull_request" in events and "workflow_dispatch" in events


@pytest.mark.parametrize(
    "defect", ["missing", "duplicate", "skipped", "failed", "stale", "truncated"]
)
def test_incomplete_matrix_is_refused(defect: str) -> None:
    subject = run()
    payload = jobs(subject)
    rows = payload["jobs"]
    if defect == "missing":
        rows.pop()
        payload["total_count"] -= 1
    elif defect == "duplicate":
        rows[1] = copy.deepcopy(rows[0])
    elif defect == "truncated":
        payload["total_count"] += 1
    else:
        rows[0]["head_sha" if defect == "stale" else "conclusion"] = defect
    with pytest.raises(ValueError):
        gate.validate_jobs(payload, subject)


def test_required_jobs_match_the_real_workflow_expansion() -> None:
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / gate.WORKFLOW_PATH).read_text()
    )
    names: Counter[str] = Counter()
    for job in workflow["jobs"].values():
        matrix = job.get("strategy", {}).get("matrix")
        if matrix:
            platforms = matrix.get("os") or [row["os"] for row in matrix["include"]]
            for platform in platforms:
                names[job["name"].replace("${{ matrix.os }}", platform)] += 1
        else:
            names[job["name"]] += 1
    assert names == gate.EXPECTED_JOBS
    assert gate.ADVISORY_JOBS <= set(names)


def test_cli_reads_exact_attempt_and_never_echoes_network_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = []

    def response(path: str) -> dict[str, Any]:
        requests.append(path)
        return (
            inventory(run())
            if "workflows" in path
            else run()
            if path == "actions/runs/42"
            else jobs(run())
        )

    monkeypatch.setattr(gate, "github_json", response)
    assert gate.main(["--candidate", SHA]) == 0
    assert requests == [
        f"actions/workflows/ci.yml/runs?head_sha={SHA}&per_page=100",
        "actions/runs/42/attempts/1/jobs?per_page=100",
        "actions/runs/42",
        f"actions/workflows/ci.yml/runs?head_sha={SHA}&per_page=100",
    ]
    assert "PASS" in capsys.readouterr().out

    def refused(path: str) -> dict[str, Any]:
        raise OSError("private payload must stay out of output")

    monkeypatch.setattr(gate, "github_json", refused)
    assert gate.main(["--candidate", SHA]) == 1
    output = capsys.readouterr()
    assert "OSError" in output.err
    assert "private payload" not in output.out + output.err


def test_cli_reports_the_advisory_result_on_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def response(path: str) -> dict[str, Any]:
        if "workflows" in path:
            return inventory(run("failure"))
        if path == "actions/runs/42":
            return run("failure")
        return jobs(run("failure"), **{ADVISORY: "failure"})

    monkeypatch.setattr(gate, "github_json", response)
    assert gate.main(["--candidate", SHA]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Public candidate CI: PASS sha={SHA} run=42 attempt=1")
    assert "windows-browser=failure (advisory)" in out


@pytest.mark.parametrize(
    "defect",
    [
        "fresh_attempt",
        "fresh_identity",
        "fresh_branch",
        "fresh_conclusion",
        "late_duplicate",
        "late_count",
        "wrong_intended_branch",
    ],
)
def test_fresh_readback_cannot_substitute_a_changed_run(defect: str) -> None:
    inventories = 0

    def response(path: str) -> dict[str, Any]:
        nonlocal inventories
        if "workflows" in path:
            inventories += 1
            if inventories == 2 and defect == "late_duplicate":
                return inventory(run(), {**run(), "id": 43})
            if inventories == 2 and defect == "late_count":
                return {**inventory(run()), "total_count": 2}
            return inventory(run())
        if path == "actions/runs/42":
            subject = run()
            if defect == "fresh_attempt":
                subject["run_attempt"] = 2
            elif defect == "fresh_identity":
                subject["id"] = 43
            elif defect == "fresh_branch":
                subject["head_branch"] = "release-candidate/other"
            elif defect == "fresh_conclusion":
                subject["conclusion"] = "failure"
            return subject
        return jobs(run())

    expected_branch = (
        "release-candidate/other"
        if defect == "wrong_intended_branch"
        else "release-candidate/check"
    )
    with pytest.raises(ValueError):
        gate.read_ci_proof(SHA, expected_branch, reader=response)
