"""Portable documents stage fresh data owners, never execution authority."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch

from test_project_document import document_wire

from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringWorkspaceRegistry
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkbenchError,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.project_document import decode_project_value

if TYPE_CHECKING:
    from comfyui_h3_context.adapters.project_document_service import ProjectDocumentService


class ProjectDocumentServiceTests(unittest.TestCase):
    def service(self, **kwargs: Any) -> ProjectDocumentService:
        name = "comfyui_h3_context.adapters.project_document_service"
        self.assertIsNotNone(importlib.util.find_spec(name), "paired document owner is required")
        from comfyui_h3_context.adapters.project_document_service import ProjectDocumentService

        return ProjectDocumentService(
            ProductionWorkspaceRegistry(
                seed_claim=Mock(side_effect=AssertionError("no Context claim during Open"))
            ),
            AuthoringWorkspaceRegistry(),
            **kwargs,
        )

    def test_open_has_fresh_pair_missing_media_and_no_execution(self) -> None:
        service = self.service()
        first = service.open_document(document_wire())
        second = service.open_document(document_wire())
        self.assertNotEqual(first["owner"], second["owner"])
        self.assertEqual(first["missing_media"], ["video.saved"])
        self.assertEqual(first["history"]["authoring"]["clips"][0]["clip_id"], "clip.saved")
        production = first["production"]
        self.assertNotIn("admit_generation_destination", production["allowed_actions"])
        self.assertNotIn("assemble_sequence", production["allowed_actions"])
        handle = first["owner"]["production_handle"]
        with self.assertRaises(ProductionWorkbenchError):
            service.production.claim_workspace_authority(
                handle,
                expected_workspace_revision=1,
                expected_workspace_fingerprint=production["workspace_fingerprint"],
            )
        entry = service.authoring._entries[first["owner"]["authoring_handle"]]
        self.assertIsNone(entry.source_binding)
        self.assertIsNone(entry.initialized_sources)

    def test_snapshot_matches_independent_all_field_fixture(self) -> None:
        service = self.service()
        expected = document_wire(frames=512)
        opened = service.open_document(copy.deepcopy(expected))
        result = service.snapshot_document(opened["owner"], expected["planning"], expected["title"])
        self.assertEqual(result["document"], expected)
        self.assertEqual(result["owner"], opened["owner"])

    def test_bad_document_does_not_mutate_old_or_publish_partial_pair(self) -> None:
        service = self.service()
        opened = service.open_document(document_wire())
        before = service.snapshot_document(
            opened["owner"], document_wire()["planning"], "Synthetic edit"
        )
        bad = document_wire()
        bad["editor"]["path"] = "forbidden"
        with self.assertRaises(ValueError):
            service.open_document(bad)
        self.assertEqual(len(service.production._editable_projects), 1)
        self.assertEqual(len(service.authoring._entries), 1)
        self.assertEqual(
            before,
            service.snapshot_document(
                opened["owner"], document_wire()["planning"], "Synthetic edit"
            ),
        )

    def test_projection_failure_is_atomic(self) -> None:
        service = self.service()
        opened = service.open_document(document_wire())
        with patch.object(
            service.authoring, "_history_projection", side_effect=ValueError("synthetic")
        ):
            with self.assertRaises(ValueError):
                service.open_document(document_wire())
        self.assertEqual(
            tuple(service.production._editable_projects), (opened["owner"]["production_handle"],)
        )
        self.assertEqual(tuple(service.authoring._entries), (opened["owner"]["authoring_handle"],))

    def test_mixed_revision_pair_refuses_without_export(self) -> None:
        service = self.service()
        opened = service.open_document(document_wire())
        stale = dict(opened["owner"], timeline_revision=2)
        with self.assertRaisesRegex(ValueError, "project_conflict"):
            service.snapshot_document(stale, document_wire()["planning"], "Synthetic edit")

    def test_data_only_selection_can_edit_and_save(self) -> None:
        service = self.service()
        opened = service.open_document(document_wire())
        production = opened["production"]
        result = service.production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "select.portable",
                "action": "set_selection",
                "payload": {
                    "workspace_handle": production["workspace_handle"],
                    "expected_workspace_revision": production["workspace_revision"],
                    "expected_workspace_fingerprint": production["workspace_fingerprint"],
                    "segment_ids": [],
                },
            }
        )
        assert isinstance(result.projection, ProductionWorkbenchProjection)
        owner = dict(
            opened["owner"],
            production_revision=result.projection.workspace_revision,
            production_fingerprint=result.projection.workspace_fingerprint,
        )
        snapshot = service.snapshot_document(owner, document_wire()["planning"], "Synthetic edit")
        self.assertEqual(snapshot["document"]["production"]["selection"], [])

    def test_empty_document_still_owns_an_editor(self) -> None:
        service = self.service()
        wire = document_wire()
        wire["production"] = {"segments": [], "selection": []}
        wire["editor"] = None
        wire["media"] = []
        opened = service.open_document(wire)
        self.assertIsNone(opened["production"])
        self.assertEqual(opened["history"]["authoring"]["clips"], [])
        self.assertEqual(opened["missing_media"], [])
        decode_project_value(
            service.snapshot_document(opened["owner"], wire["planning"], wire["title"])["document"]
        )

    def test_expired_data_project_cannot_be_kept_alive_by_read(self) -> None:
        service = self.service()
        clock = Mock(return_value=0.0)
        service.production._clock = clock
        opened = service.open_document(document_wire())
        clock.return_value = service.production._ttl_seconds + 1.0
        with self.assertRaises(ProductionWorkbenchError):
            service.production.dispatch(
                {
                    "schema": PRODUCTION_ACTION_SCHEMA,
                    "request_id": "expired.read",
                    "action": "read_projection",
                    "payload": {"workspace_handle": opened["owner"]["production_handle"]},
                }
            )
        self.assertFalse(service.production._editable_projects)

    def test_runtime_handle_allocator_cannot_collide_with_file_owner(self) -> None:
        service = self.service()
        opened = service.open_document(document_wire())
        with patch(
            "comfyui_h3_context.adapters.comfyui_production_workspace.secrets.token_urlsafe",
            return_value=opened["owner"]["production_handle"][3:],
        ):
            with self.assertRaises(ProductionWorkbenchError):
                service.production._new_workspace_handle()

    def test_production_only_export_never_creates_an_editor(self) -> None:
        from test_production_workbench import _create, _setup

        _sidebar, production, context = _setup()
        projection = production.dispatch(_create(context)).projection
        assert isinstance(projection, ProductionWorkbenchProjection)
        service = self.service()
        service.production = production
        owner: dict[str, Any] = {
            key: None
            for key in __import__(
                "comfyui_h3_context.adapters.project_document_service", fromlist=["OWNER_FIELDS"]
            ).OWNER_FIELDS
        }
        owner.update(
            production_handle=projection.workspace_handle,
            production_id=projection.workspace_id,
            production_revision=projection.workspace_revision,
            production_fingerprint=projection.workspace_fingerprint,
        )
        result = service.snapshot_document(owner, document_wire()["planning"], "Synthetic")
        self.assertIsNone(result["document"]["editor"])
        self.assertEqual(result["document"]["production"]["segments"][0]["task_mode"], "t2va")
        self.assertFalse(service.authoring._entries)
        decode_project_value(result["document"])

    def test_raw_open_refuses_duplicate_keys_before_ownership(self) -> None:
        service = self.service()
        with self.assertRaises(ValueError):
            service.open_document('{"format":"h3proj","format":"forbidden"}')
        self.assertFalse(service.authoring._entries)
        self.assertFalse(service.production._editable_projects)

    def test_production_only_export_reads_asset_ids_from_real_seed_rows(self) -> None:
        from test_production_workbench import _create, _setup

        from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarAuthoringSource
        from comfyui_h3_context.core.contracts import MediaKind

        _sidebar, production, context = _setup()
        projection = production.dispatch(_create(context)).projection
        assert isinstance(projection, ProductionWorkbenchProjection)
        anchor = production._authoring_anchors[projection.workspace_handle]
        anchor.seed = replace(
            anchor.seed,
            sources=(
                SidebarAuthoringSource(
                    "video.saved",
                    MediaKind.VIDEO,
                    1000,
                    None,
                    0,
                    "sha256:" + "a" * 64,
                ),
            ),
        )
        service = self.service()
        service.production = production
        owner: dict[str, Any] = {
            key: None
            for key in __import__(
                "comfyui_h3_context.adapters.project_document_service", fromlist=["OWNER_FIELDS"]
            ).OWNER_FIELDS
        }
        owner.update(
            production_handle=projection.workspace_handle,
            production_id=projection.workspace_id,
            production_revision=projection.workspace_revision,
            production_fingerprint=projection.workspace_fingerprint,
        )
        response = service.snapshot_document(owner, document_wire()["planning"], "Synthetic")
        self.assertIsNone(response["document"]["editor"])
        self.assertFalse(service.authoring._entries)

    def test_delete_prunes_media_only_used_by_deleted_segment(self) -> None:
        service = self.service()
        wire = document_wire()
        wire["editor"] = None
        first = wire["production"]["segments"][0]
        first["source_asset_id"] = "video.saved"
        second = dict(first, segment_id="segment.other", source_asset_id="video.other")
        wire["production"]["segments"].append(second)
        wire["media"].append(dict(wire["media"][0], asset_id="video.other"))
        opened = service.open_document(wire)
        owner = opened["owner"]
        result = service.production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "delete.portable",
                "action": "delete_segment",
                "payload": {
                    "workspace_handle": owner["production_handle"],
                    "expected_workspace_revision": owner["production_revision"],
                    "expected_workspace_fingerprint": owner["production_fingerprint"],
                    "segment_id": "segment.other",
                },
            }
        )
        self.assertEqual(
            [
                row.asset_id
                for row in service.production._editable_projects[
                    owner["production_handle"]
                ].document.media
            ],
            ["video.saved"],
        )
        self.assertEqual(result.status, 200)

    def test_standalone_editor_snapshot_does_not_create_production(self) -> None:
        service = self.service()
        opened = service.open_document(document_wire())
        handle = opened["owner"]["production_handle"]
        entry = service.authoring._entries[opened["owner"]["authoring_handle"]]
        entry.production_owner = None
        service.production._editable_projects.pop(handle)
        service.production._authoring_anchors.pop(handle)
        owner = dict(
            opened["owner"],
            **{key: None for key in opened["owner"] if key.startswith("production_")},
        )
        result = service.snapshot_document(owner, document_wire()["planning"], "Standalone")
        self.assertEqual(result["document"]["production"], {"segments": [], "selection": []})
        self.assertEqual(result["document"]["editor"]["clips"], document_wire()["editor"]["clips"])
        self.assertFalse(service.production._editable_projects)

    def test_reference_edit_updates_portable_intent_not_missing_placeholders(self) -> None:
        from comfyui_h3_context.core.reference_set_authoring import AddSource, apply_command

        service = self.service()
        wire = document_wire()
        wire["editor"]["reference"]["sources"].append(
            {"source_id": "video.missing", "kind": "video", "duration_milliseconds": 1000}
        )
        wire["media"].append(dict(wire["media"][0], asset_id="video.missing"))
        opened = service.open_document(wire)
        entry = service.authoring._entries[opened["owner"]["authoring_handle"]]
        entry.reference, _ = apply_command(
            entry.reference, AddSource(entry.reference.revision, entry.universe["video.saved"])
        )
        service.authoring._apply_reference(
            entry,
            "remove_source",
            {"expected_reference_revision": entry.reference.revision, "source_id": "video.saved"},
        )
        owner = dict(opened["owner"], reference_revision=entry.reference.revision)
        result = service.snapshot_document(owner, wire["planning"], wire["title"])
        self.assertEqual(
            result["document"]["editor"]["reference"]["sources"],
            wire["editor"]["reference"]["sources"][1:],
        )


if __name__ == "__main__":
    unittest.main()
