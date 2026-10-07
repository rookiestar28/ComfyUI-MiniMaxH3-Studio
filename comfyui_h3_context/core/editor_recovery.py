"""Bounded editable recovery data with fresh history, never persisted runtime grants."""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from typing import Any

from .durable_workspace_state import require_owner
from .nle_authoring_contract import NleAuthoringState, create_nle_authoring_state
from .project_document import (
    ProjectDocument,
    ProjectDocumentError,
    closed,
    decode_project_value,
    editable_authoring_wire,
    identifier,
    integer,
    project_bytes,
    rows,
)
from .timeline_history import MAX_REDO_ENTRIES, MAX_SELECTED_CLIPS, MAX_UNDO_ENTRIES
from .timeline_history_v2 import (
    MAX_HISTORY_BYTES_V2,
    TimelineHistoryEntryV2,
    TimelineHistoryStateV2,
    _cursor,
)

RECOVERY_SCHEMA = "h3.context.project_recovery_snapshot.v1"
MAX_RECOVERY_BYTES = 10 * 1024 * 1024
MAX_HISTORY_NODES = 1_048_576
MAX_RECOVERY_REVISION = (1 << 53) - 1
_PROJECT = re.compile(r"project_[0-9a-f]{32}\Z")


class EditorRecoveryError(ValueError):
    """Finite content-free refusals; never reflect document data or host locators."""

    CODES = frozenset(
        {
            "snapshot_invalid",
            "snapshot_corrupt",
            "snapshot_unavailable",
            "version_unsupported",
            "owner_mismatch",
            "project_invalid",
            "project_unavailable",
            "history_invalid",
            "history_bound",
            "recovery_disabled",
            "revision_conflict",
            "revision_exhausted",
            "storage_unsafe",
            "storage_unavailable",
            "storage_unverified",
            "catalog_busy",
            "owner_busy",
            "quota_bytes",
            "quota_records",
            "quota_directory",
            "service_closed",
            "host_unqualified",
            "filesystem_unqualified",
            "owner_changed",
            "dirty_protected",
            "active_project",
            "command_invalid",
            "cancelled",
            "timed_out",
            "internal_failure",
            "media_unavailable",
        }
    )

    def __init__(self, code: str = "snapshot_invalid") -> None:
        self.code = code if code in self.CODES else "internal_failure"
        super().__init__(self.code)


def require_project(value: object) -> str:
    if type(value) is not str or _PROJECT.fullmatch(value) is None:
        raise EditorRecoveryError("project_invalid")
    return value


def recovery_json(data: bytes, *, maximum_bytes: int = MAX_RECOVERY_BYTES) -> object:
    if type(data) is not bytes or not 1 <= len(data) <= maximum_bytes:
        raise EditorRecoveryError("history_bound")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise EditorRecoveryError()
            result[key] = value
        return result

    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(EditorRecoveryError()),
        )
    except (UnicodeError, ValueError, RecursionError):
        raise EditorRecoveryError() from None
    pending, count = [(value, 0)], 0
    while pending:
        child, depth = pending.pop()
        count += 1
        if depth > 32 or count > MAX_HISTORY_NODES:
            raise EditorRecoveryError("history_bound")
        if type(child) is dict:
            pending.extend((item, depth + 1) for item in child.values())
        elif type(child) is list:
            pending.extend((item, depth + 1) for item in child)
        elif type(child) is float and not math.isfinite(child):
            raise EditorRecoveryError()
    return value


def _selection(value: object, state: NleAuthoringState) -> tuple[str, ...]:
    selected = tuple(identifier(key) for key in rows(value, MAX_SELECTED_CLIPS))
    if len(set(selected)) != len(selected) or not set(selected).issubset(
        clip.clip_id for clip in state.clips
    ):
        raise EditorRecoveryError("history_invalid")
    return selected


def history_data(history: TimelineHistoryStateV2 | None) -> dict[str, Any]:
    if history is None:
        return {"selection": [], "undo": [], "redo": []}
    if history.released:
        raise EditorRecoveryError("history_invalid")

    def entry(row: TimelineHistoryEntryV2) -> dict[str, object]:
        # SECURITY: inverses are data only; runtime cursors/receipts/commands never enter storage.
        return {
            "before": editable_authoring_wire(row.before_authoring),
            "after": editable_authoring_wire(row.after_authoring),
            "before_selection": list(row.before_selection),
            "after_selection": list(row.after_selection),
            "affected_ids": list(row.affected_ids),
        }

    value = {
        "selection": list(history.selection),
        "undo": [entry(row) for row in history.undo_entries],
        "redo": [entry(row) for row in history.redo_entries],
    }
    try:
        project_bytes(value, maximum_bytes=MAX_HISTORY_BYTES_V2)
    except ProjectDocumentError:
        raise EditorRecoveryError("history_bound") from None
    return value


