"""Real private filesystem recovery, bounded references and closed manifest publication."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_project_document import document_wire

from comfyui_h3_context.adapters.segment_artifact_store import _windows_native_path
from comfyui_h3_context.core.editor_recovery import EditorRecoveryError

MODULE = "comfyui_h3_context.adapters.editor_recovery_store"
OWNER = "owner_" + "a" * 32
PROJECT = "project_" + "b" * 32


class EditorRecoveryStoreTests(unittest.TestCase):
    def module(self) -> Any:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE), "missing independent content recovery store"
        )
        return importlib.import_module(MODULE)

    def store(self, **kwargs: Any) -> Any:
        temporary = tempfile.TemporaryDirectory(
            dir=(
                _windows_native_path(Path(tempfile.gettempdir()), force=True)
                if os.name == "nt"
                else None
            )
        )
        self.addCleanup(temporary.cleanup)
        store = self.module().EditorRecoveryStore(Path(temporary.name), OWNER, **kwargs)
        self.addCleanup(store.close)
        return store

    def save(self, store: Any, title: str = "Synthetic recovery title") -> Any:
        document = document_wire()
        document["title"] = title
        return store.save(
            PROJECT, {"document": document, "history": {"selection": [], "undo": [], "redo": []}}
        )

    def enable(self, store: Any) -> None:
        store.settings(True, False, expected_revision=0)

    def test_disabled_status_has_no_content_or_files_and_save_refuses(self) -> None:
        store = self.store()
        self.assertFalse(store.inventory()["enabled"])
        self.assertFalse(store.root.exists())
        with self.assertRaises(EditorRecoveryError) as error:
            self.save(store)
        self.assertEqual(error.exception.code, "recovery_disabled")
        self.assertFalse(store.root.exists())

    def test_save_reopen_is_exact_without_logging_title_and_settings_are_cas(self) -> None:
        store = self.store()
        self.enable(store)
        snapshot = self.save(store)
        inventory = store.inventory()
        self.assertNotIn("Synthetic", json.dumps(inventory))
        self.assertEqual(store.read(PROJECT), snapshot)
        store.close()
        reopened = self.module().EditorRecoveryStore(store.private_root, OWNER)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.read(PROJECT), snapshot)
        with self.assertRaises(EditorRecoveryError) as error:
            reopened.settings(False, False, expected_revision=0)
        self.assertEqual(error.exception.code, "revision_conflict")

    def test_corrupt_latest_falls_back_but_unknown_files_and_hardlinks_are_preserved_refused(
        self,
    ) -> None:
        store = self.store()
        self.enable(store)
        first = self.save(store, "Synthetic first")
        self.save(store, "Synthetic second")
        manifest = json.loads((store.owner_directory / "manifest.json").read_bytes())
        latest = store.owner_directory / PROJECT / manifest["records"][0]["latest"]
        latest.write_bytes(b"corrupt")
        self.assertEqual(store.read(PROJECT), first)
        unknown = store.owner_directory / PROJECT / "foreign.private"
        unknown.write_bytes(b"foreign")
        with self.assertRaises(EditorRecoveryError):
            self.save(store)
        self.assertEqual(unknown.read_bytes(), b"foreign")
        unknown.unlink()
        backup = latest.parent / "foreign.link"
        os.link(latest, backup)
        with self.assertRaises(EditorRecoveryError):
            store.read(PROJECT)
        self.assertEqual(backup.read_bytes(), b"corrupt")

    def test_failed_manifest_keeps_last_good_and_reconciles_owned_reference_intent(self) -> None:
        calls: list[tuple[str, tuple[str, ...]]] = []
        store = self.store(reference_sync=lambda project, ids: calls.append((project, ids)))
        self.enable(store)
        first = self.save(store, "Synthetic first")
        with patch(MODULE + "._replace_manifest", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(EditorRecoveryError):
                self.save(store, "Synthetic unacknowledged")
        self.assertEqual(store.read(PROJECT), first)
        self.assertTrue(calls)
        self.assertFalse((store.owner_directory / "intent.json").exists())

    def test_owner_lease_is_exclusive_and_closed_retention_never_removes_active(self) -> None:
        now = [1791375400000]
        store = self.store(clock_ms=lambda: now[0])
        self.enable(store)
        self.save(store)
        second = self.module().EditorRecoveryStore(store.private_root, OWNER)
        self.addCleanup(second.close)
        with self.assertRaises(EditorRecoveryError) as error:
            second.inventory()
        self.assertEqual(error.exception.code, "owner_busy")
        store.close_record(PROJECT)
        now[0] += 8 * 86400 * 1000
        self.assertEqual(store.prune_closed(active_ids={PROJECT}), 0)
        self.assertEqual(len(store.inventory()["records"]), 1)
        self.assertEqual(store.prune_closed(active_ids=set()), 1)
        self.assertEqual(store.inventory()["records"], [])

    def test_quota_counts_previous_staged_and_unknown_bytes_before_writing(self) -> None:
        limits = self.module().RecoveryStoreLimits(max_total_bytes=9000)
        store = self.store(limits=limits)
        self.enable(store)
        first = self.save(store)
        with self.assertRaises(EditorRecoveryError) as error:
            self.save(store)
        self.assertEqual(error.exception.code, "quota_bytes")
        self.assertEqual(store.read(PROJECT), first)

    def test_actual_child_process_crash_at_closed_io_cuts_restores_only_committed_data(
        self,
    ) -> None:
        for cut in ("intent_closed", "snapshot_closed", "manifest_closed", "manifest_committed"):
            with self.subTest(cut=cut):
                store = self.store()
                self.enable(store)
                first = self.save(store, "Synthetic acknowledged")
                store.close()
                script = """
import os,sys
from pathlib import Path
from comfyui_h3_context.adapters import editor_recovery_store as module
store=module.EditorRecoveryStore(Path(sys.argv[1]),sys.argv[2])
first=store.read(sys.argv[3])
cut=sys.argv[4]
writer,replace=module._write_new_file,module._replace_manifest
def write(path,data):
    writer(path,data)
    if (cut=='intent_closed' and path.name=='intent.json' or
        cut=='snapshot_closed' and path.name.startswith('revision-') or
        cut=='manifest_closed' and path.name=='manifest.next.json'):
        print(cut,flush=True)
        os._exit(73)
def publish(*args,**kwargs):
    replace(*args,**kwargs)
    if cut=='manifest_committed':
        print(cut,flush=True)
        os._exit(73)
module._write_new_file,module._replace_manifest=write,publish
first['document']['title']='Synthetic unacknowledged'
store.save(sys.argv[3],{'document':first['document'],'history':first['history']})
raise SystemExit(99)
"""
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        script,
                        str(store.private_root),
                        OWNER,
                        PROJECT,
                        cut,
                    ],
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))
                self.assertEqual(result.stdout.decode().strip(), cut)
                recovered = store.read(PROJECT)
                self.assertEqual(
                    recovered["document"]["title"],
                    "Synthetic unacknowledged"
                    if cut == "manifest_committed"
                    else "Synthetic acknowledged",
                )
                if cut != "manifest_committed":
                    self.assertEqual(recovered, first)
                self.assertFalse((store.owner_directory / "intent.json").exists())
