"""Generate the M19-02 public surface record: who promises what, and what the hub silently picks.

Static `ast` throughout for the surfaces themselves.  Deciding whether two definitions of one name
agree needs their *values*, which cannot be read statically, so that one comparison imports the two
defining modules directly -- never the hub -- and compares the objects.  Importing a leaf module is
not the same as executing the package: it is the narrowest thing that can answer the question.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.governance.public_surface import (  # noqa: E402
    PublicSurface,
    PublicSurfaceError,
    ShadowDisposition,
    ShadowedExport,
    SurfaceAuthority,
    SurfaceLevel,
)

ARTIFACT_PATH = Path("governance/contracts/public_surface_v1.json")

PACKAGE = "comfyui_h3_context"
PKG_ROOT = ROOT / PACKAGE
CORE_INIT = PKG_ROOT / "core" / "__init__.py"
PUBLIC_API = PKG_ROOT / "public_api.py"
BASELINE = ROOT / "tests" / "acceptance_baseline.json"

#: Roots whose imports count as evidence that a hub export has a reader inside this repository.
CONSUMER_ROOTS = ("tests", "scripts", "examples")

NOTES = (
    "The package authority is the consumer promise. Pure values it exposes stay in the pure-core "
    "authority; names that need node metadata remain package facades.",
    "hub_unverified counts exports no file in this repository imports. It is not a deletion "
    "list: an installed ComfyUI user importing from comfyui_h3_context.core is invisible here.",
)


class ShadowDispositionRecord(NamedTuple):
    """The exact intended resolution for one live divergent hub shadow."""

    bound_origin: str
    shadowed_origin: str
    disposition: ShadowDisposition
    resolved_alias: str | None
    rationale: str


SHADOW_DISPOSITIONS = {
    "OracleComparisonStatus": ShadowDispositionRecord(
        bound_origin="core.base_evaluation",
        shadowed_origin="core.task_mode_retention_classifier",
        disposition=ShadowDisposition.ALIAS,
        resolved_alias="TaskModeOracleComparisonStatus",
        rationale=(
            "Preserve the base-evaluation binding and expose the distinct task-mode status under "
            "an explicit role-qualified alias."
        ),
    ),
    "TemporalInterval": ShadowDispositionRecord(
        bound_origin="core.temporal_visual_analysis",
        shadowed_origin="core.temporal_event_alignment",
        disposition=ShadowDisposition.ALIAS,
        resolved_alias="TemporalGroundingInterval",
        rationale=(
            "Preserve the visual-analysis binding and retain the event-alignment interval under "
            "its existing role-qualified alias."
        ),
    ),
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


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


def _hub_bindings() -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Bound name -> (origin module, original name), plus the alias map."""

    bound: dict[str, tuple[str, str]] = {}
    aliases: dict[str, str] = {}
    tree = _tree(CORE_INIT)
    nodes = list(tree.body)
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
            if node.test.id == "TYPE_CHECKING":
                nodes.extend(node.body)
    for node in nodes:
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
            continue
        for alias in node.names:
            name = alias.asname or alias.name
            bound[name] = (f"core.{node.module}", alias.name)
            if alias.asname:
                aliases[alias.asname] = alias.name
    return bound, aliases


