"""Lazy backend-owned recovery writer with revision-bound acknowledgements and dirty leases."""

from __future__ import annotations

import atexit
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.durable_workspace_state import DurableStateError
from ..core.editor_recovery import EditorRecoveryError, require_project
from ..core.project_document import ProjectDocumentError
from .editor_recovery_store import EditorRecoveryStore
from .project_document_service import ProjectDocumentService
from .project_source_binding import ProjectSourceBindingReceipt
from .recovery_owner import RecoveryOwner, RecoveryOwnerPort

STATUS_SCHEMA = "h3.context.project_recovery.status.v1"
RESPONSE_SCHEMA = "h3.context.project_recovery.response.v1"


@dataclass(slots=True)
class _Tracked:
    project_id: str
    owner: dict[str, Any] | None
    planning: object
    title: object
    generation: int
    saved_generation: int
    first_dirty: float
    due: float
    pending: bool = False
    reason: str | None = None
    saved_revision: int | None = None
    saved_at_ms: int | None = None
    saved_owner: dict[str, Any] | None = None
    closed: bool = False


class EditorRecoveryService:
    def __init__(
        self,
        projects: ProjectDocumentService,
        *,
        owner_port: RecoveryOwnerPort,
        clock: Callable[[], float] = time.monotonic,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.projects, self.owner_port, self._clock, self._clock_ms = (
            projects,
            owner_port,
            clock,
            clock_ms,
        )
        self._condition = threading.Condition(threading.RLock())
        self._io = threading.RLock()
        self._rows: dict[str, _Tracked] = {}
        self._handles: dict[tuple[str, str], str] = {}
        self._owner: RecoveryOwner | None = None
        self._store: EditorRecoveryStore | None = None
        self._inventory: dict[str, Any] = {
            "enabled": False,
            "include_video": False,
            "revision": 0,
            "charged_bytes": 0,
            "records": [],
        }
        self._thread: threading.Thread | None = None
        self._stop, self._closed = False, False
        self._settings_pending = False
        self._write_uses: tuple[Any, ...] = ()
        self._maintenance_due = self._clock() + 60
        projects.authoring.configure_editor_recovery(
            lambda handle, gone, production: self._changed("authoring", handle, gone, production),
            lambda handle: self._protected("authoring", handle),
        )
        projects.production.configure_editor_recovery(
            lambda handle, gone: self._changed("production", handle, gone),
            lambda handle: self._protected("production", handle),
        )

    @property
    def writer_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def qualify(self) -> RecoveryOwner:
        try:
            return self.owner_port.resolve()
        except DurableStateError as error:
            raise EditorRecoveryError(str(error)) from None

    def _qualified_store(self, admitted_owner: RecoveryOwner | None = None) -> EditorRecoveryStore:
        if self._closed:
            raise EditorRecoveryError("service_closed")
        owner = self.qualify()
        if (
            admitted_owner is not None
            and (type(admitted_owner) is not RecoveryOwner or owner != admitted_owner)
        ) or (self._owner is not None and owner != self._owner):
            raise EditorRecoveryError("owner_changed")
        if self._store is None:
            self._owner = owner
            self._store = EditorRecoveryStore(
                owner.private_root,
                owner.owner_id,
                clock_ms=self._clock_ms,
                reference_sync=self._sync_references,
            )
        return self._store

    def _sync_references(self, project: str, ids: tuple[str, ...]) -> None:
        if self.projects.retained is None:
            if ids:
                raise EditorRecoveryError("media_unavailable")
            return
        self.projects.retained.sync_recovery_references(project, ids, uses=self._write_uses)

    def _refresh(self) -> None:
        inventory = self._qualified_store().inventory()
        with self._condition:
            self._inventory = inventory

    def _start(self) -> None:
        with self._condition:
            if not self._stop and not self.writer_alive:
                self._thread = threading.Thread(
                    target=self._loop, name="h3-editor-recovery", daemon=True
                )
                self._thread.start()
            self._condition.notify_all()

    def _dirty(self, row: _Tracked) -> None:
        now = self._clock()
        if row.generation == row.saved_generation:
            row.first_dirty = now
        row.generation += 1
        row.due = min(now + 0.5, row.first_dirty + 2.0)
        row.reason = None
        self._condition.notify_all()

    def _changed(
        self, kind: str, handle: str, gone: bool = False, production_handle: str | None = None
    ) -> None:
        # IMPORTANT: registry callbacks acquire only this short condition, never IO/business locks.
        with self._condition:
            project = self._handles.get((kind, handle))
            if project is None and production_handle is not None:
                project = self._handles.get(("production", production_handle))
            row = self._rows.get(project) if project is not None else None
            if row is not None:
                if gone:
                    if row.pending or row.generation != row.saved_generation:
                        row.reason = "project_unavailable"
                    else:
                        row.closed, row.due = True, self._clock()
                        self._condition.notify_all()
                elif not row.closed:
                    self._dirty(row)

    def _protected(self, kind: str, handle: str) -> bool:
        with self._condition:
            project = self._handles.get((kind, handle))
            row = self._rows.get(project) if project is not None else None
            if row is not None and row.closed:
                return False
            if row is not None and (row.pending or row.generation != row.saved_generation):
                if not row.pending:
                    row.due = min(row.due, self._clock())
                self._condition.notify_all()
                return True
            return False

    def _bind(self, row: _Tracked) -> None:
        for key, project in tuple(self._handles.items()):
            if project == row.project_id:
                self._handles.pop(key)
        if row.owner is not None:
            for kind in ("authoring", "production"):
                handle = row.owner[kind + "_handle"]
                if handle is not None:
                    self._handles[kind, handle] = row.project_id

    def status(self, project_id: str | None = None) -> dict[str, Any]:
        self.qualify()
        if self._io.acquire(blocking=False):
            try:
                self._refresh()
            finally:
                self._io.release()
        with self._condition:
            row = self._rows.get(require_project(project_id)) if project_id is not None else None
            current = (
                None
                if row is None
                else {
                    "project_id": row.project_id,
                    "generation": row.generation,
                    "saved_generation": row.saved_generation,
                    "saved_revision": row.saved_revision,
                    "saved_at_ms": row.saved_at_ms,
                    "saved_owner": row.saved_owner,
                    "state": "saving"
                    if row.pending
                    else "error"
                    if row.reason
                    else "saved"
                    if row.generation == row.saved_generation
                    else "dirty",
                    "reason": row.reason,
                }
            )
            records = [
                dict(item, active=item["project_id"] in self._rows)
                for item in self._inventory["records"]
            ]
            return {
                "schema": STATUS_SCHEMA,
                "supported": True,
                **self._inventory,
                "records": records,
                "current": current,
                "writer_active": self.writer_alive,
            }

    def settings(
        self, enabled: bool, include_video: bool, *, expected_revision: int
    ) -> dict[str, Any]:
        with self._io:
            store = self._qualified_store()
            with self._condition:
                if any(
                    row.pending or row.generation != row.saved_generation
                    for row in self._rows.values()
                ):
                    raise EditorRecoveryError("dirty_protected")
                self._settings_pending = True
                previous = self._inventory
            try:
                inventory = store.settings(
                    enabled, include_video, expected_revision=expected_revision
                )
                with self._condition:
                    raced = not enabled and any(
                        row.generation != row.saved_generation for row in self._rows.values()
                    )
                    if not raced:
                        changed_video = (
                            inventory["include_video"] != self._inventory["include_video"]
                        )
                        self._inventory = inventory
                        if not enabled:
                            for row in self._rows.values():
                                row.closed, row.due = True, self._clock()
                        elif changed_video:
                            for row in self._rows.values():
                                self._dirty(row)
                if raced:
                    # CRITICAL: edits during settings IO must roll back disable, never hide Dirty.
                    inventory = store.settings(
                        previous["enabled"],
                        previous["include_video"],
                        expected_revision=inventory["revision"],
                    )
                    with self._condition:
                        self._inventory = inventory
                if raced:
                    raise EditorRecoveryError("dirty_protected")
            finally:
                with self._condition:
                    self._settings_pending = False
                    self._condition.notify_all()
            if enabled:
                self._start()
        return self.status()

    def watch(
        self, owner: object, planning: object, title: object, *, project_id: str | None = None
    ) -> dict[str, Any]:
        # Validate full draft data outside business locks before reserving any tracked owner.
        snapshot = self.projects.capture_recovery(owner, planning, title)
        planning, title = snapshot["document"]["planning"], snapshot["document"]["title"]
        with self._io:
            self._refresh()
        with self.projects.authoring._lock:
            with self.projects.production._lock:
                current = self.projects.current_recovery_owner(owner)
                if current != owner:
                    raise EditorRecoveryError("revision_conflict")
                with self._condition:
                    if not self._inventory["enabled"]:
                        raise EditorRecoveryError("recovery_disabled")
                    row = (
                        self._rows.get(require_project(project_id))
                        if project_id is not None
                        else None
                    )
                    if project_id is not None and row is None:
                        raise EditorRecoveryError("project_unavailable")
                    if row is None:
                        keys = (
                            []
                            if current is None
                            else [
                                (kind, current[kind + "_handle"])
                                for kind in ("authoring", "production")
                                if current[kind + "_handle"] is not None
                            ]
                        )
                        existing = {self._handles[key] for key in keys if key in self._handles}
                        if existing:
                            row = self._rows[next(iter(existing))]
                        else:
                            admitted = set(self._rows) | {
                                item["project_id"] for item in self._inventory["records"]
                            }
                            if len(admitted) >= 16:
                                raise EditorRecoveryError("quota_records")
                            now = self._clock()
                            row = _Tracked(
                                "project_" + secrets.token_hex(16),
                                current,
                                planning,
                                title,
                                1,
                                0,
                                now,
                                now + 0.5,
                            )
                            self._rows[row.project_id] = row
                    if row.owner != current or row.planning != planning or row.title != title:
                        if (
                            row.owner is not None
                            and current is not None
                            and (
                                row.owner["production_handle"] != current["production_handle"]
                                or row.owner["authoring_handle"] is not None
                                and row.owner["authoring_handle"] != current["authoring_handle"]
                            )
                        ):
                            raise EditorRecoveryError("active_project")
                        row.owner, row.planning, row.title = current, planning, title
                        self._dirty(row)
                    self._bind(row)
                    self._condition.notify_all()
        self._start()
        return self.status(row.project_id)

    def request_flush(self, project_id: str) -> None:
        project = require_project(project_id)
        with self._condition:
            row = self._rows.get(project)
            if row is None:
                raise EditorRecoveryError("project_unavailable")
            row.reason, row.due = None, self._clock()
            self._condition.notify_all()
        self._start()

    def flush(
        self, project_id: str, *, deadline: float, cancelled: Callable[[], bool] = lambda: False
    ) -> dict[str, Any]:
        self.request_flush(project_id)
        with self._condition:
            while True:
                if cancelled():
                    raise EditorRecoveryError("cancelled")
                row = self._rows.get(project_id)
                if row is None:
                    raise EditorRecoveryError("project_unavailable")
                if not row.pending and row.generation == row.saved_generation:
                    break
                if not row.pending and row.reason:
                    raise EditorRecoveryError(row.reason)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise EditorRecoveryError("timed_out")
                self._condition.wait(min(left, 0.1))
        return self.status(project_id)

    def _write(
        self, row: _Tracked, generation: int, owner: object, planning: object, title: object
    ) -> None:
        borrowers: list[Any] = []
        snapshot, current, reason = None, None, None
        try:
            current, data, binding = self.projects.capture_latest_recovery(owner, planning, title)
            with self._io:
                store = self._qualified_store()
                include_video = store.inventory()["include_video"]
                uses = []
                for media in data["document"]["media"]:
                    use = (
                        binding.uses.get(media["asset_id"])
                        if include_video and type(binding) is ProjectSourceBindingReceipt
                        else None
                    )
                    if use is not None:
                        borrower = use.source.borrow_for_render()
                        borrowers.append(borrower)
                        facts = use.source.facts
                        if (
                            media["content_fingerprint"] != facts.content_fingerprint
                            or media["byte_length"] != facts.byte_length
                        ):
                            raise EditorRecoveryError("media_unavailable")
                        media["retained_id"] = use.asset_id
                        uses.append(use)
                    else:
                        media["retained_id"] = None
                self._write_uses = tuple(uses)
                try:
                    ids = tuple(
                        sorted(
                            {
                                media["retained_id"]
                                for media in data["document"]["media"]
                                if media["retained_id"] is not None
                            }
                        )
                    )
                    snapshot = store.save(row.project_id, data, retained_ids=ids)
                    self._refresh()
                finally:
                    self._write_uses = ()
        except EditorRecoveryError as error:
            reason = error.code
        except (ProjectDocumentError, OSError, ValueError):
            reason = "storage_unavailable"
        except Exception:
            reason = "internal_failure"
        finally:
            for borrower in borrowers:
                try:
                    borrower.release()
                except Exception:
                    reason = reason or "media_unavailable"
        # CRITICAL: business locks precede the condition; never reverse them in scheduler/status.
        with self.projects.authoring._lock:
            with self.projects.production._lock:
                try:
                    latest = self.projects.current_recovery_owner(owner)
                except ProjectDocumentError:
                    latest, reason = None, reason or "project_unavailable"
                with self._condition:
                    row.pending = False
                    if self._rows.get(row.project_id) is row:
                        if reason is not None:
                            row.reason, row.due = reason, self._clock() + 2.0
                        elif snapshot is not None:
                            (
                                row.saved_generation,
                                row.saved_revision,
                                row.saved_at_ms,
                                row.saved_owner,
                            ) = generation, snapshot["revision"], snapshot["saved_at_ms"], current
                            # CRITICAL: acknowledge only the captured generation/pair.
                            # A newer edit remains Dirty even if the older write succeeded.
                            if row.generation == generation and latest != current:
                                self._dirty(row)
                            row.owner = latest
                            self._bind(row)
                        self._condition.notify_all()

    def _loop(self) -> None:
        while True:
            with self._condition:
                if self._stop:
                    return
                if self._settings_pending:
                    self._condition.wait()
                    continue
                now = self._clock()
                candidates = [
                    row
                    for row in self._rows.values()
                    if not row.pending and (row.closed or row.generation != row.saved_generation)
                ]
                row = min(candidates, key=lambda item: item.due) if candidates else None
                maintenance = now >= self._maintenance_due and self._inventory["enabled"]
                if (row is None or row.due > now) and not maintenance:
                    wait = 60 if row is None else max(0.001, row.due - now)
                    if self._inventory["enabled"]:
                        wait = min(wait, max(0.001, self._maintenance_due - now))
                    self._condition.wait(wait)
                    continue
                if row is None or row.due > now:
                    self._maintenance_due = now + 60
                    row = None
                else:
                    row.pending = True
                    captured = row.generation, row.owner, row.planning, row.title
            if row is None:
                try:
                    with self._io:
                        store = self._qualified_store()
                        with self._condition:
                            active_ids = set(self._rows)
                        # IMPORTANT: only the writer expires closed records; polling is read-only.
                        store.prune_closed(active_ids=active_ids)
                        self._refresh()
                except Exception:
                    logging.getLogger(__name__).warning("Closed recovery retention deferred")
                continue
            if row.closed:
                self._close_saved(row)
            else:
                self._write(row, *captured)

    def _close_saved(self, row: _Tracked) -> None:
        try:
            with self._io:
                store = self._qualified_store()
                store.close_record(row.project_id)
                self._refresh()
            with self._condition:
                self._rows.pop(row.project_id, None)
                for key, project in tuple(self._handles.items()):
                    if project == row.project_id:
                        self._handles.pop(key)
                self._condition.notify_all()
        except Exception:
            with self._condition:
                row.pending, row.reason, row.due = False, "storage_unavailable", self._clock() + 2
                self._condition.notify_all()

    def detach(self, project_id: str, *, discard: bool) -> dict[str, Any]:
        project = require_project(project_id)
        if type(discard) is not bool:
            raise EditorRecoveryError("command_invalid")
        with self._io:
            store = self._qualified_store()
            with self._condition:
                row = self._rows.get(project)
                if row is None:
                    raise EditorRecoveryError("project_unavailable")
                if row.pending or (not discard and row.generation != row.saved_generation):
                    raise EditorRecoveryError("dirty_protected")
                self._rows.pop(project)
                for key, bound in tuple(self._handles.items()):
                    if bound == project:
                        self._handles.pop(key)
            if any(item["project_id"] == project for item in store.inventory()["records"]):
                store.close_record(project)
            self._refresh()
        return self.status()

    def restore(
        self, project_id: str, *, deadline: float, cancelled: Callable[[], bool] = lambda: False
    ) -> dict[str, Any]:
        with self._io:
            snapshot = self._qualified_store().read(require_project(project_id))
        if cancelled() or time.monotonic() >= deadline:
            raise EditorRecoveryError("cancelled" if cancelled() else "timed_out")
        response = self.projects.restore_recovery(
            {"document": snapshot["document"], "history": snapshot["history"]}, cancelled=cancelled
        )
        try:
            for media in snapshot["document"]["media"]:
                if cancelled() or time.monotonic() >= deadline:
                    raise EditorRecoveryError("cancelled" if cancelled() else "timed_out")
                if media["retained_id"] is None:
                    continue
                try:
                    linked = self.projects.relink_document(
                        response["owner"],
                        media["asset_id"],
                        media["retained_id"],
                        deadline=deadline,
                        cancelled=cancelled,
                    )
                    response.update(
                        linked,
                        schema="h3.context.project_document.response.v1",
                        title=snapshot["document"]["title"],
                        planning=snapshot["document"]["planning"],
                    )
                except (ProjectDocumentError, ValueError):
                    pass
            if cancelled() or time.monotonic() >= deadline:
                raise EditorRecoveryError("cancelled" if cancelled() else "timed_out")
        except BaseException:
            self.projects.discard_open_response(response)
            raise
        return response

    def clear(self, project_id: str) -> dict[str, Any]:
        project = require_project(project_id)
        with self._io:
            with self._condition:
                if project in self._rows:
                    raise EditorRecoveryError("active_project")
            self._qualified_store().clear(project)
            self._refresh()
        return self.status()

    def close(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                return
        with self._io:
            self._closed = True
            if self._store is not None:
                self._store.close()


def build_editor_recovery_service(projects: ProjectDocumentService) -> EditorRecoveryService:
    service = EditorRecoveryService(projects, owner_port=RecoveryOwnerPort())
    atexit.register(service.close)
    return service
