"""Validate and run the M15-12 open-box RED acceptance contract.

The RED lane is deliberately isolated from the ordinary Vitest include.  A non-zero Vitest exit
is evidence that the accepted predecessor still exhibits the contract defect; setup failures and
unexpected passes remain failures and are never silently promoted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# CRITICAL: direct script execution must import this exact worktree, not a stale installed package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    UnsafePathError,
    prepare_regular_output,
    read_regular_file_bytes,
)

DEFAULT_CONTRACT = ROOT / "tests" / "fixtures" / "m15_12_open_box_contract.json"
DEFAULT_REPORT = ROOT / ".planning" / "260811-M15-12_OPEN_BOX_RED_REPORT.json"
RED_SCHEMA = "h3-context-open-box-red/1"
CONTRACT_SCHEMA = "h3-context-open-box-contract/1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OID_RE = re.compile(r"^[0-9a-f]{40}$")
ABSOLUTE_PATH_RE = re.compile(
    r"""(?ix)
    (?:
        \b[A-Z]:[\\/][^\s\"'<>]*
      | \\\\[^\s\"'<>]+
      | //[^\s\"'<>]+
      | (?<!:)/(?!/)[^\s\"'<>]+
    )
    """
)
MAX_JSON_BYTES = 2_000_000

_SENSITIVE_MARKERS = (
    "http://",
    "https://",
    "file://",
    "file:",
    "data:",
    "signed_url",
    "signed url",
    "signed-resource",
    "signed resource",
    "signed-url",
    "signed_resource",
    "api_key",
    "api key",
    "api-key",
    "password",
    "cookie",
    "authorization",
    "credential",
    "token=",
    "token:",
    "secret=",
    "secret:",
    "private_media",
    "private media",
    "private-media",
    "local_path",
    "local path",
    "local-path",
    "raw_prompt",
    "raw prompt",
    "raw-prompt",
    "raw_media",
    "raw media",
    "raw-media",
    "raw_provider",
    "raw provider",
    "raw-provider",
    "raw_payload",
    "raw payload",
    "raw-payload",
    "provider_payload",
    "provider payload",
    "provider-payload",
    "provider_content",
    "provider content",
    "provider-content",
)

_SOURCE_PATHS = (
    "frontend/src/components/H3Sidebar.tsx",
    "frontend/src/state/shellState.ts",
    "frontend/src/entry.tsx",
    "frontend/src/styles/tokens.css",
    "frontend/red-tests/m15_12_open_box_contract.red.test.tsx",
    "frontend/vitest.m15-12-red.config.ts",
    "scripts/m15_12_open_box_contract.py",
    "tests/fixtures/m15_12_open_box_contract.json",
    "tests/test_m15_12_open_box_contract.py",
)

_CONTRACT_KEYS = frozenset(
    {
        "schema",
        "item",
        "state_model",
        "forbidden_normal_states",
        "acceptance_semantics",
        "geometry",
        "criteria",
        "privacy",
    }
)
_ACCEPTANCE_SEMANTICS_KEYS = frozenset(
    {
        "evidence_layers",
        "final_owner",
        "forbidden_substitutes",
        "legacy_gap_sources",
        "required_join_fields",
        "review_scope",
    }
)
_GEOMETRY_KEYS = frozenset({"minimum_panel_px", "pairs", "metrics"})
_PAIR_KEYS = frozenset({"viewport_width", "panel_width"})
_PRIVACY_KEYS = frozenset({"network", "provider", "media", "storage", "sensitive_content"})
_CRITERION_KEYS = frozenset(
    {
        "id",
        "test_id",
        "surface",
        "expected_state",
        "required_roles",
        "forbidden_roles",
        "required_count",
        "required_action",
        "transition",
        "evidence_kinds",
        "geometry_case",
        "privacy_assertion",
        "retained_artifact",
        "forbidden_copy",
        "graph_mutations",
        "queue_calls",
        "projection",
        "cleanup",
        "red_expected",
        "later_owner",
    }
)

_EXPECTED_ACCEPTANCE_SEMANTICS = {
    "evidence_layers": ["reproduce", "pin", "sweep"],
    "final_owner": "M15-18",
    "forbidden_substitutes": [
        "prose_only",
        "screenshot_only",
        "dom_existence_only",
        "full_gate_only",
        "viewport_equals_panel_only",
        "prior_blocker_review_only",
    ],
    "legacy_gap_sources": ["M15-03", "M15-04", "M15-09", "M15-10", "M15-11"],
    "required_join_fields": [
        "criterion_id",
        "test_id",
        "transition",
        "dom_roles_actions",
        "graph_mutations",
        "queue_calls",
        "projection",
        "geometry",
        "privacy",
        "cleanup",
        "retained_artifact",
    ],
    "review_scope": "every_criterion",
}

# The fixture is public contract data, but these core values stay independently pinned here so a
# coordinated fixture/test edit cannot silently redefine the product and still validate itself.
_EXPECTED_CRITERION_CORE = (
    (
        "empty_open",
        "interactive",
        ("textbox", "button"),
        "start_h3_app_mode",
        ("open_sidebar", "interactive"),
        ("interaction", "dom", "state_transition"),
        "not_applicable",
        "none_before_start",
        "mount_destroy_idempotent",
        "M15-13",
    ),
    (
        "native_preference",
        "interactive",
        ("textbox", "button"),
        "return_to_h3_app_mode",
        ("interactive", "choose_native_manual", "interactive"),
        ("interaction", "dom", "state_transition", "graph"),
        "not_applicable",
        "unchanged",
        "entry_remains_mounted",
        "M15-13",
    ),
    (
        "fallback_body",
        "interactive",
        ("textbox", "button"),
        "start_h3_app_mode",
        ("interactive", "render", "interactive"),
        ("dom", "source_contract"),
        "not_applicable",
        "pending_is_not_error",
        "normal_mount_cleanup",
        "M15-13",
    ),
    (
        "base_mode_choice",
        "interactive",
        ("textbox", "button"),
        "start_h3_app_mode",
        ("interactive", "render_base_capability", "interactive"),
        ("dom", "source_contract"),
        "not_applicable",
        "none_before_start",
        "normal_mount_cleanup",
        "M15-13",
    ),
    (
        "cancelled",
        "interactive",
        ("status",),
        "restart_h3_app_mode",
        ("working", "cancel", "interactive"),
        ("interaction", "dom", "state_transition", "graph"),
        "not_applicable",
        "none",
        "abort_and_restore",
        "M15-13",
    ),
    (
        "dirty_canvas",
        "interactive",
        ("button",),
        "keep_current_canvas",
        ("dirty_canvas", "choose_keep", "interactive"),
        ("interaction", "dom", "state_transition", "graph"),
        "not_applicable",
        "unchanged",
        "no_mutation_on_cancel",
        "M15-13",
    ),
    (
        "stage_information_architecture",
        "interactive",
        ("tab", "tabpanel", "button"),
        "stage_specific_next_action",
        ("interactive", "switch_stage", "interactive"),
        ("interaction", "dom", "state_transition"),
        "not_applicable",
        "none_before_start",
        "normal_mount_cleanup",
        "M15-13",
    ),
    (
        "real_error",
        "error",
        ("alert", "button"),
        "retry_h3_app_mode",
        ("working", "queue_failure", "error", "retry", "interactive"),
        ("interaction", "dom", "state_transition", "graph"),
        "not_applicable",
        "none",
        "abort_and_restore",
        "M15-13",
    ),
    (
        "panel_geometry",
        "interactive",
        ("textbox", "button"),
        "primary_action_visible",
        ("wide_viewport_narrow_panel", "resize_panel", "interactive"),
        ("container_geometry", "interaction", "visual", "source_contract"),
        "full_matrix",
        "none_before_start",
        "owner_values_restored",
        "M15-14",
    ),
)
_EXPECTED_OPTIONAL_FIELDS: tuple[dict[str, Any], ...] = (
    {},
    {},
    {
        "forbidden_copy": [
            "Prompt export awaiting projection",
            "Assisted reconstruction unavailable",
        ]
    },
    {"forbidden_roles": ["combobox"]},
    {"forbidden_roles": ["alert"]},
    {},
    {"required_count": 5},
    {},
    {},
)
_REPORT_KEYS = frozenset(
    {
        "schema",
        "status",
        "item",
        "candidate",
        "public_worktree",
        "contract_sha256",
        "source_fingerprints",
        "command",
        "exit_code",
        "failure_count",
        "first_failure",
        "criteria",
        "output",
    }
)


class ContractError(ValueError):
    """Raised when the closed M15-12 contract or report is invalid."""


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ContractError(f"non-finite JSON value: {value}")


def load_contract(path: Path) -> dict[str, Any]:
    """Read one strict UTF-8 JSON contract/report input."""

    try:
        raw = read_regular_file_bytes(path, maximum_bytes=MAX_JSON_BYTES)
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except (OSError, UnsafePathError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read strict JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"JSON root must be an object: {path}")
    return value


def _require_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ContractError(f"{label} has unknown members: {sorted(unknown)}")
    if missing:
        raise ContractError(f"{label} is missing members: {sorted(missing)}")


def _require_string(value: Any, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{label} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ContractError(f"{label} has an invalid format")
    return value


def _require_string_list(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ContractError(f"{label} must be a non-empty string list")
    return list(value)


def _require_integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ContractError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{label} is below the minimum")
    return value


def _contains_sensitive(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS) or bool(
        ABSOLUTE_PATH_RE.search(value)
    )


def _reject_sensitive(value: str, label: str) -> None:
    if _contains_sensitive(value):
        raise ContractError(f"{label} contains forbidden sensitive/locator content")


def _validate_criterion(entry: Any, index: int) -> None:
    label = f"criteria[{index}]"
    if not isinstance(entry, dict):
        raise ContractError(f"{label} must be an object")
    required_keys = frozenset(
        {
            "id",
            "test_id",
            "surface",
            "expected_state",
            "required_roles",
            "required_action",
            "transition",
            "evidence_kinds",
            "geometry_case",
            "privacy_assertion",
            "retained_artifact",
            "graph_mutations",
            "queue_calls",
            "projection",
            "cleanup",
            "red_expected",
            "later_owner",
        }
    )
    unknown = set(entry) - _CRITERION_KEYS
    missing = required_keys - set(entry)
    if unknown:
        raise ContractError(f"{label} has unknown members: {sorted(unknown)}")
    if missing:
        raise ContractError(f"{label} is missing members: {sorted(missing)}")
    _require_string(entry["id"], f"{label}.id", pattern=re.compile(r"^AC-M15-12-\d{2}$"))
    _require_string(
        entry["test_id"],
        f"{label}.test_id",
        pattern=re.compile(r"^RED-M15-12-\d{2}$"),
    )
    _require_string(entry["surface"], f"{label}.surface")
    _require_string(entry["expected_state"], f"{label}.expected_state")
    _require_string_list(entry["required_roles"], f"{label}.required_roles")
    _require_string_list(entry["transition"], f"{label}.transition")
    _require_string_list(entry["evidence_kinds"], f"{label}.evidence_kinds")
    _require_string(entry["geometry_case"], f"{label}.geometry_case")
    _require_string(entry["privacy_assertion"], f"{label}.privacy_assertion")
    _require_string(entry["retained_artifact"], f"{label}.retained_artifact")
    if "forbidden_roles" in entry:
        _require_string_list(entry["forbidden_roles"], f"{label}.forbidden_roles")
    if "required_count" in entry:
        _require_integer(entry["required_count"], f"{label}.required_count", minimum=1)
    _require_string(entry["required_action"], f"{label}.required_action")
    if "forbidden_copy" in entry:
        forbidden_copy = _require_string_list(entry["forbidden_copy"], f"{label}.forbidden_copy")
        for copy in forbidden_copy:
            _reject_sensitive(copy, f"{label}.forbidden_copy")
    _require_integer(entry["graph_mutations"], f"{label}.graph_mutations", minimum=0)
    _require_integer(entry["queue_calls"], f"{label}.queue_calls", minimum=0)
    _require_string(entry["projection"], f"{label}.projection")
    _require_string(entry["cleanup"], f"{label}.cleanup")
    if entry["red_expected"] is not True:
        raise ContractError(f"{label}.red_expected must be true for M15-12")
    _require_string(
        entry["later_owner"], f"{label}.later_owner", pattern=re.compile(r"^M15-\d{2}$")
    )

    (
        surface,
        expected_state,
        required_roles,
        required_action,
        transition,
        evidence_kinds,
        geometry_case,
        projection,
        cleanup,
        later_owner,
    ) = _EXPECTED_CRITERION_CORE[index]
    expected_core = {
        "id": f"AC-M15-12-{index + 1:02d}",
        "test_id": f"RED-M15-12-{index + 1:02d}",
        "surface": surface,
        "expected_state": expected_state,
        "required_roles": list(required_roles),
        "required_action": required_action,
        "transition": list(transition),
        "evidence_kinds": list(evidence_kinds),
        "geometry_case": geometry_case,
        "privacy_assertion": "closed_boundary",
        "retained_artifact": "m15_12_red_report",
        "graph_mutations": 0,
        "queue_calls": 0,
        "projection": projection,
        "cleanup": cleanup,
        "red_expected": True,
        "later_owner": later_owner,
    }
    for field, expected in expected_core.items():
        if entry[field] != expected:
            suffix = "transition" if field == "transition" else "semantic contract"
            raise ContractError(f"{label}.{field} violates the pinned {suffix}")
    optional_fields = {
        field: entry[field]
        for field in entry
        if field in {"forbidden_roles", "required_count", "forbidden_copy"}
    }
    if optional_fields != _EXPECTED_OPTIONAL_FIELDS[index]:
        raise ContractError(f"{label} optional members violate the pinned semantic contract")


def validate_contract(contract: Mapping[str, Any]) -> None:
    """Validate the closed public M15-12 fixture."""

    _require_keys(contract, _CONTRACT_KEYS, "contract")
    if contract["schema"] != CONTRACT_SCHEMA:
        raise ContractError("contract schema is unsupported")
    if contract["item"] != "M15-12":
        raise ContractError("contract item must be M15-12")
    states = _require_string_list(contract["state_model"], "state_model")
    if states != ["interactive", "working", "projected", "error"]:
        raise ContractError("state_model is not the closed four-state model")
    forbidden = _require_string_list(contract["forbidden_normal_states"], "forbidden_normal_states")
    if forbidden != ["node-only", "awaiting_projection", "generic_fallback"]:
        raise ContractError("forbidden_normal_states is incomplete")

    acceptance_semantics = contract["acceptance_semantics"]
    if not isinstance(acceptance_semantics, dict):
        raise ContractError("acceptance_semantics must be an object")
    _require_keys(
        acceptance_semantics,
        _ACCEPTANCE_SEMANTICS_KEYS,
        "acceptance_semantics",
    )
    if acceptance_semantics != _EXPECTED_ACCEPTANCE_SEMANTICS:
        raise ContractError("acceptance_semantics does not close the legacy evidence gaps")

    geometry = contract["geometry"]
    if not isinstance(geometry, dict):
        raise ContractError("geometry must be an object")
    _require_keys(geometry, _GEOMETRY_KEYS, "geometry")
    minimum = _require_integer(
        geometry["minimum_panel_px"], "geometry.minimum_panel_px", minimum=480
    )
    if minimum != 480:
        raise ContractError("geometry.minimum_panel_px must preserve the provisional 480px floor")
    pairs = geometry["pairs"]
    if not isinstance(pairs, list) or not pairs:
        raise ContractError("geometry.pairs must be a non-empty list")
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            raise ContractError(f"geometry.pairs[{index}] must be an object")
        _require_keys(pair, _PAIR_KEYS, f"geometry.pairs[{index}]")
        _require_integer(
            pair["viewport_width"], f"geometry.pairs[{index}].viewport_width", minimum=320
        )
        panel = _require_integer(
            pair["panel_width"], f"geometry.pairs[{index}].panel_width", minimum=minimum
        )
        if panel < minimum:
            raise ContractError(f"geometry.pairs[{index}] is below the floor")
    expected_pairs = [
        {"viewport_width": 1280, "panel_width": 480},
        {"viewport_width": 1440, "panel_width": 480},
        {"viewport_width": 1440, "panel_width": 560},
        {"viewport_width": 1440, "panel_width": 640},
        {"viewport_width": 480, "panel_width": 480},
    ]
    if pairs != expected_pairs:
        raise ContractError(
            "geometry.pairs does not preserve the independent viewport/panel matrix"
        )
    metrics = _require_string_list(geometry["metrics"], "geometry.metrics")
    if metrics != [
        "no_horizontal_overflow",
        "visible_primary_action",
        "readable_control_label",
        "focus_contained",
    ]:
        raise ContractError("geometry.metrics is incomplete")

    privacy = contract["privacy"]
    if not isinstance(privacy, dict):
        raise ContractError("privacy must be an object")
    _require_keys(privacy, _PRIVACY_KEYS, "privacy")
    expected_privacy = {
        "network": "disabled",
        "provider": "absent",
        "media": "synthetic_only",
        "storage": "unused",
        "sensitive_content": "forbidden",
    }
    if privacy != expected_privacy:
        raise ContractError("privacy boundary is not closed")

    criteria = contract["criteria"]
    if not isinstance(criteria, list) or len(criteria) != 9:
        raise ContractError("criteria must contain exactly nine entries")
    criterion_ids: set[str] = set()
    test_ids: set[str] = set()
    for index, entry in enumerate(criteria):
        if isinstance(entry, dict):
            raw_criterion_id = entry.get("id")
            raw_test_id = entry.get("test_id")
            if (isinstance(raw_criterion_id, str) and raw_criterion_id in criterion_ids) or (
                isinstance(raw_test_id, str) and raw_test_id in test_ids
            ):
                raise ContractError("duplicate criterion or test id")
        _validate_criterion(entry, index)
        criterion_id = entry["id"]
        test_id = entry["test_id"]
        criterion_ids.add(criterion_id)
        test_ids.add(test_id)
        for field in ("surface", "expected_state", "projection", "cleanup", "later_owner"):
            _reject_sensitive(entry[field], f"criteria[{index}].{field}")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def contract_digest(contract: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractError("path escapes repository") from exc
    return path


def _safe_report_path(root: Path, supplied: str) -> Path:
    path = _safe_path(root, supplied)
    planning_root = (root / ".planning").resolve()
    try:
        path.relative_to(planning_root)
    except ValueError as exc:
        raise ContractError(
            "report path must remain under the ignored .planning directory"
        ) from exc
    if path == planning_root or path.suffix.casefold() != ".json":
        raise ContractError("report path must name one JSON file under .planning")
    return path


def source_fingerprints(root: Path = ROOT) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in _SOURCE_PATHS:
        path = _safe_path(root, relative)
        try:
            raw = read_regular_file_bytes(path, maximum_bytes=MAX_JSON_BYTES)
        except (OSError, UnsafePathError) as exc:
            raise ContractError(f"source fingerprint cannot read {relative}: {exc}") from exc
        result[relative] = _sha256_bytes(raw)
    return result


def redact_text(value: str, root: Path = ROOT) -> str:
    """Remove paths, locators, and private-content markers from command output."""

    text = value.replace(str(root), "[WORKSPACE]")
    text = text.replace(str(root).replace("\\", "/"), "[WORKSPACE]")
    text = re.sub(r"(?i)\b(?:https?|file|data):[^\s\"'<>]+", "[REDACTED_LOCATOR]", text)
    text = ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", text)
    for marker in sorted(_SENSITIVE_MARKERS, key=len, reverse=True):
        text = re.sub(re.escape(marker), "[REDACTED]", text, flags=re.IGNORECASE)
    return text


def classify_red_exit(exit_code: int, output: str) -> str:
    lowered = output.casefold()
    if exit_code == 0:
        return "UNEXPECTED_PASS"
    if exit_code != 1:
        return "SETUP_ERROR"
    if any(
        marker in lowered
        for marker in (
            "cannot find module",
            "module not found",
            "failed to load config",
            "command not found",
            "no such file",
            "error loading",
            "invalid chai property",
            "failed to resolve import",
            "transform failed",
            "the url must be of scheme",
        )
    ):
        return "SETUP_ERROR"
    return "RED_REPRODUCED"


def _require_sha(value: Any, label: str) -> str:
    return _require_string(value, label, pattern=SHA256_RE)


def validate_report(
    report: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    expected_candidate: str | None = None,
    require_clean: bool = True,
) -> None:
    validate_contract(contract)
    if not isinstance(report, dict):
        raise ContractError("report must be an object")
    _require_keys(report, _REPORT_KEYS, "report")
    if report["schema"] != RED_SCHEMA:
        raise ContractError("report schema is unsupported")
    if report["status"] != "RED_REPRODUCED":
        raise ContractError("report is not RED_REPRODUCED")
    if report["item"] != "M15-12":
        raise ContractError("report item must be M15-12")
    candidate = _require_string(report["candidate"], "report.candidate", pattern=OID_RE)
    if expected_candidate is not None and candidate != expected_candidate:
        raise ContractError("report candidate does not match expected candidate")
    _require_sha(report["contract_sha256"], "report.contract_sha256")
    if report["contract_sha256"] != contract_digest(contract):
        raise ContractError("report contract fingerprint is stale")
    fingerprints = report["source_fingerprints"]
    if not isinstance(fingerprints, dict) or not fingerprints:
        raise ContractError("report.source_fingerprints must be non-empty")
    for path, digest in fingerprints.items():
        _require_string(path, "report.source_fingerprints.path")
        _require_sha(digest, f"report.source_fingerprints[{path}]")
    expected_fingerprints = source_fingerprints(ROOT)
    if fingerprints != expected_fingerprints:
        raise ContractError("report source fingerprint inventory is stale or incomplete")
    command = report["command"]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
    ):
        raise ContractError("report.command must be a non-empty string list")
    for index, value in enumerate(command):
        _reject_sensitive(value, f"report.command[{index}]")
    expected_command = [
        _pnpm_executable(),
        "--dir",
        "frontend",
        "exec",
        "vitest",
        "run",
        "--config",
        "vitest.m15-12-red.config.ts",
    ]
    if command != expected_command:
        raise ContractError("report.command is not the dedicated M15-12 RED lane")
    exit_code = report["exit_code"]
    if type(exit_code) is not int or exit_code != 1:
        raise ContractError("report.exit_code must be exactly 1 for a reproduced RED assertion")
    expected_failure_count = len(contract["criteria"])
    if (
        type(report["failure_count"]) is not int
        or report["failure_count"] != expected_failure_count
    ):
        raise ContractError(
            f"report.failure_count must equal the {expected_failure_count} contract criteria"
        )
    first_failure = _require_string(report["first_failure"], "report.first_failure")
    output = _require_string(report["output"], "report.output")
    _reject_sensitive(first_failure, "report.first_failure")
    _reject_sensitive(output, "report.output")
    criteria = report["criteria"]
    if not isinstance(criteria, list):
        raise ContractError("report.criteria must be a list")
    expected_sequence = [entry["test_id"] for entry in contract["criteria"]]
    expected_ids = set(expected_sequence)
    if first_failure not in expected_ids:
        raise ContractError("report.first_failure is not a contract test id")
    observed_ids: set[str] = set()
    observed_sequence: list[str] = []
    for index, entry in enumerate(criteria):
        if not isinstance(entry, dict) or set(entry) != {"test_id", "status"}:
            raise ContractError(f"report.criteria[{index}] has invalid members")
        test_id = _require_string(entry["test_id"], f"report.criteria[{index}].test_id")
        if entry["status"] != "RED" or test_id in observed_ids:
            raise ContractError("report criteria status or duplicate is invalid")
        observed_ids.add(test_id)
        observed_sequence.append(test_id)
    if observed_sequence != expected_sequence:
        raise ContractError("report criteria does not match contract")
    if type(report["public_worktree"]) is not bool:
        raise ContractError("report.public_worktree must be boolean")
    if require_clean and report["public_worktree"] is not True:
        raise ContractError("report.public_worktree must be true for exact-candidate evidence")


def _git_output(*args: str, root: Path = ROOT) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _pnpm_executable() -> str:
    """Return an executable name that subprocess can resolve on the host OS.

    IMPORTANT: Windows exposes pnpm through a PowerShell shim as well as pnpm.cmd;
    subprocess without a shell cannot launch the .ps1 shim, so use the cmd shim.
    """

    return "pnpm.cmd" if os.name == "nt" else "pnpm"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    try:
        target = prepare_regular_output(path)
    except UnsafePathError as exc:
        raise ContractError(f"report output path is unsafe: {exc}") from exc
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_red(contract_path: Path, report_path: Path) -> dict[str, Any]:
    contract_path = _safe_path(ROOT, str(contract_path))
    report_path = _safe_report_path(ROOT, str(report_path))
    contract = load_contract(contract_path)
    validate_contract(contract)
    command = [
        _pnpm_executable(),
        "--dir",
        "frontend",
        "exec",
        "vitest",
        "run",
        "--config",
        "vitest.m15-12-red.config.ts",
    ]
    environment = os.environ.copy()
    environment.update(
        {"CI": "1", "H3_CONTEXT_NETWORK_DISABLED": "1", "npm_config_offline": "true"}
    )
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        raw_output = f"{completed.stdout}\n{completed.stderr}".strip()
        exit_code = completed.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        raw_output = str(exc)
        exit_code = 127
    sanitized = redact_text(raw_output)[:16_000] or "[no output]"
    _reject_sensitive(sanitized, "red runner output")
    classification = classify_red_exit(exit_code, raw_output)
    test_ids = [entry["test_id"] for entry in contract["criteria"]]
    missing = [test_id for test_id in test_ids if test_id not in raw_output]
    failure_match = re.search(r"Tests\s+(\d+)\s+failed", raw_output)
    failure_count = (
        int(failure_match.group(1)) if failure_match else max(1, len(test_ids) - len(missing))
    )
    if classification == "RED_REPRODUCED" and (missing or failure_count != len(test_ids)):
        classification = "FAIL"
    observed_positions = [
        (raw_output.find(test_id), test_id) for test_id in test_ids if test_id in raw_output
    ]
    first_failure = min(observed_positions)[1] if observed_positions else "vitest_exit"
    report: dict[str, Any] = {
        "schema": RED_SCHEMA,
        "status": classification,
        "item": "M15-12",
        "candidate": _git_output("rev-parse", "HEAD"),
        "public_worktree": not bool(_git_output("status", "--porcelain")),
        "contract_sha256": contract_digest(contract),
        "source_fingerprints": source_fingerprints(ROOT),
        "command": command,
        "exit_code": exit_code,
        "failure_count": failure_count,
        "first_failure": first_failure,
        "criteria": [{"test_id": test_id, "status": "RED"} for test_id in test_ids],
        "output": sanitized,
    }
    if classification == "RED_REPRODUCED":
        validate_report(
            report,
            contract,
            expected_candidate=_git_output("rev-parse", "HEAD"),
            require_clean=False,
        )
    _write_json(report_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--run-red", action="store_true")
    parser.add_argument("--check", action="store_true")
    return parser


def _report_display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise ContractError("report path escapes repository") from exc


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.run_red == args.check:
        raise ContractError("choose exactly one of --run-red or --check")
    contract_path = _safe_path(ROOT, str(args.contract))
    report_path = _safe_report_path(ROOT, str(args.report))
    contract = load_contract(contract_path)
    validate_contract(contract)
    if args.run_red:
        report = run_red(contract_path, report_path)
        print(
            json.dumps(
                {
                    "schema": RED_SCHEMA,
                    "status": report["status"],
                    "report": _report_display_path(report_path),
                },
                indent=2,
            )
        )
        return 0 if report["status"] == "RED_REPRODUCED" else 1
    report = load_contract(report_path)
    expected_candidate = _git_output("rev-parse", "HEAD")
    validate_report(
        report,
        contract,
        expected_candidate=expected_candidate,
        require_clean=False,
    )
    if _git_output("status", "--porcelain"):
        raise ContractError(
            "current public worktree is not clean; exact-candidate check is blocked"
        )
    if report["public_worktree"] is not True:
        raise ContractError("report.public_worktree must be true for exact-candidate evidence")
    print(
        json.dumps(
            {
                "schema": RED_SCHEMA,
                "status": "PASS",
                "report": _report_display_path(report_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        print(f"M15-12 OPEN-BOX CONTRACT: FAIL: {redact_text(str(exc))}", file=sys.stderr)
        raise SystemExit(1) from exc
