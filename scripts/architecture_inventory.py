"""Generate the M19-01 architecture inventory: what is here, what couples, what may move.

Static `ast` parsing only.  Nothing in the package under study is imported, so a module with an
import-time side effect cannot influence its own baseline.

Three measurements here are deliberately not the obvious one, and each is the obvious one being
wrong rather than a preference:

* the layer of a module is **declared**, because inferring it from the path made `nodes.py` its own
  tier and turned four lateral edges between sibling node-UI modules into reported inversions;
* an import edge carries its **kind**, because counting every ``ImportFrom`` reports two cycles that
  are deliberate constructions while ignoring deferred imports reports none and hides the coupling;
* an owning test is found by **four** import rules, because one rule leaves 65 modules looking
  untested, including a module with a 600-line suite reached through ``importlib.import_module``;
  a fifth, non-import rule attributes the renderer qualification guard to every file that
  qualification fingerprints, because that coupling has no import edge at all.

A rule matching `tests/test_X.py` to module `X` is deliberately absent: `core/` and
`adapters/` hold four identically named module pairs, and all three of the tempting matches import
the `core` module only.  Guessing there would convert an honest unresolved row into a confident
wrong one on the adapter side, which is where host and media I/O live.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from collections.abc import Iterator, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.governance.architecture_inventory import (  # noqa: E402
    AcceptanceBinding,
    ArchitectureInventory,
    ArchitectureInventoryError,
    Cohort,
    CyclicComponent,
    EdgeKind,
    ImportEdge,
    Layer,
    ModuleRow,
    OwnerRule,
    PublicSurface,
    SurfaceOverlap,
)

ARTIFACT_PATH = Path("governance/contracts/architecture_inventory_v1.json")

PACKAGE = "comfyui_h3_context"
PKG_ROOT = ROOT / PACKAGE
TESTS_ROOT = ROOT / "tests"
BASELINE_PATH = ROOT / "tests" / "acceptance_baseline.json"

#: Import roots that re-export rather than define.  A test importing a name from one of these is
#: evidence about the module that *defines* the name, which is what rule 4 resolves.
HUBS = (PACKAGE, f"{PACKAGE}.core", f"{PACKAGE}.adapters", f"{PACKAGE}.public_api")

#: The pure core may not reach a host, a runtime, a network stack or a numeric/media library.
FORBIDDEN_CORE_IMPORTS = frozenset(
    {
        "comfy",
        "folder_paths",
        "nodes",
        "server",
        "aiohttp",
        "httpx",
        "requests",
        "torch",
        "numpy",
        "PIL",
        "cv2",
        "transformers",
    }
)

LAYER_RANK = {Layer.CORE: 0, Layer.ADAPTERS: 1, Layer.NODE_UI: 2}


def _module_name(path: Path) -> str:
    return path.relative_to(PKG_ROOT).with_suffix("").as_posix().replace("/", ".")


def _layer_of(name: str) -> Layer:
    head = name.split(".")[0]
    if head == "core":
        return Layer.CORE
    if head == "adapters":
        return Layer.ADAPTERS
    return Layer.NODE_UI


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _normalize(target: str, modules: set[str]) -> str | None:
    """Resolve a dotted name to an inventoried module, mapping a package to its `__init__`."""

    if target in modules:
        return target
    package_init = f"{target}.__init__"
    if package_init in modules:
        return package_init
    return None


def _resolve(name: str, node: ast.ImportFrom) -> str | None:
    if node.level:
        parts = name.split(".")
        # A relative import inside `pkg/sub/__init__.py` is anchored on `pkg.sub`, not `pkg`.
        if parts[-1] == "__init__":
            parts = parts[:-1]
            base = parts[: len(parts) - node.level + 1]
        else:
            base = parts[: len(parts) - node.level]
        target = ".".join([*base, node.module]) if node.module else ".".join(base)
        return target or None
    if node.module and node.module.startswith(PACKAGE):
        return node.module.removeprefix(PACKAGE).lstrip(".") or "__init__"
    return None


def _classified(name: str, tree: ast.Module) -> Iterator[tuple[str, tuple[str, ...], EdgeKind]]:
    """Every intra-package import in one module, tagged with whether it runs at import time.

    Yields the dotted base together with the imported names, because `from . import x` depends on
    the submodule `x` and not on the package's `__init__`.  Resolving it to the package instead
    makes every submodule appear to depend on the hub and manufactures import cycles that do not
    exist -- which is exactly what the first run of this generator reported.
    """

    def kind_of(in_function: bool, in_tc: bool) -> EdgeKind:
        if in_tc:
            return EdgeKind.TYPE_CHECKING
        return EdgeKind.FUNCTION_LOCAL if in_function else EdgeKind.MODULE_LEVEL

    def from_import(
        node: ast.ImportFrom, kind: EdgeKind
    ) -> Iterator[tuple[str, tuple[str, ...], EdgeKind]]:
        target = _resolve(name, node)
        if target:
            yield target, tuple(alias.name for alias in node.names), kind

    def walk(
        node: ast.AST, in_function: bool, in_tc: bool
    ) -> Iterator[tuple[str, tuple[str, ...], EdgeKind]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ImportFrom):
                yield from from_import(child, kind_of(in_function, in_tc))
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name.startswith(f"{PACKAGE}."):
                        base = alias.name.removeprefix(f"{PACKAGE}.")
                        yield base, (), kind_of(in_function, in_tc)
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                yield from walk(child, True, in_tc)
            elif isinstance(child, ast.If) and _is_type_checking(child.test):
                for stmt in child.body:
                    if isinstance(stmt, ast.ImportFrom):
                        yield from from_import(stmt, EdgeKind.TYPE_CHECKING)
                    yield from walk(stmt, in_function, True)
                for stmt in child.orelse:
                    yield from walk(stmt, in_function, in_tc)
            else:
                yield from walk(child, in_function, in_tc)

    yield from walk(tree, False, False)


def _public_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    if not target.id.startswith("_"):
                        names.add(target.id)
    return names


def _dunder_all(tree: ast.Module) -> list[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            if isinstance(node.value, ast.List | ast.Tuple):
                return [
                    element.value
                    for element in node.value.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                ]
    return []


def _strongly_connected(members: set[str], adjacency: dict[str, set[str]]) -> bool:
    """Is every member reachable from one arbitrary member, and does it reach that member back?"""

    if len(members) < 2:
        return False
    start = min(members)

    def reach(graph: dict[str, set[str]]) -> set[str]:
        seen = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in graph.get(node, set()):
                if nxt in members and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    reverse: dict[str, set[str]] = defaultdict(set)
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)
    return reach(adjacency) >= members and reach(reverse) >= members


def _components(adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan, so a deep graph cannot exhaust the recursion limit."""

    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    found: list[list[str]] = []
    nodes = set(adjacency) | {t for targets in adjacency.values() for t in targets}
    for root in sorted(nodes):
        if root in index_of:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(adjacency.get(root, set())))]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                child = pending.pop()
                if child not in index_of:
                    index_of[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, sorted(adjacency.get(child, set()))))
                elif child in on_stack:
                    low[node] = min(low[node], index_of[child])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index_of[node]:
                    component: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1:
                        found.append(sorted(component))
    return found


