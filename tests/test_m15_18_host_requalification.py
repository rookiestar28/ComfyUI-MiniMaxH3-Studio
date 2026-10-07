"""M15-18 exact source and profile orchestration regressions."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.m15_18_host_requalification import (
    CoInstallSource,
    export_pinned_source,
    parse_coinstall_source,
)


class M1518HostRequalificationTests(unittest.TestCase):
    def test_parse_coinstall_source_is_closed_and_exact(self) -> None:
        value = parse_coinstall_source("openclaw=reference/ComfyUI-OpenClaw@" + "a" * 40)
        self.assertEqual(
            value,
            CoInstallSource(
                label="openclaw",
                source=Path("reference/ComfyUI-OpenClaw"),
                revision="a" * 40,
            ),
        )
        for invalid in (
            "unknown=reference/repo@" + "a" * 40,
            "openclaw=reference/repo@short",
            "openclaw=../outside@" + "a" * 40,
            "openclaw=reference/repo@" + "A" * 40,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_coinstall_source(invalid)

    def test_export_uses_exact_commit_not_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp") as raw:
            root = Path(raw)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            (source / "value.txt").write_text("committed\n", encoding="utf-8")
            subprocess.run(["git", "add", "value.txt"], cwd=source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=M15-18 Test",
                    "-c",
                    "user.email=m15-18@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                cwd=source,
                check=True,
            )
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source, text=True
            ).strip()
            (source / "value.txt").write_text("dirty\n", encoding="utf-8")

            evidence = export_pinned_source(CoInstallSource("doctor", source, revision), target)

            self.assertEqual((target / "value.txt").read_text(encoding="utf-8"), "committed\n")
            self.assertEqual(evidence["revision"], revision)
            self.assertEqual(evidence["working_tree_used"], False)
            self.assertEqual(evidence["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
