"""Generate the browser's closed duration domain from the Python product authorities.

The frontend must validate a backend response without carrying its own copy of the H3 frame
lattice. This generator executes the pure length module directly, while reading route identity and
product duration bounds as literal source data. It never imports the package entry point, registers
a host route, contacts a network, or reads user state.
"""

from __future__ import annotations

import argparse
import ast
import json
import runpy
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TypedDict, TypeVar, cast

ROOT = Path(__file__).resolve().parents[1]
LENGTH_AUTHORITY = ROOT / "comfyui_h3_context" / "core" / "length.py"
PROFILE_AUTHORITY = ROOT / "comfyui_h3_context" / "core" / "official_context_ir.py"
ROUTE_AUTHORITY = ROOT / "comfyui_h3_context" / "adapters" / "comfyui_duration_resolution.py"
OUTPUT = ROOT / "frontend" / "src" / "contracts" / "generatedDurationResolution.ts"

LiteralValue = TypeVar("LiteralValue", str, int)


class ContractIdentity(TypedDict):
    route: str
    request_schema: str
    response_schema: str
    minimum_seconds: int
    maximum_seconds: int


class ResolvedLength(Protocol):
    requested_milliseconds: int
    delivered_milliseconds: int
    frame_count: int
    snapped: bool


class DurationContractGenerationError(RuntimeError):
    """The Python authorities could not be reduced to one closed browser contract."""


def _literal(path: Path, name: str, expected_type: type[LiteralValue]) -> LiteralValue:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise DurationContractGenerationError(f"cannot parse {path.name}") from exc
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    ]
    if len(matches) != 1 or not isinstance(matches[0].value, ast.Constant):
        raise DurationContractGenerationError(f"expected one literal {name} in {path.name}")
    value = matches[0].value.value
    if not isinstance(value, expected_type) or isinstance(value, bool):
        raise DurationContractGenerationError(f"{name} has the wrong literal type")
    return value


def _length_resolver() -> Callable[[int], ResolvedLength]:
    try:
        # CRITICAL: load only the pure file; importing the package can register host routes.
        namespace = runpy.run_path(str(LENGTH_AUTHORITY))
        resolver = namespace["resolve_milliseconds"]
    except (OSError, KeyError, RuntimeError) as exc:
        raise DurationContractGenerationError("length authority is unavailable") from exc
    if not callable(resolver):
        raise DurationContractGenerationError("length authority has no callable resolver")
    return cast(Callable[[int], ResolvedLength], resolver)


def contract_identity() -> ContractIdentity:
    """Return the route/schema/bound identities the browser is allowed to expose."""

    return {
        "route": _literal(ROUTE_AUTHORITY, "DURATION_RESOLUTION_ROUTE", str),
        "request_schema": _literal(ROUTE_AUTHORITY, "DURATION_RESOLUTION_REQUEST_SCHEMA", str),
        "response_schema": _literal(ROUTE_AUTHORITY, "DURATION_RESOLUTION_RESPONSE_SCHEMA", str),
        "minimum_seconds": _literal(PROFILE_AUTHORITY, "MIN_OFFICIAL_DURATION_SECONDS", int),
        "maximum_seconds": _literal(PROFILE_AUTHORITY, "MAX_OFFICIAL_DURATION_SECONDS", int),
    }


def canonical_rows() -> tuple[dict[str, object], ...]:
    """Execute the pure backend resolver for every product-qualified integer duration."""

    identity = contract_identity()
    minimum = identity["minimum_seconds"]
    maximum = identity["maximum_seconds"]
    if minimum < 1 or maximum < minimum or maximum - minimum > 128:
        raise DurationContractGenerationError("product duration domain is invalid")
    resolve = _length_resolver()
    rows: list[dict[str, object]] = []
    for requested_seconds in range(minimum, maximum + 1):
        resolved = resolve(requested_seconds * 1000)
        try:
            row = {
                "schema": identity["response_schema"],
                "requested_seconds": requested_seconds,
                "requested_milliseconds": resolved.requested_milliseconds,
                "effective_milliseconds": resolved.delivered_milliseconds,
                "frame_count": resolved.frame_count,
                "snapped": resolved.snapped,
            }
        except AttributeError as exc:
            raise DurationContractGenerationError(
                "length resolver returned an invalid shape"
            ) from exc
        if (
            isinstance(row["requested_milliseconds"], bool)
            or not isinstance(row["requested_milliseconds"], int)
            or isinstance(row["effective_milliseconds"], bool)
            or not isinstance(row["effective_milliseconds"], int)
            or isinstance(row["frame_count"], bool)
            or not isinstance(row["frame_count"], int)
            or not isinstance(row["snapped"], bool)
        ):
            raise DurationContractGenerationError("length resolver returned non-wire values")
        rows.append(row)
    return tuple(rows)


def _typescript_literal(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render() -> str:
    """Render one deterministic, readonly TypeScript authority."""

    identity = contract_identity()
    lines = [
        "/**",
        " * GENERATED FILE -- DO NOT EDIT.",
        " *",
        " * Regenerate with `python scripts/duration_resolution_contract.py --write`.",
        " * Values come from the Python duration route, product profile and pure length authority.",
        " */",
        "",
        "export const durationResolutionContract = Object.freeze({",
    ]
    lines.extend(f"  {name}: {_typescript_literal(value)}," for name, value in identity.items())
    lines.extend(
        [
            "} as const);",
            "",
            "export const canonicalDurationResolutions = Object.freeze([",
        ]
    )
    for row in canonical_rows():
        lines.append("  Object.freeze({")
        lines.extend(f"    {name}: {_typescript_literal(value)}," for name, value in row.items())
        lines.append("  }),")
    lines.extend(
        [
            "] as const);",
            "",
            "export type CanonicalDurationResolution =",
            "  (typeof canonicalDurationResolutions)[number];",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
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
            print("generated duration resolution contract is stale", file=sys.stderr)
            return 1
        print("generated duration resolution contract is current")
        return 0
    except (DurationContractGenerationError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
