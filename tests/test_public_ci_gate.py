"""Publication proof binds exact bytes, owned push, latest attempt and every hosted job."""

from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import public_ci_gate as gate

SHA = "a" * 40


def run() -> dict[str, Any]:
    return {
        "id": 42,
        "run_attempt": 1,
        "head_sha": SHA,
        "head_branch": "release-candidate/check",
        "event": "push",
        "path": gate.WORKFLOW_PATH,
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": gate.REPOSITORY},
        "head_repository": {"full_name": gate.REPOSITORY},
    }


def jobs(subject: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {
            "name": name,
            "run_id": subject["id"],
            "head_sha": subject["head_sha"],
            "status": "completed",
            "conclusion": "success",
        }
        for name in gate.EXPECTED_JOBS
    ]
    return {"total_count": len(rows), "jobs": rows}


def test_exact_green_candidate_and_every_job_are_required() -> None:
    subject = gate.approved_run({"workflow_runs": [run()]}, SHA)
    gate.validate_jobs(jobs(subject), subject)


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("event", "pull_request"),
        ("head_branch", "main"),
        ("path", ".github/workflows/other.yml"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("conclusion", "cancelled"),
        ("run_attempt", 0),
        ("head_repository", {"full_name": "foreign/repository"}),
        ("repository", {"full_name": "foreign/repository"}),
    ],
)
def test_wrong_or_unfinished_subject_is_refused(field: str, value: object) -> None:
    subject = run()
    subject[field] = value
    with pytest.raises(ValueError):
        gate.approved_run({"workflow_runs": [subject]}, SHA)


def test_new_failure_cannot_fall_back_to_old_success() -> None:
    newer = {**run(), "id": 43, "conclusion": "failure"}
    with pytest.raises(ValueError):
        gate.approved_run({"workflow_runs": [run(), newer]}, SHA)


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


def test_cli_reads_exact_attempt_and_never_echoes_network_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = []

    def response(path: str) -> dict[str, Any]:
        requests.append(path)
        return {"workflow_runs": [run()]} if "workflows" in path else jobs(run())

    monkeypatch.setattr(gate, "github_json", response)
    assert gate.main(["--candidate", SHA]) == 0
    assert requests == [
        f"actions/workflows/ci.yml/runs?head_sha={SHA}&event=push&per_page=100",
        "actions/runs/42/attempts/1/jobs?per_page=100",
    ]
    assert "PASS" in capsys.readouterr().out

    def refused(path: str) -> dict[str, Any]:
        raise OSError("private payload must stay out of output")

    monkeypatch.setattr(gate, "github_json", refused)
    assert gate.main(["--candidate", SHA]) == 1
    output = capsys.readouterr()
    assert "OSError" in output.err
    assert "private payload" not in output.out + output.err
