"""Machine-readable mandatory-test, policy and public-ABI baseline tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from scripts import acceptance_baseline as baseline

ROOT = Path(__file__).resolve().parents[1]


class AcceptanceBaselineTests(TestCase):
    def _root(self, temporary: str) -> Path:
        root = Path(temporary)
        (root / "tests").mkdir()
        (root / "frontend" / "tests").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "tests" / "sample_test.py").write_text(
            "def test_required_transition():\n    pass\n", encoding="utf-8"
        )
        (root / "frontend" / "tests" / "sample.test.ts").write_text(
            'it("required lifecycle", () => {});\n', encoding="utf-8"
        )
        return root

    def _payload(self) -> dict[str, object]:
        return {
            "criteria": [
                {
                    "id": "sample.python-transition",
                    "path": "tests/sample_test.py",
                    "runner": "pytest",
                    "selector": "test_required_transition",
                },
                {
                    "id": "sample.frontend-lifecycle",
                    "path": "frontend/tests/sample.test.ts",
                    "runner": "vitest",
                    "selector": "required lifecycle",
                },
            ],
            "policies": [
                {
                    "consumer": "sample_module:LIMIT",
                    "direction": "maximum",
                    "id": "sample.maximum",
                    "value": 10,
                }
            ],
            "public_python_abi": [],
            "schema": "h3-context-acceptance-baseline/1",
        }

    def _write(self, root: Path, payload: object) -> Path:
        path = root / "tests" / "acceptance_baseline.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_repository_baseline_is_valid_and_registered_selectors_are_unique(self) -> None:
        report = baseline.validate_repository_baseline(ROOT)
        self.assertEqual(report["schema"], "h3-context-acceptance-baseline-report/1")
        self.assertEqual(report["status"], "PASS")
        self.assertGreaterEqual(report["criteria"], 7)
        self.assertGreaterEqual(report["public_python_abi"], 10)
        self.assertEqual(report["policies"], 0)

    def test_missing_or_ambiguous_mandatory_test_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            payload = self._payload()
            manifest = self._write(root, payload)
            with (
                mock.patch.object(baseline, "validate_public_abi", return_value=0),
                mock.patch.object(baseline, "_validate_policies", return_value=1),
            ):
                baseline.validate_baseline(manifest, root=root)
                (root / "tests" / "sample_test.py").write_text("", encoding="utf-8")
                with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "selector"):
                    baseline.validate_baseline(manifest, root=root)
                (root / "tests" / "sample_test.py").write_text(
                    "def test_required_transition():\n    pass\n"
                    "def test_required_transition():\n    pass\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "unique"):
                    baseline.validate_baseline(manifest, root=root)

    def test_vitest_comments_and_non_literal_titles_do_not_satisfy_a_criterion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            manifest = self._write(root, self._payload())
            test_path = root / "frontend" / "tests" / "sample.test.ts"
            test_path.write_text(
                '// it("required lifecycle", () => {});\n'
                'const title = "required lifecycle";\n'
                "it(title, () => {});\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(baseline, "validate_public_abi", return_value=0),
                mock.patch.object(baseline, "_validate_policies", return_value=1),
                self.assertRaisesRegex(baseline.AcceptanceBaselineError, "selector"),
            ):
                baseline.validate_baseline(manifest, root=root)

    def test_strict_closed_manifest_rejects_duplicate_unknown_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            manifest = root / "tests" / "acceptance_baseline.json"
            manifest.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
            with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "duplicate"):
                baseline.validate_baseline(manifest, root=root)

            payload = self._payload()
            payload["unknown"] = True
            self._write(root, payload)
            with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "members"):
                baseline.validate_baseline(manifest, root=root)

            payload.pop("unknown")
            payload["criteria"][0]["path"] = "../outside.py"  # type: ignore[index]
            self._write(root, payload)
            with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "path"):
                baseline.validate_baseline(manifest, root=root)

        with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "byte bound"):
            baseline.decode_baseline(b"{" + b" " * baseline.MAX_BASELINE_BYTES + b"}")

    def test_registered_test_link_is_rejected_when_the_platform_can_create_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            target = root / "tests" / "target.py"
            target.write_text("def test_required_transition():\n    pass\n", encoding="utf-8")
            link = root / "tests" / "linked.py"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("file symlink creation is unavailable")
            payload = self._payload()
            payload["criteria"][0]["path"] = "tests/linked.py"  # type: ignore[index]
            manifest = self._write(root, payload)
            with (
                mock.patch.object(baseline, "validate_public_abi", return_value=0),
                mock.patch.object(baseline, "_validate_policies", return_value=1),
                self.assertRaisesRegex(baseline.AcceptanceBaselineError, "link|reparse"),
            ):
                baseline.validate_baseline(manifest, root=root)

    def test_public_abi_parameter_order_kind_and_default_presence_are_exact(self) -> None:
        expected = baseline.describe_public_callable("GenerationSequencePlan")
        baseline.validate_public_abi([expected])
        changed = json.loads(json.dumps(expected))
        changed["parameters"][8], changed["parameters"][9] = (
            changed["parameters"][9],
            changed["parameters"][8],
        )
        with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "ABI"):
            baseline.validate_public_abi([changed])
        changed = json.loads(json.dumps(expected))
        changed["parameters"][8]["has_default"] = False
        with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "ABI"):
            baseline.validate_public_abi([changed])

    def test_policy_comparison_detects_relaxation_but_not_tightening(self) -> None:
        old = self._payload()
        current = json.loads(json.dumps(old))
        current["policies"][0]["value"] = 11
        self.assertEqual(
            baseline.compare_policy_values(old, current),
            ("sample.maximum",),
        )
        current["policies"][0]["value"] = 9
        self.assertEqual(baseline.compare_policy_values(old, current), ())
        current["policies"][0]["direction"] = "minimum"
        with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "identity"):
            baseline.compare_policy_values(old, current)
        current["policies"] = []
        with self.assertRaisesRegex(baseline.AcceptanceBaselineError, "deleted"):
            baseline.compare_policy_values(old, current)

    def test_git_base_comparison_reads_committed_policy_without_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            payload = self._payload()
            manifest = self._write(root, payload)
            commands = (
                ["git", "init", "-q"],
                ["git", "add", "tests/acceptance_baseline.json"],
                [
                    "git",
                    "-c",
                    "user.name=Acceptance Test",
                    "-c",
                    "user.email=acceptance@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "baseline",
                ],
            )
            for command in commands:
                subprocess.run(command, cwd=root, check=True, capture_output=True)
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            payload["policies"][0]["value"] = 11  # type: ignore[index]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(baseline.compare_git_base(revision, root=root), ("sample.maximum",))
            payload["policies"][0]["value"] = 9  # type: ignore[index]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(baseline.compare_git_base(revision, root=root), ())

    def test_cli_validate_passes_and_invalid_base_revision_fails_closed(self) -> None:
        valid = subprocess.run(
            [sys.executable, "scripts/acceptance_baseline.py", "validate"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        invalid = subprocess.run(
            [
                sys.executable,
                "scripts/acceptance_baseline.py",
                "compare",
                "--base-ref",
                "not-a-revision",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertNotIn(str(ROOT), invalid.stdout + invalid.stderr)


if __name__ == "__main__":
    import unittest

    unittest.main()
