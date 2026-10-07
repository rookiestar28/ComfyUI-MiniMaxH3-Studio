"""Real bounded files, owner leases and commit-point failure semantics."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import patch

from comfyui_h3_context.adapters.durable_state_store import DurableStateStore, StateStoreLimits
from comfyui_h3_context.core.durable_workspace_state import (
    DurableStateError,
    StateRecord,
    StateSnapshot,
    decode_record,
)

MODULE = "comfyui_h3_context.adapters.durable_state_store"
OWNER = "owner_" + "a" * 32


def metadata(number: int = 1, revision: int = 1) -> StateRecord:
    return decode_record(
        {
            "record_id": "record_" + f"{number:032x}",
            "kind": "production",
            "created_at_ms": 1000,
            "closed_at_ms": None,
            "segment_count": 1,
            "state": "workspace_active",
            "revisions": {
                "workspace": revision,
                "reference": None,
                "timeline": None,
                "context": None,
                "transition": 0,
            },
            "last_transition": None,
        }
    )


class DurableStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).absolute() / "private"
        self.clock = lambda: 2000

    def module(self) -> ModuleType:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE),
            "missing bounded owner lease, immutable snapshot and manifest CAS writer",
        )
        return importlib.import_module(MODULE)

    def store(
        self, owner: str = OWNER, limits: StateStoreLimits | None = None
    ) -> DurableStateStore:
        module = self.module()
        result = cast(
            DurableStateStore,
            module.DurableStateStore(self.root, owner, clock_ms=self.clock, limits=limits),
        )
        self.addCleanup(result.close)
        return result

    def enabled(self) -> DurableStateStore:
        store = self.store()
        manifest, snapshot = store.read()
        self.assertEqual(manifest.revision, 0)
        self.assertFalse(manifest.enabled)
        self.assertIsNone(snapshot)
        self.assertFalse(self.root.exists(), "default-off reads must not create a private subtree")
        store.set_enabled(True, expected_revision=0)
        return store

    def save(self, store: DurableStateStore, number: int = 1, version: int = 1) -> StateSnapshot:
        manifest, _ = store.read()
        return store.save((metadata(number, version),), expected_revision=manifest.revision)

    def current(self, store: DurableStateStore) -> StateSnapshot:
        snapshot = store.read()[1]
        assert snapshot is not None
        return snapshot

    def test_construction_and_disabled_read_have_no_filesystem_effect(self) -> None:
        store = self.store()
        self.assertFalse(self.root.exists())
        self.assertEqual(store.read()[0].revision, 0)
        self.assertFalse(self.root.exists())

    def test_enabled_state_and_two_revisions_survive_new_store_instance(self) -> None:
        store = self.enabled()
        first = self.save(store)
        second = self.save(store, version=2)
        self.assertGreater(second.revision, first.revision)
        store.close()
        reopened = self.store()
        manifest, snapshot = reopened.read()
        assert (
            snapshot is not None and manifest.latest is not None and manifest.previous is not None
        )
        self.assertTrue(manifest.enabled)
        self.assertEqual(snapshot.records, (metadata(1, 2),))
        self.assertEqual(manifest.latest.revision, snapshot.revision)
        self.assertEqual(manifest.previous.revision, first.revision)
        self.assertNotIn("prompt", json.dumps(snapshot.to_wire()))

    def test_stale_cas_cannot_replace_newer_state_or_preference(self) -> None:
        store = self.enabled()
        self.save(store)
        manifest, snapshot = store.read()
        for operation in (
            lambda: store.save((metadata(1, 9),), expected_revision=0),
            lambda: store.set_enabled(False, expected_revision=0),
            lambda: store.reset(expected_revision=0),
        ):
            with self.assertRaisesRegex(DurableStateError, "revision_conflict"):
                operation()
            self.assertEqual(store.read(), (manifest, snapshot))

    def test_disable_preserves_data_and_refuses_saves(self) -> None:
        store = self.enabled()
        self.save(store)
        before = store.read()[1]
        manifest = store.set_enabled(False, expected_revision=store.read()[0].revision)
        with self.assertRaisesRegex(DurableStateError, "recovery_disabled"):
            store.save((metadata(1, 2),), expected_revision=manifest.revision)
        self.assertEqual(store.read()[1], before)
        store.close()
        self.assertFalse(self.store().read()[0].enabled)

    def test_failed_fsync_preserves_last_committed_revision_without_false_ack(self) -> None:
        module = self.module()
        store = self.enabled()
        self.save(store)
        before = store.read()
        with patch.object(module.os, "fsync", side_effect=OSError("injected disk failure")):
            with self.assertRaises(DurableStateError):
                self.save(store, version=2)
        self.assertEqual(store.read(), before)

    def test_failed_manifest_replace_preserves_previous_committed_state(self) -> None:
        module = self.module()
        store = self.enabled()
        self.save(store)
        before = store.read()
        with patch.object(
            module, "_replace_manifest", side_effect=OSError("injected replace failure")
        ):
            with self.assertRaises(DurableStateError):
                self.save(store, version=2)
        self.assertEqual(store.read(), before)
        store.close()
        reopened = self.store()
        self.assertEqual(reopened.read(), before)
        self.save(reopened, version=3)
        self.assertEqual(self.current(reopened).records, (metadata(1, 3),))

    def test_corrupt_latest_falls_back_to_its_own_previous_not_an_unreferenced_file(self) -> None:
        store = self.enabled()
        first = self.save(store)
        self.save(store, version=2)
        manifest = store.read()[0]
        assert manifest.latest is not None
        current = store.owner_directory / manifest.latest.file_name
        current.write_bytes(b"invalid-json")
        self.assertEqual(store.read()[1], first)
        self.assertTrue(current.exists(), "corruption must not trigger deletion")

    def test_unknown_latest_version_refuses_downgrade_and_preserves_files(self) -> None:
        store = self.enabled()
        self.save(store)
        self.save(store, version=2)
        manifest = store.read()[0]
        assert manifest.latest is not None
        target = store.owner_directory / manifest.latest.file_name
        data = json.loads(target.read_bytes())
        data["schema"] = "h3.context.workspace_state.v999"
        target.write_text(json.dumps(data), encoding="utf-8")
        before = target.read_bytes()
        with self.assertRaisesRegex(DurableStateError, "version_unsupported"):
            store.read()
        self.assertEqual(target.read_bytes(), before)

    def test_owner_isolation_and_bounded_owned_reset(self) -> None:
        own = self.enabled()
        self.save(own)
        foreign = self.store("owner_" + "b" * 32)
        foreign.set_enabled(True, expected_revision=0)
        self.save(foreign, number=2)
        before = foreign.read()
        manifest = own.reset(expected_revision=own.read()[0].revision)
        self.assertFalse(manifest.enabled)
        self.assertIsNone(own.read()[1])
        self.assertEqual(foreign.read(), before)
        self.assertEqual(
            [p.name for p in own.owner_directory.iterdir()], [".owner.lock", "manifest.json"]
        )

    def test_unknown_entry_is_preserved_and_refuses_new_writes_not_valid_readback(self) -> None:
        store = self.enabled()
        self.save(store)
        before = store.read()
        extra = store.owner_directory / "unowned-file.txt"
        extra.write_bytes(b"unrelated")
        with self.assertRaises(DurableStateError):
            self.save(store, version=2)
        self.assertEqual(store.read(), before)
        self.assertEqual(extra.read_bytes(), b"unrelated")

    def test_previous_pending_and_global_bytes_are_charged_before_writing(self) -> None:
        module = self.module()
        limits = replace(module.StateStoreLimits(), max_total_bytes=1600)
        store = self.store(limits=limits)
        store.set_enabled(True, expected_revision=0)
        self.save(store)
        before = store.read()
        with self.assertRaisesRegex(DurableStateError, "quota"):
            self.save(store, version=2)
        self.assertEqual(store.read(), before)

    def test_global_distinct_record_cap_includes_previous_revisions(self) -> None:
        module = self.module()
        limits = replace(module.StateStoreLimits(), max_global_records=2)
        own = self.store(limits=limits)
        own.set_enabled(True, expected_revision=0)
        self.save(own, number=1)
        self.save(own, number=2)
        before = own.read()
        with self.assertRaisesRegex(DurableStateError, "quota"):
            self.save(own, number=3)
        self.assertEqual(own.read(), before)

    def test_global_distinct_cap_charges_two_live_owner_namespaces(self) -> None:
        limits = replace(self.module().StateStoreLimits(), max_global_records=2)
        own = self.store(limits=limits)
        own.set_enabled(True, expected_revision=0)
        self.save(own, number=1)
        foreign = self.store("owner_" + "b" * 32, limits=limits)
        foreign.set_enabled(True, expected_revision=0)
        self.save(foreign, number=2)
        before = own.read(), foreign.read()
        with self.assertRaisesRegex(DurableStateError, "quota_records"):
            self.save(own, number=3)
        self.assertEqual((own.read(), foreign.read()), before)

    def test_catalog_inventory_caps_foreign_owner_lock_without_modifying_it(self) -> None:
        own = self.enabled()
        self.save(own)
        foreign = self.store("owner_" + "b" * 32)
        foreign.set_enabled(True, expected_revision=0)
        self.save(foreign, number=2)
        before = own.read()
        path = foreign.owner_directory / ".owner.lock"
        descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            os.lseek(descriptor, 1024, os.SEEK_SET)
            os.write(descriptor, b"oversized-foreign-metadata")
        finally:
            os.close(descriptor)
        size = path.stat().st_size
        with self.assertRaises(DurableStateError):
            self.save(own, version=2)
        self.assertEqual(own.read(), before)
        self.assertEqual(path.stat().st_size, size)

    def test_hardlinked_family_marker_refuses_without_deleting_alias(self) -> None:
        own = self.enabled()
        self.save(own)
        marker = own.root / ".h3-workspace-state-v1"
        alias = Path(self.temp.name) / "family-alias.json"
        os.link(marker, alias)
        original = alias.read_bytes()
        with self.assertRaisesRegex(DurableStateError, "storage_unsafe"):
            own.read()
        self.assertEqual(alias.read_bytes(), original)

    def test_hardlinked_retained_owner_lease_refuses_without_erasing_data(self) -> None:
        own = self.enabled()
        self.save(own)
        alias = Path(self.temp.name) / "owner-alias.json"
        os.link(own.owner_directory / ".owner.lock", alias)
        before = tuple(sorted(path.name for path in own.owner_directory.iterdir()))
        with self.assertRaisesRegex(DurableStateError, "storage_unsafe"):
            own.reset(expected_revision=own.read()[0].revision)
        self.assertEqual(tuple(sorted(path.name for path in own.owner_directory.iterdir())), before)
        self.assertTrue(alias.exists())

    def test_real_foreign_process_cannot_steal_owner_lease(self) -> None:
        store = self.enabled()
        self.save(store)
        code = (
            "from pathlib import Path; from comfyui_h3_context.adapters.durable_state_store "
            "import DurableStateStore; from comfyui_h3_context.core.durable_workspace_state "
            "import DurableStateError; import sys; "
            "s=DurableStateStore(Path(sys.argv[1]),sys.argv[2]); "
            "\ntry: s.read()\nexcept DurableStateError as e: "
            "print(str(e)); sys.exit(0 if str(e)=='owner_busy' else 2)\nelse: sys.exit(3)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(self.root), OWNER],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "owner_busy")

    def test_unsafe_ids_and_linked_private_prefix_refuse(self) -> None:
        module = self.module()
        for identifier in ("../owner", "owner_" + "a" * 33, "default", "owner_a"):
            with self.subTest(identifier=identifier), self.assertRaises(DurableStateError):
                module.DurableStateStore(self.root, identifier)
        self.root.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.root / "workspace-state"
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.addCleanup(link.rmdir)
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(DurableStateError):
            self.store().set_enabled(True, expected_revision=0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_hardlinked_snapshot_refuses_read_and_cleanup(self) -> None:
        store = self.enabled()
        self.save(store)
        manifest = store.read()[0]
        assert manifest.latest is not None
        target = store.owner_directory / manifest.latest.file_name
        alias = Path(self.temp.name) / "alias.json"
        os.link(target, alias)
        before = alias.read_bytes()
        with self.assertRaises(DurableStateError):
            store.read()
        with self.assertRaises(DurableStateError):
            store.reset(expected_revision=manifest.revision)
        self.assertEqual(alias.read_bytes(), before)

    def test_retained_owner_lease_rechecks_closed_metadata_not_only_current_inode(self) -> None:
        store = self.enabled()
        self.save(store)
        lock = store.owner_directory / ".owner.lock"
        descriptor = os.open(lock, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            os.lseek(descriptor, 1, os.SEEK_SET)
            os.write(descriptor, b"x")
        finally:
            os.close(descriptor)
        with self.assertRaisesRegex(DurableStateError, "storage_unsafe"):
            store.read()

    def test_process_crash_at_each_commit_cut_preserves_only_committed_state(self) -> None:
        child = """
