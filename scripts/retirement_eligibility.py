"""Generate the M18-05 retirement-eligibility record from the accepted contract inventory.

This exists because "we looked and found nothing to retire" is not a claim anybody can check.  The
record turns it into one: every inventoried JSON Schema gets a verdict and, when it is not eligible,
the reason -- computed from the inventory rather than chosen -- and every schema that has actually
been retired is named with the commit that still has it.

The generator builds the inventory the same way `scripts/contract_inventory.py` does, so the two
records cannot describe different repositories.  It reads files as data and executes nothing.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.contract_inventory import SELF_DESCRIBING_PATHS, build_inventory  # noqa: E402
from scripts.governance.retirement_eligibility import (  # noqa: E402
    RetirementClaim,
    RetirementEligibility,
    RetirementEligibilityError,
    build_retirement_eligibility,
)

ARTIFACT_PATH = Path("governance/contracts/retirement_eligibility_v1.json")

#: File kinds that can actually *read* a schema.  A generated `.json` record is not one.
SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx"})

#: The commit whose tree still contains every file retired below.  It is the accepted `M18-04`
#: checkpoint -- the last state in which all four schemas shipped -- so `git show <commit>:<path>`
#: restores any of them exactly.  This is the whole mechanism behind AC-M18-05-04: a stable `$id`
#: may stop shipping, but it may not stop shipping without somewhere to get it back from.
#:
#: A 40-character object name reads as high-entropy hex to the secret scanner.  It is the opposite
#: of a secret: it is a public commit in this repository's own history, and the record is useless
#: without it.
RETIREMENT_ROLLBACK_COMMIT = "aea047c471e3cc87654fc67e64c6b9db6f216cfd"  # pragma: allowlist secret

#: Where a retired identity may still legitimately appear: this record, which is the deprecation
#: notice itself, and the generator that writes it.  Everywhere else is a dangling reference.
REFERENCE_EXEMPT = frozenset(
    {
        "governance/contracts/retirement_eligibility_v1.json",
        "scripts/retirement_eligibility.py",
    }
)

#: Where a surviving reference could hide.  Wider than the contract inventory's own `SCAN_ROOTS`,
#: deliberately: `docs/` and `examples/` ship (see `MANIFEST.in`) and AC-M18-05-05 names docs
#: explicitly, so a retirement must be checked against them even though they hold no contract.
REFERENCE_ROOTS = (
    "comfyui_h3_context",
    "docs",
    "examples",
    "frontend/src",
    "frontend/tests",
    "scripts",
    "subgraphs",
    "tests",
    "workflows",
)

#: Top-level shipped files that belong to no root above.
REFERENCE_FILES = ("MANIFEST.in", "NOTICE", "README.md", "pyproject.toml")

#: Directories never worth reading: third-party trees and build output.
REFERENCE_SKIP = frozenset({".git", ".tmp", "__pycache__", "build", "dist", "node_modules"})

#: The frozen removal set (V2 section 4).  A claim states only **which** file went and how to get it
#: back; every reason it was allowed to go is recomputed by `retire_schema` from the repository as
#: it stands now -- the wires the schema restated, their boundary and persistence, and whether
#: anything still names the identity.  A claim that has become wrong raises rather than being
#: recorded, so this tuple cannot quietly outlive its evidence.
RETIRED: tuple[RetirementClaim, ...] = (
    RetirementClaim(
        contract_id="comfyui-h3-context://contracts/base_assistant_v1.schema.json",
        schema_path="comfyui_h3_context/contracts/base_assistant_v1.schema.json",
        rollback_commit=RETIREMENT_ROLLBACK_COMMIT,
        surviving_authority="comfyui_h3_context/core/base_assistant.py",
        item="M18-05",
    ),
    RetirementClaim(
        contract_id="comfyui-h3-context://contracts/ui_projection_v1.schema.json",
        schema_path="comfyui_h3_context/contracts/ui_projection_v1.schema.json",
        rollback_commit=RETIREMENT_ROLLBACK_COMMIT,
        surviving_authority="comfyui_h3_context/core/ui_projection.py",
        item="M18-05",
    ),
    RetirementClaim(
        contract_id="comfyui-h3-context://contracts/validation_lifecycle_v1.schema.json",
        schema_path="comfyui_h3_context/contracts/validation_lifecycle_v1.schema.json",
        rollback_commit=RETIREMENT_ROLLBACK_COMMIT,
        surviving_authority="comfyui_h3_context/core/validation_lifecycle.py",
        item="M18-05",
    ),
    RetirementClaim(
        contract_id=(
            "https://comfyui-h3-context.invalid/schemas/execution_coordinator_v1.schema.json"
        ),
        schema_path="comfyui_h3_context/contracts/execution_coordinator_v1.schema.json",
        rollback_commit=RETIREMENT_ROLLBACK_COMMIT,
        surviving_authority="comfyui_h3_context/core/execution_coordinator.py",
        item="M18-05",
    ),
)


def hidden_consumers(inventory: object) -> dict[str, tuple[str, ...]]:
    """Readers living in the files M18-01 excludes from its own scan.

    M18-01 keeps `SELF_DESCRIBING_PATHS` out of the scan so the inventory can never supply its own
    evidence -- a good rule that has one consequence here: a schema those files read shows zero
    consumers, and zero consumers is exactly what this record treats as safe to delete.  The first
    generated draft reported `contract_inventory_v1.schema.json` as `eligible` for that reason,
    while `tests/test_contract_inventory.py` reads it on line 44.

    So the excluded files are read here, separately and explicitly, using the same three discovery
    rules the inventory uses: the `$id`, the file name and the file stem.

    Only *source* files count.  Two of the excluded paths are the generated inventory and domain
    records, which name every contract by construction; treating those as readers made every
    schema look consumed and was the second wrong answer this function gave.  A record that
    names a contract is not a reader of it.
    """

    found: dict[str, set[str]] = {}
    for relative in sorted(SELF_DESCRIBING_PATHS):
        path = ROOT / relative
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for entry in inventory.entries:  # type: ignore[attr-defined]
            if entry.kind.value != "json_schema":
                continue
            needles = {entry.contract_id}
            for authority in entry.authority_paths:
                name = authority.rsplit("/", 1)[-1]
                needles.add(name)
                needles.add(name.removesuffix(".schema.json"))
            if any(needle in text for needle in needles):
                found.setdefault(entry.contract_id, set()).add(relative)
    return {key: tuple(sorted(value)) for key, value in found.items()}


def _reference_candidates() -> list[Path]:
    """Every file a surviving reference to a retired identity could be written in."""

    found: list[Path] = []
    for name in REFERENCE_FILES:
        path = ROOT / name
        if path.is_file():
            found.append(path)
    for root in REFERENCE_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or REFERENCE_SKIP & set(path.parts):
                continue
            if path.suffix.lower() in {".py", ".ts", ".tsx", ".json", ".md", ".toml", ".in"}:
                found.append(path)
    return found


def surviving_references(claims: tuple[RetirementClaim, ...]) -> dict[str, tuple[str, ...]]:
    """Files that still name a retired identity, under the three rules the inventory uses.

    This is the zero-consumer half of the eligibility rule, and it is the half that survives
    deletion intact: whether anything still names an identity is a property of the rest of the
    repository, so it can be re-derived in full every time this record is generated rather than
    trusted from the day the decision was made.  A non-empty result stops the build.
    """

    needles: dict[str, set[str]] = {}
    for claim in claims:
        name = claim.schema_path.rsplit("/", 1)[-1]
        needles[claim.contract_id] = {
            claim.contract_id,
            name,
            name.removesuffix(".schema.json"),
        }
    found: dict[str, set[str]] = {}
    for path in _reference_candidates():
        relative = path.relative_to(ROOT).as_posix()
        if relative in REFERENCE_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for contract_id, group in needles.items():
            if any(needle in text for needle in group):
                found.setdefault(contract_id, set()).add(relative)
    return {key: tuple(sorted(value)) for key, value in found.items()}


def reexported_by() -> dict[str, tuple[str, ...]]:
    """Defining module -> the aggregation surfaces that re-export from it.

    `described_wires` pairs a schema with a wire by matching the schema's stem against the wire's
    authority module, which is how this repository names them.  M19-05 split three large modules
    into layers behind a surface, so a wire constant that used to live in `X.py` now lives in
    `X_primitives.py` while `X_v1.schema.json` is still named after `X`.  The convention did not
    stop holding; it just has to be read through the surface.

    A surface is recognised structurally, not by name: a module that imports a name from a sibling
    and lists that same name in its own `__all__` is re-exporting it.  A module that imports a name
    to *use* it does not qualify, which is what keeps this from matching every import in the
    package.

    Known limits of that structural test, from M19-06's distinct review.  It over-matches when a
    module imports a name, rebinds it locally, and still exports it -- ruff's `F811` refuses that
    shape repository-wide, which is why it cannot arise here.  It under-matches an absolute import,
    a renamed import (`as`), and `from . import module`; all three fail closed, leaving a wire
    undescribed rather than pairing it with the wrong schema.  Widen the AST match before relying
    on this for a surface built any of those three ways.
    """

    surfaces: dict[str, set[str]] = {}
    for path in sorted((ROOT / "comfyui_h3_context").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken tree fails the gate elsewhere
            continue
        exported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            ):
                exported = {
                    element.value
                    for element in getattr(node.value, "elts", [])
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                }
        if not exported:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                continue
            if any(alias.asname is None and alias.name in exported for alias in node.names):
                surfaces.setdefault(node.module, set()).add(path.stem)
    return {key: tuple(sorted(value)) for key, value in sorted(surfaces.items())}


def build_eligibility() -> RetirementEligibility:
    """Assess every schema the accepted inventory knows about, and attach the retired set."""

    inventory = build_inventory()
    return build_retirement_eligibility(
        inventory,
        claims=RETIRED,
        hidden_consumers=hidden_consumers(inventory),
        surviving_references=surviving_references(RETIRED),
        reexported_by=reexported_by(),
    )


def artifact_bytes(record: RetirementEligibility) -> bytes:
    document = json.dumps(record.to_wire(), ensure_ascii=False, indent=2, sort_keys=True)
    return (document + "\n").encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    args = parser.parse_args(argv)
    try:
        record = build_eligibility()
    except (RetirementEligibilityError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1
    expected = artifact_bytes(record)
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check and (target.read_bytes() if target.is_file() else b"") != expected:
        print(
            json.dumps(
                {"status": "FAIL", "detail": "retirement eligibility artifact is stale"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"status": "PASS", **record.to_public_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
