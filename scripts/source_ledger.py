"""Validate an offline H3 source, claim, unknown, and hypothesis ledger.

The ledger is an evidence index, not a source downloader. URLs are metadata only. A source can
optionally name a repository-relative local canary and an expected SHA-256 digest; the checker
reports drift and never updates the ledger or the canary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ID = "h3-context-source-ledger/1"
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_CANARY_BYTES = 16 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
MAX_SOURCES = 512
MAX_RECORDS = 2048

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LOCAL_PATH_PATTERN = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$")
EVIDENCE_CLASSES = frozenset(
    {
        "official",
        "framework_reference",
        "community_recommended",
        "experimental",
        "modified",
    }
)
CONFIDENCE_VALUES = frozenset({"high", "medium", "low", "unknown"})
STATUS_VALUES = frozenset(
    {"accepted", "blocked", "open", "proposed", "rejected", "resolved", "superseded", "tested"}
)
DRIFT_POLICIES = frozenset(
    {"fail_on_change", "report_only", "revalidate", "source_maintainer_review"}
)
KINDS = frozenset({"claim", "unknown", "hypothesis", "conflict"})


class LedgerError(ValueError):
    """Raised when a ledger violates its versioned contract."""


@dataclass(frozen=True)
class Source:
    source_id: str
    url: str
    revision: str
    retrieved_at: str
    evidence_class: str
    role: str | None
    local_path: str | None
    sha256: str | None
    canary: bool


@dataclass(frozen=True)
class Record:
    record_id: str
    kind: str
    statement: str
    status: str
    evidence_class: str
    confidence: str
    source_ids: tuple[str, ...]
    owning_tests: tuple[str, ...]
    drift_policy: str
    affected_profiles: tuple[str, ...]
    related_record_ids: tuple[str, ...]
    resolution: str | None


@dataclass(frozen=True)
class Ledger:
    ledger_id: str
    ledger_version: str
    updated_at: str
    sources: tuple[Source, ...]
    records: tuple[Record, ...]


@dataclass(frozen=True)
class CanaryResult:
    source_id: str
    status: str
    path: str
    expected_sha256: str
    actual_sha256: str | None
    size_bytes: int | None
    diagnostic: str | None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "source_id": self.source_id,
            "status": self.status,
            "path": self.path,
            "expected_sha256": self.expected_sha256,
        }
        if self.actual_sha256 is not None:
            result["actual_sha256"] = self.actual_sha256
        if self.size_bytes is not None:
            result["size_bytes"] = self.size_bytes
        if self.diagnostic is not None:
            result["diagnostic"] = self.diagnostic
        return result


@dataclass(frozen=True)
class LedgerReport:
    schema: str
    ledger_id: str | None
    ledger_version: str | None
    status: str
    source_count: int
    record_count: int
    canaries: tuple[CanaryResult, ...]
    diagnostics: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "ledger_id": self.ledger_id,
            "ledger_version": self.ledger_version,
            "status": self.status,
            "source_count": self.source_count,
            "record_count": self.record_count,
            "canaries": [item.as_dict() for item in self.canaries],
            "diagnostics": list(self.diagnostics),
        }


JsonObject = dict[str, object]


def _object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise LedgerError(f"{field} must be an object")
    return cast(JsonObject, value)


def _string(value: object, field: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{field} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise LedgerError(f"{field} has an invalid format")
    return value


def _bounded_string(value: object, field: str, maximum: int) -> str:
    result = _string(value, field)
    if len(result) > maximum:
        raise LedgerError(f"{field} exceeds the {maximum}-character limit")
    return result


def _date(value: object, field: str) -> str:
    result = _string(value, field)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise LedgerError(f"{field} must be an ISO date") from exc
    return result


def _enum(value: object, field: str, allowed: frozenset[str]) -> str:
    result = _string(value, field)
    if result not in allowed:
        choices = ", ".join(sorted(allowed))
        raise LedgerError(f"{field} must be one of: {choices}")
    return result


def _string_list(value: object, field: str, *, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise LedgerError(f"{field} must be a non-empty list")
    if len(value) > maximum:
        raise LedgerError(f"{field} exceeds the {maximum}-item limit")
    values = tuple(
        _string(item, f"{field}[{index}]", pattern=IDENTIFIER_PATTERN)
        for index, item in enumerate(value)
    )
    if len(values) != len(set(values)):
        raise LedgerError(f"{field} contains duplicate values")
    return values


def _check_keys(
    value: Mapping[str, object], required: frozenset[str], allowed: frozenset[str], field: str
) -> None:
    missing = sorted(required.difference(value))
    extra = sorted(set(value).difference(allowed))
    if missing:
        raise LedgerError(f"{field} is missing required fields: {', '.join(missing)}")
    if extra:
        raise LedgerError(f"{field} contains unsupported fields: {', '.join(extra)}")


def _safe_local_path(value: object, field: str) -> str:
    result = _string(value, field)
    if LOCAL_PATH_PATTERN.fullmatch(result) is None:
        raise LedgerError(f"{field} must be a safe repository-relative path")
    parts = PurePosixPath(result).parts
    if not parts or any(part in {"", ".", ".."} for part in parts) or "\\" in result:
        raise LedgerError(f"{field} must be a normalized repository-relative path")
    return result


def _validate_url(value: object, field: str) -> str:
    result = _string(value, field)
    if len(result) > 2048:
        raise LedgerError(f"{field} exceeds the 2048-character limit")
    try:
        parsed = urlsplit(result)
    except ValueError as exc:
        raise LedgerError(f"{field} is not a valid URL") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise LedgerError(f"{field} must be an HTTPS URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise LedgerError(f"{field} must not contain userinfo")
    if any(
        marker in parsed.query.casefold() for marker in ("token=", "sig=", "signature=", "x-amz-")
    ):
        raise LedgerError(f"{field} must not contain signed or credential query parameters")
    return result


def _validate_source(value: object, index: int) -> Source:
    field = f"sources[{index}]"
    source = _object(value, field)
    _check_keys(
        source,
        frozenset({"source_id", "url", "revision", "retrieved_at", "evidence_class", "canary"}),
        frozenset(
            {
                "source_id",
                "url",
                "revision",
                "retrieved_at",
                "evidence_class",
                "role",
                "local_path",
                "sha256",
                "canary",
            }
        ),
        field,
    )
    source_id = _string(source["source_id"], f"{field}.source_id", pattern=IDENTIFIER_PATTERN)
    url = _validate_url(source["url"], f"{field}.url")
    revision = _string(source["revision"], f"{field}.revision", pattern=REVISION_PATTERN)
    retrieved_at = _date(source["retrieved_at"], f"{field}.retrieved_at")
    evidence_class = _enum(source["evidence_class"], f"{field}.evidence_class", EVIDENCE_CLASSES)
    role = None
    if "role" in source:
        role = _string(source["role"], f"{field}.role", pattern=IDENTIFIER_PATTERN)
    canary = source["canary"]
    if not isinstance(canary, bool):
        raise LedgerError(f"{field}.canary must be a boolean")
    local_path: str | None = None
    if "local_path" in source:
        local_path = _safe_local_path(source["local_path"], f"{field}.local_path")
    sha256: str | None = None
    if "sha256" in source:
        sha256 = _string(source["sha256"], f"{field}.sha256")
        if SHA256_PATTERN.fullmatch(sha256) is None:
            raise LedgerError(f"{field}.sha256 must be a lowercase SHA-256 digest")
    if canary and (local_path is None or sha256 is None):
        raise LedgerError(f"{field} canaries require local_path and sha256")
    if not canary and sha256 is not None:
        raise LedgerError(f"{field}.sha256 is only allowed for canaries")
    return Source(
        source_id, url, revision, retrieved_at, evidence_class, role, local_path, sha256, canary
    )


def _validate_record(value: object, index: int) -> Record:
    field = f"records[{index}]"
    record = _object(value, field)
    _check_keys(
        record,
        frozenset(
            {
                "record_id",
                "kind",
                "statement",
                "status",
                "evidence_class",
                "confidence",
                "source_ids",
                "owning_tests",
                "drift_policy",
                "affected_profiles",
            }
        ),
        frozenset(
            {
                "record_id",
                "kind",
                "statement",
                "status",
                "evidence_class",
                "confidence",
                "source_ids",
                "owning_tests",
                "drift_policy",
                "affected_profiles",
                "related_record_ids",
                "resolution",
            }
        ),
        field,
    )
    record_id = _string(record["record_id"], f"{field}.record_id", pattern=IDENTIFIER_PATTERN)
    kind = _enum(record["kind"], f"{field}.kind", KINDS)
    statement = _bounded_string(record["statement"], f"{field}.statement", 4096)
    status = _enum(record["status"], f"{field}.status", STATUS_VALUES)
    evidence_class = _enum(record["evidence_class"], f"{field}.evidence_class", EVIDENCE_CLASSES)
    confidence = _enum(record["confidence"], f"{field}.confidence", CONFIDENCE_VALUES)
    source_ids = _string_list(record["source_ids"], f"{field}.source_ids")
    owning_tests = _string_list(record["owning_tests"], f"{field}.owning_tests")
    drift_policy = _enum(record["drift_policy"], f"{field}.drift_policy", DRIFT_POLICIES)
    affected_profiles = _string_list(record["affected_profiles"], f"{field}.affected_profiles")
    related_record_ids: tuple[str, ...] = ()
    if "related_record_ids" in record:
        related_record_ids = _string_list(
            record["related_record_ids"], f"{field}.related_record_ids"
        )
    resolution: str | None = None
    if "resolution" in record:
        resolution = _bounded_string(record["resolution"], f"{field}.resolution", 4096)
    if kind == "conflict":
        if len(related_record_ids) < 2 or resolution is None:
            raise LedgerError(f"{field} conflicts require two related records and a resolution")
    elif resolution is not None:
        raise LedgerError(f"{field}.resolution is only allowed for conflicts")
    return Record(
        record_id,
        kind,
        statement,
        status,
        evidence_class,
        confidence,
        source_ids,
        owning_tests,
        drift_policy,
        affected_profiles,
        related_record_ids,
        resolution,
    )


def validate_ledger(value: object) -> Ledger:
    """Validate a decoded ledger and return its immutable typed representation."""

    document = _object(value, "ledger")
    _check_keys(
        document,
        frozenset({"schema", "ledger_id", "ledger_version", "updated_at", "sources", "records"}),
        frozenset({"schema", "ledger_id", "ledger_version", "updated_at", "sources", "records"}),
        "ledger",
    )
    schema = _string(document["schema"], "ledger.schema")
    if schema != SCHEMA_ID:
        raise LedgerError(f"unsupported ledger schema: {schema}")
    ledger_id = _string(document["ledger_id"], "ledger.ledger_id", pattern=IDENTIFIER_PATTERN)
    ledger_version = _string(document["ledger_version"], "ledger.ledger_version")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", ledger_version) is None:
        raise LedgerError("ledger.ledger_version must be semantic version text")
    updated_at = _date(document["updated_at"], "ledger.updated_at")
    sources_value = document["sources"]
    records_value = document["records"]
    if not isinstance(sources_value, list) or not sources_value:
        raise LedgerError("ledger.sources must be a non-empty list")
    if len(sources_value) > MAX_SOURCES:
        raise LedgerError(f"ledger.sources exceeds the {MAX_SOURCES}-item limit")
    if not isinstance(records_value, list) or not records_value:
        raise LedgerError("ledger.records must be a non-empty list")
    if len(records_value) > MAX_RECORDS:
        raise LedgerError(f"ledger.records exceeds the {MAX_RECORDS}-item limit")
    sources = tuple(_validate_source(value, index) for index, value in enumerate(sources_value))
    records = tuple(_validate_record(value, index) for index, value in enumerate(records_value))
    source_ids = {source.source_id for source in sources}
    record_ids = {record.record_id for record in records}
    if len(source_ids) != len(sources):
        raise LedgerError("source IDs must be unique")
    if len(record_ids) != len(records):
        raise LedgerError("record IDs must be unique")
    for record in records:
        missing_sources = sorted(set(record.source_ids).difference(source_ids))
        if missing_sources:
            raise LedgerError(
                f"{record.record_id} references unknown sources: {', '.join(missing_sources)}"
            )
        missing_records = sorted(set(record.related_record_ids).difference(record_ids))
        if missing_records:
            raise LedgerError(
                f"{record.record_id} references unknown records: {', '.join(missing_records)}"
            )
        if record.record_id in record.related_record_ids:
            raise LedgerError(f"{record.record_id} cannot relate to itself")
    return Ledger(ledger_id, ledger_version, updated_at, sources, records)


def load_ledger(path: Path) -> Ledger:
    """Read and validate a bounded ledger without following a ledger symlink."""

    if path.is_symlink():
        raise LedgerError("ledger path must not be a symlink")
    try:
        if not path.is_file():
            raise LedgerError("ledger file does not exist")
        size = path.stat().st_size
        if size > MAX_LEDGER_BYTES:
            raise LedgerError(f"ledger exceeds the {MAX_LEDGER_BYTES}-byte limit")
        payload = path.read_bytes()
    except LedgerError:
        raise
    except OSError as exc:
        raise LedgerError("ledger file cannot be read") from exc
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError("ledger must be UTF-8 JSON") from exc
    return validate_ledger(value)


def _safe_repo_root(repo_root: Path) -> Path:
    try:
        resolved = repo_root.resolve(strict=True)
    except OSError as exc:
        raise LedgerError("repository root cannot be resolved") from exc
    if not resolved.is_dir():
        raise LedgerError("repository root must be a directory")
    return resolved


def _canary_path(source: Source, repo_root: Path) -> Path:
    if source.local_path is None:
        raise LedgerError(f"{source.source_id} is missing its canary path")
    root = _safe_repo_root(repo_root)
    candidate_raw = root / PurePosixPath(source.local_path)
    if candidate_raw.is_symlink():
        raise LedgerError(f"{source.source_id} canary must not be a symlink")
    candidate = candidate_raw.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LedgerError(f"{source.source_id} canary escapes the repository root") from exc
    return candidate


def _hash_file(path: Path) -> tuple[str, int]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise LedgerError("canary metadata cannot be read") from exc
    if size > MAX_CANARY_BYTES:
        raise LedgerError(f"canary exceeds the {MAX_CANARY_BYTES}-byte limit")
    digest = hashlib.sha256()
    read = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(HASH_CHUNK_BYTES)
                if not chunk:
                    break
                read += len(chunk)
                if read > MAX_CANARY_BYTES:
                    raise LedgerError(f"canary exceeds the {MAX_CANARY_BYTES}-byte limit")
                digest.update(chunk)
    except LedgerError:
        raise
    except OSError as exc:
        raise LedgerError("canary cannot be read") from exc
    return digest.hexdigest(), read


def _check_canary(source: Source, repo_root: Path) -> CanaryResult:
    if source.local_path is None or source.sha256 is None:
        return CanaryResult(
            source.source_id, "INVALID", "", "", None, None, "canary metadata is incomplete"
        )
    try:
        path = _canary_path(source, repo_root)
    except LedgerError as exc:
        return CanaryResult(
            source.source_id,
            "INVALID",
            source.local_path,
            source.sha256,
            None,
            None,
            str(exc),
        )
    if not path.exists():
        return CanaryResult(
            source.source_id,
            "MISSING",
            source.local_path,
            source.sha256,
            None,
            None,
            "canary file is missing",
        )
    if not path.is_file():
        return CanaryResult(
            source.source_id,
            "INVALID",
            source.local_path,
            source.sha256,
            None,
            None,
            "canary path is not a regular file",
        )
    try:
        actual, size = _hash_file(path)
    except LedgerError as exc:
        return CanaryResult(
            source.source_id,
            "INVALID",
            source.local_path,
            source.sha256,
            None,
            None,
            str(exc),
        )
    status = "PASS" if actual == source.sha256 else "DRIFT"
    diagnostic = None if status == "PASS" else "SHA-256 digest differs from the accepted canary"
    return CanaryResult(
        source.source_id, status, source.local_path, source.sha256, actual, size, diagnostic
    )


def inspect_ledger(path: Path, repo_root: Path = ROOT) -> LedgerReport:
    """Validate a ledger and check local canaries without mutation or network access."""

    try:
        ledger = load_ledger(path)
    except LedgerError as exc:
        return LedgerReport(SCHEMA_ID, None, None, "INVALID", 0, 0, (), (str(exc),))
    canaries = tuple(_check_canary(source, repo_root) for source in ledger.sources if source.canary)
    statuses = {item.status for item in canaries}
    if "INVALID" in statuses:
        status = "INVALID"
    elif "DRIFT" in statuses:
        status = "DRIFT"
    elif "MISSING" in statuses:
        status = "MISSING"
    else:
        status = "PASS"
    diagnostics = tuple(item.diagnostic for item in canaries if item.diagnostic is not None)
    return LedgerReport(
        SCHEMA_ID,
        ledger.ledger_id,
        ledger.ledger_version,
        status,
        len(ledger.sources),
        len(ledger.records),
        canaries,
        diagnostics,
    )


def render_json(report: LedgerReport) -> str:
    """Render a stable, compact JSON report suitable for CI artifacts."""

    return json.dumps(report.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path, help="path to the JSON ledger")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help="repository root used to resolve repository-relative canaries",
    )
    parser.add_argument("--json", action="store_true", help="emit one deterministic JSON report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checker; non-zero statuses fail closed for CI."""

    arguments = _parser().parse_args(argv)
    report = inspect_ledger(arguments.ledger, arguments.repo_root)
    if arguments.json:
        print(render_json(report))
    else:
        print(f"{report.status}: {arguments.ledger}")
        for diagnostic in report.diagnostics:
            print(f"- {diagnostic}")
    return {"PASS": 0, "DRIFT": 2, "MISSING": 3, "INVALID": 4}[report.status]


if __name__ == "__main__":
    sys.exit(main())
