"""Opt-in metadata sampling and explicit non-executable recovery commands."""

from __future__ import annotations

import atexit
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..core.durable_workspace_state import (
    MAX_OWNER_RECORDS,
    MAX_OWNER_REVISION,
    DurableStateError,
    StateRecord,
    StateSnapshot,
    decode_record,
    json_bytes,
    recover_record,
    require_record_id,
    strict_json,
)
from .durable_state_store import CLOSED_RETENTION_MS, DurableStateStore, StateManifest
from .recovery_owner import RecoveryOwner, RecoveryOwnerPort

RESPONSE_SCHEMA = "h3.context.workspace_state_response.v1"
STATUS_SCHEMA = "h3.context.workspace_state_status.v1"
MAX_ACTION_BYTES = 4096
_INTENT_KEYS = {
    "status": {"intent"},
    "list": {"intent"},
    "set_enabled": {"intent", "enabled", "expected_revision"},
    "save": {"intent", "expected_revision"},
    "restore": {"intent", "record_id", "expected_revision"},
    "reset": {"intent", "expected_revision"},
}


def decode_state_action(payload: bytes) -> dict[str, object]:
    action = strict_json(payload, maximum_bytes=MAX_ACTION_BYTES, maximum_depth=4)
    if type(action) is not dict or type(action.get("intent")) is not str:
        raise DurableStateError("intent_invalid")
    intent = action["intent"]
    if intent not in _INTENT_KEYS or set(action) != _INTENT_KEYS[intent]:
        raise DurableStateError("intent_invalid")
    if "expected_revision" in action:
        revision = action["expected_revision"]
        if type(revision) is not int or not 0 <= revision <= MAX_OWNER_REVISION:
            raise DurableStateError("revision_invalid")
    if intent == "set_enabled" and type(action["enabled"]) is not bool:
        raise DurableStateError("intent_invalid")
    if intent == "restore":
        require_record_id(action["record_id"])
    return action


def unavailable_projection() -> dict[str, object]:
    return {
        "schema": STATUS_SCHEMA,
        "supported": False,
        "enabled": False,
        "revision": 0,
        "count": 0,
        "saved_at_ms": None,
        "current": False,
        "save_state": "blocked",
        "sampler_active": False,
        "records": [],
    }


def _merge(
    current: tuple[StateRecord, ...], saved: StateSnapshot | None, now: int
) -> tuple[StateRecord, ...]:
    rows = {row.record_id: row for row in current}
    if saved is not None:
        for record in saved.records:
            if record.record_id in rows:
                continue
            closed = (
                record.closed_at_ms
                if record.closed_at_ms is not None
                else max(now, record.created_at_ms)
            )
            if now - closed >= CLOSED_RETENTION_MS:
                continue
            rows[record.record_id] = replace(
                record,
                closed_at_ms=closed,
                state=record.state if record.kind == "managed_run" else "workspace_closed",
            )
    if len(rows) > MAX_OWNER_RECORDS:
        raise DurableStateError("record_bound")
    return tuple(sorted(rows.values(), key=lambda row: row.record_id))


