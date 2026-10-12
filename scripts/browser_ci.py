"""Run every portable browser case, retaining explicit native qualification separately."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.browser_ci_evidence import (  # noqa: E402 - direct script invocation needs ROOT first
    evidence_directory,
    publish_evidence,
    require_owned_path,
    sanitize_report,
    server_event,
    validate_media_config,
)

__all__ = ["evidence_directory", "publish_evidence", "sanitize_report"]

INVENTORY = "frontend/tests/fixtures/browserNativeCases.json"
PROFILE = "playwright.hermetic-ci.config.ts"
Case = tuple[str, str]


def collected(report: dict[str, Any], *, executed: bool = False) -> Counter[Case]:
    result: Counter[Case] = Counter()

    def visit(suite: dict[str, Any], parents: tuple[str, ...]) -> None:
        for spec in suite.get("specs", []):
            key = (spec["file"].replace("\\", "/"), " ".join((*parents, spec["title"])))
            for test in spec["tests"]:
                if test["expectedStatus"] != "passed":
                    raise ValueError("every portable case must be enabled and expected to pass")
                if executed:
                    attempts = test.get("results", [])
                    if len(attempts) != 1 or attempts[0].get("status") != "passed":
                        raise ValueError("browser cases must pass once without skips or retries")
                result[key] += 1
        for child in suite.get("suites", []):
            visit(child, (*parents, child["title"]))

    for suite in report["suites"]:
        visit(suite, ())
    if not result or any(count != 1 for count in result.values()):
        raise ValueError("browser inventory is empty or contains duplicate identities")
    if report.get("errors"):
        raise ValueError("browser report contains global errors")
    return result


def native_cases(root: Path) -> Counter[Case]:
    rows = json.loads((root / INVENTORY).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("native inventory must contain exact case identities")
    result: Counter[Case] = Counter()
    for row in rows:
        if set(row) != {"file", "title", "reason"} or not all(
            isinstance(value, str) and value.strip() == value and value for value in row.values()
        ):
            raise ValueError("native inventory needs exact file, title and prerequisite reason")
        result[(row["file"], row["title"])] += 1
    return result


def validate_partition(
    broad: Counter[Case], portable: Counter[Case], native: Counter[Case]
) -> None:
    if (
        not broad
        or not portable
        or not native
        or any(count != 1 for cases in (broad, portable, native) for count in cases.values())
    ):
        raise ValueError("browser partition contains empty or duplicate inventories")
    # IMPORTANT: fail on stale exclusions and same-title cross-file collisions; never compensate
    # for missing cases by lowering a count or silently dropping an unknown inventory row.
    if portable & native or portable + native != broad:
        raise ValueError("browser profiles do not conserve the exact broad case inventory")


def require_interpreter(root: Path) -> Path:
    from scripts.ci_preflight import fixture_python

    return fixture_python(root)


def command(config: str) -> list[str]:
    return [
        "pnpm.cmd" if sys.platform == "win32" else "pnpm",
        "--dir",
        "frontend",
        "exec",
        "playwright",
        "test",
        "--config",
        config,
    ]


def collect(config: str, env: dict[str, str], *, root: Path = ROOT) -> Counter[Case]:
    value = subprocess.run(
        [*command(config), "--list", "--reporter=json"],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=120,
    )
    return collected(json.loads(value.stdout))


def collection_environment(source: dict[str, str]) -> dict[str, str]:
    env = dict(source)
    # Collection must never encode native media as an import side effect. Native cases keep
    # their explicit commands and guards; the portable profile owns only the hermetic service.
    for name in (
        "H3_CONTEXT_AUTHORIZED_FFMPEG_PATH",
        "H3_CONTEXT_AUTHORIZED_FFPROBE_PATH",
        "H3_CONTEXT_E2E_SERVICE_LOOPBACK_MEDIA",
        "PLAYWRIGHT_JSON_OUTPUT_NAME",
        "PLAYWRIGHT_JSON_OUTPUT_FILE",
        "PLAYWRIGHT_JSON_OUTPUT_DIR",
    ):
        env.pop(name, None)
    env["H3_CONTEXT_E2E_SERVICE_LOOPBACK"] = "1"
    return env


def execute_browser(argv: list[str], env: dict[str, str]) -> tuple[int, list[dict[str, str]]]:
    # CRITICAL: redirected Windows stdout may use a legacy codec; Vite's Unicode
    # output must remain writable so its consumer does not abandon the pipe.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    events: list[dict[str, str]] = []
    with subprocess.Popen(
        argv,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as process:
        stdout = process.stdout
        if stdout is None:
            raise ValueError("browser stdout pipe is unavailable")
        reader_failed = False

        def consume() -> None:
            nonlocal reader_failed
            try:
                for line in stdout:
                    # CRITICAL: keep draining after a sink/parser failure; abandoning the
                    # pipe blocks the producer and masks the refusal with an 85-minute timeout.
                    if reader_failed:
                        continue
                    try:
                        print(line, end="", flush=True)
                        event = server_event(line)
                        if event is not None and len(events) < 512:
                            events.append(event)
                    except Exception:
                        reader_failed = True
            except Exception:
                reader_failed = True
                # An unreadable pipe cannot be drained. Stop its producer before waiting.
                try:
                    process.kill()
                except OSError:
                    pass

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        try:
            status = process.wait(timeout=85 * 60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            reader.join(timeout=10)
        if reader.is_alive():
            raise ValueError("browser stdout reader did not finish")
        # CRITICAL: a reader failure must refuse evidence, even when the child exits zero.
        # Never retain the exception text: it may carry a private server payload or path.
        if reader_failed:
            raise ValueError("browser stdout reader failed")
    return status, events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument(
        "--evidence-dir", help="retain sanitized failure evidence in a fresh ignored directory"
    )
    args = parser.parse_args()
    try:
        python = require_interpreter(ROOT)
        subprocess.run(
            [
                str(python),
                "-c",
                "import aiohttp; import scripts.m25_16_timeline_fixture; "
                "import scripts.m25_16_import_fixture; import scripts.m25_16_service_loopback",
            ],
            cwd=ROOT,
            check=True,
            timeout=120,
        )
        env = collection_environment(dict(os.environ))
        env["H3_CONTEXT_E2E_PYTHON"] = str(python)
        broad = collect("playwright.config.ts", env)
        portable = collect(PROFILE, env)
        native = native_cases(ROOT)
        validate_partition(broad, portable, native)
        print(
            f"Browser inventory: {sum(broad.values())} broad = "
            f"{sum(portable.values())} hermetic + {sum(native.values())} native NOT_RUN",
            flush=True,
        )
        if args.list_only:
            return 0
        destination = evidence_directory(ROOT, args.evidence_dir) if args.evidence_dir else None
        if destination is not None:
            validate_media_config(ROOT)
        scratch = ROOT / ".tmp"
        require_owned_path(ROOT, scratch)
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="browser-ci-", dir=scratch) as directory:
            report_path = Path(directory) / "results.json"
            env["PLAYWRIGHT_JSON_OUTPUT_FILE"] = str(report_path)
            if destination is not None:
                env["H3_CONTEXT_PLAYWRIGHT_OUTPUT"] = str(Path(directory) / "test-results")
            status, server_events = execute_browser(
                [*command(PROFILE), "--reporter=line,json"],
                env,
            )
            if report_path.exists():
                report = json.loads(report_path.read_text(encoding="utf-8"))
                report["serverEvents"] = server_events
                report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            if destination is not None and report_path.exists():
                require_owned_path(ROOT, destination)
                publish_evidence(report_path, Path(directory) / "test-results", destination)
            if status:
                return status
            executed = collected(json.loads(report_path.read_text(encoding="utf-8")), executed=True)
            if executed != portable:
                raise ValueError("executed cases differ from verified collection")
        print("Hermetic browser: PASS; explicit native qualification: NOT_RUN")
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(
            f"Hermetic browser preflight/result: FAIL ({type(error).__name__}: {error})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
