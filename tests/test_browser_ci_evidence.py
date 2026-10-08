"""Failure evidence must conserve outcomes without publishing raw reporter media or paths."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import browser_ci, ci_preflight

ROOT = Path(__file__).resolve().parents[1]
PIN = "cf430e030ddbb5b0abf93d22962f4752f3646cd9"  # pragma: allowlist secret - public action commit
PATHS = (
    "frontend/test-results/browser-ci/results.json\n"
    "frontend/test-results/browser-ci/test-results/**/error-context.md"
)


def raw_report() -> dict[str, Any]:
    return {
        "config": {"rootDir": "C:/private/project", "env": {"PRIVATE": "excluded"}},
        "suites": [
            {
                "title": "journeys/a.spec.ts",
                "file": "journeys/a.spec.ts",
                "specs": [
                    {
                        "file": "journeys/a.spec.ts",
                        "title": "edit budget",
                        "line": 10,
                        "tests": [
                            {
                                "expectedStatus": "passed",
                                "status": "unexpected",
                                "projectName": "",
                                "results": [
                                    {
                                        "status": "failed",
                                        "duration": 42,
                                        "retry": 0,
                                        "error": {
                                            "message": (
                                                "Expected: <= 16\nReceived: 19.5\n"
                                                " at C:\\private\\file.ts:10"
                                            ),
                                            "stack": "private stack",
                                        },
                                        "stdout": [{"text": "arbitrary fixture payload excluded"}],
                                        "attachments": [
                                            {
                                                "name": "capture",
                                                "contentType": "image/png",
                                                "body": "cHJpdmF0ZQ==",
                                            },
                                            {
                                                "name": "unknown",
                                                "contentType": "application/json",
                                                "body": '{"payload":"excluded"}',
                                            },
                                            {
                                                "name": "discrete-edit-budgets",
                                                "contentType": "application/json",
                                                "path": "C:/outside/raw.json",
                                            },
                                            {
                                                "name": "discrete-edit-budgets",
                                                "contentType": "application/json",
                                                "body": base64.b64encode(
                                                    json.dumps(
                                                        {
                                                            "commits": [3],
                                                            "renderP95": 19.5,
                                                            "handlerP95": 4,
                                                            "renderSamplesSortedMs": [1, 19.5],
                                                        }
                                                    ).encode()
                                                ).decode(),
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "errors": [],
        "stats": {"expected": 0, "unexpected": 1, "flaky": 0, "skipped": 0},
    }


def outcomes(value: dict[str, Any]) -> list[tuple[str, str, str, tuple[str, ...]]]:
    result = []

    def visit(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            for test in spec["tests"]:
                result.append(
                    (
                        spec["file"],
                        spec["title"],
                        test["expectedStatus"],
                        tuple(x["status"] for x in test.get("results", [])),
                    )
                )
        for child in suite.get("suites", []):
            visit(child)

    for suite in value["suites"]:
        visit(suite)
    return result


def test_manual_media_and_unknown_payloads_never_enter_sanitized_report() -> None:
    original = raw_report()
    frozen = copy.deepcopy(original)
    safe = browser_ci.sanitize_report(original)
    assert original == frozen
    assert outcomes(safe) == outcomes(original)
    serialized = json.dumps(safe)
    for forbidden in (
        "image/png",
        "cHJpdmF0ZQ",
        "private",
        "excluded",
        "C:/",
        "C:\\",
        "stack",
        "env",
    ):
        assert forbidden not in serialized
    assert "19.5" in serialized and "Expected: <= 16" in serialized
    attachments = safe["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"]
    assert len(attachments) == 1
    assert json.loads(base64.b64decode(attachments[0]["body"]))["renderP95"] == 19.5


@pytest.mark.parametrize(
    "location",
    [
        "/secret.txt",
        "/var/private/file.txt",
        "C:\\private folder\\file.ts",
        "\\\\private-host\\share\\file",
        "https://private.invalid/path",
    ],
)
def test_absolute_locations_in_failure_text_are_redacted(location: str) -> None:
    value = raw_report()
    attempt = value["suites"][0]["specs"][0]["tests"][0]["results"][0]
    attempt["error"]["message"] = "Expected: <= 16\n" + location
    safe = browser_ci.sanitize_report(value)
    message = safe["suites"][0]["specs"][0]["tests"][0]["results"][0]["error"]["message"]
    assert message == "Expected: <= 16\n[redacted location]"


@pytest.mark.parametrize(
    "damage",
    [
        "no-suites",
        "bad-results",
        "absolute-file",
        "uri-file",
        "file-uri",
        "malformed-budget",
        "nonfinite",
    ],
)
def test_malformed_reports_fail_closed(damage: str) -> None:
    value = raw_report()
    spec = value["suites"][0]["specs"][0]
    if damage == "no-suites":
        value.pop("suites")
    elif damage == "bad-results":
        spec["tests"][0]["results"] = "not a list"
    elif damage == "absolute-file":
        spec["file"] = "C:/outside/spec.ts"
    elif damage == "uri-file":
        spec["file"] = "https://private.invalid/spec.ts"
    elif damage == "file-uri":
        spec["file"] = "file:///private/spec.ts"
    elif damage == "malformed-budget":
        spec["tests"][0]["results"][0]["attachments"][-1]["body"] = "not-json"
    else:
        spec["tests"][0]["results"][0]["attachments"][-1]["body"] = base64.b64encode(
            b'{"renderP95":NaN}'
        ).decode()
    with pytest.raises(ValueError):
        browser_ci.sanitize_report(value)


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/outside",
        "C:/outside",
        "C:relative",
        "\\\\host\\share",
        ".tmp/private",
        "frontend/test-results/.hidden",
        "frontend/../test-results",
        "frontend/src",
    ],
)
def test_destination_refuses_unowned_hidden_and_escaping_paths(path: str) -> None:
    with pytest.raises(ValueError):
        browser_ci.evidence_directory(ROOT, path)


def test_reparse_ancestor_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.workspace_link_guard as guard

    parent = tmp_path / "frontend"
    parent.mkdir()
    monkeypatch.setattr(guard, "is_reparse_point", lambda path: path == parent)
    with pytest.raises(ValueError, match="link"):
        browser_ci.evidence_directory(tmp_path, "frontend/test-results/browser-ci")
    assert not (parent / "test-results").exists()


def test_publication_copies_only_sanitized_regular_context_files_and_json(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "results.json").write_text(json.dumps(raw_report()), encoding="utf-8")
    case = raw / "test-results/case"
    case.mkdir(parents=True)
    (case / "error-context.md").write_text(
        '# Snapshot\n- button "Open full editor"\n- generic "C:/outside/private.png"\n',
        encoding="utf-8",
    )
    (case / "screenshot.png").write_bytes(b"must not publish")
    destination = tmp_path / "safe"
    browser_ci.publish_evidence(raw / "results.json", raw / "test-results", destination)
    assert sorted(
        x.relative_to(destination).as_posix() for x in destination.rglob("*") if x.is_file()
    ) == ["results.json", "test-results/case/error-context.md"]
    assert "outside" not in (destination / "test-results/case/error-context.md").read_text()
    assert "Open full editor" in (destination / "test-results/case/error-context.md").read_text()
    with pytest.raises(ValueError, match="fresh"):
        browser_ci.publish_evidence(raw / "results.json", raw / "test-results", destination)


def test_interrupted_or_failed_sanitation_cannot_expose_raw_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    report = raw / "results.json"
    report.write_text(json.dumps(raw_report()), encoding="utf-8")
    destination = tmp_path / "safe"
    # Failure during sanitation precedes publication; never copy a raw report as a fallback.
    with pytest.raises(ValueError):
        report.write_text('{"suites": "broken", "body": "raw-media"}', encoding="utf-8")
        browser_ci.publish_evidence(report, raw / "test-results", destination)
    assert not (destination / "results.json").exists()
    assert not list(destination.glob("test-results/**/error-context.md"))


def test_interruption_before_atomic_publish_never_leaves_an_uploadable_raw_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    report = raw / "results.json"
    report.write_text(json.dumps(raw_report()), encoding="utf-8")
    destination = tmp_path / "safe"

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise OSError("interrupted before publish")

    monkeypatch.setattr(Path, "rename", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        browser_ci.publish_evidence(report, raw / "test-results", destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".browser-safe-*"))


def test_output_reparse_file_is_refused_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.workspace_link_guard as guard

    report = tmp_path / "results.json"
    report.write_text(json.dumps(raw_report()), encoding="utf-8")
    output = tmp_path / "test-results"
    output.mkdir()
    context = output / "error-context.md"
    context.write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(guard, "is_reparse_point", lambda path: path == context)
    destination = tmp_path / "safe"
    with pytest.raises(ValueError, match="link"):
        browser_ci.publish_evidence(report, output, destination)
    assert not destination.exists()


@pytest.mark.parametrize("retain", [False, True])
def test_driver_preserves_default_cleanup_and_retains_sanitized_failure_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retain: bool
) -> None:
    import subprocess
    import sys
    from collections import Counter

    native = Counter({("journeys/a.spec.ts", "native"): 1})
    portable = Counter({("journeys/a.spec.ts", "edit budget"): 1})
    inventories = iter((native + portable, portable))
    monkeypatch.setattr(browser_ci, "ROOT", tmp_path)
    monkeypatch.setattr(browser_ci, "require_interpreter", lambda root: Path(sys.executable))
    monkeypatch.setattr(browser_ci, "collect", lambda *args: next(inventories))
    monkeypatch.setattr(browser_ci, "native_cases", lambda root: native)
    monkeypatch.setattr(browser_ci, "validate_media_config", lambda root: None)
    destination = tmp_path / "retained"
    monkeypatch.setattr(browser_ci, "evidence_directory", lambda root, path: destination)
    monkeypatch.setattr(
        sys, "argv", ["browser_ci.py", *(["--evidence-dir", "retained"] if retain else [])]
    )
    reports = []

    def child(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if "--reporter=line,json" in args:
            path = Path(kwargs["env"]["PLAYWRIGHT_JSON_OUTPUT_FILE"])
            path.write_text(json.dumps(raw_report()), encoding="utf-8")
            reports.append(path)
            if retain:
                assert Path(kwargs["env"]["H3_CONTEXT_PLAYWRIGHT_OUTPUT"]).parent == path.parent
            return subprocess.CompletedProcess(args, 1)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", child)
    assert browser_ci.main() == 1  # Original child failure propagates, irrespective of retention.
    assert reports and not reports[0].exists()
    assert (destination / "results.json").exists() is retain
    if retain:
        safe = json.loads((destination / "results.json").read_text())
        assert outcomes(safe) == outcomes(raw_report())


def upload_workflow() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["browser"]["steps"]
    steps[:] = [x for x in steps if not x.get("uses", "").startswith("actions/upload-artifact@")]
    consumer = next(x for x in steps if "scripts/browser_ci.py" in x.get("run", ""))
    consumer["run"] = (
        "${{ matrix.python }} scripts/browser_ci.py --evidence-dir frontend/test-results/browser-ci"
    )
    steps.append(
        {
            "name": "Upload hermetic browser failure evidence",
            "if": "failure()",
            "uses": f"actions/upload-artifact@{PIN}",
            "with": {
                "name": "hermetic-browser-failure-${{ matrix.os }}",
                "path": PATHS,
                "if-no-files-found": "ignore",
                "retention-days": 7,
            },
        }
    )
    return workflow


def test_exact_failure_upload_preserves_unconditional_runtime_contract() -> None:
    ci_preflight.validate_workflow(upload_workflow())


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "before",
        "unconditional",
        "always",
        "extra-path",
        "retention90",
        "retention0",
        "retention-negative",
        "retention-bool",
        "retention-missing",
        "tag",
        "hidden",
        "soft-fail",
        "env",
        "unrelated-upload",
    ],
)
def test_failure_upload_contract_refuses_privacy_and_execution_mutations(damage: str) -> None:
    value = upload_workflow()
    steps = value["jobs"]["browser"]["steps"]
    upload = steps[-1]
    if damage == "missing":
        steps.pop()
    elif damage == "duplicate":
        steps.append(copy.deepcopy(upload))
    elif damage == "before":
        steps.insert(0, steps.pop())
    elif damage == "unconditional":
        upload.pop("if")
    elif damage == "always":
        upload["if"] = "always()"
    elif damage == "extra-path":
        upload["with"]["path"] += "\nfrontend/test-results/**"
    elif damage.startswith("retention"):
        if damage == "retention-missing":
            upload["with"].pop("retention-days")
        else:
            upload["with"]["retention-days"] = {
                "retention90": 90,
                "retention0": 0,
                "retention-negative": -1,
                "retention-bool": True,
            }[damage]
    elif damage == "tag":
        upload["uses"] = "actions/upload-artifact@v7"
    elif damage == "hidden":
        upload["with"]["include-hidden-files"] = True
    elif damage == "soft-fail":
        upload["continue-on-error"] = True
    elif damage == "env":
        upload["env"] = {"EXTRA": "override"}
    else:
        value["jobs"]["backend"]["steps"].append(copy.deepcopy(upload))
    with pytest.raises(ValueError):
        ci_preflight.validate_workflow(value)


def test_automatic_media_remains_disabled() -> None:
    from scripts import browser_ci_evidence

    browser_ci_evidence.validate_media_config(ROOT)


@pytest.mark.parametrize("kind", ["trace", "screenshot", "video"])
def test_media_configuration_mutation_is_refused(tmp_path: Path, kind: str) -> None:
    from scripts import browser_ci_evidence

    (tmp_path / "frontend").mkdir()
    text = (ROOT / "frontend/playwright.config.ts").read_text()
    (tmp_path / "frontend/playwright.config.ts").write_text(
        text.replace(f'{kind}: "off"', f'{kind}: "on"'), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        browser_ci_evidence.validate_media_config(tmp_path)