class WorkspaceStateService:
    def __init__(
        self,
        *,
        owner_port: RecoveryOwnerPort,
        collector: Callable[[], tuple[StateRecord, ...]],
        poll_seconds: float = 2.0,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        if type(poll_seconds) not in (int, float) or not 0.01 <= poll_seconds <= 2:
            raise DurableStateError("limits_invalid")
        self.owner_port, self._collector = owner_port, collector
        self._poll_seconds, self._clock_ms = poll_seconds, clock_ms
        self._lock = threading.RLock()
        self._owner: RecoveryOwner | None = None
        self._store: DurableStateStore | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._closed = False
        self._last_error: str | None = None

    @property
    def sampler_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.5)
            if thread.is_alive():
                # Retain the lease while an OS call is still in flight; process exit releases it.
                return
        with self._lock:
            self._closed = True
            if self._store is not None:
                self._store.close()

    def qualify(self) -> RecoveryOwner:
        return self.owner_port.resolve()

    def _qualified_store(self, admitted_owner: RecoveryOwner | None = None) -> DurableStateStore:
        if self._closed:
            raise DurableStateError("service_closed")
        owner = self.qualify()
        if admitted_owner is not None and (
            type(admitted_owner) is not RecoveryOwner or owner != admitted_owner
        ):
            raise DurableStateError("owner_changed")
        if self._owner is not None and owner != self._owner:
            raise DurableStateError("owner_changed")
        if self._store is None:
            self._owner = owner
            self._store = DurableStateStore(
                owner.private_root, owner.owner_id, clock_ms=self._clock_ms
            )
        return self._store

    def _collect(self) -> tuple[StateRecord, ...]:
        records = self._collector()
        if type(records) is not tuple or len(records) > MAX_OWNER_RECORDS:
            raise DurableStateError("record_bound")
        rows = tuple(
            sorted(
                (decode_record(row.to_wire()) for row in records if type(row) is StateRecord),
                key=lambda row: row.record_id,
            )
        )
        if len(rows) != len(records) or len({row.record_id for row in rows}) != len(rows):
            raise DurableStateError("record_duplicate")
        return rows

    def _ensure_sampler(self, manifest: StateManifest) -> None:
        if manifest.enabled and not self.sampler_alive and not self._stop.is_set():
            self._thread = threading.Thread(
                target=self._sample_loop, name="h3-workspace-state", daemon=True
            )
            self._thread.start()

    def _sample_loop(self) -> None:
        try:
            while not self._stop.wait(self._poll_seconds):
                with self._lock:
                    try:
                        store = self._qualified_store()
                        manifest, saved = store.read()
                        if not manifest.enabled:
                            return
                        self._save(store, manifest, saved)
                    except DurableStateError as error:
                        self._last_error = str(error)
                    except Exception:
                        self._last_error = "storage_unavailable"
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    @staticmethod
    def _expect(manifest: StateManifest, revision: int) -> None:
        if revision != manifest.revision:
            raise DurableStateError("revision_conflict")

    def _save(
        self, store: DurableStateStore, manifest: StateManifest, saved: StateSnapshot | None
    ) -> StateSnapshot:
        if not manifest.enabled:
            raise DurableStateError("recovery_disabled")
        current, now = self._collect(), self._clock_ms()
        # Retire expired records without new IDs first: physical previous/staged IDs remain
        # charged until their owned retirement commits, even when this sample contains new work.
        if saved is not None and any(
            row.closed_at_ms is not None
            and now - row.closed_at_ms >= CLOSED_RETENTION_MS
            and row.record_id not in {item.record_id for item in current}
            for row in saved.records
        ):
            retained = tuple(
                row
                for row in saved.records
                if row.closed_at_ms is None
                or now - row.closed_at_ms < CLOSED_RETENTION_MS
                or row.record_id in {item.record_id for item in current}
            )
            saved = store.save(retained, expected_revision=manifest.revision)
            manifest, saved = store.read()
        target = _merge(current, saved, now)
        if saved is None or target != saved.records:
            # CRITICAL: collector/business locks are gone before IO, and a commit acknowledges
            # only captured facts. A later status compares again; it never clears newer dirty work.
            saved = store.save(target, expected_revision=manifest.revision)
        self._last_error = None
        return saved

    def _projection(self, manifest: StateManifest, saved: StateSnapshot | None) -> dict[str, Any]:
        now = self._clock_ms()
        visible = (
            ()
            if saved is None
            else tuple(
                row
                for row in saved.records
                if row.closed_at_ms is None or now - row.closed_at_ms < CLOSED_RETENTION_MS
            )
        )
        current = False
        if manifest.enabled:
            try:
                current = saved is not None and _merge(self._collect(), saved, now) == saved.records
            except DurableStateError as error:
                # CRITICAL: refusing a live sample must not hide an already verified catalog.
                # Keep read-only restore/reset available; never drop stored rows to fit new work.
                self._last_error = str(error)
        state = (
            "disabled"
            if not manifest.enabled
            else "blocked"
            if self._last_error
            else "saved"
            if current
            else "dirty"
        )
        return {
            "schema": STATUS_SCHEMA,
            "supported": True,
            "enabled": manifest.enabled,
            "revision": manifest.revision,
            "count": len(visible),
            "saved_at_ms": None if saved is None else saved.saved_at_ms,
            "current": current,
            "save_state": state,
            "sampler_active": manifest.enabled and self.sampler_alive,
            "records": [row.to_wire() for row in visible],
        }

    def dispatch(
        self, action: dict[str, object], *, admitted_owner: RecoveryOwner | None = None
    ) -> dict[str, Any]:
        command = decode_state_action(json_bytes(action))
        with self._lock:
            store = self._qualified_store(admitted_owner)
            manifest, saved = store.read()
            intent, recovered = command["intent"], None
            if "expected_revision" in command:
                expected = command["expected_revision"]
                if type(expected) is not int:
                    raise DurableStateError("revision_invalid")
                self._expect(manifest, expected)
            try:
                if intent == "set_enabled":
                    enabled = command["enabled"]
                    if type(enabled) is not bool:
                        raise DurableStateError("intent_invalid")
                    manifest = store.set_enabled(enabled, expected_revision=manifest.revision)
                    self._last_error = None
                elif intent == "save":
                    self._save(store, manifest, saved)
                    manifest, saved = store.read()
                elif intent == "reset":
                    manifest = store.reset(expected_revision=manifest.revision)
                    saved, self._last_error = None, None
                elif intent == "restore":
                    if not manifest.enabled:
                        raise DurableStateError("recovery_disabled")
                    visible = self._projection(manifest, saved)["records"]
                    record = next(
                        (row for row in visible if row["record_id"] == command["record_id"]), None
                    )
                    if record is None:
                        raise DurableStateError("record_unavailable")
                    recovered = recover_record(decode_record(record))
            except DurableStateError as error:
                self._last_error = str(error)
                raise
            self._ensure_sampler(manifest)
            return {
                "schema": RESPONSE_SCHEMA,
                "projection": self._projection(manifest, saved),
                "error": self._last_error,
                "recovered": recovered,
            }


def _collect_live_metadata() -> tuple[StateRecord, ...]:
    from .composition_root import (
        AUTHORING_WORKSPACE,
        PRODUCTION_WORKSPACE,
        SEQUENCE_COORDINATOR,
        installed,
    )

    records = []
    for name in (PRODUCTION_WORKSPACE, AUTHORING_WORKSPACE, SEQUENCE_COORDINATOR):
        owner = installed(name)
        if owner is not None:
            reader = getattr(owner, "recovery_metadata", None)
            if not callable(reader):
                raise DurableStateError("shape_invalid")
            records.extend(reader())
            if len(records) > MAX_OWNER_RECORDS:
                raise DurableStateError("record_bound")
    return tuple(records)


def build_workspace_state_service() -> WorkspaceStateService:
    service = WorkspaceStateService(
        owner_port=RecoveryOwnerPort(), collector=_collect_live_metadata
    )
    atexit.register(service.close)
    return service
