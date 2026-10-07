"""Validate the M16-02 reconstruction and clean-install regression matrix offline.

The matrix is a claim-boundary record, not a model/provider execution harness.  It proves that
every roadmap criterion is represented and that unavailable or unsupported lanes cannot be promoted
to PASS or substituted by a stripped/native-drift/fallback lane.
"""

# The closed matrix rows keep evidence/assertion text together for deterministic review.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "h3-context-reconstruction-regression/1"
ITEM = "M16-02"
VERSION = "1.0.0"
UPDATED_AT = "2026-08-11"
SUPPORTED_PROFILE_ID = "supported-c44-v1"

EXPECTED_CRITERIA = (
    "R3",
    "R4",
    "R5",
    "R6",
    "R8",
    "R9",
    "R10",
    *(f"P{index}" for index in range(1, 16)),
    *(f"E{index}" for index in range(1, 8)),
)
EXPECTED_REGRESSIONS = (
    "standard_string_export",
    "real_native_prompt_validation",
    "fully_qualified_v3_child_bindings",
    "native_required_inputs_enums",
    "graph_identity_order_pairing_ownership",
    "workflow_migration",
    "cancellation_and_cleanup",
    "node_only_fallback",
    "offline_provider_absence",
    "disable_uninstall_rollback",
)
ALLOWED_STATUS = frozenset({"PASS", "BLOCKED", "UNAVAILABLE", "UNSUPPORTED", "NOT_APPLICABLE"})
ALLOWED_DISPOSITION = frozenset(
    {"qualified", "manual_only", "blocked", "unavailable", "unsupported", "not_applicable"}
)
ALLOWED_EVIDENCE_KIND = frozenset(
    {
        "structural_fixture",
        "host_e2e",
        "browser_host_probe",
        "release_artifact",
        "security_audit",
        "oracle_disposition",
        "generation_disposition",
        "provider_disposition",
        "manual_boundary",
    }
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_CRITERION_EVIDENCE_KIND = {
    "R3": "release_artifact",
    "R4": "release_artifact",
    "R5": "release_artifact",
    "R6": "release_artifact",
    "R8": "release_artifact",
    "R9": "host_e2e",
    "R10": "security_audit",
    "P1": "security_audit",
    "P2": "structural_fixture",
    "P3": "structural_fixture",
    "P4": "structural_fixture",
    "P5": "provider_disposition",
    "P6": "browser_host_probe",
    "P7": "structural_fixture",
    "P8": "structural_fixture",
    "P9": "oracle_disposition",
    "P10": "generation_disposition",
    "P11": "host_e2e",
    "P12": "host_e2e",
    "P13": "host_e2e",
    "P14": "structural_fixture",
    "P15": "host_e2e",
    "E1": "structural_fixture",
    "E2": "host_e2e",
    "E3": "oracle_disposition",
    "E4": "generation_disposition",
    "E5": "structural_fixture",
    "E6": "security_audit",
    "E7": "manual_boundary",
}
_REGRESSION_EVIDENCE_KIND = {
    "standard_string_export": "host_e2e",
    "real_native_prompt_validation": "host_e2e",
    "fully_qualified_v3_child_bindings": "host_e2e",
    "native_required_inputs_enums": "host_e2e",
    "graph_identity_order_pairing_ownership": "structural_fixture",
    "workflow_migration": "structural_fixture",
    "cancellation_and_cleanup": "host_e2e",
    "node_only_fallback": "host_e2e",
    "offline_provider_absence": "provider_disposition",
    "disable_uninstall_rollback": "release_artifact",
}


class ReconstructionRegressionError(ValueError):
    """Raised when a reconstruction matrix is incomplete or claim-escalating."""


def _object(
    value: object, field: str, *, required: set[str], allowed: set[str]
) -> dict[str, object]:
    if type(value) is not dict:
        raise ReconstructionRegressionError(f"{field} must be an object")
    result = cast(dict[str, object], value)
    missing = sorted(required.difference(result))
    unknown = sorted(set(result).difference(allowed))
    if missing:
        raise ReconstructionRegressionError(
            f"{field} is missing required fields: {', '.join(missing)}"
        )
    if unknown:
        raise ReconstructionRegressionError(
            f"{field} contains unknown/private members: {', '.join(unknown)}"
        )
    return result


def _string(
    value: object, field: str, *, maximum: int = 4096, pattern: re.Pattern[str] | None = None
) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ReconstructionRegressionError(f"{field} must be bounded text")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ReconstructionRegressionError(f"{field} has an invalid format")
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ReconstructionRegressionError(f"{field} must be an exact boolean")
    return value


def _list(value: object, field: str, *, maximum: int = 128) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        raise ReconstructionRegressionError(f"{field} must be a bounded list")
    return cast(list[object], value)


def _string_list(value: object, field: str, *, maximum: int = 16) -> tuple[str, ...]:
    values = _list(value, field, maximum=maximum)
    result = tuple(
        _string(item, f"{field}[{index}]", maximum=512) for index, item in enumerate(values)
    )
    if len(result) != len(set(result)):
        raise ReconstructionRegressionError(f"{field} contains duplicate values")
    return result


def _finite(value: object, field: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ReconstructionRegressionError(f"{field} must contain finite numbers")
    if type(value) is dict:
        for key, child in value.items():
            _finite(child, f"{field}.{key}")
    elif type(value) is list:
        for index, child in enumerate(value):
            _finite(child, f"{field}[{index}]")


def _canonical_payload_bytes(value: object) -> bytes:
    """Return canonical matrix bytes without the self-reference."""

    if type(value) is not dict:
        raise ReconstructionRegressionError("matrix must be an object")
    payload = dict(cast(dict[str, object], value))
    payload.pop("fingerprint", None)
    try:
        return json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReconstructionRegressionError("matrix cannot be canonically encoded") from exc


def canonical_fingerprint(value: object) -> str:
    """Return the SHA-256 over canonical matrix bytes without the self-reference."""

    return "sha256:" + hashlib.sha256(_canonical_payload_bytes(value)).hexdigest()


def _reject_constant(value: str) -> object:
    raise ReconstructionRegressionError(f"non-finite JSON constant {value} is forbidden")


def _reject_duplicate(members: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in members:
        if key in result:
            raise ReconstructionRegressionError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def load_matrix(path: Path) -> dict[str, object]:
    """Read one strict UTF-8 matrix without accepting duplicate/non-finite JSON."""

    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ReconstructionRegressionError("matrix must be strict UTF-8 without a BOM")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise ReconstructionRegressionError("matrix must be strict UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ReconstructionRegressionError(f"matrix JSON is invalid: {exc.msg}") from exc
    if type(value) is not dict:
        raise ReconstructionRegressionError("matrix root must be an object")
    _finite(value, "matrix")
    return cast(dict[str, object], value)


def _safe_evidence_path(value: object, field: str, repo_root: Path) -> str:
    reference = _string(value, field, maximum=512)
    if (
        "\\" in reference
        or reference.startswith(("/", "~"))
        or ":" in reference
        or ".." in Path(reference).parts
        or reference.startswith(".")
        or reference.casefold().startswith(("http:", "https:"))
    ):
        raise ReconstructionRegressionError(f"{field} is an unsafe evidence path")
    parts = {part.casefold() for part in Path(reference).parts}
    if parts.intersection({".planning", "reference", ".tmp", ".git"}):
        raise ReconstructionRegressionError(f"{field} references internal/private evidence")
    resolved_root = repo_root.resolve()
    candidate = (repo_root / reference).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ReconstructionRegressionError(f"{field} escapes the repository") from exc
    current = repo_root
    for part in Path(reference).parts:
        current = current / part
        attributes = getattr(current.stat(), "st_file_attributes", 0) if current.exists() else 0
        if current.is_symlink() or attributes & 0x400:
            raise ReconstructionRegressionError(f"{field} contains a symlink component")
    if not candidate.is_file():
        raise ReconstructionRegressionError(f"{field} does not reference an existing file")
    return reference


def _reject_sensitive_text(value: object, field: str = "matrix") -> None:
    """Reject locator/credential-like values before they can become public evidence."""

    if type(value) is str:
        lowered = value.casefold()
        forbidden = (
            "http://",
            "https://",
            "file://",
            "file:",
            "data://",
            "data:",
            "signed_url",
            "signed_resource",
            "signed-url",
            "signed url",
            "signed resource",
            "api_key",
            "api key",
            "password",
            "cookie",
            "authorization",
            "credential",
            "secret=",
            "secret:",
            "token=",
            "token:",
            "provider_payload",
            "provider payload",
            "provider_content",
            "provider content",
            "raw_provider",
            "raw provider",
            "raw_payload",
            "raw_prompt",
            "raw prompt",
            "raw_media",
            "raw media",
            "private_media",
            "private media",
            "local_path",
            "local path",
        )
        if lowered in {"secret", "token"} or any(marker in lowered for marker in forbidden):
            raise ReconstructionRegressionError(
                f"{field} contains forbidden locator/private content"
            )
    elif type(value) is dict:
        for key, child in value.items():
            _reject_sensitive_text(child, f"{field}.{key}")
    elif type(value) is list:
        for index, child in enumerate(value):
            _reject_sensitive_text(child, f"{field}[{index}]")


def _validate_evidence(value: object, field: str, repo_root: Path) -> tuple[str, ...]:
    values = _string_list(value, field, maximum=8)
    if not values:
        raise ReconstructionRegressionError(f"{field} must contain evidence")
    return tuple(
        _safe_evidence_path(item, f"{field}[{index}]", repo_root)
        for index, item in enumerate(values)
    )


def _validate_execution(value: object, field: str) -> dict[str, object]:
    execution = _object(
        value,
        field,
        required={
            "network_contacted",
            "provider_enabled",
            "model_loaded",
            "media_opened",
            "host_started",
            "native_present",
        },
        allowed={
            "network_contacted",
            "provider_enabled",
            "model_loaded",
            "media_opened",
            "host_started",
            "native_present",
        },
    )
    for key in execution:
        _bool(execution[key], f"{field}.{key}")
    for key in ("network_contacted", "provider_enabled", "model_loaded", "media_opened"):
        if execution[key] is True:
            raise ReconstructionRegressionError(f"{field} has a forbidden side effect: {key}")
    return execution


def _validate_evidence_kind(value: object, field: str) -> str:
    evidence_kind = _string(value, field, maximum=64)
    if evidence_kind not in ALLOWED_EVIDENCE_KIND:
        raise ReconstructionRegressionError(f"{field} is unsupported")
    return evidence_kind


def _validate_profiles(value: object, repo_root: Path) -> tuple[dict[str, object], ...]:
    profiles = _list(value, "profiles", maximum=16)
    if not profiles:
        raise ReconstructionRegressionError("profiles must not be empty")
    required = {
        "profile_id",
        "route",
        "disposition",
        "claim_ceiling",
        "qualified",
        "native_required",
        "evidence_refs",
        "host_version",
        "host_revision",
        "frontend_version",
        "frontend_revision",
        "node_api",
        "prompt_socket",
        "evidence_kind",
    }
    result: list[dict[str, object]] = []
    ids: set[str] = set()
    for index, raw in enumerate(profiles):
        field = f"profiles[{index}]"
        profile = _object(raw, field, required=required, allowed=required)
        profile_id = _string(profile["profile_id"], f"{field}.profile_id", pattern=_ID, maximum=128)
        if profile_id in ids:
            raise ReconstructionRegressionError(f"{field}.profile_id is duplicated")
        ids.add(profile_id)
        disposition = _string(profile["disposition"], f"{field}.disposition", maximum=32)
        if disposition not in ALLOWED_DISPOSITION:
            raise ReconstructionRegressionError(f"{field}.disposition is unsupported")
        qualified = _bool(profile["qualified"], f"{field}.qualified")
        _bool(profile["native_required"], f"{field}.native_required")
        _string(profile["route"], f"{field}.route", pattern=_ID, maximum=64)
        _string(profile["claim_ceiling"], f"{field}.claim_ceiling", maximum=256)
        _string(profile["host_version"], f"{field}.host_version", maximum=32)
        host_revision = _string(profile["host_revision"], f"{field}.host_revision", maximum=64)
        if host_revision != "unavailable" and _REVISION.fullmatch(host_revision) is None:
            raise ReconstructionRegressionError(f"{field}.host_revision is invalid")
        _string(profile["frontend_version"], f"{field}.frontend_version", maximum=32)
        _string(profile["frontend_revision"], f"{field}.frontend_revision", maximum=64)
        _string(profile["node_api"], f"{field}.node_api", maximum=32)
        _string(profile["prompt_socket"], f"{field}.prompt_socket", maximum=32)
        _validate_evidence_kind(profile["evidence_kind"], f"{field}.evidence_kind")
        _validate_evidence(profile["evidence_refs"], f"{field}.evidence_refs", repo_root)
        if profile_id == SUPPORTED_PROFILE_ID:
            if disposition != "qualified" or not qualified:
                raise ReconstructionRegressionError("supported profile must remain qualified")
            expected = {
                "host_version": "0.30.0",
                "host_revision": "c44dea18809e3ca0e12e25cbe6938c2c45a29c9d",
                "frontend_version": "1.47.12",
                "frontend_revision": "f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da",
                "node_api": "V1_ONLY",
                "prompt_socket": "STRING",
            }
            if any(profile[key] != expected[key] for key in expected):
                raise ReconstructionRegressionError("supported profile identity drifted")
        elif qualified or disposition == "qualified":
            raise ReconstructionRegressionError(
                f"profile {profile_id} is an unqualified profile promotion"
            )
        result.append(profile)
    expected_ids = {
        SUPPORTED_PROFILE_ID,
        "v3-numbered",
        "latest-compatible",
        "fixed-h3-generation",
        "official-oracle",
        "ollama-live",
    }
    if ids != expected_ids:
        raise ReconstructionRegressionError("profiles do not cover the closed M16-02 inventory")
    return tuple(result)


def _validate_criterion(value: object, index: int, repo_root: Path) -> dict[str, object]:
    field = f"criteria[{index}]"
    required = {
        "id",
        "title",
        "status",
        "disposition",
        "reason",
        "claim_ceiling",
        "evidence_kind",
        "evidence_refs",
        "execution",
        "native_required",
        "substitution_allowed",
    }
    criterion = _object(value, field, required=required, allowed=required)
    criterion_id = _string(criterion["id"], f"{field}.id", pattern=_ID, maximum=16)
    status = _string(criterion["status"], f"{field}.status", maximum=32)
    if status not in ALLOWED_STATUS:
        raise ReconstructionRegressionError(f"{field}.status is unsupported; SKIP is never valid")
    disposition = _string(criterion["disposition"], f"{field}.disposition", maximum=32)
    if disposition not in ALLOWED_DISPOSITION:
        raise ReconstructionRegressionError(f"{field}.disposition is unsupported")
    if criterion_id in {"P9", "P10", "E3", "E4"} and status == "PASS":
        raise ReconstructionRegressionError(f"{field} unavailable oracle/H3 lane cannot be PASS")
    if criterion_id == "P13" and status == "PASS":
        raise ReconstructionRegressionError(
            "V3 child-binding criterion cannot be PASS before requalification"
        )
    reason = criterion["reason"]
    if reason is not None:
        _string(reason, f"{field}.reason", maximum=512)
    if status == "PASS":
        if disposition not in {"qualified", "manual_only"}:
            raise ReconstructionRegressionError(f"{field} unavailable/blocked lane cannot be PASS")
        if disposition == "qualified":
            raise ReconstructionRegressionError(
                f"{field} cannot promote a qualified claim under MANUAL_ONLY_SCOPED"
            )
        if reason is not None:
            raise ReconstructionRegressionError(f"{field}.reason must be null for PASS")
    else:
        expected_disposition = status.casefold()
        if disposition != expected_disposition:
            raise ReconstructionRegressionError(f"{field} status/disposition disagree")
        if reason is None:
            raise ReconstructionRegressionError(f"{field} non-PASS status requires a reason")
    _string(criterion["title"], f"{field}.title", maximum=256)
    _string(criterion["claim_ceiling"], f"{field}.claim_ceiling", maximum=512)
    _validate_evidence_kind(criterion["evidence_kind"], f"{field}.evidence_kind")
    _validate_evidence(criterion["evidence_refs"], f"{field}.evidence_refs", repo_root)
    execution = _validate_execution(criterion["execution"], f"{field}.execution")
    native_required = _bool(criterion["native_required"], f"{field}.native_required")
    if native_required and execution["native_present"] is not True:
        raise ReconstructionRegressionError(f"{field} requires real native evidence")
    if _bool(criterion["substitution_allowed"], f"{field}.substitution_allowed"):
        raise ReconstructionRegressionError(f"{field} permits a forbidden substitution")
    return criterion


def _validate_regression(value: object, index: int, repo_root: Path) -> dict[str, object]:
    field = f"regressions[{index}]"
    required = {
        "id",
        "status",
        "subject_disposition",
        "assertion",
        "evidence_kind",
        "evidence_refs",
        "execution",
        "native_required",
    }
    regression = _object(value, field, required=required, allowed=required)
    _string(regression["id"], f"{field}.id", pattern=_ID, maximum=128)
    if _string(regression["status"], f"{field}.status", maximum=16) != "PASS":
        raise ReconstructionRegressionError(
            f"{field}.status must PASS for the regression assertion"
        )
    subject = _string(regression["subject_disposition"], f"{field}.subject_disposition", maximum=32)
    if subject not in ALLOWED_DISPOSITION:
        raise ReconstructionRegressionError(f"{field}.subject_disposition is unsupported")
    if subject == "qualified":
        raise ReconstructionRegressionError(
            f"{field}.subject_disposition cannot promote a qualified claim under MANUAL_ONLY_SCOPED"
        )
    assertion = _string(regression["assertion"], f"{field}.assertion", maximum=512)
    if subject in {"blocked", "unavailable", "unsupported"} and not any(
        token in assertion.casefold() for token in ("reject", "not promoted", "unavailable")
    ):
        raise ReconstructionRegressionError(f"{field} must assert non-PASS disposition")
    _validate_evidence_kind(regression["evidence_kind"], f"{field}.evidence_kind")
    _validate_evidence(regression["evidence_refs"], f"{field}.evidence_refs", repo_root)
    execution = _validate_execution(regression["execution"], f"{field}.execution")
    native_required = _bool(regression["native_required"], f"{field}.native_required")
    if native_required and execution["native_present"] is not True:
        raise ReconstructionRegressionError(f"{field} requires real native evidence")
    return regression


def _validate_skip_debt(value: object) -> None:
    debt = _object(
        value,
        "skip_debt",
        required={"approved_count", "unapproved_count", "items"},
        allowed={"approved_count", "unapproved_count", "items"},
    )
    approved_count = debt["approved_count"]
    unapproved_count = debt["unapproved_count"]
    if type(approved_count) is not int or approved_count < 0:
        raise ReconstructionRegressionError(
            "skip_debt.approved_count must be a non-negative integer"
        )
    if type(unapproved_count) is not int or unapproved_count < 0:
        raise ReconstructionRegressionError(
            "skip_debt.unapproved_count must be a non-negative integer"
        )
    if unapproved_count != 0:
        raise ReconstructionRegressionError("skip debt contains unapproved/flake work")
    items = _list(debt["items"], "skip_debt.items", maximum=16)
    if approved_count != len(items):
        raise ReconstructionRegressionError("skip debt count does not match approved items")
    identifiers: set[str] = set()
    for index, raw in enumerate(items):
        field = f"skip_debt.items[{index}]"
        item = _object(
            raw,
            field,
            required={"skip_id", "reason", "status"},
            allowed={"skip_id", "reason", "status"},
        )
        identifier = _string(item["skip_id"], f"{field}.skip_id", pattern=_ID, maximum=128)
        if identifier in identifiers:
            raise ReconstructionRegressionError(f"{field}.skip_id is duplicated")
        identifiers.add(identifier)
        _string(item["reason"], f"{field}.reason", maximum=256)
        if _string(item["status"], f"{field}.status", maximum=16) != "approved":
            raise ReconstructionRegressionError(f"{field}.status must be approved")


def _validate_guards(value: object) -> None:
    guards = _object(
        value,
        "substitution_guards",
        required={
            "native_stripped_qualifies",
            "fallback_satisfies_native",
            "latest_satisfies_supported",
            "ollama_satisfies_native",
            "generic_registration_satisfies_native",
            "unavailable_scored_as_pass",
        },
        allowed={
            "native_stripped_qualifies",
            "fallback_satisfies_native",
            "latest_satisfies_supported",
            "ollama_satisfies_native",
            "generic_registration_satisfies_native",
            "unavailable_scored_as_pass",
        },
    )
    for key in guards:
        if _bool(guards[key], f"substitution_guards.{key}"):
            raise ReconstructionRegressionError(f"substitution_guards.{key} must be false")


def _validate_artifact_boundary(value: object, repo_root: Path) -> None:
    boundary = _object(
        value,
        "artifact_boundary",
        required={
            "clean_install",
            "coinstallation",
            "rollback",
            "one_entry_bundle",
            "runtime_entry_count",
            "runtime_entries",
            "artifact_kinds",
            "network",
            "provider_absence",
            "artifact_hashes_bound",
            "release_matrix_bound",
            "evidence_kind",
            "evidence_refs",
        },
        allowed={
            "clean_install",
            "coinstallation",
            "rollback",
            "one_entry_bundle",
            "runtime_entry_count",
            "runtime_entries",
            "artifact_kinds",
            "network",
            "provider_absence",
            "artifact_hashes_bound",
            "release_matrix_bound",
            "evidence_kind",
            "evidence_refs",
        },
    )
    for key in (
        "clean_install",
        "coinstallation",
        "rollback",
        "one_entry_bundle",
        "provider_absence",
        "artifact_hashes_bound",
        "release_matrix_bound",
    ):
        if _bool(boundary[key], f"artifact_boundary.{key}") is not True:
            raise ReconstructionRegressionError(f"artifact_boundary.{key} must be true")
    if boundary["runtime_entry_count"] != 1 or type(boundary["runtime_entry_count"]) is not int:
        raise ReconstructionRegressionError("artifact_boundary.runtime_entry_count must be one")
    runtime_entries = _string_list(
        boundary["runtime_entries"], "artifact_boundary.runtime_entries", maximum=4
    )
    if runtime_entries != ("h3-context-sidebar.js",):
        raise ReconstructionRegressionError("artifact_boundary runtime entry inventory drifted")
    artifact_kinds = _string_list(
        boundary["artifact_kinds"], "artifact_boundary.artifact_kinds", maximum=4
    )
    if artifact_kinds != ("sdist", "direct_wheel", "wheel_from_sdist"):
        raise ReconstructionRegressionError("artifact_boundary artifact inventory drifted")
    if _string(boundary["network"], "artifact_boundary.network", maximum=16) != "disabled":
        raise ReconstructionRegressionError("artifact_boundary network must remain disabled")
    _validate_evidence_kind(boundary["evidence_kind"], "artifact_boundary.evidence_kind")
    if boundary["evidence_kind"] != "release_artifact":
        raise ReconstructionRegressionError(
            "artifact_boundary evidence kind must be release_artifact"
        )
    _validate_evidence(boundary["evidence_refs"], "artifact_boundary.evidence_refs", repo_root)


def validate_matrix(value: object, repo_root: Path = ROOT, *, frozen: bool = True) -> str:
    """Validate a matrix and, by default, bind it to the generated frozen output."""

    matrix = _object(
        value,
        "matrix",
        required={
            "schema",
            "item",
            "version",
            "updated_at",
            "qualification_status",
            "supported_profile_id",
            "profiles",
            "criteria",
            "regressions",
            "execution_boundary",
            "artifact_boundary",
            "skip_debt",
            "substitution_guards",
            "fingerprint",
        },
        allowed={
            "schema",
            "item",
            "version",
            "updated_at",
            "qualification_status",
            "supported_profile_id",
            "profiles",
            "criteria",
            "regressions",
            "execution_boundary",
            "artifact_boundary",
            "skip_debt",
            "substitution_guards",
            "fingerprint",
        },
    )
    _reject_sensitive_text(matrix)
    if _string(matrix["schema"], "schema", maximum=128) != SCHEMA:
        raise ReconstructionRegressionError("schema is unsupported")
    if _string(matrix["item"], "item", maximum=32) != ITEM:
        raise ReconstructionRegressionError("item must be M16-02")
    if _string(matrix["version"], "version", maximum=32) != VERSION:
        raise ReconstructionRegressionError("version is unsupported")
    if _string(matrix["updated_at"], "updated_at", maximum=32) != UPDATED_AT:
        raise ReconstructionRegressionError("updated_at is not the frozen date")
    if (
        _string(matrix["qualification_status"], "qualification_status", maximum=64)
        != "MANUAL_ONLY_SCOPED"
    ):
        raise ReconstructionRegressionError("qualification_status must remain MANUAL_ONLY_SCOPED")
    if (
        _string(matrix["supported_profile_id"], "supported_profile_id", pattern=_ID, maximum=128)
        != SUPPORTED_PROFILE_ID
    ):
        raise ReconstructionRegressionError(
            "supported_profile_id is not the exact qualified profile"
        )
    _validate_profiles(matrix["profiles"], repo_root)
    criteria_raw = _list(matrix["criteria"], "criteria", maximum=64)
    criteria: list[dict[str, object]] = []
    for index, item in enumerate(criteria_raw):
        if type(item) is not dict:
            raise ReconstructionRegressionError(f"criteria[{index}] must be an object")
        criteria.append(cast(dict[str, object], item))
    if (
        tuple(_string(item["id"], "criteria.id", maximum=16) for item in criteria)
        != EXPECTED_CRITERIA
    ):
        raise ReconstructionRegressionError("criteria do not cover the closed M16-02 inventory")
    for index, item in enumerate(criteria):
        _validate_criterion(item, index, repo_root)
    regressions_raw = _list(matrix["regressions"], "regressions", maximum=32)
    regressions: list[dict[str, object]] = []
    for index, item in enumerate(regressions_raw):
        if type(item) is not dict:
            raise ReconstructionRegressionError(f"regressions[{index}] must be an object")
        regressions.append(cast(dict[str, object], item))
    if (
        tuple(_string(item["id"], "regressions.id", maximum=128) for item in regressions)
        != EXPECTED_REGRESSIONS
    ):
        raise ReconstructionRegressionError("regressions do not cover the closed M16-02 inventory")
    for index, item in enumerate(regressions):
        _validate_regression(item, index, repo_root)
    boundary = _object(
        matrix["execution_boundary"],
        "execution_boundary",
        required={
            "network_contacted",
            "provider_enabled",
            "model_loaded",
            "media_opened",
            "credentials_used",
        },
        allowed={
            "network_contacted",
            "provider_enabled",
            "model_loaded",
            "media_opened",
            "credentials_used",
        },
    )
    for key in boundary:
        if _bool(boundary[key], f"execution_boundary.{key}"):
            raise ReconstructionRegressionError(
                f"execution boundary has a forbidden side effect: {key}"
            )
    _validate_skip_debt(matrix["skip_debt"])
    _validate_guards(matrix["substitution_guards"])
    _validate_artifact_boundary(matrix["artifact_boundary"], repo_root)
    declared = _string(matrix["fingerprint"], "fingerprint", pattern=_SHA256, maximum=80)
    actual = canonical_fingerprint(matrix)
    if declared != actual:
        raise ReconstructionRegressionError("fingerprint does not match canonical matrix bytes")
    if frozen:
        expected = build_matrix(repo_root, validate=False)
        if _canonical_payload_bytes(matrix) != _canonical_payload_bytes(expected):
            raise ReconstructionRegressionError("matrix does not match generated frozen output")
    return declared


def _execution(*, native: bool, host: bool = False) -> dict[str, bool]:
    return {
        "network_contacted": False,
        "provider_enabled": False,
        "model_loaded": False,
        "media_opened": False,
        "host_started": host,
        "native_present": native,
    }


def _criterion(
    criterion_id: str,
    title: str,
    evidence: tuple[str, ...],
    *,
    status: str = "PASS",
    disposition: str = "manual_only",
    reason: str | None = None,
    ceiling: str = "structural_and_declared_matrix_only",
    native: bool = False,
    host: bool = False,
    evidence_kind: str | None = None,
) -> dict[str, object]:
    return {
        "id": criterion_id,
        "title": title,
        "status": status,
        "disposition": disposition,
        "reason": reason,
        "claim_ceiling": ceiling,
        "evidence_kind": evidence_kind or _CRITERION_EVIDENCE_KIND[criterion_id],
        "evidence_refs": list(evidence),
        "execution": _execution(native=native, host=host),
        "native_required": native,
        "substitution_allowed": False,
    }


def _regression(
    regression_id: str,
    assertion: str,
    evidence: tuple[str, ...],
    *,
    subject: str = "manual_only",
    native: bool = False,
    host: bool = False,
    evidence_kind: str | None = None,
) -> dict[str, object]:
    return {
        "id": regression_id,
        "status": "PASS",
        "subject_disposition": subject,
        "assertion": assertion,
        "evidence_kind": evidence_kind or _REGRESSION_EVIDENCE_KIND[regression_id],
        "evidence_refs": list(evidence),
        "execution": _execution(native=native, host=host),
        "native_required": native,
    }


def build_matrix(repo_root: Path = ROOT, *, validate: bool = True) -> dict[str, object]:
    """Build the frozen M16-02 fixture from repository-owned public evidence identifiers."""

    common = ("tests/acceptance_baseline.json", "scripts/run_full_tests_windows.ps1")
    criteria = [
        _criterion("R3", "clean_artifact", ("scripts/build_gate_report.py",)),
        _criterion("R4", "clean_install", ("scripts/release_matrix.py",)),
        _criterion("R5", "one_entry_bundle", ("scripts/frontend_build_report.py",)),
        _criterion("R6", "packaging_migration_rollback", ("scripts/release_matrix.py",)),
        _criterion("R8", "co_installation", ("scripts/release_matrix.py",)),
        _criterion(
            "R9",
            "performance_cancellation",
            ("tests/test_performance_gate.py", "tests/test_media_subprocess_hardening.py"),
        ),
        _criterion("R10", "offline_extension_provider_absence", ("scripts/security_audit.py",)),
        _criterion("P1", "secret_and_hygiene", ("scripts/security_audit.py",)),
        _criterion(
            "P2",
            "format_lint_type_unit_build",
            (
                "scripts/run_full_tests_windows.ps1",
                "scripts/run_full_tests_linux.sh",
            ),
        ),
        _criterion("P3", "pure_core", ("tests/test_contracts_v2.py",)),
        _criterion("P4", "schema_golden_property", ("tests/test_contracts_v2.py",)),
        _criterion(
            "P5",
            "adversarial_media_provider",
            ("tests/test_media_security.py", "tests/test_provider_policy.py"),
        ),
        _criterion(
            "P6",
            "browser_and_frontend_boundary",
            ("scripts/m15_03_host_frontend_e2e.py",),
            host=True,
        ),
        _criterion("P7", "perception", ("tests/test_perturbation_evaluation.py",)),
        _criterion("P8", "fusion_and_planner", ("tests/test_constrained_semantic_planning.py",)),
        _criterion(
            "P9",
            "official_oracle",
            ("tests/test_comparative_evaluation.py",),
            status="UNAVAILABLE",
            disposition="unavailable",
            reason="official_oracle_not_authorized_or_available",
            ceiling="no_official_oracle_claim",
        ),
        _criterion(
            "P10",
            "fixed_h3_generation",
            ("tests/fixtures/m14_05_fixed_h3_generation.json",),
            status="UNAVAILABLE",
            disposition="unavailable",
            reason="weight_backed_fixed_h3_lane_not_authorized",
            ceiling="no_fixed_h3_generation_claim",
        ),
        _criterion(
            "P11",
            "standard_string_export",
            ("tests/test_native_h3_adapter.py",),
            native=True,
            host=True,
        ),
        _criterion(
            "P12",
            "native_schema_inputs_enums",
            ("tests/test_native_h3_adapter.py",),
            native=True,
            host=True,
        ),
        _criterion(
            "P13",
            "fully_qualified_v3_child_bindings",
            ("tests/test_supported_host_e2e.py",),
            status="BLOCKED",
            disposition="blocked",
            reason="numbered_v3_profile_not_requalified_after_source_drift",
            ceiling="no_v3_executable_claim",
            native=True,
            host=True,
        ),
        _criterion(
            "P14",
            "serialized_queued_identity_order_pairing_ownership",
            ("tests/test_workflow_fixtures.py",),
            native=True,
        ),
        _criterion(
            "P15",
            "workflow_migration_and_recovery",
            ("tests/test_workflow_migration.py", "tests/test_supported_host_e2e.py"),
            native=True,
            host=True,
        ),
        _criterion("E1", "structural_evidence", common),
        _criterion(
            "E2",
            "supported_host_evidence",
            ("tests/test_supported_host_e2e.py",),
            native=True,
            host=True,
        ),
        _criterion(
            "E3",
            "oracle_evidence",
            ("tests/test_comparative_evaluation.py",),
            status="UNAVAILABLE",
            disposition="unavailable",
            reason="official_oracle_not_authorized_or_available",
            ceiling="no_oracle_comparison_claim",
        ),
        _criterion(
            "E4",
            "fixed_h3_evidence",
            ("tests/fixtures/m14_05_fixed_h3_generation.json",),
            status="UNAVAILABLE",
            disposition="unavailable",
            reason="weight_backed_fixed_h3_lane_not_authorized",
            ceiling="no_fixed_h3_generation_claim",
        ),
        _criterion(
            "E5",
            "perception_fusion_planner_evidence",
            (
                "tests/test_perturbation_evaluation.py",
                "tests/test_constrained_semantic_planning.py",
            ),
        ),
        _criterion(
            "E6",
            "adversarial_and_privacy_evidence",
            ("tests/test_media_security.py", "scripts/security_audit.py"),
        ),
        _criterion(
            "E7",
            "claim_and_disposition_evidence",
            (
                "governance/contracts/capability_manifest_v1.schema.json",
                "governance/contracts/fixed_h3_generation_v1.schema.json",
            ),
        ),
    ]
    regressions = [
        _regression(
            "standard_string_export",
            "standard STRING export remains typed and is not replaced by H3_PROMPT_STRING",
            ("tests/test_native_h3_adapter.py",),
            native=True,
            host=True,
        ),
        _regression(
            "real_native_prompt_validation",
            "real native classes and prompt wiring are validated; stripped classes do not pass",
            ("tests/test_native_h3_adapter.py",),
            native=True,
            host=True,
        ),
        _regression(
            "fully_qualified_v3_child_bindings",
            "unqualified V3 child bindings are rejected and not promoted",
            ("tests/test_supported_host_e2e.py",),
            subject="blocked",
            native=True,
            host=True,
        ),
        _regression(
            "native_required_inputs_enums",
            "required native inputs and enum values are checked against the accepted profile",
            ("tests/test_native_h3_adapter.py",),
            native=True,
            host=True,
        ),
        _regression(
            "graph_identity_order_pairing_ownership",
            "manifest, serialized graph, and queued edges retain identity/order/pairing/ownership",
            ("tests/test_workflow_fixtures.py",),
            native=True,
        ),
        _regression(
            "workflow_migration",
            "legacy and current workflow migration paths are explicit and rollback-safe",
            ("tests/test_workflow_migration.py",),
            native=True,
        ),
        _regression(
            "cancellation_and_cleanup",
            "cancelled execution does not emit a fake result and cleans its bounded resources",
            ("tests/test_media_subprocess_hardening.py",),
        ),
        _regression(
            "node_only_fallback",
            "node-only fallback remains usable without silently enabling an unavailable shell/provider",
            ("tests/test_supported_host_e2e.py",),
            host=True,
        ),
        _regression(
            "offline_provider_absence",
            "offline and optional-provider-absent paths remain explicit and no fallback satisfies native evidence",
            ("tests/test_provider_policy.py", "scripts/security_audit.py"),
        ),
        _regression(
            "disable_uninstall_rollback",
            "disable, uninstall, and rollback leave no stale registration or bundle entry",
            ("tests/test_registration_harness.py", "scripts/release_matrix.py"),
        ),
    ]
    matrix: dict[str, object] = {
        "schema": SCHEMA,
        "item": ITEM,
        "version": VERSION,
        "updated_at": UPDATED_AT,
        "qualification_status": "MANUAL_ONLY_SCOPED",
        "supported_profile_id": SUPPORTED_PROFILE_ID,
        "profiles": [
            {
                "profile_id": SUPPORTED_PROFILE_ID,
                "route": "v1",
                "disposition": "qualified",
                "claim_ceiling": "structural_native_manual_only",
                "qualified": True,
                "native_required": True,
                "evidence_refs": [
                    "governance/contracts/compatibility_matrix_v1.json",
                    "tests/test_native_h3_adapter.py",
                ],
                "host_version": "0.30.0",
                "host_revision": "c44dea18809e3ca0e12e25cbe6938c2c45a29c9d",
                "frontend_version": "1.47.12",
                "frontend_revision": "f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da",
                "node_api": "V1_ONLY",
                "prompt_socket": "STRING",
                "evidence_kind": "structural_fixture",
            },
            {
                "profile_id": "v3-numbered",
                "route": "v3",
                "disposition": "blocked",
                "claim_ceiling": "no_v3_executable_claim",
                "qualified": False,
                "native_required": True,
                "evidence_refs": [
                    "governance/contracts/compatibility_matrix_v1.json",
                    "tests/fixtures/m16_01_source_requalification.json",
                ],
                "host_version": "unavailable",
                "host_revision": "unavailable",
                "frontend_version": "1.47.12",
                "frontend_revision": "f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da",
                "node_api": "V3_UNPINNED",
                "prompt_socket": "STRING",
                "evidence_kind": "structural_fixture",
            },
            {
                "profile_id": "latest-compatible",
                "route": "latest",
                "disposition": "blocked",
                "claim_ceiling": "no_latest_substitution",
                "qualified": False,
                "native_required": True,
                "evidence_refs": [
                    "governance/contracts/compatibility_matrix_v1.json",
                    "scripts/m16_01_source_requalification.py",
                ],
                "host_version": "latest-compatible",
                "host_revision": "unavailable",
                "frontend_version": "latest-compatible",
                "frontend_revision": "unavailable",
                "node_api": "UNDECLARED",
                "prompt_socket": "STRING",
                "evidence_kind": "structural_fixture",
            },
            {
                "profile_id": "fixed-h3-generation",
                "route": "h3",
                "disposition": "unavailable",
                "claim_ceiling": "no_fixed_h3_generation_claim",
                "qualified": False,
                "native_required": True,
                "evidence_refs": [
                    "governance/contracts/fixed_h3_generation_v1.schema.json",
                    "tests/fixtures/m14_05_fixed_h3_generation.json",
                ],
                "host_version": "0.30.0",
                "host_revision": "c44dea18809e3ca0e12e25cbe6938c2c45a29c9d",
                "frontend_version": "1.47.12",
                "frontend_revision": "f339c6b2ec0cc90e1c4fa717d9f7d6d565a8f6da",
                "node_api": "V1_ONLY",
                "prompt_socket": "STRING",
                "evidence_kind": "generation_disposition",
            },
            {
                "profile_id": "official-oracle",
                "route": "oracle",
                "disposition": "unavailable",
                "claim_ceiling": "no_official_oracle_claim",
                "qualified": False,
                "native_required": False,
                "evidence_refs": ["tests/test_comparative_evaluation.py"],
                "host_version": "unavailable",
                "host_revision": "unavailable",
                "frontend_version": "unavailable",
                "frontend_revision": "unavailable",
                "node_api": "UNAVAILABLE",
                "prompt_socket": "STRING",
                "evidence_kind": "oracle_disposition",
            },
            {
                "profile_id": "ollama-live",
                "route": "ollama",
                "disposition": "unavailable",
                "claim_ceiling": "no_live_ollama_claim",
                "qualified": False,
                "native_required": False,
                "evidence_refs": [
                    "comfyui_h3_context/contracts/prompt_model_profiles_v5.json",
                    "tests/test_provider_policy.py",
                ],
                "host_version": "unavailable",
                "host_revision": "unavailable",
                "frontend_version": "unavailable",
                "frontend_revision": "unavailable",
                "node_api": "UNAVAILABLE",
                "prompt_socket": "STRING",
                "evidence_kind": "provider_disposition",
            },
        ],
        "criteria": criteria,
        "regressions": regressions,
        "execution_boundary": {
            "network_contacted": False,
            "provider_enabled": False,
            "model_loaded": False,
            "media_opened": False,
            "credentials_used": False,
        },
        "artifact_boundary": {
            "clean_install": True,
            "coinstallation": True,
            "rollback": True,
            "one_entry_bundle": True,
            "runtime_entry_count": 1,
            "runtime_entries": ["h3-context-sidebar.js"],
            "artifact_kinds": ["sdist", "direct_wheel", "wheel_from_sdist"],
            "network": "disabled",
            "provider_absence": True,
            "artifact_hashes_bound": True,
            "release_matrix_bound": True,
            "evidence_kind": "release_artifact",
            "evidence_refs": [
                "scripts/build_gate_report.py",
                "scripts/release_matrix.py",
                "scripts/frontend_build_report.py",
            ],
        },
        "skip_debt": {
            "approved_count": 5,
            "unapproved_count": 0,
            "items": [
                {
                    "skip_id": f"symlink_guard_{index}",
                    "reason": "governed contained-symlink fixture guard",
                    "status": "approved",
                }
                for index in range(1, 6)
            ],
        },
        "substitution_guards": {
            "native_stripped_qualifies": False,
            "fallback_satisfies_native": False,
            "latest_satisfies_supported": False,
            "ollama_satisfies_native": False,
            "generic_registration_satisfies_native": False,
            "unavailable_scored_as_pass": False,
        },
    }
    matrix["fingerprint"] = canonical_fingerprint(matrix)
    if validate:
        validate_matrix(matrix, repo_root=repo_root, frozen=False)
    return matrix


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--matrix", type=Path, default=ROOT / "tests/fixtures/m16_02_reconstruction_regression.json"
    )
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.write:
            matrix = build_matrix(args.repo_root.resolve())
            args.matrix.parent.mkdir(parents=True, exist_ok=True)
            args.matrix.write_bytes(_json_bytes(matrix))
        matrix = load_matrix(args.matrix)
        fingerprint = validate_matrix(matrix, repo_root=args.repo_root.resolve())
    except (OSError, ReconstructionRegressionError) as exc:
        print(f"M16-02 reconstruction regression matrix: FAIL: {exc}")
        return 1
    print(
        json.dumps({"schema": SCHEMA, "status": "PASS", "fingerprint": fingerprint}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
