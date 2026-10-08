"""Require successful hosted execution of the exact public candidate before publication.

This is read-only: it never pushes, promotes, dispatches or publishes. CI must run from a
release-candidate/* push, whose checkout is the projection itself rather than a PR merge tree.
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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise ValueError("hosted API redirect refused")


def approved_run(payload: dict[str, Any], candidate: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or candidate == "0" * 40:
        raise ValueError("candidate must be a nonzero full commit SHA")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ValueError("missing hosted run inventory")
    matches = [
        row
        for row in runs
        if isinstance(row, dict)
        and row.get("head_sha") == candidate
        and isinstance(row.get("head_branch"), str)
        and row["head_branch"].startswith("release-candidate/")
    ]
    if not matches or any(type(row.get("id")) is not int for row in matches):
        raise ValueError("no exact candidate push run")
    # CRITICAL: an older green attempt must never override the newest candidate failure.
    run = max(matches, key=lambda row: row["id"])
    if (
        run.get("event") != "push"
        or run.get("path") != WORKFLOW_PATH
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] < 1
    ):
        raise ValueError("exact candidate CI is not a completed successful owned push")
    return run


def validate_jobs(payload: dict[str, Any], run: dict[str, Any]) -> None:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or payload.get("total_count") != len(jobs):
        raise ValueError("missing or truncated hosted jobs")
    if any(not isinstance(job, dict) for job in jobs):
        raise ValueError("invalid hosted job")
    if Counter(job.get("name") for job in jobs) != EXPECTED_JOBS:
        raise ValueError("hosted jobs do not conserve the required CI matrix")
    if any(
        job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or job.get("run_id") != run["id"]
        or job.get("head_sha") != run["head_sha"]
        for job in jobs
    ):
        raise ValueError("hosted job did not pass on the exact candidate")


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
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("invalid hosted response")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.candidate) or args.candidate == "0" * 40:
            raise ValueError("candidate must be a nonzero full commit SHA")
        runs = github_json(
            f"actions/workflows/ci.yml/runs?head_sha={args.candidate}&event=push&per_page=100"
        )
        run = approved_run(runs, args.candidate)
        jobs = github_json(
            f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
        )
        validate_jobs(jobs, run)
        print(
            f"Public candidate CI: PASS sha={args.candidate} "
            f"run={run['id']} attempt={run['run_attempt']}"
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