def restore_history_data(
    document: ProjectDocument,
    value: object,
    *,
    project_id: str,
    workspace_handle: str,
) -> TimelineHistoryStateV2:
    try:
        wire = closed(value, {"selection", "undo", "redo"})
        project_bytes(wire, maximum_bytes=MAX_HISTORY_BYTES_V2)
        current = document.fresh_authoring(project_id, workspace_handle)
        current_assets = {asset.asset_id: asset for asset in current.assets}

        def state(data: object) -> NleAuthoringState:
            base = document.to_wire()
            material = closed(data, set(editable_authoring_wire(current)))
            base["editor"] = dict(material, reference=document.reference)
            historical = decode_project_value(base).fresh_authoring(project_id, workspace_handle)
            if any(current_assets.get(asset.asset_id) != asset for asset in historical.assets):
                raise EditorRecoveryError("history_invalid")
            return create_nle_authoring_state(
                project_id=project_id,
                workspace_handle=workspace_handle,
                workspace_revision=1,
                timeline_revision=1,
                edit_capacity_frames=current.edit_capacity_frames,
                assets=current.assets,
                tracks=historical.tracks,
                clips=historical.clips,
                audio_extension=historical.audio_extension,
            )

        sequence = 0

        def entries(data: object, maximum: int) -> tuple[TimelineHistoryEntryV2, ...]:
            nonlocal sequence
            result = []
            for item in rows(data, maximum):
                row = closed(
                    item, {"before", "after", "before_selection", "after_selection", "affected_ids"}
                )
                before, after = state(row["before"]), state(row["after"])
                affected = tuple(identifier(key) for key in rows(row["affected_ids"], 256))
                if len(set(affected)) != len(affected):
                    raise EditorRecoveryError("history_invalid")
                sequence += 1
                result.append(
                    TimelineHistoryEntryV2(
                        sequence,
                        _cursor(workspace_handle, sequence, after),
                        before,
                        after,
                        _selection(row["before_selection"], before),
                        _selection(row["after_selection"], after),
                        "[]",
                        affected,
                    )
                )
            return tuple(result)

        undo, redo = (
            entries(wire["undo"], MAX_UNDO_ENTRIES),
            entries(wire["redo"], MAX_REDO_ENTRIES),
        )
        if document.editor is None and (undo or redo or wire["selection"]):
            raise EditorRecoveryError("history_invalid")
        for left, right in zip(undo, undo[1:], strict=False):
            if editable_authoring_wire(left.after_authoring) != editable_authoring_wire(
                right.before_authoring
            ):
                raise EditorRecoveryError("history_invalid")
        if undo and editable_authoring_wire(undo[-1].after_authoring) != editable_authoring_wire(
            current
        ):
            raise EditorRecoveryError("history_invalid")
        next_state = current
        for row in reversed(redo):
            if editable_authoring_wire(row.before_authoring) != editable_authoring_wire(next_state):
                raise EditorRecoveryError("history_invalid")
            next_state = row.after_authoring
        if sum(row.byte_size() for row in (*undo, *redo)) > MAX_HISTORY_BYTES_V2:
            raise EditorRecoveryError("history_bound")
        # CRITICAL: all restored cursors belong to the new handle; old ledgers remain empty.
        return replace(
            TimelineHistoryStateV2.initialize(current),
            selection=_selection(wire["selection"], current),
            undo_entries=undo,
            redo_entries=redo,
            next_sequence=sequence + 1,
        )
    except EditorRecoveryError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise EditorRecoveryError("history_invalid") from None


def decode_recovery_snapshot(data: bytes, *, expected_owner: str) -> dict[str, Any]:
    try:
        value = closed(
            recovery_json(data),
            {"schema", "owner_id", "project_id", "revision", "saved_at_ms", "document", "history"},
        )
        if value["schema"] != RECOVERY_SCHEMA:
            raise EditorRecoveryError("version_unsupported")
        require_owner(expected_owner)
        if value["owner_id"] != expected_owner:
            raise EditorRecoveryError("owner_mismatch")
        require_project(value["project_id"])
        integer(value["revision"], 1, MAX_RECOVERY_REVISION)
        integer(value["saved_at_ms"], 1, 4_102_444_800_000)
        document = decode_project_value(value["document"])
        restore_history_data(
            document,
            value["history"],
            project_id="project.validated",
            workspace_handle="authoring-validated",
        )
        return value
    except EditorRecoveryError:
        raise
    except ValueError:
        raise EditorRecoveryError() from None
