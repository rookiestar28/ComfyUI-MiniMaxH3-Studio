"""Validate the shipped package compatibility policy and deprecation chronology.

JSON Schema owns the closed structural shape.  This validator owns the ordering comparisons that
JSON Schema cannot express: successor releases follow the announcement, removals follow every
successor, and removal occurs at a later ``X.0.0`` MAJOR boundary.  It is release tooling only and
never authorizes publication or runtime execution.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = Path("governance/contracts/package_compatibility_policy_v1.json")
SCHEMA_PATH = Path("governance/contracts/package_compatibility_policy_v1.schema.json")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class PackageCompatibilityPolicyError(RuntimeError):
    """Raised when the policy shape or release chronology is not fail-closed."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackageCompatibilityPolicyError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=_object_without_duplicate_keys)
    except PackageCompatibilityPolicyError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise PackageCompatibilityPolicyError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageCompatibilityPolicyError(f"{label} must be an object")
    return value


def _version(value: object, field: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise PackageCompatibilityPolicyError(f"{field} must be a three-part SemVer string")
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise PackageCompatibilityPolicyError(f"{field} must be a strict three-part SemVer")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _validate_shape(document: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise PackageCompatibilityPolicyError(f"policy schema is invalid: {exc.message}") from exc
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document), key=lambda item: item.json_path
    )
    if errors:
        first = errors[0]
        raise PackageCompatibilityPolicyError(
            f"policy schema validation failed at {first.json_path}: {first.message}"
        )


def validate_policy(
    document: Mapping[str, Any], *, schema: Mapping[str, Any] | None = None
) -> None:
    """Validate structure, unique notice identity, and monotonic removal chronology."""

    effective_schema = schema if schema is not None else _read_json(ROOT / SCHEMA_PATH, "schema")
    _validate_shape(document, effective_schema)

    policy = cast(Mapping[str, Any], document["deprecation_policy"])
    entries = cast(Sequence[Mapping[str, Any]], policy["deprecations"])
    seen_notices: set[str] = set()
    for index, entry in enumerate(entries):
        notice_code = cast(str, entry["notice_code"])
        if notice_code in seen_notices:
            raise PackageCompatibilityPolicyError(f"duplicate deprecation notice {notice_code!r}")
        seen_notices.add(notice_code)

        announced = _version(entry["announced_in"], f"deprecations[{index}].announced_in")
        successors = cast(Sequence[str], entry["successor_releases"])
        previous = announced
        for successor_index, raw_successor in enumerate(successors):
            successor = _version(
                raw_successor,
                f"deprecations[{index}].successor_releases[{successor_index}]",
            )
            if successor <= previous:
                raise PackageCompatibilityPolicyError(
                    f"deprecation {notice_code!r} successor releases must be strictly "
                    "chronological and later than the announcement"
                )
            previous = successor

        if entry["state"] != "removed":
            continue
        if len(successors) < 2:
            raise PackageCompatibilityPolicyError(
                f"removed deprecation {notice_code!r} requires two successor releases"
            )
        removal = _version(entry["removal_version"], f"deprecations[{index}].removal_version")
        if removal <= previous:
            raise PackageCompatibilityPolicyError(
                f"deprecation {notice_code!r} removal must follow every successor release"
            )
        if removal[0] <= announced[0] or removal[1:] != (0, 0):
            raise PackageCompatibilityPolicyError(
                f"deprecation {notice_code!r} removal must be a later X.0.0 MAJOR release"
            )


def check_shipped_policy() -> dict[str, object]:
    """Read the shipped files strictly and return a bounded, content-free PASS projection."""

    document = _read_json(ROOT / ARTIFACT_PATH, "policy")
    schema = _read_json(ROOT / SCHEMA_PATH, "schema")
    validate_policy(document, schema=schema)
    policy = cast(Mapping[str, Any], document["deprecation_policy"])
    entries = cast(Sequence[Mapping[str, Any]], policy["deprecations"])
    return {
        "status": "PASS",
        "schema": cast(str, document["schema"]),
        "deprecation_count": len(entries),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate the shipped policy")
    args = parser.parse_args()
    if not args.check:
        parser.error("--check is required")
    try:
        result = check_shipped_policy()
    except PackageCompatibilityPolicyError as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