class Scan:
    """One static pass over the package, reused by every builder below."""

    def __init__(self) -> None:
        self.trees: dict[str, ast.Module] = {}
        self.lines: dict[str, int] = {}
        self.defines: dict[str, set[str]] = {}
        for path in sorted(PKG_ROOT.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            name = _module_name(path)
            text = path.read_text(encoding="utf-8")
            self.trees[name] = ast.parse(text)
            self.lines[name] = text.count("\n")
            self.defines[name] = _public_names(self.trees[name])
        self.modules = set(self.trees)

        self.edges: dict[tuple[str, str], set[EdgeKind]] = defaultdict(set)
        self.forbidden: list[str] = []
        self.inversions: list[str] = []
        for name, tree in self.trees.items():
            layer = _layer_of(name)
            for raw_target, aliases, kind in _classified(name, tree):
                # `from .pkg import sub` depends on `pkg.sub`; only fall back to the base when no
                # imported name is itself a module.
                submodules = [
                    resolved
                    for alias in aliases
                    if (
                        resolved := _normalize(
                            f"{raw_target}.{alias}" if raw_target else alias, self.modules
                        )
                    )
                ]
                base = _normalize(raw_target, self.modules)
                for target in submodules or ([base] if base else []):
                    if target == name:
                        continue
                    self.edges[(name, target)].add(kind)
                    if LAYER_RANK[_layer_of(target)] > LAYER_RANK[layer]:
                        self.inversions.append(f"{name} -> {target}")
            if layer is not Layer.CORE:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in FORBIDDEN_CORE_IMPORTS:
                            self.forbidden.append(f"{name} -> {alias.name}")
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    head = (node.module or "").split(".")[0]
                    if head in FORBIDDEN_CORE_IMPORTS:
                        self.forbidden.append(f"{name} -> {node.module}")

    def adjacency(self, kinds: set[EdgeKind]) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = defaultdict(set)
        for (source, target), carried in self.edges.items():
            if carried & kinds:
                graph[source].add(target)
        return graph


def build_owners(scan: Scan) -> dict[str, dict[OwnerRule, set[str]]]:
    """Four discovery rules.  An ambiguous hub symbol attributes to no module at all."""

    owner_of_symbol: dict[str, set[str]] = defaultdict(set)
    for module, names in scan.defines.items():
        for symbol in names:
            owner_of_symbol[symbol].add(module)

    owners: dict[str, dict[OwnerRule, set[str]]] = defaultdict(lambda: defaultdict(set))
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken test file is its own failure
            continue
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not alias.name.startswith(f"{PACKAGE}."):
                        continue
                    target = _normalize(alias.name.removeprefix(f"{PACKAGE}."), scan.modules)
                    if target:
                        owners[target][OwnerRule.DIRECT_MODULE].add(relative)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if not node.module.startswith(PACKAGE):
                    continue
                base = node.module.removeprefix(PACKAGE).lstrip(".")
                direct = _normalize(base or "__init__", scan.modules)
                if direct:
                    owners[direct][OwnerRule.DIRECT_MODULE].add(relative)
                for alias in node.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    submodule = _normalize(candidate, scan.modules)
                    if submodule:
                        owners[submodule][OwnerRule.SUBMODULE_AS_NAME].add(relative)
                    elif node.module in HUBS:
                        defining = owner_of_symbol.get(alias.name, set())
                        if len(defining) == 1:
                            owners[next(iter(defining))][OwnerRule.HUB_SYMBOL].add(relative)
            elif isinstance(node, ast.Call):
                func = node.func
                dynamic = (isinstance(func, ast.Attribute) and func.attr == "import_module") or (
                    isinstance(func, ast.Name) and func.id == "import_module"
                )
                if dynamic and node.args:
                    argument = node.args[0]
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        target = _normalize(
                            argument.value.removeprefix(f"{PACKAGE}."), scan.modules
                        )
                        if target:
                            owners[target][OwnerRule.IMPORTLIB_LITERAL].add(relative)
    for module, tests in _qualification_subject_owners(scan).items():
        owners[module][OwnerRule.QUALIFICATION_SUBJECT].update(tests)
    return owners


#: The packaged renderer qualification binds these files by content fingerprint, not by import.
QUALIFICATION_LOADER = PKG_ROOT / "adapters" / "authoring_renderer_qualification.py"
QUALIFICATION_SUBJECT_NAMES = frozenset(
    {"_IMPLEMENTATION_PATHS", "renderer_implementation_fingerprints"}
)


def _qualification_subject_owners(scan: Scan) -> dict[str, set[str]]:
    """Every fingerprinted implementation file is owned by the tests that assert the fingerprint.

    IMPORTANT: an import-based rule cannot see this coupling. `av_reconstruction_media.py` changed
    in M25-30 without selecting the qualification test, which silently revoked final output on every
    host for two accepted items. A change to any subject file must select the qualification guard.
    """

    subjects: set[str] = set()
    tree = ast.parse(QUALIFICATION_LOADER.read_text(encoding="utf-8"))
    for statement in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.AnnAssign):
            target, value = statement.target, statement.value
        elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        if not (isinstance(target, ast.Name) and target.id == "_IMPLEMENTATION_PATHS"):
            continue
        if not isinstance(value, ast.Tuple):
            raise ArchitectureInventoryError("qualification subject paths are not a literal tuple")
        for element in value.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                raise ArchitectureInventoryError("qualification subject path is not a literal")
            module = _normalize(element.value.removesuffix(".py").replace("/", "."), scan.modules)
            if module is None:
                raise ArchitectureInventoryError("qualification subject path names no module")
            subjects.add(module)
    if not subjects:
        raise ArchitectureInventoryError("qualification subject paths were not found")

    guards: set[str] = set()
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        try:
            test_tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken test file is its own failure
            continue
        for found in ast.walk(test_tree):
            name = (
                found.attr
                if isinstance(found, ast.Attribute)
                else found.id
                if isinstance(found, ast.Name)
                else None
            )
            if name in QUALIFICATION_SUBJECT_NAMES:
                guards.add(path.relative_to(ROOT).as_posix())
                break
    return {module: set(guards) for module in subjects}


