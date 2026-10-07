"""Backend-owned save generation, actual aggregate signals and dirty-before-prune protection."""

from __future__ import annotations

import importlib
import importlib.util
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from test_m25_timeline_history_v2 import transaction
from test_project_document import document_wire

from comfyui_h3_context.adapters.comfyui_authoring_workspace import (
    AUTHORING_ACTION_SCHEMA,
    AuthoringWorkbenchError,
    AuthoringWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.project_document_service import ProjectDocumentService
from comfyui_h3_context.adapters.recovery_owner import RecoveryOwner
from comfyui_h3_context.adapters.segment_artifact_store import _windows_native_path
from comfyui_h3_context.core.editor_recovery import EditorRecoveryError
from comfyui_h3_context.core.timeline_authoring import NleCommand, NleCommandKind

MODULE = "comfyui_h3_context.adapters.editor_recovery_service"


class EditorRecoveryServiceTests(unittest.TestCase):
    def service(
        self,
        *,
        capacity: int = 2,
        scheduling: list[float] | None = None,
        epoch: list[int] | None = None,
    ) -> tuple[Any, ProjectDocumentService, list[float]]:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE), "missing backend dirty/save lifecycle"
        )
        temporary = tempfile.TemporaryDirectory(
            dir=(
                _windows_native_path(Path(tempfile.gettempdir()), force=True)
                if os.name == "nt"
                else None
            )
        )
        self.addCleanup(temporary.cleanup)
        clock = [0.0]
        projects = ProjectDocumentService(
            ProductionWorkspaceRegistry(
                clock=lambda: clock[0],
                max_entries=capacity,
                ttl_seconds=1,
                seed_claim=Mock(side_effect=AssertionError("no Context claim")),
            ),
            AuthoringWorkspaceRegistry(clock=lambda: clock[0], max_entries=capacity, ttl_seconds=1),
        )
        owner = RecoveryOwner("owner_" + "a" * 32, Path(temporary.name))
        options: dict[str, Any] = {}
        if scheduling is not None:
            options["clock"] = lambda: scheduling[0]
        if epoch is not None:
            options["clock_ms"] = lambda: epoch[0]
        service = importlib.import_module(MODULE).EditorRecoveryService(
            projects, owner_port=Mock(resolve=Mock(return_value=owner)), **options
        )
        self.addCleanup(service.close)
        return service, projects, clock

    def watch(self, service: Any, projects: ProjectDocumentService) -> tuple[str, dict[str, Any]]:
        wire = document_wire()
        opened = projects.open_document(wire)
        service.settings(True, False, expected_revision=0)
        status = service.watch(opened["owner"], wire["planning"], wire["title"])
        return status["current"]["project_id"], opened

    def edit(self, projects: ProjectDocumentService, opened: dict[str, Any]) -> None:
        handle = opened["owner"]["authoring_handle"]
        state = projects.authoring._entries[handle].timeline_history_v2
        assert state is not None
        request = transaction(
            state,
            "recovery-edit",
            NleCommand(
                NleCommandKind.SET_CLIP_ENABLED, {"clip_id": "clip.saved", "enabled": False}
            ),
        )
        result = projects.authoring.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "recovery-edit",
                "action": "apply_timeline_transaction",
                "payload": request,
            }
        )
        self.assertEqual(result.status, 200)

    def test_defaults_off_no_thread_no_content_writes(self) -> None:
        service, _projects, _clock = self.service()
        status = service.status()
        self.assertFalse(status["enabled"])
        self.assertFalse(service.writer_alive)
        self.assertFalse(service._store.root.exists())

    def test_backend_signal_saves_real_edit_without_browser_update_and_polling_is_read_only(
        self,
    ) -> None:
        service, projects, _clock = self.service()
        project, opened = self.watch(service, projects)
        service.flush(project, deadline=time.monotonic() + 5)
        self.edit(projects, opened)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = service.status(project)
            if status["current"]["state"] == "saved" and status["current"]["generation"] > 1:
                break
            time.sleep(0.01)
        self.assertEqual(status["current"]["state"], "saved")
        snapshot = service._store.read(project)
        self.assertFalse(snapshot["document"]["editor"]["clips"][0]["enabled"])
        self.assertEqual(len(snapshot["history"]["undo"]), 1)
        revision = status["revision"]
        for _ in range(3):
            self.assertEqual(service.status(project)["revision"], revision)
        self.assertNotIn("Synthetic", str(status))

    def test_late_ack_never_clears_newer_edit_and_io_is_outside_business_locks(self) -> None:
        service, projects, _clock = self.service()
        project, opened = self.watch(service, projects)
        entered, release = threading.Event(), threading.Event()
        original = service._store.save

        def slow(*args: Any, **kwargs: Any) -> Any:
            self.assertFalse(cast(Any, projects.authoring._lock)._is_owned())
            self.assertFalse(cast(Any, projects.production._lock)._is_owned())
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)

        with patch.object(service._store, "save", side_effect=slow):
            service.request_flush(project)
            self.assertTrue(entered.wait(3))
            self.assertEqual(service.status(project)["current"]["state"], "saving")
            self.edit(projects, opened)
            release.set()
            status = service.flush(project, deadline=time.monotonic() + 5)
        self.assertEqual(status["current"]["state"], "saved")
        self.assertGreaterEqual(status["current"]["generation"], 2)
        self.assertFalse(service._store.read(project)["document"]["editor"]["clips"][0]["enabled"])

    def test_failed_dirty_save_protects_both_ttl_and_release_and_refuses_capacity(self) -> None:
        service, projects, clock = self.service(capacity=1)
        project, opened = self.watch(service, projects)
        with patch.object(service._store, "save", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(EditorRecoveryError):
                service.flush(project, deadline=time.monotonic() + 5)
            clock[0] = 2.0
            with projects.authoring._lock:
                projects.authoring._prune(clock[0])
            with projects.production._lock:
                projects.production._prune(clock[0])
            self.assertIn(opened["owner"]["authoring_handle"], projects.authoring._entries)
            self.assertIn(
                opened["owner"]["production_handle"], projects.production._editable_projects
            )
            with self.assertRaises(ValueError):
                projects.open_document(document_wire())
            with self.assertRaises(AuthoringWorkbenchError) as refusal:
                projects.authoring.dispatch(
                    {
                        "schema": AUTHORING_ACTION_SCHEMA,
                        "request_id": "dirty-release",
                        "action": "release_workspace",
                        "payload": {"workspace_handle": opened["owner"]["authoring_handle"]},
                    }
                )
            self.assertEqual(refusal.exception.code, "dirty_recovery_protected")
            with self.assertRaises(EditorRecoveryError):
                service.settings(False, False, expected_revision=service.status()["revision"])
        service.flush(project, deadline=time.monotonic() + 5)
        with projects.authoring._lock:
            projects.authoring._prune(clock[0])
        with projects.production._lock:
            projects.production._prune(clock[0])
        self.assertNotIn(opened["owner"]["authoring_handle"], projects.authoring._entries)
        restored = service.restore(project, deadline=time.monotonic() + 5)
        self.assertNotEqual(restored["owner"], opened["owner"])
        self.assertEqual(restored["missing_media"], ["video.saved"])

    def test_explicit_detach_required_for_clear_and_dirty_discard_preserves_last_good(self) -> None:
        service, projects, _clock = self.service()
        project, opened = self.watch(service, projects)
        service.flush(project, deadline=time.monotonic() + 5)
        with self.assertRaises(EditorRecoveryError):
            service.clear(project)
        self.edit(projects, opened)
        with self.assertRaises(EditorRecoveryError):
            service.detach(project, discard=False)
        service.detach(project, discard=True)
        self.assertTrue(service._store.read(project)["document"]["editor"]["clips"][0]["enabled"])
        service.clear(project)
        self.assertEqual(service.status()["records"], [])

    def test_disable_racing_real_edit_rolls_back_enabled_and_keeps_dirty_owner(self) -> None:
        service, projects, _clock = self.service()
        project, opened = self.watch(service, projects)
        service.flush(project, deadline=time.monotonic() + 5)
        entered, release = threading.Event(), threading.Event()
        errors: list[str] = []
        original = service._store.settings

        def delayed(*args: Any, **kwargs: Any) -> Any:
            if args[0] is False:
                entered.set()
                self.assertTrue(release.wait(3))
            return original(*args, **kwargs)

        revision = service.status()["revision"]

        def disable() -> None:
            try:
                service.settings(False, False, expected_revision=revision)
            except EditorRecoveryError as error:
                errors.append(error.code)

        with patch.object(service._store, "settings", side_effect=delayed):
            thread = threading.Thread(target=disable)
            thread.start()
            self.assertTrue(entered.wait(3))
            self.edit(projects, opened)
            release.set()
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["dirty_protected"])
        self.assertTrue(service.status(project)["enabled"])
        service.flush(project, deadline=time.monotonic() + 5)
        self.assertFalse(service._store.read(project)["document"]["editor"]["clips"][0]["enabled"])

    def test_clean_release_closes_snapshot_without_trying_to_save_a_gone_owner(self) -> None:
        service, projects, _clock = self.service()
        project, opened = self.watch(service, projects)
        service.flush(project, deadline=time.monotonic() + 5)
        projects.authoring.dispatch(
            {
                "schema": AUTHORING_ACTION_SCHEMA,
                "request_id": "clean-release",
                "action": "release_workspace",
                "payload": {"workspace_handle": opened["owner"]["authoring_handle"]},
            }
        )
        deadline = time.monotonic() + 3
        status = service.status(project)
        while time.monotonic() < deadline and status["current"] is not None:
            time.sleep(0.01)
            status = service.status(project)
        self.assertIsNone(status["current"])
        self.assertIsNotNone(status["records"][0]["closed_at_ms"])
        self.assertFalse(status["records"][0]["active"])

    def test_coalescing_is_bounded_by_first_dirty_not_latest_edit(self) -> None:
        service, projects, _clock = self.service()
        scheduling = [0.0]
        service._clock = lambda: scheduling[0]
        wire = document_wire()
        opened = projects.open_document(wire)
        service.settings(True, False, expected_revision=0)
        status = service.watch(opened["owner"], wire["planning"], wire["title"])
        project = status["current"]["project_id"]
        for number, moment in enumerate((0.1, 0.4, 0.9, 1.7, 1.99)):
            scheduling[0] = moment
            service.watch(
                opened["owner"],
                wire["planning"],
                "Synthetic title " + str(number),
                project_id=project,
            )
            self.assertEqual(service._rows[project].due, min(moment + 0.5, 2.0))
            self.assertEqual(service.status(project)["current"]["saved_generation"], 0)
        saved = service.flush(project, deadline=time.monotonic() + 5)
        self.assertEqual(
            saved["current"]["saved_revision"], 2
        )  # Settings consumed manifest revision 1.
        self.assertEqual(service._store.read(project)["document"]["title"], "Synthetic title 4")

    def test_cancelled_restored_history_discards_only_the_fresh_undelivered_pair(self) -> None:
        service, projects, _clock = self.service(capacity=3)
        project, opened = self.watch(service, projects)
        self.edit(projects, opened)
        service.flush(project, deadline=time.monotonic() + 5)
        before = set(projects.authoring._entries), set(projects.production._editable_projects)
        interrupted = [False]
        original = projects.restore_recovery

        def published(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = original(*args, **kwargs)
            interrupted[0] = True
            return response

        with patch.object(projects, "restore_recovery", side_effect=published):
            with self.assertRaises(EditorRecoveryError) as refusal:
                service.restore(
                    project, deadline=time.monotonic() + 5, cancelled=lambda: interrupted[0]
                )
        self.assertEqual(refusal.exception.code, "cancelled")
        self.assertEqual(
            (set(projects.authoring._entries), set(projects.production._editable_projects)), before
        )
        self.assertEqual(len(service._store.read(project)["history"]["undo"]), 1)

    def test_writer_expires_only_closed_inactive_records_without_poll_mutation(self) -> None:
        scheduling, epoch = [0.0], [1000]
        service, projects, _clock = self.service(capacity=3, scheduling=scheduling, epoch=epoch)
        project, _opened = self.watch(service, projects)
        service.flush(project, deadline=time.monotonic() + 5)
        service.detach(project, discard=False)
        wire = document_wire()
        opened = projects.open_document(wire)
        active = service.watch(opened["owner"], wire["planning"], wire["title"])["current"][
            "project_id"
        ]
        service.flush(active, deadline=time.monotonic() + 5)
        epoch[0] += 8 * 86400 * 1000
        scheduling[0] = 61.0
        with service._condition:
            service._condition.notify_all()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            records = service.status(active)["records"]
            if not any(row["project_id"] == project for row in records):
                break
            time.sleep(0.01)
        self.assertEqual([row["project_id"] for row in records], [active])
        self.assertTrue(records[0]["active"])
        self.assertEqual(service.status(active)["current"]["state"], "saved")
