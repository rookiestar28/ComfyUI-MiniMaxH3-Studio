"""Admit only sanitized synthetic failure facts to the public browser artifact."""

from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from scripts import workspace_link_guard

BUDGET_NAMES = {"integrated-discrete-edit-budgets", "discrete-edit-budgets"}
NUMERIC_FIELDS = {
    "setupCommits",
    "editCommits",
    "commits",
    "renderP95Ms",
    "renderP95",
    "handlerP95Ms",
    "handlerP95",
    "renderSamplesSortedMs",
    "renderMedianMs",
    "handlerSamplesSortedMs",
    "measuredIntervalMs",
    "durationMs",
    "elapsedMs",
    "bootstrapCompletedMs",
    "schemaCompletedMs",
    "pageErrors",
    "fixtureCallsOmitted",
}
STATUSES = {"passed", "failed", "timedOut", "skipped", "interrupted"}
LOCATION = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\|(?<![\w/])/[^\s/]+|https?://|file://)")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def require_owned_path(root: Path, path: Path) -> None:
    root = root.absolute()
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as error:
        raise ValueError("evidence path leaves workspace") from error
    # CRITICAL: is_symlink misses Windows junctions on Python 3.10. Check every lexical
    # ancestor before creating or reading it, or evidence writes can escape the workspace.
    current = root
    for part in ("", *relative.parts):
        current = current / part
        if workspace_link_guard.is_reparse_point(current):
            raise ValueError("evidence path contains a filesystem link")
    if path.resolve() != path.absolute():
        raise ValueError("evidence path is not an owned lexical path")


def evidence_directory(root: Path, relative: str) -> Path:
    parts = relative.replace("\\", "/").split("/")
    if (
        not relative
        or PureWindowsPath(relative).drive
        or PureWindowsPath(relative).root
        or PurePosixPath(relative).is_absolute()
        or any(not part or part.startswith(".") for part in parts)
        or any(":" in part for part in parts)
    ):
        raise ValueError("evidence destination must be a non-hidden relative directory")
    destination = root.absolute().joinpath(*parts)
    require_owned_path(root, destination)
    for filename in ("results.json", "test-results/error-context.md"):
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", str(destination / filename)],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=30,
        )
        if ignored.returncode != 0:
            raise ValueError("evidence destination must be Git-ignored")
    if destination.exists():
        raise ValueError("evidence destination must be fresh, without stale reports")
    return destination


def validate_media_config(root: Path) -> None:
    text = (root / "frontend/playwright.config.ts").read_text(encoding="utf-8")
    for kind in ("trace", "screenshot", "video"):
        values = re.findall(rf"\b{kind}\s*:\s*([\"'][^\"']*[\"'])", text)
        if values != ['"off"']:
            raise ValueError("automatic browser media retention must remain off")


def safe_text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2_000_000:
        raise ValueError("failure text is malformed or oversized")
    # Preserve synthetic accessibility/budget facts, never a path-bearing stack/URL line.
    # Whole-line redaction also covers paths containing spaces and non-ASCII components.
    lines = ANSI.sub("", value).splitlines()
    return "\n".join("[redacted location]" if LOCATION.search(line) else line for line in lines)


def numeric(value: Any) -> Any:
    if isinstance(value, list) and len(value) <= 100:
        return [numeric(x) for x in value]
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    raise ValueError("budget diagnostics must contain bounded finite numeric facts")


