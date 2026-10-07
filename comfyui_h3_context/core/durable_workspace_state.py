"""Closed, no-prose metadata and non-executable recovery projections."""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from .managed_run import (
    HOST_MAY_HOLD_THE_PROMPT,
    MANAGED_RUN_MACHINE,
    ManagedRunTrigger,
    advance,
    restore,
)

STATE_SCHEMA = "h3.context.workspace_state.v1"
MAX_RECORD_BYTES = 64 * 1024
MAX_OWNER_RECORDS = 64
MAX_SNAPSHOT_BYTES = MAX_RECORD_BYTES * MAX_OWNER_RECORDS
MAX_TIMESTAMP_MS = 4_102_444_800_000
MAX_OWNER_REVISION = (1 << 53) - 1
_OWNER = re.compile(r"owner_[0-9a-f]{32}\Z")
_RECORD = re.compile(r"record_[0-9a-f]{32}\Z")
_REVISIONS = frozenset({"workspace", "reference", "timeline", "context", "transition"})
_RECORD_KEYS = frozenset(
    {
        "record_id",
        "kind",
        "created_at_ms",
        "closed_at_ms",
        "segment_count",
        "state",
        "revisions",
        "last_transition",
    }
)


class DurableStateError(ValueError):
    """A closed content-free refusal reason, suitable for route projection."""

    CODES = frozenset(
        {
            "owner_invalid",
            "record_invalid",
            "integer_invalid",
            "shape_invalid",
            "duplicate_key",
            "number_invalid",
            "json_invalid",
            "size_invalid",
            "shape_bound",
            "kind_invalid",
            "transition_invalid",
            "state_invalid",
            "version_unsupported",
            "owner_mismatch",
            "record_bound",
            "record_duplicate",
            "timestamp_invalid",
            "limits_invalid",
            "revision_invalid",
            "manifest_invalid",
            "storage_unsafe",
            "storage_unavailable",
            "quota_directory",
            "quota_owners",
            "quota_records",
            "quota_bytes",
            "owner_busy",
            "catalog_busy",
            "recovery_disabled",
            "revision_conflict",
            "revision_exhausted",
            "snapshot_corrupt",
            "snapshot_unavailable",
            "storage_unverified",
            "intent_invalid",
            "host_unqualified",
            "filesystem_unqualified",
            "owner_changed",
            "service_closed",
            "record_unavailable",
        }
    )

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in self.CODES:
            raise ValueError("unknown durable state refusal")
        super().__init__(code)


