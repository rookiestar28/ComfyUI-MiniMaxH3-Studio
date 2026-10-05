"""Fail-closed version gate for the Comfy Registry publication workflow."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the pinned Python 3.10 publication environment
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
SEMVER = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)$"
)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class PublishGuardError(ValueError):
    """Raised when publication eligibility cannot be proven safely."""


def _table(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PublishGuardError(f"{field} must be a TOML table")
    return value


def parse_project_version(data: bytes, source: str) -> tuple[str, tuple[int, int, int]]:
    """Parse one strict three-part project version without importing project code."""

    try:
        document = tomllib.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PublishGuardError(f"cannot parse {source}: {exc}") from exc
    project = _table(document.get("project"), f"{source} [project]")
    version = project.get("version")
    if not isinstance(version, str):
        raise PublishGuardError(f"{source} project.version must be a string")
    match = SEMVER.fullmatch(version)
    if match is None:
        raise PublishGuardError(f"{source} project.version must be X.Y.Z")
    parts = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    return version, (parts[0], parts[1], parts[2])


def read_current_version(path: Path | None = None) -> tuple[str, tuple[int, int, int]]:
    target = PYPROJECT if path is None else path
    try:
        return parse_project_version(target.read_bytes(), target.as_posix())
    except OSError as exc:
        raise PublishGuardError(f"cannot read {target.as_posix()}: {exc}") from exc


def read_previous_version_from_git(ref: str) -> tuple[str, tuple[int, int, int]] | None:
    if FULL_SHA.fullmatch(ref) is None or ref == "0" * 40:
        raise PublishGuardError("previous ref must be one non-zero full Git SHA")
    try:
        commit_probe = subprocess.run(
            ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishGuardError("reading the previous Git commit timed out") from exc
    if commit_probe.returncode != 0:
        raise PublishGuardError("cannot read the previous Git commit")

    try:
        manifest_probe = subprocess.run(
            ["git", "ls-tree", "-z", "--name-only", ref, "--", "pyproject.toml"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishGuardError("probing the previous pyproject.toml timed out") from exc
    # IMPORTANT: a proven commit without this path is the initial-publication state; every other
    # Git lookup failure remains an error rather than silently authorizing a release.
    if manifest_probe.returncode != 0:
        raise PublishGuardError("cannot determine whether the previous pyproject.toml exists")
    if manifest_probe.stdout == b"":
        return None
    if manifest_probe.stdout != b"pyproject.toml\0":
        raise PublishGuardError("previous pyproject.toml lookup returned an unexpected path")

    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:pyproject.toml"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishGuardError("reading the previous pyproject.toml timed out") from exc
    if result.returncode != 0:
        raise PublishGuardError("cannot read pyproject.toml from the previous Git SHA")
    return parse_project_version(result.stdout, "previous pyproject.toml")


def read_checked_out_commit(root: Path = ROOT) -> str:
    """Resolve the exact checked-out commit without accepting a symbolic caller ref."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishGuardError("cannot resolve the checked-out candidate commit") from exc
    try:
        commit = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise PublishGuardError("checked-out candidate identity is invalid") from exc
    if result.returncode != 0 or FULL_SHA.fullmatch(commit) is None or commit == "0" * 40:
        raise PublishGuardError("cannot resolve the checked-out candidate commit")
    return commit


def validate_candidate_identity(current_ref: str, expected_candidate: str) -> str:
    """Bind event identity, explicit expectation and actual HEAD to one commit."""

    # CRITICAL: never pass either untrusted identity back to Git; only the fixed HEAD probe runs.
    if FULL_SHA.fullmatch(current_ref) is None or current_ref == "0" * 40:
        raise PublishGuardError("current ref must be one non-zero full Git SHA")
    if FULL_SHA.fullmatch(expected_candidate) is None or expected_candidate == "0" * 40:
        raise PublishGuardError("expected candidate must be one non-zero full Git SHA")
    if current_ref != expected_candidate:
        raise PublishGuardError("expected candidate does not match the event candidate")
    checked_out = read_checked_out_commit(ROOT)
    if checked_out != current_ref:
        raise PublishGuardError("checked-out HEAD does not match the event candidate")
    return checked_out


def decide_publish(
    current: tuple[str, tuple[int, int, int]],
    previous: tuple[str, tuple[int, int, int]] | None,
) -> tuple[bool, str]:
    if previous is None:
        return True, "initial_version"
    current_text, current_parts = current
    previous_text, previous_parts = previous
    if current_parts == previous_parts:
        return False, "version_unchanged"
    if current_parts < previous_parts:
        raise PublishGuardError(
            f"project.version downgrade is not publishable: {previous_text} -> {current_text}"
        )
    return True, "version_increased"


def validate_event_expectations(
    event_name: str,
    *,
    expected_version: str | None,
    current_version: str,
    should_publish: bool,
) -> None:
    if event_name == "push":
        if expected_version is not None:
            raise PublishGuardError("push cannot accept a manual expected version")
        return
    if event_name != "workflow_dispatch":
        raise PublishGuardError("event name must be push or workflow_dispatch")
    if expected_version is None or SEMVER.fullmatch(expected_version) is None:
        raise PublishGuardError("manual expected version must be X.Y.Z")
    if expected_version != current_version:
        raise PublishGuardError("manual expected version does not match project.version")
    if not should_publish:
        raise PublishGuardError("manual dispatch cannot publish an unchanged version")


def write_outputs(
    path: Path,
    *,
    should_publish: bool,
    reason: str,
    version: str,
    candidate_commit: str,
) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"should_publish={'true' if should_publish else 'false'}\n")
        stream.write(f"reason={reason}\n")
        stream.write(f"current_version={version}\n")
        stream.write(f"candidate_commit={candidate_commit}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--previous-ref", required=True)
    parser.add_argument("--current-ref", required=True)
    parser.add_argument("--expected-candidate", required=True)
    parser.add_argument("--expected-version", default="")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        candidate_commit = validate_candidate_identity(
            args.current_ref,
            args.expected_candidate,
        )
        current = read_current_version()
        previous = read_previous_version_from_git(args.previous_ref)
        should_publish, reason = decide_publish(current, previous)
        validate_event_expectations(
            args.event_name,
            expected_version=args.expected_version or None,
            current_version=current[0],
            should_publish=should_publish,
        )
        write_outputs(
            args.github_output,
            should_publish=should_publish,
            reason=reason,
            version=current[0],
            candidate_commit=candidate_commit,
        )
    except (OSError, PublishGuardError) as exc:
        print(f"Registry publish guard: FAIL: {exc}")
        return 1
    print(
        "Registry publish guard: PASS "
        f"(should_publish={should_publish}; reason={reason}; version={current[0]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
