"""Generate frontend identity inventories from the two Python enum authorities.

The generator parses source as data. It does not import product modules, execute providers, read
user state, or contact a network. The generated module contains stable identifiers only; localized
copy remains owned by the frontend catalog.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "frontend" / "src" / "i18n" / "generatedBackendIdentities.ts"
_IDENTITY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\Z")


class IdentityRegistryGenerationError(RuntimeError):
    """Raised when a declared enum cannot be extracted exactly."""


@dataclass(frozen=True, slots=True)
class EnumAuthority:
    module_path: str
    class_name: str
    export_name: str


AUTHORITIES = (
    EnumAuthority(
        "comfyui_h3_context/core/prompt_fidelity.py",
        "PromptFidelityDiagnosticId",
        "promptFidelityDiagnosticIds",
    ),
    EnumAuthority(
        "comfyui_h3_context/core/prompt_model_provider.py",
        "PromptModelOutcomeId",
        "promptModelOutcomeIds",
    ),
)


def extract_enum(path: Path, class_name: str) -> tuple[str, ...]:
    """Return exact string values in declaration order, refusing computed members."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise IdentityRegistryGenerationError(f"cannot parse {path.name}") from exc
    matching = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(matching) != 1:
        raise IdentityRegistryGenerationError(f"expected one enum {class_name}")
    enum_node = matching[0]
    bases = {base.id for base in enum_node.bases if isinstance(base, ast.Name)}
    if "Enum" not in bases:
        raise IdentityRegistryGenerationError(f"{class_name} is not a direct Enum")
    values: list[str] = []
    for member in enum_node.body:
        if not isinstance(member, ast.Assign):
            continue
        if len(member.targets) != 1 or not isinstance(member.targets[0], ast.Name):
            raise IdentityRegistryGenerationError(f"{class_name} has a computed member target")
        name = member.targets[0].id
        if name.startswith("_"):
            continue
        if not isinstance(member.value, ast.Constant) or not isinstance(member.value.value, str):
            raise IdentityRegistryGenerationError(f"{class_name}.{name} is not a literal string")
        value = member.value.value
        if _IDENTITY.fullmatch(value) is None:
            raise IdentityRegistryGenerationError(f"{class_name}.{name} is not a stable identity")
        values.append(value)
    if not values or len(values) != len(set(values)):
        raise IdentityRegistryGenerationError(f"{class_name} is empty or has duplicate values")
    return tuple(values)


def render() -> str:
    """Render the deterministic TypeScript module."""

    lines = [
        "/**",
        " * GENERATED FILE -- DO NOT EDIT.",
        " *",
        " * Regenerate with `python scripts/localized_identity_registry.py --write`.",
        " * Values come from the Python enum authorities and contain identities only.",
        " */",
        "",
    ]
    for authority in AUTHORITIES:
        values = extract_enum(ROOT / authority.module_path, authority.class_name)
        lines.append(f"/** {authority.module_path} :: {authority.class_name} */")
        lines.append(f"export const {authority.export_name} = Object.freeze([")
        lines.extend(f'  "{value}",' for value in values)
        lines.append("] as const);")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = render()
        if args.write:
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_text(expected, encoding="utf-8", newline="\n")
            print(f"wrote {OUTPUT.relative_to(ROOT).as_posix()}")
            return 0
        actual = OUTPUT.read_text(encoding="utf-8")
        if actual != expected:
            print("generated backend identity inventory is stale", file=sys.stderr)
            return 1
        print("generated backend identity inventory is current")
        return 0
    except (IdentityRegistryGenerationError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