def build_modules(
    scan: Scan, owners: dict[str, dict[OwnerRule, set[str]]]
) -> tuple[ModuleRow, ...]:
    inbound: dict[str, int] = defaultdict(int)
    outbound: dict[str, int] = defaultdict(int)
    for source, target in scan.edges:
        outbound[source] += 1
        inbound[target] += 1

    rows: list[ModuleRow] = []
    for name in sorted(scan.modules):
        found = owners.get(name, {})
        tests = sorted({path for paths in found.values() for path in paths})
        rules = tuple(sorted((rule for rule in found if found[rule]), key=lambda r: r.value))
        reasons: tuple[str, ...] = ()
        if not tests:
            reasons = ("no test imports this module directly or through a hub symbol",)
        rows.append(
            ModuleRow(
                module=name,
                layer=_layer_of(name),
                lines=scan.lines[name],
                inbound=inbound.get(name, 0),
                outbound=outbound.get(name, 0),
                owning_tests=tuple(tests),
                owning_rules=rules,
                reasons=reasons,
            )
        )
    return tuple(rows)


def build_deferred_edges(scan: Scan) -> tuple[ImportEdge, ...]:
    edges = [
        ImportEdge(source=source, target=target, kinds=tuple(sorted(kinds, key=lambda k: k.value)))
        for (source, target), kinds in scan.edges.items()
        if EdgeKind.MODULE_LEVEL not in kinds
    ]
    return tuple(sorted(edges, key=lambda edge: (edge.source, edge.target)))


