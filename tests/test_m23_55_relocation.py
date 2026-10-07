"""M23-55: the governance relocation is computed from measured readership, never from a name.

The failure this whole item guards against is one file travelling with the move that should not.
`host_seam_census_v1.json` is the standing proof that no heuristic gets it right -- it is a
hand-maintained census with no generator, and a module under `frontend/src/host/` imports it, so
the bundle compiles it in. M23-47 spent a gate run discovering that.

So these tests do not check that the tool moved 134 named files. They check the properties that
survive the tree changing underneath them: the set comes from the record, a reader of any kind is
refused, a reference the rewrite could not reach refuses the whole transaction, and an interrupted
apply is recoverable rather than resumable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts import packaged_artifact_readership as census
from scripts.governance import relocate_packaged_artifacts as relocate

REPO_ROOT = Path(__file__).resolve().parents[1]

PACKAGED = relocate.PACKAGE_ROOT
GOVERNANCE = relocate.GOVERNANCE_ROOT


def _row(name: str, readership: str, readers: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "path": f"{PACKAGED}/{name}",
        "readership": readership,
        "readers": list(readers),
        "disposition": relocate_disposition(readership),
    }


def _font_row(name: str, readership: str = "runtime_read") -> dict[str, object]:
    return {
        "path": f"comfyui_h3_context/fonts/{name}",
        "readership": readership,
        "readers": ["comfyui_h3_context/adapters/authoring_fonts.py"],
        "disposition": relocate_disposition(readership),
    }


def relocate_disposition(readership: str) -> str:
    return {
        "frontend_source_read": "retain: the bundle compiles it in",
        "runtime_read": "retain: the installed package opens it at run time",
        "tooling_or_evidence_only": "movable: no runtime or bundle reader",
        "no_reader": "retain pending retirement review: nothing reads it",
    }[readership]


def _record(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "h3.context.packaged_artifact_readership.v1",
        "artifact_count": len(rows),
        "artifacts": rows,
        "counts": {},
        "dispositions": {},
        "movable": [row["path"] for row in rows if str(row["disposition"]).startswith("movable")],
    }


DEFAULT_ROWS = [
    _row("kept_runtime_v1.json", "runtime_read", ("comfyui_h3_context/core/loader.py",)),
    _row("kept_bundle_v1.json", "frontend_source_read", ("frontend/src/host/seams.ts",)),
    _row("moved_record_v1.json", "tooling_or_evidence_only", ("scripts/census.py",)),
    _row("moved_schema_v1.schema.json", "tooling_or_evidence_only", ("tests/test_census.py",)),
    _row("unread_v1.json", "no_reader"),
]


@contextmanager
def temporary_repository(files: dict[str, str], rows: list[dict[str, object]]) -> Iterator[Path]:
    """A small real git repository with a synthetic record, `relocate.ROOT` pointed at it.

    CRITICAL: the tool runs `git mv` and replaces file bytes. Exercising it against the repository
    running the tests would relocate the repository. Everything stays inside a temporary directory,
    and `_bundle_inputs` is stubbed because there is no `scripts/derived_artifacts.py` in here.
    """

    root = Path(tempfile.mkdtemp(prefix="m23-55-repo-")).resolve()

    def run(*arguments: str) -> None:
        result = subprocess.run(["git", *arguments], cwd=root, capture_output=True)
        if result.returncode != 0:
            raise AssertionError(f"fixture: git {' '.join(arguments)} failed")

    record = _record(rows)
    payload = dict(files)
    payload[relocate.READERSHIP] = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    for row in rows:
        path = str(row["path"])
        payload.setdefault(path, json.dumps({"schema": Path(path).stem}) + "\n")
    for name, text in payload.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    run("init", "--quiet")
    run("config", "user.email", "fixture@example.invalid")
    run("config", "user.name", "fixture")
    run("config", "core.autocrlf", "false")
    run("add", "--all")
    run("commit", "--quiet", "-m", "fixture")

    bundle = frozenset(
        str(row["path"]) for row in rows if row["readership"] == "frontend_source_read"
    )
    try:
        with (
            patch.object(relocate, "ROOT", root),
            patch.object(relocate, "_bundle_inputs", lambda: bundle),
        ):
            yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class TheSetComesFromTheRecordTests(unittest.TestCase):
    def test_the_selection_is_the_movable_rows_plus_the_unread_ones(self) -> None:
        with temporary_repository({}, DEFAULT_ROWS):
            self.assertEqual(
                relocate.selected_paths(),
                (
                    f"{PACKAGED}/moved_record_v1.json",
                    f"{PACKAGED}/moved_schema_v1.schema.json",
                    f"{PACKAGED}/unread_v1.json",
                ),
            )

    def test_reader_backed_font_artifacts_are_retained_outside_the_contract_move_root(self) -> None:
        rows = [*DEFAULT_ROWS, _font_row("font_manifest_v1.json")]
        with temporary_repository({}, rows):
            self.assertEqual(
                relocate.selected_paths(),
                (
                    f"{PACKAGED}/moved_record_v1.json",
                    f"{PACKAGED}/moved_schema_v1.schema.json",
                    f"{PACKAGED}/unread_v1.json",
                ),
            )

    def test_a_non_reader_font_artifact_cannot_escape_contract_relocation_governance(self) -> None:
        rows = [*DEFAULT_ROWS, _font_row("orphan.ttf", "tooling_or_evidence_only")]
        with temporary_repository({}, rows):
            with self.assertRaisesRegex(relocate.RelocationError, "retained packaged root"):
                relocate.selected_paths()

    def test_a_reader_backed_font_artifact_marked_movable_is_refused(self) -> None:
        font = _font_row("font_manifest_v1.json")
        font["disposition"] = relocate_disposition("tooling_or_evidence_only")
        with temporary_repository({}, [*DEFAULT_ROWS, font]):
            with self.assertRaisesRegex(relocate.RelocationError, "shipped reader"):
                relocate.selected_paths()

    def test_a_duplicated_retained_font_row_is_refused(self) -> None:
        font = _font_row("font_manifest_v1.json")
        with temporary_repository({}, [*DEFAULT_ROWS, font, dict(font)]):
            with self.assertRaisesRegex(relocate.RelocationError, "twice"):
                relocate.selected_paths()

    def test_a_runtime_reader_marked_movable_is_refused(self) -> None:
        # The disposition and the readership class disagree, which is the shape of a census bug.
        # Refusing here is the difference between a caught contradiction and a broken wheel.
        rows = [dict(row) for row in DEFAULT_ROWS]
        rows[0]["disposition"] = relocate_disposition("tooling_or_evidence_only")
        with temporary_repository({}, rows):
            with self.assertRaisesRegex(relocate.RelocationError, "reader inside the shipped"):
                relocate.selected_paths()

    def test_a_bundle_input_marked_movable_is_refused(self) -> None:
        # CRITICAL: this is the `host_seam_census_v1.json` case, and it is caught by a *second*
        # authority. The record and the generator declaration are produced by different scripts
        # from different evidence, so an artifact that becomes a bundle input without the census
        # noticing is only visible where the two are compared.
        rows = [dict(row) for row in DEFAULT_ROWS]
        rows[1]["readership"] = "tooling_or_evidence_only"
        rows[1]["disposition"] = relocate_disposition("tooling_or_evidence_only")
        with temporary_repository({}, rows) as root:
            del root
            with patch.object(
                relocate, "_bundle_inputs", lambda: frozenset({f"{PACKAGED}/kept_bundle_v1.json"})
            ):
                with self.assertRaisesRegex(relocate.RelocationError, "declared bundle inputs"):
                    relocate.selected_paths()

    def test_a_row_outside_the_packaged_directory_is_refused(self) -> None:
        rows = [dict(row) for row in DEFAULT_ROWS]
        rows[2]["path"] = "governance/contracts/already_moved_v1.json"
        with temporary_repository({}, rows):
            with self.assertRaisesRegex(relocate.RelocationError, "outside"):
                relocate.selected_paths()

    def test_a_traversing_path_is_refused_before_anything_is_touched(self) -> None:
        rows = [dict(row) for row in DEFAULT_ROWS]
        rows[2]["path"] = f"{PACKAGED}/../../etc/passwd"
        with temporary_repository({}, rows):
            with self.assertRaisesRegex(relocate.RelocationError, "traversing"):
                relocate.selected_paths()

    def test_a_duplicated_row_is_refused(self) -> None:
        rows = [dict(row) for row in DEFAULT_ROWS]
        rows.append(dict(rows[2]))
        with temporary_repository({}, rows):
            with self.assertRaisesRegex(relocate.RelocationError, "twice"):
                relocate.selected_paths()

    def test_an_empty_record_is_refused(self) -> None:
        with temporary_repository({}, []):
            with self.assertRaisesRegex(relocate.RelocationError, "no artifact rows"):
                relocate.selected_paths()


class TheRewriteReachesBothReferenceFormsTests(unittest.TestCase):
    FILES = {
        "scripts/reader.py": (
            "from pathlib import Path\n"
            'MOVED = Path("comfyui_h3_context/contracts/moved_record_v1.json")\n'
            'KEPT = Path("comfyui_h3_context/contracts/kept_runtime_v1.json")\n'
            'JOINED = Path("comfyui_h3_context") / "contracts" / "moved_schema_v1.schema.json"\n'
            "BROKEN = (\n"
            '    "comfyui_h3_context",\n'
            '    "contracts",\n'
            '    "unread_v1.json",\n'
            ")\n"
        ),
        # The real baseline keys paths with Windows separators, so the file text carries an
        # escaped backslash. A forward-slash-only rewrite leaves seven of these stale.
        ".secrets.baseline": json.dumps(
            {
                "results": {
                    "comfyui_h3_context\\contracts\\moved_record_v1.json": [],
                    "comfyui_h3_context\\contracts\\kept_runtime_v1.json": [],
                }
            },
            indent=2,
        )
        + "\n",
    }

    def test_every_form_of_a_moved_reference_is_rewritten_and_a_kept_one_is_not(self) -> None:
        with temporary_repository(self.FILES, DEFAULT_ROWS) as root:
            plan = relocate.build_plan()
            self.assertEqual(plan.manual, ())
            relocate.apply_plan(plan)
            source = (root / "scripts/reader.py").read_text(encoding="utf-8")
            self.assertIn(f'"{GOVERNANCE}/moved_record_v1.json"', source)
            self.assertIn(f'"{PACKAGED}/kept_runtime_v1.json"', source)
            self.assertIn('Path("governance") / "contracts" / "moved_schema', source)
            self.assertIn('"governance",\n    "contracts",\n    "unread_v1.json",', source)
            baseline = json.loads((root / ".secrets.baseline").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(baseline["results"]),
                [
                    "comfyui_h3_context\\contracts\\kept_runtime_v1.json",
                    "governance\\contracts\\moved_record_v1.json",
                ],
            )

    def test_the_files_land_where_the_plan_said_and_nothing_else_moves(self) -> None:
        with temporary_repository(self.FILES, DEFAULT_ROWS) as root:
            plan = relocate.build_plan()
            relocate.apply_plan(plan)
            # The record already sits under the governance root -- that is where the generator
            # writes it and where `relocate.READERSHIP` reads it from -- so it is present at the
            # destination without ever having been part of the move. Subtracting it here says
            # exactly that; globbing the directory and hardcoding a list would silently start
            # asserting the fixture's own layout instead of the plan's outcome.
            landed = {path.name for path in (root / GOVERNANCE).glob("*.json")}
            record_name = Path(relocate.READERSHIP).name
            self.assertIn(record_name, landed)
            self.assertEqual(
                sorted(landed - {record_name}),
                sorted(Path(destination).name for _, destination in plan.moves),
            )
            self.assertEqual(
                sorted(landed - {record_name}),
                ["moved_record_v1.json", "moved_schema_v1.schema.json", "unread_v1.json"],
            )
            kept = sorted(path.name for path in (root / PACKAGED).glob("*.json"))
            self.assertEqual(kept, ["kept_bundle_v1.json", "kept_runtime_v1.json"])

    def test_the_move_is_staged_as_a_rename(self) -> None:
        # A copy-and-delete loses the file's history. `git mv` is what keeps `git log --follow`
        # working across 134 files.
        with temporary_repository(self.FILES, DEFAULT_ROWS) as root:
            relocate.apply_plan(relocate.build_plan())
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout
            self.assertIn("R ", status)
            self.assertNotIn("?? governance", status)

    def test_a_reference_the_rewrite_cannot_reach_refuses_the_transaction(self) -> None:
        # The mutation proof for the whole item: a constant bound to the packaged directory and
        # joined with a moved basename later resolves to nothing after the move, and no
        # per-basename substitution can see it. Planting one must stop the transaction.
        planted = dict(self.FILES)
        planted["scripts/constant.py"] = (
            "from pathlib import Path\n"
            'CONTRACTS = Path("comfyui_h3_context") / "contracts"\n'
            'TARGET = CONTRACTS / "moved_record_v1.json"\n'
        )
        with temporary_repository(planted, DEFAULT_ROWS):
            plan = relocate.build_plan()
            self.assertEqual(
                plan.manual,
                ("scripts/constant.py: joins a packaged constant to moved_record_v1.json",),
            )
            with self.assertRaisesRegex(relocate.RelocationError, "scripts/constant.py"):
                relocate.apply_plan(plan)

    def test_a_sibling_derived_from_a_retained_path_refuses_the_transaction(self) -> None:
        # The regression this check was added for. A document that stays in the package and a
        # schema that moves out are no longer siblings, but `Path.with_name` still says they are:
        # the produced path exists nowhere in the source, so the per-basename rewrite has nothing
        # to match and the residue check sees no *directory* constant to anchor on -- the anchor
        # here is a complete path, which `_without_inline_paths` strips. This planted file is the
        # shape of `tests/test_hc_09_host_seam_contract.py`, which shipped broken and cost a Full
        # Gate. Reverting `_derived_sibling_names` to an empty frozenset fails exactly this test.
        planted = dict(self.FILES)
        planted["scripts/sibling.py"] = (
            "from pathlib import Path\n"
            'KEPT = Path("comfyui_h3_context") / "contracts" / "kept_runtime_v1.json"\n'
            'SCHEMA = KEPT.with_name("moved_schema_v1.schema.json")\n'
        )
        with temporary_repository(planted, DEFAULT_ROWS):
            plan = relocate.build_plan()
            self.assertEqual(
                plan.manual,
                ("scripts/sibling.py: derives sibling moved_schema_v1.schema.json",),
            )
            with self.assertRaisesRegex(relocate.RelocationError, "scripts/sibling.py"):
                relocate.apply_plan(plan)

    def test_every_derivation_idiom_is_seen_and_a_retained_sibling_is_not_reported(self) -> None:
        # `with_suffix` and `.parent /` build a sibling exactly as `with_name` does, and a
        # derivation that lands on a *retained* basename is correct and must stay quiet -- the
        # check reports a broken destination, not the use of the idiom.
        for expression in (
            'SCHEMA = KEPT.with_name("moved_schema_v1.schema.json")',
            'SCHEMA = KEPT.with_suffix("moved_schema_v1.schema.json")',
            'SCHEMA = KEPT.parent / "moved_schema_v1.schema.json"',
        ):
            with self.subTest(expression=expression):
                planted = dict(self.FILES)
                planted["scripts/sibling.py"] = (
                    "from pathlib import Path\n"
                    'KEPT = Path("comfyui_h3_context") / "contracts" / "kept_runtime_v1.json"\n'
                    f"{expression}\n"
                )
                with temporary_repository(planted, DEFAULT_ROWS):
                    self.assertEqual(
                        relocate.build_plan().manual,
                        ("scripts/sibling.py: derives sibling moved_schema_v1.schema.json",),
                    )
        planted = dict(self.FILES)
        planted["scripts/sibling.py"] = (
            "from pathlib import Path\n"
            'KEPT = Path("comfyui_h3_context") / "contracts" / "kept_runtime_v1.json"\n'
            'OTHER = KEPT.with_name("kept_bundle_v1.json")\n'
        )
        with temporary_repository(planted, DEFAULT_ROWS):
            self.assertEqual(relocate.build_plan().manual, ())

    def test_a_sibling_derivation_that_was_read_by_hand_can_be_pinned(self) -> None:
        # The acknowledgement is per basename, so pinning the one that was checked still refuses a
        # different one appearing later. Both `tests/test_m22_16_compatibility_closeout.py` and
        # `tests/test_managed_run_aggregate.py` are pinned this way: their anchors moved with their
        # schemas, so the derivation stays correct, but only because somebody looked.
        planted = dict(self.FILES)
        planted["scripts/sibling.py"] = (
            "from pathlib import Path\n"
            'MOVED = Path("comfyui_h3_context") / "contracts" / "moved_record_v1.json"\n'
            'SCHEMA = MOVED.with_name("moved_schema_v1.schema.json")\n'
        )
        with temporary_repository(planted, DEFAULT_ROWS):
            with patch.dict(
                relocate.ACKNOWLEDGED_SPLIT_ROOTS,
                {"scripts/sibling.py": frozenset({"moved_schema_v1.schema.json"})},
                clear=False,
            ):
                self.assertEqual(relocate.build_plan().manual, ())
            with patch.dict(
                relocate.ACKNOWLEDGED_SPLIT_ROOTS,
                {"scripts/sibling.py": frozenset({"unread_v1.json"})},
                clear=False,
            ):
                self.assertEqual(
                    relocate.build_plan().manual,
                    ("scripts/sibling.py: derives sibling moved_schema_v1.schema.json",),
                )

    def test_a_declared_historical_reference_is_left_alone(self) -> None:
        # `git show <commit>:<path>` resolves inside that commit, where the artifact was still
        # packaged. Rewriting one produces a generator that fails on a path that never existed.
        planted = dict(self.FILES)
        span = 'HISTORICAL = "comfyui_h3_context/contracts/moved_record_v1.json"'
        planted["scripts/history.py"] = f"{span}\n"
        with temporary_repository(planted, DEFAULT_ROWS) as root:
            with patch.dict(
                relocate.HISTORICAL_REFERENCES, {"scripts/history.py": (span,)}, clear=False
            ):
                relocate.apply_plan(relocate.build_plan())
                self.assertEqual(
                    (root / "scripts/history.py").read_text(encoding="utf-8"), f"{span}\n"
                )

    def test_a_historical_declaration_that_stopped_matching_refuses(self) -> None:
        # A declaration nobody can find is a silent no-op, and the next relocation would rewrite
        # the path it was protecting.
        with temporary_repository(self.FILES, DEFAULT_ROWS):
            with patch.dict(
                relocate.HISTORICAL_REFERENCES,
                {"scripts/reader.py": ('GONE = "nothing here"',)},
                clear=False,
            ):
                with self.assertRaisesRegex(relocate.RelocationError, "appears 0 times"):
                    relocate.build_plan()


class TheTransactionIsAllOrNothingTests(unittest.TestCase):
    FILES = TheRewriteReachesBothReferenceFormsTests.FILES

    def test_the_dry_run_predicts_exactly_what_the_apply_writes(self) -> None:
        with temporary_repository(self.FILES, DEFAULT_ROWS) as root:
            plan = relocate.build_plan()
            predicted = {rewrite.destination: rewrite.after_sha256 for rewrite in plan.rewrites}
            relocate.apply_plan(plan)
            for destination, expected in predicted.items():
                with self.subTest(path=destination):
                    self.assertEqual(
                        relocate._digest((root / destination).read_bytes()),  # noqa: SLF001
                        expected,
                    )

    def test_a_file_that_changed_since_the_plan_stops_the_write(self) -> None:
        with temporary_repository(self.FILES, DEFAULT_ROWS) as root:
            plan = relocate.build_plan()
            (root / "scripts/reader.py").write_bytes(b"# replaced after planning\n")
            with self.assertRaisesRegex(relocate.RelocationError, "changed since the plan"):
                relocate.apply_plan(plan)

    def test_applying_over_a_half_applied_tree_is_refused_rather_than_resumed(self) -> None:
        # CRITICAL: resuming is the tempting behaviour and it is wrong. The tool cannot tell a
        # partly applied transaction from a tree someone edited by hand in between, and the
        # recovery for both is the same: restore from git and start over.
        with temporary_repository(self.FILES, DEFAULT_ROWS) as root:
            plan = relocate.build_plan()
            # `exist_ok`: the fixture already created this directory, because the readership
            # record lives under the governance root and is read from there.
            (root / GOVERNANCE).mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "mv", plan.moves[0][0], plan.moves[0][1]], cwd=root, capture_output=True
            )
            with self.assertRaisesRegex(relocate.RelocationError, "half-applied"):
                relocate.apply_plan(relocate.build_plan())
            del plan

    def test_the_prior_tree_is_recoverable_after_an_interrupted_apply(self) -> None:
        with temporary_repository(self.FILES, DEFAULT_ROWS) as root:
            plan = relocate.build_plan()
            # `exist_ok`: the fixture already created this directory, because the readership
            # record lives under the governance root and is read from there.
            (root / GOVERNANCE).mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "mv", plan.moves[0][0], plan.moves[0][1]], cwd=root, capture_output=True
            )
            subprocess.run(["git", "reset", "--hard", "--quiet"], cwd=root, capture_output=True)
            self.assertTrue((root / plan.moves[0][0]).is_file())
            self.assertEqual(relocate.build_plan().moves, plan.moves)

    def test_a_second_run_refuses_and_changes_nothing(self) -> None:
        # The property is that a finished relocation is never partly redone, whichever refusal
        # fires first -- the record's own rows now name the destination, and once it is
        # regenerated it selects nothing at all. Asserting the tree instead of the message keeps
        # this honest across both.
        with temporary_repository(self.FILES, DEFAULT_ROWS) as root:
            relocate.apply_plan(relocate.build_plan())
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file() and ".git" not in path.parts
            }
            with self.assertRaises(relocate.RelocationError):
                relocate.apply_plan(relocate.build_plan())
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file() and ".git" not in path.parts
            }
            self.assertEqual(before, after)

    def test_an_oversized_file_is_reported_rather_than_loaded(self) -> None:
        planted = dict(self.FILES)
        planted["tests/huge.py"] = "# padding\n"
        with temporary_repository(planted, DEFAULT_ROWS) as root:
            (root / "tests/huge.py").write_bytes(b"x" * (relocate.MAX_SCAN_BYTES + 1))
            with self.assertRaisesRegex(relocate.RelocationError, "larger than"):
                relocate.build_plan()


class TheResidueCheckIsRealTests(unittest.TestCase):
    def test_a_stale_reference_planted_after_the_rewrite_is_reported(self) -> None:
        files = dict(TheRewriteReachesBothReferenceFormsTests.FILES)
        with temporary_repository(files, DEFAULT_ROWS) as root:
            relocate.apply_plan(relocate.build_plan())
            self.assertEqual(
                relocate.surviving_references(frozenset({f"{PACKAGED}/moved_record_v1.json"})), ()
            )
            (root / "scripts/reader.py").write_text(
                f'STALE = "{PACKAGED}/moved_record_v1.json"\n', encoding="utf-8"
            )
            self.assertEqual(
                relocate.surviving_references(frozenset({f"{PACKAGED}/moved_record_v1.json"})),
                (("scripts/reader.py", 1),),
            )


class TheRepositoryTopologyMatchesTheRecordTests(unittest.TestCase):
    """Asserted against the real tree, so the item's outcome cannot silently regress."""

    def test_the_packaged_directory_holds_only_artifacts_with_a_reader(self) -> None:
        # The record is read from the generator's own output constant rather than from a path
        # spelled here, so this test follows the artifact if it ever moves again.
        record = json.loads((REPO_ROOT / census.ARTIFACT_PATH).read_text(encoding="utf-8"))
        packaged = {
            path.relative_to(REPO_ROOT).as_posix()
            for root in relocate.PACKAGED_ARTIFACT_ROOTS
            for path in (REPO_ROOT / root).iterdir()
            if path.is_file()
        }
        classified = {str(row["path"]): str(row["readership"]) for row in record["artifacts"]}
        self.assertEqual(packaged, set(classified))
        self.assertTrue(packaged)
        for path, readership in classified.items():
            with self.subTest(path=path):
                self.assertIn(readership, relocate.READING_CLASSES)

    def test_the_declared_bundle_inputs_all_stayed_in_the_package(self) -> None:
        # `sys.executable`, not a hardcoded `.venv/Scripts/python.exe`: TEST_SOP 3.1 admits a
        # Linux/WSL runner as well as the Windows one, and a Windows-only path would skip there
        # rather than fail, which is the quietest way for this check to stop existing.
        result = subprocess.run(
            [sys.executable, "scripts/derived_artifacts.py", "bundle-inputs"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        inputs = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(inputs)
        for path in inputs:
            with self.subTest(path=path):
                self.assertTrue(path.startswith(f"{PACKAGED}/"), path)
                self.assertTrue((REPO_ROOT / path).is_file(), path)

    def test_the_tool_can_read_the_record_where_it_actually_lives(self) -> None:
        # CRITICAL: the unmocked path, against the real repository. Every other test in this module
        # builds a temporary tree and writes the record to wherever `relocate.READERSHIP` points,
        # so the record is found no matter what that constant says -- which is how the constant
        # came to spell the pre-move location, survive the relocation that moved the record out
        # from under it, and make every real invocation of the tool die with "the readership record
        # is missing" while all 22 tests stayed green. A reviewer found it by running the tool.
        #
        # Three assertions, because the constant can be wrong in three ways: it can name a file
        # that is not there, it can drift from the generator that writes the file, and it can name
        # something that is not a readable record. Reverting it to `PACKAGE_ROOT` fails the first.
        self.assertTrue(
            (REPO_ROOT / relocate.READERSHIP).is_file(),
            f"the tool reads {relocate.READERSHIP}, which does not exist",
        )
        self.assertEqual(relocate.READERSHIP, census.ARTIFACT_PATH.as_posix())
        record = relocate._read_record()  # noqa: SLF001
        self.assertIsInstance(record.get("artifacts"), list)

    def test_an_applied_tree_refuses_both_commands_instead_of_planning_nothing(self) -> None:
        # The relocation has been applied, so the regenerated record classifies only the retained
        # set and selects nothing. That must refuse, not render a plan: with no moved names the
        # alternation is empty, `(?:)` matches the empty string, and the rewrite pattern would
        # match every packaged contracts prefix in the repository. Before the guard, `dry-run`
        # reported nine phantom rewrites against the nine retained paths.
        with self.assertRaisesRegex(relocate.RelocationError, "selects nothing to move"):
            relocate.build_plan()
        with self.assertRaisesRegex(relocate.RelocationError, "empty name set"):
            relocate._patterns(frozenset())  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
