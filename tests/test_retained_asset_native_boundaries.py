"""Actual foreign processes, native links and volatile sweeping cannot bypass retention guards."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from queue import Queue

import pytest
from test_managed_artifact_scopes import _dead_scope
from test_retained_asset_restore import retained
from test_retained_asset_source import OWNER
from test_retained_asset_store import live_source, owner_directory, store_module

from comfyui_h3_context.adapters import managed_artifact_scopes as scopes
from comfyui_h3_context.core.retained_assets import RetainedAssetError

_CHILD = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
import pytest
from test_retained_asset_store import live_source
import comfyui_h3_context.adapters.retained_asset_store as storage
import comfyui_h3_context.adapters.retained_asset_source as admission
root, owner, mode, fixture = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
store = storage.RetainedAssetStore(root, owner,
    limits=storage.RetainedStoreLimits(max_global_assets=1))
source, monkey = None, pytest.MonkeyPatch()
try:
    catalog = store.read()
    if mode == 'owner':
        print('READY', flush=True)
        sys.stdin.readline()
    else:
        fixture.mkdir()
        source = live_source(fixture, monkey)
        copy = admission._copy_regular_file_into_new
        def blocked_copy(*args, **kwargs):
            print('READY', flush=True)
            sys.stdin.readline()
            return copy(*args, **kwargs)
        admission._copy_regular_file_into_new = blocked_copy
        updated, asset = store.admit(source, expected_revision=catalog.revision,
            deadline=time.monotonic() + 20.0)
        assert len(updated.assets) == 1
finally:
    if source is not None:
        source.release()
    store.close()
    monkey.undo()
"""


@contextmanager
def _native_child(tmp_path: Path, mode: str) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            _CHILD,
            str(tmp_path / "private"),
            OWNER,
            mode,
            str(tmp_path / "child-source"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready: Queue[str] = Queue()
        stdout = process.stdout
        assert stdout is not None
        reader = threading.Thread(target=lambda: ready.put(stdout.readline()), daemon=True)
        reader.start()
        assert ready.get(timeout=15).strip() == "READY"
        yield process
    finally:
        output, errors = process.communicate(input="\n", timeout=15)
        assert process.returncode == 0, (output, errors)


def test_foreign_process_owner_lease_blocks_read_and_mutation_until_actual_close(
    tmp_path: Path,
) -> None:
    module = store_module()
    initialized = module.RetainedAssetStore(tmp_path / "private", OWNER)
    catalog = initialized.set_enabled(True, expected_revision=0)
    initialized.close()
    candidate = module.RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        with _native_child(tmp_path, "owner"):
            for operation in (
                candidate.read,
                lambda: candidate.clear(expected_revision=catalog.revision),
            ):
                with pytest.raises(RetainedAssetError, match="owner_busy"):
                    operation()
        assert candidate.read() == catalog
    finally:
        candidate.close()


def test_competing_foreign_process_write_holds_global_guard_then_charges_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = store_module()
    first = module.RetainedAssetStore(tmp_path / "private", OWNER)
    first.set_enabled(True, expected_revision=0)
    first.close()
    other = "owner_" + "b" * 32
    second = module.RetainedAssetStore(
        tmp_path / "private",
        other,
        limits=module.RetainedStoreLimits(max_global_assets=1),
    )
    catalog = second.set_enabled(True, expected_revision=0)
    source_root = tmp_path / "parent-source"
    source_root.mkdir()
    source = live_source(source_root, monkeypatch, owner_id=other)
    try:
        with _native_child(tmp_path, "writer"):
            with pytest.raises(RetainedAssetError, match="catalog_busy"):
                second.admit(
                    source, expected_revision=catalog.revision, deadline=time.monotonic() + 10.0
                )
        # Separate explicit admission after the competitor's whole commit; no retry loop.
        with pytest.raises(RetainedAssetError, match="quota_assets"):
            second.admit(
                source, expected_revision=catalog.revision, deadline=time.monotonic() + 10.0
            )
        assert second.read() == catalog
        assert len(list(owner_directory(tmp_path).glob("asset_*.mp4"))) == 1
        assert not list(owner_directory(tmp_path, other).glob("asset_*.mp4"))
    finally:
        source.release()
        second.close()


@pytest.mark.parametrize("entry", ("media", "manifest", "owner-lock", "family", "global-lock"))
def test_retained_native_hardlink_is_refused_without_deleting_any_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    store, _, asset, _, _, _ = retained(tmp_path, monkeypatch)
    store.close()
    directory = owner_directory(tmp_path)
    path = {
        "media": directory / (asset.asset_id + ".mp4"),
        "manifest": directory / "manifest.json",
        "owner-lock": directory / ".owner.lock",
        "family": directory.parent / ".h3-retained-assets-v1",
        "global-lock": directory.parent / ".catalog.lock",
    }[entry]
    linked = tmp_path / "foreign-link.bin"
    os.link(path, linked)
    before = linked.read_bytes()
    candidate = store_module().RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        with pytest.raises(RetainedAssetError, match="storage_unsafe"):
            candidate.inventory()
        assert linked.read_bytes() == before and path.read_bytes() == before
    finally:
        candidate.close()


def test_retained_private_prefix_junction_is_refused_and_foreign_target_preserved(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction integration is qualified on Windows")
    link, target = tmp_path / "private", tmp_path / "foreign-target"
    target.mkdir()
    sentinel = target / "sentinel.bin"
    sentinel.write_bytes(b"foreign")
    assert link.is_relative_to(tmp_path) and target.is_relative_to(tmp_path)
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    store = store_module().RetainedAssetStore(link, OWNER)
    try:
        with pytest.raises(RetainedAssetError, match="storage_unsafe"):
            store.set_enabled(True, expected_revision=0)
        assert sentinel.read_bytes() == b"foreign" and list(target.iterdir()) == [sentinel]
    finally:
        store.close()
        link.rmdir()


def test_dead_volatile_scope_sweep_preserves_real_retained_catalog_and_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, catalog, asset, _, _, _ = retained(tmp_path, monkeypatch)
    store.close()
    root = _dead_scope(tmp_path)
    (owner_directory(tmp_path) / "unknown-retained.bin").write_bytes(b"preserve uncertainty")
    before = {path.name: path.read_bytes() for path in owner_directory(tmp_path).iterdir()}
    report = scopes.sweep_managed_artifact_scopes(root.parent)
    assert report.removed_scopes == 1 and not root.exists()
    assert {path.name: path.read_bytes() for path in owner_directory(tmp_path).iterdir()} == before
    candidate = store_module().RetainedAssetStore(tmp_path / "private", OWNER)
    try:
        assert candidate.read() == catalog
        assert candidate.inventory().remnant_count == 1
        assert (owner_directory(tmp_path) / (asset.asset_id + ".mp4")).exists()
    finally:
        candidate.close()