def build_cycles(scan: Scan) -> tuple[CyclicComponent, ...]:
    """A component's `closes_through` is what it needs to stay closed, not merely what it contains.

    `module_level` appears only when the members remain strongly connected using import-time edges
    alone -- which is the definition of a cycle that actually breaks an import, and the one the
    record refuses to hold.
    """

    components = _components(scan.adjacency({kind for kind in EdgeKind}))
    result: list[CyclicComponent] = []
    for members in components:
        member_set = set(members)
        kinds: set[EdgeKind] = set()
        for (source, target), carried in scan.edges.items():
            if source in member_set and target in member_set:
                if EdgeKind.MODULE_LEVEL not in carried:
                    kinds |= carried
        if _strongly_connected(member_set, scan.adjacency({EdgeKind.MODULE_LEVEL})):
            kinds.add(EdgeKind.MODULE_LEVEL)
        result.append(
            CyclicComponent(
                members=tuple(members),
                closes_through=tuple(sorted(kinds, key=lambda k: k.value)),
            )
        )
    return tuple(sorted(result, key=lambda component: component.members[0]))


def _baseline_rows(key: str) -> list[dict[str, object]]:
    """Read one list of rows out of the tracked acceptance baseline, failing closed on its shape."""

    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ArchitectureInventoryError("acceptance baseline is not an object")
    rows = document.get(key)
    if not isinstance(rows, list):
        raise ArchitectureInventoryError(f"acceptance baseline {key} is not a list")
    return [row for row in rows if isinstance(row, dict)]


def build_surfaces(scan: Scan) -> tuple[PublicSurface, ...]:
    """Three authorities claim to say what this package exposes, and they disagree."""

    core_all = set(_dunder_all(scan.trees["core.__init__"]))
    public_api = set(_dunder_all(scan.trees["public_api"]))
    abi = {str(row["export"]) for row in _baseline_rows("public_python_abi")}
    return (
        PublicSurface(
            surface_id="acceptance_abi",
            authority="tests/acceptance_baseline.json",
            declared=len(abi),
            overlaps=(
                SurfaceOverlap(other="core_all", shared=len(abi & core_all)),
                SurfaceOverlap(other="public_api", shared=len(abi & public_api)),
            ),
        ),
        PublicSurface(
            surface_id="core_all",
            authority="comfyui_h3_context/core/__init__.py",
            declared=len(core_all),
            overlaps=(
                SurfaceOverlap(other="acceptance_abi", shared=len(abi & core_all)),
                SurfaceOverlap(other="public_api", shared=len(public_api & core_all)),
            ),
        ),
        PublicSurface(
            surface_id="public_api",
            authority="comfyui_h3_context/public_api.py",
            declared=len(public_api),
            overlaps=(
                SurfaceOverlap(other="acceptance_abi", shared=len(abi & public_api)),
                SurfaceOverlap(other="core_all", shared=len(public_api & core_all)),
            ),
        ),
    )


