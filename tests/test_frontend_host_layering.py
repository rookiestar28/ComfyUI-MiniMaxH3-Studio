"""M19-04 guard for the one-way import direction of the frontend host graph modules.

The M19-04 decomposition cut `frontend/src/host/graphAdapter.ts` into layered modules and
rests on two structural claims: the layers only ever import downward, and the modules below the
aggregation surface are private to `frontend/src/host/`. Nothing in the repository enforced either
one.

`tsc --noEmit` does not: a cycle between ES modules is legal TypeScript and type-checks clean.
The Rolldown/oxc bundler behind `vite build` does not: it builds a cyclic module graph silently.
`scripts/governance/architecture_inventory.py` does not: it walks `*.py` only. There is no
ESLint configuration under `frontend/`. So the invariant was true by construction on the day it was
written and unprotected from that day forward, which is what this module fixes -- the same role
`tests/test_node_surface.py::HostIndependenceTests` plays for the Python node modules M19-03 split.

Import specifiers are read with a regular expression rather than a TypeScript parser, which is the
only option available in the Python lane. Every relative specifier it finds is resolved to a real
file on disk and an unresolvable one fails the test, so a mis-parse surfaces as a failure rather
than as a silent pass.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
HOST = FRONTEND / "src" / "host"

# The frozen layer order from the M19-04 plan section 4. A module may import from the modules above
# it in this tuple and from nothing below it.
GRAPH_LAYERS: tuple[str, ...] = (
    "graphSerialization",
    "graphRecognition",
    "graphPortShapes",
    "graphNodeContracts",
    "graphSubgraphLinks",
    "graphReferenceQualification",
    "graphTopology",
    "graphAdapter",
)

# `graphAdapter` is the aggregation surface -- the one door consumers import through.
# The layers below it are internal to `frontend/src/host/`.
PUBLIC_SURFACE = "graphAdapter"
INTERNAL_LAYERS: tuple[str, ...] = tuple(name for name in GRAPH_LAYERS if name != PUBLIC_SURFACE)

SKIP_DIRECTORIES = frozenset({"node_modules", "dist", "test-results", "test-results-host"})

# An import or re-export statement, up to the specifier it reads from. `[^;]` keeps the match inside
# one statement, so a declaration body cannot swallow a later import; the leading anchor keeps it to
# statement position. Multi-line brace lists are covered by DOTALL.
IMPORT_FROM = re.compile(
    r"^[ \t]*(?:import|export)\b[^;]*?\bfrom\s*[\"']([^\"']+)[\"']",
    re.MULTILINE | re.DOTALL,
)
SIDE_EFFECT_IMPORT = re.compile(r"^[ \t]*import\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _typescript_sources() -> list[Path]:
    """Every TypeScript source under `frontend/`, excluding installed and generated trees."""

    found: list[Path] = []
    for path in FRONTEND.rglob("*"):
        if path.suffix not in {".ts", ".tsx"} or not path.is_file():
            continue
        if SKIP_DIRECTORIES.intersection(part for part in path.relative_to(FRONTEND).parts):
            continue
        found.append(path)
    return sorted(found)


def _candidates(target: Path) -> tuple[Path, ...]:
    """The files a specifier could name, in TypeScript's resolution order."""

    found = [target, target.with_name(target.name + ".ts"), target.with_name(target.name + ".tsx")]
    # TypeScript resolves a `.js` specifier to the `.ts` source that emits it.
    if target.suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        found += [target.with_suffix(".ts"), target.with_suffix(".tsx")]
    found += [target / "index.ts", target / "index.tsx"]
    return tuple(found)


def _resolve(importer: Path, specifier: str) -> Path | None:
    """Resolve a relative import specifier to the file it names, or None if nothing matches."""

    if not specifier.startswith("."):
        return None
    # Vite carries a query or fragment on asset specifiers: `./styles/tokens.css?inline`.
    bare = specifier.split("?", 1)[0].split("#", 1)[0]
    target = (importer.parent / bare).resolve()
    for candidate in _candidates(target):
        if candidate.is_file():
            return candidate
    return None


