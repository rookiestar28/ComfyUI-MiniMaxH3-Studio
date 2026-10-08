"""The hosted profile must conserve cases and fail before missing fixture startup."""

from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from scripts import browser_ci


def test_collection_environment_keeps_interpreter_but_removes_native_authority() -> None:
    original = {
        "H3_CONTEXT_E2E_PYTHON": "owned-interpreter",
        "H3_CONTEXT_AUTHORIZED_FFMPEG_PATH": "native",
        "H3_CONTEXT_AUTHORIZED_FFPROBE_PATH": "native",
        "H3_CONTEXT_E2E_SERVICE_LOOPBACK_MEDIA": "native",
        "PLAYWRIGHT_JSON_OUTPUT_FILE": "foreign-output",
    }
    selected = browser_ci.collection_environment(original)
    assert selected == {
        "H3_CONTEXT_E2E_PYTHON": "owned-interpreter",
        "H3_CONTEXT_E2E_SERVICE_LOOPBACK": "1",
    }
    assert original["H3_CONTEXT_AUTHORIZED_FFMPEG_PATH"] == "native"


def report(title: str = "portable", *, expected: str = "passed") -> dict[str, Any]:
    return {
        "suites": [
            {
                "title": "journeys/a.spec.ts",
                "specs": [
                    {
                        "file": "journeys/a.spec.ts",
                        "title": title,
                        "tests": [{"expectedStatus": expected}],
                    }
                ],
            }
        ]
    }


class BrowserCiTests(unittest.TestCase):
    def test_case_identity_keeps_describe_and_file(self) -> None:
        value = report()
        child = value["suites"][0]
        value = {
            "suites": [{"title": "journeys/a.spec.ts", "suites": [{**child, "title": "nested"}]}]
        }
        self.assertEqual(
            browser_ci.collected(value), Counter({("journeys/a.spec.ts", "nested portable"): 1})
        )

    def test_expected_skip_is_not_an_accepted_collection(self) -> None:
        with self.assertRaisesRegex(ValueError, "enabled"):
            browser_ci.collected(report(expected="skipped"))

    def test_execution_rejects_runtime_skips_retries_and_global_errors(self) -> None:
        for attempts in ([], [{"status": "skipped"}], [{"status": "passed"}] * 2):
            value = report()
            value["suites"][0]["specs"][0]["tests"][0]["results"] = attempts
            with self.assertRaisesRegex(ValueError, "without skips or retries"):
                browser_ci.collected(value, executed=True)
        value = report()
        value["errors"] = [{"message": "web server failed"}]
        with self.assertRaisesRegex(ValueError, "global errors"):
            browser_ci.collected(value)

    def test_execution_accepts_one_actual_pass(self) -> None:
        value = report()
        value["suites"][0]["specs"][0]["tests"][0]["results"] = [{"status": "passed"}]
        self.assertEqual(browser_ci.collected(value, executed=True), browser_ci.collected(value))

    def test_partition_rejects_unknown_and_duplicate_native_rows(self) -> None:
        portable = Counter({("a", "portable"): 1})
        native = Counter({("a", "native"): 1})
        browser_ci.validate_partition(portable + native, portable, native)
        for bad in (Counter({("a", "unknown"): 1}), native + native):
            with self.assertRaises(ValueError):
                browser_ci.validate_partition(portable + native, portable, bad)

    def test_partition_rejects_lost_capable_case_and_cross_file_title_collision(self) -> None:
        native = Counter({("a", "same title"): 1})
        portable = Counter({("b", "same title"): 1, ("b", "retained"): 1})
        with self.assertRaisesRegex(ValueError, "conserve"):
            browser_ci.validate_partition(
                portable + native, Counter({("b", "retained"): 1}), native
            )

    def test_partition_rejects_native_in_portable(self) -> None:
        native = Counter({("a", "native"): 1})
        with self.assertRaises(ValueError):
            browser_ci.validate_partition(native, native, native)

    def test_missing_local_interpreter_fails_before_collection(self) -> None:
        scratch = Path(__file__).resolve().parents[1] / ".tmp"
        scratch.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="browser-ci-test-", dir=scratch) as directory:
            with self.assertRaisesRegex(ValueError, "local Python"):
                browser_ci.require_interpreter(Path(directory))

    def test_workflow_prepares_interpreter_used_by_browser_driver(self) -> None:
        import yaml

        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
        job = workflow["jobs"]["browser"]
        rows = job["strategy"]["matrix"]["include"]
        self.assertEqual(
            {row["python"] for row in rows}, {".venv/bin/python", ".venv/Scripts/python.exe"}
        )
        steps = job["steps"]
        setup = next(
            i for i, step in enumerate(steps) if "actions/setup-python@" in step.get("uses", "")
        )
        prepare = next(i for i, step in enumerate(steps) if "-m venv .venv" in step.get("run", ""))
        run = next(
            i for i, step in enumerate(steps) if "scripts/browser_ci.py" in step.get("run", "")
        )
        self.assertLess(setup, prepare)
        self.assertLess(prepare, run)
        self.assertIn("${{ matrix.python }} -m pip install", steps[prepare]["run"])
        self.assertIn('".[dev,host-tests]"', steps[prepare]["run"])
        self.assertIn("${{ matrix.python }} scripts/browser_ci.py", steps[run]["run"])


if __name__ == "__main__":
    unittest.main()
