"""M19-01 architecture inventory tests.

The baseline is only worth having if it refuses to record the things that would make it a lie.
Most of these are about what it will not hold: a forbidden pure-core dependency, a layer inversion,
a cycle that closes at import time, a cohort proposing to move a module nothing tests.

Three tests exist because the obvious implementation of a measurement was tried first and was
wrong. A single owning-test rule reports a module with a dedicated 600-line suite as untested. A
filename-similarity rule attributes a `core` module's suite to its identically named adapter twin.
Resolving `from . import x` to the package rather than the submodule manufactures import cycles that
do not exist. Each has a test named after the mistake, so re-making it fails here rather than in a
record someone trusts.

No fixture here carries a prompt, a media value, a private path or a credential.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any

from scripts.governance.architecture_inventory import (
    ARCHITECTURE_INVENTORY_SCHEMA,
    AcceptanceBinding,
    ArchitectureInventory,
    ArchitectureInventoryError,
    Cohort,
    CyclicComponent,
    EdgeKind,
    ImportEdge,
    Layer,
    ModuleDisposition,
    ModuleRow,
    OwnerRule,
    PublicSurface,
    SurfaceOverlap,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "governance" / "contracts" / "architecture_inventory_v1.json"
ARTIFACT_SCHEMA = REPO_ROOT / "governance" / "contracts" / "architecture_inventory_v1.schema.json"


def _load_generator() -> Any:
    path = REPO_ROOT / "scripts" / "architecture_inventory.py"
    spec = importlib.util.spec_from_file_location("_m19_01_architecture_inventory", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()


def _row(module: str, **overrides: Any) -> ModuleRow:
    fields: dict[str, Any] = {
        "module": module,
        "layer": Layer.CORE,
        "lines": 10,
        "inbound": 0,
        "outbound": 0,
        "owning_tests": ("tests/test_example.py",),
        "owning_rules": (OwnerRule.DIRECT_MODULE,),
    }
    fields.update(overrides)
    return ModuleRow(**fields)


class ModuleRowTests(unittest.TestCase):
    def test_a_module_no_test_imports_is_not_movable(self) -> None:
        row = _row("core.alpha", owning_tests=(), owning_rules=())
        self.assertIs(row.disposition, ModuleDisposition.NEED_EVIDENCE)

    def test_a_module_some_test_imports_is_movable(self) -> None:
        self.assertIs(_row("core.alpha").disposition, ModuleDisposition.MOVABLE)

    def test_a_test_without_the_rule_that_found_it_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            _row("core.alpha", owning_rules=())

    def test_a_rule_that_found_nothing_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            _row("core.alpha", owning_tests=())

    def test_owning_tests_must_be_sorted_and_unique(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            _row("core.alpha", owning_tests=("tests/test_b.py", "tests/test_a.py"))

    def test_a_negative_edge_count_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            _row("core.alpha", inbound=-1)

    def test_an_absolute_owning_test_path_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            _row("core.alpha", owning_tests=("/etc/passwd",))


class ImportEdgeTests(unittest.TestCase):
    def test_an_edge_carrying_no_kind_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            ImportEdge(source="core.a", target="core.b", kinds=())

    def test_a_self_loop_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            ImportEdge(source="core.a", target="core.a", kinds=(EdgeKind.MODULE_LEVEL,))

    def test_an_edge_is_deferred_only_when_nothing_runs_at_import_time(self) -> None:
        deferred = ImportEdge(
            source="core.a",
            target="core.b",
            kinds=(EdgeKind.FUNCTION_LOCAL, EdgeKind.TYPE_CHECKING),
        )
        self.assertTrue(deferred.deferred)
        mixed = ImportEdge(
            source="core.a",
            target="core.b",
            kinds=(EdgeKind.FUNCTION_LOCAL, EdgeKind.MODULE_LEVEL),
        )
        self.assertFalse(mixed.deferred)


class CyclicComponentTests(unittest.TestCase):
    def test_a_component_must_say_what_closes_it(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            CyclicComponent(members=("core.a", "core.b"), closes_through=())

    def test_a_component_of_one_is_not_a_cycle(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            CyclicComponent(members=("core.a",), closes_through=(EdgeKind.TYPE_CHECKING,))

    def test_a_component_closing_at_import_time_is_a_runtime_cycle(self) -> None:
        component = CyclicComponent(
            members=("core.a", "core.b"), closes_through=(EdgeKind.MODULE_LEVEL,)
        )
        self.assertTrue(component.runtime_cycle)


class InventoryGuardTests(unittest.TestCase):
    """The four things the record refuses, each of which is the regression worth catching."""

    def test_a_forbidden_pure_core_import_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            ArchitectureInventory(
                modules=(_row("core.alpha"),), forbidden_imports=("core.alpha -> torch",)
            )

    def test_a_layer_inversion_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            ArchitectureInventory(
                modules=(_row("core.alpha"),), layer_inversions=("core.alpha -> nodes",)
            )

    def test_a_module_level_import_cycle_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            ArchitectureInventory(
                modules=(_row("core.alpha"), _row("core.beta")),
                cycles=(
                    CyclicComponent(
                        members=("core.alpha", "core.beta"),
                        closes_through=(EdgeKind.MODULE_LEVEL,),
                    ),
                ),
            )

    def test_a_deferred_cycle_is_recorded_rather_than_refused(self) -> None:
        inventory = ArchitectureInventory(
            modules=(_row("core.alpha"), _row("core.beta")),
            cycles=(
                CyclicComponent(
                    members=("core.alpha", "core.beta"),
                    closes_through=(EdgeKind.TYPE_CHECKING,),
                ),
            ),
        )
        self.assertEqual(len(inventory.cycles), 1)
        self.assertEqual(inventory.runtime_cycles, ())

    def test_a_cohort_may_not_move_a_module_with_no_owning_test(self) -> None:
        with self.assertRaises(ArchitectureInventoryError) as caught:
            ArchitectureInventory(
                modules=(_row("core.alpha", owning_tests=(), owning_rules=()),),
                cohorts=(
                    Cohort(
                        cohort_id="c",
                        successor="M19-02",
                        modules=("core.alpha",),
                        reason="because",
                    ),
                ),
            )
        self.assertIn("no owning test", str(caught.exception))

    def test_a_cohort_may_not_name_a_module_outside_the_inventory(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            ArchitectureInventory(
                modules=(_row("core.alpha"),),
                cohorts=(
                    Cohort(
                        cohort_id="c", successor="M19-02", modules=("core.ghost",), reason="because"
                    ),
                ),
            )

    def test_an_edge_that_runs_at_import_time_is_not_a_deferred_edge(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            ArchitectureInventory(
                modules=(_row("core.alpha"), _row("core.beta")),
                deferred_edges=(
                    ImportEdge(
                        source="core.alpha",
                        target="core.beta",
                        kinds=(EdgeKind.MODULE_LEVEL,),
                    ),
                ),
            )

    def test_a_surface_may_not_overlap_more_than_it_declares(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            PublicSurface(
                surface_id="small",
                authority="a/b.py",
                declared=2,
                overlaps=(SurfaceOverlap(other="big", shared=3),),
            )

    def test_an_unsupported_schema_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            ArchitectureInventory(modules=(_row("core.alpha"),), schema="something-else/9")


class GeneratedInventoryTests(unittest.TestCase):
    """The committed record, and the repository it claims to describe."""

    document: dict[str, Any]
    inventory: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        cls.inventory = GENERATOR.build_inventory()

    def test_the_artifact_regenerates_byte_identically(self) -> None:
        self.assertEqual(
            GENERATOR.artifact_bytes(self.inventory),
            ARTIFACT.read_bytes().replace(b"\r\n", b"\n"),
        )

    def test_the_record_and_its_fingerprint_agree(self) -> None:
        self.assertEqual(self.document["schema"], ARCHITECTURE_INVENTORY_SCHEMA)
        self.assertEqual(self.document["fingerprint"], self.inventory.fingerprint)

    def test_the_artifact_validates_against_its_schema(self) -> None:
        import jsonschema

        schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_the_pure_core_reaches_no_host_runtime_or_network_stack(self) -> None:
        self.assertEqual(self.document["forbidden_imports"], [])
        self.assertEqual(self.document["layer_inversions"], [])

    def test_no_cycle_closes_at_import_time(self) -> None:
        """The number that matters. Two static components exist; neither breaks an import."""

        self.assertEqual(self.inventory.runtime_cycles, ())
        for component in self.inventory.cycles:
            self.assertNotIn(EdgeKind.MODULE_LEVEL, component.closes_through)
            self.assertGreaterEqual(len(component.members), 2)

    def test_every_recorded_cycle_is_held_open_by_a_recorded_deferred_edge(self) -> None:
        """A component that closes through deferred edges must name at least one of them."""

        deferred = {(edge.source, edge.target) for edge in self.inventory.deferred_edges}
        for component in self.inventory.cycles:
            members = set(component.members)
            internal = {pair for pair in deferred if pair[0] in members and pair[1] in members}
            self.assertTrue(internal, f"{component.members} closes through nothing recorded")

    def test_unresolved_modules_are_named_rather_than_counted(self) -> None:
        unresolved = [row.module for row in self.inventory.need_evidence]
        self.assertEqual(unresolved, sorted(unresolved))
        # Fail-closed states, not a to-do list: each blocks a move and none is called unused.
        for row in self.inventory.need_evidence:
            self.assertEqual(row.owning_tests, ())
            self.assertTrue(row.reasons)
        for cohort in self.inventory.cohorts:
            self.assertFalse(set(cohort.modules) & set(unresolved))

    def test_the_three_surface_levels_and_their_containment_are_recorded(self) -> None:

        surfaces = {item.surface_id: item for item in self.inventory.surfaces}
        self.assertEqual(set(surfaces), {"acceptance_abi", "core_all", "public_api"})
        overlap = {
            (item.surface_id, link.other): link.shared
            for item in self.inventory.surfaces
            for link in item.overlaps
        }
        # The ABI baseline and the declared public API name no symbol in common.
        self.assertEqual(overlap[("acceptance_abi", "public_api")], 0)
        self.assertEqual(overlap[("public_api", "acceptance_abi")], 0)
        # Every ABI row resolves through the hub; six host-metadata facades stay outside it.
        self.assertEqual(
            overlap[("acceptance_abi", "core_all")], surfaces["acceptance_abi"].declared
        )
        self.assertLess(overlap[("public_api", "core_all")], surfaces["public_api"].declared)
        self.assertEqual(overlap[("public_api", "core_all")], 9)
        self.assertEqual(overlap[("core_all", "public_api")], 9)
        # The implementation hub remains larger than either contract-level authority.
        self.assertGreater(surfaces["core_all"].declared, 10 * surfaces["public_api"].declared)

    def test_every_acceptance_binding_names_the_successor_that_owns_it(self) -> None:
        baseline = json.loads((REPO_ROOT / "tests" / "acceptance_baseline.json").read_text("utf-8"))
        abi = {row["export"] for row in baseline["public_python_abi"]}
        criteria = {row["id"] for row in baseline["criteria"]}
        recorded_abi = {
            item.binding_id for item in self.inventory.bindings if item.kind == "public_python_abi"
        }
        recorded_criteria = {
            item.binding_id for item in self.inventory.bindings if item.kind == "criterion"
        }
        self.assertEqual(recorded_abi, abi)
        self.assertEqual(recorded_criteria, criteria)
        # Every ABI row is M19-02's, because export reduction is the only cohort that can drop a
        # name from core.__all__ and break `describe_public_callable`.
        for item in self.inventory.bindings:
            if item.kind == "public_python_abi":
                self.assertEqual(item.owner, "M19-02")

    def test_the_criteria_concentrate_on_the_frontend_cohort(self) -> None:
        """20 of 23 criteria are vitest rows, which is not what M19-04's size suggests."""

        owners = [item.owner for item in self.inventory.bindings if item.kind == "criterion"]
        self.assertGreaterEqual(owners.count("M19-04"), 20)

    def test_the_record_carries_identifiers_and_counts_and_nothing_else(self) -> None:
        raw = ARTIFACT.read_text(encoding="utf-8")
        for forbidden in ("password", "token", "secret", "Bearer ", "://user:"):
            self.assertNotIn(forbidden, raw)
        # Repository-relative paths only: no drive letter, no home directory, no UNC path.
        self.assertNotIn(":\\", raw)
        self.assertNotIn("C:/", raw)


