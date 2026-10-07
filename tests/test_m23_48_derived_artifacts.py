"""M23-48: the regeneration order is derived from a declaration, not remembered from prose.

`tests/TEST_SOP.md` 3.3.4 is the prose that two items in one day failed to follow -- M23-46 put the
contract inventory after the first provenance write, M23-47 hand-edited a contract JSON that
`frontend/src` compiles in. Neither mistake was visible to any generator's `--check`; both surfaced
only in the offline rebuild parity inside the backend stage of the Full Gate.

So the test that matters is not "does the module have a list". It is that the order **falls out of**
the declared edges, and that what falls out is the sequence the SOP describes. If someone edits an
edge and the SOP sequence stops appearing, one of the two is now wrong and this fails.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts import derived_artifacts as declaration

REPO_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def temporary_repository(files: dict[str, bytes]) -> Iterator[Path]:
    """A small real git repository, with `declaration.ROOT` pointed at it for the duration.

    CRITICAL: the code under test clones, stages and *deletes* trees. Exercising it against the
    repository that is running the tests has two failure modes that are not hypothetical: a probe
    file left behind by an interrupted run is picked up by the next `--write` cascade and baked
    into the shipped artifacts, and a weakened guard in `_remove_tree` takes the working tree with
    it. Both stay inside a temporary directory here.
    """

    root = Path(tempfile.mkdtemp(prefix="m23-48-repo-")).resolve()

    def run(*arguments: str) -> None:
        result = subprocess.run(["git", *arguments], cwd=root, capture_output=True)
        if result.returncode != 0:
            raise AssertionError(f"fixture: git {' '.join(arguments)} failed")

    for name, payload in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    run("init", "--quiet")
    run("config", "user.email", "fixture@example.invalid")
    run("config", "user.name", "fixture")
    run("config", "core.autocrlf", "false")
    run("add", "--all")
    run("commit", "--quiet", "-m", "fixture")
    try:
        with patch.object(declaration, "ROOT", root):
            yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _link_directory(link: Path, target: Path) -> bool:
    """Create a directory link, by whichever mechanism this machine permits. False if none does."""

    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
    )
    return result.returncode == 0 and link.exists()


def _workspace_tracked(destination: Path) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=destination, capture_output=True, text=True, encoding="utf-8"
    )
    return {line for line in result.stdout.splitlines() if line}


def _candidate(path: str, generator: str, verdict: str) -> declaration.Candidate:
    return declaration.Candidate(
        path=path,
        generator=generator,
        verdict=verdict,  # type: ignore[arg-type]
        before_sha256="sha256:" + "0" * 64,
        after_sha256="sha256:" + "1" * 64,
        size=1,
    )


#: `tests/TEST_SOP.md` 3.3.4, as a subsequence the derived order must contain contiguously. The
#: bundle build sits in the middle, and the second provenance write after it is the whole point:
#: the first records the frontend sources, the bundle embeds that identity, the second records the
#: resulting bundle bytes.
TEST_SOP_334 = (
    "contract_inventory",
    "build_provenance",
    declaration.BUNDLE_BUILD,
    "build_provenance",
    "supply_chain_manifest",
)


def _scripts_declaring_write() -> set[str]:
    """Scripts whose CLI declares `--write`, found by parsing rather than by substring.

    CRITICAL: a substring search for `"--write"` also matches `derived_artifacts.py` itself, which
    names the flag when it invokes a generator and is not one. It would also miss
    `supply_chain_manifest.py`, whose `add_argument` call puts the flag on its own line. Parsing the
    call is what makes "is this a generator" a property of the CLI rather than of formatting.
    """

    found: set[str] = set()
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "add_argument" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "--write":
                found.add(path.stem)
    return found


def _contiguous(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    return any(
        haystack[index : index + len(needle)] == needle
        for index in range(len(haystack) - len(needle) + 1)
    )


class TheDeclarationDescribesRealFilesTests(unittest.TestCase):
    def test_every_declared_output_exists(self) -> None:
        for generator in declaration.GENERATORS:
            for path in generator.outputs:
                with self.subTest(generator=generator.name, output=path):
                    self.assertTrue(
                        (REPO_ROOT / path).is_file(),
                        f"{generator.name} declares {path}, which is not in the tree",
                    )

    def test_every_declared_generator_is_a_script_that_writes(self) -> None:
        writing = _scripts_declaring_write()
        for generator in declaration.GENERATORS:
            with self.subTest(generator=generator.name):
                self.assertTrue(
                    (REPO_ROOT / "scripts" / f"{generator.name}.py").is_file(),
                    f"{generator.name} is not a script",
                )
                self.assertIn(generator.name, writing, f"{generator.name} does not expose --write")

    def test_the_declaration_covers_every_writing_script(self) -> None:
        # A generator added later and left undeclared is the failure this catches: the orchestrator
        # would order the declared ones perfectly and silently leave the new one out.
        self.assertEqual(_scripts_declaring_write(), set(declaration.by_name()))

    def test_no_two_generators_claim_one_output(self) -> None:
        declaration.validate()
        seen: dict[str, str] = {}
        for generator in declaration.GENERATORS:
            for path in generator.outputs:
                self.assertNotIn(path, seen, f"{path} is claimed twice")
                seen[path] = generator.name


class TheOrderReproducesTheSopTests(unittest.TestCase):
    def test_the_sop_sequence_falls_out_of_the_declared_edges(self) -> None:
        self.assertTrue(
            _contiguous(declaration.order(), TEST_SOP_334),
            f"the derived order does not contain the SOP sequence: {declaration.order()}",
        )

    def test_the_order_is_deterministic(self) -> None:
        self.assertEqual(declaration.order(), declaration.order())

    def test_every_generator_runs_after_everything_it_reads(self) -> None:
        order = declaration.order()
        cycle = set(declaration.CYCLE)
        for generator in declaration.GENERATORS:
            if generator.name in cycle:
                continue  # ordered by CYCLE_SEQUENCE, which the SOP test pins
            position = order.index(generator.name)
            for read in generator.reads:
                if read in cycle:
                    # The cycle is one node: reading any member means running after all of them.
                    required = max(index for index, step in enumerate(order) if step in cycle)
                else:
                    required = order.index(read)
                with self.subTest(generator=generator.name, reads=read):
                    self.assertGreater(
                        position,
                        required,
                        f"{generator.name} runs before {read}, which it reads",
                    )

    def test_the_generator_that_reads_everything_is_last(self) -> None:
        self.assertEqual(declaration.order()[-1], "retirement_eligibility")

    def test_the_bundle_build_appears_once_and_only_inside_the_cycle(self) -> None:
        order = declaration.order()
        self.assertEqual(order.count(declaration.BUNDLE_BUILD), 1)
        position = order.index(declaration.BUNDLE_BUILD)
        self.assertIn(order[position - 1], declaration.CYCLE)
        self.assertIn(order[position + 1], declaration.CYCLE)

    def test_only_the_cycle_runs_a_generator_twice(self) -> None:
        order = declaration.order()
        repeated = {step for step in order if order.count(step) > 1}
        self.assertEqual(repeated, {"build_provenance"})


class TheCycleIsDeclaredNotDiscoveredTests(unittest.TestCase):
    """The three-member cycle is real; a pure topological sort over these edges does not exist."""

    def test_the_declared_cycle_is_genuinely_cyclic(self) -> None:
        known = declaration.by_name()
        for name in declaration.CYCLE:
            others = set(declaration.CYCLE) - {name}
            with self.subTest(generator=name):
                self.assertTrue(
                    others & set(known[name].reads),
                    f"{name} is declared in the cycle but reads no other member",
                )

    def test_removing_the_cycle_break_makes_the_sort_impossible(self) -> None:
        # CRITICAL: this is why `CYCLE_SEQUENCE` exists rather than being a stylistic choice. With
        # the three members left as ordinary nodes the edges do not sort at all, so a future change
        # that "simplifies" the collapse away cannot silently produce a plausible-looking order.
        #
        # Both constants are emptied together, so `validate` still passes and the failure comes
        # from the sort itself. Emptying only `CYCLE` would raise from `validate` instead, and the
        # test would pass while proving nothing about whether the graph is cyclic.
        cycle, sequence = declaration.CYCLE, declaration.CYCLE_SEQUENCE
        try:
            declaration.CYCLE = ()
            declaration.CYCLE_SEQUENCE = ()
            declaration.validate()
            with self.assertRaises(declaration.DeclarationError) as raised:
                declaration.order()
            self.assertIn("do not sort", str(raised.exception))
            stuck = str(raised.exception)
            for name in cycle:
                self.assertIn(name, stuck)
        finally:
            declaration.CYCLE = cycle
            declaration.CYCLE_SEQUENCE = sequence


class TheDeclarationRefusesToContradictItselfTests(unittest.TestCase):
    def test_an_edge_to_an_unknown_generator_is_refused(self) -> None:
        original = declaration.GENERATORS
        broken = replace(original[0], reads=("not_a_generator",))
        try:
            declaration.GENERATORS = (broken, *original[1:])
            with self.assertRaises(declaration.DeclarationError):
                declaration.validate()
        finally:
            declaration.GENERATORS = original

    def test_a_generator_that_reads_itself_is_refused(self) -> None:
        # `self_reading` records that a generator opens its own output, which is a property of the
        # apply. Declaring it as an edge would be a self-loop and is a different, invalid thing.
        original = declaration.GENERATORS
        broken = replace(original[0], reads=(original[0].name,))
        try:
            declaration.GENERATORS = (broken, *original[1:])
            with self.assertRaises(declaration.DeclarationError):
                declaration.validate()
        finally:
            declaration.GENERATORS = original

    def test_a_cycle_sequence_that_does_not_cover_the_cycle_is_refused(self) -> None:
        original = declaration.CYCLE_SEQUENCE
        try:
            declaration.CYCLE_SEQUENCE = ("contract_inventory",)
            with self.assertRaises(declaration.DeclarationError):
                declaration.validate()
        finally:
            declaration.CYCLE_SEQUENCE = original


class TheMeasuredPropertiesAreRecordedTests(unittest.TestCase):
    """The four facts that make this graph different from the obvious model of it."""

    def test_three_generators_own_more_than_one_output(self) -> None:
        multiple = {g.name for g in declaration.GENERATORS if len(g.outputs) > 1}
        self.assertEqual(
            multiple,
            {"authoring_render_schemas", "cross_language_surface", "supply_chain_manifest"},
        )

    def test_one_generator_writes_shipped_python_rather_than_a_contract(self) -> None:
        outputs = declaration.by_name()["core_public_surface"].outputs
        self.assertEqual(outputs, ("comfyui_h3_context/core/__init__.py",))

    def test_the_generators_that_write_into_the_bundle_source_are_named(self) -> None:
        into_frontend = {
            generator.name
            for generator in declaration.GENERATORS
            for path in generator.outputs
            if path.startswith("frontend/src/")
        }
        self.assertEqual(
            into_frontend,
            {
                "cross_language_surface",
                "duration_resolution_contract",
                "localized_identity_registry",
            },
        )

    def test_only_build_provenance_takes_the_repository_position_as_an_input(self) -> None:
        self.assertEqual(
            {g.name for g in declaration.GENERATORS if g.reads_head}, {"build_provenance"}
        )

    def test_the_generators_that_cannot_run_outside_the_repository_are_named(self) -> None:
        self.assertEqual(
            {g.name for g in declaration.GENERATORS if g.locus == "in_repository"},
            {"build_provenance", "closeout_matrix", "m19_closeout"},
        )

    def test_the_one_generator_without_a_check_flag_is_named(self) -> None:
        without = {g.name for g in declaration.GENERATORS if g.check != ("--check",)}
        self.assertEqual(without, {"supply_chain_manifest"})
        self.assertEqual(
            declaration.by_name()["supply_chain_manifest"].check, ("--emit", "validate")
        )


class TheDeclarationMatchesWhatWasMeasuredTests(unittest.TestCase):
    """The declaration is hand-written; this is what stops it drifting from the measurement.

    CRITICAL: without this, deleting a declared edge is invisible. Every other test in this file
    still passes with `fingerprint_domain <- contract_inventory` removed, because a shorter edge
    list still sorts and still contains the SOP sequence -- it just no longer describes the
    repository. The fixture is the recorded output of the audit-hook run named in its `method`
    field, so a genuine change to a generator's inputs is a deliberate fixture update, and a
    careless edit to the declaration is a failure here.
    """

    def setUp(self) -> None:
        self.measured = json.loads(
            (REPO_ROOT / "tests/fixtures/m23_48_derived_artifact_graph_v1.json").read_text(
                encoding="utf-8"
            )
        )

    def test_the_declared_outputs_are_the_measured_outputs(self) -> None:
        declared = {
            generator.name: sorted(generator.outputs) for generator in declaration.GENERATORS
        }
        self.assertEqual(declared, self.measured["outputs"])

    def test_the_declared_edges_are_the_measured_edges(self) -> None:
        declared = {generator.name: sorted(generator.reads) for generator in declaration.GENERATORS}
        self.assertEqual(declared, self.measured["reads"])

    def test_the_declared_self_reading_set_is_the_measured_one(self) -> None:
        declared = sorted(g.name for g in declaration.GENERATORS if g.self_reading)
        self.assertEqual(declared, self.measured["self_reading"])

    def test_the_declared_cycle_is_the_measured_cycle(self) -> None:
        self.assertEqual(sorted(declaration.CYCLE), self.measured["cycle"])

    def test_the_fixture_names_no_private_path_and_records_its_method(self) -> None:
        text = json.dumps(self.measured, ensure_ascii=False)
        for marker in ("C:\\", "B:\\", "/Users/", "/home/", "AppData"):
            self.assertNotIn(marker, text)
        self.assertEqual(set(self.measured["method"]), {"outputs", "edges", "locus"})


class TheTransactionIsAllOrNothingTests(unittest.TestCase):
    """AC 1: a failure anywhere leaves every final path byte-identical.

    These drive `apply_transaction` against a temporary destination root rather than the repository,
    so the test can assert "nothing was written" without being able to write anything real.
    """

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="h3-workspace-"))
        self.destination = Path(tempfile.mkdtemp(prefix="h3-destination-"))
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.addCleanup(shutil.rmtree, self.destination, True)
        self.before: dict[str, bytes] = {}
        for name in ("first.json", "second.json"):
            (self.destination / name).write_bytes(f"final {name}".encode())
            self.before[name] = (self.destination / name).read_bytes()

    def _candidate(self, name: str, payload: bytes) -> declaration.Candidate:
        (self.workspace / name).write_bytes(payload)
        final = self.destination / name
        before = final.read_bytes() if final.is_file() else None
        return declaration.Candidate(
            path=name,
            generator="fake",
            verdict="changed" if before is not None else "created",
            before_sha256=hashlib.sha256(before).hexdigest() if before is not None else None,
            after_sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
        )

    def _unchanged(self) -> None:
        for name, payload in self.before.items():
            self.assertEqual((self.destination / name).read_bytes(), payload, f"{name} was written")

    def test_a_reported_failure_writes_nothing(self) -> None:
        transaction = declaration.Transaction(
            head="0" * 40,
            candidates=(self._candidate("first.json", b"new first"),),
            failures=(("second_generator", "exploded"),),
        )
        with self.assertRaises(declaration.TransactionError):
            declaration.apply_transaction(self.workspace, transaction, root=self.destination)
        self._unchanged()

    def test_a_candidate_that_moved_under_the_transaction_writes_nothing(self) -> None:
        # The planted failure: the first candidate is valid, the second no longer matches what the
        # dry-run measured. Everything is verified before anything is written, so the valid one
        # must not land either.
        first = self._candidate("first.json", b"new first")
        second = self._candidate("second.json", b"new second")
        (self.workspace / "second.json").write_bytes(b"tampered")
        transaction = declaration.Transaction(head="0" * 40, candidates=(first, second))
        with self.assertRaises(declaration.TransactionError):
            declaration.apply_transaction(self.workspace, transaction, root=self.destination)
        self._unchanged()

    def test_a_missing_candidate_writes_nothing(self) -> None:
        first = self._candidate("first.json", b"new first")
        second = self._candidate("second.json", b"new second")
        (self.workspace / "second.json").unlink()
        transaction = declaration.Transaction(head="0" * 40, candidates=(first, second))
        with self.assertRaises(declaration.TransactionError):
            declaration.apply_transaction(self.workspace, transaction, root=self.destination)
        self._unchanged()

    def test_a_clean_transaction_writes_every_changed_candidate_and_no_other(self) -> None:
        first = self._candidate("first.json", b"new first")
        payload = self.before["second.json"]
        unchanged = declaration.Candidate(
            path="second.json",
            generator="fake",
            verdict="unchanged",
            before_sha256=hashlib.sha256(payload).hexdigest(),
            after_sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
        )
        transaction = declaration.Transaction(head="0" * 40, candidates=(first, unchanged))
        written = declaration.apply_transaction(self.workspace, transaction, root=self.destination)
        self.assertEqual(written, ("first.json",))
        self.assertEqual((self.destination / "first.json").read_bytes(), b"new first")
        self.assertEqual((self.destination / "second.json").read_bytes(), payload)

    def test_no_temporary_file_is_left_behind(self) -> None:
        transaction = declaration.Transaction(
            head="0" * 40, candidates=(self._candidate("first.json", b"new first"),)
        )
        declaration.apply_transaction(self.workspace, transaction, root=self.destination)
        leftovers = [p.name for p in self.destination.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_the_apply_never_unlinks_a_final_path(self) -> None:
        # CRITICAL: `core_public_surface` writes the package `__init__` it must itself import, so a
        # window in which that path is absent leaves the repository unimportable. The replacement is
        # a rename over the existing file; an `unlink` in this function would reintroduce that
        # window, and the source is asserted rather than the behaviour because the window is not
        # observable from outside.
        source = (REPO_ROOT / "scripts/derived_artifacts.py").read_text(encoding="utf-8")
        function = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "apply_transaction"
        )
        called = {
            node.func.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        # The prose of the docstring says the same thing; this asserts the code, because a
        # substring search over the source would be satisfied by the sentence that explains it.
        self.assertNotIn("unlink", called)
        self.assertNotIn("rmtree", called)
        self.assertIn("replace", called)


class TheDryRunNamesWhatItImpliesTests(unittest.TestCase):
    """AC 2 and AC 7, on the parts that do not need a sixty-second run."""

    def test_the_bundle_inputs_are_computed_from_imports(self) -> None:
        found = declaration.bundle_input_contracts()
        # The census is the case that matters: it is hand-maintained, has no `--write`, and reads
        # like a governance record, yet `frontend/src` imports it, so it is a bundle input.
        self.assertIn(declaration.CONTRACTS + "host_seam_census_v1.json", found)
        for path in found:
            self.assertTrue((REPO_ROOT / path).is_file())

    def test_the_working_tree_set_is_a_superset_of_the_tracked_set(self) -> None:
        # A dry-run seeded only from `git ls-files` reports "unchanged" for an artifact that a new,
        # unadded file has already made stale. That is the one answer it must never give.
        listed = set(declaration.working_tree_files())
        tracked = {line for line in declaration.git("ls-files").splitlines() if line}
        self.assertTrue(tracked <= listed)
        self.assertIn("scripts/derived_artifacts.py", listed)

    def test_the_receipt_records_the_head_and_no_absolute_path(self) -> None:
        transaction = declaration.Transaction(head="a" * 40, candidates=())
        payload = declaration.receipt(transaction, ())
        self.assertEqual(payload["observed_head"], "a" * 40)
        text = json.dumps(payload, ensure_ascii=False)
        for marker in ("C:\\", "B:\\", "/Users/", "/home/", "AppData"):
            self.assertNotIn(marker, text)
        versions = payload["generator_versions"]
        assert isinstance(versions, dict)
        self.assertEqual(set(versions), set(declaration.by_name()))

    def test_an_empty_transaction_is_clean_and_changes_nothing(self) -> None:
        transaction = declaration.Transaction(head="0" * 40, candidates=())
        self.assertEqual(transaction.changed, ())
        self.assertTrue(transaction.ok)


class ABundleInputNoGeneratorOwnsIsStillWatchedTests(unittest.TestCase):
    """The M23-47 case: a bundle input that is hand-maintained and has no `--write`.

    `dry_run`'s reason loop can only see declared outputs, and three of the four contracts
    `frontend/src` imports are owned by no generator at all. Without a separate comparison a hand
    edit to one of them produces a clean dry-run and a bundle that has silently moved -- which is
    the exact failure this module was written to move earlier than the Full Gate.
    """

    def test_some_bundle_input_is_owned_by_no_generator(self) -> None:
        # If this ever becomes false the branch it justifies is dead code, and the test that pins
        # the branch should be revisited rather than left asserting something vacuous.
        owned = {path for generator in declaration.GENERATORS for path in generator.outputs}
        unowned = set(declaration.bundle_input_contracts()) - owned
        self.assertTrue(unowned, "every bundle input is now generated; revisit the unowned branch")
        self.assertIn(declaration.CONTRACTS + "host_seam_census_v1.json", unowned)

    def test_an_unmodified_file_does_not_differ_from_head(self) -> None:
        paths = declaration.bundle_input_contracts()
        # IMPORTANT: a valid candidate may edit these inputs. Compare against a controlled
        # fixture commit, or legitimate staged census changes make the acceptance suite fail.
        with temporary_repository(dict.fromkeys(paths, b"{}")) as root:
            for path in paths:
                with self.subTest(path=path):
                    self.assertFalse(declaration._differs_from_head(path))  # noqa: SLF001
                    (root / path).write_bytes(b"{ }")
                    self.assertTrue(declaration._differs_from_head(path))  # noqa: SLF001
                    declaration.git("add", "--", path, cwd=root)
                    self.assertTrue(declaration._differs_from_head(path))  # noqa: SLF001

    def test_a_path_absent_from_head_counts_as_differing(self) -> None:
        # A new, unadded bundle input has no committed bytes to compare against; treating that as
        # "unchanged" would be the same silence in a different disguise.
        with temporary_repository({"committed.json": b"{}"}) as root:
            (root / "fresh.json").write_bytes(b"{}")
            self.assertTrue(declaration._differs_from_head("fresh.json"))  # noqa: SLF001
            self.assertFalse(declaration._differs_from_head("committed.json"))  # noqa: SLF001
            (root / "committed.json").write_bytes(b"{ }")
            self.assertTrue(declaration._differs_from_head("committed.json"))  # noqa: SLF001

    def test_an_absent_path_is_not_reported_as_a_change(self) -> None:
        with temporary_repository({"committed.json": b"{}"}):
            self.assertFalse(
                declaration._differs_from_head("absent.json")  # noqa: SLF001
            )

    def test_an_unowned_input_that_differs_is_reported(self) -> None:
        # CRITICAL: the case an earlier review found missing. Two rewrites reintroduce it while
        # looking almost identical -- iterating the owned paths, or the candidates -- and both
        # leave every other reason working, so only this assertion separates them.
        census = declaration.CONTRACTS + "host_seam_census_v1.json"
        reasons = declaration.bundle_rebuild_reasons(
            [
                _candidate(
                    "comfyui_h3_context/contracts/contract_inventory_v1.json",
                    "contract_inventory",
                    "unchanged",
                )
            ],
            {census},
            head="a" * 40,
            recorded_head="a" * 40,
            differs_from_head=lambda path: path == census,
        )
        self.assertEqual(len(reasons), 1)
        self.assertIn(census, reasons[0])
        self.assertIn("owned by no generator", reasons[0])

    def test_an_unowned_input_that_matches_head_is_not_reported(self) -> None:
        reasons = declaration.bundle_rebuild_reasons(
            [],
            {declaration.CONTRACTS + "host_seam_census_v1.json"},
            head="a" * 40,
            recorded_head="a" * 40,
            differs_from_head=lambda _path: False,
        )
        self.assertEqual(reasons, ())

    def test_the_recorded_head_is_read_from_the_provenance_and_fails_soft(self) -> None:
        # The other half of criterion 7: the comparison above is only as good as the value fed
        # into it, and this reader is the only producer of that value. It must return None rather
        # than raise for every shape of unreadable provenance, because a dry-run that cannot tell
        # whether HEAD moved must still run and report everything else.
        contracts = "comfyui_h3_context/contracts/build_provenance_v1.json"
        with temporary_repository({"placeholder.txt": b"x"}) as root:
            self.assertIsNone(declaration._recorded_head())  # noqa: SLF001
            target = root / contracts
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                json.dumps({"external_parameters": {"source_commit": "c" * 40}}).encode("utf-8")
            )
            self.assertEqual(declaration._recorded_head(), "c" * 40)  # noqa: SLF001
            for unreadable in (
                b"not json",
                b"{}",
                b'{"external_parameters": {}}',
                b'{"external_parameters": {"source_commit": 7}}',
            ):
                with self.subTest(payload=unreadable[:24]):
                    target.write_bytes(unreadable)
                    self.assertIsNone(declaration._recorded_head())  # noqa: SLF001

    def test_a_changed_provenance_is_reported_even_though_no_module_imports_it(self) -> None:
        # CRITICAL: M23-46's route, and the one that looks like an oversight rather than a rule.
        # No `frontend/src` module imports the provenance, so neither the path prefix nor the
        # import scan reaches it, and `frontend/buildProvenance.ts` compiles it in regardless.
        # Dropping this branch passes every other assertion in this module.
        provenance = declaration.CONTRACTS + "build_provenance_v1.json"
        reasons = declaration.bundle_rebuild_reasons(
            [_candidate(provenance, "build_provenance", "changed")],
            set(),  # deliberately not a declared bundle input: the branch is the only route
            head="a" * 40,
            recorded_head="a" * 40,
            differs_from_head=lambda _path: False,
        )
        self.assertEqual(len(reasons), 1)
        self.assertIn("compiles its embedded identity in", reasons[0])

    def test_a_moved_head_is_reported_with_no_file_input_changed(self) -> None:
        # Acceptance criterion 7's positive half: the bundle's embedded identity moves on the
        # repository's own position alone, with every file input unchanged. The negative half --
        # nothing reported when neither has moved -- is the test above this one.
        reasons = declaration.bundle_rebuild_reasons(
            [],
            set(),
            head="b" * 40,
            recorded_head="a" * 40,
            differs_from_head=lambda _path: False,
        )
        self.assertEqual(len(reasons), 1)
        self.assertIn("identity would move", reasons[0])

    def test_an_owned_input_is_not_consulted_a_second_time(self) -> None:
        # A generated bundle input is reported once, by its candidate, and must not also be
        # compared against HEAD -- that would double-report every ordinary regeneration.
        owned = declaration.CONTRACTS + "cross_language_surface_v1.json"
        reasons = declaration.bundle_rebuild_reasons(
            [_candidate(owned, "cross_language_surface", "changed")],
            {owned},
            head="a" * 40,
            recorded_head="a" * 40,
            differs_from_head=lambda _path: self.fail("an owned input was compared against HEAD"),
        )
        self.assertEqual(len(reasons), 1)
        self.assertIn("is a bundle input and changed", reasons[0])


class TheWorkspaceMirrorsTheTreeUnderTestTests(unittest.TestCase):
    """The workspace has to be the tree under test to git, not only to the filesystem.

    Not every generator finds its inputs by walking directories. `packaged_artifact_readership`
    runs `git grep`, which searches tracked files, so a clone whose index still describes `HEAD`
    hides every file the candidate adds and keeps every file it removes. The dry-run then answers
    for a tree nobody has, and an apply copies that answer over correct final bytes.
    """

    FILES = {
        "keep.txt": b"keep",
        "doomed.txt": b"doomed",
        "nested/file.txt": b"nested",
    }

    @contextmanager
    def _seeded(self, root: Path) -> Iterator[Path]:
        holder = Path(tempfile.mkdtemp(prefix="m23-48-seed-")).resolve()
        try:
            destination = holder / "tree"
            declaration._seed(destination)  # noqa: SLF001
            yield destination
        finally:
            shutil.rmtree(holder, ignore_errors=True)

    def test_a_file_absent_from_head_is_tracked_in_the_workspace(self) -> None:
        with temporary_repository(self.FILES) as root:
            (root / "added.txt").write_bytes(b"added")
            with self._seeded(root) as destination:
                self.assertEqual(
                    _workspace_tracked(destination),
                    {"added.txt", "doomed.txt", "keep.txt", "nested/file.txt"},
                )
                self.assertEqual((destination / "added.txt").read_bytes(), b"added")

    def test_a_staged_deletion_is_not_carried_into_the_workspace(self) -> None:
        # CRITICAL: an overlay can add and replace but never remove, so a clone that checks out
        # HEAD keeps every file the candidate deletes. M23-55 moves 129 files -- a delete plus an
        # add each -- so every generator would run against a tree that still has all of them, and
        # `apply` would write that answer over the correct bytes.
        with temporary_repository(self.FILES) as root:
            subprocess.run(["git", "rm", "--quiet", "doomed.txt"], cwd=root, check=True)
            with self._seeded(root) as destination:
                self.assertEqual(_workspace_tracked(destination), {"keep.txt", "nested/file.txt"})
                self.assertFalse((destination / "doomed.txt").exists())

    def test_an_unstaged_deletion_is_refused_rather_than_guessed(self) -> None:
        # `git commit` would keep the file and `git commit -a` would drop it, so which tree the
        # transaction is about is genuinely unknown. Before this it was an uncaught
        # FileNotFoundError from inside a copy loop.
        with temporary_repository(self.FILES) as root:
            (root / "doomed.txt").unlink()
            holder = Path(tempfile.mkdtemp(prefix="m23-48-refuse-")).resolve()
            try:
                with self.assertRaises(declaration.TransactionError) as raised:
                    declaration._seed(holder / "tree")  # noqa: SLF001
                self.assertIn("doomed.txt", str(raised.exception))
                self.assertIn("stage the deletion", str(raised.exception))
            finally:
                shutil.rmtree(holder, ignore_errors=True)

    def test_seeding_twice_into_the_same_path_succeeds(self) -> None:
        # The regression: the first dry-run left the workspace behind because removing a git
        # clone's read-only objects failed silently, and the second run then died in `_seed`.
        with temporary_repository(self.FILES):
            holder = Path(tempfile.mkdtemp(prefix="m23-48-reseed-")).resolve()
            try:
                destination = holder / "tree"
                declaration._seed(destination)  # noqa: SLF001
                declaration._seed(destination)  # noqa: SLF001
                self.assertTrue((destination / ".git").is_dir())
                self.assertEqual(_workspace_tracked(destination), set(self.FILES))
            finally:
                shutil.rmtree(holder, ignore_errors=True)

    def test_a_read_only_file_does_not_block_removal(self) -> None:
        # CRITICAL: this is the exact shape git leaves behind. `shutil.rmtree` raises WinError 5 on
        # it, and the tempting repair -- `ignore_errors=True` -- converts a loud failure into a
        # workspace that silently survives and breaks the next run instead.
        workspace = Path(tempfile.mkdtemp(prefix="m23-48-readonly-")).resolve()
        nested = workspace / "objects" / "11"
        nested.mkdir(parents=True)
        target = nested / "47f84b"
        target.write_bytes(b"object")
        target.chmod(stat.S_IREAD)
        try:
            declaration._remove_tree(workspace)  # noqa: SLF001
            self.assertFalse(workspace.exists())
        finally:
            if workspace.exists():  # pragma: no cover - only on a failing run
                target.chmod(stat.S_IWRITE)
                shutil.rmtree(workspace, ignore_errors=True)

    def test_the_removal_refuses_anything_inside_the_repository(self) -> None:
        # CRITICAL: driven against a temporary repository, never the real one. A weakened guard
        # makes this test chmod every file in the tree writable and then delete it -- .git,
        # ignored planning state and all -- so the fixture is what has to be at risk here.
        with temporary_repository(self.FILES) as root:
            for refused in (root, root.parent, root / "nested"):
                with self.subTest(path=refused.name):
                    with self.assertRaises(declaration.TransactionError):
                        declaration._remove_tree(refused)  # noqa: SLF001
            self.assertTrue((root / "keep.txt").is_file())
            self.assertTrue((root / "nested" / "file.txt").is_file())

    def test_a_link_at_the_removal_path_is_refused_rather_than_followed(self) -> None:
        # CRITICAL: this is the regression the traversal fix nearly introduced. `resolve()` is
        # what makes the whitelist correct, and it is also what hands `shutil.rmtree` a junction's
        # target -- `rmtree` refuses a junction and deletes a real directory happily, so resolving
        # first turns a safe refusal into a silent delete of whatever the link points at. `.tmp/`
        # is where this repository keeps its worktrees. The refusal must come before the resolve.
        #
        # The target is deliberately OUTSIDE the repository. Pointing it inside would let the
        # whitelist refuse the resolved path for an unrelated reason, and the test would then pass
        # with the link check deleted -- which is exactly what it must not do.
        outside = Path(tempfile.mkdtemp(prefix="m23-48-target-")).resolve()
        (outside / "precious.txt").write_bytes(b"precious")
        try:
            with temporary_repository(self.FILES) as root:
                (root / ".tmp").mkdir()
                link = root / ".tmp" / "workspace"
                if not _link_directory(link, outside):
                    self.skipTest("this environment cannot create a directory link")
                with self.assertRaises(declaration.TransactionError):
                    declaration._remove_tree(link)  # noqa: SLF001
                self.assertTrue((outside / "precious.txt").is_file())
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_a_link_inside_the_removed_tree_is_not_walked_through(self) -> None:
        # CRITICAL: asserted at the call site, not on the walk helper, because swapping the helper
        # back for `Path.rglob` leaves the helper's own test green. What `rglob` would do here is
        # clear the read-only bit on a file outside the tree being removed, so the observable
        # difference is that bit -- not the file's existence, which `shutil.rmtree` preserves on
        # this interpreter either way.
        outside = Path(tempfile.mkdtemp(prefix="m23-48-outside-")).resolve()
        guarded = outside / "read-only.txt"
        guarded.write_bytes(b"guarded")
        guarded.chmod(stat.S_IREAD)
        try:
            with temporary_repository(self.FILES) as root:
                scratch = root / ".tmp" / "derived-artifacts"
                scratch.mkdir(parents=True)
                (scratch / "own.txt").write_bytes(b"own")
                if not _link_directory(scratch / "escape", outside):
                    self.skipTest("this environment cannot create a directory link")
                declaration._remove_tree(scratch, tolerant=True)  # noqa: SLF001
                self.assertTrue(guarded.is_file())
                still_read_only = not (guarded.stat().st_mode & stat.S_IWRITE)
                self.assertTrue(
                    still_read_only, "the removal walked through the link and chmodded outside it"
                )
        finally:
            guarded.chmod(stat.S_IWRITE)
            shutil.rmtree(outside, ignore_errors=True)

    def test_a_junction_is_recognised_although_it_is_not_a_symlink(self) -> None:
        # `Path.is_symlink()` is False for a Windows junction, which is precisely why the check
        # above cannot be written with it. Without this the guard passes for the wrong reason on
        # any platform where the link happens to be a real symlink.
        with temporary_repository(self.FILES) as root:
            link = root / "link"
            if not _link_directory(link, root / "nested"):
                self.skipTest("this environment cannot create a directory link")
            self.assertTrue(declaration._is_link(link))  # noqa: SLF001
            self.assertFalse(declaration._is_link(root / "nested"))  # noqa: SLF001
            self.assertFalse(declaration._is_link(root / "keep.txt"))  # noqa: SLF001

    def test_an_unreadable_path_answers_link_rather_than_walkable(self) -> None:
        # CRITICAL: the `except OSError` in `_is_link` must answer True. Both callers act on
        # "not a link" by *entering* the path -- `_real_files_under` descends into it and
        # `_remove_tree` deletes it -- so answering False for a path the tool could not stat turns
        # an entry that vanished between `os.scandir` and the check into a walk or a delete of
        # something never inspected. Flipping the branch to `return False` leaves every other test
        # in this module green, so the direction is pinned here rather than left to the callers.
        #
        # The errno matters: `Path.is_symlink()` re-raises anything `pathlib` does not classify as
        # "the path is simply not there", so the branch is reachable only for an ignorable error
        # -- `is_symlink()` answers False and the explicit `lstat()` right after it raises.
        vanished = OSError(errno.ENOENT, "vanished between the scan and the check")
        with temporary_repository(self.FILES) as root:
            with patch.object(Path, "lstat", side_effect=vanished):
                self.assertTrue(declaration._is_link(root / "keep.txt"))  # noqa: SLF001

    def test_the_walk_does_not_descend_through_a_link(self) -> None:
        # Clearing read-only bits by globbing would chmod files on the far side of a junction,
        # outside the tree being removed.
        with temporary_repository(self.FILES) as root:
            outside = root / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_bytes(b"secret")
            tree = root / "tree"
            tree.mkdir()
            (tree / "own.txt").write_bytes(b"own")
            if not _link_directory(tree / "escape", outside):
                self.skipTest("this environment cannot create a directory link")
            found = {p.name for p in declaration._real_files_under(tree)}  # noqa: SLF001
            self.assertEqual(found, {"own.txt"})

    def test_a_traversal_out_of_the_scratch_directory_is_refused(self) -> None:
        # CRITICAL: `Path.parents` compares lexically, so `.tmp/../comfyui_h3_context` reads as
        # "inside the scratch directory" to a whitelist that does not resolve first, and the
        # removal would then take a shipped package directory. Traversal is named in AGENTS.md
        # section 9 for exactly this shape.
        with temporary_repository(self.FILES) as root:
            (root / ".tmp").mkdir()
            escaping = root / ".tmp" / ".." / "nested"
            with self.assertRaises(declaration.TransactionError):
                declaration._remove_tree(escaping)  # noqa: SLF001
            self.assertTrue((root / "nested" / "file.txt").is_file())

    def test_the_removal_allows_the_scratch_directory(self) -> None:
        with temporary_repository(self.FILES) as root:
            scratch = root / ".tmp" / "derived-artifacts"
            scratch.mkdir(parents=True)
            (scratch / "file.txt").write_bytes(b"scratch")
            declaration._remove_tree(scratch)  # noqa: SLF001
            self.assertFalse(scratch.exists())

    def test_the_seed_checks_out_nothing_and_stages_after_it_copies(self) -> None:
        # CRITICAL: both halves are invisible at a glance. Without `--no-checkout` the clone
        # materializes HEAD and a deletion survives; staging before the copy records HEAD instead
        # of the candidate. Order and flag together are what make the workspace the tree under
        # test rather than an approximation of it.
        source = (REPO_ROOT / "scripts/derived_artifacts.py").read_text(encoding="utf-8")
        function = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_seed"
        )
        body = ast.unparse(function)
        self.assertIn("'--no-checkout'", body)
        self.assertIn("'git', 'add', '--all'", body)
        self.assertLess(body.index("shutil.copyfile"), body.index("'git', 'add', '--all'"))


class TheDescriptionIsSerialisableTests(unittest.TestCase):
    def test_describe_round_trips_through_json_and_names_no_private_path(self) -> None:
        payload = declaration.describe()
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertEqual(json.loads(text), payload)
        for marker in ("C:\\\\", "B:\\\\", "/Users/", "/home/"):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
