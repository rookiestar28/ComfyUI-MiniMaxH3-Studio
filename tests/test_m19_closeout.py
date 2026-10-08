"""M19-06 closeout matrix tests.

Two things are worth pinning here.

The matrix is a check, not a table. A row carries an expectation, and a `constant` row whose value
moved reports `broken`. These tests exercise that machinery on synthetic rows rather than trusting
that the real matrix happens to be green — a matrix that could not go red would be decoration.

The real artifact is then held to the substance: the chain's structural invariants held at every
checkpoint, the host-observable surface never moved once it existed, and no contract identity the
baseline knew about was lost.

No fixture here carries a prompt, a media value, a private path or a credential.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from history_fixture import commit, git_history, require_history, write_json

from scripts.governance.m19_closeout import (
    ABSENT,
    M19_CLOSEOUT_SCHEMA,
    Checkpoint,
    CloseoutRow,
    M19Closeout,
    M19CloseoutError,
    Reading,
    RowExpectation,
    RowVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "governance" / "contracts" / "m19_closeout_v1.json"
ARTIFACT_SCHEMA = REPO_ROOT / "governance" / "contracts" / "m19_closeout_v1.schema.json"


def _load_generator() -> Any:
    path = REPO_ROOT / "scripts" / "m19_closeout.py"
    spec = importlib.util.spec_from_file_location("_m19_06_closeout", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR: Any = _load_generator()


def _row(values: list[str], expectation: RowExpectation, introduced: str = "M19-01") -> CloseoutRow:
    items = ["M19-01", "M19-02", "M19-03"]
    return CloseoutRow(
        domain="example",
        metric="example_metric",
        source="comfyui_h3_context/contracts/example_v1.json",
        introduced_by=introduced,
        expectation=expectation,
        readings=tuple(
            Reading(item=item, value=value) for item, value in zip(items, values, strict=True)
        ),
    )


class RowVerdictTests(unittest.TestCase):
    """The machinery that makes the matrix able to go red."""

    def test_a_constant_row_that_did_not_move_held(self) -> None:
        row = _row(["7", "7", "7"], RowExpectation.CONSTANT)
        self.assertIs(row.verdict, RowVerdict.HELD)

    def test_a_constant_row_that_moved_is_broken(self) -> None:
        row = _row(["7", "7", "8"], RowExpectation.CONSTANT)
        self.assertIs(row.verdict, RowVerdict.BROKEN)

    def test_an_informational_row_claims_no_verdict_either_way(self) -> None:
        self.assertIs(
            _row(["1", "2", "3"], RowExpectation.INFORMATIONAL).verdict, RowVerdict.REPORTED
        )
        self.assertIs(
            _row(["1", "1", "1"], RowExpectation.INFORMATIONAL).verdict, RowVerdict.REPORTED
        )

    def test_absent_is_not_a_reading_and_does_not_break_a_constant_row(self) -> None:
        """An artifact that did not exist yet has no value, which is not a different value."""

        row = _row([ABSENT, "7", "7"], RowExpectation.CONSTANT, introduced="M19-02")
        self.assertIs(row.verdict, RowVerdict.HELD)
        self.assertEqual(row.observed, ("7", "7"))

    def test_a_row_may_not_move_its_introduction_point_to_excuse_a_change(self) -> None:
        """Otherwise a value that moved could be relabelled as having started later."""

        with self.assertRaises(M19CloseoutError):
            _row(["7", "8", "8"], RowExpectation.CONSTANT, introduced="M19-02")

    def test_a_row_that_reads_nothing_anywhere_is_refused(self) -> None:
        with self.assertRaises(M19CloseoutError):
            _row([ABSENT, ABSENT, ABSENT], RowExpectation.CONSTANT)

    def test_a_reading_may_not_be_free_text(self) -> None:
        with self.assertRaises(M19CloseoutError):
            Reading(item="M19-01", value="B:/private/path")

    def test_a_checkpoint_may_be_read_once_per_row(self) -> None:
        with self.assertRaises(M19CloseoutError):
            CloseoutRow(
                domain="example",
                metric="example_metric",
                source="a.json",
                introduced_by="M19-01",
                expectation=RowExpectation.CONSTANT,
                readings=(Reading(item="M19-01", value="1"), Reading(item="M19-01", value="1")),
            )


class MatrixGuardTests(unittest.TestCase):
    def test_every_row_must_read_every_checkpoint_in_order(self) -> None:
        checkpoints = (
            Checkpoint(item="M19-01", commit="0" * 40, summary="first"),
            Checkpoint(item="M19-02", commit="1" * 40, summary="second"),
        )
        short = CloseoutRow(
            domain="example",
            metric="example_metric",
            source="a.json",
            introduced_by="M19-01",
            expectation=RowExpectation.CONSTANT,
            readings=(Reading(item="M19-01", value="1"),),
        )
        with self.assertRaises(M19CloseoutError):
            M19Closeout(checkpoints=checkpoints, rows=(short,))

    def test_checkpoints_must_be_in_chain_order(self) -> None:
        out_of_order = (
            Checkpoint(item="M19-02", commit="1" * 40, summary="second"),
            Checkpoint(item="M19-01", commit="0" * 40, summary="first"),
        )
        with self.assertRaises(M19CloseoutError):
            M19Closeout(
                checkpoints=out_of_order, rows=(_row(["1", "1", "1"], RowExpectation.CONSTANT),)
            )


class GeneratedMatrixTests(unittest.TestCase):
    document: dict[str, Any]
    closeout: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        # CRITICAL: public snapshots lack maintainer checkpoints. Keep artifact validation
        # independent of historical replay, or one missing object disables every assertion.
        cls.closeout = M19Closeout(
            checkpoints=tuple(Checkpoint(**row) for row in cls.document["checkpoints"]),
            rows=tuple(
                CloseoutRow(
                    domain=row["domain"],
                    metric=row["metric"],
                    source=row["source"],
                    introduced_by=row["introduced_by"],
                    expectation=RowExpectation(row["expectation"]),
                    readings=tuple(Reading(**reading) for reading in row["readings"]),
                )
                for row in cls.document["rows"]
            ),
        )

    def test_the_artifact_round_trips_with_recomputed_verdicts(self) -> None:
        self.assertEqual(
            GENERATOR.artifact_bytes(self.closeout),
            ARTIFACT.read_bytes().replace(b"\r\n", b"\n"),
        )

    def test_the_record_and_its_fingerprint_agree(self) -> None:
        self.assertEqual(self.document["schema"], M19_CLOSEOUT_SCHEMA)
        self.assertEqual(self.document["fingerprint"], self.closeout.fingerprint)

    def test_the_artifact_validates_against_its_schema(self) -> None:
        import jsonschema

        schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_the_chain_is_the_five_accepted_items(self) -> None:
        items = [checkpoint["item"] for checkpoint in self.document["checkpoints"]]
        self.assertEqual(items, ["M19-01", "M19-02", "M19-03", "M19-04", "M19-05"])

    def test_nothing_the_chain_had_to_hold_moved(self) -> None:
        """The closeout's whole point. A broken row names an item that changed what it must not."""

        self.assertEqual([row.metric for row in self.closeout.broken], [])

    def test_the_structural_invariants_were_clean_at_every_checkpoint(self) -> None:
        for metric in ("forbidden_imports", "layer_inversions", "runtime_cycles"):
            with self.subTest(metric=metric):
                row = self._row(metric)
                self.assertEqual(set(row["readings"][index]["value"] for index in range(5)), {"0"})

    def test_the_host_observable_surface_never_moved_once_it_existed(self) -> None:
        """M19-03 built it precisely so M19-04 and M19-05 could not change it unnoticed."""

        row = self._row("node_surface_fingerprint")
        self.assertEqual(row["verdict"], "held")
        observed = [entry["value"] for entry in row["readings"] if entry["value"] != ABSENT]
        self.assertEqual(len(observed), 3)
        self.assertTrue(observed[0].startswith("sha256:"))

    def test_no_contract_identity_the_baseline_knew_about_was_lost(self) -> None:
        row = self._row("baseline_identities_retained")
        self.assertEqual(row["verdict"], "held")
        self.assertEqual(len({entry["value"] for entry in row["readings"]}), 1)

    def test_the_bundle_moved_exactly_once_and_the_item_is_named(self) -> None:
        """M19-04 records why: a changed module graph reallocates minified identifiers."""

        values = [entry["value"] for entry in self._row("bundle_sha256")["readings"]]
        self.assertEqual(len(set(values)), 2)
        self.assertEqual(values[2], values[1])
        self.assertNotEqual(values[3], values[2])
        self.assertEqual(values[4], values[3])

    def test_the_frontend_cohort_and_what_it_left_are_reported_separately(self) -> None:
        """AC-M19-04-04 was corrected for exactly this reason; the matrix keeps both readable."""

        cohort = [entry["value"] for entry in self._row("largest_graph_module_bytes")["readings"]]
        directory = [entry["value"] for entry in self._row("largest_host_module_bytes")["readings"]]
        self.assertGreater(int(cohort[0]), int(cohort[4]))
        self.assertEqual(len(set(directory)), 1)

    def test_the_record_carries_identifiers_and_counts_and_nothing_else(self) -> None:
        raw = ARTIFACT.read_text(encoding="utf-8")
        for forbidden in ("password", "token", "secret", "Bearer ", "://user:"):
            self.assertNotIn(forbidden, raw)
        self.assertNotIn(":\\", raw)
        self.assertNotIn("C:/", raw)

    def _row(self, metric: str) -> dict[str, Any]:
        for row in self.document["rows"]:
            if row["metric"] == metric:
                return dict(row)
        raise AssertionError(f"no row named {metric}")


