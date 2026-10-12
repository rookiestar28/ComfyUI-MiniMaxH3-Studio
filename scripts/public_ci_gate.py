"""Require successful hosted execution of the exact public candidate before publication.

This is read-only: it never pushes, promotes, dispatches or publishes. CI must run from a
release-candidate/* push, whose checkout is the projection itself rather than a PR merge tree.
Every job must pass except the advisory Windows browser job, which must only be present and
terminal; its conclusion is reported, not enforced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import Any
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = (
    "rookiestar28/ComfyUI-MiniMaxH3-Studio"  # pragma: allowlist secret - public repository ID
)
WORKFLOW_PATH = ".github/workflows/ci.yml"
EXPECTED_JOBS = Counter(
    {
        "Import smoke (ubuntu-latest)": 1,
        "Import smoke (windows-latest)": 1,
        "Supply and quality": 1,
        "Backend tests and security": 1,
        "Untraced semantic conformance": 1,
        "Frontend check, unit and build": 1,
        "Hermetic browser (ubuntu-latest)": 1,
        "Hermetic browser (windows-latest)": 1,
    }
)
# CRITICAL: exactly this one job is advisory (, 2026-10-12). Its result is printed
# for the release review but does not block publication. Adding any other job here lets
# a real failure in that lane publish; the local Windows Full Gate remains the acceptance result.
WINDOWS_BROWSER = "Hermetic browser (windows-latest)"
ADVISORY_JOBS = frozenset({WINDOWS_BROWSER})
RUN_CONCLUSIONS = frozenset({"success", "failure", "cancelled"})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise ValueError("hosted API redirect refused")


def inventory_runs(payload: dict[str, Any], candidate: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or candidate == "0" * 40:
        raise ValueError("candidate must be a nonzero full commit SHA")
    runs = payload.get("workflow_runs")
    count = payload.get("total_count")
    if (
        not isinstance(runs, list)
        or type(count) is not int
        or count != len(runs)
        or not 0 <= count <= 100
    ):
        raise ValueError("missing or truncated hosted run inventory")
    if any(
        not isinstance(row, dict)
        or row.get("head_sha") != candidate
        or type(row.get("id")) is not int
        or row["id"] <= 0
        for row in runs
    ):
        raise ValueError("invalid exact candidate run inventory")
    if len({row["id"] for row in runs}) != len(runs):
        raise ValueError("duplicate hosted run identity")
    return runs


def approved_run(
    payload: dict[str, Any], candidate: str, branch: str | None = None
) -> dict[str, Any]:
    runs = inventory_runs(payload, candidate)
    # CRITICAL: inspect all exact-SHA events, not only green RC pushes; filtering or choosing
    # the newest run hides retries and failed-first/later-green publication attempts.
    if len(runs) != 1:
        raise ValueError("exact candidate must have one original CI run")
    run = runs[0]
    if (
        run.get("event") != "push"
        or not isinstance(run.get("head_branch"), str)
        or not run["head_branch"].startswith("release-candidate/")
        or run["head_branch"] == "release-candidate/"
        or (branch is not None and run["head_branch"] != branch)
        or run.get("path") != WORKFLOW_PATH
        or not isinstance(run.get("repository"), dict)
        or not isinstance(run.get("head_repository"), dict)
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("status") != "completed"
        or run.get("conclusion") not in RUN_CONCLUSIONS
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] != 1
    ):
        raise ValueError("exact candidate CI is not a completed owned push")
    return run


def validate_jobs(payload: dict[str, Any], run: dict[str, Any]) -> str:
    """Admit the run's jobs and return the advisory job's conclusion."""
    jobs = payload.get("jobs")
    if (
        not isinstance(jobs, list)
        or type(payload.get("total_count")) is not int
        or payload.get("total_count") != len(jobs)
    ):
        raise ValueError("missing or truncated hosted jobs")
    if any(not isinstance(job, dict) or not isinstance(job.get("name"), str) for job in jobs):
        raise ValueError("invalid hosted job")
    if Counter(job.get("name") for job in jobs) != EXPECTED_JOBS:
        raise ValueError("hosted jobs do not conserve the required CI matrix")
    if any(
        job.get("status") != "completed"
        or job.get("run_id") != run["id"]
        or job.get("head_sha") != run["head_sha"]
        or (job["name"] not in ADVISORY_JOBS and job.get("conclusion") != "success")
        for job in jobs
    ):
        raise ValueError("hosted job did not pass on the exact candidate")
    unsuccessful = {job["name"] for job in jobs if job.get("conclusion") != "success"}
    # The run's own conclusion must agree with its jobs: green only when every job is green, and
    # red only when the advisory job is the one that did not succeed.
    if (run["conclusion"] == "success") != (not unsuccessful):
        raise ValueError("hosted run conclusion disagrees with its jobs")
    # Select the reported job by its exact name, never by unpacking the advisory set: an unpack
    # raises for a widened set, and that error would make a required failure look refused.
    advisory = next(job for job in jobs if job["name"] == WINDOWS_BROWSER)
    conclusion = advisory.get("conclusion")
    if not isinstance(conclusion, str):
        raise ValueError("advisory hosted job has no conclusion")
    return conclusion


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate hosted JSON member")
        result[key] = value
    return result


def _finite_constant(value: str) -> None:
    raise ValueError("nonfinite hosted JSON constant")


def github_json(path: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "public-candidate-ci-gate",
        "Cache-Control": "no-cache",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"https://api.github.com/repos/{REPOSITORY}/{path}", headers=headers)
    # CRITICAL: redirects must not forward the read token to a different origin.
    with build_opener(_NoRedirect).open(request, timeout=20) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("hosted response exceeds the bounded inventory")
    payload = json.loads(raw, object_pairs_hook=_unique_members, parse_constant=_finite_constant)
    if not isinstance(payload, dict):
        raise ValueError("invalid hosted response")
    return payload


def read_ci_proof(
    candidate: str, branch: str | None = None, *, reader: Any = None
) -> tuple[dict[str, Any], str]:
    """Read one complete first-run proof and refuse a run/attempt change during observation."""
    read = github_json if reader is None else reader
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or candidate == "0" * 40:
        raise ValueError("candidate must be a nonzero full commit SHA")
    path = f"actions/workflows/ci.yml/runs?head_sha={candidate}&per_page=100"
    first = approved_run(read(path), candidate, branch)
    jobs = read(f"actions/runs/{first['id']}/attempts/1/jobs?per_page=100")
    advisory = validate_jobs(jobs, first)
    fresh = approved_run(
        {"total_count": 1, "workflow_runs": [read(f"actions/runs/{first['id']}")]},
        candidate,
        branch,
    )
    final = approved_run(read(path), candidate, branch)
    if fresh["id"] != first["id"] or final["id"] != first["id"]:
        raise ValueError("hosted first-run identity changed during observation")
    if validate_jobs(jobs, fresh) != advisory or validate_jobs(jobs, final) != advisory:
        raise ValueError("hosted advisory result changed during observation")
    return final, advisory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.candidate) or args.candidate == "0" * 40:
            raise ValueError("candidate must be a nonzero full commit SHA")
        run, advisory = read_ci_proof(args.candidate)
        print(
            f"Public candidate CI: PASS sha={args.candidate} "
            f"run={run['id']} attempt={run['run_attempt']} "
            f"windows-browser={advisory} (advisory)"
        )
        return 0
    except Exception as error:  # noqa: BLE001 - malformed/network results must remain content-free
        # Never echo exception text: HTTP errors and JSON input can contain sensitive values.
        print(
            f"Public candidate CI: FAIL ({type(error).__name__}); publication refused",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
