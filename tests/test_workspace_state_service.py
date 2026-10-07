"""Real store sampling, currentness and explicit read-only recovery intents."""

from __future__ import annotations

import importlib
import importlib.util
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from test_durable_state_store import metadata

from comfyui_h3_context.adapters.durable_state_store import DurableStateStore
from comfyui_h3_context.adapters.recovery_owner import RecoveryOwner
from comfyui_h3_context.adapters.workspace_state_service import WorkspaceStateService
from comfyui_h3_context.core.durable_workspace_state import (
    DurableStateError,
    StateRecord,
    StateSnapshot,
)

MODULE = "comfyui_h3_context.adapters.workspace_state_service"
OWNER = "owner_" + "a" * 32


class WorkspaceStateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).absolute() / "private"
        self.current: tuple[StateRecord, ...] = (metadata(),)

    def module(self) -> ModuleType:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE),
            "missing opt-in metadata sampler and explicit recovery service",
        )
        return importlib.import_module(MODULE)

    def service(
        self,
        *,
        collector: Callable[[], tuple[StateRecord, ...]] | None = None,
        poll_seconds: float = 2.0,
    ) -> WorkspaceStateService:
        module = self.module()
        owner = RecoveryOwner(OWNER, self.root)
        result = cast(
            WorkspaceStateService,
            module.WorkspaceStateService(
                owner_port=SimpleNamespace(resolve=lambda: owner),
                collector=collector or (lambda: self.current),
                poll_seconds=poll_seconds,
            ),
        )
        self.addCleanup(result.close)
        return result

    def test_closed_retention_boundary_releases_physical_ids_before_admitting_new_work(
        self,
    ) -> None:
        module = self.module()
        now = [2000]
        self.current = tuple(metadata(number) for number in range(1, 65))
        owner = RecoveryOwner(OWNER, self.root)
        service = module.WorkspaceStateService(
            owner_port=SimpleNamespace(resolve=lambda: owner),
            collector=lambda: self.current,
            clock_ms=lambda: now[0],
        )
        self.addCleanup(service.close)
        self.enable(service)
        self.save(service)
        self.current, now[0] = (), 3000
        closed = self.save(service)
        self.assertEqual(closed["projection"]["count"], 64)
        self.assertTrue(
            all(
                row["state"] == "workspace_closed" and row["closed_at_ms"] == 3000
                for row in closed["projection"]["records"]
            )
        )
        self.current = (metadata(65),)
        now[0] = 3000 + module.CLOSED_RETENTION_MS - 1
        with self.assertRaisesRegex(DurableStateError, "record_bound"):
            self.save(service)
        now[0] += 1
        saved = self.save(service)
        self.assertEqual(saved["projection"]["count"], 1)
        self.assertEqual(saved["projection"]["records"][0]["record_id"], metadata(65).record_id)
        manifest, snapshot = service._store.read()
        self.assertEqual(snapshot.records, self.current)
        self.assertEqual(
            manifest.previous.record_ids, (), "the retained retirement revision owns no expired IDs"
        )
        self.assertEqual(len(list(service._store.owner_directory.glob("revision-*.json"))), 2)

    def test_active_records_do_not_expire_or_get_evicted_to_admit_overflow(self) -> None:
        module = self.module()
        now = [2000]
        self.current = tuple(metadata(number) for number in range(1, 65))
        owner = RecoveryOwner(OWNER, self.root)
        service = module.WorkspaceStateService(
            owner_port=SimpleNamespace(resolve=lambda: owner),
            collector=lambda: self.current,
            clock_ms=lambda: now[0],
        )
        self.addCleanup(service.close)
        self.enable(service)
        saved = self.save(service)
        now[0] += module.CLOSED_RETENTION_MS + 1
        status = service.dispatch({"intent": "status"})
        self.assertEqual(status["projection"]["records"], saved["projection"]["records"])
        self.assertTrue(status["projection"]["current"])
        before = service._store.read()
        self.current += (metadata(65),)
        with self.assertRaisesRegex(DurableStateError, "record_bound"):
            self.save(service)
        self.assertEqual(service._store.read(), before)

    def test_overflow_blocks_sampling_not_existing_catalog_read_restore_or_reset(self) -> None:
        service = self.service()
        self.enable(service)
        saved = self.save(service)
        self.current = tuple(metadata(number) for number in range(1, 66))
        status = service.dispatch({"intent": "status"})
        self.assertTrue(status["projection"]["supported"])
        self.assertEqual(status["projection"]["save_state"], "blocked")
        self.assertEqual(status["projection"]["records"], saved["projection"]["records"])
        self.assertEqual(status["error"], "record_bound")
        restored = service.dispatch(
            {
                "intent": "restore",
                "record_id": metadata().record_id,
                "expected_revision": status["projection"]["revision"],
            }
        )
        self.assertFalse(restored["recovered"]["executable"])
        cleared = service.dispatch(
            {"intent": "reset", "expected_revision": status["projection"]["revision"]}
        )
        self.assertFalse(cleared["projection"]["enabled"])
        self.assertEqual(cleared["projection"]["records"], [])
        self.assertEqual(len(self.current), 65)

    def enable(self, service: WorkspaceStateService) -> dict[str, Any]:
        return service.dispatch({"intent": "set_enabled", "enabled": True, "expected_revision": 0})

    def save(self, service: WorkspaceStateService) -> dict[str, Any]:
        status = service.dispatch({"intent": "status"})
        return service.dispatch(
            {"intent": "save", "expected_revision": status["projection"]["revision"]}
        )

    def test_default_off_does_not_sample_construct_owners_or_create_files(self) -> None:
        service = self.service(
            collector=lambda: self.fail("disabled recovery must not sample live owners")
        )
        self.assertFalse(self.root.exists())
        result = service.dispatch({"intent": "status"})
        self.assertFalse(result["projection"]["enabled"])
        self.assertEqual(result["projection"]["save_state"], "disabled")
        self.assertFalse(self.root.exists())

    def test_explicit_sample_is_saved_but_live_change_reports_dirty(self) -> None:
        service = self.service()
        self.enable(service)
        saved = self.save(service)
        self.assertEqual(saved["projection"]["save_state"], "saved")
        self.current = (metadata(revision=2),)
        dirty = service.dispatch({"intent": "status"})
        self.assertFalse(dirty["projection"]["current"])
        self.assertEqual(dirty["projection"]["save_state"], "dirty")

    def test_write_ack_cannot_clear_a_change_that_happened_during_manifest_publication(
        self,
    ) -> None:
        store_module = importlib.import_module("comfyui_h3_context.adapters.durable_state_store")
        service = self.service()
        self.enable(service)
        original = store_module._replace_manifest

        def changed_during_write(source: Path, target: Path) -> None:
            original(source, target)
            self.current = (metadata(revision=2),)

        with patch.object(store_module, "_replace_manifest", side_effect=changed_during_write):
            response = self.save(service)
        self.assertEqual(response["projection"]["save_state"], "dirty")
        self.assertFalse(response["projection"]["current"])

    def test_restore_creates_only_non_executable_read_only_projection(self) -> None:
        service = self.service()
        self.enable(service)
        saved = self.save(service)
        self.current = ()
        row = saved["projection"]["records"][0]
        result = service.dispatch(
            {
                "intent": "restore",
                "record_id": row["record_id"],
                "expected_revision": saved["projection"]["revision"],
            }
        )
        restored = result["recovered"]
        self.assertFalse(restored["executable"])
        self.assertEqual(restored["source_status"], "source_reauthorization_required")
        self.assertTrue(restored["recovery_handle"].startswith("recovery_"))
        self.assertEqual(
            self.current, (), "recovery must never publish back into the live collector"
        )
        with self.assertRaisesRegex(DurableStateError, "record_unavailable"):
            service.dispatch(
                {
                    "intent": "restore",
                    "record_id": "record_" + "b" * 32,
                    "expected_revision": saved["projection"]["revision"],
                }
            )

    def test_stale_request_unknown_fields_and_path_or_owner_selection_refuse(self) -> None:
        service = self.service()
        self.enable(service)
        actions: tuple[dict[str, object], ...] = (
            {"intent": "save", "expected_revision": 0},
            {"intent": "reset", "expected_revision": True},
            {"intent": "restore", "record_id": "../file", "expected_revision": 1},
            {"intent": "status", "owner_id": OWNER},
            {"intent": "save", "expected_revision": 1, "data": {}},
            {"intent": "status", "path": "../foreign"},
        )
        for action in actions:
            with self.subTest(action=action), self.assertRaises(DurableStateError):
                service.dispatch(action)

    def test_real_sampler_commits_with_bounded_lifecycle_and_reset_does_not_touch_live_data(
        self,
    ) -> None:
        module = self.module()
        committed = threading.Event()
        service = self.service(poll_seconds=0.05)
        original = module.DurableStateStore.save

        def observe(
            store: DurableStateStore, records: tuple[StateRecord, ...], **kwargs: Any
        ) -> StateSnapshot:
            result = original(store, records, **kwargs)
            committed.set()
            return cast(StateSnapshot, result)

        with patch.object(module.DurableStateStore, "save", observe):
            self.enable(service)
            self.assertTrue(committed.wait(1.0), "opt-in sampler must reach a real manifest commit")
        status = service.dispatch({"intent": "status"})
        self.assertEqual(status["projection"]["save_state"], "saved")
        cleared = service.dispatch(
            {"intent": "reset", "expected_revision": status["projection"]["revision"]}
        )
        self.assertFalse(cleared["projection"]["enabled"])
        self.assertEqual(cleared["projection"]["records"], [])
        self.assertEqual(self.current, (metadata(),))
        service.close()
        self.assertFalse(service.sampler_alive)


if __name__ == "__main__":
    unittest.main()
