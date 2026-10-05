"""Atomic V2 NLE authoring transactions and bounded undo/redo history."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from .canonical import MAX_CANONICAL_BYTES, canonical_bytes, canonical_fingerprint
from .composition_contract import PublicAsset
from .errors import ContractValidationError
from .nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    TIMELINE_HISTORY_CURSOR_SCHEMA_V2,
    TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
    TIMELINE_RECEIPT_SCHEMA_V2,
    TIMELINE_TRANSACTION_SCHEMA_V2,
    NleAuthoringState,
    create_nle_authoring_state,
    materialize_render_snapshot,
)
from .timeline_authoring import (
    MAX_REVISION,
    NleCommand,
    NleCommandKind,
    apply_nle_command,
    decode_nle_command,
)
from .timeline_history import (
    MAX_IDEMPOTENCY_RECEIPTS,
    MAX_INVALIDATED_CURSORS,
    MAX_REDO_ENTRIES,
    MAX_SELECTED_CLIPS,
    MAX_TRANSACTION_BYTES,
    MAX_TRANSACTION_COMMANDS,
    MAX_TRANSACTION_WORK_UNITS,
    MAX_UNDO_ENTRIES,
    _work_units,
)

MAX_HISTORY_BYTES_V2 = 8_388_608

_TRANSACTION_KEYS = {
    "schema",
    "authoring_schema",
    "profile_id",
    "operation_profile_id",
    "request_id",
    "transaction_id",
    "workspace_handle",
    "expected_workspace_revision",
    "expected_timeline_revision",
    "expected_timeline_fingerprint",
    "expected_authoring_fingerprint",
    "commands",
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CURSOR = re.compile(
    rf"{re.escape(TIMELINE_HISTORY_CURSOR_SCHEMA_V2)}:([1-9][0-9]{{0,6}}):[0-9a-f]{{64}}\Z"
)
_HISTORY_KINDS = {NleCommandKind.UNDO, NleCommandKind.REDO, NleCommandKind.REBASE_TRANSACTION}
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


class TimelineHistoryV2Error(ContractValidationError):
    """Stable, content-free rejection from the V2 authoring-history boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _reject(code: str, message: str) -> TimelineHistoryV2Error:
    return TimelineHistoryV2Error(code, message)


