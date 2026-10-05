"""M25-11 pure transaction, CAS, receipt, and bounded history authority.

The accepted M25-10 public snapshot is the only persisted edit state. This module stores immutable
before/after snapshots and delegates every edit to :mod:`timeline_authoring`; it owns only
transaction validation, selection, cursors, idempotency, branch movement, rebase, and quotas.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from .canonical import MAX_CANONICAL_BYTES, canonical_bytes, canonical_fingerprint
from .composition_contract import (
    PublicAsset,
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from .errors import ContractValidationError
from .timeline_authoring import (
    MAX_REVISION,
    NleCommand,
    NleCommandKind,
    apply_nle_command,
    decode_nle_command,
    resign_nle_snapshot,
)

TIMELINE_TRANSACTION_SCHEMA = "h3.context.timeline_transaction.v1"
TIMELINE_RECEIPT_SCHEMA = "h3.context.timeline_receipt.v1"
TIMELINE_HISTORY_CURSOR_SCHEMA = "h3.context.timeline_history_cursor.v1"

MAX_TRANSACTION_COMMANDS = 32
MAX_TRANSACTION_BYTES = 262_144
MAX_TRANSACTION_WORK_UNITS = 4_096
MAX_SELECTED_CLIPS = 128
MAX_UNDO_ENTRIES = 64
MAX_REDO_ENTRIES = 64
MAX_IDEMPOTENCY_RECEIPTS = 128
MAX_HISTORY_BYTES = 8_388_608
MAX_INVALIDATED_CURSORS = 128

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HISTORY_CURSOR = re.compile(
    rf"{re.escape(TIMELINE_HISTORY_CURSOR_SCHEMA)}:([1-9][0-9]{{0,6}}):[0-9a-f]{{64}}\Z"
)

_TRANSACTION_KEYS = {
    "schema",
    "request_id",
    "transaction_id",
    "workspace_handle",
    "expected_workspace_revision",
    "expected_timeline_revision",
    "expected_timeline_fingerprint",
    "commands",
}
_HISTORY_KINDS = {
    NleCommandKind.UNDO,
    NleCommandKind.REDO,
    NleCommandKind.REBASE_TRANSACTION,
}
_REBASE_PROPERTY_KINDS = {
    NleCommandKind.SET_CLIP_ENABLED,
    NleCommandKind.SET_VISUAL_TRANSFORM,
    NleCommandKind.SET_CROP,
    NleCommandKind.SET_OPACITY_BLEND,
    NleCommandKind.SET_TEXT_CONTENT,
    NleCommandKind.SET_TEXT_STYLE,
    NleCommandKind.SET_TRANSITION,
    NleCommandKind.SET_EFFECT,
    NleCommandKind.SET_CLIP_AUDIO,
    NleCommandKind.SELECT_CLIPS,
}


class TimelineHistoryError(ContractValidationError):
    """Stable, content-free transaction/history rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _reject(code: str, message: str) -> TimelineHistoryError:
    return TimelineHistoryError(code, message)


