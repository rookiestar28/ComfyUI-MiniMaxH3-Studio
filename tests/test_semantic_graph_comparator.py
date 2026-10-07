"""M14-03 semantic prompt graph, typed comparator, and behavior-profile tests."""

from __future__ import annotations

import gzip
import importlib
import json
import unittest
from ast import Import, ImportFrom, parse
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from test_full_rendering import full_plan

from comfyui_h3_context.core import (
    OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
    OFFICIAL_H3_GUIDE_REVISION,
    ProfileIdentity,
    SchemaVersion,
    TaskMode,
    render_full_reference_prompt,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m14_03_semantic_graph_comparator.json.gz"
SCHEMA = ROOT / "governance" / "contracts" / "semantic_graph_comparator_v1.schema.json"


def _fixture_text() -> str:
    return gzip.decompress(FIXTURE.read_bytes()).decode("utf-8")


class SemanticGraphComparatorTests(unittest.TestCase):
    @staticmethod
    def _full_source() -> tuple[str, object]:
        plan = full_plan()
        return render_full_reference_prompt(plan).text, plan.request.profile

    def test_full_reference_prompt_builds_versioned_required_semantic_graph(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        source = render_full_reference_prompt(full_plan()).text

        graph = semantic.parse_semantic_prompt(
            source,
            full_plan().request.profile,
            TaskMode.REF2VA,
            semantic.SemanticSourceKind.LOCAL,
            "local.full_reference.red",
        )

        self.assertEqual(graph.schema, "h3.prompt.semantic_graph.v2")
        self.assertEqual(semantic.render_semantic_prompt(graph), source)
        self.assertEqual(
            {item.value for item in semantic.SemanticNodeKind},
            {item.kind.value for item in graph.nodes},
        )
        self.assertEqual(
            {item.value for item in semantic.SemanticRelationKind},
            {item.kind.value for item in graph.relations},
        )
        for node in graph.nodes:
            self.assertEqual(source[node.anchor.start : node.anchor.end], node.anchor.text)
        for relation in graph.relations:
            self.assertEqual(
                source[relation.anchor.start : relation.anchor.end], relation.anchor.text
            )

    def test_p0_outcomes_and_unscorable_are_typed_and_cannot_be_overridden(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        source, profile = self._full_source()

        def graph(text: str, source_id: str) -> object:
            return semantic.parse_semantic_prompt(
                text,
                profile,
                TaskMode.REF2VA,
                semantic.SemanticSourceKind.LOCAL,
                source_id,
            )

        baseline = graph(source, "local.p0.baseline")
        cases = {
            semantic.SemanticDiffOutcome.HARD_MUTATION: source.replace(
                "Keep the light on.", "Turn the light off."
            ),
            semantic.SemanticDiffOutcome.ROLE: source.replace(
                "<Video 1> is the footage being edited.",
                "<Video 1> is the style reference.",
            ),
            semantic.SemanticDiffOutcome.OWNERSHIP: source.replace(
                "<Subject 1> is the baker from <Picture 1>",
                "<Subject 1> is the baker from <Picture 2>",
            ),
            semantic.SemanticDiffOutcome.TEMPORAL: source.replace("At 00:02.000", "At 00:03.000"),
        }
        for expected, mutated in cases.items():
            with self.subTest(expected=expected.value):
                result = semantic.compare_semantic_graphs(
                    graph(mutated, f"local.p0.{expected.value}"), baseline
                )
                self.assertEqual(result.primary_outcome, expected)
                self.assertIn(expected, {finding.outcome for finding in result.findings})
                self.assertNotEqual(result.primary_outcome, semantic.SemanticDiffOutcome.COMPATIBLE)

        unscorable = semantic.compare_semantic_prompts(
            "not a canonical prompt",
            source,
            profile,
            TaskMode.REF2VA,
            semantic.SemanticSourceKind.LOCAL,
            semantic.SemanticSourceKind.PUBLIC_GUIDE,
            "local.invalid",
            "guide.valid",
        )
        self.assertEqual(unscorable.primary_outcome, semantic.SemanticDiffOutcome.UNSCORABLE)

    def test_graph_is_fully_source_derived_and_wire_forgery_fails(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        source, profile = self._full_source()
        graph = semantic.parse_semantic_prompt(
            source,
            profile,
            TaskMode.REF2VA,
            semantic.SemanticSourceKind.LOCAL,
            "local.derived.authority",
        )

        with self.assertRaisesRegex(semantic.SemanticGraphError, "source-derived"):
            replace(
                graph,
                source_text=source.replace("Keep the light on.", "Turn the light off."),
            )

        wire = graph.to_wire()
        nodes = list(wire["nodes"])
        forged = dict(nodes[0])
        forged["value"] = "forged semantic value"
        nodes[0] = forged
        wire["nodes"] = nodes
        with self.assertRaisesRegex(semantic.SemanticGraphError, "source-derived"):
            semantic.validate_semantic_graph_wire(wire)

    def test_derived_comparison_report_and_profile_reject_direct_construction(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")

        with self.assertRaisesRegex(semantic.SemanticGraphError, "comparator-owned"):
            semantic.SemanticComparison(
                semantic.SemanticDiffOutcome.EXACT,
                (
                    semantic.SemanticDiffFinding(
                        "finding.forged",
                        semantic.SemanticDiffOutcome.EXACT,
                        "graph",
                        "forged",
                        "forged",
                    ),
                ),
                "sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
            )
        with self.assertRaisesRegex(semantic.SemanticGraphError, "evaluator-owned"):
            semantic.ComparatorEvaluationReport(
                0,
                0,
                0,
                0,
                (),
                0,
                0,
                0,
                0,
                "1.00000000",
                "1.00000000",
                ("1.00000000", "1.00000000"),
                ("1.00000000", "1.00000000"),
                True,
            )
        with self.assertRaisesRegex(semantic.SemanticGraphError, "bundle-owned"):
            semantic.OfficialBehaviorProfile(
                semantic.BehaviorProfileDisposition.UNAVAILABLE,
                semantic.M14_01_TERMINAL_DISPOSITION_SHA256,
                "sha256:" + "0" * 64,
            )

    def test_derived_aggregates_reject_duck_typed_members(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        bundle = semantic.decode_semantic_evaluation_json(_fixture_text())

        class WireValue(Protocol):
            def to_wire(self) -> dict[str, object]: ...

        class Spoof:
            def __init__(self, wrapped: WireValue, **overrides: object) -> None:
                self._wrapped = wrapped
                self.__dict__.update(overrides)

            def __getattr__(self, name: str) -> object:
                return getattr(self._wrapped, name)

            def __eq__(self, _other: object) -> bool:
                return True

            def to_wire(self) -> dict[str, object]:
                return self._wrapped.to_wire()

        class AlwaysEqual:
            def __eq__(self, _other: object) -> bool:
                return True

        case = bundle.p0_cases[0]
        with self.assertRaisesRegex(semantic.SemanticGraphError, "comparison is invalid"):
            semantic.ComparatorEvaluationCase(
                case.case_id,
                case.source_cluster,
                case.seed,
                case.partition,
                case.expected_outcome,
                case.expected_material_difference,
                case.local_graph,
                case.reference_graph,
                Spoof(case.comparison),
            )

        for field, report, profile in (
            (
                "report",
                Spoof(bundle.report, guide_passed=0),
                bundle.official_profile,
            ),
            (
                "official_profile",
                bundle.report,
                Spoof(
                    bundle.official_profile,
                    local_qualification_fingerprint="sha256:" + "0" * 64,
                ),
            ),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(semantic.SemanticGraphError, "report or profile is invalid"),
            ):
                semantic.SemanticEvaluationBundle(
                    bundle.design,
                    bundle.guide_goldens,
                    bundle.p0_cases,
                    bundle.non_p0_cases,
                    report,
                    profile,
                    bundle.bundle_fingerprint,
                )

        with self.assertRaisesRegex(semantic.SemanticGraphError, "exact primitive types"):
            replace(bundle.design, guide_cluster_count=AlwaysEqual())

        graph = case.local_graph
        uncertain_graph = bundle.guide_goldens[0].graph
        for field, target_graph, values in (
            ("nodes", graph, graph.nodes),
            ("relations", graph, graph.relations),
            ("uncertainties", uncertain_graph, uncertain_graph.uncertainties),
        ):
            spoofed = (Spoof(values[0]), *values[1:])
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    semantic.SemanticGraphError, f"{field} inventory is invalid"
                ),
            ):
                replace(target_graph, **{field: spoofed})

    def test_source_and_version_authority_require_exact_types(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        bundle = semantic.decode_semantic_evaluation_json(_fixture_text())
        guide_graph = bundle.guide_goldens[0].graph
        provenance = guide_graph.provenance

        class EqualValue:
            def __init__(self, value: str) -> None:
                self.value = value

            def __eq__(self, _other: object) -> bool:
                return True

        class EqualVersion(SchemaVersion):
            def __eq__(self, _other: object) -> bool:
                return True

            def __str__(self) -> str:
                return "999.99"

        class EqualInt(int):
            def __eq__(self, _other: object) -> bool:
                return True

        with self.assertRaisesRegex(semantic.SemanticGraphError, "schema is invalid"):
            replace(guide_graph, schema=EqualValue("forged.schema"))

        for field, value in (
            ("authority_revision", EqualValue("wrong.revision")),
            ("authority_digest", EqualValue("wrong.digest")),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(semantic.SemanticGraphError, f"{field} is invalid"),
            ):
                replace(provenance, **{field: value})

        with self.assertRaisesRegex(semantic.SemanticGraphError, "source fingerprint is invalid"):
            replace(provenance, source_fingerprint=EqualValue("wrong.fingerprint"))

        forged_profile = ProfileIdentity(provenance.profile.name, EqualVersion(999, 99))
        with self.assertRaisesRegex(semantic.SemanticGraphError, "profile version is invalid"):
            replace(provenance, profile=forged_profile)
        forged_nested_primitives = ProfileIdentity(
            provenance.profile.name,
            SchemaVersion(EqualInt(999), EqualInt(99)),
        )
        with self.assertRaisesRegex(semantic.SemanticGraphError, "profile version is invalid"):
            replace(provenance, profile=forged_nested_primitives)

    def test_public_mapping_validators_require_exact_json_member_types(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        bundle = semantic.decode_semantic_evaluation_json(_fixture_text())

        class AlwaysEqual:
            def __eq__(self, _other: object) -> bool:
                return True

        class EqualInt(int):
            pass

        class EqualString(str):
            pass

        class EqualList(list[object]):
            pass

        class EqualDict(dict[str, object]):
            pass

        class ArmedString(str):
            armed = False

            def __eq__(self, other: object) -> bool:
                if self.armed:
                    raise RuntimeError("member-name equality invoked")
                return super().__eq__(other)

            __hash__ = str.__hash__

        graph_wire = bundle.p0_cases[0].local_graph.to_wire()
        graph_mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "schema": lambda wire: wire.__setitem__("schema", AlwaysEqual()),
            "source_fingerprint": lambda wire: wire["provenance"].__setitem__(
                "source_fingerprint", AlwaysEqual()
            ),
            "graph_fingerprint": lambda wire: wire.__setitem__("graph_fingerprint", AlwaysEqual()),
            "nodes": lambda wire: wire.__setitem__("nodes", AlwaysEqual()),
            "task_mode": lambda wire: wire["provenance"].__setitem__("task_mode", AlwaysEqual()),
            "source_kind": lambda wire: wire["provenance"].__setitem__(
                "source_kind", AlwaysEqual()
            ),
            "profile_name": lambda wire: wire["provenance"]["profile"].__setitem__(
                "name", AlwaysEqual()
            ),
            "schema_subclass": lambda wire: wire.__setitem__("schema", EqualString(wire["schema"])),
            "nodes_subclass": lambda wire: wire.__setitem__("nodes", EqualList(wire["nodes"])),
        }
        for field, mutate in graph_mutations.items():
            with self.subTest(validator="graph", field=field):
                wire = json.loads(json.dumps(graph_wire))
                mutate(wire)
                with self.assertRaisesRegex(semantic.SemanticGraphError, "exact JSON member types"):
                    semantic.validate_semantic_graph_wire(wire)

        for field in ("member_name_subclass", "cyclic_container", "non_finite_number"):
            with self.subTest(validator="graph", field=field):
                malformed: Any = json.loads(json.dumps(graph_wire))
                if field == "member_name_subclass":
                    schema = malformed.pop("schema")
                    malformed[EqualString("schema")] = schema
                elif field == "cyclic_container":
                    malformed["nodes"].append(malformed["nodes"])
                else:
                    malformed["graph_fingerprint"] = float("inf")
                with self.assertRaisesRegex(semantic.SemanticGraphError, "exact JSON member types"):
                    semantic.validate_semantic_graph_wire(malformed)

        evaluation_wire = bundle.to_wire()
        armed_member_wire: Any = json.loads(json.dumps(evaluation_wire))
        schema = armed_member_wire.pop("schema")
        armed_member = ArmedString("schema")
        armed_member_wire[armed_member] = schema
        armed_member.armed = True
        with self.assertRaisesRegex(semantic.SemanticGraphError, "exact JSON member types"):
            semantic.validate_semantic_evaluation_wire(armed_member_wire)

        evaluation_mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "schema": lambda wire: wire.__setitem__("schema", AlwaysEqual()),
            "design": lambda wire: wire.__setitem__("design", AlwaysEqual()),
            "design_count": lambda wire: wire["design"].__setitem__(
                "guide_cluster_count", EqualInt(wire["design"]["guide_cluster_count"])
            ),
            "report": lambda wire: wire.__setitem__("report", AlwaysEqual()),
            "official_profile": lambda wire: wire.__setitem__("official_profile", AlwaysEqual()),
            "bundle_fingerprint": lambda wire: wire.__setitem__(
                "bundle_fingerprint", AlwaysEqual()
            ),
            "case_comparison": lambda wire: wire["p0_cases"][0].__setitem__(
                "comparison", AlwaysEqual()
            ),
            "design_subclass": lambda wire: wire.__setitem__("design", EqualDict(wire["design"])),
            "comparison_subclass": lambda wire: wire["p0_cases"][0].__setitem__(
                "comparison", EqualDict(wire["p0_cases"][0]["comparison"])
            ),
        }
        for field, mutate in evaluation_mutations.items():
            with self.subTest(validator="evaluation", field=field):
                wire = json.loads(json.dumps(evaluation_wire))
                mutate(wire)
                with self.assertRaisesRegex(semantic.SemanticGraphError, "exact JSON member types"):
                    semantic.validate_semantic_evaluation_wire(wire)

    def test_each_timing_relation_joins_the_nearest_preceding_shot(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        source, profile = self._full_source()
        source = source.replace("[Shot 1]", "[Shot 1] At 00:00.000,", 1)

        graph = semantic.parse_semantic_prompt(
            source,
            profile,
            TaskMode.REF2VA,
            semantic.SemanticSourceKind.LOCAL,
            "local.multi_timing.regression",
        )
        temporal = tuple(
            (relation.source_id, relation.target_id, relation.value)
            for relation in graph.relations
            if relation.kind is semantic.SemanticRelationKind.TEMPORAL
        )

        self.assertEqual(
            temporal,
            (
                ("event.shot.1", "timing.1", "00:00.000"),
                ("event.shot.2", "timing.2", "00:02.000"),
            ),
        )

        swapped_source = (
            source.replace("At 00:00.000,", "At 99:99.999,", 1)
            .replace("At 00:02.000,", "At 00:00.000,", 1)
            .replace("At 99:99.999,", "At 00:02.000,", 1)
        )
        swapped_graph = semantic.parse_semantic_prompt(
            swapped_source,
            profile,
            TaskMode.REF2VA,
            semantic.SemanticSourceKind.LOCAL,
            "local.multi_timing.swapped",
        )
        comparison = semantic.compare_semantic_graphs(swapped_graph, graph)
        self.assertEqual(comparison.primary_outcome, semantic.SemanticDiffOutcome.TEMPORAL)

    def test_frozen_evaluation_supports_metrics_and_unavailable_profile(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        bundle = semantic.decode_semantic_evaluation_json(_fixture_text())
        self.assertLess(FIXTURE.stat().st_size, 500 * 1024)
        self.assertEqual(FIXTURE.read_bytes()[4:8], b"\x00\x00\x00\x00")
        self.assertEqual(FIXTURE.read_bytes()[9], 255)

        self.assertEqual(bundle.design.guide_cluster_count, 2)
        self.assertEqual(bundle.design.guide_support, 6)
        self.assertEqual(bundle.design.p0_cluster_count, 8)
        self.assertEqual(bundle.design.p0_support, 24)
        self.assertEqual(bundle.design.non_p0_cluster_count, 10)
        self.assertEqual(bundle.design.non_p0_support, 40)
        self.assertEqual(bundle.design.non_p0_positive_support, 20)
        self.assertEqual(bundle.design.non_p0_negative_support, 20)
        self.assertEqual(bundle.design.interval_method, "wilson_two_sided_95")
        self.assertEqual(bundle.design.interval_z, "1.959963984540054")

        altered_holdout = list(bundle.non_p0_cases)
        altered_holdout[0] = replace(altered_holdout[0], case_id="holdout.changed.after.freeze")
        with self.assertRaisesRegex(semantic.SemanticGraphError, "frozen fingerprint"):
            semantic.build_semantic_evaluation_bundle(
                bundle.guide_goldens,
                bundle.p0_cases,
                tuple(altered_holdout),
            )

        guide_clusters = {item.source_cluster for item in bundle.guide_goldens}
        p0_clusters = {item.source_cluster for item in bundle.p0_cases}
        holdout_clusters = {item.source_cluster for item in bundle.non_p0_cases}
        self.assertTrue(guide_clusters.isdisjoint(p0_clusters))
        self.assertTrue(guide_clusters.isdisjoint(holdout_clusters))
        self.assertTrue(p0_clusters.isdisjoint(holdout_clusters))
        for values, expected_clusters in (
            (bundle.p0_cases, 8),
            (bundle.non_p0_cases, 10),
        ):
            references_by_cluster: dict[str, set[str]] = {}
            for item in values:
                references_by_cluster.setdefault(item.source_cluster, set()).add(
                    item.reference_graph.source_text
                )
            self.assertEqual(
                {len(sources) for sources in references_by_cluster.values()},
                {1},
            )
            self.assertEqual(
                len({next(iter(sources)) for sources in references_by_cluster.values()}),
                expected_clusters,
            )
        uneven_p0 = list(bundle.p0_cases)
        uneven_p0[0] = replace(uneven_p0[0], source_cluster=uneven_p0[1].source_cluster)
        with self.assertRaisesRegex(semantic.SemanticGraphError, "per-cluster support"):
            semantic.build_semantic_evaluation_bundle(
                bundle.guide_goldens,
                tuple(uneven_p0),
                bundle.non_p0_cases,
            )
        contradiction = next(
            item
            for item in bundle.p0_cases
            if item.expected_outcome is semantic.SemanticDiffOutcome.CONTRADICTION
        )
        wrong_distribution = tuple(
            replace(
                contradiction,
                case_id=f"wrong.p0.{index}",
                source_cluster=f"p0.cluster.{((index - 1) % 8) + 1}",
                seed=1000 + index,
            )
            for index in range(1, 25)
        )
        with self.assertRaisesRegex(semantic.SemanticGraphError, "outcome distribution"):
            semantic.build_semantic_evaluation_bundle(
                bundle.guide_goldens,
                wrong_distribution,
                bundle.non_p0_cases,
            )

        imbalanced_holdout = list(bundle.non_p0_cases)
        positive_index = next(
            index
            for index, item in enumerate(imbalanced_holdout)
            if item.source_cluster == "holdout.cluster.1" and item.expected_material_difference
        )
        negative_index = next(
            index
            for index, item in enumerate(imbalanced_holdout)
            if item.source_cluster == "holdout.cluster.2" and not item.expected_material_difference
        )
        imbalanced_holdout[positive_index] = replace(
            imbalanced_holdout[positive_index], source_cluster="holdout.cluster.2"
        )
        imbalanced_holdout[negative_index] = replace(
            imbalanced_holdout[negative_index], source_cluster="holdout.cluster.1"
        )
        with self.assertRaisesRegex(semantic.SemanticGraphError, "holdout cluster balance"):
            semantic.build_semantic_evaluation_bundle(
                bundle.guide_goldens,
                bundle.p0_cases,
                tuple(imbalanced_holdout),
            )
        self.assertEqual(bundle.report.guide_passed, 6)
        self.assertEqual(bundle.report.p0_detected, 24)
        self.assertEqual(
            bundle.report.p0_outcome_counts,
            (
                ("contradiction", 2),
                ("hard_mutation", 6),
                ("order", 4),
                ("ownership", 4),
                ("role", 4),
                ("temporal", 4),
            ),
        )
        self.assertEqual(
            (
                bundle.report.true_positive,
                bundle.report.false_positive,
                bundle.report.false_negative,
                bundle.report.true_negative,
            ),
            (20, 0, 0, 20),
        )
        self.assertEqual(bundle.report.precision, "1.00000000")
        self.assertEqual(bundle.report.recall, "1.00000000")
        self.assertEqual(
            bundle.report.precision_interval,
            ("0.83887484", "1.00000000"),
        )
        self.assertEqual(bundle.report.recall_interval, ("0.83887484", "1.00000000"))
        self.assertTrue(bundle.report.qualified)

        profile = bundle.official_profile
        self.assertEqual(profile.disposition.value, "UNAVAILABLE")
        self.assertIsNone(profile.oracle_corpus_fingerprint)
        self.assertIsNone(profile.oracle_comparison_count)
        self.assertIsNone(profile.oracle_agreement)
        self.assertIsNone(profile.oracle_variability)
        self.assertIsNone(profile.oracle_latency)
        self.assertIsNone(profile.oracle_provider_failures)
        self.assertEqual(profile.observed_official_rules, ())
        self.assertEqual(profile.hypotheses, ())
        self.assertIn("official.capture_missing", profile.unknowns)
        self.assertIn("no_official_agreement_claim", profile.limitations)

    def test_public_guide_goldens_parse_and_round_trip_six_of_six(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        bundle = semantic.decode_semantic_evaluation_json(_fixture_text())

        self.assertEqual(len(bundle.guide_goldens), 6)
        for golden in bundle.guide_goldens:
            with self.subTest(case_id=golden.case_id):
                self.assertEqual(
                    golden.graph.provenance.source_kind,
                    semantic.SemanticSourceKind.PUBLIC_GUIDE,
                )
                self.assertEqual(
                    semantic.render_semantic_prompt(golden.graph),
                    golden.graph.source_text,
                )
                self.assertEqual(
                    golden.graph.provenance.authority_revision,
                    OFFICIAL_H3_GUIDE_REVISION,
                )
                self.assertRegex(
                    golden.graph.provenance.authority_digest or "",
                    r"^[0-9a-f]{64}$",
                )

    def test_source_classification_cannot_mint_guide_or_oracle_authority(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        source, profile = self._full_source()
        with self.assertRaisesRegex(semantic.SemanticGraphError, "guide authority"):
            semantic.parse_semantic_prompt(
                source,
                profile,
                TaskMode.REF2VA,
                semantic.SemanticSourceKind.PUBLIC_GUIDE,
                "forged.guide",
            )
        with self.assertRaisesRegex(semantic.SemanticGraphError, "not an admitted golden"):
            semantic.parse_semantic_prompt(
                source,
                profile,
                TaskMode.REF2VA,
                semantic.SemanticSourceKind.PUBLIC_GUIDE,
                "forged.guide",
                OFFICIAL_H3_GUIDE_REVISION,
                OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
            )
        with self.assertRaisesRegex(semantic.SemanticGraphError, "official oracle.*unavailable"):
            semantic.parse_semantic_prompt(
                source,
                profile,
                TaskMode.REF2VA,
                semantic.SemanticSourceKind.OFFICIAL_ORACLE,
                "forged.oracle",
            )
        guide_bundle = semantic.decode_semantic_evaluation_json(_fixture_text())
        guide_graph = guide_bundle.guide_goldens[3].graph
        compared = semantic.compare_semantic_prompts(
            guide_graph.source_text,
            guide_graph.source_text,
            profile,
            TaskMode.REF2VA,
            semantic.SemanticSourceKind.LOCAL,
            semantic.SemanticSourceKind.PUBLIC_GUIDE,
            "bound.local",
            "guide.reference.1",
            reference_authority_revision=OFFICIAL_H3_GUIDE_REVISION,
            reference_authority_digest=OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
        )
        self.assertEqual(compared.primary_outcome, semantic.SemanticDiffOutcome.EXACT)
        with self.assertRaisesRegex(semantic.SemanticGraphError, "local-only support"):
            semantic.build_comparator_evaluation_case(
                "forged.guide.case",
                "forged.guide.cluster",
                7,
                semantic.EvaluationPartition.NON_P0,
                semantic.SemanticDiffOutcome.EXACT,
                False,
                guide_graph,
                guide_graph,
            )

    def test_typed_diff_exposes_all_closed_outcomes(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        source, profile = self._full_source()

        def graph(text: str, source_id: str) -> object:
            return semantic.parse_semantic_prompt(
                text,
                profile,
                TaskMode.REF2VA,
                semantic.SemanticSourceKind.LOCAL,
                source_id,
            )

        baseline = graph(source, "outcome.reference")
        subject_one = "<Subject 1> is the baker from <Picture 1>."
        subject_two = "<Subject 2> is the bakery interior from <Video 1>."
        swapped = (
            source.replace(subject_one, "SWAP", 1)
            .replace(subject_two, subject_one, 1)
            .replace("SWAP", subject_two, 1)
        )
        extra_asset = source.replace(
            "<Audio 2> is the supplied audio track.",
            "<Audio 2> is the supplied audio track. <Picture 9> is a supplied visual reference.",
        )
        cases = (
            (semantic.SemanticDiffOutcome.EXACT, source, baseline),
            (
                semantic.SemanticDiffOutcome.COMPATIBLE,
                source.replace("target video.", "target video!"),
                baseline,
            ),
            (
                semantic.SemanticDiffOutcome.LOCAL_ONLY,
                extra_asset,
                baseline,
            ),
            (
                semantic.SemanticDiffOutcome.OFFICIAL_ONLY,
                source,
                graph(extra_asset, "outcome.reference.extra"),
            ),
            (
                semantic.SemanticDiffOutcome.CONTRADICTION,
                source.replace("a slow tracking shot", "a locked camera"),
                baseline,
            ),
            (
                semantic.SemanticDiffOutcome.ROLE,
                source.replace(
                    "<Video 1> is the footage being edited.",
                    "<Video 1> is the style reference.",
                ),
                baseline,
            ),
            (
                semantic.SemanticDiffOutcome.OWNERSHIP,
                source.replace(
                    "<Subject 1> is the baker from <Picture 1>",
                    "<Subject 1> is the baker from <Picture 2>",
                ),
                baseline,
            ),
            (semantic.SemanticDiffOutcome.ORDER, swapped, baseline),
            (
                semantic.SemanticDiffOutcome.TEMPORAL,
                source.replace("00:02.000", "00:06.000"),
                baseline,
            ),
            (
                semantic.SemanticDiffOutcome.HARD_MUTATION,
                source.replace("Keep the light on.", "Turn the light off."),
                baseline,
            ),
        )
        reached: set[object] = set()
        for index, (expected, local_text, reference) in enumerate(cases, 1):
            with self.subTest(expected=expected.value):
                result = semantic.compare_semantic_graphs(
                    graph(local_text, f"outcome.local.{index}"), reference
                )
                self.assertEqual(result.primary_outcome, expected)
                reached.add(result.primary_outcome)
        unscorable = semantic.compare_semantic_prompts(
            "invalid",
            source,
            profile,
            TaskMode.REF2VA,
            semantic.SemanticSourceKind.LOCAL,
            semantic.SemanticSourceKind.PUBLIC_GUIDE,
            "outcome.invalid",
            "outcome.guide",
        )
        reached.add(unscorable.primary_outcome)
        self.assertEqual(reached, set(semantic.SemanticDiffOutcome))

    def test_schema_python_and_hostile_wire_fail_closed(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        wire = json.loads(_fixture_text())
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(wire)), [])
        self.assertEqual(semantic.validate_semantic_evaluation_wire(wire).to_wire(), wire)

        drifted_fingerprint = dict(wire)
        drifted_fingerprint["bundle_fingerprint"] = "sha256:" + ("f" * 64)
        self.assertTrue(list(validator.iter_errors(drifted_fingerprint)))

        opened = dict(wire)
        opened["unexpected"] = True
        self.assertTrue(list(validator.iter_errors(opened)))
        with self.assertRaisesRegex(semantic.SemanticGraphError, "not closed"):
            semantic.validate_semantic_evaluation_wire(opened)

        duplicate = _fixture_text().replace(
            '"case_id": "guide.base.1",',
            '"case_id": "guide.base.1",\n      "case_id": "guide.base.1",',
            1,
        )
        with self.assertRaisesRegex(semantic.SemanticGraphError, "duplicate"):
            semantic.decode_semantic_evaluation_json(duplicate)

        fingerprint_member = (
            f'"bundle_fingerprint": "{semantic.FROZEN_EVALUATION_BUNDLE_FINGERPRINT}"'
        )
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(non_finite=token):
                non_finite = _fixture_text().replace(
                    fingerprint_member,
                    f'"bundle_fingerprint": {token}',
                    1,
                )
                with self.assertRaisesRegex(semantic.SemanticGraphError, "JSON is invalid"):
                    semantic.decode_semantic_evaluation_json(non_finite)

        with self.assertRaises(semantic.SemanticGraphError):
            semantic.decode_semantic_evaluation_json(
                "😀" * ((semantic.MAX_EVALUATION_JSON_BYTES // 4) + 1)
            )

        oversized_integer = _fixture_text().replace(
            '"seed": 1001',
            '"seed": ' + ("9" * 5000),
            1,
        )
        with self.assertRaisesRegex(semantic.SemanticGraphError, "JSON is invalid"):
            semantic.decode_semantic_evaluation_json(oversized_integer)

        deeply_nested = ("[" * 5000) + "0" + ("]" * 5000)
        with self.assertRaisesRegex(semantic.SemanticGraphError, "JSON is invalid"):
            semantic.decode_semantic_evaluation_json(deeply_nested)

        for unsafe_character in ("\u0001", "\ud800"):
            with self.subTest(unsafe_code_point=ascii(unsafe_character)):
                unsafe_wire = json.loads(_fixture_text())
                unsafe_wire["guide_goldens"][0]["graph"]["source_text"] = (
                    "unsafe" + unsafe_character
                )
                unsafe_code_point = json.dumps(unsafe_wire)
                with self.assertRaisesRegex(semantic.SemanticGraphError, "unsafe code point"):
                    semantic.decode_semantic_evaluation_json(unsafe_code_point)

        unsafe = _fixture_text().replace(
            "A baker opens the bakery before sunrise.",
            "https://example.invalid/private",
        )
        with self.assertRaises(semantic.SemanticGraphError) as captured:
            semantic.decode_semantic_evaluation_json(unsafe)
        self.assertIn("unsafe public fixture", str(captured.exception))
        self.assertNotIn("example.invalid", str(captured.exception))

    def test_module_has_no_optional_runtime_network_embedding_or_judge_dependency(self) -> None:
        module_path = ROOT / "comfyui_h3_context" / "core" / "semantic_graph_comparator.py"
        module = parse(module_path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in module.body:
            if isinstance(node, Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(
            {
                "comfy",
                "torch",
                "numpy",
                "requests",
                "httpx",
                "openai",
                "sentence_transformers",
                "sklearn",
            }.isdisjoint(imported)
        )
        source = module_path.read_text(encoding="utf-8").lower()
        self.assertNotIn("embedding", source)
        self.assertNotIn("model_judge", source)

    def test_leaf_exports_and_wilson_bounds_are_closed(self) -> None:
        semantic = importlib.import_module("comfyui_h3_context.core.semantic_graph_comparator")
        for name in (
            "SemanticPromptGraph",
            "SemanticDiffOutcome",
            "SemanticEvaluationBundle",
            "OfficialBehaviorProfile",
            "FROZEN_EVALUATION_BUNDLE_FINGERPRINT",
            "parse_semantic_prompt",
            "compare_semantic_graphs",
            "validate_semantic_graph_wire",
            "validate_semantic_evaluation_wire",
            "decode_semantic_evaluation_json",
            "wilson_interval",
        ):
            self.assertIn(name, semantic.__all__)
            self.assertTrue(hasattr(semantic, name), name)
        self.assertEqual(semantic.wilson_interval(20, 20), ("0.83887484", "1.00000000"))
        with self.assertRaises(semantic.SemanticGraphError):
            semantic.wilson_interval(21, 20)
        with self.assertRaises(semantic.SemanticGraphError):
            semantic.wilson_interval(0, 0)


if __name__ == "__main__":
    unittest.main()