def _definitions() -> dict[str, set[str]]:
    """Every public name defined at module scope, mapped to the modules that define it."""

    owner: dict[str, set[str]] = defaultdict(set)
    for path in sorted(PKG_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "__init__.py":
            continue
        module = path.relative_to(PKG_ROOT).with_suffix("").as_posix().replace("/", ".")
        for node in _tree(path).body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if not node.name.startswith("_"):
                    owner[node.name].add(module)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        if not target.id.startswith("_"):
                            owner[target.id].add(module)
    return owner


def _reexports() -> dict[tuple[str, str], str]:
    """(module, name) -> the sibling module that module imports the name from, unaliased.

    An aggregation surface re-exports names it does not define. Without this map the collision test
    below sees the surface, finds no definition in it, and concludes there is nothing to compare --
    which would silently retire a real shadow the moment its owner gained an aggregation surface.
    """

    found: dict[tuple[str, str], str] = {}
    for path in sorted(PKG_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "__init__.py":
            continue
        module = path.relative_to(PKG_ROOT).with_suffix("").as_posix().replace("/", ".")
        package = module.rsplit(".", 1)[0] if "." in module else ""
        for node in _tree(path).body:
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                continue
            target = f"{package}.{node.module}" if package else node.module
            for alias in node.names:
                if alias.asname is None:
                    found[(module, alias.name)] = target
    return found


def _defining_origin(
    origin: str, name: str, owner: dict[str, set[str]], reexports: dict[tuple[str, str], str]
) -> str:
    """Follow a re-export chain from `origin` to the module that actually defines `name`."""

    seen: set[str] = set()
    while origin not in owner.get(name, set()):
        if origin in seen:
            return origin
        seen.add(origin)
        following = reexports.get((origin, name))
        if following is None:
            return origin
        origin = following
    return origin


def _values_agree(name: str, first: str, second: str) -> bool:
    """Do the two definitions of one name mean the same thing?

    Imports the two leaf modules directly and compares. A static scan cannot answer this, and the
    answer is what separates an untidy duplicate from a bound an importer silently gets wrong.
    """

    try:
        left = getattr(importlib.import_module(f"{PACKAGE}.{first}"), name)
        right = getattr(importlib.import_module(f"{PACKAGE}.{second}"), name)
    except (ImportError, AttributeError) as exc:  # pragma: no cover - a broken tree fails elsewhere
        raise PublicSurfaceError(f"cannot compare {name} across {first} and {second}") from exc
    if left is right:
        return True
    if isinstance(left, type) and isinstance(right, type):
        # Two enums are the same promise when they carry the same members. Two other distinct
        # classes are not: comparing them by an attribute neither has would report agreement for
        # every pair of unrelated dataclasses, which is worse than reporting none.
        members, other_members = (
            getattr(left, "__members__", None),
            getattr(right, "__members__", None),
        )
        if members is None or other_members is None:
            return False
        return sorted(members) == sorted(other_members)
    try:
        return bool(left == right)
    except Exception:  # pragma: no cover - an exotic __eq__ is treated as disagreement
        return False


def _consumer_names() -> set[str]:
    """Names imported from the pure-core hub anywhere under tests/ or scripts/."""

    hubs = {f"{PACKAGE}.core", PACKAGE, f"{PACKAGE}.public_api"}
    found: set[str] = set()
    for root in CONSUMER_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = _tree(path)
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in hubs:
                    found.update(alias.name for alias in node.names)
    return found


def build_surface() -> PublicSurface:
    core_all = _dunder_all(_tree(CORE_INIT))
    duplicated = sorted({name for name in core_all if core_all.count(name) > 1})
    if duplicated:
        raise PublicSurfaceError(f"core.__all__ lists {duplicated[0]} more than once")

    public_api = _dunder_all(_tree(PUBLIC_API))
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    abi = sorted({str(row["export"]) for row in baseline["public_python_abi"]})

    bound, aliases = _hub_bindings()
    owner = _definitions()
    reexported = _reexports()

    shadowed: list[ShadowedExport] = []
    for name in sorted(set(core_all)):
        binding = bound.get(name)
        if binding is None:
            continue
        origin, original = binding
        if name != original:
            continue  # an aliased export is a resolution, not a collision
        defining = owner.get(original, set())
        # Only a genuine collision counts: two definitions competing for one name, not one
        # definition reached through two paths. The binding module may be an aggregation surface
        # that re-exports rather than defines, so resolve through the re-export chain first --
        # otherwise splitting a module behind a surface would retire the shadow it still has.
        origin = _defining_origin(origin, original, owner, reexported)
        if origin not in defining or len(defining) < 2:
            continue
        other = sorted(defining - {origin})[0]
        # Is the shadowed definition reachable under an alias the hub also exports?
        alias = next(
            (
                exported
                for exported, source in aliases.items()
                if source == original and bound.get(exported, ("", ""))[0] == other
            ),
            None,
        )
        decision = SHADOW_DISPOSITIONS.get(name)
        if decision is None:
            raise PublicSurfaceError(f"live shadow has no disposition: {name}")
        if (decision.bound_origin, decision.shadowed_origin) != (origin, other):
            raise PublicSurfaceError(f"shadow disposition origins drifted: {name}")
        if alias != decision.resolved_alias:
            raise PublicSurfaceError(f"shadow disposition alias does not resolve: {name}")
        shadowed.append(
            ShadowedExport(
                name=name,
                bound_origin=origin,
                shadowed_origin=other,
                values_agree=_values_agree(original, origin, other),
                resolved_alias=decision.resolved_alias,
                disposition=decision.disposition,
                rationale=decision.rationale,
            )
        )

    live_shadows = {item.name for item in shadowed}
    stale_dispositions = sorted(set(SHADOW_DISPOSITIONS) - live_shadows)
    if stale_dispositions:
        raise PublicSurfaceError(f"shadow disposition is stale: {stale_dispositions[0]}")

    consumed = _consumer_names() & set(core_all)
    return PublicSurface(
        authorities=(
            SurfaceAuthority(
                authority_id="acceptance_abi",
                path="tests/acceptance_baseline.json",
                level=SurfaceLevel.GATE_ENFORCED,
                names=tuple(abi),
            ),
            SurfaceAuthority(
                authority_id="package",
                path="comfyui_h3_context/public_api.py",
                level=SurfaceLevel.PACKAGE,
                names=tuple(sorted(set(public_api))),
            ),
            SurfaceAuthority(
                authority_id="pure_core",
                path="comfyui_h3_context/core/__init__.py",
                level=SurfaceLevel.PURE_CORE,
                names=tuple(sorted(set(core_all))),
            ),
        ),
        shadowed=tuple(shadowed),
        hub_consumed=len(consumed),
        hub_unverified=len(set(core_all) - consumed),
        aliased_exports=dict(sorted(aliases.items())),
        notes=NOTES,
    )


def artifact_bytes(surface: PublicSurface) -> bytes:
    document = json.dumps(surface.to_wire(), ensure_ascii=False, indent=2, sort_keys=True)
    return (document + "\n").encode("utf-8")


def summary(surface: PublicSurface) -> dict[str, object]:
    return {
        "schema": surface.schema,
        "authorities": {item.authority_id: item.declared for item in surface.authorities},
        "hub_consumed": surface.hub_consumed,
        "hub_unverified": surface.hub_unverified,
        "shadowed": len(surface.shadowed),
        "shadowed_unresolved": len(surface.unresolved_shadowed),
        "shadowed_divergent": [item.name for item in surface.divergent_shadowed],
        "fingerprint": surface.fingerprint,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    args = parser.parse_args(argv)
    try:
        surface = build_surface()
    except (PublicSurfaceError, OSError, ValueError, SyntaxError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1
    expected = artifact_bytes(surface)
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check and (target.read_bytes() if target.is_file() else b"") != expected:
        print(
            json.dumps(
                {"status": "FAIL", "detail": "public surface artifact is stale"}, ensure_ascii=False
            )
        )
        return 1
    print(json.dumps({"status": "PASS", **summary(surface)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