def _local_imports(path: Path) -> list[tuple[str, Path]]:
    """The (specifier, resolved path) pairs this file imports from within the repository."""

    text = path.read_text(encoding="utf-8")
    specifiers = IMPORT_FROM.findall(text) + SIDE_EFFECT_IMPORT.findall(text)
    resolved: list[tuple[str, Path]] = []
    for specifier in specifiers:
        if not specifier.startswith("."):
            continue
        target = _resolve(path, specifier)
        if target is None:
            bare = specifier.split("?", 1)[0].split("#", 1)[0]
            naive = (path.parent / bare).resolve()
            if not naive.is_relative_to(FRONTEND):
                # ComfyUI supplies its own modules at runtime -- `entry.tsx` reaches the host's
                # `../../scripts/app.js`, which is not a file in this repository and never will be.
                continue
            raise AssertionError(
                f"{path.relative_to(ROOT)} imports {specifier!r}, which resolves to no file under "
                "frontend/. Either the import is broken or this test's specifier reader is wrong; "
                "both are worth failing on."
            )
        resolved.append((specifier, target))
    return resolved


class GraphLayerDirectionTests(unittest.TestCase):
    """The graph modules import downward only."""

    def test_every_declared_layer_exists(self) -> None:
        for name in GRAPH_LAYERS:
            with self.subTest(module=name):
                self.assertTrue((HOST / f"{name}.ts").is_file(), f"{name}.ts is missing")

    def test_the_declared_layers_are_every_graph_module(self) -> None:
        """A new sibling cannot join the cohort without joining this guard."""

        on_disk = {path.stem for path in HOST.glob("graph*.ts")}
        self.assertEqual(
            on_disk,
            set(GRAPH_LAYERS),
            "frontend/src/host/graph*.ts and GRAPH_LAYERS disagree. A module added to that "
            "cohort must be given a layer position here, otherwise its direction is unchecked.",
        )

    def test_imports_only_ever_point_at_an_earlier_layer(self) -> None:
        rank = {name: index for index, name in enumerate(GRAPH_LAYERS)}
        violations: list[str] = []
        edges: list[tuple[str, str]] = []
        for name in GRAPH_LAYERS:
            for specifier, target in _local_imports(HOST / f"{name}.ts"):
                if target.parent != HOST or target.stem not in rank:
                    continue
                edges.append((name, target.stem))
                if rank[target.stem] >= rank[name]:
                    violations.append(
                        f"{name}.ts imports {specifier} "
                        f"(layer {rank[target.stem]} from layer {rank[name]})"
                    )
        self.assertEqual(
            violations,
            [],
            "The M19-04 layering is one-way: a module may import only from the layers above it in "
            "GRAPH_LAYERS. Violations: " + "; ".join(violations),
        )
        # A vacuous pass would be indistinguishable from a real one, so assert the edges exist.
        self.assertGreater(len(edges), 15, "no intra-cohort imports found; the reader is broken")

    def test_the_aggregation_surface_is_never_imported_back(self) -> None:
        for name in INTERNAL_LAYERS:
            imported = {target.stem for _, target in _local_imports(HOST / f"{name}.ts")}
            with self.subTest(module=name):
                self.assertNotIn(
                    PUBLIC_SURFACE,
                    imported,
                    f"{name}.ts imports {PUBLIC_SURFACE}, which makes the public door a cycle",
                )


class GraphModulePrivacyTests(unittest.TestCase):
    """The modules below the aggregation surface stay inside `frontend/src/host/`."""

    def test_nothing_outside_the_host_directory_imports_an_internal_module(self) -> None:
        internal = {HOST / f"{name}.ts" for name in INTERNAL_LAYERS}
        outside: list[str] = []
        scanned = 0
        for path in _typescript_sources():
            scanned += 1
            if path.parent == HOST:
                continue
            for specifier, target in _local_imports(path):
                if target in internal:
                    outside.append(f"{path.relative_to(ROOT).as_posix()} -> {specifier}")
        self.assertEqual(
            outside,
            [],
            "Only frontend/src/host/graphAdapter.ts is the door to the graph cohort. Importing an "
            "internal layer directly is how a second graph authority starts. Offenders: "
            + "; ".join(outside),
        )
        self.assertGreater(scanned, 100, "the source sweep found almost nothing; check SKIP rules")

    def test_consumers_reach_the_cohort_through_the_aggregation_surface(self) -> None:
        """At least one file outside `host/` still imports `graphAdapter`, so the door is real."""

        door = HOST / f"{PUBLIC_SURFACE}.ts"
        importers = [
            path.relative_to(ROOT).as_posix()
            for path in _typescript_sources()
            if path != door
            for _, target in _local_imports(path)
            if target == door
        ]
        self.assertNotEqual(importers, [], "nothing imports graphAdapter; the cohort is orphaned")


if __name__ == "__main__":
    unittest.main()
