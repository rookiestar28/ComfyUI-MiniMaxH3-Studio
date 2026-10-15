"""Failure evidence must conserve outcomes without publishing raw reporter media or paths."""

from __future__ import annotations

import base64
import copy
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import browser_ci, ci_preflight

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failure", ["codec", "sink", "parser", "pipe"])
def test_native_output_larger_than_pipe_drains_after_forwarding_failure(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[subprocess.Popen[str]] = []

    class BoundedProcess(subprocess.Popen[str]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if failure == "pipe":
                kwargs.update(encoding="ascii", errors="strict")
            super().__init__(*args, **kwargs)
            processes.append(self)

        def wait(self, timeout: float | None = None) -> int:
            # Bound only this regression's watchdog; keep the driver's real budget intact.
            return super().wait(timeout=3 if timeout == 85 * 60 else timeout)

    def fail_forwarding(*args: Any, **kwargs: Any) -> None:
        raise ValueError("private-forwarding-payload")

    target = io.BytesIO()
    sink = io.TextIOWrapper(target, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", sink)
    monkeypatch.setattr(subprocess, "Popen", BoundedProcess)
    if failure == "sink":
        monkeypatch.setattr(browser_ci, "print", fail_forwarding, raising=False)
    if failure == "parser":
        monkeypatch.setattr(browser_ci, "server_event", fail_forwarding)
    child = (
        "import sys; sys.stdout.reconfigure(encoding='utf-8'); "
        "print('\u279c', flush=True); "
        "sys.stdout.write(('x'*8192+'\\n')*256); sys.stdout.flush()"
    )
    try:
        if failure == "codec":
            status, events = browser_ci.execute_browser(
                [sys.executable, "-c", child], dict(os.environ)
            )
            assert status == 0 and events == []
            sink.flush()
            assert target.getvalue().decode("utf-8").startswith("\u279c")
            assert len(target.getvalue()) > 2 * 1024 * 1024
        else:
            with pytest.raises(ValueError, match="^browser stdout reader failed$"):
                browser_ci.execute_browser([sys.executable, "-c", child], dict(os.environ))
        assert len(processes) == 1 and processes[0].poll() is not None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_stream_reader_failure_refuses_zero_exit_without_exception_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import os
    import sys

    def fail_reader(line: str) -> None:
        raise ValueError("private-reader-payload")

    monkeypatch.setattr(browser_ci, "server_event", fail_reader)
    with pytest.raises(ValueError, match="^browser stdout reader failed$"):
        browser_ci.execute_browser(
            [sys.executable, "-c", "print('closed input')"], dict(os.environ)
        )
    assert "private-reader-payload" not in str(capsys.readouterr())


def test_boot_attachment_retains_only_closed_counts_and_module_labels() -> None:
    report = raw_report()
    facts = {
        "phase": "html",
        "pageErrors": 1,
        "requestFailures": 0,
        "mainFrameNavigations": 1,
        "crashes": 0,
        "responses": [{"module": "nleShell.tsx", "status": 404}],
        "events": [{"kind": "window-error", "name": "TypeError", "module": "src/entry.tsx"}],
    }
    attachment = {
        "name": "harness-boot-diagnostics",
        "contentType": "application/json",
        "body": base64.b64encode(json.dumps(facts).encode()).decode(),
    }
    report["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"] = [attachment]
    safe = browser_ci.sanitize_report(report)
    retained = safe["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"]
    assert len(retained) == 1
    assert json.loads(base64.b64decode(retained[0]["body"])) == facts


@pytest.mark.parametrize(
    "bad",
    [
        {"message": "private"},
        {"events": [{"kind": "window-error", "name": "private", "module": None}]},
        {"responses": [{"module": "src/../private.ts", "status": 404}]},
        {"responses": [{"module": "src/entry.tsx?token=private", "status": 404}]},
        {"pageErrors": True},
        {"crashes": -1},
    ],
)
def test_boot_diagnostics_refuse_nonclosed_payloads(bad: dict[str, Any]) -> None:
    from scripts.browser_ci_evidence import boot_diagnostic

    facts = dict(
        phase="html",
        pageErrors=0,
        requestFailures=0,
        mainFrameNavigations=1,
        crashes=0,
        responses=[],
        events=[],
    )
    facts.update(bad)
    with pytest.raises(ValueError):
        boot_diagnostic(facts)


def test_vite_events_keep_kind_and_time_without_payload() -> None:
    from scripts.browser_ci_evidence import server_event

    for text, kind in [
        (
            "[WebServer] 15:40:01 [vite] new dependencies optimized: secret",
            "dependency-optimization",
        ),
        ("[WebServer] [vite] page reload C:/private/file.ts?token=secret", "page-reload"),
        ("[WebServer] [vite] hmr update /src/private.tsx", "hmr-update"),
        ("[WebServer] [vite] server restarted.", "server-restart"),
    ]:
        event = server_event(text)
        assert event is not None and event["kind"] == kind
        assert set(event) == {"kind", "time"}
        assert "secret" not in json.dumps(event) and "private" not in json.dumps(event)
    assert server_event("[WebServer] arbitrary payload page reload") is None
    assert server_event("fixture page reload secret") is None


def test_actual_stdout_reader_preserves_child_exit_and_closed_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import os
    import sys

    monkeypatch.setattr(browser_ci, "ROOT", tmp_path)
    status, events = browser_ci.execute_browser(
        [
            sys.executable,
            "-c",
            'print("[WebServer] [vite] page reload C:/private/file.ts?token=private"); '
            "raise SystemExit(7)",
        ],
        dict(os.environ),
    )
    assert status == 7 and len(events) == 1
    assert events[0]["kind"] == "page-reload"
    assert "private" not in json.dumps(events)
    assert "page reload" in capsys.readouterr().out
    report = raw_report()
    report["serverEvents"] = events
    assert browser_ci.sanitize_report(report)["serverEvents"] == events


@pytest.mark.parametrize(
    "row",
    [
        {"kind": "page-reload", "time": "2026-10-11T00:00:00.1+00:00", "path": "private"},
        {"kind": "unknown", "time": "2026-10-11T00:00:00.1+00:00"},
        {"kind": "page-reload", "time": "private"},
    ],
)
def test_server_event_artifact_refuses_unknown_fields_or_values(row: dict[str, Any]) -> None:
    report = raw_report()
    report["serverEvents"] = [row]
    with pytest.raises(ValueError):
        browser_ci.sanitize_report(report)


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


def presentation_report() -> dict[str, Any]:
    value = raw_report()
    attempt = value["suites"][0]["specs"][0]["tests"][0]["results"][0]
    attempt["attachments"] = [
        {
            "name": "integrated-presentation-timeline",
            "contentType": "application/json",
            "body": base64.b64encode(
                json.dumps(
                    {
                        "version": 1,
                        "samplesDropped": 0,
                        "samples": [
                            {
                                "offsetMs": index * 20,
                                "deliveredMs": duration,
                                "paintMs": 1,
                                "phase": "playback" if index != 2 else "edit",
                                "path": "native",
                            }
                            for index, duration in enumerate([18, 21, 1066.1, 22, 19, 24])
                        ],
                    }
                ).encode()
            ).decode(),
        }
    ]
    return value


def test_presentation_timeline_retains_maximum_and_neighbours_without_raw_payload() -> None:
    report = presentation_report()
    safe = browser_ci.sanitize_report(report)
    attachments = safe["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"]
    assert len(attachments) == 1
    facts = json.loads(base64.b64decode(attachments[0]["body"]))
    assert len(facts["samples"]) == 6
    assert facts["maximum"] == facts["samples"][2]
    assert facts["maximum"]["phase"] == "edit"
    assert facts["maximum"]["offsetMs"] == 40
    assert [row["deliveredMs"] for row in facts["topFive"]] == [1066.1, 24, 22, 21, 19]
    assert "private" not in json.dumps(safe)


@pytest.mark.parametrize(
    "damage",
    [
        "content",
        "private-path",
        "secret",
        "phase",
        "path",
        "boolean",
        "nan",
        "negative",
        "oversized",
        "empty",
        "dropped",
        "version",
        "unordered",
    ],
)
def test_presentation_timeline_refuses_non_closed_or_incomplete_facts(damage: str) -> None:
    report = presentation_report()
    attachment = report["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"][0]
    facts = json.loads(base64.b64decode(attachment["body"]))
    row = facts["samples"][0]
    if damage == "content":
        row["prompt"] = "private source content"
    elif damage == "private-path":
        row["path"] = "C:/private/source.mp4"
    elif damage == "secret":
        facts["token"] = "synthetic-secret-content"  # noqa: S105 - content-free refusal fixture
    elif damage == "phase":
        row["phase"] = "private user input"
    elif damage == "path":
        row["path"] = "https://private.invalid/video"
    elif damage == "boolean":
        row["offsetMs"] = True
    elif damage == "nan":
        row["deliveredMs"] = float("nan")
    elif damage == "negative":
        row["paintMs"] = -1
    elif damage == "oversized":
        facts["samples"] = [row] * 20_001
    elif damage == "empty":
        facts["samples"] = []
    elif damage == "dropped":
        facts["samplesDropped"] = 1
    elif damage == "version":
        facts["version"] = True
    else:
        facts["samples"][1]["offsetMs"] = 0
        facts["samples"][0]["offsetMs"] = 1
    attachment["body"] = base64.b64encode(json.dumps(facts).encode()).decode()
    with pytest.raises(ValueError, match="presentation"):
        browser_ci.sanitize_report(report)


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
            path.write_text(json.dumps(presentation_report()), encoding="utf-8")
            reports.append(path)
            if retain:
                assert Path(kwargs["env"]["H3_CONTEXT_PLAYWRIGHT_OUTPUT"]).parent == path.parent
            return subprocess.CompletedProcess(args, 1)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", child)
    monkeypatch.setattr(
        browser_ci, "execute_browser", lambda args, env: (child(args, env=env).returncode, [])
    )
    assert browser_ci.main() == 1  # Original child failure propagates, irrespective of retention.
    assert reports and not reports[0].exists()
    assert (destination / "results.json").exists() is retain
    if retain:
        safe = json.loads((destination / "results.json").read_text())
        assert outcomes(safe) == outcomes(presentation_report())
        attachment = safe["suites"][0]["specs"][0]["tests"][0]["results"][0]["attachments"][0]
        facts = json.loads(base64.b64decode(attachment["body"]))
        assert facts["maximum"]["deliveredMs"] == 1066.1
        assert len(facts["samples"]) == 6


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