def diagnostic(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("budget diagnostics must be an object")
    result = {key: numeric(item) for key, item in value.items() if key in NUMERIC_FIELDS}
    if "shape" in value:
        if value["shape"] not in ("smoke", "virtualized"):
            raise ValueError("unknown budget fixture shape")
        result["shape"] = value["shape"]
    if "sampleCounts" in value:
        counts = value["sampleCounts"]
        if not isinstance(counts, dict) or set(counts) != {"renders", "handlers"}:
            raise ValueError("malformed budget sample counts")
        result["sampleCounts"] = {key: numeric(item) for key, item in counts.items()}
    for key in ("slowCommitPhases", "slowCommitStates"):
        if key in value:
            states = value[key]
            if (
                not isinstance(states, list)
                or len(states) > 3
                or not all(
                    isinstance(x, str) and len(x) <= 1024 and not LOCATION.search(x) for x in states
                )
            ):
                raise ValueError("malformed content-free commit facts")
            result[key] = states
    return result


def relative_label(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("browser location is missing or oversized")
    path = value.replace("\\", "/")
    # CRITICAL: URI schemes evade Windows drive checks; never retain them as relative labels.
    if (
        PureWindowsPath(value).drive
        or ":" in path
        or LOCATION.search(value)
        or path.startswith("/")
        or ".." in path.split("/")
    ):
        raise ValueError("browser location must be workspace-relative")
    return path


def sanitized_error(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("malformed browser error")
    result = {}
    for key in ("message", "value"):
        if key in value:
            result[key] = safe_text(value[key])
    return result


def sanitize_report(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict) or not isinstance(report.get("suites"), list):
        raise ValueError("malformed browser report suites")

    def suite(value: Any, depth: int = 0) -> dict[str, Any]:
        if not isinstance(value, dict) or depth > 20:
            raise ValueError("malformed browser report suite")
        result: dict[str, Any] = {"title": value["title"]}
        if not isinstance(value["title"], str) or LOCATION.search(value["title"]):
            raise ValueError("suite title contains a location")
        if "file" in value:
            result["file"] = relative_label(value["file"])
        for key in ("line", "column"):
            if key in value:
                result[key] = numeric(value[key])
        specs = value.get("specs", [])
        children = value.get("suites", [])
        if not isinstance(specs, list) or not isinstance(children, list):
            raise ValueError("malformed browser suite children")
        result["specs"] = [spec(x) for x in specs]
        result["suites"] = [suite(x, depth + 1) for x in children]
        return result

    def spec(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("tests"), list):
            raise ValueError("malformed browser spec")
        title = value["title"]
        if not isinstance(title, str) or LOCATION.search(title):
            raise ValueError("case title contains a location")
        result = {
            "title": title,
            "file": relative_label(value["file"]),
            "tests": [test(x) for x in value["tests"]],
        }
        for key in ("line", "column", "ok"):
            if key in value:
                result[key] = value[key] if key == "ok" else numeric(value[key])
        return result

    def test(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("expectedStatus") not in STATUSES:
            raise ValueError("malformed browser case status")
        attempts = value.get("results", [])
        if not isinstance(attempts, list):
            raise ValueError("malformed browser results")
        result = {
            "expectedStatus": value["expectedStatus"],
            "results": [attempt(x) for x in attempts],
        }
        if "status" in value:
            if value["status"] not in ("expected", "unexpected", "flaky", "skipped"):
                raise ValueError("malformed aggregate browser status")
            result["status"] = value["status"]
        return result

    def attempt(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("status") not in STATUSES:
            raise ValueError("malformed browser attempt status")
        result: dict[str, Any] = {"status": value["status"]}
        for key in ("duration", "retry", "workerIndex", "parallelIndex"):
            if key in value:
                result[key] = (
                    -1
                    if key in ("workerIndex", "parallelIndex") and value[key] == -1
                    else numeric(value[key])
                )
        if "error" in value:
            result["error"] = sanitized_error(value["error"])
        if "errors" in value:
            result["errors"] = [sanitized_error(x) for x in value["errors"]]
        result["attachments"] = []
        attachments = value.get("attachments", [])
        if not isinstance(attachments, list):
            raise ValueError("malformed attachment list")
        # CRITICAL: media-off settings do not remove manual PNG bodies from JSON. Never
        # retain arbitrary attachments, stdout, reporter config or raw attachment paths.
        for attachment in attachments:
            if not isinstance(attachment, dict):
                raise ValueError("malformed attachment")
            if (
                attachment.get("name") in BUDGET_NAMES
                and attachment.get("contentType") == "application/json"
                and "body" in attachment
                and "path" not in attachment
            ):
                # JSON reporter encodes attachment buffers as base64, including JSON.
                # Decode the actual wire form; never treat an arbitrary text body as facts.
                facts = diagnostic(json.loads(base64.b64decode(attachment["body"], validate=True)))
                body = base64.b64encode(json.dumps(facts, allow_nan=False).encode()).decode("ascii")
                result["attachments"].append(
                    {"name": attachment["name"], "contentType": "application/json", "body": body}
                )
        # Only our closed failure-stage JSON, not arbitrary browser console payloads.
        for output in value.get("stdout", []):
            text = output.get("text", "") if isinstance(output, dict) else ""
            if text.startswith('{"canonicalWorkspaceDiagnostics":'):
                payload = json.loads(text)["canonicalWorkspaceDiagnostics"]
                if not isinstance(payload, dict):
                    raise ValueError("malformed fixture diagnostics")
                facts = diagnostic(payload)
                facts["fixtureCalls"] = []
                for call in payload.get("fixtureCalls", []):
                    if len(facts["fixtureCalls"]) >= 64 or call.get("outcome") not in (
                        "ok",
                        "timeout",
                        "error",
                    ):
                        raise ValueError("malformed fixture call diagnostics")
                    facts["fixtureCalls"].append(
                        {"durationMs": numeric(call["durationMs"]), "outcome": call["outcome"]}
                    )
                if (
                    payload.get("surface")
                    not in ("collapsed", "opening", "expanded", "closing", "absent", "unknown")
                    or type(payload.get("launcherAttached")) is not bool
                ):
                    raise ValueError("malformed fixture surface diagnostics")
                facts.update(
                    surface=payload["surface"], launcherAttached=payload["launcherAttached"]
                )
                result.setdefault("stdout", []).append(
                    {"text": json.dumps({"canonicalWorkspaceDiagnostics": facts})}
                )
        return result

    safe: dict[str, Any] = {
        "suites": [suite(x) for x in report["suites"]],
        "errors": [sanitized_error(x) for x in report.get("errors", [])],
    }
    if "stats" in report:
        safe["stats"] = {
            key: numeric(report["stats"][key])
            for key in ("expected", "unexpected", "flaky", "skipped", "duration")
            if key in report["stats"]
        }
    return safe


def publish_evidence(report: Path, output: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("evidence destination must be fresh")
    require_owned_path(report.parent, report)
    if report.stat().st_size > 35_000_000:
        raise ValueError("browser report is oversized")
    safe = sanitize_report(json.loads(report.read_text(encoding="utf-8")))
    require_owned_path(report.parent, output)
    contexts: list[tuple[Path, str]] = []
    if output.exists():
        for directory, folders, files in os.walk(output, followlinks=False):
            for name in (*folders, *files):
                require_owned_path(output, Path(directory) / name)
            if "error-context.md" in files:
                path = Path(directory) / "error-context.md"
                if not stat.S_ISREG(path.stat().st_mode) or len(contexts) >= 1000:
                    raise ValueError("error-context must be a bounded regular file")
                contexts.append(
                    (path.relative_to(output), safe_text(path.read_text(encoding="utf-8")))
                )
    destination.parent.mkdir(parents=True, exist_ok=True)
    require_owned_path(destination.parent, destination)
    # Sanitize completely before publishing. If killed, the hidden staging directory is
    # outside both admitted upload globs and never contains a copy of raw reporter data.
    staging = Path(tempfile.mkdtemp(prefix=".browser-safe-", dir=destination.parent))
    try:
        (staging / "results.json").write_text(
            json.dumps(safe, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
        )
        for relative, text in contexts:
            target = staging / "test-results" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        if destination.exists():
            raise ValueError("evidence destination must remain fresh")
        require_owned_path(destination.parent, destination)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
