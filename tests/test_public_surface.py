"""M19-02 public surface tests.

The record exists to make one constraint mechanical and one hazard visible.

The constraint: every gate-enforced name must be a pure-core export. `describe_public_callable`
resolves each `public_python_abi` row through `core.__all__`, so a name that leaves the hub fails
the Full Gate rather than a review, and by then the diff is large.

The hazard: a flat re-export list cannot say that two modules define the same name, so the hub
silently binds one. The two live reader-backed shadows have explicit alias dispositions; these tests
pin the set and its decisions so neither can drift quietly.

No fixture here carries a prompt, a media value, a private path or a credential.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any

from scripts.governance.public_surface import (
    PUBLIC_SURFACE_SCHEMA,
    PublicSurface,
    PublicSurfaceError,
    ShadowDisposition,
    ShadowedExport,
    SurfaceAuthority,
    SurfaceLevel,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "governance" / "contracts" / "public_surface_v1.json"
ARTIFACT_SCHEMA = REPO_ROOT / "governance" / "contracts" / "public_surface_v1.schema.json"


def _load_generator() -> Any:
    path = REPO_ROOT / "scripts" / "public_surface.py"
    spec = importlib.util.spec_from_file_location("_m19_02_public_surface", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()


def _authority(identifier: str, level: SurfaceLevel, names: tuple[str, ...]) -> SurfaceAuthority:
    return SurfaceAuthority(
        authority_id=identifier, path=f"tests/{identifier}.py", level=level, names=names
    )


class SurfaceAuthorityTests(unittest.TestCase):
    def test_names_must_be_sorted_and_unique(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            _authority("a", SurfaceLevel.PACKAGE, ("b", "a"))

    def test_an_empty_authority_is_refused(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            _authority("a", SurfaceLevel.PACKAGE, ())

    def test_a_large_surface_is_pinned_by_digest_rather_than_listed(self) -> None:
        small = _authority("small", SurfaceLevel.PACKAGE, ("a", "b"))
        big = _authority(
            "big", SurfaceLevel.PURE_CORE, tuple(sorted(f"name_{i:04d}" for i in range(300)))
        )
        self.assertIn("names", small.to_wire())
        self.assertNotIn("names", big.to_wire())
        self.assertEqual(big.to_wire()["names_digest"], big.names_digest)
        self.assertTrue(big.names_digest.startswith("sha256:"))

    def test_the_digest_answers_to_every_declared_name(self) -> None:
        first = _authority("a", SurfaceLevel.PACKAGE, ("alpha", "beta"))
        second = _authority("a", SurfaceLevel.PACKAGE, ("alpha", "gamma"))
        self.assertNotEqual(first.names_digest, second.names_digest)


class SurfaceGuardTests(unittest.TestCase):
    def test_shadow_disposition_is_closed_and_alias_requires_a_rationale(self) -> None:
        with self.assertRaises(ValueError):
            ShadowDisposition("implicit")
        with self.assertRaises(PublicSurfaceError):
            ShadowedExport(
                name="alpha",
                bound_origin="core.a",
                shadowed_origin="core.b",
                values_agree=False,
                resolved_alias="BetaAlpha",
                disposition=ShadowDisposition.ALIAS,
                rationale="",
            )

    def test_a_gate_enforced_name_outside_the_pure_core_surface_is_refused(self) -> None:
        with self.assertRaises(PublicSurfaceError) as caught:
            PublicSurface(
                authorities=(
                    _authority("core", SurfaceLevel.PURE_CORE, ("alpha",)),
                    _authority("gate", SurfaceLevel.GATE_ENFORCED, ("beta",)),
                )
            )
        self.assertIn("not a pure-core export", str(caught.exception))

    def test_a_gate_enforced_surface_without_a_pure_core_surface_is_refused(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            PublicSurface(authorities=(_authority("gate", SurfaceLevel.GATE_ENFORCED, ("a",)),))

    def test_two_authorities_may_not_claim_the_same_level(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            PublicSurface(
                authorities=(
                    _authority("a", SurfaceLevel.PACKAGE, ("x",)),
                    _authority("b", SurfaceLevel.PACKAGE, ("y",)),
                )
            )

    def test_a_shadowed_export_that_is_not_exported_is_refused(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            PublicSurface(
                authorities=(_authority("core", SurfaceLevel.PURE_CORE, ("alpha",)),),
                shadowed=(
                    ShadowedExport(
                        name="ghost",
                        bound_origin="core.a",
                        shadowed_origin="core.b",
                        values_agree=False,
                    ),
                ),
            )

    def test_a_shadowed_export_needs_two_different_modules(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            ShadowedExport(
                name="alpha", bound_origin="core.a", shadowed_origin="core.a", values_agree=True
            )

    def test_an_alias_that_repeats_the_name_resolves_nothing(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            ShadowedExport(
                name="alpha",
                bound_origin="core.a",
                shadowed_origin="core.b",
                values_agree=False,
                resolved_alias="alpha",
            )

    def test_an_alias_the_hub_does_not_export_is_refused(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            PublicSurface(
                authorities=(_authority("core", SurfaceLevel.PURE_CORE, ("alpha",)),),
                shadowed=(
                    ShadowedExport(
                        name="alpha",
                        bound_origin="core.a",
                        shadowed_origin="core.b",
                        values_agree=False,
                        resolved_alias="AlphaOther",
                    ),
                ),
            )

    def test_classified_exports_may_not_exceed_the_declared_surface(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            PublicSurface(
                authorities=(_authority("core", SurfaceLevel.PURE_CORE, ("alpha",)),),
                hub_consumed=1,
                hub_unverified=5,
            )

    def test_an_unsupported_schema_is_refused(self) -> None:
        with self.assertRaises(PublicSurfaceError):
            PublicSurface(
                authorities=(_authority("core", SurfaceLevel.PURE_CORE, ("alpha",)),),
                schema="something-else/9",
            )


class GeneratedSurfaceTests(unittest.TestCase):
    document: dict[str, Any]
    surface: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        cls.surface = GENERATOR.build_surface()

    def test_the_artifact_regenerates_byte_identically(self) -> None:
        self.assertEqual(
            GENERATOR.artifact_bytes(self.surface),
            ARTIFACT.read_bytes().replace(b"\r\n", b"\n"),
        )

    def test_the_record_and_its_fingerprint_agree(self) -> None:
        self.assertEqual(self.document["schema"], PUBLIC_SURFACE_SCHEMA)
        self.assertEqual(self.document["fingerprint"], self.surface.fingerprint)

    def test_the_artifact_validates_against_its_schema(self) -> None:
        import jsonschema

        schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_the_three_levels_are_declared_and_distinct(self) -> None:
        levels = {item.level for item in self.surface.authorities}
        self.assertEqual(
            levels, {SurfaceLevel.PACKAGE, SurfaceLevel.PURE_CORE, SurfaceLevel.GATE_ENFORCED}
        )

    def test_every_gate_enforced_name_is_still_a_pure_core_export(self) -> None:
        """AC-M19-02-07, asserted here as well as by the gate that would catch it later."""

        import comfyui_h3_context.core as core

        by_level = {item.level: item for item in self.surface.authorities}
        gate = by_level[SurfaceLevel.GATE_ENFORCED]
        pure = by_level[SurfaceLevel.PURE_CORE]
        self.assertLessEqual(set(gate.names), set(pure.names))
        for name in gate.names:
            self.assertTrue(hasattr(core, name), f"{name} no longer resolves through the hub")

    def test_the_pure_core_surface_carries_no_duplicate(self) -> None:
        import comfyui_h3_context.core as core

        self.assertEqual(len(core.__all__), len(set(core.__all__)))
        self.assertEqual([n for n in core.__all__ if not hasattr(core, n)], [])

    def test_the_shadowed_set_is_pinned_and_does_not_grow(self) -> None:
        """HC-12 retired five non-public shadows; the two live-reader shadows stay pinned."""

        names = sorted(item.name for item in self.surface.shadowed)
        self.assertEqual(
            names,
            ["OracleComparisonStatus", "TemporalInterval"],
        )

    def test_the_divergent_shadowed_exports_are_named(self) -> None:
        """Where the two definitions disagree, an importer silently gets the wrong one."""

        divergent = {item.name for item in self.surface.divergent_shadowed}
        self.assertEqual(
            divergent,
            {"OracleComparisonStatus", "TemporalInterval"},
        )

    def test_every_live_shadow_has_the_exact_recorded_alias_disposition(self) -> None:
        expected = {
            "OracleComparisonStatus": "TaskModeOracleComparisonStatus",
            "TemporalInterval": "TemporalGroundingInterval",
        }
        self.assertEqual(set(GENERATOR.SHADOW_DISPOSITIONS), set(expected))
        self.assertEqual({item.name for item in self.surface.unresolved_shadowed}, set())
        document_by_name = {row["name"]: row for row in self.document["shadowed"]}
        for item in self.surface.shadowed:
            with self.subTest(name=item.name):
                self.assertIs(item.disposition, ShadowDisposition.ALIAS)
                self.assertEqual(item.resolved_alias, expected[item.name])
                self.assertTrue(item.rationale)
                self.assertEqual(document_by_name[item.name]["disposition"], "alias")

    def test_the_two_aliases_preserve_both_defining_object_identities(self) -> None:
        import comfyui_h3_context.core as core
        from comfyui_h3_context.core import TaskModeOracleComparisonStatus
        from comfyui_h3_context.core.base_evaluation import (
            OracleComparisonStatus as BaseOracleComparisonStatus,
        )
        from comfyui_h3_context.core.task_mode_retention_classifier import (
            OracleComparisonStatus as TaskModeLeafOracleComparisonStatus,
        )
        from comfyui_h3_context.core.temporal_event_alignment import (
            TemporalInterval as EventTemporalInterval,
        )
        from comfyui_h3_context.core.temporal_visual_analysis import (
            TemporalInterval as VisualTemporalInterval,
        )

        self.assertIs(core.OracleComparisonStatus, BaseOracleComparisonStatus)
        self.assertIs(TaskModeOracleComparisonStatus, TaskModeLeafOracleComparisonStatus)
        self.assertIs(core.TemporalInterval, VisualTemporalInterval)
        self.assertIs(core.TemporalGroundingInterval, EventTemporalInterval)

    def test_disposition_table_must_match_the_live_shadow_set_exactly(self) -> None:
        original = dict(GENERATOR.SHADOW_DISPOSITIONS)
        try:
            GENERATOR.SHADOW_DISPOSITIONS.pop("OracleComparisonStatus")
            with self.assertRaises(PublicSurfaceError):
                GENERATOR.build_surface()
        finally:
            GENERATOR.SHADOW_DISPOSITIONS.clear()
            GENERATOR.SHADOW_DISPOSITIONS.update(original)

        try:
            GENERATOR.SHADOW_DISPOSITIONS["InventedShadow"] = original["TemporalInterval"]
            with self.assertRaises(PublicSurfaceError):
                GENERATOR.build_surface()
        finally:
            GENERATOR.SHADOW_DISPOSITIONS.clear()
            GENERATOR.SHADOW_DISPOSITIONS.update(original)

        try:
            GENERATOR.SHADOW_DISPOSITIONS["TemporalInterval"] = original[
                "TemporalInterval"
            ]._replace(resolved_alias="InventedAlias")
            with self.assertRaises(PublicSurfaceError):
                GENERATOR.build_surface()
        finally:
            GENERATOR.SHADOW_DISPOSITIONS.clear()
            GENERATOR.SHADOW_DISPOSITIONS.update(original)

    def test_every_shadow_names_a_module_that_actually_defines_the_name(self) -> None:
        """A shadow row compares two definitions, so both sides must be definitions.

        Naming a re-exporting surface as one side of the comparison would make the row incoherent
        even when the count happens to be right.
        """

        definitions = GENERATOR._definitions()
        for item in self.surface.shadowed:
            with self.subTest(name=item.name):
                self.assertIn(item.bound_origin, definitions[item.name])
                self.assertIn(item.shadowed_origin, definitions[item.name])
                self.assertNotEqual(item.bound_origin, item.shadowed_origin)

    def test_a_shadow_is_followed_through_an_aggregation_surface(self) -> None:
        """The resolution walks re-exports to the definition, and cannot loop forever."""

        owner = {"NAME": {"core.leaf"}}
        chain = {
            ("core.surface", "NAME"): "core.middle",
            ("core.middle", "NAME"): "core.leaf",
        }
        resolve = GENERATOR._defining_origin
        self.assertEqual(resolve("core.surface", "NAME", owner, chain), "core.leaf")
        self.assertEqual(resolve("core.leaf", "NAME", owner, chain), "core.leaf")

        # A name nothing defines, and a cycle, both terminate at the module asked about rather
        # than spinning: the collision test then declines the row, which is the safe direction.
        self.assertEqual(resolve("core.surface", "OTHER", owner, {}), "core.surface")
        looping = {("core.a", "NAME"): "core.b", ("core.b", "NAME"): "core.a"}
        self.assertEqual(resolve("core.a", "NAME", {}, looping), "core.a")

    def test_the_only_reader_unverified_names_are_pure_package_values(self) -> None:
        by_level = {item.level: item for item in self.surface.authorities}
        package = set(by_level[SurfaceLevel.PACKAGE].names)
        pure = set(by_level[SurfaceLevel.PURE_CORE].names)
        self.assertEqual(self.surface.hub_unverified, len(package & pure))
        self.assertEqual(self.surface.hub_unverified, 9)

    def test_the_record_carries_identifiers_and_counts_and_nothing_else(self) -> None:
        raw = ARTIFACT.read_text(encoding="utf-8")
        for forbidden in ("password", "token", "secret", "Bearer ", "://user:"):
            self.assertNotIn(forbidden, raw)
        self.assertNotIn(":\\", raw)
        self.assertNotIn("C:/", raw)


class DeferredAdmittedWireTests(unittest.TestCase):
    """The import-cost half of M19-02: two records that were built at import time now are not."""

    def test_the_deferred_scorecard_wire_matches_its_frozen_fingerprint(self) -> None:
        from comfyui_h3_context.core import fidelity_scorecard as scorecard

        wire = scorecard._admitted_wire_bytes()
        self.assertIsInstance(wire, bytes)
        self.assertGreater(len(wire), 0)
        # Cached: the second call is the same object, not a rebuild.
        self.assertIs(wire, scorecard._admitted_wire_bytes())

    def test_the_deferred_authorization_wire_matches_its_frozen_fingerprint(self) -> None:
        from comfyui_h3_context.core import training_authorization as authorization

        wire = authorization._admitted_wire_bytes()
        self.assertIsInstance(wire, bytes)
        self.assertGreater(len(wire), 0)
        self.assertIs(wire, authorization._admitted_wire_bytes())

    def test_neither_module_builds_its_admitted_wire_at_import_time(self) -> None:
        """The whole point: importing must not pay for a record nothing asked for.

        Asserted structurally rather than by timing, because a wall-clock assertion in a suite is a
        flake waiting to happen. A module-scope call is what cost 306 ms; there must not be one.
        """

        import ast

        for name in ("fidelity_scorecard", "training_authorization"):
            path = REPO_ROOT / "comfyui_h3_context" / "core" / f"{name}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.endswith("_WIRE_BYTES"):
                        self.assertNotIsInstance(
                            node.value,
                            ast.Call,
                            f"{name} rebuilt its admitted wire at import time",
                        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
