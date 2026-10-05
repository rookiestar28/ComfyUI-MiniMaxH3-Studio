"""Bounded reading of an untrusted workflow document, and the audit it produces.

Every limit this module enforces is a refusal, not a truncation: a document past the byte ceiling,
the depth ceiling, the item ceiling or the string ceiling is rejected before anything looks at what
it says. `_walk_json` is the only path in, so there is no second reader with different bounds.

`_member_error` exists because the obvious error message -- "key X is wrong" -- would echo an
untrusted key straight into a record. It names the position instead.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

MIGRATION_AUDIT_SCHEMA = "h3-context-workflow-migration-audit/1"


MAX_WORKFLOW_BYTES = 2 * 1024 * 1024


MAX_WORKFLOW_DEPTH = 14


MAX_WORKFLOW_ITEMS = 4096


MAX_WORKFLOW_STRING = 32768


_PRIVATE_KEY = re.compile(
    r"(?:token|credential|password|secret|authorization|bearer|api[_-]?key|private|local[_-]?path|url)",
    re.IGNORECASE,
)


class WorkflowMigrationError(ValueError):
    """Raised when a serialized migration input is not bounded JSON-safe data."""


class MigrationDisposition(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class WorkflowMigrationAudit:
    """Immutable, content-free migration disposition."""

    disposition: MigrationDisposition
    reason_code: str | None
    next_action: str
    workflow_kind: str
    migration_policy: str
    schema: str = MIGRATION_AUDIT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MIGRATION_AUDIT_SCHEMA:
            raise WorkflowMigrationError("migration audit schema drifted")
        if self.disposition is MigrationDisposition.ACCEPTED:
            if self.reason_code is not None or self.next_action != "queue_after_host_validation":
                raise WorkflowMigrationError("accepted migration disposition is malformed")
        elif not self.reason_code or self.next_action == "queue_after_host_validation":
            raise WorkflowMigrationError("rejected migration disposition is malformed")

    def to_wire(self) -> dict[str, str | None]:
        return {
            "schema": self.schema,
            "disposition": self.disposition.value,
            "reason_code": self.reason_code,
            "next_action": self.next_action,
            "workflow_kind": self.workflow_kind,
            "migration_policy": self.migration_policy,
        }


def _walk_json(value: object, *, depth: int = 0, items: list[int] | None = None) -> None:
    if items is None:
        items = [0]
    if depth > MAX_WORKFLOW_DEPTH:
        raise WorkflowMigrationError("workflow JSON exceeds the depth limit")
    items[0] += 1
    if items[0] > MAX_WORKFLOW_ITEMS:
        raise WorkflowMigrationError("workflow JSON exceeds the item limit")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise WorkflowMigrationError("workflow JSON contains non-finite number")
        return
    if isinstance(value, str):
        if len(value) > MAX_WORKFLOW_STRING:
            raise WorkflowMigrationError("workflow JSON string exceeds the limit")
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_WORKFLOW_ITEMS:
            raise WorkflowMigrationError("workflow JSON mapping exceeds the limit")
        for key, child in value.items():
            if type(key) is not str:
                raise WorkflowMigrationError("workflow JSON keys must be strings")
            _walk_json(child, depth=depth + 1, items=items)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_WORKFLOW_ITEMS:
            raise WorkflowMigrationError("workflow JSON list exceeds the limit")
        for child in value:
            _walk_json(child, depth=depth + 1, items=items)
        return
    raise WorkflowMigrationError("workflow JSON contains an unsupported value")


def decode_workflow_json(value: str | bytes) -> dict[str, object]:
    """Decode strict JSON with recursive duplicate/non-finite rejection."""

    raw = value.encode("utf-8") if isinstance(value, str) else value
    if type(raw) is not bytes or len(raw) > MAX_WORKFLOW_BYTES:
        raise WorkflowMigrationError("workflow JSON exceeds the byte limit")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if type(key) is not str or key in result:
                raise WorkflowMigrationError("duplicate JSON member")
            result[key] = item
        return result

    def constant(token: str) -> object:
        raise WorkflowMigrationError(f"workflow JSON contains non-finite value: {token}")

    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except WorkflowMigrationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise WorkflowMigrationError("workflow JSON is invalid") from exc
    _walk_json(decoded)
    if not isinstance(decoded, dict):
        raise WorkflowMigrationError("workflow root must be an object")
    return decoded


def _accepted(kind: str, policy: str) -> WorkflowMigrationAudit:
    return WorkflowMigrationAudit(
        MigrationDisposition.ACCEPTED,
        None,
        "queue_after_host_validation",
        kind,
        policy,
    )


def _rejected(kind: str, policy: str, reason: str, action: str) -> WorkflowMigrationAudit:
    return WorkflowMigrationAudit(MigrationDisposition.REJECTED, reason, action, kind, policy)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowMigrationError(f"{label} must be an object")
    return value


def _member_error(value: Mapping[str, object], allowed: frozenset[str]) -> str | None:
    """Return a stable rejection code instead of allowing unknown graph data through."""

    for key in value:
        if not isinstance(key, str):
            return "unknown_workflow_member"
        if key not in allowed:
            return (
                "private_workflow_field" if _PRIVATE_KEY.search(key) else "unknown_workflow_member"
            )
    return None


def _strict_int(value: object) -> bool:
    """Reject bool-as-int JSON values at graph/link boundaries."""

    return type(value) is int


def _strict_projection_pair(value: object, node_id: str, slot: int | str) -> bool:
    """Match a serialized projection pair without Python bool/int coercion."""

    return (
        isinstance(value, list)
        and len(value) == 2
        and type(value[0]) is str
        and value[0] == node_id
        and type(value[1]) is type(slot)
        and value[1] == slot
    )
