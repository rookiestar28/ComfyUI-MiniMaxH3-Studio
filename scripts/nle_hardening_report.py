"""M25-21: join gathered hardening observations to the frozen coverage manifest.

Input is the ``h3.context.nle_hardening_observations.v1`` document the browser gatherer writes
(``frontend/tests/e2e/helpers/nleHardeningGatherer.mjs``), optionally merged with backend
observation documents of the same schema. Output is ``nle_hardening_report.v1`` with exactly the
sections of plan section 14.3.

A dimension of a row passes only when all of these hold:

- the observation ``nle_hardening.<row>.<dimension>`` exists;
- it was recorded by exactly the collected test the manifest names for that dimension;
- that test passed;
- the observation itself carries a ``PASS`` verdict with at least one identified assertion.

Anything else is ``FAIL`` (a recorded failure, a failing recording test, or an observation from
the wrong test) or ``NOT_RUN`` (nothing recorded). A missing measurement is ``NOT_RUN``, never zero,
and ``N/A`` is reserved for the closed list of unselected capabilities below; it cannot hide an
advertised feature. The disposition is ``PASS`` only when every row, subcase, seam, measurement,
workload and the dependency audit pass.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import nle_hardening_manifest as manifest_generator  # noqa: E402

REPORT_SCHEMA: Final = "nle_hardening_report.v1"
OBSERVATIONS_SCHEMA: Final = "h3.context.nle_hardening_observations.v1"
AUDIT_SCHEMA: Final = "h3.context.nle_hardening_dependency_audit.v1"
REPORT_SECTIONS: Final = (
    "identity",
    "workloads",
    "measurements",
    "command_rows",
    "ui_rows",
    "import_rows",
    "recovery_rows",
    "dependency_audit",
    "disposition",
)
STATUSES: Final = ("PASS", "FAIL", "NOT_RUN", "N/A")
PREFIX: Final = "nle_hardening."

# Plan section 14.3 caps.
MAX_ROW_SUMMARY_BYTES: Final = 64 * 1024
MAX_REPORT_BYTES: Final = 16 * 1024 * 1024
MAX_RETAINED_ARTIFACT_BYTES: Final = 256 * 1024 * 1024
MAX_OBSERVATIONS: Final = 4096

# The only measurements that may be N/A, each with the concrete unselected capability it names.
# Empty: every frozen measurement of this item is measurable on the selected native-media profile
# (Worker and WebGL are measured as zero allocations, not declared N/A).
NOT_APPLICABLE: Final[Mapping[str, str]] = {}

# Loopback URLs are the hermetic server; anything else, a drive or UNC path, or a named private
# payload field must never enter a report. Matched against serialized JSON, where a literal
# backslash is doubled (see frontend/tests/e2e/helpers/evidence.ts for the same rule).
_FORBIDDEN: Final = re.compile(
    r"(?:https?://(?!127\.0\.0\.1)|[A-Z]:\\\\|\\\\\\\\[A-Z0-9]|credential|cookie|prompt_text"
    r"|media_bytes|signed_url)",
    re.IGNORECASE,
)

# The dependency audit names real packages, and a package identifier is not a payload: npm's own
# cookie jar is called "tough-cookie". Each admitted identifier is pinned here by its exact name.
_DEPENDENCY_NAME_EXCEPTIONS: Final = frozenset({"tough-cookie"})

IDENTITY_FIELDS: Final = (
    "candidate_head",
    "candidate_tree_fingerprint",
    "manifest_sha256",
    "chromium_version",
    "playwright_version",
    "node_version",
    "os_build",
)


class ReportError(ValueError):
    """The observation input cannot be joined (malformed, oversized or privacy-unsafe)."""


def privacy_unsafe(text: str) -> bool:
    return _FORBIDDEN.search(text) is not None


def _scannable(report: Mapping[str, Any]) -> str:
    """The serialized report with pinned dependency identifiers elided before the privacy scan.

    IMPORTANT (M25-21 B3-D50): elide only the exact pinned identifier, and only where the audit
    itself recorded it as an entry name. Dropping ``cookie`` from ``_FORBIDDEN`` instead would let
    a real cookie payload through anywhere in the report, and exempting every ``name`` field would
    let a package chosen by an upstream carry one.
    """
    text = _text(report)
    audit = report.get("dependency_audit")
    entries = audit.get("entries") if isinstance(audit, Mapping) else None
    for entry in entries or ():
        name = entry.get("name") if isinstance(entry, Mapping) else None
        if isinstance(name, str) and name in _DEPENDENCY_NAME_EXCEPTIONS:
            text = text.replace(_text(name), '""')
    return text


def _text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def read_observations(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_REPORT_BYTES:
        raise ReportError(f"{path.name}: observation document exceeds {MAX_REPORT_BYTES} bytes")
    document = json.loads(raw.decode("utf-8"))
    validate_observations(document)
    # `validate_observations` raises unless this is a mapping with the expected schema, so the
    # cast asserts what the validator has already established rather than widening anything.
    return cast(dict[str, Any], document)


def validate_observations(document: object) -> None:
    if not isinstance(document, dict) or document.get("schema") != OBSERVATIONS_SCHEMA:
        raise ReportError("observations: wrong or missing schema")
    tests = document.get("tests")
    observations = document.get("observations")
    if not isinstance(tests, list) or not isinstance(observations, list):
        raise ReportError("observations: tests and observations must be lists")
    if len(observations) > MAX_OBSERVATIONS:
        raise ReportError("observations: too many observations")
    seen_tests: set[str] = set()
    for test in tests:
        if not isinstance(test, dict) or not isinstance(test.get("test_id"), str):
            raise ReportError("observations: malformed test entry")
        if test["test_id"] in seen_tests:
            raise ReportError(f"observations: duplicate test {test['test_id']}")
        seen_tests.add(test["test_id"])
        if not isinstance(test.get("status"), str):
            raise ReportError(f"observations: {test['test_id']} has no status")
    seen_names: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict):
            raise ReportError("observations: malformed observation")
        name = observation.get("name")
        if not isinstance(name, str) or not name.startswith(PREFIX):
            raise ReportError(f"observations: bad observation name {name!r}")
        # One observation per name: two tests claiming the same row dimension is ambiguous, and
        # letting the later one win would let an unrelated test overwrite a failure.
        if name in seen_names:
            raise ReportError(f"observations: duplicate observation {name}")
        seen_names.add(name)
        if not isinstance(observation.get("test_id"), str):
            raise ReportError(f"observations: {name} has no recording test")
        if not isinstance(observation.get("body"), dict):
            raise ReportError(f"observations: {name} has no body")
        if privacy_unsafe(_text(observation["body"])):
            raise ReportError(f"observations: {name} carries privacy-unsafe content")


def _merge(documents: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], dict[str, Any]]:
    statuses: dict[str, str] = {}
    by_name: dict[str, Any] = {}
    for document in documents:
        for test in document["tests"]:
            if test["test_id"] in statuses:
                raise ReportError(f"observations: {test['test_id']} reported twice")
            statuses[test["test_id"]] = test["status"]
        for observation in document["observations"]:
            if observation["name"] in by_name:
                raise ReportError(f"observations: duplicate observation {observation['name']}")
            by_name[observation["name"]] = observation
    return statuses, by_name


def _dimension(
    name: str,
    evidence_id: str,
    statuses: Mapping[str, str],
    observations: Mapping[str, Any],
) -> dict[str, Any]:
    observation = observations.get(name)
    if observation is None:
        test_status = statuses.get(evidence_id, "not_run")
        return {
            "status": "NOT_RUN" if test_status in ("not_run", "skipped") else "FAIL",
            "evidence_id": evidence_id,
            "reason": (
                "no observation was recorded"
                if test_status in ("not_run", "skipped")
                else f"the evidence test ended {test_status} without recording an observation"
            ),
        }
    if observation["test_id"] != evidence_id:
        return {
            "status": "FAIL",
            "evidence_id": evidence_id,
            "reason": "the observation was recorded by a different test than its evidence ID",
            "recorded_by": observation["test_id"],
        }
    test_status = statuses.get(evidence_id, "not_run")
    body = observation["body"]
    assertions = body.get("assertions")
    if test_status != "passed":
        return {
            "status": "FAIL",
            "evidence_id": evidence_id,
            "reason": f"the recording test ended {test_status}",
        }
    if body.get("status") != "PASS" or not isinstance(assertions, list) or not assertions:
        return {
            "status": "FAIL",
            "evidence_id": evidence_id,
            "reason": "the observation carries no PASS verdict with identified assertions",
        }
    summary = {
        "status": "PASS",
        "evidence_id": evidence_id,
        "assertions": assertions,
        "facts": body.get("facts", {}),
    }
    if len(_text(summary).encode("utf-8")) > MAX_ROW_SUMMARY_BYTES:
        return {
            "status": "FAIL",
            "evidence_id": evidence_id,
            "reason": f"the row summary exceeds {MAX_ROW_SUMMARY_BYTES} bytes",
        }
    return summary


def _row_status(parts: Sequence[Mapping[str, Any]]) -> str:
    states = [part["status"] for part in parts]
    if states and all(state == "PASS" for state in states):
        return "PASS"
    if "FAIL" in states:
        return "FAIL"
    return "NOT_RUN"


def _dimensions(
    key: str,
    evidence: Mapping[str, str],
    statuses: Mapping[str, str],
    observations: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        dimension: _dimension(
            f"{PREFIX}{key}.{dimension}",
            evidence[f"{dimension}_evidence_id"],
            statuses,
            observations,
        )
        for dimension in manifest_generator.DIMENSIONS
    }


def _command_rows(
    manifest: Mapping[str, Any], statuses: Mapping[str, str], observations: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in manifest["command_rows"]:
        key = f"command.{row['command']}"
        dimensions = _dimensions(key, row["evidence"], statuses, observations)
        subcases = [
            {
                "subcase_id": subcase["subcase_id"],
                "dimension": subcase["dimension"],
                **_dimension(
                    f"{PREFIX}{key}.{subcase['subcase_id']}",
                    subcase["evidence_id"],
                    statuses,
                    observations,
                ),
            }
            for subcase in row.get("subcases", [])
        ]
        rows.append(
            {
                "command": row["command"],
                "operation_id": row["operation_id"],
                "dimensions": dimensions,
                "subcases": subcases,
                "status": _row_status([*dimensions.values(), *subcases]),
            }
        )
    return rows


def _ui_rows(
    manifest: Mapping[str, Any], statuses: Mapping[str, str], observations: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in manifest["ui_rows"]:
        dimensions = _dimensions(
            f"ui.{row['invariant_id']}", row["evidence"], statuses, observations
        )
        rows.append(
            {
                "invariant_id": row["invariant_id"],
                "dimensions": dimensions,
                "status": _row_status(list(dimensions.values())),
            }
        )
    return rows


def _import_rows(
    manifest: Mapping[str, Any], statuses: Mapping[str, str], observations: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in manifest["action_rows"]:
        dimensions = _dimensions(row["row_id"], row["evidence"], statuses, observations)
        rows.append(
            {
                "row_id": row["row_id"],
                "row_class": row["row_class"],
                "authority_class": row["authority_class"],
                "dimensions": dimensions,
                "status": _row_status(list(dimensions.values())),
            }
        )
    return rows


def _recovery_rows(
    manifest: Mapping[str, Any], statuses: Mapping[str, str], observations: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in manifest["recovery_rows"]:
        result = _dimension(
            f"{PREFIX}seam.{row['seam_id'].removeprefix('seam.')}.recovery",
            row["evidence_id"],
            statuses,
            observations,
        )
        if result["status"] == "PASS":
            injections = result["facts"].get("injections")
            if not isinstance(injections, int) or injections < row["injections_min"]:
                result = {
                    "status": "FAIL",
                    "evidence_id": row["evidence_id"],
                    "reason": (
                        f"{injections!r} injections recorded; the floor is {row['injections_min']}"
                    ),
                }
        rows.append(
            {
                "seam_id": row["seam_id"],
                "injection": row["injection"],
                "injections_min": row["injections_min"],
                **result,
            }
        )
    return rows


def _satisfies(op: str, observed: float, threshold: float) -> bool:
    if op == "<=":
        return observed <= threshold
    if op == ">=":
        return observed >= threshold
    return observed == threshold


def _measurements(
    manifest: Mapping[str, Any], statuses: Mapping[str, str], observations: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for measurement in manifest["measurements"]:
        measurement_id = measurement["measurement_id"]
        base = {
            "measurement_id": measurement_id,
            "scope": measurement["scope"],
            "unit": measurement["unit"],
            "threshold": measurement["threshold"],
            "evidence_id": measurement["evidence_id"],
        }
        if measurement_id in NOT_APPLICABLE:
            rows.append(
                {**base, "status": "N/A", "unselected_capability": NOT_APPLICABLE[measurement_id]}
            )
            continue
        name = f"{PREFIX}measurement.{measurement_id}"
        observation = observations.get(name)
        test_status = statuses.get(measurement["evidence_id"], "not_run")
        if observation is None:
            rows.append(
                {
                    **base,
                    "status": "NOT_RUN" if test_status in ("not_run", "skipped") else "FAIL",
                    "reason": "no measurement was recorded",
                }
            )
            continue
        body = observation["body"]
        observed = body.get("observed")
        problems: list[str] = []
        if observation["test_id"] != measurement["evidence_id"]:
            problems.append("recorded by a different test than its evidence ID")
        if test_status != "passed":
            problems.append(f"the recording test ended {test_status}")
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not math.isfinite(observed)
        ):
            problems.append("no finite observed value")
        for field in ("method", "sample_count", "interval_ms"):
            if field not in body:
                problems.append(f"no {field}")
        if body.get("unit") != measurement["unit"]:
            problems.append(f"unit {body.get('unit')!r} is not {measurement['unit']!r}")
        row = {
            **base,
            "method": body.get("method"),
            "observed": observed,
            "sample_count": body.get("sample_count"),
            "interval_ms": body.get("interval_ms"),
        }
        if problems:
            rows.append({**row, "status": "FAIL", "reason": "; ".join(problems)})
            continue
        passed = _satisfies(
            measurement["threshold"]["op"],
            float(observed),
            float(measurement["threshold"]["value"]),
        )
        rows.append({**row, "status": "PASS" if passed else "FAIL"})
    return rows


def _workloads(
    manifest: Mapping[str, Any], statuses: Mapping[str, str], observations: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    evidence_by_fixture = {
        "smoke": manifest_generator.SMOKE_WORKLOAD,
        "stress": manifest_generator.PLAYBACK_WORKLOAD,
        "render": manifest_generator.OUTPUT_WORKLOAD,
        "ui_stress": manifest_generator.UI_WORKLOAD,
    }
    for key, fixture in manifest["fixtures"].items():
        result = _dimension(
            f"{PREFIX}workload.{key}.identity",
            evidence_by_fixture[key],
            statuses,
            observations,
        )
        rows.append({"fixture": fixture, **result})
    return rows


def _identity(documents: Sequence[Mapping[str, Any]], manifest_sha256: str) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    conflicts: list[str] = []
    for document in documents:
        for key, value in document.get("identity", {}).items():
            if key in identity and identity[key] != value:
                conflicts.append(key)
            identity.setdefault(key, value)
    missing = [field for field in IDENTITY_FIELDS if not identity.get(field)]
    identity["status"] = "PASS" if not missing else "NOT_RUN"
    if missing:
        identity["missing"] = missing
    # Every gathered document declares the manifest it ran against; a run against another
    # membership cannot be joined to this one.
    if identity.get("manifest_sha256", manifest_sha256) != manifest_sha256:
        identity["status"] = "FAIL"
        identity["reason"] = "the observations were gathered against a different manifest"
    if conflicts:
        identity["status"] = "FAIL"
        identity["reason"] = f"the observation documents disagree on {sorted(set(conflicts))}"
    return identity


def _dependency_audit(audit: Mapping[str, Any] | None) -> dict[str, Any]:
    if audit is None:
        return {"status": "NOT_RUN", "reason": "no dependency audit was supplied"}
    if audit.get("schema") != AUDIT_SCHEMA:
        return {"status": "FAIL", "reason": "the dependency audit has the wrong schema"}
    entries = audit.get("entries")
    if not isinstance(entries, list) or not entries:
        return {"status": "FAIL", "reason": "the dependency audit lists no inventory"}
    for entry in entries:
        for field in ("name", "version", "license", "disposition"):
            if not entry.get(field):
                return {"status": "FAIL", "reason": f"an audit entry has no {field}"}
    failed = [entry["name"] for entry in entries if entry["disposition"] not in ("unchanged",)]
    return {
        "status": "PASS" if not failed and audit.get("status") == "PASS" else "FAIL",
        "entries": entries,
        "changed": failed,
    }


def join(
    manifest: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    audit: Mapping[str, Any] | None = None,
    *,
    manifest_sha256: str,
) -> dict[str, Any]:
    statuses, observations = _merge(documents)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "identity": _identity(documents, manifest_sha256),
        "workloads": _workloads(manifest, statuses, observations),
        "measurements": _measurements(manifest, statuses, observations),
        "command_rows": _command_rows(manifest, statuses, observations),
        "ui_rows": _ui_rows(manifest, statuses, observations),
        "import_rows": _import_rows(manifest, statuses, observations),
        "recovery_rows": _recovery_rows(manifest, statuses, observations),
        "dependency_audit": _dependency_audit(audit),
    }
    report["disposition"] = disposition(report)
    encoded = _text(report)
    if privacy_unsafe(_scannable(report)):
        raise ReportError("the joined report carries privacy-unsafe content")
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ReportError(f"the joined report exceeds {MAX_REPORT_BYTES} bytes")
    return report


def disposition(report: Mapping[str, Any]) -> dict[str, Any]:
    states: list[str] = [report["identity"]["status"], report["dependency_audit"]["status"]]
    for section in ("workloads", "measurements", "command_rows", "ui_rows", "import_rows"):
        states.extend(row["status"] for row in report[section])
    states.extend(row["status"] for row in report["recovery_rows"])
    counts = {status: states.count(status) for status in STATUSES}
    if counts["FAIL"]:
        verdict = "FAIL"
    elif counts["NOT_RUN"]:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"
    return {"verdict": verdict, "counts": counts}


def validate_report(report: object) -> None:
    """Reject a report whose shape or statuses were not produced by `join`."""

    if not isinstance(report, dict) or report.get("schema") != REPORT_SCHEMA:
        raise ReportError("report: wrong schema")
    if tuple(key for key in report if key != "schema") != REPORT_SECTIONS:
        raise ReportError("report: sections differ from the frozen section list")
    for section in ("workloads", "measurements", "command_rows", "ui_rows", "import_rows"):
        for row in report[section]:
            if row.get("status") not in STATUSES:
                raise ReportError(f"report: {section} row has status {row.get('status')!r}")
            if row["status"] == "N/A" and (
                section != "measurements" or row.get("measurement_id") not in NOT_APPLICABLE
            ):
                raise ReportError(f"report: {section} row claims N/A outside the closed list")
    if report["disposition"] != disposition(report):
        raise ReportError("report: the disposition does not follow from the rows")


def retained_artifact_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", nargs="+", type=Path)
    parser.add_argument("--dependency-audit", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="retained diagnostic directory checked against the 256 MiB cap",
    )
    args = parser.parse_args(argv)
    manifest = json.loads(manifest_generator.MANIFEST_PATH.read_text(encoding="utf-8"))
    documents = [read_observations(path) for path in args.observations]
    audit = (
        json.loads(args.dependency_audit.read_text(encoding="utf-8"))
        if args.dependency_audit
        else None
    )
    report = join(
        manifest,
        documents,
        audit,
        manifest_sha256=manifest_generator.sha256_of(manifest_generator.MANIFEST_PATH),
    )
    if args.evidence_dir is not None:
        retained = retained_artifact_bytes(args.evidence_dir)
        if retained > MAX_RETAINED_ARTIFACT_BYTES:
            raise ReportError(
                f"retained artifacts are {retained} bytes; the cap is {MAX_RETAINED_ARTIFACT_BYTES}"
            )
    args.output.write_bytes((json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode())
    verdict = report["disposition"]["verdict"]
    print(f"wrote {args.output.name}: {verdict} {report['disposition']['counts']}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
