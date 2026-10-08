"""M18-06 closeout matrix tests.

A closeout record is only worth having if it can be wrong. These tests are mostly about the ways it
must refuse to be written: a probe that claims an execution identity depends on a render, a mutation
that leaks across domains, a semantic change that moves no semantic identity, a mutation that moves
nothing at all.

The last one matters more than it looks. Separation is trivially satisfied by a matrix where nothing
ever happens, so "every mutation moves something" is what stops the other three invariants from
being decoration.

No fixture here carries a prompt, a media value, a private path or a credential.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from history_fixture import commit, git_history, require_history, write_json

from comfyui_h3_context.core.fingerprint_domain import IdentityDomain
from scripts.governance.closeout_matrix import (
    CLOSEOUT_MATRIX_SCHEMA,
    BlastRadiusRow,
    CloseoutMatrix,
    CloseoutMatrixError,
    CloseoutMetric,
    Probe,
    observe_blast_radius,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "governance" / "contracts" / "closeout_matrix_v1.json"
ARTIFACT_SCHEMA = REPO_ROOT / "governance" / "contracts" / "closeout_matrix_v1.schema.json"

COMMIT = "0" * 40


def _load_generator() -> Any:
    name = "m18_06_closeout_matrix_generator"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "closeout_matrix.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR: Any = _load_generator()

SEMANTIC_PROBE = Probe(
    probe_id="semantic_probe",
    domain=IdentityDomain.SEMANTIC,
    authority="an execution identity",
)
RENDER_PROBE = Probe(
    probe_id="render_probe",
    domain=IdentityDomain.PRESENTATION,
    depends_on=(IdentityDomain.SEMANTIC,),
    authority="a stale-render identity",
)
FIXTURE_PROBE = Probe(
    probe_id="fixture_probe",
    domain=IdentityDomain.FIXTURE,
    authority="a fixture identity",
)
WIRE_PROBE = Probe(
    probe_id="wire_probe",
    domain=IdentityDomain.WIRE_CONTRACT,
    authority="a contract shape identity",
)
RELEASE_PROBE = Probe(
    probe_id="release_probe",
    domain=IdentityDomain.RELEASE_INTEGRITY,
    authority="a bundle identity",
)
ALL_PROBES = (FIXTURE_PROBE, RELEASE_PROBE, RENDER_PROBE, SEMANTIC_PROBE, WIRE_PROBE)


def _metric(**overrides: object) -> CloseoutMetric:
    base: dict[str, object] = {
        "metric_id": "example_metric",
        "unit": "contracts",
        "baseline": 10,
        "final": 4,
        "baseline_commit": COMMIT,
        "final_commit": COMMIT,
        "evidence": "an example measurement",
    }
    base.update(overrides)
    return CloseoutMetric(**base)  # type: ignore[arg-type]


def _row(domain: IdentityDomain, moved: tuple[str, ...], mutation_id: str) -> BlastRadiusRow:
    names = {probe.probe_id for probe in ALL_PROBES}
    return BlastRadiusRow(
        mutation_id=mutation_id,
        domain=domain,
        moved=tuple(sorted(moved)),
        unmoved=tuple(sorted(names - set(moved))),
        evidence="an example observation",
    )


def _full_matrix(rows: tuple[BlastRadiusRow, ...]) -> CloseoutMatrix:
    return CloseoutMatrix(
        probes=tuple(sorted(ALL_PROBES, key=lambda probe: probe.probe_id)),
        metrics=(_metric(),),
        rows=tuple(sorted(rows, key=lambda row: row.mutation_id)),
    )


def _every_domain_covered() -> tuple[BlastRadiusRow, ...]:
    return (
        _row(IdentityDomain.FIXTURE, ("fixture_probe",), "a_fixture_change"),
        _row(IdentityDomain.PRESENTATION, ("render_probe",), "b_render_change"),
        _row(IdentityDomain.RELEASE_INTEGRITY, ("release_probe",), "c_release_change"),
        _row(
            IdentityDomain.SEMANTIC,
            ("render_probe", "semantic_probe"),
            "d_semantic_change",
        ),
        _row(IdentityDomain.WIRE_CONTRACT, ("wire_probe",), "e_wire_change"),
    )


class ProbeTests(unittest.TestCase):
    """Invariant 1 is a property of the identity, so it is refused before any mutation runs."""

    def test_an_execution_identity_may_not_claim_a_non_semantic_dependency(self) -> None:
        for dependency in (
            IdentityDomain.PRESENTATION,
            IdentityDomain.FIXTURE,
            IdentityDomain.RELEASE_INTEGRITY,
            IdentityDomain.WIRE_CONTRACT,
        ):
            with self.subTest(dependency=dependency.value):
                with self.assertRaises(CloseoutMatrixError):
                    Probe(
                        probe_id="semantic_probe",
                        domain=IdentityDomain.SEMANTIC,
                        depends_on=(dependency,),
                        authority="an execution identity that should not exist",
                    )

    def test_a_render_identity_may_declare_the_thing_it_renders(self) -> None:
        self.assertEqual(
            RENDER_PROBE.inputs,
            frozenset({IdentityDomain.PRESENTATION, IdentityDomain.SEMANTIC}),
        )

    def test_a_probe_may_not_restate_its_own_domain_as_a_dependency(self) -> None:
        with self.assertRaises(CloseoutMatrixError):
            Probe(
                probe_id="fixture_probe",
                domain=IdentityDomain.FIXTURE,
                depends_on=(IdentityDomain.FIXTURE,),
                authority="a fixture identity",
            )


class BlastRadiusTests(unittest.TestCase):
    def test_a_mutation_that_moves_nothing_is_refused(self) -> None:
        """Invariant 4. Without it, the other three are satisfied by a matrix of inert probes."""

        with self.assertRaises(CloseoutMatrixError):
            _row(IdentityDomain.FIXTURE, (), "a_mutation_that_observes_nothing")

    def test_a_mutation_that_moves_a_probe_outside_its_declared_inputs_is_refused(self) -> None:
        """Invariant 2, in the direction that matters: a fixture change reaching an execution id."""

        leaking = _row(
            IdentityDomain.FIXTURE, ("fixture_probe", "semantic_probe"), "a_leaking_change"
        )
        with self.assertRaises(CloseoutMatrixError) as failure:
            _full_matrix((leaking, *_every_domain_covered()[1:]))
        self.assertIn("semantic_probe", str(failure.exception))

    def test_a_semantic_mutation_that_moves_no_semantic_identity_is_refused(self) -> None:
        """Invariant 3. A cache identity ignoring a meaning change authorises the wrong reuse."""

        underbound = _row(IdentityDomain.SEMANTIC, ("render_probe",), "d_semantic_change")
        rows = tuple(
            underbound if row.mutation_id == "d_semantic_change" else row
            for row in _every_domain_covered()
        )
        with self.assertRaises(CloseoutMatrixError) as failure:
            _full_matrix(rows)
        self.assertIn("no semantic identity", str(failure.exception))

    def test_a_row_must_say_what_every_probe_did(self) -> None:
        partial = BlastRadiusRow(
            mutation_id="a_partial_change",
            domain=IdentityDomain.FIXTURE,
            moved=("fixture_probe",),
            unmoved=("semantic_probe",),
            evidence="a row that omits three probes",
        )
        with self.assertRaises(CloseoutMatrixError) as failure:
            _full_matrix((partial, *_every_domain_covered()[1:]))
        self.assertIn("every probe", str(failure.exception))

    def test_a_probe_cannot_both_move_and_not_move(self) -> None:
        with self.assertRaises(CloseoutMatrixError):
            BlastRadiusRow(
                mutation_id="a_contradictory_change",
                domain=IdentityDomain.FIXTURE,
                moved=("fixture_probe",),
                unmoved=("fixture_probe",),
                evidence="a contradiction",
            )

    def test_an_undeclared_probe_is_refused(self) -> None:
        stray = BlastRadiusRow(
            mutation_id="a_stray_change",
            domain=IdentityDomain.FIXTURE,
            moved=("an_undeclared_probe",),
            unmoved=tuple(sorted(probe.probe_id for probe in ALL_PROBES)),
            evidence="a row naming a probe nobody declared",
        )
        with self.assertRaises(CloseoutMatrixError) as failure:
            _full_matrix((stray, *_every_domain_covered()[1:]))
        self.assertIn("undeclared probe", str(failure.exception))

    def test_observe_decides_what_moved_rather_than_being_told(self) -> None:
        baseline = {probe.probe_id: "sha256:" + "0" * 64 for probe in ALL_PROBES}
        mutated = dict(baseline)
        mutated["fixture_probe"] = "sha256:" + "1" * 64
        row = observe_blast_radius(
            "a_fixture_change",
            IdentityDomain.FIXTURE,
            baseline,
            mutated,
            evidence="one reading against another",
        )
        self.assertEqual(row.moved, ("fixture_probe",))
        self.assertIn("semantic_probe", row.unmoved)

    def test_comparing_different_probe_sets_is_a_measurement_error(self) -> None:
        baseline = {"fixture_probe": "a"}
        with self.assertRaises(CloseoutMatrixError):
            observe_blast_radius(
                "a_mismatched_change",
                IdentityDomain.FIXTURE,
                baseline,
                {"fixture_probe": "a", "semantic_probe": "b"},
                evidence="two readings of different things",
            )


class MatrixGuardTests(unittest.TestCase):
    def test_every_identity_domain_must_be_exercised(self) -> None:
        rows = _every_domain_covered()
        _full_matrix(rows)  # the complete set is accepted
        for index in range(len(rows)):
            reduced = rows[:index] + rows[index + 1 :]
            with self.subTest(dropped=rows[index].mutation_id):
                with self.assertRaises(CloseoutMatrixError) as failure:
                    _full_matrix(reduced)
                self.assertIn("no mutation exercises", str(failure.exception))

    def test_a_metric_that_did_not_move_says_so(self) -> None:
        self.assertFalse(_metric(baseline=89, final=89).moved)
        self.assertTrue(_metric(baseline=89, final=88).moved)

    def test_a_metric_needs_a_commit_at_each_end(self) -> None:
        for field in ("baseline_commit", "final_commit"):
            for value in ("", "not-a-commit", "0" * 39, "0" * 41):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(CloseoutMatrixError):
                        _metric(**{field: value})

    def test_an_unsupported_schema_is_refused(self) -> None:
        with self.assertRaises(CloseoutMatrixError):
            CloseoutMatrix(
                probes=tuple(sorted(ALL_PROBES, key=lambda probe: probe.probe_id)),
                metrics=(_metric(),),
                rows=_every_domain_covered(),
                schema="h3-context-closeout-matrix/2",
            )

    def test_the_fingerprint_answers_to_probes_metrics_and_rows(self) -> None:
        base = _full_matrix(_every_domain_covered())
        self.assertEqual(base.fingerprint, _full_matrix(_every_domain_covered()).fingerprint)
        moved = CloseoutMatrix(
            probes=base.probes,
            metrics=(_metric(final=5),),
            rows=base.rows,
        )
        self.assertNotEqual(moved.fingerprint, base.fingerprint)


class GeneratedMatrixTests(unittest.TestCase):
    """The committed record, and the chain it claims to describe."""

    document: dict[str, Any]
    matrix: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        # CRITICAL: a public snapshot cannot replay private checkpoints. Recompute the typed
        # artifact and current runtime rows here; keep historical re-derivation separate.
        cls.matrix = CloseoutMatrix(
            probes=tuple(
                Probe(
                    probe_id=row["probe_id"],
                    domain=IdentityDomain(row["domain"]),
                    authority=row["authority"],
                    depends_on=tuple(IdentityDomain(value) for value in row["depends_on"]),
                )
                for row in cls.document["probes"]
            ),
            metrics=tuple(
                CloseoutMetric(
                    metric_id=row["metric_id"],
                    unit=row["unit"],
                    baseline=row["baseline"],
                    final=row["final"],
                    baseline_commit=row["baseline_commit"],
                    final_commit=row["final_commit"],
                    evidence=row["evidence"],
                )
                for row in cls.document["metrics"]
            ),
            rows=tuple(
                BlastRadiusRow(
                    mutation_id=row["mutation_id"],
                    domain=IdentityDomain(row["domain"]),
                    moved=tuple(row["moved"]),
                    unmoved=tuple(row["unmoved"]),
                    evidence=row["evidence"],
                )
                for row in cls.document["rows"]
            ),
        )

    def test_the_artifact_round_trips_with_recomputed_fingerprint(self) -> None:
        self.assertEqual(
            GENERATOR.artifact_bytes(self.matrix), ARTIFACT.read_bytes().replace(b"\r\n", b"\n")
        )

    def test_current_runtime_mutations_reproduce_the_recorded_rows(self) -> None:
        self.assertEqual([row.to_wire() for row in GENERATOR.build_rows()], self.document["rows"])
        self.assertEqual(
            [probe.to_wire() for probe in sorted(GENERATOR.PROBES, key=lambda row: row.probe_id)],
            self.document["probes"],
        )

    def test_the_record_and_its_fingerprint_agree(self) -> None:
        self.assertEqual(self.document["schema"], CLOSEOUT_MATRIX_SCHEMA)
        self.assertEqual(self.document["fingerprint"], self.matrix.fingerprint)

    def test_the_artifact_validates_against_its_schema(self) -> None:
        import jsonschema

        schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_the_recomputable_byte_metric_is_the_sections_it_names(self) -> None:
        """The metric says "bindings and reachability", so it must be those two and not a residual.

        Review found this shipped as `v1_total - v2_total`, even though the v1-to-v2 delta also
        folds in other representation changes and can move independently as those sections evolve.
        The number and the sentence explaining it have to be checked against each other, or a
        metric can keep an honest-looking value while describing something it is not.
        """

        metric = next(
            row
            for row in self.document["metrics"]
            if row["metric_id"] == "manifest_bytes_restating_a_computable_fact"
        )
        from comfyui_h3_context.public_api import get_public_manifest, get_public_manifest_v2

        v1 = get_public_manifest().to_wire()
        v2 = get_public_manifest_v2().to_wire()

        def sized(value: object) -> int:
            return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))

        named_sections = sized(v1["bindings"]) + sized(v1["reachability"])
        self.assertEqual(metric["baseline"], named_sections)
        self.assertEqual(metric["final"], 0)
        # The residual is a different number; conflating them is the defect being guarded.
        self.assertNotEqual(named_sections, sized(v1) - sized(v2))

    def test_the_wire_shape_mutation_digests_real_declared_fields(self) -> None:
        """The wire row must move because the shipped surface changed, not because a stand-in did.

        Review found the mutated field list built from `read_probes(...)`'s *keys* — the six probe
        identifiers — rather than the projection's declared field names. The row still reported the
        right answer, from an unrelated blob. Pin the surface to the real one.
        """

        fields = set(GENERATOR._wire_field_names(GENERATOR.BASELINE_WORLD))
        probe_ids = {probe.probe_id for probe in self.matrix.probes}
        self.assertNotEqual(fields, probe_ids)
        # Real projection fields no probe identifier resembles.
        self.assertLessEqual({"prompt_text", "state", "validation_status", "schema"}, fields)
        # Only the two fingerprint names legitimately appear in both namespaces.
        self.assertEqual(fields & probe_ids, {"prompt_fingerprint", "report_fingerprint"})

    def test_no_execution_identity_follows_a_non_semantic_change(self) -> None:
        """AC-M18-06-02, stated as the result rather than as the mechanism.

        This is the whole point of M18-02 measured end to end: a render, a fixture or a bundle
        change must never move an identity a runtime would consume for execution or cache reuse.
        """

        semantic = {
            probe["probe_id"] for probe in self.document["probes"] if probe["domain"] == "semantic"
        }
        self.assertTrue(semantic, "the matrix declares no execution identity to protect")
        for row in self.document["rows"]:
            if row["domain"] == "semantic":
                continue
            with self.subTest(mutation=row["mutation_id"]):
                self.assertEqual(sorted(set(row["moved"]) & semantic), [])

    def test_a_meaning_change_moves_every_execution_identity(self) -> None:
        semantic = {
            probe["probe_id"] for probe in self.document["probes"] if probe["domain"] == "semantic"
        }
        rows = [row for row in self.document["rows"] if row["domain"] == "semantic"]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(mutation=row["mutation_id"]):
                self.assertEqual(sorted(semantic - set(row["moved"])), [])

    def test_the_headline_number_is_reported_as_unchanged(self) -> None:
        """AC-M18-06-07. The number a reader reaches for first is the one that moved least."""

        metric = next(
            item for item in self.document["metrics"] if item["metric_id"] == "shipped_schema_files"
        )
        self.assertEqual(metric["baseline"], metric["final"])
        self.assertFalse(metric["moved"])

    def test_the_record_carries_identifiers_and_counts_and_nothing_else(self) -> None:
        from comfyui_h3_context.core.contract_inventory import FORBIDDEN_RECORD_TEXT

        text = ARTIFACT.read_text(encoding="utf-8")
        match = FORBIDDEN_RECORD_TEXT.search(text)
        self.assertIsNone(match, match.group(0) if match else "")


class ChainCloseoutTests(unittest.TestCase):
    """AC-M18-06-01/03/04/05, asserted against the accepted chain rather than restated."""

    def test_no_retired_or_migrated_entry_is_left_undecided(self) -> None:
        """AC-M18-06-01. The surviving NEED_EVIDENCE rows are live fail-closed states."""

        inventory = json.loads(
            (REPO_ROOT / "comfyui_h3_context/contracts/contract_inventory_v1.json").read_text(
                encoding="utf-8"
            )
        )
        retirement = json.loads(
            (REPO_ROOT / "governance/contracts/retirement_eligibility_v1.json").read_text(
                encoding="utf-8"
            )
        )
        retired = {item["contract_id"] for item in retirement["retired"]}
        undecided = {
            entry["contract_id"]
            for entry in inventory["entries"]
            if entry["disposition"] == "NEED_EVIDENCE"
        }
        self.assertEqual(sorted(undecided & retired), [])
        for entry in inventory["entries"]:
            with self.subTest(contract=entry["contract_id"]):
                self.assertTrue(entry["fingerprint_domain"])
                self.assertTrue(entry["boundary"])
                self.assertTrue(entry["authority_paths"])
                self.assertTrue(entry["evidence"])

    def test_every_identity_carries_exactly_one_domain(self) -> None:
        domains = json.loads(
            (REPO_ROOT / "governance/contracts/fingerprint_domain_v1.json").read_text(
                encoding="utf-8"
            )
        )
        seen: dict[str, str] = {}
        for row in domains["assignments"]:
            consumer = row["consumer_id"]
            with self.subTest(consumer=consumer):
                self.assertNotIn(consumer, seen, "an identity carries two domains")
                seen[consumer] = row["domain"]
        self.assertTrue(seen)

    def test_the_v1_public_manifest_still_projects_from_v2_unchanged(self) -> None:
        """v1 still projects exactly from v2, including the chosen semantic model input.

        Pinned to the literal fingerprint as well as to the projection, because "the projection
        matches what v1 builds" would stay true if both moved together.
        """

        from comfyui_h3_context.core.public_manifest_v2 import project_public_manifest_v1
        from comfyui_h3_context.public_api import get_public_manifest, get_public_manifest_v2

        v1 = get_public_manifest()
        projected = project_public_manifest_v1(get_public_manifest_v2())
        self.assertEqual(projected.to_wire(), v1.to_wire())
        self.assertEqual(projected.fingerprint, v1.fingerprint)
        # IMPORTANT: keep this independent literal pin; deriving it from v1 would
        # hide public-surface drift.
        self.assertEqual(
            v1.fingerprint,
            "sha256:f7f921d6363ad8f9d3cfc5448247dfaf587e58a8b797879b1e2d023e9e99feff",
        )

    def test_perception_manifest_preserves_required_inputs_and_optional_admission(self) -> None:
        from comfyui_h3_context.public_api import get_public_manifest

        nodes = get_public_manifest().contracts.definitions
        self.assertEqual(len(nodes), 28)
        by_id = {node.node_id: node for node in nodes}
        for suffix in ("VisualPerceptionProducer", "AudioPerceptionProducer"):
            with self.subTest(node=suffix):
                inputs = by_id["comfyui_h3_context.H3Context." + suffix].inputs
                self.assertEqual(
                    [item.name for item in inputs if item.required],
                    ["media", "route", "profile_id", "device", "cancel_requested"],
                )
                optional = [item for item in inputs if not item.required]
                self.assertEqual(
                    [item.name for item in optional],
                    ["local_service_consent", "request"]
                    if suffix == "VisualPerceptionProducer"
                    else ["local_service_consent"],
                )
                self.assertEqual(optional[0].name, "local_service_consent")
                self.assertEqual(optional[0].socket_type.value, "BOOLEAN")
                self.assertIs(optional[0].default, False)
                if suffix == "VisualPerceptionProducer":
                    self.assertEqual(optional[1].socket_type.value, "H3_CONTEXT_REQUEST")
                    self.assertIsNone(optional[1].default)
        timeline = by_id["comfyui_h3_context.H3Context.FullReferenceTimelineProducer"].inputs
        self.assertEqual(
            [item.name for item in timeline if item.required],
            [
                "request",
                "reference_registry",
                "media",
                "evidence_graph",
                "evidence_report",
                "cross_reference_graph",
                "cross_reference_report",
                "directive_authority",
                "directive_report",
                "intent_graph",
                "intent_report",
            ],
        )
        optional = [item for item in timeline if not item.required]
        self.assertEqual(len(optional), 1)
        self.assertEqual(optional[0].name, "visual_result")
        self.assertEqual(optional[0].socket_type.value, "H3_VISUAL_PRODUCER_RESULT")
        self.assertIsNone(optional[0].default)

    def test_every_generator_reproduces_byte_identically(self) -> None:
        """AC-M18-06-03, run as the gate runs it rather than described."""

        for script in (
            "contract_inventory.py",
            "fingerprint_domain.py",
            "cross_language_surface.py",
            "retirement_eligibility.py",
        ):
            with self.subTest(generator=script):
                result = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "scripts" / script), "--check"],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class HistoricalReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_history(REPO_ROOT, [GENERATOR.M18_01, GENERATOR.M18_04, GENERATOR.M18_05])

    def test_the_accepted_history_regenerates_byte_identically(self) -> None:
        self.assertEqual(
            GENERATOR.artifact_bytes(GENERATOR.build_matrix()),
            ARTIFACT.read_bytes().replace(b"\r\n", b"\n"),
        )

    def test_every_recorded_metric_commit_exists(self) -> None:
        document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        for metric in document["metrics"]:
            for field in ("baseline_commit", "final_commit"):
                subprocess.run(
                    ["git", "cat-file", "-e", f"{metric[field]}^{{commit}}"],
                    cwd=REPO_ROOT,
                    check=True,
                    capture_output=True,
                )

    def test_the_historical_cli_checks_the_actual_artifact(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts/closeout_matrix.py"), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SyntheticHistoryTests(unittest.TestCase):
    def test_real_commit_metrics_and_missing_objects(self) -> None:
        with git_history() as root:
            write_json(
                root,
                GENERATOR.INVENTORY,
                {
                    "entries": [
                        {"disposition": "NEED_EVIDENCE"},
                        {"disposition": "NEED_EVIDENCE"},
                    ]
                },
            )
            write_json(root, "comfyui_h3_context/contracts/example.schema.json", {})
            first = commit(root)
            write_json(root, GENERATOR.INVENTORY, {"entries": [{"disposition": "ACTIVE"}]})
            write_json(root, GENERATOR.DOMAIN, {"assignments": [1, 2, 3]})
            write_json(
                root,
                "comfyui_h3_context/contracts/retirement_eligibility_v1.json",
                {
                    "retired": [1, 2],
                },
            )
            last = commit(root)
            with (
                patch.object(GENERATOR, "ROOT", root),
                patch.object(GENERATOR, "M18_01", first),
                patch.object(GENERATOR, "M18_04", last),
                patch.object(GENERATOR, "M18_05", last),
            ):
                metrics = {row.metric_id: row for row in GENERATOR.build_metrics()}
                undecided = metrics["contracts_with_undecided_disposition"]
                self.assertEqual((undecided.baseline, undecided.final), (2, 0))
                self.assertEqual(metrics["identities_without_a_declared_domain"].baseline, 3)
                self.assertEqual(metrics["schemas_restating_a_wire_with_no_reader"].baseline, 2)
                self.assertEqual(metrics["shipped_schema_files"].baseline, 1)
                self.assertEqual(metrics["shipped_schema_files"].final, 1)
                with self.assertRaises(subprocess.CalledProcessError):
                    GENERATOR.git_show(last, "absent.json")
                with patch.object(GENERATOR, "M18_01", "0" * 40):
                    with self.assertRaises(subprocess.CalledProcessError):
                        GENERATOR.build_matrix()

    def test_public_artifact_and_runtime_checks_do_not_read_history(self) -> None:
        with patch.object(GENERATOR, "git_show", side_effect=AssertionError("history unavailable")):
            GeneratedMatrixTests.setUpClass()
            self.assertEqual(
                GeneratedMatrixTests.matrix.fingerprint,
                GeneratedMatrixTests.document["fingerprint"],
            )
            self.assertEqual(
                [row.to_wire() for row in GENERATOR.build_rows()],
                GeneratedMatrixTests.document["rows"],
            )


if __name__ == "__main__":  # pragma: no cover - convenience for a single-file run
    unittest.main()