from pathlib import Path
import os, sys
import comfyui_h3_context.adapters.durable_state_store as store_module
from comfyui_h3_context.core.durable_workspace_state import decode_record
root, owner, cut = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
store = store_module.DurableStateStore(root, owner, clock_ms=lambda: 3000)
row = decode_record({'record_id':'record_'+'00000000000000000000000000000001',
 'kind':'production','created_at_ms':1000,'closed_at_ms':None,'segment_count':1,
 'state':'workspace_active','last_transition':None,
 'revisions':{'workspace':2,'reference':None,'timeline':None,'context':None,'transition':0}})
write = store_module._write_new_file
publish = store_module._replace_manifest
def crash_write(path, payload):
    selected = (path.name.startswith('revision-') if cut=='partial-snapshot'
                else path.name=='manifest.next.json')
    if selected and cut in {'partial-snapshot','partial-manifest'}:
        original = os.write
        def partial(fd, data):
            original(fd, data[:13])
            os._exit(71)
        os.write = partial
    write(path, payload)
    if ((cut=='snapshot-closed' and path.name.startswith('revision-'))
        or (cut=='manifest-closed' and path.name=='manifest.next.json')):
        os._exit(72)
def crash_publish(source, target):
    publish(source,target)
    if cut=='manifest-published':
        os._exit(73)