def _integer(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise _reject("invalid_transaction", f"{name} is outside its integer bounds")
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _reject("invalid_transaction", f"{name} is not a bounded identifier")
    return value


def _fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise _reject("invalid_transaction", f"{name} is not a canonical fingerprint")
    return value


def _history_cursor(value: object) -> str:
    if not isinstance(value, str):
        raise _reject("history_cursor_invalid", "history cursor is malformed")
    match = _CURSOR.fullmatch(value)
    if match is None or int(match.group(1)) > MAX_REVISION:
        raise _reject("history_cursor_invalid", "history cursor is malformed")
    return value


@dataclass(frozen=True, slots=True)
class TimelineTransactionV2:
    schema: str
    authoring_schema: str
    profile_id: str
    operation_profile_id: str
    request_id: str
    transaction_id: str
    workspace_handle: str
    expected_workspace_revision: int
    expected_timeline_revision: int
    expected_timeline_fingerprint: str
    expected_authoring_fingerprint: str
    commands: tuple[NleCommand, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "authoring_schema": self.authoring_schema,
            "profile_id": self.profile_id,
            "operation_profile_id": self.operation_profile_id,
            "request_id": self.request_id,
            "transaction_id": self.transaction_id,
            "workspace_handle": self.workspace_handle,
            "expected_workspace_revision": self.expected_workspace_revision,
            "expected_timeline_revision": self.expected_timeline_revision,
            "expected_timeline_fingerprint": self.expected_timeline_fingerprint,
            "expected_authoring_fingerprint": self.expected_authoring_fingerprint,
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
    commands = tuple(decode_nle_command(row) for row in rows)
    if any(command.kind not in _REBASE_PROPERTY_KINDS for command in commands):
        raise _reject("rebase_conflict", "rebase contains a non-rebasable command")


def decode_timeline_transaction_v2(value: object) -> TimelineTransactionV2:
    if not isinstance(value, Mapping) or set(value) != _TRANSACTION_KEYS:
        raise _reject("invalid_transaction", "transaction members do not match the v2 schema")
    try:
        encoded = canonical_bytes(value)
    except ContractValidationError as exc:
        raise _reject("invalid_transaction", "transaction is not bounded canonical JSON") from exc
    if len(encoded) > MAX_TRANSACTION_BYTES or _work_units(value) > MAX_TRANSACTION_WORK_UNITS:
        raise _reject("resource_limit", "transaction exceeds its bounded work profile")
    if (
        value["schema"] != TIMELINE_TRANSACTION_SCHEMA_V2
        or value["authoring_schema"] != NLE_AUTHORING_SCHEMA
        or value["profile_id"] != NLE_AUTHORING_PROFILE_ID
        or value["operation_profile_id"] != NLE_OPERATION_PROFILE_ID
    ):
        raise _reject("unsupported_profile", "transaction authoring profile is unsupported")
    rows = value["commands"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_TRANSACTION_COMMANDS:
        raise _reject("invalid_transaction", "transaction command count is outside bounds")
    commands = tuple(decode_nle_command(row) for row in rows)
    if any(command.kind in _HISTORY_KINDS for command in commands) and len(commands) != 1:
        raise _reject("invalid_transaction", "a history command must be the sole command")
    if commands[0].kind in (NleCommandKind.UNDO, NleCommandKind.REDO):
        _history_cursor(commands[0].payload["history_cursor"])
    elif commands[0].kind is NleCommandKind.REBASE_TRANSACTION:
        _decode_rebase_command(commands[0])
    return TimelineTransactionV2(
        TIMELINE_TRANSACTION_SCHEMA_V2,
        NLE_AUTHORING_SCHEMA,
        NLE_AUTHORING_PROFILE_ID,
        NLE_OPERATION_PROFILE_ID,
        _identifier(value["request_id"], "request_id"),
        _identifier(value["transaction_id"], "transaction_id"),
        _identifier(value["workspace_handle"], "workspace_handle"),
        _integer(value["expected_workspace_revision"], "expected_workspace_revision"),
        _integer(value["expected_timeline_revision"], "expected_timeline_revision"),
        _fingerprint(value["expected_timeline_fingerprint"], "expected_timeline_fingerprint"),
        _fingerprint(value["expected_authoring_fingerprint"], "expected_authoring_fingerprint"),
        commands,
    )


@dataclass(frozen=True, slots=True)
class TimelineReceiptV2:
    request_id: str
    transaction_id: str
    workspace_handle: str
    before_authoring_fingerprint: str
    after_authoring_fingerprint: str
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
    history_cursor: str | None
    selection: tuple[str, ...]
    authoring: NleAuthoringState

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
        render_snapshot = materialize_render_snapshot(self.authoring)
        return {
            "schema": TIMELINE_RECEIPT_SCHEMA_V2,
            "request_id": self.request_id,
            "transaction_id": self.transaction_id,
            "workspace_handle": self.workspace_handle,
            "before_authoring_fingerprint": self.before_authoring_fingerprint,
            "after_authoring_fingerprint": self.after_authoring_fingerprint,
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
            "authoring": self.authoring.to_wire(),
            "render_snapshot": None if render_snapshot is None else render_snapshot.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class TimelineHistoryEntryV2:
    sequence: int
    cursor: str
    before_authoring: NleAuthoringState
    after_authoring: NleAuthoringState
    before_selection: tuple[str, ...]
    after_selection: tuple[str, ...]
    commands_json: str
    affected_ids: tuple[str, ...]

    def byte_size(self) -> int:
        return _byte_size(
            {
                "sequence": self.sequence,
                "cursor": self.cursor,
                "before_authoring": self.before_authoring.to_wire(),
                "after_authoring": self.after_authoring.to_wire(),
                "before_selection": list(self.before_selection),
                "after_selection": list(self.after_selection),
                "commands": json.loads(self.commands_json),
                "affected_ids": list(self.affected_ids),
            }
        )


@dataclass(frozen=True, slots=True)
class _IdempotencyRecordV2:
    sequence: int
    request_id: str
    transaction_fingerprint: str
    receipt: TimelineReceiptV2

    def byte_size(self) -> int:
        return _byte_size(
            {
                "sequence": self.sequence,
                "request_id": self.request_id,
                "transaction_fingerprint": self.transaction_fingerprint,
                "receipt": self.receipt.to_wire(),
            }
        )


@dataclass(frozen=True, slots=True)
class TimelineHistoryStateV2:
    authoring: NleAuthoringState
    selection: tuple[str, ...] = ()
    undo_entries: tuple[TimelineHistoryEntryV2, ...] = ()
    redo_entries: tuple[TimelineHistoryEntryV2, ...] = ()
    idempotency_records: tuple[_IdempotencyRecordV2, ...] = ()
    invalidated_cursors: tuple[str, ...] = ()
    next_sequence: int = 1
    released: bool = False

    @classmethod
    def initialize(cls, authoring: NleAuthoringState) -> TimelineHistoryStateV2:
        if not isinstance(authoring, NleAuthoringState):
            raise _reject("invalid_transaction", "V2 history requires accepted authoring state")
        return cls(authoring)


def _byte_size(value: Mapping[str, object]) -> int:
    # CRITICAL: admitted video assets can contain 512 timing landmarks. The generic canonicalizer
    # caps arrays at 256 items, so using it for storage accounting rejects valid V2 edits before
    # the explicit byte quota can bound them. Measure the complete strict-JSON wire instead.
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _reject("invalid_transaction", "history material is not canonical JSON") from exc
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise _reject("resource_limit", "history material exceeds its byte limit")
    return len(encoded)


def _cursor(workspace_handle: str, sequence: int, authoring: NleAuthoringState) -> str:
    digest = canonical_fingerprint(
        {
            "schema": TIMELINE_HISTORY_CURSOR_SCHEMA_V2,
            "workspace_handle": workspace_handle,
            "sequence": sequence,
            "timeline_fingerprint": authoring.timeline_fingerprint,
            "authoring_fingerprint": authoring.authoring_fingerprint,
        }
    ).removeprefix("sha256:")
    return f"{TIMELINE_HISTORY_CURSOR_SCHEMA_V2}:{sequence}:{digest}"


def _receipt(
    transaction: TimelineTransactionV2,
    before: NleAuthoringState,
    after: NleAuthoringState,
    *,
    affected_ids: tuple[str, ...],
    inverse: Mapping[str, object],
    history_cursor: str | None,
    selection: tuple[str, ...],
) -> TimelineReceiptV2:
    return TimelineReceiptV2(
        transaction.request_id,
        transaction.transaction_id,
        transaction.workspace_handle,
        before.authoring_fingerprint,
        after.authoring_fingerprint,
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
        canonical_bytes(dict(inverse)).decode("utf-8"),
        history_cursor,
        selection,
        after,
    )


def _with_record(
    state: TimelineHistoryStateV2,
    transaction: TimelineTransactionV2,
    receipt: TimelineReceiptV2,
    *,
    sequence: int,
) -> TimelineHistoryStateV2:
    record = _IdempotencyRecordV2(
        sequence, transaction.request_id, transaction.fingerprint, receipt
    )
    return _bound_history(replace(state, idempotency_records=state.idempotency_records + (record,)))


def _bound_history(state: TimelineHistoryStateV2) -> TimelineHistoryStateV2:
    undo = list(state.undo_entries[-MAX_UNDO_ENTRIES:])
    redo = list(state.redo_entries[-MAX_REDO_ENTRIES:])
    records = list(state.idempotency_records[-MAX_IDEMPOTENCY_RECEIPTS:])
    while (
        sum(entry.byte_size() for entry in undo + redo)
        + sum(record.byte_size() for record in records)
        > MAX_HISTORY_BYTES_V2
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
    state: TimelineHistoryStateV2,
    transaction: TimelineTransactionV2,
) -> TimelineReceiptV2 | None:
    for record in state.idempotency_records:
        if record.request_id == transaction.request_id:
            if record.transaction_fingerprint != transaction.fingerprint:
                raise _reject("idempotency_conflict", "request identifier payload changed")
            return record.receipt
    return None


def _check_cas(state: TimelineHistoryStateV2, transaction: TimelineTransactionV2) -> None:
    authoring = state.authoring
    if transaction.workspace_handle != authoring.workspace_handle:
        raise _reject("invalid_transaction", "transaction targets another workspace")
    if transaction.expected_workspace_revision != authoring.workspace_revision:
        raise _reject("stale_workspace_revision", "workspace revision changed")
    if transaction.expected_timeline_revision != authoring.timeline_revision:
        raise _reject("stale_timeline_revision", "timeline revision changed")
    if transaction.expected_timeline_fingerprint != authoring.timeline_fingerprint:
        raise _reject("stale_timeline_fingerprint", "timeline fingerprint changed")
    if transaction.expected_authoring_fingerprint != authoring.authoring_fingerprint:
        raise _reject("stale_authoring_fingerprint", "authoring fingerprint changed")


def _advance(
    current: NleAuthoringState,
    source: NleAuthoringState,
) -> NleAuthoringState:
    if current.workspace_revision >= MAX_REVISION or current.timeline_revision >= MAX_REVISION:
        raise _reject("resource_limit", "revision capacity is exhausted")
    return create_nle_authoring_state(
        project_id=current.project_id,
        workspace_handle=current.workspace_handle,
        workspace_revision=current.workspace_revision + 1,
        timeline_revision=current.timeline_revision + 1,
        edit_capacity_frames=current.edit_capacity_frames,
        assets=current.assets,
        tracks=source.tracks,
        clips=source.clips,
        audio_extension=source.audio_extension,
        blockers=source.blockers,
    )


def refresh_authoring_asset_catalog_v2(
    state: TimelineHistoryStateV2,
    additions: tuple[PublicAsset, ...],
) -> TimelineHistoryStateV2:
    if not isinstance(state, TimelineHistoryStateV2) or state.released:
        raise _reject("invalid_transaction", "history is unavailable")
    if (
        type(additions) is not tuple
        or not additions
        or not all(type(item) is PublicAsset for item in additions)
    ):
        raise _reject("invalid_transaction", "catalog additions are invalid")
    if state.authoring.workspace_revision >= MAX_REVISION:
        raise _reject("resource_limit", "workspace revision capacity is exhausted")
    existing = {asset.asset_id: asset for asset in state.authoring.assets}
    if len({asset.asset_id for asset in additions}) != len(additions) or any(
        asset.asset_id in existing for asset in additions
    ):
        raise _reject("invalid_transaction", "catalog additions replace or repeat an asset")
    assets = list(state.authoring.assets)
    first_font = next(
        (index for index, asset in enumerate(assets) if asset.kind == "font"), len(assets)
    )
    assets[first_font:first_font] = additions
    authoring = create_nle_authoring_state(
        project_id=state.authoring.project_id,
        workspace_handle=state.authoring.workspace_handle,
        workspace_revision=state.authoring.workspace_revision + 1,
        timeline_revision=state.authoring.timeline_revision,
        edit_capacity_frames=state.authoring.edit_capacity_frames,
        assets=tuple(assets),
        tracks=state.authoring.tracks,
        clips=state.authoring.clips,
        audio_extension=state.authoring.audio_extension,
        blockers=state.authoring.blockers,
    )
    return replace(state, authoring=authoring)


def timeline_history_projection_v2_from_state(
    state: TimelineHistoryStateV2,
    *,
    workspace_handle: str | None = None,
) -> dict[str, object]:
    if not isinstance(state, TimelineHistoryStateV2) or state.released:
        raise _reject("invalid_transaction", "history is unavailable")
    handle = state.authoring.workspace_handle if workspace_handle is None else workspace_handle
    if handle != state.authoring.workspace_handle:
        raise _reject("invalid_transaction", "history projection targets another workspace")
    return timeline_history_projection_v2(handle, state)


def _apply_ordinary(
    state: TimelineHistoryStateV2,
    transaction: TimelineTransactionV2,
    *,
    effective_commands: tuple[NleCommand, ...] | None = None,
) -> tuple[TimelineHistoryStateV2, TimelineReceiptV2]:
    before = state.authoring
    authoring = before
    selection = state.selection
    affected: set[str] = set()
    commands = transaction.commands if effective_commands is None else effective_commands
    semantic_mutation = False
    for command in commands:
        try:
            transition = apply_nle_command(authoring, command, selection=selection)
        except ContractValidationError as exc:
            code = getattr(exc, "code", "invalid_command")
            message = str(exc).partition(": ")[2] or "command violates the authoring profile"
            raise _reject(code, message) from None
        if not isinstance(transition.snapshot, NleAuthoringState):
            raise AssertionError("V2 command returned a V1 render snapshot")
        authoring = transition.snapshot
        selection = transition.selection
        if len(selection) > MAX_SELECTED_CLIPS:
            raise _reject("resource_limit", "selection exceeds its bounded profile")
        affected.update(transition.affected_ids)
        semantic_mutation = semantic_mutation or transition.semantic_mutation

    sequence = state.next_sequence
    if not semantic_mutation:
        cursor = None
        candidate = replace(state, selection=selection, next_sequence=sequence + 1)
        receipt = _receipt(
            transaction,
            before,
            before,
            affected_ids=tuple(sorted(affected)),
            inverse={"kind": "restore_selection", "selection": list(state.selection)},
            history_cursor=cursor,
            selection=selection,
        )
        return _with_record(candidate, transaction, receipt, sequence=sequence), receipt

    after = _advance(before, authoring)
    cursor = _cursor(before.workspace_handle, sequence, after)
    commands_json = canonical_bytes([command.to_wire() for command in transaction.commands]).decode(
        "utf-8"
    )
    entry = TimelineHistoryEntryV2(
        sequence,
        cursor,
        before,
        after,
        state.selection,
        selection,
        commands_json,
        tuple(sorted(affected)),
    )
    candidate = replace(
        state,
        authoring=after,
        selection=selection,
        undo_entries=state.undo_entries + (entry,),
        redo_entries=(),
        invalidated_cursors=state.invalidated_cursors
        + tuple(item.cursor for item in state.redo_entries),
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
    return _with_record(candidate, transaction, receipt, sequence=sequence), receipt


def _cursor_disposition(
    state: TimelineHistoryStateV2,
    cursor: str,
    *,
    owner: tuple[TimelineHistoryEntryV2, ...],
    other: tuple[TimelineHistoryEntryV2, ...],
) -> TimelineHistoryEntryV2:
    if cursor in state.invalidated_cursors or any(entry.cursor == cursor for entry in other):
        raise _reject("history_branch_invalid", "history cursor belongs to another branch")
    if not owner or owner[-1].cursor != cursor:
        if any(entry.cursor == cursor for entry in owner):
            raise _reject("history_branch_invalid", "history cursor is not the branch head")
        raise _reject("history_cursor_invalid", "history cursor is unavailable or evicted")
    return owner[-1]


def _overlay_current_catalog(
    current: NleAuthoringState,
    historical: NleAuthoringState,
) -> NleAuthoringState:
    historical_assets = {asset.asset_id: asset for asset in historical.assets}
    current_assets = {asset.asset_id: asset for asset in current.assets}
    if any(current_assets.get(asset_id) != asset for asset_id, asset in historical_assets.items()):
        raise _reject("invalid_transaction", "history catalog conflicts with current authority")
    return create_nle_authoring_state(
        project_id=current.project_id,
        workspace_handle=current.workspace_handle,
        workspace_revision=historical.workspace_revision,
        timeline_revision=historical.timeline_revision,
        edit_capacity_frames=current.edit_capacity_frames,
        assets=current.assets,
        tracks=historical.tracks,
        clips=historical.clips,
        audio_extension=historical.audio_extension,
        blockers=historical.blockers,
    )


def _apply_history_move(
    state: TimelineHistoryStateV2,
    transaction: TimelineTransactionV2,
    *,
    undo: bool,
) -> tuple[TimelineHistoryStateV2, TimelineReceiptV2]:
    command = transaction.commands[0]
    requested_cursor = _history_cursor(command.payload["history_cursor"])
    owner = state.undo_entries if undo else state.redo_entries
    other = state.redo_entries if undo else state.undo_entries
    entry = _cursor_disposition(state, requested_cursor, owner=owner, other=other)
    before = state.authoring
    source = entry.before_authoring if undo else entry.after_authoring
    after = _advance(before, _overlay_current_catalog(before, source))
    sequence = state.next_sequence
    cursor = _cursor(before.workspace_handle, sequence, after)
    moved = replace(entry, sequence=sequence, cursor=cursor)
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
        authoring=after,
        selection=entry.before_selection if undo else entry.after_selection,
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
        inverse={"kind": inverse_kind, "history_cursor": cursor},
        history_cursor=cursor,
        selection=candidate.selection,
    )
    return _with_record(candidate, transaction, receipt, sequence=sequence), receipt


def _find_base_state(
    state: TimelineHistoryStateV2,
    timeline_fingerprint: str,
) -> NleAuthoringState | None:
    candidates = [state.authoring]
    for entry in (*state.undo_entries, *state.redo_entries):
        candidates.extend((entry.before_authoring, entry.after_authoring))
    return next(
        (
            item
            for item in reversed(candidates)
            if item.timeline_fingerprint == timeline_fingerprint
        ),
        None,
    )


def _clip_wire(state: NleAuthoringState, clip_id: str) -> dict[str, object] | None:
    clips = state.to_wire()["clips"]
    if not isinstance(clips, list):
        raise AssertionError("validated authoring state lost its clip list")
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
        return {key: item for key, item in text.items() if key != "content"}
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


def _rebase_commands(
    state: TimelineHistoryStateV2,
    command: NleCommand,
) -> tuple[NleCommand, ...]:
    payload = command.payload
    base_fingerprint = _fingerprint(
        payload["base_timeline_fingerprint"], "base_timeline_fingerprint"
    )
    base = _find_base_state(state, base_fingerprint)
    if base is None:
        raise _reject("rebase_conflict", "rebase base is unavailable or evicted")
    rows = payload["commands"]
    if not isinstance(rows, list):
        raise _reject("invalid_transaction", "rebase commands must be an array")
    commands = tuple(decode_nle_command(row) for row in rows)
    for member in commands:
        if member.kind is NleCommandKind.SELECT_CLIPS:
            selected = member.payload["clip_ids"]
            if not isinstance(selected, list) or any(
                not isinstance(clip_id, str)
                or _clip_wire(base, clip_id) is None
                or _clip_wire(state.authoring, clip_id) is None
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
        current_clip = _clip_wire(state.authoring, clip_id)
        if before_clip is None or current_clip is None:
            raise _reject("rebase_conflict", "property target changed or disappeared")
        if _property_projection(member, before_clip) != _property_projection(member, current_clip):
            raise _reject("rebase_conflict", "addressed property changed since the base")
    return commands


def apply_timeline_transaction_v2(
    state: TimelineHistoryStateV2,
    value: TimelineTransactionV2 | object,
) -> tuple[TimelineHistoryStateV2, TimelineReceiptV2]:
    if not isinstance(state, TimelineHistoryStateV2):
        raise _reject("invalid_transaction", "V2 transaction requires V2 history state")
    transaction = decode_timeline_transaction_v2(
        value.to_wire() if isinstance(value, TimelineTransactionV2) else value
    )
    if state.released:
        raise _reject("workspace_released", "workspace history has been released")
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


def timeline_history_projection_v2(
    workspace_handle: str,
    state: TimelineHistoryStateV2,
    rejection: str | None = None,
) -> dict[str, object]:
    if workspace_handle != state.authoring.workspace_handle:
        raise _reject("invalid_transaction", "history projection targets another workspace")
    authoring = state.authoring
    render_snapshot = materialize_render_snapshot(authoring)
    return {
        "schema": TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
        "workspace_handle": workspace_handle,
        "authoring": authoring.to_wire(),
        "render_snapshot": None if render_snapshot is None else render_snapshot.to_wire(),
        "selection": list(state.selection),
        "undo_cursor": state.undo_entries[-1].cursor if state.undo_entries else None,
        "redo_cursor": state.redo_entries[-1].cursor if state.redo_entries else None,
        "rejection": None if rejection is None else {"code": rejection},
    }


def release_timeline_history_v2(state: TimelineHistoryStateV2) -> TimelineHistoryStateV2:
    if not isinstance(state, TimelineHistoryStateV2):
        raise _reject("invalid_transaction", "release requires V2 history state")
    return replace(
        state,
        undo_entries=(),
        redo_entries=(),
        idempotency_records=(),
        invalidated_cursors=(),
        released=True,
    )


__all__ = [
    "TimelineHistoryStateV2",
    "TimelineHistoryV2Error",
    "TimelineReceiptV2",
    "TimelineTransactionV2",
    "apply_timeline_transaction_v2",
    "decode_timeline_transaction_v2",
    "refresh_authoring_asset_catalog_v2",
    "release_timeline_history_v2",
    "timeline_history_projection_v2",
]
