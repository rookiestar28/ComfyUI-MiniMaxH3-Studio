"""Audit the ignored, dated M9-04 official-oracle decision artifact.

The checker validates the repository decision record only. It never fetches a source, resolves a
credential, or turns a negative decision into authorization.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

SCHEMA = "h3.context.ir.governance-decision/1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
SENSITIVE_MARKERS = ("api_key", "authorization", "credential", "password", "secret", "token")
SOURCE_CATEGORIES = frozenset(
    {
        "general_terms",
        "product_terms",
        "privacy_policy",
        "api_lifecycle",
        "deletion",
        "pricing",
        "account_order",
        "supplemental",
    }
)
LANE_IDS = frozenset(
    {
        "official_live_oracle",
        "official_mocked_contract",
        "recorded_oracle_fixture",
        "benchmark",
        "publication",
        "training_distillation",
    }
)
OPERATIONS = (
    "api_use",
    "output_retention",
    "redistribution",
    "benchmarking",
    "publication",
    "automated_probing",
    "training_distillation",
)


def _has_sensitive_identifier_segment(value: str) -> bool:
    segments = tuple(part for part in re.split(r"[_.:-]+", value.casefold()) if part)
    return any(marker in segments for marker in SENSITIVE_MARKERS)


class DecisionAuditError(ValueError):
    """Raised when the dated decision artifact is incomplete or unsafe."""


JsonObject = dict[str, object]


def _object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise DecisionAuditError(f"{field} must be an object")
    return cast(JsonObject, value)


def _string(value: object, field: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise DecisionAuditError(f"{field} must be a non-empty string")
    if identifier and IDENTIFIER.fullmatch(value) is None:
        raise DecisionAuditError(f"{field} must be a bounded identifier")
    if identifier and _has_sensitive_identifier_segment(value):
        raise DecisionAuditError(f"{field} contains a sensitive marker")
    return value


def _date(value: object, field: str) -> str:
    result = _string(value, field)
    if DATE.fullmatch(result) is None:
        raise DecisionAuditError(f"{field} must be an ISO date")
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise DecisionAuditError(f"{field} must be a valid ISO date") from exc
    return result


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise DecisionAuditError(f"{field} must be a list")
    return value


def _safe_url(value: object, field: str) -> str:
    result = _string(value, field)
    try:
        parsed = urlsplit(result)
    except ValueError as exc:
        raise DecisionAuditError(f"{field} is malformed") from exc
    if parsed.scheme.casefold() != "https" or parsed.hostname is None:
        raise DecisionAuditError(f"{field} must be an HTTPS URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise DecisionAuditError(f"{field} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise DecisionAuditError(f"{field} must not contain a query or fragment")
    return result


def _validate_source(source: JsonObject, index: int) -> tuple[str, str]:
    prefix = f"sources[{index}]"
    source_id = _string(source.get("source_id"), f"{prefix}.source_id", identifier=True)
    category = _string(source.get("category"), f"{prefix}.category")
    if category not in SOURCE_CATEGORIES:
        raise DecisionAuditError(f"{prefix}.category is not governed")
    _safe_url(source.get("url"), f"{prefix}.url")
    _string(source.get("revision"), f"{prefix}.revision", identifier=True)
    effective = source.get("effective_date")
    applicability = _string(source.get("applicability"), f"{prefix}.applicability")
    if applicability not in {"applicable", "not_applicable", "unresolved"}:
        raise DecisionAuditError(f"{prefix}.applicability is invalid")
    if effective is not None:
        _date(effective, f"{prefix}.effective_date")
    elif applicability != "unresolved":
        raise DecisionAuditError(f"{prefix} has no effective date without unresolved applicability")
    _date(source.get("retrieved_at"), f"{prefix}.retrieved_at")
    digest = _string(source.get("sha256"), f"{prefix}.sha256")
    if SHA256.fullmatch(digest) is None:
        raise DecisionAuditError(f"{prefix}.sha256 is not a lowercase SHA-256 digest")
    _string(source.get("authority"), f"{prefix}.authority", identifier=True)
    _string(source.get("jurisdiction"), f"{prefix}.jurisdiction", identifier=True)
    return source_id, category


def validate_decision(document: object) -> JsonObject:
    root = _object(document, "decision")
    if root.get("schema") != SCHEMA:
        raise DecisionAuditError("decision schema is not the M9-04 governance artifact schema")
    for field in (
        "decision_id",
        "decision_revision",
        "source_ledger_id",
        "source_ledger_revision",
        "reviewer_reference",
        "approver_reference",
        "jurisdiction",
        "account_owner",
    ):
        _string(root.get(field), f"decision.{field}", identifier=True)
    _date(root.get("reviewed_at"), "decision.reviewed_at")
    unresolved = _list(root.get("unresolved_questions"), "decision.unresolved_questions")
    if not unresolved or not all(
        isinstance(item, str) and IDENTIFIER.fullmatch(item) is not None for item in unresolved
    ):
        raise DecisionAuditError("decision.unresolved_questions must contain bounded IDs")

    sources = _list(root.get("sources"), "decision.sources")
    if len(sources) != len(SOURCE_CATEGORIES):
        raise DecisionAuditError("decision must pin exactly one source for every governed category")
    source_ids: set[str] = set()
    categories: set[str] = set()
    for index, value in enumerate(sources):
        source_id, category = _validate_source(_object(value, f"sources[{index}]"), index)
        if source_id in source_ids:
            raise DecisionAuditError("source IDs must be unique")
        source_ids.add(source_id)
        categories.add(category)
    if categories != SOURCE_CATEGORIES:
        raise DecisionAuditError("source categories do not cover the governed source ledger")

    terms = _object(root.get("terms_decision"), "decision.terms_decision")
    if terms.get("status") != "do_not_implement":
        raise DecisionAuditError("unresolved terms must remain do_not_implement")
    _string(terms.get("terms_revision"), "terms_decision.terms_revision", identifier=True)
    for operation in OPERATIONS:
        if terms.get(operation) != "deny":
            raise DecisionAuditError(f"terms_decision.{operation} must be deny")
    _string(terms.get("reason"), "terms_decision.reason")

    lanes = _list(root.get("lane_dispositions"), "decision.lane_dispositions")
    if len(lanes) != len(LANE_IDS):
        raise DecisionAuditError("decision must disposition every governed lane exactly once")
    seen_lanes: set[str] = set()
    for index, value in enumerate(lanes):
        lane = _object(value, f"lane_dispositions[{index}]")
        lane_id = _string(
            lane.get("lane_id"), f"lane_dispositions[{index}].lane_id", identifier=True
        )
        if lane_id in seen_lanes or lane_id not in LANE_IDS:
            raise DecisionAuditError("lane IDs must be unique and governed")
        seen_lanes.add(lane_id)
        status = _string(lane.get("status"), f"lane_dispositions[{index}].status")
        scope = _string(lane.get("execution_scope"), f"lane_dispositions[{index}].execution_scope")
        ceiling = _string(lane.get("claim_ceiling"), f"lane_dispositions[{index}].claim_ceiling")
        retention = _string(
            lane.get("retention_policy"), f"lane_dispositions[{index}].retention_policy"
        )
        _string(
            lane.get("reason_reference"),
            f"lane_dispositions[{index}].reason_reference",
            identifier=True,
        )
        if lane_id == "official_mocked_contract":
            expected = ("limited", "mocked_only", "structural_only", "hash_only")
        else:
            expected = ("unavailable_prohibited", "offline_only", "no_claim", "none")
        if (status, scope, ceiling, retention) != expected:
            raise DecisionAuditError(f"lane {lane_id} has an unsafe downstream disposition")
    if seen_lanes != LANE_IDS:
        raise DecisionAuditError("lane dispositions do not cover every governed lane")

    consent = _object(root.get("consent"), "decision.consent")
    if consent.get("operator_consent") is not False or any(
        consent.get(field) is not None
        for field in ("consent_reference", "consent_terms_revision", "consent_policy_revision")
    ):
        raise DecisionAuditError("negative decision cannot carry consent")

    budget = _object(root.get("budget"), "decision.budget")
    if any(
        budget.get(field) != 0
        for field in (
            "max_calls",
            "max_requests_per_window",
            "window_seconds",
            "max_spend_minor_units",
            "max_output_tokens",
        )
    ):
        raise DecisionAuditError("negative decision must have zero live budget")
    if budget.get("currency") != "UNRESOLVED":
        raise DecisionAuditError("negative decision must not bind a currency")
    if root.get("decision") != "DO_NOT_IMPLEMENT_LIVE_ORACLE":
        raise DecisionAuditError("decision must prohibit live oracle implementation")
    notes = _list(root.get("notes"), "decision.notes")
    note_text = " ".join(item for item in notes if isinstance(item, str)).casefold()
    for marker in ("no live call", "cross-process", "settlement", "training/distillation"):
        if marker not in note_text:
            raise DecisionAuditError(f"decision.notes must preserve the boundary: {marker}")
    return {
        "status": "PASS",
        "decision_id": root["decision_id"],
        "source_count": len(sources),
        "lane_count": len(lanes),
        "decision": root["decision"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decision", type=Path, help="path to the dated decision JSON")
    arguments = parser.parse_args(argv)
    try:
        document = json.loads(arguments.decision.read_text(encoding="utf-8"))
        report = validate_decision(document)
    except (OSError, json.JSONDecodeError, DecisionAuditError) as exc:
        print(f"OFFICIAL ORACLE DECISION AUDIT: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    print("OFFICIAL ORACLE DECISION AUDIT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