class HistoricalReplayTests(unittest.TestCase):
    def test_the_accepted_history_regenerates_byte_identically(self) -> None:
        require_history(REPO_ROOT, [row[1] for row in GENERATOR.CHECKPOINTS])
        self.assertEqual(
            GENERATOR.artifact_bytes(GENERATOR.build_closeout()),
            ARTIFACT.read_bytes().replace(b"\r\n", b"\n"),
        )


class SyntheticHistoryTests(unittest.TestCase):
    def test_real_commit_reads_detect_changed_invariants_and_missing_objects(self) -> None:
        with git_history() as root:
            checkpoints = []
            for index in range(1, 6):
                write_json(
                    root,
                    GENERATOR.ARCHITECTURE,
                    {
                        "forbidden_imports": ["synthetic"] if index == 5 else [],
                        "layer_inversions": [],
                        "cycles": [],
                        "modules": [{"module": "example", "lines": 10}],
                    },
                )
                write_json(
                    root,
                    GENERATOR.CONTRACTS,
                    {
                        "entries": [{"contract_id": "first"}]
                        if index == 5
                        else [{"contract_id": "first"}, {"contract_id": "second"}],
                    },
                )
                write_json(root, GENERATOR.BASELINE, {"criteria": [1], "public_python_abi": [1]})
                write_json(root, GENERATOR.BUNDLE, {"bundle": 1 if index < 4 else 2})
                write_json(root, "frontend/src/host/graph.ts", "x" * (10 if index < 4 else 5))
                write_json(root, "frontend/src/host/app.ts", "x" * 20)
                if index >= 2:
                    write_json(
                        root,
                        GENERATOR.SURFACE,
                        {
                            "authorities": [
                                {
                                    "level": "pure_core",
                                    "declared": 1,
                                    "names_digest": "sha256:" + "1" * 64,
                                }
                            ],
                            "shadowed": [],
                        },
                    )
                if index >= 3:
                    write_json(
                        root,
                        GENERATOR.NODES,
                        {
                            "fingerprint": "sha256:" + "2" * 64,
                            "nodes": [1],
                            "exported_names": [1],
                        },
                    )
                checkpoints.append((f"M19-{index:02}", commit(root), "synthetic checkpoint"))
            with (
                patch.object(GENERATOR, "ROOT", root),
                patch.object(GENERATOR, "CHECKPOINTS", tuple(checkpoints)),
            ):
                result = GENERATOR.build_closeout()
                rows = {row.metric: row for row in result.rows}
                self.assertEqual(
                    {row.metric for row in result.broken},
                    {"forbidden_imports", "baseline_identities_retained"},
                )
                self.assertEqual(rows["node_surface_fingerprint"].readings[0].value, ABSENT)
                self.assertEqual(rows["node_surface_fingerprint"].verdict, RowVerdict.HELD)
                self.assertEqual(rows["baseline_identities_retained"].readings[-1].value, "1")
                self.assertEqual(len(set(rows["bundle_sha256"].observed)), 2)
                self.assertIsNone(GENERATOR._blob(checkpoints[0][1], "absent.json"))
                missing = (("M19-01", "0" * 40, "missing checkpoint"), *checkpoints[1:])
                with patch.object(GENERATOR, "CHECKPOINTS", missing):
                    with self.assertRaises(subprocess.CalledProcessError):
                        GENERATOR.build_closeout()

    def test_public_artifact_checks_do_not_read_history(self) -> None:
        with patch.object(GENERATOR, "_git", side_effect=AssertionError("history unavailable")):
            GeneratedMatrixTests.setUpClass()
            self.assertEqual(
                GeneratedMatrixTests.closeout.fingerprint,
                GeneratedMatrixTests.document["fingerprint"],
            )


if __name__ == "__main__":
    unittest.main()
