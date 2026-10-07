"""HC-16 bounded tooling-retirement policy and live census tests."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hc16_tooling_retirement", ROOT / "scripts" / "tooling_retirement.py"
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery invariant
    raise RuntimeError("cannot load tooling-retirement policy")
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


class ToolingRetirementPolicyTests(unittest.TestCase):
    def test_census_is_complete_sorted_and_byte_deterministic(self) -> None:
        first = TOOL.build_census(ROOT)
        second = TOOL.build_census(ROOT)
        paths = tuple(row.path for row in first.tools)
        expected = tuple(path for path in TOOL.tracked_paths(ROOT) if TOOL.is_tool_path(path, ROOT))
        self.assertEqual(paths, tuple(sorted(expected)))
        self.assertEqual(TOOL.report_bytes(first), TOOL.report_bytes(second))

    def test_every_unreferenced_retained_tool_has_a_bounded_reason(self) -> None:
        census = TOOL.build_census(ROOT)
        unreferenced = tuple(row for row in census.tools if not row.consumers)
        self.assertTrue(unreferenced)
        for row in unreferenced:
            with self.subTest(path=row.path):
                self.assertIn(row.disposition, {"retain_explicit", "retain_required"})
                self.assertGreaterEqual(len(row.rationale), 24)
                self.assertLessEqual(len(row.rationale), TOOL.MAX_RATIONALE)

    def test_a_nested_entry_point_is_censused_and_its_library_siblings_are_not(self) -> None:
        # M23-49 register row 8. `scripts/governance/relocate_packaged_artifacts.py` shipped in
        # M23-55 outside a census that reports itself complete, because the path rule admitted only
        # `scripts/<name>.py`. The fix must admit the tool without admitting the eighteen library
        # modules beside it, or the unreferenced population stops meaning anything.
        censused = {row.path for row in TOOL.build_census(ROOT).tools}
        self.assertIn("scripts/governance/relocate_packaged_artifacts.py", censused)
        for module in (
            "scripts/governance/__init__.py",
            "scripts/governance/architecture_inventory.py",
            "scripts/governance/public_surface.py",
            "scripts/m19_07/host.py",
        ):
            with self.subTest(path=module):
                self.assertNotIn(module, censused)

    def test_the_nested_rule_reads_the_entry_point_rather_than_a_list(self) -> None:
        # CRITICAL: a hand-maintained list of nested tools reintroduces exactly the gap this
        # closes -- the next nested tool is written, nobody remembers the list, and the census is
        # quietly incomplete again while still reporting PASS. The predicate must decide from the
        # file itself.
        self.assertTrue(
            TOOL.is_nested_tool_path("scripts/governance/relocate_packaged_artifacts.py", ROOT)
        )
        self.assertFalse(TOOL.is_nested_tool_path("scripts/governance/node_surface.py", ROOT))
        self.assertFalse(TOOL.is_nested_tool_path("scripts/governance/__init__.py", ROOT))
        # A top-level path is not a nested one.
        self.assertFalse(TOOL.is_nested_tool_path("scripts/tooling_retirement.py", ROOT))
        # Non-tool suffixes stay out even inside a package.
        self.assertFalse(
            TOOL.is_nested_tool_path("scripts/m19_07/qualification_evidence_v1.schema.json", ROOT)
        )

    def test_the_nested_rule_fails_closed_rather_than_answering_no(self) -> None:
        # Escaping the repository, and a path that cannot be read, are both loud. A predicate that
        # answered "not a tool" for either would drop a file out of a census that reports itself
        # complete -- the same silence row 8 exists to remove.
        for path in ("scripts/../../outside/tool.py", "scripts/governance/absent.py"):
            with self.subTest(path=path), self.assertRaises(TOOL.ToolingRetirementError):
                TOOL.is_nested_tool_path(path, ROOT)

    def test_an_unknown_unreferenced_tool_fails_closed(self) -> None:
        with self.assertRaises(TOOL.ToolingRetirementError):
            TOOL.classify_tool(
                "scripts/new_orphan.py",
                (),
                manual_retentions={},
                required_retentions={},
            )

    def test_a_remaining_reader_blocks_a_retirement_claim(self) -> None:
        with self.assertRaises(TOOL.ToolingRetirementError):
            TOOL.validate_retired_readers(
                ("scripts/old_generator.py",),
                {"old_generator": {"tests/test_live_consumer.py"}},
            )

    def test_tracked_path_traversal_is_rejected(self) -> None:
        with self.assertRaises(TOOL.ToolingRetirementError):
            TOOL._safe_file(ROOT, "../outside.py")

    def test_remote_qualification_capability_is_required_and_retained(self) -> None:
        path = "scripts/m22_14_remote_qualification.py"
        census = TOOL.build_census(ROOT)
        row = {item.path: item for item in census.tools}[path]
        self.assertTrue((ROOT / path).is_file())
        self.assertEqual(row.disposition, "retain_required")
        self.assertIn("typed receipt", row.rationale)

    def test_retired_paths_are_absent_and_recoverable_from_git_history(self) -> None:
        census = TOOL.build_census(ROOT)
        self.assertEqual(len(census.retired_paths), 9)
        for record in census.retired_tools:
            with self.subTest(path=record.path):
                self.assertFalse((ROOT / record.path).exists())
                self.assertEqual(record.consumers, ())
                self.assertGreaterEqual(len(record.rationale), 24)
                self.assertGreaterEqual(len(record.artifact_disposition), 24)
                self.assertRegex(record.recovery_commit, r"\A[0-9a-f]{40}\Z")
                subprocess.run(
                    [
                        "git",
                        "cat-file",
                        "-e",
                        f"{record.recovery_commit}:{record.path}",
                    ],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                )

    def test_a_generated_record_that_names_tools_is_not_counted_as_their_reader(self) -> None:
        # The exclusion is load-bearing, not decorative. A generated record that *describes* which
        # tool consumes what would otherwise keep alive the very tools the census is judging.
        # M23-50 added `source_reachability.owners` to the fitness artifact and it credited two
        # fixture generators with a "consumer" whose only statement about them is that nothing else
        # uses them; the M23-50 review then found the same defect already live in the retirement
        # artifact, where an `internal_consumers` field was one tool's ONLY tracked mention. Both
        # directions are asserted here: no excluded record is a consumer, and no excluded record is
        # a stale entry that names no tool at all.
        # CRITICAL: both roots. M23-55 moved every generated record with no runtime or bundle
        # reader to `governance/contracts/`, leaving exactly one of these under the packaged root,
        # so a packaged-only filter would still satisfy `assertTrue` below while silently checking
        # a sixth of the population.
        excluded = sorted(
            path
            for path in TOOL.SELF_DESCRIBING_PATHS
            if path.startswith(("comfyui_h3_context/contracts/", "governance/contracts/"))
        )
        self.assertTrue(excluded)
        rows = {row.path: row for row in TOOL.build_census(ROOT).tools}
        stems = {Path(path).stem: path for path in rows}
        for record in excluded:
            with self.subTest(record=record):
                text = (ROOT / record).read_text(encoding="utf-8")
                named = sorted(path for stem, path in stems.items() if stem in text)
                self.assertTrue(named, f"{record} names no tool and need not be excluded")
                for path in named:
                    self.assertNotIn(record, rows[path].consumers)

    def test_no_tool_is_kept_alive_only_by_a_generated_record(self) -> None:
        # The forward guard for the whole class, stated as the invariant rather than as a list of
        # known cases: a generated record describes structure, so it is never the thing that keeps
        # a tool alive. A generator that starts emitting tracked script paths is invisible in its
        # own diff and real in the artifact, and this fails on the next one instead of the complete
        # backend stage of a Full Gate discovering it hours later.
        # CRITICAL: a generated record lives under either root since M23-55, and 134 of the 143
        # moved. Checking only the packaged prefix would let a tool whose sole consumer is a
        # `governance/contracts/` record read as alive -- which is precisely the defect.
        generated = ("comfyui_h3_context/contracts/", "governance/contracts/")
        for row in TOOL.build_census(ROOT).tools:
            if not row.consumers:
                continue
            with self.subTest(path=row.path):
                self.assertTrue(
                    [item for item in row.consumers if not item.startswith(generated)],
                    f"{row.path} is referenced only by generated records",
                )


if __name__ == "__main__":
    unittest.main()