class RecordIdentityMap:
    """Bounded metadata IDs survive replacement of a live entry, but confer no authority."""

    def __init__(self, *, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self._clock_ms = clock_ms
        self._identities: dict[str, tuple[str, int]] = {}

    def reconcile(self, keys: tuple[str, ...]) -> dict[str, tuple[str, int]]:
        if len(keys) > MAX_OWNER_RECORDS or len(set(keys)) != len(keys):
            raise DurableStateError("record_bound")
        retained = {key: self._identities[key] for key in keys if key in self._identities}
        for key in keys:
            if key not in retained:
                retained[key] = ("record_" + secrets.token_hex(16), self._clock_ms())
        self._identities = retained
        return retained.copy()


def require_owner(value: object) -> str:
    if type(value) is not str or _OWNER.fullmatch(value) is None:
        raise DurableStateError("owner_invalid")
    return value


def require_record_id(value: object) -> str:
    if type(value) is not str or _RECORD.fullmatch(value) is None:
        raise DurableStateError("record_invalid")
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise DurableStateError("integer_invalid")
    return value


def _keys(value: object, expected: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise DurableStateError("shape_invalid")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise DurableStateError("duplicate_key")
        result[name] = value
    return result


def _constant(_value: str) -> object:
    raise DurableStateError("number_invalid")


def strict_json(data: bytes, *, maximum_bytes: int, maximum_depth: int = 10) -> object:
    if type(data) is not bytes or len(data) > maximum_bytes:
        raise DurableStateError("size_invalid")
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise DurableStateError("json_invalid") from error
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > maximum_depth or nodes > 4096:
            raise DurableStateError("shape_bound")
        if type(item) is dict:
            if len(item) + nodes > 4096:
                raise DurableStateError("shape_bound")
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            if len(item) + nodes > 4096:
                raise DurableStateError("shape_bound")
            pending.extend((child, depth + 1) for child in item)
    return value


def json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise DurableStateError("shape_invalid") from error


@dataclass(frozen=True, slots=True)
class StateRevisions:
    workspace: int | None
    reference: int | None
    timeline: int | None
    context: int | None
    transition: int

    def to_wire(self) -> dict[str, object]:
        return {
            "workspace": self.workspace,
            "reference": self.reference,
            "timeline": self.timeline,
            "context": self.context,
            "transition": self.transition,
        }


@dataclass(frozen=True, slots=True)
class StateTransition:
    sequence: int
    trigger: str
    source: str
    target: str
    guard: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "trigger": self.trigger,
            "source": self.source,
            "target": self.target,
            "guard": self.guard,
        }


@dataclass(frozen=True, slots=True)
class StateRecord:
    record_id: str
    kind: str
    created_at_ms: int
    closed_at_ms: int | None
    segment_count: int
    state: str
    revisions: StateRevisions
    last_transition: StateTransition | None

    def to_wire(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "created_at_ms": self.created_at_ms,
            "closed_at_ms": self.closed_at_ms,
            "segment_count": self.segment_count,
            "state": self.state,
            "revisions": self.revisions.to_wire(),
            "last_transition": None
            if self.last_transition is None
            else self.last_transition.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    owner_id: str
    revision: int
    saved_at_ms: int
    records: tuple[StateRecord, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": STATE_SCHEMA,
            "owner_id": self.owner_id,
            "revision": self.revision,
            "saved_at_ms": self.saved_at_ms,
            "records": [row.to_wire() for row in self.records],
        }


def decode_record(value: object) -> StateRecord:
    row = _keys(value, _RECORD_KEYS)
    identifier = require_record_id(row["record_id"])
    kind, state = row["kind"], row["state"]
    if type(kind) is not str or kind not in {"production", "authoring", "managed_run"}:
        raise DurableStateError("kind_invalid")
    if type(state) is not str:
        raise DurableStateError("state_invalid")
    created = _integer(row["created_at_ms"], 1, MAX_TIMESTAMP_MS)
    closed = row["closed_at_ms"]
    if closed is not None:
        closed = _integer(closed, created, MAX_TIMESTAMP_MS)
    count = _integer(row["segment_count"], 0, 64)
    raw = _keys(row["revisions"], _REVISIONS)
    values = {
        name: None if raw[name] is None else _integer(raw[name], 0, 1_000_000)
        for name in ("workspace", "reference", "timeline", "context")
    }
    sequence = _integer(raw["transition"], 0, 64)
    revisions = StateRevisions(**values, transition=sequence)
    transition = None
    if row["last_transition"] is not None:
        fact = _keys(
            row["last_transition"], frozenset({"sequence", "trigger", "source", "target", "guard"})
        )
        number = _integer(fact["sequence"], 0, 64)
        trigger, source, target, guard = (
            fact["trigger"],
            fact["source"],
            fact["target"],
            fact["guard"],
        )
        if (
            type(trigger) is not str
            or type(source) is not str
            or type(target) is not str
            or (guard is not None and type(guard) is not str)
        ):
            raise DurableStateError("transition_invalid")
        if (
            number != sequence
            or target != state
            or not any(
                trigger == edge.trigger
                and source in edge.source
                and target == edge.target
                and guard == edge.guard
                for edge in MANAGED_RUN_MACHINE.transitions
            )
        ):
            raise DurableStateError("transition_invalid")
        transition = StateTransition(number, trigger, source, target, guard)
    if kind == "managed_run":
        if (
            state not in MANAGED_RUN_MACHINE.states
            or any(values[name] is not None for name in ("workspace", "reference", "timeline"))
            or (state == "created" and (transition is not None or sequence != 0))
            or (state != "created" and transition is None)
        ):
            raise DurableStateError("state_invalid")
    elif (
        state not in {"workspace_active", "workspace_closed"}
        or transition is not None
        or sequence != 0
        or values["context"] is not None
        or values["workspace"] is None
        or (
            kind == "production"
            and (
                values["workspace"] < 1
                or values["reference"] is not None
                or values["timeline"] is not None
            )
        )
        or (
            kind == "authoring"
            and (count != 0 or values["reference"] is None or values["timeline"] is None)
        )
        or ((state == "workspace_closed") != (closed is not None))
    ):
        raise DurableStateError("state_invalid")
    result = StateRecord(identifier, kind, created, closed, count, state, revisions, transition)
    if len(json_bytes(result.to_wire())) > MAX_RECORD_BYTES:
        raise DurableStateError("size_invalid")
    return result


def decode_snapshot(data: bytes, *, expected_owner: str) -> StateSnapshot:
    owner = require_owner(expected_owner)
    row = _keys(
        strict_json(data, maximum_bytes=MAX_SNAPSHOT_BYTES),
        frozenset({"schema", "owner_id", "revision", "saved_at_ms", "records"}),
    )
    if row["schema"] != STATE_SCHEMA:
        raise DurableStateError("version_unsupported")
    if row["owner_id"] != owner:
        raise DurableStateError("owner_mismatch")
    revision = _integer(row["revision"], 1, MAX_OWNER_REVISION)
    saved = _integer(row["saved_at_ms"], 1, MAX_TIMESTAMP_MS)
    raw = row["records"]
    if type(raw) is not list or len(raw) > MAX_OWNER_RECORDS:
        raise DurableStateError("record_bound")
    records = tuple(
        sorted((decode_record(value) for value in raw), key=lambda item: item.record_id)
    )
    if len({item.record_id for item in records}) != len(records):
        raise DurableStateError("record_duplicate")
    if any(
        item.created_at_ms > saved or (item.closed_at_ms is not None and item.closed_at_ms > saved)
        for item in records
    ):
        raise DurableStateError("timestamp_invalid")
    return StateSnapshot(owner, revision, saved, records)


def encode_snapshot(snapshot: StateSnapshot) -> bytes:
    if type(snapshot) is not StateSnapshot:
        raise DurableStateError("shape_invalid")
    payload = json_bytes(snapshot.to_wire())
    # CRITICAL: even an internally constructed DTO must pass the closed decoder before disk IO;
    # a generic dataclass dump would silently widen the persisted privacy contract.
    decode_snapshot(payload, expected_owner=snapshot.owner_id)
    return payload


def recover_record(record: StateRecord) -> dict[str, object]:
    if type(record) is not StateRecord:
        raise DurableStateError("record_invalid")
    admitted = decode_record(record.to_wire())
    handle = "recovery_" + secrets.token_urlsafe(32)
    state = admitted.state
    if admitted.kind == "managed_run":
        run = restore(handle, admitted.state)
        # CRITICAL: a saved inflight fact is not current queue ownership. Apply only the pure
        # existing uncertainty transition; never install this run or replay execution callbacks.
        if run.state in HOST_MAY_HOLD_THE_PROMPT:
            run = advance(run, ManagedRunTrigger.CANCEL_AFTER_HOST)
        state = run.state.value
    return {
        "recovery_handle": handle,
        "record_id": admitted.record_id,
        "kind": admitted.kind,
        "state": state,
        "source_status": "source_reauthorization_required",
        "executable": False,
        "segment_count": admitted.segment_count,
        "revisions": admitted.revisions.to_wire(),
    }