def _revision(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_REVISION:
        raise _reject("invalid_transaction", f"{name} is outside its integer bounds")
    return value


def _fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise _reject("invalid_transaction", f"{name} is not a canonical fingerprint")
    return value


def _transaction_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _reject("invalid_transaction", f"{name} is not a bounded identifier")
    return value


def _history_cursor(value: object) -> str:
    # CRITICAL: accept only cursors this authority can generate; prefix-only validation lets
    # malformed branch material cross the closed transaction boundary.
    if not isinstance(value, str):
        raise _reject("history_cursor_invalid", "history cursor is malformed")
    match = _HISTORY_CURSOR.fullmatch(value)
    if match is None or int(match.group(1)) > MAX_REVISION:
        raise _reject("history_cursor_invalid", "history cursor is malformed")
    return value


def _work_units(value: object, *, depth: int = 0) -> int:
    if depth > 32:
        raise _reject("resource_limit", "transaction nesting exceeds the work profile")
    if isinstance(value, Mapping):
        return 1 + sum(
            _work_units(key, depth=depth + 1) + _work_units(member, depth=depth + 1)
            for key, member in value.items()
        )
    if isinstance(value, (list, tuple)):
        return 1 + sum(_work_units(member, depth=depth + 1) for member in value)
    if isinstance(value, str):
        return 1 + (len(value.encode("utf-8")) // 64)
    return 1


@dataclass(frozen=True, slots=True)
class TimelineTransaction:
    schema: str
    request_id: str
    transaction_id: str
    workspace_handle: str
    expected_workspace_revision: int
    expected_timeline_revision: int
    expected_timeline_fingerprint: str
    commands: tuple[NleCommand, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "transaction_id": self.transaction_id,
            "workspace_handle": self.workspace_handle,
            "expected_workspace_revision": self.expected_workspace_revision,
            "expected_timeline_revision": self.expected_timeline_revision,
            "expected_timeline_fingerprint": self.expected_timeline_fingerprint,
            "commands": [command.to_wire() for command in self.commands],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def _decode_rebase_command(command: NleCommand) -> None:
    payload = command.payload
    _fingerprint(payload["base_timeline_fingerprint"], "base_timeline_fingerprint")
    rows = payload["commands"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_TRANSACTION_COMMANDS:
        raise _reject("invalid_transaction", "rebase commands are outside transaction bounds")
    nested = tuple(decode_nle_command(row) for row in rows)
    if any(member.kind not in _REBASE_PROPERTY_KINDS for member in nested):
        raise _reject("rebase_conflict", "rebase contains a non-rebasable command")


def decode_timeline_transaction(value: object) -> TimelineTransaction:
    if not isinstance(value, Mapping) or set(value) != _TRANSACTION_KEYS:
        raise _reject("invalid_transaction", "transaction members do not match the v1 schema")
    try:
        encoded = canonical_bytes(value)
    except ContractValidationError as exc:
        raise _reject("invalid_transaction", "transaction is not bounded canonical JSON") from exc
    if len(encoded) > MAX_TRANSACTION_BYTES:
        raise _reject("resource_limit", "transaction exceeds the byte profile")
    if _work_units(value) > MAX_TRANSACTION_WORK_UNITS:
        raise _reject("resource_limit", "transaction exceeds the work profile")
    if value["schema"] != TIMELINE_TRANSACTION_SCHEMA:
        raise _reject("invalid_transaction", "transaction schema is unsupported")
    rows = value["commands"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_TRANSACTION_COMMANDS:
        raise _reject("invalid_transaction", "transaction command count is outside bounds")
    commands = tuple(decode_nle_command(row) for row in rows)
    if any(command.kind in _HISTORY_KINDS for command in commands) and len(commands) != 1:
        raise _reject("invalid_transaction", "a history command must be the sole command")
    command = commands[0]
    if command.kind in (NleCommandKind.UNDO, NleCommandKind.REDO):
        _history_cursor(command.payload["history_cursor"])
    elif command.kind is NleCommandKind.REBASE_TRANSACTION:
        _decode_rebase_command(command)
    return TimelineTransaction(
        TIMELINE_TRANSACTION_SCHEMA,
        _transaction_identifier(value["request_id"], "request_id"),
        _transaction_identifier(value["transaction_id"], "transaction_id"),
        _transaction_identifier(value["workspace_handle"], "workspace_handle"),
        _revision(value["expected_workspace_revision"], "expected_workspace_revision"),
        _revision(value["expected_timeline_revision"], "expected_timeline_revision"),
        _fingerprint(value["expected_timeline_fingerprint"], "expected_timeline_fingerprint"),
        commands,
    )


@dataclass(frozen=True, slots=True)
class TimelineReceipt:
    schema: str
    request_id: str
    transaction_id: str
    workspace_handle: str
    before_workspace_revision: int
    after_workspace_revision: int
    before_workspace_fingerprint: str
    after_workspace_fingerprint: str
    before_timeline_revision: int
    after_timeline_revision: int
    before_timeline_fingerprint: str
    after_timeline_fingerprint: str
    commands_json: str
    affected_ids: tuple[str, ...]
    inverse_json: str
    history_cursor: str
    selection: tuple[str, ...]
    snapshot: PublicCompositionSnapshot

    @property
    def commands(self) -> tuple[dict[str, object], ...]:
        rows = json.loads(self.commands_json)
        if not isinstance(rows, list):
            raise AssertionError("receipt commands lost their array shape")
        return tuple(rows)

    @property
    def inverse(self) -> dict[str, object]:
        value = json.loads(self.inverse_json)
        if not isinstance(value, dict):
            raise AssertionError("receipt inverse lost its object shape")
        return value

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "transaction_id": self.transaction_id,
            "workspace_handle": self.workspace_handle,
            "before_workspace_revision": self.before_workspace_revision,
            "after_workspace_revision": self.after_workspace_revision,
            "before_workspace_fingerprint": self.before_workspace_fingerprint,
            "after_workspace_fingerprint": self.after_workspace_fingerprint,
            "before_timeline_revision": self.before_timeline_revision,
            "after_timeline_revision": self.after_timeline_revision,
            "before_timeline_fingerprint": self.before_timeline_fingerprint,
            "after_timeline_fingerprint": self.after_timeline_fingerprint,
            "commands": list(self.commands),
            "affected_ids": list(self.affected_ids),
            "inverse": self.inverse,
            "history_cursor": self.history_cursor,
            "selection": list(self.selection),
            "snapshot": self.snapshot.to_wire(),
        }


def _history_material_byte_size(material: Mapping[str, object]) -> int:
    # CRITICAL: admitted snapshots can contain 512 timing rows per asset. Reusing the generic
    # 256-item canonicalizer here rejects valid edits/replays; count their complete stored wire.
    try:
        encoded = json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _reject("invalid_transaction", "history material is not strict UTF-8 JSON") from exc
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise _reject("resource_limit", "history material exceeds its byte limit")
    return len(encoded)


@dataclass(frozen=True, slots=True)
class TimelineHistoryEntry:
    sequence: int
    cursor: str
    before_snapshot: PublicCompositionSnapshot
    after_snapshot: PublicCompositionSnapshot
    before_selection: tuple[str, ...]
    after_selection: tuple[str, ...]
    commands_json: str
    affected_ids: tuple[str, ...]

    def byte_size(self) -> int:
        return _history_material_byte_size(
            {
                "sequence": self.sequence,
                "cursor": self.cursor,
                "before_snapshot": self.before_snapshot.to_wire(),
                "after_snapshot": self.after_snapshot.to_wire(),
                "before_selection": list(self.before_selection),
                "after_selection": list(self.after_selection),
                "commands": json.loads(self.commands_json),
                "affected_ids": list(self.affected_ids),
            }
        )


@dataclass(frozen=True, slots=True)
class _IdempotencyRecord:
    sequence: int
    request_id: str
    transaction_fingerprint: str
    receipt: TimelineReceipt

    def byte_size(self) -> int:
        return _history_material_byte_size(
            {
                "sequence": self.sequence,
                "request_id": self.request_id,
                "transaction_fingerprint": self.transaction_fingerprint,
                "receipt": self.receipt.to_wire(),
            }
        )


@dataclass(frozen=True, slots=True)
class TimelineHistoryState:
    snapshot: PublicCompositionSnapshot
    selection: tuple[str, ...] = ()
    undo_entries: tuple[TimelineHistoryEntry, ...] = ()
    redo_entries: tuple[TimelineHistoryEntry, ...] = ()
    idempotency_records: tuple[_IdempotencyRecord, ...] = ()
    invalidated_cursors: tuple[str, ...] = ()
    next_sequence: int = 1
    released: bool = False

    @classmethod
    def initialize(cls, snapshot: PublicCompositionSnapshot) -> TimelineHistoryState:
        if not isinstance(snapshot, PublicCompositionSnapshot):
            raise _reject("invalid_transaction", "history requires an accepted public snapshot")
        return cls(snapshot=snapshot)


def _cursor(workspace_handle: str, sequence: int, timeline_fingerprint: str) -> str:
    digest = canonical_fingerprint(
        {
            "schema": TIMELINE_HISTORY_CURSOR_SCHEMA,
            "workspace_handle": workspace_handle,
            "sequence": sequence,
            "timeline_fingerprint": timeline_fingerprint,
        }
    ).removeprefix("sha256:")
    return f"{TIMELINE_HISTORY_CURSOR_SCHEMA}:{sequence}:{digest}"


def _receipt(
    transaction: TimelineTransaction,
    before: PublicCompositionSnapshot,
    after: PublicCompositionSnapshot,
    *,
    affected_ids: tuple[str, ...],
    inverse: Mapping[str, object],
    history_cursor: str,
    selection: tuple[str, ...],
) -> TimelineReceipt:
    return TimelineReceipt(
        TIMELINE_RECEIPT_SCHEMA,
        transaction.request_id,
        transaction.transaction_id,
        transaction.workspace_handle,
        before.workspace_revision,
        after.workspace_revision,
        before.workspace_fingerprint,
        after.workspace_fingerprint,
        before.timeline_revision,
        after.timeline_revision,
        before.timeline_fingerprint,
        after.timeline_fingerprint,
        canonical_bytes([command.to_wire() for command in transaction.commands]).decode("utf-8"),
        tuple(sorted(set(affected_ids))),
        canonical_bytes(inverse).decode("utf-8"),
        history_cursor,
        selection,
        after,
    )


def _with_idempotency(
    state: TimelineHistoryState,
    transaction: TimelineTransaction,
    receipt: TimelineReceipt,
) -> TimelineHistoryState:
    record = _IdempotencyRecord(
        state.next_sequence - 1,
        transaction.request_id,
        transaction.fingerprint,
        receipt,
    )
    return _bound_history(replace(state, idempotency_records=state.idempotency_records + (record,)))


def _bound_history(state: TimelineHistoryState) -> TimelineHistoryState:
    undo = list(state.undo_entries[-MAX_UNDO_ENTRIES:])
    redo = list(state.redo_entries[-MAX_REDO_ENTRIES:])
    records = list(state.idempotency_records[-MAX_IDEMPOTENCY_RECEIPTS:])
    while (
        sum(entry.byte_size() for entry in undo + redo)
        + sum(record.byte_size() for record in records)
        > MAX_HISTORY_BYTES
    ):
        candidates: list[tuple[int, str]] = []
        if undo:
            candidates.append((undo[0].sequence, "undo"))
        if redo:
            candidates.append((redo[0].sequence, "redo"))
        if records:
            candidates.append((records[0].sequence, "record"))
        if not candidates:
            raise _reject("resource_limit", "history state exceeds its byte profile")
        _, owner = min(candidates)
        if owner == "undo":
            undo.pop(0)
        elif owner == "redo":
            redo.pop(0)
        else:
            records.pop(0)
    return replace(
        state,
        undo_entries=tuple(undo),
        redo_entries=tuple(redo),
        idempotency_records=tuple(records),
        invalidated_cursors=state.invalidated_cursors[-MAX_INVALIDATED_CURSORS:],
    )


def _check_idempotency(
    state: TimelineHistoryState, transaction: TimelineTransaction
) -> TimelineReceipt | None:
    for record in state.idempotency_records:
        if record.request_id == transaction.request_id:
            if record.transaction_fingerprint != transaction.fingerprint:
                raise _reject("idempotency_conflict", "request identifier payload changed")
            return record.receipt
    return None


def _check_cas(state: TimelineHistoryState, transaction: TimelineTransaction) -> None:
    snapshot = state.snapshot
    if transaction.workspace_handle != snapshot.workspace_handle:
        raise _reject("invalid_transaction", "transaction targets another workspace")
    if transaction.expected_workspace_revision != snapshot.workspace_revision:
        raise _reject("stale_workspace_revision", "workspace revision changed")
    if transaction.expected_timeline_revision != snapshot.timeline_revision:
        raise _reject("stale_timeline_revision", "timeline revision changed")
    if transaction.expected_timeline_fingerprint != snapshot.timeline_fingerprint:
        raise _reject("stale_timeline_fingerprint", "timeline fingerprint changed")


def _advance_snapshot(
    snapshot: PublicCompositionSnapshot, source: PublicCompositionSnapshot
) -> PublicCompositionSnapshot:
    if snapshot.workspace_revision >= MAX_REVISION or snapshot.timeline_revision >= MAX_REVISION:
        raise _reject("resource_limit", "revision capacity is exhausted")
    return resign_nle_snapshot(
        source,
        workspace_revision=snapshot.workspace_revision + 1,
        timeline_revision=snapshot.timeline_revision + 1,
    )


def refresh_timeline_asset_catalog(
    state: TimelineHistoryState,
    additions: tuple[PublicAsset, ...],
) -> TimelineHistoryState:
    """Add exact assets without creating a timeline command or history entry."""

    if not isinstance(state, TimelineHistoryState) or state.released:
        raise _reject("invalid_transaction", "history is unavailable")
    if (
        type(additions) is not tuple
        or not additions
        or not all(type(asset) is PublicAsset for asset in additions)
    ):
        raise _reject("invalid_transaction", "catalog additions are invalid")
    if state.snapshot.workspace_revision >= MAX_REVISION:
        raise _reject("resource_limit", "workspace revision capacity is exhausted")
    existing = {asset.asset_id: asset for asset in state.snapshot.assets}
    if len({asset.asset_id for asset in additions}) != len(additions) or any(
        asset.asset_id in existing for asset in additions
    ):
        raise _reject("invalid_transaction", "catalog additions replace or repeat an asset")
    # Keep packaged fonts last, matching source-owned history initialization.  Asset order is part
    # of the public fingerprint, so this insertion rule must stay deterministic.
    assets = list(state.snapshot.assets)
    first_font = next(
        (index for index, asset in enumerate(assets) if asset.kind == "font"), len(assets)
    )
    assets[first_font:first_font] = additions
    wire = state.snapshot.to_wire()
    wire["assets"] = [asset.to_wire() for asset in assets]
    wire["workspace_revision"] = state.snapshot.workspace_revision + 1
    wire["workspace_fingerprint"] = canonical_fingerprint(
        {
            "project_id": wire["project_id"],
            "workspace_handle": wire["workspace_handle"],
            "workspace_revision": wire["workspace_revision"],
            "timeline_revision": wire["timeline_revision"],
            "timeline_fingerprint": wire["timeline_fingerprint"],
        }
    )
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    refreshed = decode_public_snapshot(wire)
    if (
        refreshed.timeline_fingerprint != state.snapshot.timeline_fingerprint
        or refreshed.timeline_revision != state.snapshot.timeline_revision
    ):
        raise _reject("invalid_transaction", "catalog refresh changed timeline identity")
    return replace(state, snapshot=refreshed)


def _overlay_current_catalog(
    current: PublicCompositionSnapshot,
    historical: PublicCompositionSnapshot,
) -> PublicCompositionSnapshot:
    historical_by_id = {asset.asset_id: asset for asset in historical.assets}
    current_by_id = {asset.asset_id: asset for asset in current.assets}
    if any(current_by_id.get(asset_id) != asset for asset_id, asset in historical_by_id.items()):
        raise _reject("invalid_transaction", "history catalog conflicts with current authority")
    # IMPORTANT: undo/redo restores scene state, not the Authoring library.  Reusing the retained
    # historical catalog here silently loses M25-29 imports even though no removal command ran.
    return replace(historical, assets=current.assets)


def _apply_ordinary(
    state: TimelineHistoryState,
    transaction: TimelineTransaction,
    *,
    effective_commands: tuple[NleCommand, ...] | None = None,
) -> tuple[TimelineHistoryState, TimelineReceipt]:
    before = state.snapshot
    snapshot = before
    selection = state.selection
    affected: set[str] = set()
    commands = transaction.commands if effective_commands is None else effective_commands
    for command in commands:
        transition = apply_nle_command(snapshot, command, selection=selection)
        if not isinstance(transition.snapshot, PublicCompositionSnapshot):
            raise AssertionError("V1 history command returned a V2 authoring state")
        snapshot = transition.snapshot
        selection = transition.selection
        affected.update(transition.affected_ids)
    after = _advance_snapshot(before, snapshot)
    sequence = state.next_sequence
    cursor = _cursor(before.workspace_handle, sequence, after.timeline_fingerprint)
    commands_json = canonical_bytes([command.to_wire() for command in transaction.commands]).decode(
        "utf-8"
    )
    entry = TimelineHistoryEntry(
        sequence,
        cursor,
        before,
        after,
        state.selection,
        selection,
        commands_json,
        tuple(sorted(affected)),
    )
    # IMPORTANT: every newly accepted ordinary branch invalidates all redo cursors; retaining them
    # would let a stale branch overwrite the current accepted snapshot.
    invalidated = state.invalidated_cursors + tuple(item.cursor for item in state.redo_entries)
    candidate = replace(
        state,
        snapshot=after,
        selection=selection,
        undo_entries=state.undo_entries + (entry,),
        redo_entries=(),
        invalidated_cursors=invalidated,
        next_sequence=sequence + 1,
    )
    receipt = _receipt(
        transaction,
        before,
        after,
        affected_ids=entry.affected_ids,
        inverse={"kind": "restore_transaction_state", "history_cursor": cursor},
        history_cursor=cursor,
        selection=selection,
    )
    return _with_idempotency(candidate, transaction, receipt), receipt


def _cursor_disposition(
    state: TimelineHistoryState,
    cursor: str,
    *,
    owner: tuple[TimelineHistoryEntry, ...],
    other: tuple[TimelineHistoryEntry, ...],
) -> TimelineHistoryEntry:
    if cursor in state.invalidated_cursors or any(entry.cursor == cursor for entry in other):
        raise _reject("history_branch_invalid", "history cursor belongs to another branch")
    if not owner or owner[-1].cursor != cursor:
        if any(entry.cursor == cursor for entry in owner):
            raise _reject("history_branch_invalid", "history cursor is not the branch head")
        raise _reject("history_cursor_invalid", "history cursor is unavailable or evicted")
    return owner[-1]


def _apply_history_move(
    state: TimelineHistoryState,
    transaction: TimelineTransaction,
    *,
    undo: bool,
) -> tuple[TimelineHistoryState, TimelineReceipt]:
    command = transaction.commands[0]
    requested_cursor = _history_cursor(command.payload["history_cursor"])
    owner = state.undo_entries if undo else state.redo_entries
    other = state.redo_entries if undo else state.undo_entries
    entry = _cursor_disposition(state, requested_cursor, owner=owner, other=other)
    before = state.snapshot
    source = entry.before_snapshot if undo else entry.after_snapshot
    source = _overlay_current_catalog(before, source)
    selection = entry.before_selection if undo else entry.after_selection
    after = _advance_snapshot(before, source)
    sequence = state.next_sequence
    new_cursor = _cursor(before.workspace_handle, sequence, after.timeline_fingerprint)
    moved = replace(entry, sequence=sequence, cursor=new_cursor)
    if undo:
        undo_entries = state.undo_entries[:-1]
        redo_entries = state.redo_entries + (moved,)
        inverse_kind = "redo"
    else:
        undo_entries = state.undo_entries + (moved,)
        redo_entries = state.redo_entries[:-1]
        inverse_kind = "undo"
    candidate = replace(
        state,
        snapshot=after,
        selection=selection,
        undo_entries=undo_entries,
        redo_entries=redo_entries,
        invalidated_cursors=state.invalidated_cursors + (requested_cursor,),
        next_sequence=sequence + 1,
    )
    receipt = _receipt(
        transaction,
        before,
        after,
        affected_ids=entry.affected_ids,
        inverse={"kind": inverse_kind, "history_cursor": new_cursor},
        history_cursor=new_cursor,
        selection=selection,
    )
    return _with_idempotency(candidate, transaction, receipt), receipt


def _find_base_snapshot(
    state: TimelineHistoryState, timeline_fingerprint: str
) -> PublicCompositionSnapshot | None:
    candidates = [state.snapshot]
    for entry in (*state.undo_entries, *state.redo_entries):
        candidates.extend((entry.before_snapshot, entry.after_snapshot))
    return next(
        (
            snapshot
            for snapshot in reversed(candidates)
            if snapshot.timeline_fingerprint == timeline_fingerprint
        ),
        None,
    )


def _clip_wire(snapshot: PublicCompositionSnapshot, clip_id: str) -> dict[str, object] | None:
    clips = snapshot.to_wire()["clips"]
    if not isinstance(clips, list):
        raise AssertionError("accepted snapshot lost its clip list")
    for clip in clips:
        if isinstance(clip, dict) and clip.get("clip_id") == clip_id:
            return clip
    return None


def _property_projection(command: NleCommand, clip: dict[str, object]) -> object:
    kind = command.kind
    if kind is NleCommandKind.SET_CLIP_ENABLED:
        return clip.get("enabled")
    if kind is NleCommandKind.SET_VISUAL_TRANSFORM:
        return clip.get("transform")
    if kind is NleCommandKind.SET_CROP:
        return clip.get("crop")
    if kind is NleCommandKind.SET_OPACITY_BLEND:
        return (clip.get("opacity_bp"), clip.get("blend"))
    text = clip.get("text")
    if kind is NleCommandKind.SET_TEXT_CONTENT:
        return text.get("content") if isinstance(text, dict) else None
    if kind is NleCommandKind.SET_TEXT_STYLE:
        if not isinstance(text, dict):
            return None
        return {key: value for key, value in text.items() if key != "content"}
    if kind is NleCommandKind.SET_TRANSITION:
        return (
            clip.get("track_id"),
            clip.get("start_frame"),
            clip.get("duration_frames"),
            clip.get("transition"),
        )
    if kind is NleCommandKind.SET_EFFECT:
        return clip.get("effect")
    if kind is NleCommandKind.SET_CLIP_AUDIO:
        # The fades are bounded by the duration and the member applies only on a clip whose
        # place and length are the ones it was written for, so a concurrent change of the
        # extent conflicts as it does for a transition.
        return (
            clip.get("track_id"),
            clip.get("start_frame"),
            clip.get("duration_frames"),
            clip.get("audio"),
        )
    raise _reject("rebase_conflict", "command is not a rebasable property")


def _rebase_commands(state: TimelineHistoryState, command: NleCommand) -> tuple[NleCommand, ...]:
    payload = command.payload
    base_fingerprint = _fingerprint(
        payload["base_timeline_fingerprint"], "base_timeline_fingerprint"
    )
    base = _find_base_snapshot(state, base_fingerprint)
    if base is None:
        raise _reject("rebase_conflict", "rebase base is unavailable or evicted")
    rows = payload["commands"]
    if not isinstance(rows, list):
        raise _reject("invalid_transaction", "rebase commands must be an array")
    commands = tuple(decode_nle_command(row) for row in rows)
    for member in commands:
        if member.kind is NleCommandKind.SELECT_CLIPS:
            selected = member.payload["clip_ids"]
            # CRITICAL: a rebase may select only targets that existed at the declared base and
            # still exist now. Checking current state alone lets stale input address a new clip.
            if not isinstance(selected, list) or any(
                not isinstance(clip_id, str)
                or _clip_wire(base, clip_id) is None
                or _clip_wire(state.snapshot, clip_id) is None
                for clip_id in selected
            ):
                raise _reject("rebase_conflict", "selection target changed or disappeared")
            continue
        if member.kind not in _REBASE_PROPERTY_KINDS:
            raise _reject("rebase_conflict", "command is not in the rebase allowlist")
        clip_id = member.payload.get("clip_id")
        if not isinstance(clip_id, str):
            raise _reject("rebase_conflict", "property command has no clip target")
        before_clip = _clip_wire(base, clip_id)
        current_clip = _clip_wire(state.snapshot, clip_id)
        if before_clip is None or current_clip is None:
            raise _reject("rebase_conflict", "property target changed or disappeared")
        if _property_projection(member, before_clip) != _property_projection(member, current_clip):
            raise _reject("rebase_conflict", "addressed property changed since the base")
    return commands


def apply_timeline_transaction(
    state: TimelineHistoryState,
    value: TimelineTransaction | object,
) -> tuple[TimelineHistoryState, TimelineReceipt]:
    if not isinstance(state, TimelineHistoryState):
        raise _reject("invalid_transaction", "transaction requires a history state")
    transaction = decode_timeline_transaction(
        value.to_wire() if isinstance(value, TimelineTransaction) else value
    )
    if state.released:
        raise _reject("workspace_released", "workspace history has been released")
    # IMPORTANT: retry lookup precedes CAS so an identical lost-response retry returns its original
    # receipt after the accepted revision advanced; changing this order breaks safe idempotency.
    replay = _check_idempotency(state, transaction)
    if replay is not None:
        return state, replay
    _check_cas(state, transaction)
    kind = transaction.commands[0].kind
    if kind is NleCommandKind.UNDO:
        return _apply_history_move(state, transaction, undo=True)
    if kind is NleCommandKind.REDO:
        return _apply_history_move(state, transaction, undo=False)
    if kind is NleCommandKind.REBASE_TRANSACTION:
        return _apply_ordinary(
            state,
            transaction,
            effective_commands=_rebase_commands(state, transaction.commands[0]),
        )
    return _apply_ordinary(state, transaction)


def release_timeline_history(state: TimelineHistoryState) -> TimelineHistoryState:
    if not isinstance(state, TimelineHistoryState):
        raise _reject("invalid_transaction", "release requires a history state")
    return replace(
        state,
        undo_entries=(),
        redo_entries=(),
        idempotency_records=(),
        invalidated_cursors=(),
        released=True,
    )