class DiscoveryRuleTests(unittest.TestCase):
    """Each of these is a measurement that was tried the obvious way first and came out wrong."""

    inventory: Any
    rows: dict[str, ModuleRow]

    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = GENERATOR.build_inventory()
        cls.rows = {row.module: row for row in cls.inventory.modules}

    def test_a_module_reached_only_by_a_dynamic_import_resolves_to_its_real_suite(self) -> None:
        """`core.semantic_graph_comparator` has a dedicated suite reached via import_module."""

        row = self.rows["core.semantic_graph_comparator"]
        self.assertIn(OwnerRule.IMPORTLIB_LITERAL, row.owning_rules)
        self.assertIn("tests/test_semantic_graph_comparator.py", row.owning_tests)

    def test_a_module_reached_only_through_the_hub_resolves_to_its_real_suite(self) -> None:
        """Rule 4 exists because core/__init__.py re-exports two thousand names."""

        row = self.rows["core.rendering"]
        self.assertIn(OwnerRule.HUB_SYMBOL, row.owning_rules)
        self.assertTrue(row.owning_tests)

    def test_every_renderer_qualification_subject_selects_the_qualification_guard(self) -> None:
        """The qualification binds files by content; no import edge reaches its guard.

        A change to `av_reconstruction_media.py` revoked final output while the owning-test
        selection, found by imports alone, never named the test that asserts the fingerprint.
        """

        from comfyui_h3_context.adapters import authoring_renderer_qualification

        # Read by key: naming the constant here would make this suite a qualification guard too.
        subjects = vars(authoring_renderer_qualification)["_IMPLEMENTATION_PATHS"]
        guard = "tests/test_m25_native_renderer.py"
        self.assertGreater(len(subjects), 20)
        for relative in subjects:
            module = relative.removesuffix(".py").replace("/", ".")
            with self.subTest(module=module):
                row = self.rows[module]
                self.assertIn(OwnerRule.QUALIFICATION_SUBJECT, row.owning_rules)
                self.assertIn(guard, row.owning_tests)
        unrelated = self.rows["adapters.media_runtime_manager"]
        self.assertNotIn(OwnerRule.QUALIFICATION_SUBJECT, unrelated.owning_rules)

    def test_an_adapter_is_not_credited_with_its_core_twins_suite(self) -> None:
        """A filename-similarity rule someone will want to add, refused by construction.

        `core/` and `adapters/` hold identically named pairs, and the test file named after each
        imports the core one only. A filename-similarity rule would turn an honest unresolved row
        into a confident wrong one on the adapter side, where host and media I/O live.
        """

        for name in ("perception_tracking", "temporal_visual_analysis", "visual_evidence_fusion"):
            core_row = self.rows[f"core.{name}"]
            adapter_row = self.rows[f"adapters.{name}"]
            self.assertTrue(core_row.owning_tests, f"core.{name} should resolve")
            self.assertEqual(
                adapter_row.owning_tests, (), f"adapters.{name} must not inherit the core suite"
            )

    def test_importing_a_submodule_from_its_package_does_not_depend_on_the_package(self) -> None:
        """`from . import x` depends on `core.x`, not on `core/__init__.py`.

        Resolving it to the package made every submodule appear to import the hub and reported a
        four-module module-level cycle that does not exist.
        """

        scan = GENERATOR.Scan()
        self.assertIn(("core.fidelity_scorecard", "core.fixed_h3_generation"), scan.edges)
        self.assertNotIn(("core.fidelity_scorecard", "core.__init__"), scan.edges)

    def test_a_symbol_defined_by_two_modules_attributes_to_neither(self) -> None:
        scan = GENERATOR.Scan()
        owner_of: dict[str, set[str]] = {}
        for module, names in scan.defines.items():
            for symbol in names:
                owner_of.setdefault(symbol, set()).add(module)
        ambiguous = {symbol for symbol, owners in owner_of.items() if len(owners) > 1}
        self.assertTrue(ambiguous, "expected at least one name defined twice")
        owners = GENERATOR.build_owners(scan)
        for symbol in ambiguous:
            for module in owner_of[symbol]:
                found = owners.get(module, {}).get(OwnerRule.HUB_SYMBOL, set())
                # The ambiguous symbol alone never credits a module; another rule may still.
                self.assertNotIn(symbol, found)


class BindingTests(unittest.TestCase):
    def test_a_binding_kind_outside_the_baseline_is_refused(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            AcceptanceBinding(
                binding_id="x", kind="invented", path="tests/test_a.py", owner="M19-02"
            )

    def test_a_cohort_needs_a_reason(self) -> None:
        with self.assertRaises(ArchitectureInventoryError):
            Cohort(cohort_id="c", successor="M19-02", modules=("core.a",), reason="")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