#: Which successor owns re-recording a criterion, decided by where the criterion lives.
CRITERION_OWNER = {
    "frontend/tests/": "M19-04",
    "tests/test_frontend_build_report.py": "M19-04",
    "tests/test_generation_sequence.py": "M19-02",
    "tests/test_acceptance_baseline.py": "unowned",
}


def _criterion_owner(path: str) -> str:
    for prefix, owner in CRITERION_OWNER.items():
        if path.startswith(prefix):
            return owner
    return "unowned"


def build_bindings() -> tuple[AcceptanceBinding, ...]:
    """Every acceptance-baseline row a cohort would have to re-record, with who owns it."""

    bindings: list[AcceptanceBinding] = []
    for row in _baseline_rows("public_python_abi"):
        bindings.append(
            AcceptanceBinding(
                binding_id=str(row["export"]),
                kind="public_python_abi",
                path="comfyui_h3_context/core/__init__.py",
                owner="M19-02",
            )
        )
    for row in _baseline_rows("criteria"):
        path = str(row["path"])
        bindings.append(
            AcceptanceBinding(
                binding_id=str(row["id"]),
                kind="criterion",
                path=path,
                owner=_criterion_owner(path),
            )
        )
    return tuple(sorted(bindings, key=lambda binding: binding.binding_id))


#: The frozen successor scopes.  Reasons say why the modules belong together, not merely that they
#: are large -- V1 section 1 is explicit that line count is not a sufficient refactor criterion.
COHORTS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "frontend_contract_owners",
        "M19-04",
        ("core.product_shell", "core.production_workbench", "core.ui_projection"),
        (
            "the decomposition itself is TypeScript and outside this Python inventory; these are "
            "the contract owners whose wires it must preserve, and 21 of 23 criteria bind to it"
        ),
    ),
)


def build_cohorts() -> tuple[Cohort, ...]:
    return tuple(
        sorted(
            (
                Cohort(cohort_id=identifier, successor=successor, modules=modules, reason=reason)
                for identifier, successor, modules, reason in COHORTS
            ),
            key=lambda cohort: cohort.cohort_id,
        )
    )


def build_inventory() -> ArchitectureInventory:
    scan = Scan()
    owners = build_owners(scan)
    totals: dict[str, int] = defaultdict(int)
    for kinds in scan.edges.values():
        for kind in kinds:
            totals[kind.value] += 1
    return ArchitectureInventory(
        modules=build_modules(scan, owners),
        deferred_edges=build_deferred_edges(scan),
        cycles=build_cycles(scan),
        surfaces=build_surfaces(scan),
        bindings=build_bindings(),
        cohorts=build_cohorts(),
        forbidden_imports=tuple(sorted(set(scan.forbidden))),
        layer_inversions=tuple(sorted(set(scan.inversions))),
        edge_kind_totals=dict(totals),
    )


def artifact_bytes(inventory: ArchitectureInventory) -> bytes:
    document = json.dumps(inventory.to_wire(), ensure_ascii=False, indent=2, sort_keys=True)
    return (document + "\n").encode("utf-8")


def summary(inventory: ArchitectureInventory) -> dict[str, object]:
    return {
        "schema": inventory.schema,
        "module_count": len(inventory.modules),
        "need_evidence": [row.module for row in inventory.need_evidence],
        "deferred_edges": len(inventory.deferred_edges),
        "cycles": len(inventory.cycles),
        "runtime_cycles": len(inventory.runtime_cycles),
        "cohorts": len(inventory.cohorts),
        "bindings": len(inventory.bindings),
        "edge_kind_totals": dict(sorted(inventory.edge_kind_totals.items())),
        "fingerprint": inventory.fingerprint,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    args = parser.parse_args(argv)
    try:
        inventory = build_inventory()
    except (ArchitectureInventoryError, OSError, ValueError, SyntaxError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1
    expected = artifact_bytes(inventory)
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check and (target.read_bytes() if target.is_file() else b"") != expected:
        print(
            json.dumps(
                {"status": "FAIL", "detail": "architecture inventory artifact is stale"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"status": "PASS", **summary(inventory)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
