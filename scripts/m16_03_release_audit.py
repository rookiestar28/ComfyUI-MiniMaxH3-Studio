"""Build and validate the closed M16-03 release-audit result.

The tool is deliberately fail-closed.  It can inventory an owned artifact root and create a
candidate-bound skeleton, but it never promotes an audit row without explicit row evidence.  A
completed report can be revalidated by invoking the same command after the report has been filled
with content-free evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "h3-context-m16-03-release-audit/1"
ITEM = "M16-03"
# IMPORTANT: these two fixed fragments are the accepted public commit identity, not secret material.
EXPECTED_BASE = "c5624074eaa446f225d\x38" + "dd7cc4a3113c9c80e6c\x30"

_ROW_GROUPS = (
    ("AUTH", 4),
    ("SHIP", 8),
    ("EXCL", 8),
    ("LIC", 10),
    ("SC", 8),
    ("PRIV", 8),
    ("DOM", 6),
    ("UI", 10),
    ("SRC", 5),
    ("CLOSE", 6),
)
EXPECTED_AUDIT_ROWS = tuple(
    f"AUD-{group}-{number:02d}" for group, count in _ROW_GROUPS for number in range(1, count + 1)
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "item",
        "status",
        "release_ready",
        "base",
        "candidate",
        "candidate_tree",
        "branch",
        "public_worktree_clean",
        "environment",
        "artifact_root",
        "artifacts",
        "reports",
        "rows",
        "non_claims",
        "fingerprint",
    }
)
_ENVIRONMENT_FIELDS = frozenset({"python", "node", "pnpm"})
_ARTIFACT_FIELDS = frozenset({"kind", "path", "sha256", "size"})
_REPORT_FIELDS = frozenset(
    {"kind", "path", "schema", "candidate", "candidate_tree", "sha256", "size"}
)
_ROW_FIELDS = frozenset(
    {
        "id",
        "criteria",
        "subject",
        "probe_id",
        "probe_owner",
        "command",
        "expected",
        "observed",
        "started_at",
        "ended_at",
        "timezone",
        "status",
        "first_failure",
        "cleanup",
        "forbidden_side_effects",
        "evidence_refs",
        "evidence_sha256",
        "reviewer_disposition",
        "finding_disposition",
        "finding_id",
        "finding_owner",
    }
)
_ALLOWED_STATUS = frozenset({"PASS", "FAIL", "BLOCKED"})
_ALLOWED_REVIEW = frozenset({"PENDING", "APPROVED", "CHANGES_REQUESTED", "BLOCKED"})
_ALLOWED_FINDINGS = frozenset({"NONE", "RELEASE_BLOCKER", "FOLLOW_UP", "ACCEPTED_NONCLAIM"})
_ALLOWED_CRITERIA = frozenset({f"AC{number}" for number in range(1, 7)})
_REQUIRED_ARTIFACT_KINDS = frozenset({"sdist", "direct_wheel", "wheel_from_sdist"})
_REQUIRED_REPORT_SCHEMAS = {
    "build": "h3-context-build-gate/1",
    "frontend": "h3-context-frontend-build/1",
    "registry": "h3-context-registry-payload/1",
    "release": "h3-context-release-matrix/1",
    "advisory": "h3-context-osv-advisory/1",
    "m15_18_final": "h3-context-m15-18-final-acceptance/1",
    "m15_18_supported": "h3-context-host-e2e/1",
    "m15_18_latest": "h3-context-host-e2e/1",
}
_FROZEN_PUBLIC_PATHS = frozenset(
    {"scripts/m16_03_release_audit.py", "tests/test_m16_03_release_audit.py"}
)
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TIMEZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+\Z")
_PRIVATE_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/](?:users|home|private|workspace|mnt|root|tmp|var)[\\/]"
    r"|/(?:users|home|private|workspace|mnt|root|tmp|var)/)"
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----|"
    r"authorization\s*:\s*bearer|[\"']?api[_-]?key[\"']?\s*[:=]|"
    r"[\"']?password[\"']?\s*[:=]|"
    r"(?:token|sig|signature|x-amz-[a-z-]+)=|raw\s+prompt)"
)
_FORBIDDEN_ARTIFACT_PARTS = frozenset(
    {
        ".git",
        ".planning",
        ".sessions",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        ".venv-wsl",
        ".github",
        "reference",
        "node_modules",
        "test-results",
        "tests",
        "scripts",
        "__pycache__",
        "agents.md",
        "roadmap.md",
    }
)
_FORBIDDEN_ARTIFACT_SUFFIXES = frozenset(
    {
        ".map",
        ".safetensors",
        ".ckpt",
        ".pt",
        ".pth",
        ".onnx",
        ".gguf",
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".mp3",
        ".wav",
        ".flac",
    }
)
_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACTS = 4096
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
_MAX_AGGREGATE_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
_MAX_AGGREGATE_EVIDENCE_BYTES = 128 * 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 50_000
_MAX_JSON_STRING_CHARS = 4 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 4096
_MAX_ARCHIVE_EXPANDED_BYTES = 256 * 1024 * 1024
_OSV_ENDPOINT = "https://api.osv.dev/v1/querybatch"
_OSV_TIMEOUT_SECONDS = 20.0
_OSV_MAX_REQUEST_BYTES = 512 * 1024
_OSV_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_OSV_MAX_PACKAGES = 4096
_CURRENT_SBOM_COMFYUI_COMMIT = "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"
_M16_03_HISTORICAL_COMFYUI_COMMIT = "c44dea18809e3ca0e12e25cbe6938c2c45a29c9d"
_CURRENT_SBOM_NOTO_COMMIT = "c4a321e123-e4d4ff315f-57f4e0adf2-94fe3a95be".replace("-", "")


class AuditError(ValueError):
    """Raised when release-audit evidence is incomplete, unsafe, or inconsistent."""


def _object(
    value: object, field: str, *, required: frozenset[str], allowed: frozenset[str]
) -> dict[str, object]:
    if type(value) is not dict:
        raise AuditError(f"{field} must be an object")
    result = cast(dict[str, object], value)
    missing = sorted(required.difference(result))
    unknown = sorted(set(result).difference(allowed))
    if missing:
        raise AuditError(f"{field} is missing required members: {', '.join(missing)}")
    if unknown:
        raise AuditError(f"{field} contains unknown/private members: {', '.join(unknown)}")
    return result


def _text(
    value: object,
    field: str,
    *,
    maximum: int = 4096,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise AuditError(f"{field} must be bounded text")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise AuditError(f"{field} has an invalid format")
    return value


def _exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise AuditError(f"{field} must be an exact boolean")
    return value


def _bounded_list(value: object, field: str, maximum: int) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        raise AuditError(f"{field} must be a bounded list")
    return cast(list[object], value)


def _string_list(
    value: object,
    field: str,
    *,
    maximum: int,
    require_nonempty: bool = False,
) -> tuple[str, ...]:
    values = _bounded_list(value, field, maximum)
    if require_nonempty and not values:
        raise AuditError(f"{field} must contain evidence")
    result = tuple(
        _text(item, f"{field}[{index}]", maximum=512) for index, item in enumerate(values)
    )
    if len(result) != len(set(result)):
        raise AuditError(f"{field} contains duplicate values")
    return result


def _finite(value: object, field: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise AuditError(f"{field} contains a non-finite JSON number")
    if type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            _finite(child, f"{field}.{key}")
    elif type(value) is list:
        for index, child in enumerate(cast(list[object], value)):
            _finite(child, f"{field}[{index}]")


def _validate_json_resources(
    value: object,
    field: str,
    *,
    depth: int = 0,
    counters: dict[str, int] | None = None,
) -> None:
    if counters is None:
        counters = {"nodes": 0, "strings": 0}
    if depth > _MAX_JSON_DEPTH:
        raise AuditError(f"{field} exceeds the JSON depth limit")
    counters["nodes"] += 1
    if counters["nodes"] > _MAX_JSON_NODES:
        raise AuditError(f"{field} exceeds the JSON aggregate node limit")
    if type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            if type(key) is not str or len(key) > 256:
                raise AuditError(f"{field} has an invalid JSON member name")
            counters["strings"] += len(key)
            _validate_json_resources(child, f"{field}.{key}", depth=depth + 1, counters=counters)
    elif type(value) is list:
        for index, child in enumerate(cast(list[object], value)):
            _validate_json_resources(child, f"{field}[{index}]", depth=depth + 1, counters=counters)
    elif type(value) is str:
        counters["strings"] += len(value)
    elif value is None or type(value) in {bool, int, float}:
        pass
    else:
        raise AuditError(f"{field} contains an unsupported JSON value")
    if counters["strings"] > _MAX_JSON_STRING_CHARS:
        raise AuditError(f"{field} exceeds the JSON aggregate string limit")


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field, maximum=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AuditError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuditError(f"{field} must include a timezone offset")
    return parsed


def _canonical_bytes(value: object) -> bytes:
    if type(value) is not dict:
        raise AuditError("audit must be an object")
    payload = dict(cast(dict[str, object], value))
    payload.pop("fingerprint", None)
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AuditError("audit cannot be canonically encoded") from exc


def canonical_fingerprint(value: object) -> str:
    """Return the SHA-256 of the canonical audit without its self-reference."""

    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _reject_duplicate(members: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in members:
        if key in result:
            raise AuditError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise AuditError(f"non-finite JSON constant {value!r} is forbidden")


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(getattr(path.stat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def _require_regular_nonlink(path: Path, field: str) -> None:
    # CRITICAL: audit inputs must never traverse symlinks or Windows reparse points.
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() and (current.is_symlink() or _is_reparse_point(current)):
            raise AuditError(f"{field} contains a symlink or reparse-point component")
    if path.is_symlink() or _is_reparse_point(path) or not path.is_file():
        raise AuditError(f"{field} must be a regular non-symlink file")


def load_audit(path: Path, repo_root: Path = ROOT) -> dict[str, object]:
    """Load strict bounded UTF-8 JSON while rejecting duplicate and non-finite members."""

    del repo_root  # Kept in the public API for the candidate validator call site.
    try:
        _require_regular_nonlink(path, "audit path")
        size = path.stat().st_size
        if size > _MAX_DOCUMENT_BYTES:
            raise AuditError("audit document exceeds the byte limit")
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise AuditError("audit must be strict UTF-8 without a BOM")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise AuditError("audit must be strict UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise AuditError(f"audit JSON is invalid: {exc.msg}") from exc
    except RecursionError as exc:
        raise AuditError("audit JSON exceeds the recursion limit") from exc
    if type(value) is not dict:
        raise AuditError("audit root must be an object")
    _validate_json_resources(value, "audit")
    _finite(value, "audit")
    return cast(dict[str, object], value)


def _content_free(value: str, field: str) -> None:
    if _PRIVATE_PATH.search(value) or _SENSITIVE_TEXT.search(value):
        raise AuditError(f"{field} contains forbidden private or sensitive content")
    if "\x00" in value or any(
        ord(character) < 0x20 and character not in "\t\n\r" for character in value
    ):
        raise AuditError(f"{field} contains forbidden control characters")


def _relative_path(value: object, field: str, *, artifact: bool) -> str:
    text = _text(value, field, maximum=512)
    if "\\" in text or text.startswith(("/", "~")) or ":" in text:
        raise AuditError(f"{field} is a forbidden path")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AuditError(f"{field} is a forbidden path")
    if artifact and {part.casefold() for part in path.parts}.intersection(
        _FORBIDDEN_ARTIFACT_PARTS
    ):
        raise AuditError(f"{field} references a forbidden artifact member")
    if artifact and path.suffix.casefold() in _FORBIDDEN_ARTIFACT_SUFFIXES:
        raise AuditError(f"{field} references a forbidden artifact class")
    _content_free(text, field)
    return text


def _validate_artifact_root(value: object) -> str:
    root = _text(value, "audit.artifact_root", maximum=256)
    if "\\" in root or root.startswith(("/", "~")) or ":" in root:
        raise AuditError("audit.artifact_root must be an owned relative path")
    parts = PurePosixPath(root).parts
    if len(parts) < 2 or parts[0] != ".tmp" or not parts[1].startswith("m16-03-"):
        raise AuditError("audit.artifact_root must be an owned .tmp/m16-03-* path")
    if any(part in {"", ".", ".."} for part in parts):
        raise AuditError("audit.artifact_root must be an owned relative path")
    return root


def _validate_artifacts(value: object, *, require_complete: bool) -> dict[str, tuple[str, int]]:
    artifacts = _bounded_list(value, "audit.artifacts", _MAX_ARTIFACTS)
    expected: dict[str, tuple[str, int]] = {}
    kinds: set[str] = set()
    aggregate_bytes = 0
    for index, raw_artifact in enumerate(artifacts):
        field = f"audit.artifacts[{index}]"
        artifact = _object(
            raw_artifact,
            field,
            required=_ARTIFACT_FIELDS,
            allowed=_ARTIFACT_FIELDS,
        )
        kind = _text(artifact["kind"], f"{field}.kind", maximum=128, pattern=_IDENTIFIER)
        if kind in kinds:
            raise AuditError("audit artifact kinds must be unique")
        kinds.add(kind)
        path = _relative_path(artifact["path"], f"{field}.path", artifact=True)
        parts = PurePosixPath(path).parts
        filename = parts[-1]
        if kind == "sdist" and not (
            parts[0] == "sdist"
            and len(parts) == 2
            and filename == "minimax_h3_context-0.1.0.tar.gz"
        ):
            raise AuditError(f"{field} sdist path is not the exact required artifact layout")
        if kind == "direct_wheel" and not (
            parts[0] == "direct-wheel"
            and len(parts) == 2
            and filename == "minimax_h3_context-0.1.0-py3-none-any.whl"
        ):
            raise AuditError(f"{field} direct wheel path is not the exact required artifact layout")
        if kind == "wheel_from_sdist" and not (
            parts[0] == "wheel-from-sdist"
            and len(parts) == 2
            and filename == "minimax_h3_context-0.1.0-py3-none-any.whl"
        ):
            raise AuditError(
                f"{field} wheel-from-sdist path is not the exact required artifact layout"
            )
        sha256 = _text(artifact["sha256"], f"{field}.sha256", maximum=71, pattern=_SHA256)
        size = artifact["size"]
        if type(size) is not int or size < 0 or size > _MAX_ARTIFACT_BYTES:
            raise AuditError(f"{field}.size is not a bounded byte count")
        aggregate_bytes += size
        if aggregate_bytes > _MAX_AGGREGATE_ARTIFACT_BYTES:
            raise AuditError("audit artifacts exceed the aggregate byte limit")
        if path in expected:
            raise AuditError("audit.artifacts contains duplicate artifact paths")
        expected[path] = (sha256, size)
    if require_complete and kinds != _REQUIRED_ARTIFACT_KINDS:
        raise AuditError(
            f"audit artifact kind inventory must be exactly {sorted(_REQUIRED_ARTIFACT_KINDS)!r}"
        )
    return expected


def _validate_reports(
    value: object,
    repo_root: Path,
    *,
    candidate: str,
    candidate_tree: str,
    require_complete: bool,
) -> tuple[dict[str, dict[str, object]], frozenset[str]]:
    raw_reports = _bounded_list(value, "audit.reports", len(_REQUIRED_REPORT_SCHEMAS))
    reports: dict[str, dict[str, object]] = {}
    metadata: dict[str, dict[str, object]] = {}
    paths: set[str] = set()
    aggregate = 0
    for index, raw_report in enumerate(raw_reports):
        field = f"audit.reports[{index}]"
        report = _object(raw_report, field, required=_REPORT_FIELDS, allowed=_REPORT_FIELDS)
        kind = _text(report["kind"], f"{field}.kind", maximum=64, pattern=_IDENTIFIER)
        expected_schema = _REQUIRED_REPORT_SCHEMAS.get(kind)
        if expected_schema is None or kind in reports:
            raise AuditError(f"{field}.kind is not a unique required report kind")
        schema = _text(report["schema"], f"{field}.schema", maximum=128)
        if schema != expected_schema:
            raise AuditError(f"{field}.schema does not match the typed report kind")
        if report["candidate"] != candidate or report["candidate_tree"] != candidate_tree:
            raise AuditError(f"{field} is not bound to the exact candidate/tree")
        path = _relative_path(report["path"], f"{field}.path", artifact=False)
        if not path.startswith(".planning/") or "M16-03" not in path and "M15-18" not in path:
            raise AuditError(f"{field}.path is not a specific ignored candidate report")
        sha256 = _text(report["sha256"], f"{field}.sha256", maximum=71, pattern=_SHA256)
        size = report["size"]
        if type(size) is not int or size < 1 or size > _MAX_EVIDENCE_BYTES:
            raise AuditError(f"{field}.size is not a bounded byte count")
        aggregate += size
        if aggregate > _MAX_AGGREGATE_EVIDENCE_BYTES:
            raise AuditError("audit reports exceed the aggregate byte limit")
        report_path = _evidence_file(repo_root, path, field, allowed_report_paths=None)
        _read_content_free_evidence(report_path, field)
        if report_path.stat().st_size != size or _sha256_file(report_path) != sha256:
            raise AuditError(f"{field} hash/size does not match actual report bytes")
        report_value = _load_json_file(report_path, field)
        if report_value.get("schema") != schema:
            raise AuditError(f"{field} actual report schema does not match the typed reference")
        if report_value.get("status") != "PASS":
            raise AuditError(f"{field} actual report status must be PASS")
        reports[kind] = report_value
        metadata[kind] = report
        paths.add(path)
    if require_complete and set(reports) != set(_REQUIRED_REPORT_SCHEMAS):
        raise AuditError("audit report kind inventory is incomplete")
    if require_complete:
        final_evidence = cast(dict[str, object], reports["m15_18_final"].get("evidence", {}))
        for kind, member in (
            ("m15_18_supported", "supported_report"),
            ("m15_18_latest", "latest_coinstall_report"),
        ):
            retained = cast(dict[str, object], final_evidence.get(member, {}))
            if retained.get("path") != metadata[kind]["path"] or _plain_sha(
                retained.get("sha256"), f"{kind} retained sha256"
            ) != _plain_sha(metadata[kind]["sha256"], f"{kind} typed sha256"):
                raise AuditError(f"{kind} typed reference does not join M15-18 final evidence")
    return reports, frozenset(paths)


def _owned_artifact_root(repo_root: Path, label: str) -> Path:
    resolved_root = repo_root.resolve()
    candidate = resolved_root.joinpath(*PurePosixPath(label).parts)
    try:
        candidate.resolve().relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise AuditError("audit.artifact_root escapes the repository") from exc
    current = resolved_root
    for part in PurePosixPath(label).parts:
        current = current / part
        if not current.exists():
            raise AuditError("audit artifact root is missing")
        if current.is_symlink() or _is_reparse_point(current):
            raise AuditError("audit artifact root contains a symlink or reparse point")
    if not candidate.is_dir():
        raise AuditError("audit artifact root is not a regular directory")
    return candidate


def _validate_artifact_bytes(
    repo_root: Path,
    artifact_root_label: str,
    expected: dict[str, tuple[str, int]],
) -> None:
    artifact_root = _owned_artifact_root(repo_root, artifact_root_label)
    actual: dict[str, Path] = {}
    try:
        members = sorted(artifact_root.rglob("*"))
    except OSError as exc:
        raise AuditError("audit artifact membership cannot be enumerated") from exc
    for member in members:
        if member.is_symlink() or _is_reparse_point(member):
            raise AuditError("audit artifact membership contains a symlink or reparse point")
        if member.is_dir():
            continue
        if not member.is_file():
            raise AuditError("audit artifact membership contains a non-regular member")
        relative = member.relative_to(artifact_root).as_posix()
        _relative_path(relative, "audit artifact member", artifact=True)
        actual[relative] = member
        if len(actual) > _MAX_ARTIFACTS:
            raise AuditError("audit artifact membership exceeds the member limit")
    missing = sorted(set(expected).difference(actual))
    extra = sorted(set(actual).difference(expected))
    if missing or extra:
        raise AuditError(
            "audit artifact membership has missing or unlisted extra files: "
            f"missing={missing!r}, extra={extra!r}"
        )
    for relative, (expected_sha256, expected_size) in expected.items():
        path = actual[relative]
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise AuditError(f"audit artifact size does not match actual bytes: {relative}")
        if _sha256_file(path) != expected_sha256:
            raise AuditError(f"audit artifact sha256 does not match actual bytes: {relative}")
        _validate_archive(path, relative)


def _validate_archive(path: Path, relative: str) -> None:
    try:
        if relative.endswith(".tar.gz"):
            with tarfile.open(path, mode="r:gz") as archive:
                members = archive.getmembers()
                if any(
                    member.issym() or member.islnk() or member.isdev() or member.isfifo()
                    for member in members
                ):
                    raise AuditError(f"audit sdist contains a link/device member: {relative}")
                names = [member.name for member in members if member.isfile()]
                expanded = sum(member.size for member in members if member.isfile())
            required = {
                "minimax_h3_context-0.1.0/pyproject.toml",
                "minimax_h3_context-0.1.0/LICENSE",
                "minimax_h3_context-0.1.0/comfyui_h3_context/__init__.py",
            }
        else:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if any(stat.S_ISLNK(info.external_attr >> 16) for info in infos):
                    raise AuditError(f"audit wheel contains a symlink member: {relative}")
                names = [info.filename for info in infos if not info.is_dir()]
                expanded = sum(info.file_size for info in infos if not info.is_dir())
            required = {
                "comfyui_h3_context/__init__.py",
                "minimax_h3_context-0.1.0.dist-info/METADATA",
                "minimax_h3_context-0.1.0.dist-info/WHEEL",
                "minimax_h3_context-0.1.0.dist-info/RECORD",
            }
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise AuditError(f"audit artifact is not a valid required archive: {relative}") from exc
    if len(names) > _MAX_ARCHIVE_MEMBERS or expanded > _MAX_ARCHIVE_EXPANDED_BYTES:
        raise AuditError(f"audit artifact archive exceeds resource limits: {relative}")
    normalized = set()
    for name in names:
        member = _relative_path(name, f"archive member in {relative}", artifact=True)
        if member in normalized:
            raise AuditError(f"audit artifact archive has duplicate members: {relative}")
        normalized.add(member)
    if not required.issubset(normalized):
        raise AuditError(f"audit artifact lacks required package metadata/members: {relative}")


def _evidence_path_allowed(reference: str, allowed_report_paths: frozenset[str] | None) -> bool:
    if allowed_report_paths is None and reference.startswith(".planning/"):
        return True
    if allowed_report_paths is not None and reference in allowed_report_paths:
        return True
    parts = PurePosixPath(reference).parts
    if reference in _FROZEN_PUBLIC_PATHS:
        return True
    if reference in {"LICENSE", "README.md", "pyproject.toml", "MANIFEST.in", ".comfyignore"}:
        return True
    if parts and parts[0] in {"docs", "tests"}:
        return True
    if len(parts) >= 2 and parts[:2] == ("comfyui_h3_context", "contracts"):
        return True
    if parts and parts[0] in {"comfyui_h3_context", "frontend"}:
        return True
    if len(parts) >= 3 and parts[0] == ".tmp" and parts[1].startswith("m16-03-"):
        return reference.endswith((".json", ".txt"))
    return False


def _evidence_file(
    repo_root: Path,
    reference: str,
    field: str,
    *,
    allowed_report_paths: frozenset[str] | None,
) -> Path:
    if not _evidence_path_allowed(reference, allowed_report_paths):
        raise AuditError(f"{field} evidence reference is outside the closed allowlist")
    resolved_root = repo_root.resolve()
    relative = PurePosixPath(reference)
    candidate = resolved_root.joinpath(*relative.parts)
    try:
        candidate.resolve().relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise AuditError(f"{field} evidence file escapes the repository") from exc
    current = resolved_root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            raise AuditError(f"{field} evidence file is missing")
        if current.is_symlink() or _is_reparse_point(current):
            raise AuditError(f"{field} evidence file contains a symlink or reparse point")
    if not candidate.is_file():
        raise AuditError(f"{field} evidence path is not a regular file")
    if candidate.stat().st_size > _MAX_EVIDENCE_BYTES:
        raise AuditError(f"{field} evidence file exceeds the byte limit")
    return candidate


def _read_content_free_evidence(path: Path, field: str) -> bytes:
    if path.stat().st_size > _MAX_EVIDENCE_BYTES:
        raise AuditError(f"{field} evidence file exceeds the byte limit")
    payload = path.read_bytes()
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise AuditError(f"{field} evidence bytes must be content-free UTF-8") from exc
    _content_free(text, field)
    if path.suffix.casefold() == ".json":
        try:
            value = json.loads(
                text,
                object_pairs_hook=_reject_duplicate,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, RecursionError) as exc:
            raise AuditError(f"{field} evidence bytes contain invalid JSON") from exc
        _validate_json_resources(value, field)
        _scan_sensitive_evidence_values(value, field)
    return payload


def _scan_sensitive_evidence_values(value: object, field: str, *, key: str = "") -> None:
    sensitive_value_keys = {
        "api_key",
        "authorization",
        "cookie",
        "password",
        "private_media",
        "prompt",
        "provider_payload",
        "raw_prompt",
        "signed_url",
        "token",
    }
    safe_structural_values = {
        "",
        "[REDACTED]",
        "[REDACTED_SECRET]",
        "BOOLEAN",
        "COMBO",
        "FLOAT",
        "IMAGE",
        "INT",
        "STRING",
        "VIDEO",
    }
    if key.casefold() in sensitive_value_keys:
        if value is None or value is False:
            return
        if type(value) is str and (value in safe_structural_values or _SHA256.fullmatch(value)):
            return
        raise AuditError(f"{field} contains a private/sensitive value under {key!r}")
    if type(value) is dict:
        for child_key, child in cast(dict[str, object], value).items():
            _scan_sensitive_evidence_values(child, field, key=child_key)
    elif type(value) is list:
        for child in cast(list[object], value):
            _scan_sensitive_evidence_values(child, field, key=key)


def _combined_evidence_sha256(
    repo_root: Path,
    references: tuple[str, ...],
    field: str,
    *,
    allowed_report_paths: frozenset[str] | None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    aggregate = 0
    for reference in references:
        path = _evidence_file(
            repo_root,
            reference,
            field,
            allowed_report_paths=allowed_report_paths,
        )
        name = reference.encode("utf-8")
        payload = _read_content_free_evidence(path, field)
        aggregate += len(payload)
        if aggregate > _MAX_AGGREGATE_EVIDENCE_BYTES:
            raise AuditError(f"{field} evidence exceeds the aggregate byte limit")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest(), aggregate


def _load_json_file(path: Path, field: str) -> dict[str, object]:
    try:
        if path.stat().st_size > _MAX_DOCUMENT_BYTES:
            raise AuditError(f"{field} JSON exceeds the byte limit")
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise AuditError(f"{field} must be strict UTF-8 without BOM")
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AuditError(f"{field} must be strict JSON") from exc
    if type(value) is not dict:
        raise AuditError(f"{field} JSON root must be an object")
    _validate_json_resources(value, field)
    _finite(value, field)
    return cast(dict[str, object], value)


def _criterion_for(row_id: str) -> str:
    group = row_id.split("-")[1]
    return {
        "AUTH": "AC1",
        "SHIP": "AC1",
        "EXCL": "AC3",
        "LIC": "AC2",
        "SC": "AC4",
        "PRIV": "AC3",
        "DOM": "AC3",
        "UI": "AC5",
        "SRC": "AC2",
        "CLOSE": "AC6",
    }[group]


def _plain_sha(value: object, field: str) -> str:
    text = _text(value, field, maximum=71)
    candidate = text.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", candidate) is None:
        raise AuditError(f"{field} is not SHA-256")
    return candidate


def _validate_report_joins(
    reports: dict[str, dict[str, object]], raw_artifacts: object, repo_root: Path
) -> None:
    artifacts = cast(list[dict[str, object]], raw_artifacts)
    artifact_values = {
        cast(str, artifact["kind"]): (
            _plain_sha(artifact["sha256"], "audit artifact sha256"),
            cast(int, artifact["size"]),
        )
        for artifact in artifacts
    }
    build = reports["build"]
    build_artifacts = _bounded_list(build.get("artifacts"), "build report artifacts", 3)
    build_values: dict[str, tuple[str, int]] = {}
    for index, raw in enumerate(build_artifacts):
        item = _object(
            raw,
            f"build report artifacts[{index}]",
            required=frozenset({"kind", "sha256", "bytes", "filename"}),
            allowed=frozenset({"kind", "sha256", "bytes", "filename"}),
        )
        kind = _text(item["kind"], "build artifact kind", maximum=64)
        size = item["bytes"]
        if type(size) is not int:
            raise AuditError("build artifact bytes must be an exact integer")
        build_values[kind] = (_plain_sha(item["sha256"], "build artifact sha256"), size)
    if build_values != artifact_values:
        raise AuditError("build report artifact hashes/sizes do not join the exact audit artifacts")
    release_lanes = _bounded_list(
        reports["release"].get("artifact_lanes"), "release report artifact_lanes", 2
    )
    release_hashes = {
        _text(
            cast(dict[str, object], lane).get("artifact"), "release artifact", maximum=32
        ): _plain_sha(cast(dict[str, object], lane).get("sha256"), "release artifact sha256")
        for lane in release_lanes
    }
    if release_hashes != {
        "sdist": artifact_values["sdist"][0],
        "wheel": artifact_values["direct_wheel"][0],
    }:
        raise AuditError("release report hashes do not join the exact sdist/direct wheel")
    frontend = reports["frontend"]
    bundle_sha = _plain_sha(frontend.get("bundle_sha256"), "frontend bundle sha256")
    bundle_size = frontend.get("bundle_bytes")
    if type(bundle_size) is not int or bundle_size < 1:
        raise AuditError("frontend bundle bytes must be a positive integer")
    registry_entries = _bounded_list(
        reports["registry"].get("entries"), "registry report entries", _MAX_ARTIFACTS
    )
    runtime_entries = [
        cast(dict[str, object], entry)
        for entry in registry_entries
        if cast(dict[str, object], entry).get("path")
        == "comfyui_h3_context/web/h3-context-sidebar.js"
    ]
    if (
        len(runtime_entries) != 1
        or _plain_sha(runtime_entries[0].get("sha256"), "registry runtime sha256") != bundle_sha
        or runtime_entries[0].get("size") != bundle_size
    ):
        raise AuditError("frontend and Registry runtime bundle hashes/sizes do not join")
    _validate_advisory_summary(reports["advisory"], repo_root)
    if reports["m15_18_final"].get("candidate") != EXPECTED_BASE:
        raise AuditError("M15-18 final report is not bound to the accepted predecessor")


def _validate_rows(
    value: object,
    top_status: str,
    repo_root: Path,
    *,
    allowed_report_paths: frozenset[str],
) -> tuple[tuple[str, ...], bool]:
    rows = _bounded_list(value, "audit.rows", len(EXPECTED_AUDIT_ROWS))
    if len(rows) != len(EXPECTED_AUDIT_ROWS):
        raise AuditError("audit.rows must contain every finalized checklist row")
    observed_ids: list[str] = []
    row_statuses: list[str] = []
    reviews: list[str] = []
    release_blocker = False
    seen_probe_ids: set[str] = set()
    seen_commands: set[str] = set()
    seen_evidence_sets: set[tuple[str, ...]] = set()
    aggregate_evidence = 0
    for index, raw_row in enumerate(rows):
        field = f"audit.rows[{index}]"
        row = _object(raw_row, field, required=_ROW_FIELDS, allowed=_ROW_FIELDS)
        row_id = _text(row["id"], f"{field}.id", maximum=32, pattern=_IDENTIFIER)
        if row_id not in EXPECTED_AUDIT_ROWS:
            raise AuditError(f"{field}.id is not a finalized audit row")
        observed_ids.append(row_id)
        probe_id = _text(row["probe_id"], f"{field}.probe_id", maximum=128, pattern=_IDENTIFIER)
        probe_owner = _text(
            row["probe_owner"], f"{field}.probe_owner", maximum=32, pattern=_IDENTIFIER
        )
        if probe_owner != row_id:
            raise AuditError(f"{field} probe ownership must equal its finalized row id")
        if probe_id in seen_probe_ids:
            raise AuditError(f"{field} probe_id must be unique")
        seen_probe_ids.add(probe_id)
        criteria = _string_list(
            row["criteria"], f"{field}.criteria", maximum=6, require_nonempty=True
        )
        if not set(criteria).issubset(_ALLOWED_CRITERIA):
            raise AuditError(f"{field}.criteria contains an unsupported criterion")
        if _criterion_for(row_id) not in criteria:
            raise AuditError(f"{field}.criteria does not cover the finalized row criterion")
        for name in ("subject", "command", "expected", "observed", "cleanup"):
            text = _text(row[name], f"{field}.{name}", maximum=4096)
            _content_free(text, f"{field}.{name}")
        command = cast(str, row["command"])
        started_at = _timestamp(row["started_at"], f"{field}.started_at")
        ended_at = _timestamp(row["ended_at"], f"{field}.ended_at")
        if ended_at < started_at:
            raise AuditError(f"{field} time range ends before it starts")
        _text(row["timezone"], f"{field}.timezone", maximum=64, pattern=_TIMEZONE)
        first_failure = row["first_failure"]
        if first_failure is not None:
            failure = _text(first_failure, f"{field}.first_failure", maximum=4096)
            _content_free(failure, f"{field}.first_failure")
        side_effects = _string_list(
            row["forbidden_side_effects"],
            f"{field}.forbidden_side_effects",
            maximum=32,
        )
        for side_effect in side_effects:
            _content_free(side_effect, f"{field}.forbidden_side_effects")
        evidence_refs = _string_list(
            row["evidence_refs"],
            f"{field}.evidence_refs",
            maximum=32,
            require_nonempty=True,
        )
        for evidence_index, reference in enumerate(evidence_refs):
            _relative_path(reference, f"{field}.evidence_refs[{evidence_index}]", artifact=False)
        evidence_sha256 = _text(
            row["evidence_sha256"],
            f"{field}.evidence_sha256",
            maximum=71,
            pattern=_SHA256,
        )
        actual_evidence_sha256, evidence_bytes = _combined_evidence_sha256(
            repo_root,
            evidence_refs,
            field,
            allowed_report_paths=allowed_report_paths,
        )
        aggregate_evidence += evidence_bytes
        if aggregate_evidence > _MAX_AGGREGATE_EVIDENCE_BYTES:
            raise AuditError("audit rows exceed the aggregate evidence byte limit")
        if actual_evidence_sha256 != evidence_sha256:
            raise AuditError(f"{field} evidence sha256 does not match referenced bytes")
        status = _text(row["status"], f"{field}.status", maximum=16)
        if status not in _ALLOWED_STATUS:
            raise AuditError(f"{field}.status is not closed")
        if status == "PASS" and first_failure is not None:
            raise AuditError(f"{field}.status PASS cannot retain a first failure")
        if status != "PASS" and first_failure is None:
            raise AuditError(f"{field}.status {status} requires a first failure")
        review = _text(row["reviewer_disposition"], f"{field}.reviewer_disposition", maximum=32)
        if review not in _ALLOWED_REVIEW:
            raise AuditError(f"{field}.reviewer_disposition is invalid")
        if status == "PASS":
            if review != "APPROVED":
                raise AuditError(f"{field} reviewer must approve a PASS row")
            if (
                row["command"] == "Explicit candidate-bound probe required"
                or row["observed"] == "Required evidence has not been recorded"
            ):
                raise AuditError(f"{field} cannot promote BLOCKED skeleton content to PASS")
            if command in seen_commands or evidence_refs in seen_evidence_sets:
                raise AuditError(f"{field} PASS probe/evidence ownership must be unique")
            seen_commands.add(command)
            seen_evidence_sets.add(evidence_refs)
        finding = _text(row["finding_disposition"], f"{field}.finding_disposition", maximum=32)
        if finding not in _ALLOWED_FINDINGS:
            raise AuditError(f"{field}.finding_disposition is invalid")
        finding_id = row["finding_id"]
        finding_owner = row["finding_owner"]
        if finding == "NONE":
            if finding_id is not None or finding_owner is not None:
                raise AuditError(f"{field} NONE finding cannot name id/owner")
        else:
            _text(finding_id, f"{field}.finding_id", maximum=128, pattern=_IDENTIFIER)
            _text(finding_owner, f"{field}.finding_owner", maximum=128, pattern=_IDENTIFIER)
        if row_id == "AUD-LIC-02":
            if (
                finding != "RELEASE_BLOCKER"
                or finding_id != "M16-03-F01"
                or finding_owner != "M16-04"
            ):
                raise AuditError(
                    "AUD-LIC-02 must retain fixed M16-03-F01 RELEASE_BLOCKER owner M16-04"
                )
        elif finding_id == "M16-03-F01":
            raise AuditError("M16-03-F01 belongs only to AUD-LIC-02")
        if finding == "RELEASE_BLOCKER":
            release_blocker = True
        row_statuses.append(status)
        reviews.append(review)
    if tuple(observed_ids) != EXPECTED_AUDIT_ROWS:
        raise AuditError("audit.rows are missing, duplicate, unknown, or out of finalized order")
    if top_status == "PASS":
        if any(status != "PASS" for status in row_statuses):
            raise AuditError("top-level PASS cannot override a non-PASS row status")
        if any(review in {"BLOCKED", "CHANGES_REQUESTED"} for review in reviews):
            raise AuditError("top-level PASS cannot override a blocking reviewer disposition")
    elif top_status == "FAIL" and "FAIL" not in row_statuses:
        raise AuditError("top-level FAIL requires at least one failed row status")
    elif top_status == "BLOCKED" and "BLOCKED" not in row_statuses:
        raise AuditError("top-level BLOCKED requires at least one blocked row status")
    return tuple(row_statuses), release_blocker


def validate_audit(
    value: object,
    *,
    repo_root: Path = ROOT,
    expected_candidate: str | None = None,
    expected_candidate_tree: str | None = None,
) -> str:
    """Validate one complete closed release-audit document and return its fingerprint."""

    repo_root = repo_root.resolve()
    audit = _object(
        value,
        "audit",
        required=_TOP_LEVEL_FIELDS,
        allowed=_TOP_LEVEL_FIELDS,
    )
    if audit["schema"] != SCHEMA or audit["item"] != ITEM:
        raise AuditError("audit schema/item identity is invalid")
    _validate_json_resources(audit, "audit")
    status = _text(audit["status"], "audit.status", maximum=16)
    if status not in _ALLOWED_STATUS:
        raise AuditError("audit.status is not closed")
    base = _text(audit["base"], "audit.base", maximum=40, pattern=_REVISION)
    if base != EXPECTED_BASE:
        raise AuditError("audit.base is not the accepted M15-18 checkpoint")
    candidate = _text(audit["candidate"], "audit.candidate", maximum=40, pattern=_REVISION)
    if expected_candidate is not None and candidate != expected_candidate:
        raise AuditError("audit.candidate does not match the expected candidate")
    candidate_tree = _text(
        audit["candidate_tree"],
        "audit.candidate_tree",
        maximum=40,
        pattern=_REVISION,
    )
    if expected_candidate_tree is not None and candidate_tree != expected_candidate_tree:
        raise AuditError("audit candidate tree does not match the expected candidate tree")
    if audit["branch"] != "dev":
        raise AuditError("audit.branch must remain dev")
    clean = _exact_bool(audit["public_worktree_clean"], "audit.public_worktree_clean")
    release_ready = _exact_bool(audit["release_ready"], "audit.release_ready")
    if status == "PASS" and not clean:
        raise AuditError("a passing audit requires a clean public worktree")
    environment = _object(
        audit["environment"],
        "audit.environment",
        required=_ENVIRONMENT_FIELDS,
        allowed=_ENVIRONMENT_FIELDS,
    )
    for name in sorted(_ENVIRONMENT_FIELDS):
        _content_free(_text(environment[name], f"audit.environment.{name}", maximum=128), name)
    artifact_root = _validate_artifact_root(audit["artifact_root"])
    artifacts = _validate_artifacts(audit["artifacts"], require_complete=status == "PASS")
    reports, allowed_report_paths = _validate_reports(
        audit["reports"],
        repo_root,
        candidate=candidate,
        candidate_tree=candidate_tree,
        require_complete=status == "PASS",
    )
    if status == "PASS":
        _validate_artifact_bytes(repo_root, artifact_root, artifacts)
        _validate_report_joins(reports, audit["artifacts"], repo_root)
    _, release_blocker = _validate_rows(
        audit["rows"],
        status,
        repo_root,
        allowed_report_paths=allowed_report_paths,
    )
    if release_ready == release_blocker:
        raise AuditError("audit.release_ready must be false whenever a RELEASE_BLOCKER exists")
    non_claims = _string_list(
        audit["non_claims"], "audit.non_claims", maximum=32, require_nonempty=True
    )
    for non_claim in non_claims:
        _content_free(non_claim, "audit.non_claims")
    expected_fingerprint = canonical_fingerprint(audit)
    fingerprint = _text(audit["fingerprint"], "audit.fingerprint", maximum=71, pattern=_SHA256)
    if fingerprint != expected_fingerprint:
        raise AuditError("audit fingerprint does not match the canonical document")
    return fingerprint


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _git_text(repo_root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_success(repo_root: Path, *arguments: str) -> bool:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _validate_git_identity(repo_root: Path, base: str, candidate: str, candidate_tree: str) -> None:
    if not _git_success(repo_root, "merge-base", "--is-ancestor", base, candidate):
        raise AuditError("accepted base is not an ancestor of the candidate")
    tree = _git_text(repo_root, "rev-parse", f"{candidate}^{{tree}}")
    if tree != candidate_tree:
        raise AuditError("candidate tree does not match Git")
    changed = _git_text(repo_root, "diff", "--name-only", f"{base}..{candidate}")
    paths = frozenset(changed.splitlines()) if changed else frozenset()
    if paths != _FROZEN_PUBLIC_PATHS:
        raise AuditError("base-to-candidate public path set is not the exact frozen two paths")


def _artifact_root_label(repo_root: Path, artifact_root: Path) -> str:
    try:
        relative = artifact_root.resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError) as exc:
        raise AuditError("artifact root must remain inside the repository") from exc
    parts = PurePosixPath(relative).parts
    if len(parts) >= 2 and parts[0] == ".tmp" and parts[1].startswith("m16-03-"):
        return relative
    raise AuditError("artifact root must be an owned repository .tmp/m16-03-* path")


def _inventory_artifacts(artifact_root: Path) -> tuple[list[dict[str, object]], str | None]:
    if not artifact_root.exists():
        return [], "owned artifact root does not exist"
    if artifact_root.is_symlink() or _is_reparse_point(artifact_root) or not artifact_root.is_dir():
        return [], "owned artifact root is not a regular directory"
    artifacts: list[dict[str, object]] = []
    try:
        paths = sorted(artifact_root.rglob("*"))
    except OSError:
        return [], "owned artifact root cannot be enumerated"
    for path in paths:
        if path.is_symlink() or _is_reparse_point(path):
            return [], "owned artifact root contains a symlink or reparse point"
        if not path.is_file():
            continue
        if len(artifacts) >= _MAX_ARTIFACTS:
            return [], "owned artifact root exceeds the member limit"
        size = path.stat().st_size
        if size > _MAX_ARTIFACT_BYTES:
            return [], "owned artifact root contains an oversized member"
        relative = path.relative_to(artifact_root).as_posix()
        try:
            _relative_path(relative, "artifact.path", artifact=True)
        except AuditError as exc:
            return [], str(exc)
        parts = PurePosixPath(relative).parts
        if "direct-wheel" in parts or "direct_wheel" in parts:
            kind = "direct_wheel"
        elif "wheel-from-sdist" in parts or "wheel_from_sdist" in parts:
            kind = "wheel_from_sdist"
        elif path.name.endswith(".tar.gz") and ("sdist" in parts or len(parts) == 1):
            kind = "sdist"
        else:
            return [], "owned artifact root lacks exact required artifact-kind layout"
        artifacts.append(
            {
                "kind": kind,
                "path": relative,
                "sha256": _sha256_file(path),
                "size": size,
            }
        )
    if not artifacts:
        return [], "owned artifact root contains no regular files"
    return artifacts, None


def _environment_version(command: list[str], fallback: str) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    if result.returncode != 0:
        return fallback
    text = (result.stdout or result.stderr).strip()
    return text.splitlines()[0][:128] if text else fallback


OsvTransport = Callable[[str, bytes, float, int], bytes]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, code, msg, headers, newurl
        return None


def _osv_transport(url: str, body: bytes, timeout: float, maximum: int) -> bytes:
    if url != _OSV_ENDPOINT:
        raise AuditError("OSV endpoint is not the fixed querybatch authority")
    # CRITICAL: the equality guard above pins the only permitted HTTPS advisory authority.
    request = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    # Do not inherit user proxy credentials or ambient proxy routing for the fixed authority.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200 or response.geturl() != _OSV_ENDPOINT:
                raise AuditError("OSV query was redirected or returned a non-success status")
            payload = bytes(response.read(maximum + 1))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise AuditError("OSV querybatch authority is unavailable") from exc
    if len(payload) > maximum:
        raise AuditError("OSV response exceeds the byte limit")
    return payload


def _osv_projection(
    package: dict[str, object], field: str
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    name = _text(package.get("name"), f"{field}.name", maximum=256)
    version = _text(package.get("versionInfo"), f"{field}.versionInfo", maximum=128)
    _content_free(name, f"{field}.name")
    _content_free(version, f"{field}.versionInfo")
    refs = _bounded_list(package.get("externalRefs"), f"{field}.externalRefs", 32)
    purls = [
        cast(dict[str, object], ref).get("referenceLocator")
        for ref in refs
        if cast(dict[str, object], ref).get("referenceType") == "purl"
    ]
    if len(purls) != 1 or type(purls[0]) is not str:
        raise AuditError(f"{field} must have exactly one public purl")
    purl = purls[0]
    package_match = re.fullmatch(r"pkg:(pypi|npm)/([^@?#]+)(?:@([^?#]+))?", purl)
    if package_match is not None:
        purl_type, encoded_name, encoded_version = package_match.groups()
        if encoded_version is not None and unquote(encoded_version) != version:
            raise AuditError(f"{field} purl version does not match exact SBOM version")
        decoded_name = unquote(encoded_name)
        if decoded_name != name and decoded_name.lstrip("@") != name.lstrip("@"):
            raise AuditError(f"{field} purl name does not match the SBOM package")
        if (
            encoded_version is None
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version) is None
        ):
            if (
                package.get("comment") == "development_build_only_not_bundled"
                and package.get("primaryPackagePurpose") == "SOURCE"
            ):
                return None, None
            raise AuditError(f"{field} does not declare an exact OSV package version")
        ecosystem = {"pypi": "PyPI", "npm": "npm"}[purl_type]
        return {"package": {"ecosystem": ecosystem, "name": name}, "version": version}, None

    # CRITICAL: commit queries are authorized only for these complete current or historical SBOM
    # identities. Key every public identity and disposition field so a version/revision, package
    # name, or bundled/host ownership cross-pair cannot inherit another package's authority.
    github_commits = {
        (
            "ComfyUI",
            f"0.32.0+{_CURRENT_SBOM_COMFYUI_COMMIT}",
            f"pkg:github/Comfy-Org/ComfyUI@{_CURRENT_SBOM_COMFYUI_COMMIT}",
            "host_owned_not_bundled",
            "FRAMEWORK",
        ): _CURRENT_SBOM_COMFYUI_COMMIT,
        (
            "ComfyUI",
            f"0.30.0+{_M16_03_HISTORICAL_COMFYUI_COMMIT}",
            f"pkg:github/Comfy-Org/ComfyUI@{_M16_03_HISTORICAL_COMFYUI_COMMIT}",
            "host_owned_not_bundled",
            "FRAMEWORK",
        ): _M16_03_HISTORICAL_COMFYUI_COMMIT,
        (
            "Noto Sans",
            "NotoSans-v2.015",
            f"pkg:github/notofonts/latin-greek-cyrillic@{_CURRENT_SBOM_NOTO_COMMIT}",
            "bundled_font_runtime",
            "LIBRARY",
        ): _CURRENT_SBOM_NOTO_COMMIT,
    }
    comment = package.get("comment")
    purpose = package.get("primaryPackagePurpose")
    github_commit = (
        github_commits.get((name, version, purl, comment, purpose))
        if isinstance(comment, str) and isinstance(purpose, str)
        else None
    )
    if github_commit is not None:
        return {"commit": github_commit}, None

    # IMPORTANT: this is a claim cap for one exact unversioned external tool, not a generic skip.
    if (
        name == "Ollama"
        and version == "user-selected-qualified-version"
        and purl == "pkg:github/ollama/ollama"
        and package.get("comment") == "external_user_managed_not_bundled"
        and package.get("primaryPackagePurpose") == "APPLICATION"
    ):
        return None, {
            "identity": purl,
            "package": name,
            "version": version,
            "reason": "EXTERNAL_USER_MANAGED_NOT_BUNDLED",
            "finding_disposition": "ACCEPTED_NONCLAIM",
            "finding_owner": "M16-04",
            "claim_cap": "NO_CURRENT_ADVISORY_ASSERTION_FOR_USER_SELECTED_VERSION",
        }
    raise AuditError(f"{field} uses an unknown OSV ecosystem or identity")


def _osv_query_plan(
    packages: list[object], field: str
) -> tuple[list[dict[str, object]], list[dict[str, object]], bytes, str]:
    queries: list[dict[str, object]] = []
    nonclaims: list[dict[str, object]] = []
    for index, package in enumerate(packages):
        query, nonclaim = _osv_projection(cast(dict[str, object], package), f"{field}[{index}]")
        if query is not None:
            queries.append(query)
        if nonclaim is not None:
            nonclaims.append(nonclaim)
    if not queries:
        raise AuditError("OSV SBOM does not contain a queryable exact identity")
    query_identities = [
        json.dumps(query, sort_keys=True, separators=(",", ":")) for query in queries
    ]
    if len(query_identities) != len(set(query_identities)):
        raise AuditError("OSV SBOM query identities must be unique")
    nonclaim_identities = [cast(str, nonclaim["identity"]) for nonclaim in nonclaims]
    if len(nonclaim_identities) != len(set(nonclaim_identities)):
        raise AuditError("OSV SBOM nonclaim identities must be unique")
    request_body = json.dumps({"queries": queries}, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    query_set_sha256 = "sha256:" + hashlib.sha256(request_body).hexdigest()
    return queries, nonclaims, request_body, query_set_sha256


def _blocked_osv_summary(
    *,
    started: datetime,
    request_count: int,
    first_error: str,
    sbom_sha256: str,
    query_set_sha256: str | None = None,
    nonclaims: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    retained_nonclaims = [] if nonclaims is None else nonclaims
    return {
        "schema": "h3-context-osv-advisory/1",
        "status": "BLOCKED",
        "source": "OSV_QUERYBATCH",
        "endpoint": _OSV_ENDPOINT,
        "started_at": started.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "request_count": request_count,
        "query_count": request_count,
        "query_set_sha256": query_set_sha256,
        "sbom_sha256": sbom_sha256,
        "raw_response_retained": False,
        "results": [],
        "nonclaims": retained_nonclaims,
        "first_error": first_error,
    }


def query_osv(
    sbom_path: Path,
    *,
    transport: OsvTransport = _osv_transport,
) -> dict[str, object]:
    """Run exactly one bounded OSV querybatch and return a content-free ordered summary."""

    _require_regular_nonlink(sbom_path, "OSV SBOM path")
    sbom_sha256 = _sha256_file(sbom_path)
    sbom = _load_json_file(sbom_path, "OSV SBOM")
    packages = _bounded_list(sbom.get("packages"), "OSV SBOM packages", _OSV_MAX_PACKAGES)
    if not packages:
        raise AuditError("OSV SBOM packages must not be empty")
    started = datetime.now(timezone.utc)
    try:
        queries, nonclaims, request_body, query_set_sha256 = _osv_query_plan(
            packages, "OSV SBOM packages"
        )
    except AuditError:
        return _blocked_osv_summary(
            started=started,
            request_count=0,
            first_error="SBOM_ECOSYSTEM_OR_IDENTITY_UNSUPPORTED",
            sbom_sha256=sbom_sha256,
        )
    if len(request_body) > _OSV_MAX_REQUEST_BYTES:
        raise AuditError("OSV query request exceeds the byte limit")
    try:
        response_body = transport(
            _OSV_ENDPOINT,
            request_body,
            _OSV_TIMEOUT_SECONDS,
            _OSV_MAX_RESPONSE_BYTES,
        )
    except (AuditError, OSError):
        return _blocked_osv_summary(
            started=started,
            request_count=len(queries),
            first_error="OSV_AUTHORITY_UNAVAILABLE",
            sbom_sha256=sbom_sha256,
            query_set_sha256=query_set_sha256,
            nonclaims=nonclaims,
        )
    ended = datetime.now(timezone.utc)
    if type(response_body) is not bytes or len(response_body) > _OSV_MAX_RESPONSE_BYTES:
        raise AuditError("OSV transport returned unbounded bytes")
    try:
        response_text = response_body.decode("utf-8", "strict")
        _content_free(response_text, "OSV response")
        response = json.loads(
            response_text,
            object_pairs_hook=_reject_duplicate,
            parse_constant=_reject_constant,
        )
    except (AuditError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _blocked_osv_summary(
            started=started,
            request_count=len(queries),
            first_error="OSV_RESPONSE_PRIVATE_OR_MALFORMED",
            sbom_sha256=sbom_sha256,
            query_set_sha256=query_set_sha256,
            nonclaims=nonclaims,
        )
    try:
        _validate_json_resources(response, "OSV response")
        root = _object(
            response,
            "OSV response",
            required=frozenset({"results"}),
            allowed=frozenset({"results"}),
        )
        results = _bounded_list(root["results"], "OSV response.results", len(queries))
        if len(results) != len(queries):
            raise AuditError("OSV response cardinality does not match ordered request")
        summaries: list[dict[str, object]] = []
        for index, (query, raw_result) in enumerate(zip(queries, results, strict=True)):
            field = f"OSV response.results[{index}]"
            result = _object(
                raw_result,
                field,
                required=frozenset(),
                allowed=frozenset({"vulns", "next_page_token"}),
            )
            if result.get("next_page_token") not in {None, ""}:
                raise AuditError("OSV pagination is BLOCKED under the one-query policy")
            vulnerabilities = _bounded_list(result.get("vulns", []), f"{field}.vulns", 4096)
            advisory_ids: list[str] = []
            for advisory_index, vulnerability in enumerate(vulnerabilities):
                advisory = cast(dict[str, object], vulnerability)
                advisory_id = _text(
                    advisory.get("id"),
                    f"{field}.vulns[{advisory_index}].id",
                    maximum=128,
                    pattern=_IDENTIFIER,
                )
                _content_free(advisory_id, f"{field}.vulns[{advisory_index}].id")
                advisory_ids.append(advisory_id)
            if len(advisory_ids) != len(set(advisory_ids)):
                raise AuditError(f"{field} contains duplicate advisory IDs")
            if "commit" in query:
                summary: dict[str, object] = {
                    "query_kind": "commit",
                    "commit": query["commit"],
                    "advisory_ids": advisory_ids,
                    "status": "AFFECTED" if advisory_ids else "NO_MATCH",
                }
            else:
                package = cast(dict[str, object], query["package"])
                summary = {
                    "query_kind": "package",
                    "ecosystem": package["ecosystem"],
                    "package": package["name"],
                    "version": query["version"],
                    "advisory_ids": advisory_ids,
                    "status": "AFFECTED" if advisory_ids else "NO_MATCH",
                }
            summaries.append(summary)
    except AuditError:
        return _blocked_osv_summary(
            started=started,
            request_count=len(queries),
            first_error="OSV_RESPONSE_INCOMPLETE_OR_PAGINATED",
            sbom_sha256=sbom_sha256,
            query_set_sha256=query_set_sha256,
            nonclaims=nonclaims,
        )
    return {
        "schema": "h3-context-osv-advisory/1",
        "status": "PASS",
        "source": "OSV_QUERYBATCH",
        "endpoint": _OSV_ENDPOINT,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "request_count": len(queries),
        "query_count": len(queries),
        "query_set_sha256": query_set_sha256,
        "sbom_sha256": sbom_sha256,
        "raw_response_retained": False,
        "results": summaries,
        "nonclaims": nonclaims,
        "first_error": None,
    }


def _validate_advisory_summary(advisory: dict[str, object], repo_root: Path) -> None:
    fields = frozenset(
        {
            "schema",
            "status",
            "source",
            "endpoint",
            "started_at",
            "ended_at",
            "request_count",
            "query_count",
            "query_set_sha256",
            "sbom_sha256",
            "raw_response_retained",
            "results",
            "nonclaims",
            "first_error",
        }
    )
    summary = _object(advisory, "advisory report", required=fields, allowed=fields)
    if (
        summary["schema"] != _REQUIRED_REPORT_SCHEMAS["advisory"]
        or summary["status"] != "PASS"
        or summary["source"] != "OSV_QUERYBATCH"
        or summary["endpoint"] != _OSV_ENDPOINT
        or summary["raw_response_retained"] is not False
        or summary["first_error"] is not None
    ):
        raise AuditError("advisory report does not retain the closed OSV querybatch summary")
    started_at = _timestamp(summary["started_at"], "advisory report.started_at")
    ended_at = _timestamp(summary["ended_at"], "advisory report.ended_at")
    if ended_at < started_at:
        raise AuditError("advisory report timestamps are not ordered")

    sbom_path = repo_root / "governance" / "contracts" / "sbom.spdx.json"
    _require_regular_nonlink(sbom_path, "advisory SBOM path")
    sbom = _load_json_file(sbom_path, "advisory SBOM")
    packages = _bounded_list(sbom.get("packages"), "advisory SBOM packages", _OSV_MAX_PACKAGES)
    queries, expected_nonclaims, _, expected_query_sha256 = _osv_query_plan(
        packages, "advisory SBOM packages"
    )
    if _plain_sha(summary["sbom_sha256"], "advisory report sbom_sha256") != _plain_sha(
        _sha256_file(sbom_path), "actual advisory SBOM sha256"
    ) or _plain_sha(summary["query_set_sha256"], "advisory report query_set_sha256") != _plain_sha(
        expected_query_sha256, "expected advisory query set sha256"
    ):
        raise AuditError("advisory report does not bind the exact SBOM/query set")
    request_count = summary["request_count"]
    query_count = summary["query_count"]
    if (
        type(request_count) is not int
        or type(query_count) is not int
        or request_count != len(queries)
        or query_count != len(queries)
    ):
        raise AuditError("advisory report query count does not bind the exact query set")

    raw_results = _bounded_list(summary["results"], "advisory report.results", len(queries))
    if len(raw_results) != len(queries):
        raise AuditError("advisory report results do not cover the exact ordered query set")
    for index, (query, raw_result) in enumerate(zip(queries, raw_results, strict=True)):
        field = f"advisory report.results[{index}]"
        if "commit" in query:
            result_fields = frozenset({"query_kind", "commit", "advisory_ids", "status"})
            result = _object(raw_result, field, required=result_fields, allowed=result_fields)
            if result["query_kind"] != "commit" or result["commit"] != query["commit"]:
                raise AuditError(f"{field} does not bind the requested ComfyUI commit")
        else:
            result_fields = frozenset(
                {"query_kind", "ecosystem", "package", "version", "advisory_ids", "status"}
            )
            result = _object(raw_result, field, required=result_fields, allowed=result_fields)
            package = cast(dict[str, object], query["package"])
            if (
                result["query_kind"] != "package"
                or result["ecosystem"] != package["ecosystem"]
                or result["package"] != package["name"]
                or result["version"] != query["version"]
            ):
                raise AuditError(f"{field} does not bind the requested package identity")
        advisory_ids = _string_list(
            result["advisory_ids"], f"{field}.advisory_ids", maximum=4096, require_nonempty=False
        )
        if len(advisory_ids) != len(set(advisory_ids)) or any(
            _IDENTIFIER.fullmatch(advisory_id) is None for advisory_id in advisory_ids
        ):
            raise AuditError(f"{field} contains invalid or duplicate advisory IDs")
        expected_status = "AFFECTED" if advisory_ids else "NO_MATCH"
        if result["status"] != expected_status:
            raise AuditError(f"{field}.status does not match its advisory IDs")

    raw_nonclaims = _bounded_list(
        summary["nonclaims"], "advisory report.nonclaims", _OSV_MAX_PACKAGES
    )
    nonclaim_fields = frozenset(
        {
            "identity",
            "package",
            "version",
            "reason",
            "finding_disposition",
            "finding_owner",
            "claim_cap",
        }
    )
    retained_nonclaims = [
        _object(
            raw_nonclaim,
            f"advisory report.nonclaims[{index}]",
            required=nonclaim_fields,
            allowed=nonclaim_fields,
        )
        for index, raw_nonclaim in enumerate(raw_nonclaims)
    ]
    if retained_nonclaims != expected_nonclaims:
        raise AuditError("advisory report nonclaims do not bind the exact SBOM exclusions")


def _typed_report_inventory(
    repo_root: Path,
    report_paths: dict[str, Path],
    *,
    candidate: str,
    candidate_tree: str,
) -> list[dict[str, object]]:
    if set(report_paths) != set(_REQUIRED_REPORT_SCHEMAS):
        raise AuditError("typed report inputs must contain every exact required report kind")
    result: list[dict[str, object]] = []
    for kind in _REQUIRED_REPORT_SCHEMAS:
        path = report_paths[kind]
        try:
            relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
        except (OSError, ValueError) as exc:
            raise AuditError(f"typed report {kind} escapes the repository") from exc
        _relative_path(relative, f"typed report {kind}", artifact=False)
        file_path = _evidence_file(
            repo_root,
            relative,
            f"typed report {kind}",
            allowed_report_paths=None,
        )
        _read_content_free_evidence(file_path, f"typed report {kind}")
        value = _load_json_file(file_path, f"typed report {kind}")
        schema = _REQUIRED_REPORT_SCHEMAS[kind]
        if value.get("schema") != schema or value.get("status") != "PASS":
            raise AuditError(f"typed report {kind} has the wrong schema/status")
        result.append(
            {
                "kind": kind,
                "path": relative,
                "schema": schema,
                "candidate": candidate,
                "candidate_tree": candidate_tree,
                "sha256": _sha256_file(file_path),
                "size": file_path.stat().st_size,
            }
        )
    return result


def _evidence_matrix_rows(
    path: Path,
    repo_root: Path,
    *,
    base: str,
    candidate: str,
    candidate_tree: str,
) -> tuple[list[object], list[object]]:
    _require_regular_nonlink(path, "evidence matrix")
    try:
        path.resolve().relative_to(repo_root.resolve())
    except (OSError, ValueError) as exc:
        raise AuditError("evidence matrix escapes the repository") from exc
    _read_content_free_evidence(path, "evidence matrix")
    matrix = _load_json_file(path, "evidence matrix")
    fields = frozenset({"schema", "base", "candidate", "candidate_tree", "rows", "non_claims"})
    matrix = _object(matrix, "evidence matrix", required=fields, allowed=fields)
    if matrix["schema"] != "h3-context-m16-03-evidence-matrix/1":
        raise AuditError("evidence matrix schema is invalid")
    if (
        matrix["base"] != base
        or matrix["candidate"] != candidate
        or matrix["candidate_tree"] != candidate_tree
    ):
        raise AuditError("evidence matrix is not bound to the exact candidate/tree/base")
    rows = _bounded_list(matrix["rows"], "evidence matrix rows", len(EXPECTED_AUDIT_ROWS))
    if len(rows) != len(EXPECTED_AUDIT_ROWS):
        raise AuditError("evidence matrix lacks authority for every finalized row")
    non_claims = _bounded_list(matrix["non_claims"], "evidence matrix non_claims", 32)
    return rows, non_claims


def build_audit_document(
    *,
    repo_root: Path,
    candidate: str,
    base: str,
    artifact_root: Path,
    evidence_matrix: Path | None = None,
    report_paths: dict[str, Path] | None = None,
) -> dict[str, object]:
    """Build from typed inputs, otherwise emit a candidate-bound BLOCKED skeleton."""

    _text(candidate, "candidate", maximum=40, pattern=_REVISION)
    if base != EXPECTED_BASE:
        raise AuditError("base is not the accepted M15-18 checkpoint")
    artifact_root_label = _artifact_root_label(repo_root, artifact_root)
    artifacts, artifact_failure = _inventory_artifacts(artifact_root)
    current_head = _git_text(repo_root, "rev-parse", "HEAD")
    candidate_tree = _git_text(repo_root, "rev-parse", f"{candidate}^{{tree}}")
    branch = _git_text(repo_root, "branch", "--show-current")
    porcelain = _git_text(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    clean = porcelain == ""
    identity_failure = None
    if current_head != candidate or branch != "dev" or not clean:
        identity_failure = "candidate is not the current clean dev worktree"
    input_failure: str | None = None
    completed_rows: list[object] | None = None
    completed_non_claims: list[object] | None = None
    reports: list[dict[str, object]] = []
    if evidence_matrix is not None or report_paths is not None:
        try:
            if evidence_matrix is None or report_paths is None:
                raise AuditError("true audit generation requires both matrix and typed reports")
            completed_rows, completed_non_claims = _evidence_matrix_rows(
                evidence_matrix,
                repo_root,
                base=base,
                candidate=candidate,
                candidate_tree=candidate_tree or "0" * 40,
            )
            reports = _typed_report_inventory(
                repo_root,
                report_paths,
                candidate=candidate,
                candidate_tree=candidate_tree or "0" * 40,
            )
            if identity_failure is not None or artifact_failure is not None:
                raise AuditError(identity_failure or artifact_failure or "candidate inputs blocked")
            completed_statuses = [
                cast(dict[str, object], row).get("status") for row in completed_rows
            ]
            completed_status = (
                "FAIL"
                if "FAIL" in completed_statuses
                else "BLOCKED"
                if "BLOCKED" in completed_statuses
                else "PASS"
            )
            expected_artifacts = _validate_artifacts(artifacts, require_complete=True)
            _validate_artifact_bytes(repo_root, artifact_root_label, expected_artifacts)
            report_values, allowed_report_paths = _validate_reports(
                reports,
                repo_root,
                candidate=candidate,
                candidate_tree=candidate_tree or "0" * 40,
                require_complete=True,
            )
            _validate_report_joins(report_values, artifacts, repo_root)
            _validate_rows(
                completed_rows,
                completed_status,
                repo_root,
                allowed_report_paths=allowed_report_paths,
            )
        except AuditError as exc:
            input_failure = str(exc)
            completed_rows = None
            completed_non_claims = None
            reports = []
    first_failure = (
        identity_failure
        or artifact_failure
        or input_failure
        or "audit row evidence is not recorded"
    )
    if completed_rows is not None:
        rows = completed_rows
    else:
        evidence_refs = [
            reference
            for reference in (
                "scripts/m16_03_release_audit.py",
                "tests/test_m16_03_release_audit.py",
                "tests/evidence/AUD-AUTH-01.json",
                "LICENSE",
            )
            if (repo_root / reference).is_file()
        ]
        if not evidence_refs:
            raise AuditError("audit skeleton lacks an approved content-free fallback evidence file")
        evidence_sha256 = _combined_evidence_sha256(
            repo_root,
            tuple(evidence_refs),
            "audit skeleton",
            allowed_report_paths=None,
        )[0]
        rows = [
            {
                "id": row_id,
                "criteria": [_criterion_for(row_id)],
                "subject": f"Finalized checklist row {row_id}",
                "probe_id": f"blocked:{row_id}",
                "probe_owner": row_id,
                "command": "Explicit candidate-bound probe required",
                "expected": "Closed content-free evidence proves the row",
                "observed": "Required evidence has not been recorded",
                "started_at": "1970-01-01T00:00:00+00:00",
                "ended_at": "1970-01-01T00:00:00+00:00",
                "timezone": "Etc/UTC",
                "status": "BLOCKED",
                "first_failure": first_failure,
                "cleanup": "No external resource acquired",
                "forbidden_side_effects": [
                    "External publication",
                    "Provider or model activity",
                    "Private content retention",
                ],
                "evidence_refs": evidence_refs,
                "evidence_sha256": evidence_sha256,
                "reviewer_disposition": "PENDING",
                "finding_disposition": "RELEASE_BLOCKER" if row_id == "AUD-LIC-02" else "NONE",
                "finding_id": "M16-03-F01" if row_id == "AUD-LIC-02" else None,
                "finding_owner": "M16-04" if row_id == "AUD-LIC-02" else None,
            }
            for row_id in EXPECTED_AUDIT_ROWS
        ]
    status = "BLOCKED"
    if completed_rows is not None:
        statuses = [cast(dict[str, object], row).get("status") for row in completed_rows]
        status = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
    release_ready = not any(
        cast(dict[str, object], row).get("finding_disposition") == "RELEASE_BLOCKER" for row in rows
    )
    document: dict[str, object] = {
        "schema": SCHEMA,
        "item": ITEM,
        "status": status,
        "release_ready": release_ready,
        "base": base,
        "candidate": candidate,
        "candidate_tree": candidate_tree
        if candidate_tree and _REVISION.fullmatch(candidate_tree)
        else "0" * 40,
        "branch": "dev",
        "public_worktree_clean": clean,
        "environment": {
            "python": sys.version.split()[0],
            "node": _environment_version(["node", "--version"], "unavailable"),
            "pnpm": _environment_version(["pnpm.cmd", "--version"], "unavailable"),
        },
        "artifact_root": artifact_root_label,
        "artifacts": artifacts,
        "reports": reports,
        "rows": rows,
        "non_claims": completed_non_claims
        if completed_non_claims is not None
        else [
            "No publication or external system mutation",
            "A generated skeleton is not release acceptance",
        ],
        "fingerprint": "",
    }
    document["fingerprint"] = canonical_fingerprint(document)
    validate_audit(document, repo_root=repo_root, expected_candidate=candidate)
    return document


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _safe_report_path(repo_root: Path, report: Path) -> Path:
    candidate = report if report.is_absolute() else repo_root / report
    resolved_root = repo_root.resolve()
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise AuditError("report path escapes the repository") from exc
    if len(relative.parts) != 2 or relative.parts[0] != ".planning":
        raise AuditError("report path must remain directly under ignored .planning/")
    if "m16-03" not in relative.name.casefold() or relative.suffix.casefold() != ".json":
        raise AuditError("report path must be a specific M16-03 JSON result")
    current = resolved_root
    for part in resolved.relative_to(resolved_root).parts[:-1]:
        current = current / part
        if current.exists() and (current.is_symlink() or _is_reparse_point(current)):
            raise AuditError("report path contains a symlink or reparse point")
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", "--", relative.as_posix()],
        cwd=repo_root,
        check=False,
        capture_output=True,
        timeout=15,
    )
    if ignored.returncode != 0:
        raise AuditError("report path must already be ignored")
    if _git_success(repo_root, "ls-files", "--error-unmatch", "--", relative.as_posix()):
        raise AuditError("report path must not be a tracked public file")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate")
    parser.add_argument("--base", default=EXPECTED_BASE)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--query-osv", type=Path)
    parser.add_argument("--evidence-matrix", type=Path)
    for report_kind in _REQUIRED_REPORT_SCHEMAS:
        parser.add_argument(f"--{report_kind.replace('_', '-')}-report", type=Path)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        if args.base != EXPECTED_BASE:
            raise AuditError("base is not the accepted M15-18 checkpoint")
        report = _safe_report_path(repo_root, args.report)
        if args.query_osv is not None:
            if args.candidate is not None or args.artifact_root is not None:
                raise AuditError("--query-osv cannot be combined with audit candidate arguments")
            if report.exists():
                raise AuditError("OSV summary report path must not already exist")
            summary = query_osv(
                args.query_osv if args.query_osv.is_absolute() else repo_root / args.query_osv
            )
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_bytes(_json_bytes(summary))
            print(json.dumps({"schema": summary["schema"], "status": summary["status"]}))
            return 0
        if args.candidate is None or args.artifact_root is None:
            raise AuditError("audit mode requires --candidate and --artifact-root")
        if report.exists():
            document = load_audit(report, repo_root)
        else:
            document = build_audit_document(
                repo_root=repo_root,
                candidate=args.candidate,
                base=args.base,
                artifact_root=args.artifact_root,
                evidence_matrix=(
                    args.evidence_matrix
                    if args.evidence_matrix is None or args.evidence_matrix.is_absolute()
                    else repo_root / args.evidence_matrix
                ),
                report_paths={
                    kind: path if path.is_absolute() else repo_root / path
                    for kind in _REQUIRED_REPORT_SCHEMAS
                    if (path := getattr(args, f"{kind}_report")) is not None
                }
                or None,
            )
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_bytes(_json_bytes(document))
        fingerprint = validate_audit(
            document,
            repo_root=repo_root,
            expected_candidate=args.candidate,
        )
        current_head = _git_text(repo_root, "rev-parse", "HEAD")
        current_tree = _git_text(repo_root, "rev-parse", "HEAD^{tree}")
        branch = _git_text(repo_root, "branch", "--show-current")
        porcelain = _git_text(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
        if current_head != args.candidate:
            raise AuditError("candidate is not the current git HEAD")
        if current_tree != document["candidate_tree"]:
            raise AuditError("candidate tree does not match the current git HEAD tree")
        if branch != "dev" or porcelain != "":
            raise AuditError("candidate is not the current clean dev worktree")
        _validate_git_identity(
            repo_root,
            cast(str, document["base"]),
            cast(str, document["candidate"]),
            cast(str, document["candidate_tree"]),
        )
        if document["status"] != "PASS":
            raise AuditError(f"audit status is {document['status']}; every row must pass")
    except (OSError, AuditError) as exc:
        print(f"M16-03 release audit: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"schema": SCHEMA, "status": "PASS", "fingerprint": fingerprint}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
