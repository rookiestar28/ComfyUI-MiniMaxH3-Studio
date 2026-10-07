"""Real process death proves publication and deletion cut points without retrying a writer."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from test_retained_asset_restore import retained
from test_retained_asset_source import OWNER
from test_retained_asset_store import owner_directory, store_module

from comfyui_h3_context.core.retained_assets import RetainedAssetError

CHILD = r"""
import os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
import pytest
from test_retained_asset_source import ready_claim
from test_m25_29_production_authoring_import import _qualified_test_adapter
import comfyui_h3_context.adapters.retained_asset_store as storage
import comfyui_h3_context.adapters.retained_asset_source as admission
root, owner, cut = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
store = storage.RetainedAssetStore(root, owner, clock_ms=lambda: 1000)
write, publish, remove, stream = (storage._write_new_file, storage._replace_manifest,
                                storage._delete, admission._copy_regular_file_into_new)
def partial():
    original = os.write
    def interrupted(fd, data):
        original(fd, data[:7])
        os._exit(71)
    os.write = interrupted
def crash_write(path, payload):
    selected = (path.name.startswith('intent-') if cut.startswith('intent-')
                else path.name.startswith('verified-') if cut.startswith('verified-')
                else path.name.startswith('garbage-') if cut.startswith('clear-garbage-')
                else path.name == 'manifest.next.json')
    if selected and cut.endswith('-partial'):
        partial()
    write(path, payload)
    if selected and cut.endswith('-closed'):
        os._exit(72)
def crash_publish(source, target, **kwargs):
    publish(source, target, **kwargs)
    if cut.endswith('catalog-published'):
        os._exit(73)
def crash_remove(path, identity):
    remove(path, identity)
    if cut == 'clear-media-deleted' and path.suffix == '.mp4':
        os._exit(74)
def crash_stream(source, target, **kwargs):
    if cut == 'media-partial':
        partial()
    result = stream(source, target, **kwargs)
    if cut == 'media-closed':
        os._exit(72)
    return result
storage._write_new_file, storage._replace_manifest, storage._delete = (
    crash_write, crash_publish, crash_remove)
admission._copy_regular_file_into_new = crash_stream
catalog = store.read()
if cut.startswith('clear-'):
    store.clear(expected_revision=catalog.revision)
else:
    claim, _, _, _ = ready_claim(root.parent)
    monkey = pytest.MonkeyPatch()
    adapter = _qualified_test_adapter(root.parent, monkey)
    source = admission.stage_retention_source(claim=claim, owner_id=owner,
        media_adapter=adapter, scratch_root=root.parent / 'authoring-scratch',
        deadline=time.monotonic() + 10.0)
    store.admit(source, expected_revision=catalog.revision, deadline=time.monotonic() + 10.0)
sys.exit(5)
"""


def crash(tmp_path: Path, cut: str) -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-c", CHILD, str(tmp_path / "private"), OWNER, cut],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode in {71, 72, 73, 74}, result.stdout + result.stderr


def snapshot(directory: Path) -> dict[str, bytes]:
    # Do not reopen the native owner's byte-zero lock while that process owns it.
    return {
        path.name: path.read_bytes() for path in directory.iterdir() if path.name != ".owner.lock"
    }


@pytest.mark.parametrize(
    "cut",
    (
        "intent-partial",
        "intent-closed",
        "media-partial",
        "media-closed",
        "verified-partial",
        "verified-closed",
        "catalog-partial",
        "catalog-closed",
        "catalog-published",
    ),
)
def test_copy_process_cuts_expose_only_verified_whole_catalog_rows(
    tmp_path: Path, cut: str
) -> None:
    module = store_module()
    store = module.RetainedAssetStore(tmp_path / "private", OWNER, clock_ms=lambda: 1000)
    before = store.set_enabled(True, expected_revision=0)
    store.close()
    crash(tmp_path, cut)
    restarted = module.RetainedAssetStore(tmp_path / "private", OWNER, clock_ms=lambda: 1000)
    directory = owner_directory(tmp_path)
    try:
        recovered = restarted.read()
        if cut == "catalog-published":
            assert recovered.revision == before.revision + 1 and len(recovered.assets) == 1
            media = directory / (recovered.assets[0].asset_id + ".mp4")
            assert media.read_bytes() == b"bounded-generated-video-1"
        else:
            assert recovered == before
        if cut.endswith("-partial") and cut != "media-partial":
            intact = snapshot(directory)
            with pytest.raises(RetainedAssetError):
                restarted.collect(expected_revision=recovered.revision)
            assert snapshot(directory) == intact
        else:
            restarted.collect(expected_revision=recovered.revision)
            if cut in {"media-partial", "media-closed"}:
                assert list(directory.glob("asset_*.mp4"))
                assert list(directory.glob("intent-*.json"))
            elif cut == "catalog-published":
                assert list(directory.glob("asset_*.mp4"))
                assert not list(directory.glob("intent-*.json"))
                assert not list(directory.glob("verified-*.json"))
            else:
                assert not list(directory.glob("asset_*.mp4"))
                assert not list(directory.glob("intent-*.json"))
                assert not list(directory.glob("verified-*.json"))
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "cut",
    (
        "clear-garbage-partial",
        "clear-garbage-closed",
        "clear-catalog-partial",
        "clear-catalog-closed",
        "clear-catalog-published",
        "clear-media-deleted",
    ),
)
def test_cleanup_process_cuts_never_delete_media_still_in_the_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
) -> None:
    store, before, asset, _, _, _ = retained(tmp_path, monkeypatch)
    store.close()
    crash(tmp_path, cut)
    restarted = store_module().RetainedAssetStore(tmp_path / "private", OWNER)
    directory = owner_directory(tmp_path)
    try:
        recovered = restarted.read()
        media = directory / (asset.asset_id + ".mp4")
        committed = cut in {"clear-catalog-published", "clear-media-deleted"}
        if committed:
            assert recovered.revision == before.revision + 1 and recovered.assets == ()
        else:
            assert recovered == before and media.read_bytes() == b"bounded-generated-video-1"
        if cut.endswith("-partial"):
            intact = snapshot(directory)
            with pytest.raises(RetainedAssetError):
                restarted.collect(expected_revision=recovered.revision)
            assert snapshot(directory) == intact
        else:
            restarted.collect(expected_revision=recovered.revision)
            assert media.exists() != committed
            assert not list(directory.glob("garbage-*.json"))
    finally:
        restarted.close()
