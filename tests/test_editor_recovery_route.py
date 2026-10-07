"""Recovery edge uses qualified loopback owners, closed commands and explicit restore."""

from __future__ import annotations

import importlib
import importlib.util
import os
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock, patch

from test_project_document import document_wire
from test_project_document_route import ProjectDocumentRouteTests

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.editor_recovery_service import EditorRecoveryService
from comfyui_h3_context.adapters.recovery_owner import RecoveryOwner
from comfyui_h3_context.adapters.segment_artifact_store import _windows_native_path
from comfyui_h3_context.core.durable_workspace_state import DurableStateError
from comfyui_h3_context.core.project_document import ProjectDocumentError


class EditorRecoveryRouteTests(ProjectDocumentRouteTests):
    def register(self) -> tuple[ModuleType, Any, Any]:
        name = "comfyui_h3_context.adapters.comfyui_editor_recovery"
        self.assertIsNotNone(importlib.util.find_spec(name), "missing qualified recovery route")
        module = importlib.import_module(name)
        _base, projects, _handler = super().register()
        temporary = tempfile.TemporaryDirectory(
            dir=(
                _windows_native_path(Path(tempfile.gettempdir()), force=True)
                if os.name == "nt"
                else Path(tempfile.gettempdir())
            )
        )
        self.addCleanup(temporary.cleanup)
        recovery = EditorRecoveryService(
            projects,
            owner_port=Mock(
                resolve=Mock(return_value=RecoveryOwner("owner_" + "a" * 32, Path(temporary.name)))
            ),
        )
        self.addCleanup(recovery.close)
        replacement = composition_root.substituted(composition_root.EDITOR_RECOVERY, recovery)
        replacement.__enter__()
        self.addCleanup(replacement.__exit__, None, None, None)
        self.assertTrue(module.ensure_editor_recovery_route_registered())
        self.assertTrue(module.ensure_editor_recovery_route_registered())
        self.assertEqual(len(self.routes), 2)
        return module, recovery, self.routes[-1].handler

    async def test_explicit_save_restore_catalog_and_privacy(self) -> None:
        _module, recovery, handler = self.register()
        response = await handler(self.request({"intent": "status", "project_id": None}))
        self.assertEqual(response.status, 200)
        self.assertFalse(response.body["enabled"])
        enabled = await handler(
            self.request(
                {
                    "intent": "settings",
                    "enabled": True,
                    "include_video": False,
                    "expected_revision": 0,
                }
            )
        )
        self.assertEqual(enabled.status, 200)
        wire = document_wire()
        opened = recovery.projects.open_document(wire)
        watched = await handler(
            self.request(
                {
                    "intent": "watch",
                    "owner": opened["owner"],
                    "planning": wire["planning"],
                    "title": wire["title"],
                    "project_id": None,
                }
            )
        )
        project = watched.body["current"]["project_id"]
        saved = await handler(self.request({"intent": "flush", "project_id": project}))
        self.assertEqual(saved.body["current"]["state"], "saved")
        self.assertNotIn("Synthetic", str(saved.body))
        restored = await handler(self.request({"intent": "restore", "project_id": project}))
        self.assertEqual(restored.status, 200)
        self.assertEqual(restored.body["schema"], "h3.context.project_document.response.v1")
        self.assertNotEqual(restored.body["owner"], opened["owner"])
        self.assertEqual(restored.headers["Cache-Control"], "no-store")

    async def test_owner_refusal_precedes_body_and_defaults_have_no_io(self) -> None:
        _module, recovery, handler = self.register()
        recovery.owner_port.resolve.side_effect = DurableStateError("host_unqualified")
        response = await handler(self.request({}, unreadable=True))
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["code"], "host_unqualified")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIsNone(recovery._store)

    async def test_origin_size_and_unknown_command_refuse_before_staging(self) -> None:
        module, recovery, handler = self.register()
        for request, status in (
            (self.request({}, origin="https://foreign.invalid", unreadable=True), 403),
            (self.request({}, unreadable=True, length=module.MAX_RECOVERY_REQUEST_BYTES + 1), 413),
            (self.request({"intent": "queue", "path": "private"}), 400),
        ):
            response = await handler(request)
            self.assertEqual(response.status, status)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse(recovery.projects.authoring._entries)

    async def test_closed_transport_cannot_publish_new_pair(self) -> None:
        _module, recovery, handler = self.register()
        response = await handler(
            self.request({"intent": "status", "project_id": None}, closing=True)
        )
        self.assertNotEqual(response.status, 200)
        self.assertFalse(recovery.projects.authoring._entries)

    async def test_actual_open_export_pair_and_no_store(self) -> None:
        # The inherited portable-file journey remains in its own suite; this edge is recovery-only.
        _module, _recovery, handler = self.register()
        response = await handler(self.request({"intent": "status", "project_id": None}))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_failed_response_encoding_discards_the_undelivered_restored_pair(self) -> None:
        import time

        module, recovery, handler = self.register()
        wire = document_wire()
        opened = recovery.projects.open_document(wire)
        recovery.settings(True, False, expected_revision=0)
        status = recovery.watch(opened["owner"], wire["planning"], wire["title"])
        project = status["current"]["project_id"]
        recovery.flush(project, deadline=time.monotonic() + 5)
        before = set(recovery.projects.authoring._entries)
        with patch.object(
            module, "project_bytes", side_effect=ProjectDocumentError("document_too_large")
        ):
            response = await handler(self.request({"intent": "restore", "project_id": project}))
        self.assertEqual(response.status, 400)
        self.assertEqual(set(recovery.projects.authoring._entries), before)

    async def test_requalified_owner_drift_cannot_admit_a_watch(self) -> None:
        _module, recovery, handler = self.register()
        admitted = recovery.qualify()
        changed = RecoveryOwner("owner_" + "b" * 32, admitted.private_root)
        recovery.qualify = Mock(side_effect=(admitted, changed))
        response = await handler(self.request({"intent": "status", "project_id": None}))
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["code"], "owner_changed")
        self.assertIsNone(recovery._store)
