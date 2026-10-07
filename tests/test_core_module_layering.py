"""M19-05 guard for the one-way import direction of the decomposed pure-core cohorts.

Each cohort is a module that was split into layers behind an aggregation surface. Two properties
make that split worth having, and neither is self-enforcing:

* the layers import downward only, so there is no cycle and no ambiguity about which layer owns a
  question;
* only the aggregation surface is imported from outside the cohort, so the layers stay free to move
  and a consumer cannot start answering a question the cohort owns.

`scripts/governance/architecture_inventory.py` reports cycles across the whole package, which
catches the worst case after the fact, but nothing asserted the intended direction of a particular
cohort or the privacy of its layers. M19-04's review found exactly this gap on the TypeScript side;
`tests/test_frontend_host_layering.py` is this module's counterpart there.

Imports are read with `ast`, so the edges are exact rather than inferred from name occurrences.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "comfyui_h3_context"
PKG_ROOT = ROOT / PACKAGE
CORE = PKG_ROOT / "core"

#: Cohort name -> its modules in dependency order. A module may import from the ones before it and
#: from none after. The last entry is the aggregation surface: the only one anything outside the
#: cohort may import.
COHORTS: dict[str, tuple[str, ...]] = {
    "semantic_graph": (
        "semantic_graph_primitives",
        "semantic_graph_model",
        "semantic_graph_diff",
        "semantic_graph_evaluation",
        "semantic_graph_wire",
        "semantic_graph_comparator",
    ),
    "node_contracts": (
        "node_contracts_types",
        "node_contracts_registry",
        "node_contracts",
    ),
    "workflow_migration": (
        "workflow_migration_primitives",
        "workflow_migration_expectations",
        "workflow_migration_subgraph_shape",
        "workflow_migration_api",
        "workflow_migration_subgraph",
        "workflow_migration",
    ),
}

SCAN_ROOTS = (PACKAGE, "tests", "scripts")


def _qualified(cohort_module: str) -> str:
    return f"{PACKAGE}.core.{cohort_module}"


def _imports(path: Path) -> set[str]:
    """Fully-qualified module names this file imports from, absolute and relative alike."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative_root = path.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
    package = relative_root.rsplit(".", 1)[0] if "." in relative_root else ""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    found.add(node.module)
                    # `from a.b import c` may name a module rather than an attribute.
                    found.update(f"{node.module}.{alias.name}" for alias in node.names)
                continue
            base = package
            for _ in range(node.level - 1):
                base = base.rsplit(".", 1)[0] if "." in base else ""
            target = f"{base}.{node.module}" if node.module else base
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)
    return found


def _sources() -> list[Path]:
    found: list[Path] = []
    for root in SCAN_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if "__pycache__" not in path.parts:
                found.append(path)
    return found


class CohortLayerDirectionTests(unittest.TestCase):
    def test_every_declared_module_exists(self) -> None:
        for cohort, modules in COHORTS.items():
            for name in modules:
                with self.subTest(cohort=cohort, module=name):
                    self.assertTrue((CORE / f"{name}.py").is_file(), f"{name}.py is missing")

    def test_the_declared_layers_are_every_module_of_the_cohort(self) -> None:
        """A module added to a cohort must be given a layer position, not just a file."""

        for cohort, modules in COHORTS.items():
            with self.subTest(cohort=cohort):
                on_disk = {path.stem for path in CORE.glob(f"{cohort}_*.py")} | {
                    path.stem for path in CORE.glob(f"{cohort}.py")
                }
                self.assertEqual(
                    on_disk,
                    set(modules),
                    f"{cohort}: the modules on disk and the declared layer order disagree. A "
                    "module added to the cohort must be given a position here, or its direction "
                    "is unchecked.",
                )

    def test_imports_only_ever_point_at_an_earlier_layer(self) -> None:
        violations: list[str] = []
        edges = 0
        for cohort, modules in COHORTS.items():
            rank = {_qualified(name): index for index, name in enumerate(modules)}
            for index, name in enumerate(modules):
                for target in _imports(CORE / f"{name}.py"):
                    if target not in rank:
                        continue
                    edges += 1
                    if rank[target] >= index:
                        violations.append(f"{cohort}: {name} imports {target.rsplit('.', 1)[1]}")
        self.assertEqual(
            violations,
            [],
            "A cohort layer may import only from the layers declared before it. Violations: "
            + "; ".join(violations),
        )
        self.assertGreater(edges, 5, "no intra-cohort imports found; the reader is broken")

    def test_the_aggregation_surface_is_never_imported_back(self) -> None:
        for cohort, modules in COHORTS.items():
            surface = _qualified(modules[-1])
            for name in modules[:-1]:
                with self.subTest(cohort=cohort, module=name):
                    self.assertNotIn(
                        surface,
                        _imports(CORE / f"{name}.py"),
                        f"{name} imports its own aggregation surface, which is a cycle",
                    )


class CohortPrivacyTests(unittest.TestCase):
    def test_only_the_aggregation_surface_is_imported_from_outside_the_cohort(self) -> None:
        offenders: list[str] = []
        scanned = 0
        for cohort, modules in COHORTS.items():
            internal = {_qualified(name) for name in modules[:-1]}
            own_files = {CORE / f"{name}.py" for name in modules}
            for path in _sources():
                if path in own_files:
                    continue
                scanned += 1
                reached = _imports(path) & internal
                for target in sorted(reached):
                    offenders.append(f"{path.relative_to(ROOT).as_posix()} -> {target} ({cohort})")
        self.assertEqual(
            offenders,
            [],
            "Only the aggregation surface is the door to a cohort. Importing a layer directly is "
            "how a second authority for the cohort's question starts. Offenders: "
            + "; ".join(offenders),
        )
        self.assertGreater(scanned, 100, "the source sweep found almost nothing; check SCAN_ROOTS")

    def test_the_aggregation_surface_has_readers(self) -> None:
        """A door nothing walks through would make the privacy test vacuous."""

        for cohort, modules in COHORTS.items():
            surface = _qualified(modules[-1])
            own_files = {CORE / f"{name}.py" for name in modules}
            readers = [
                path.relative_to(ROOT).as_posix()
                for path in _sources()
                if path not in own_files and surface in _imports(path)
            ]
            with self.subTest(cohort=cohort):
                self.assertNotEqual(readers, [], f"nothing imports {surface}")


if __name__ == "__main__":
    unittest.main()
