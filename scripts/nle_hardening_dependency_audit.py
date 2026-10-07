"""M25-21: audit the candidate's dependency inventory against the accepted checkpoint.

The hardening report accepts a dependency audit only when every entry it lists is ``unchanged``.
This script produces that audit from what already exists rather than from a second inventory: the
frontend package inventory the supply-chain contract already pins
(``frontend/license-inventory.json``) and the Python distributions ``pyproject.toml`` declares.
Each entry is compared, by name, with the same source read out of the accepted checkpoint through
read-only Git, so an added, removed or version-changed dependency is reported as such instead of
disappearing into a regenerated file.

The audit asserts nothing about what the tree *should* contain: it reports what changed. The plan's
acceptance criterion is that M25-21 changes no dependency, so a non-``unchanged`` disposition is a
finding for the record, never something this script may smooth over.

Run with ``--baseline <git ref>`` (the accepted checkpoint) and ``--out <path>``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import tomllib

ROOT: Final = Path(__file__).resolve().parents[1]
AUDIT_SCHEMA: Final = "h3.context.nle_hardening_dependency_audit.v1"
INVENTORY: Final = "frontend/license-inventory.json"
PYPROJECT: Final = "pyproject.toml"


class AuditError(RuntimeError):
    """A baseline that cannot be read is a blocked audit, never an empty one."""


def _at(ref: str, path: str) -> bytes:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "show", f"{ref}:{path}"],  # noqa: S607 - git resolved from PATH by design
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:  # pragma: no cover - environment
        raise AuditError(f"cannot read {path} at {ref}") from error


def _frontend_entries(raw: bytes) -> dict[str, dict[str, str]]:
    packages = json.loads(raw.decode("utf-8"))
    if not isinstance(packages, list) or not packages:
        raise AuditError("the frontend license inventory is empty")
    entries: dict[str, dict[str, str]] = {}
    for package in packages:
        name = str(package["name"])
        entries[name] = {
            "name": name,
            "version": str(package["version"]),
            "license": str(package["license"]),
            "ecosystem": "npm",
        }
    return entries


def _python_entries(raw: bytes) -> dict[str, dict[str, str]]:
    project = tomllib.loads(raw.decode("utf-8"))["project"]
    declared: list[str] = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        declared.extend(group)
    entries: dict[str, dict[str, str]] = {}
    for requirement in declared:
        # The declaration is the audited fact: a requirement string is recorded whole, so a
        # widened or narrowed bound is a version change rather than an invisible edit.
        name = requirement.split(";")[0].strip()
        for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
            if separator in name:
                name = name.split(separator)[0]
                break
        name = name.strip()
        entries[name] = {
            "name": name,
            "version": requirement.strip(),
            "license": "declared_by_distribution",
            "ecosystem": "python",
        }
    return entries


def _compare(
    current: dict[str, dict[str, str]], baseline: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name in sorted(set(current) | set(baseline)):
        now = current.get(name)
        before = baseline.get(name)
        if now is None and before is not None:
            entries.append(
                {**before, "disposition": "removed", "baseline_version": before["version"]}
            )
            continue
        assert now is not None  # noqa: S101 - one of the two is always present
        if before is None:
            entries.append({**now, "disposition": "added", "baseline_version": ""})
        elif now["version"] != before["version"]:
            entries.append(
                {**now, "disposition": "version_changed", "baseline_version": before["version"]}
            )
        elif now["license"] != before["license"]:
            entries.append(
                {**now, "disposition": "license_changed", "baseline_version": before["version"]}
            )
        else:
            entries.append(
                {**now, "disposition": "unchanged", "baseline_version": before["version"]}
            )
    return entries


def audit(baseline_ref: str) -> dict[str, Any]:
    current = _frontend_entries((ROOT / INVENTORY).read_bytes())
    current.update(_python_entries((ROOT / PYPROJECT).read_bytes()))
    baseline = _frontend_entries(_at(baseline_ref, INVENTORY))
    baseline.update(_python_entries(_at(baseline_ref, PYPROJECT)))
    entries = _compare(current, baseline)
    changed = [entry["name"] for entry in entries if entry["disposition"] != "unchanged"]
    return {
        "schema": AUDIT_SCHEMA,
        "baseline_ref": baseline_ref,
        "sources": [INVENTORY, PYPROJECT],
        "entry_count": len(entries),
        "changed": changed,
        "status": "PASS" if not changed else "FAIL",
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="accepted checkpoint Git ref")
    parser.add_argument("--out", type=Path, required=True, help="where to write the audit JSON")
    args = parser.parse_args(argv)
    try:
        document = audit(args.baseline)
    except AuditError as error:
        print(f"dependency audit blocked: {error}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"{document['entry_count']} dependencies audited against {args.baseline}: "
        f"{document['status']}"
        + (f" ({', '.join(document['changed'])})" if document["changed"] else "")
    )
    return 0 if document["status"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover - console entry
    raise SystemExit(main())