store_module._write_new_file = crash_write
store_module._replace_manifest = crash_publish
manifest, _ = store.read()
store.save((row,), expected_revision=manifest.revision)
sys.exit(5)
"""
        for cut in (
            "partial-snapshot",
            "snapshot-closed",
            "partial-manifest",
            "manifest-closed",
            "manifest-published",
        ):
            with self.subTest(cut=cut):
                self.root = Path(self.temp.name).absolute() / cut
                store = self.enabled()
                before = self.save(store)
                store.close()
                result = subprocess.run(
                    [sys.executable, "-B", "-c", child, str(self.root), OWNER, cut],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertIn(result.returncode, (71, 72, 73), result.stdout + result.stderr)
                reopened = self.store()
                recovered = reopened.read()[1]
                assert recovered is not None
                if cut == "manifest-published":
                    self.assertEqual(recovered.records, (metadata(1, 2),))
                    self.assertGreater(recovered.revision, before.revision)
                else:
                    self.assertEqual(recovered, before)
                if cut.startswith("partial-"):
                    with self.assertRaises(DurableStateError):
                        self.save(reopened, version=3)
                    self.assertEqual(reopened.read()[1], before)
                else:
                    self.save(reopened, version=3)
                    self.assertEqual(self.current(reopened).records, (metadata(1, 3),))
                reopened.close()


if __name__ == "__main__":
    unittest.main()
