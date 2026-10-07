"""Recovery copies editable history, never its original runtime authority."""

from __future__ import annotations

import copy
import json
import unittest
from typing import Any
from unittest.mock import Mock

from test_m25_timeline_history_v2 import transaction
from test_project_document import document_wire

from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringWorkspaceRegistry
from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkspaceRegistry
from comfyui_h3_context.adapters.project_document_service import ProjectDocumentService
from comfyui_h3_context.core.editor_recovery import (
    RECOVERY_SCHEMA,
    EditorRecoveryError,
    decode_recovery_snapshot,
    history_data,
    recovery_json,
    restore_history_data,
)
from comfyui_h3_context.core.project_document import decode_project_value, editable_authoring_wire
from comfyui_h3_context.core.timeline_authoring import NleCommand, NleCommandKind
from comfyui_h3_context.core.timeline_history_v2 import (
    TimelineHistoryStateV2,
    TimelineHistoryV2Error,
    apply_timeline_transaction_v2,
)


def projects() -> ProjectDocumentService:
    return ProjectDocumentService(
        ProductionWorkspaceRegistry(seed_claim=Mock(side_effect=AssertionError("no Context"))),
        AuthoringWorkspaceRegistry(),
    )


class EditorRecoveryTests(unittest.TestCase):
    def edited(self, frames: int = 24) -> tuple[dict[str, Any], TimelineHistoryStateV2]:
        wire = document_wire(frames)
        state = TimelineHistoryStateV2.initialize(
            decode_project_value(wire).fresh_authoring("project.original", "authoring-original")
        )
        state, _ = apply_timeline_transaction_v2(
            state,
            transaction(
                state,
                "edit-disabled",
                NleCommand(
                    NleCommandKind.SET_CLIP_ENABLED, {"clip_id": "clip.saved", "enabled": False}
                ),
            ),
        )
        wire["editor"].update(editable_authoring_wire(state.authoring))
        return wire, state

    def test_snapshot_is_data_only_and_explicit_restore_issues_fresh_history(self) -> None:
        service = projects()
        wire = document_wire()
        first = service.open_document(wire)
        capture: Any = getattr(service, "capture_recovery", None)
        self.assertTrue(callable(capture), "editable recovery capture is required, not metadata")
        snapshot = capture(first["owner"], wire["planning"], wire["title"])
        self.assertEqual(snapshot["document"], wire)
        self.assertEqual(snapshot["history"], {"selection": [], "undo": [], "redo": []})
        restore: Any = getattr(service, "restore_recovery", None)
        self.assertTrue(callable(restore), "fresh data/history restore is required")
        second = restore(snapshot)
        self.assertNotEqual(second["owner"], first["owner"])
        self.assertEqual(second["missing_media"], ["video.saved"])
        self.assertIsNone(
            service.authoring._entries[second["owner"]["authoring_handle"]].source_binding
        )

    def test_real_history_restores_fresh_undo_redo_without_old_cursors_or_ledger(self) -> None:
        wire, original = self.edited(512)
        restored = restore_history_data(
            decode_project_value(wire),
            history_data(original),
            project_id="project.new",
            workspace_handle="authoring-new",
        )
        self.assertEqual(restored.authoring.assets, original.authoring.assets)
        self.assertEqual(restored.idempotency_records, ())
        self.assertEqual(restored.invalidated_cursors, ())
        old, fresh = original.undo_entries[-1].cursor, restored.undo_entries[-1].cursor
        self.assertNotEqual(old, fresh)
        with self.assertRaises(TimelineHistoryV2Error):
            apply_timeline_transaction_v2(
                restored,
                transaction(
                    restored, "old-undo", NleCommand(NleCommandKind.UNDO, {"history_cursor": old})
                ),
            )
        undone, receipt = apply_timeline_transaction_v2(
            restored,
            transaction(
                restored, "new-undo", NleCommand(NleCommandKind.UNDO, {"history_cursor": fresh})
            ),
        )
        self.assertTrue(undone.authoring.clips[0].enabled)
        self.assertEqual(restored.undo_entries[0].commands_json, "[]")
        redo_wire = copy.deepcopy(wire)
        redo_wire["editor"].update(editable_authoring_wire(undone.authoring))
        reopened = restore_history_data(
            decode_project_value(redo_wire),
            history_data(undone),
            project_id="project.redo",
            workspace_handle="authoring-redo",
        )
        self.assertNotEqual(reopened.redo_entries[-1].cursor, receipt.history_cursor)
        redone, _ = apply_timeline_transaction_v2(
            reopened,
            transaction(
                reopened,
                "new-redo",
                NleCommand(
                    NleCommandKind.REDO, {"history_cursor": reopened.redo_entries[-1].cursor}
                ),
            ),
        )
        self.assertFalse(redone.authoring.clips[0].enabled)
        self.assertEqual(len(redone.authoring.assets[0].landmarks), 512)

    def test_history_before_later_asset_import_uses_verified_complete_current_catalog(self) -> None:
        wire, original = self.edited()
        added = copy.deepcopy(wire["editor"]["assets"][0])
        added["asset_id"] = "video.added"
        wire["editor"]["assets"].append(added)
        reference = copy.deepcopy(wire["editor"]["reference"]["sources"][0])
        reference["source_id"] = "video.added"
        wire["editor"]["reference"]["sources"].append(reference)
        media = copy.deepcopy(wire["media"][0])
        media["asset_id"] = "video.added"
        wire["media"].append(media)
        restored = restore_history_data(
            decode_project_value(wire),
            history_data(original),
            project_id="project.new",
            workspace_handle="authoring-new",
        )
        self.assertEqual(len(restored.undo_entries[0].before_authoring.assets), 2)
        self.assertEqual(
            restored.undo_entries[0].before_authoring.assets, restored.authoring.assets
        )
        tampered = history_data(original)
        tampered["undo"][0]["before"]["assets"][0]["source_frame_count"] = 1
        with self.assertRaises(EditorRecoveryError):
            restore_history_data(
                decode_project_value(wire),
                tampered,
                project_id="project.new",
                workspace_handle="authoring-new",
            )

    def test_unknown_authority_disconnected_branches_and_selection_refuse(self) -> None:
        wire, original = self.edited()
        for field in ("cursor", "commands_json", "workspace_handle", "receipt"):
            invalid = history_data(original)
            invalid["undo"][0][field] = "old authority"
            with self.subTest(field=field), self.assertRaises(EditorRecoveryError):
                restore_history_data(
                    decode_project_value(wire),
                    invalid,
                    project_id="project.new",
                    workspace_handle="authoring-new",
                )
        for mutation in ("selection", "chain", "overflow"):
            invalid = history_data(original)
            if mutation == "selection":
                invalid["selection"] = ["clip.absent"]
            elif mutation == "chain":
                invalid["undo"][0]["after"]["clips"][0]["enabled"] = True
            else:
                invalid["undo"] *= 65
            with self.subTest(mutation=mutation), self.assertRaises(EditorRecoveryError):
                restore_history_data(
                    decode_project_value(wire),
                    invalid,
                    project_id="project.new",
                    workspace_handle="authoring-new",
                )

    def test_snapshot_owner_version_duplicate_nonfinite_and_content_free_errors(self) -> None:
        owner = "owner_" + "a" * 32
        value = {
            "schema": RECOVERY_SCHEMA,
            "owner_id": owner,
            "project_id": "project_" + "b" * 32,
            "revision": 1,
            "saved_at_ms": 1791375400000,
            "document": document_wire(),
            "history": {"selection": [], "undo": [], "redo": []},
        }
        encoded = json.dumps(value).encode()
        self.assertEqual(decode_recovery_snapshot(encoded, expected_owner=owner), value)
        for field, replacement, code in (
            ("owner_id", "owner_" + "c" * 32, "owner_mismatch"),
            ("schema", "future private schema", "version_unsupported"),
            ("revision", True, "snapshot_invalid"),
        ):
            invalid = dict(value, **{field: replacement})
            with self.subTest(field=field), self.assertRaises(EditorRecoveryError) as error:
                decode_recovery_snapshot(json.dumps(invalid).encode(), expected_owner=owner)
            self.assertEqual(str(error.exception), code)
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b'"\xff"'):
            with self.subTest(raw=raw), self.assertRaises(EditorRecoveryError):
                recovery_json(raw)


if __name__ == "__main__":
    unittest.main()
