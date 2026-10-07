"""Production private-root and lock-proven process-scope lifetime regressions."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from comfyui_h3_context.adapters import comfyui_sequence_coordinator as coordinator
from comfyui_h3_context.adapters import managed_artifact_scopes as scopes
from comfyui_h3_context.adapters.media_runtime_resolution import private_layout
from comfyui_h3_context.adapters.segment_artifact_store import (
    ArtifactStoreError,
    PrivateSegmentArtifactStore,
    _write_new_file,
)
from comfyui_h3_context.core import private_storage_layout as layout

_CHILD = """
from pathlib import Path
import sys
from comfyui_h3_context.adapters import managed_artifact_scopes as scopes
root, mode = Path(sys.argv[1]), sys.argv[2]
if mode == 'guard':
    with scopes._namespace(root):
        print('READY', flush=True)
        sys.stdin.readline()
else:
    store = scopes.open_managed_artifact_store(root, policy=scopes._POLICY, clock_ms=lambda: 100)
    same_store = scopes.open_managed_artifact_store(
        root, policy=scopes._POLICY, clock_ms=lambda: 100
    )
    assert same_store is store
    for name in ('first.bin', 'second.bin'):
        scopes._write_new_file(root / 'artifacts' / name, b'data')
    scopes._write_new_file(root / 'receipts/first.json', b'{}')
    scopes._write_new_file(root / 'staging/first.partial.json', b'{}')
    scopes._write_new_file(root / 'staging/write.tmp', b'tmp')
    print('READY', flush=True)
    if mode == 'live':
        sys.stdin.readline()
"""


@contextmanager
def _child(root: Path, mode: str) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _CHILD, str(root), mode],
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
        # End only this fixture's cooperative child; no production lease release seam.
        output, errors = process.communicate(input="\n", timeout=15)
        assert process.returncode == 0, (output, errors)


def _dead_scope(tmp_path: Path, name: str = "d" * 32) -> Path:
    root = tmp_path / "private/managed-artifacts" / name
    with _child(root, "dead"):
        pass
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def _host_roots(root: Path, *, private: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_input_directory=lambda: str(root / "input"),
        get_output_directory=lambda: str(root / "output"),
        get_temp_directory=lambda: str(root / "temp"),
        get_system_user_directory=lambda _name: str(
            root / "user/__h3_context" if private is None else private
        ),
    )


def test_actual_production_factory_uses_private_off_served_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host_roots(tmp_path)
    monkeypatch.setitem(sys.modules, "folder_paths", host)
    selected = coordinator._host_private_root()
    expected = Path(host.get_system_user_directory("h3_context")) / "managed-artifacts"
    assert selected.parent == expected
    for root in (
        host.get_input_directory(),
        host.get_output_directory(),
        host.get_temp_directory(),
    ):
        served = Path(root)
        assert not selected.is_relative_to(served)
        assert not served.is_relative_to(selected)
    assert not selected.exists(), "Root selection must not initialize or sweep storage"


@pytest.mark.parametrize("placement", ["input", "output/child", "temp", "."])
def test_production_factory_refuses_private_root_overlapping_any_served_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placement: str
) -> None:
    host = _host_roots(tmp_path, private=tmp_path / placement)
    monkeypatch.setitem(sys.modules, "folder_paths", host)
    with pytest.raises(coordinator.SequenceCoordinatorError) as failure:
        coordinator._host_private_root()
    assert failure.value.code == "host_storage_unavailable"
    assert failure.value.status == 503
    # Pin the cause: on Linux before M23-67 this refusal came from the locator grammar, so the
    # outer code alone passed while the overlap check never ran.
    cause = failure.value.__cause__
    assert isinstance(cause, ArtifactStoreError) and cause.code == "unsafe_private_root"
    assert not (tmp_path / "temp/h3-context").exists()


def test_production_factory_has_no_missing_private_api_temp_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host_roots(tmp_path)
    del host.get_system_user_directory
    monkeypatch.setitem(sys.modules, "folder_paths", host)
    with pytest.raises(coordinator.SequenceCoordinatorError) as failure:
        coordinator._host_private_root()
    assert failure.value.code == "host_storage_unavailable"
    assert failure.value.status == 503
    cause = failure.value.__cause__
    assert isinstance(cause, ArtifactStoreError) and cause.code == "private_root_unavailable"
    assert not (tmp_path / "temp/h3-context").exists()


def test_scope_live_foreign_process_is_retained_then_dead_owner_is_reaped(tmp_path: Path) -> None:
    root = tmp_path / "private/managed-artifacts" / ("l" * 32)
    with _child(root, "live") as process:
        # Windows byte zero is really locked by another OS process, not a mocked lock.
        assert process.poll() is None
        report = scopes.sweep_managed_artifact_scopes(root.parent)
        assert report.busy_scopes == report.retained_scopes == 1
        assert report.removed_scopes == report.removed_files == 0
        assert (root / "artifacts/first.bin").read_bytes() == b"data"
    report = scopes.sweep_managed_artifact_scopes(root.parent)
    assert report.removed_scopes == 1
    assert report.removed_files == 5
    assert not root.exists()


def test_namespace_guard_held_by_foreign_process_blocks_admission(tmp_path: Path) -> None:
    parent = tmp_path / "private/managed-artifacts"
    with _child(parent, "guard"):
        with pytest.raises(ArtifactStoreError, match="scope_busy"):
            scopes.sweep_managed_artifact_scopes(parent)
        with pytest.raises(ArtifactStoreError, match="scope_busy"):
            scopes.open_managed_artifact_store(
                parent / ("n" * 32), policy=scopes._POLICY, clock_ms=lambda: 100
            )
        assert not (parent / ("n" * 32)).exists()


@pytest.mark.parametrize(
    "entry",
    [
        "root",
        "artifacts",
        "receipts",
        "staging",
        "policy",
        "state",
        "hardlink",
        "missing-owner",
        "missing-marker",
    ],
)
def test_unknown_scope_inventory_preserves_every_file_before_unlink(
    tmp_path: Path, entry: str
) -> None:
    root = _dead_scope(tmp_path)
    if entry == "root":
        (root / "foreign.bin").write_bytes(b"foreign")
    elif entry in {"artifacts", "receipts", "staging"}:
        (root / entry / "unknown.unrecognized").write_bytes(b"foreign")
    elif entry in {"policy", "state"}:
        target = root / (scopes._STORE_MARKER if entry == "policy" else scopes._STATE)
        target.write_bytes(b"{}")
    elif entry == "hardlink":
        os.link(root / "artifacts/first.bin", tmp_path / "foreign-hardlink.bin")
    else:
        (root / (scopes._OWNER if entry == "missing-owner" else scopes._STORE_MARKER)).unlink()
    before = _snapshot(root)
    report = scopes.sweep_managed_artifact_scopes(root.parent)
    assert report.retained_scopes == 1
    assert report.removed_files == report.removed_scopes == 0
    assert _snapshot(root) == before


def test_unmarked_nonempty_namespace_is_never_adopted(tmp_path: Path) -> None:
    parent = tmp_path / "private/managed-artifacts"
    parent.mkdir(parents=True)
    (parent / "foreign.txt").write_bytes(b"foreign")
    with pytest.raises(ArtifactStoreError, match="foreign_scope_namespace"):
        scopes.sweep_managed_artifact_scopes(parent)
    assert _snapshot(parent) == {"foreign.txt": b"foreign"}


def test_namespace_exclusive_create_race_admits_only_completed_exact_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = _write_new_file

    def raced_write(path: Path, payload: bytes) -> None:
        real_write(path, payload)
        # The existing writer wraps FileExistsError in ArtifactStoreError.
        raise ArtifactStoreError("store_write_failed") from FileExistsError()

    monkeypatch.setattr(scopes, "_write_new_file", raced_write)
    report = scopes.sweep_managed_artifact_scopes(tmp_path / "managed-artifacts")
    assert report.scanned_entries == 2
    assert report.stop_reason == "complete"


@pytest.mark.parametrize("filename", [scopes._FAMILY_MARKER, scopes._GUARD])
def test_namespace_race_never_admits_wrong_or_partial_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    real_write = _write_new_file

    def raced_write(path: Path, payload: bytes) -> None:
        real_write(path, b"{}" if path.name == filename else payload)
        raise ArtifactStoreError("store_write_failed") from FileExistsError()

    monkeypatch.setattr(scopes, "_write_new_file", raced_write)
    with pytest.raises(ArtifactStoreError):
        scopes.sweep_managed_artifact_scopes(tmp_path / "managed-artifacts")


def test_metadata_identity_change_after_inventory_preserves_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _dead_scope(tmp_path)
    real_inventory = scopes._inventory

    def changed_inventory(*args: Any, **kwargs: Any) -> Any:
        result = real_inventory(*args, **kwargs)
        marker = root / scopes._STORE_MARKER
        payload = marker.read_bytes()
        replacement = root / "replacement"
        replacement.write_bytes(payload)
        os.replace(replacement, marker)
        return result

    monkeypatch.setattr(scopes, "_inventory", changed_inventory)
    report = scopes.sweep_managed_artifact_scopes(root.parent)
    assert report.retained_scopes == 1
    assert report.removed_files == 0
    assert (root / "artifacts/first.bin").read_bytes() == b"data"
    assert (root / scopes._STORE_MARKER).exists()


def test_data_parent_identity_change_is_not_authorized_by_same_file_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _dead_scope(tmp_path)
    real_inventory = scopes._inventory

    def changed_inventory(*args: Any, **kwargs: Any) -> Any:
        result = real_inventory(*args, **kwargs)
        original = root / "artifacts"
        moved = tmp_path / "original-artifacts"
        original.rename(moved)
        original.mkdir()
        (moved / "first.bin").rename(original / "first.bin")
        return result

    monkeypatch.setattr(scopes, "_inventory", changed_inventory)
    report = scopes.sweep_managed_artifact_scopes(root.parent)
    assert report.removed_files == report.removed_scopes == 0
    assert report.retained_scopes == 1
    assert (root / "artifacts/first.bin").read_bytes() == b"data"
    assert (tmp_path / "original-artifacts/second.bin").read_bytes() == b"data"


def test_cleanup_work_budget_includes_metadata_reads_and_selected_unlinks(tmp_path: Path) -> None:
    root = _dead_scope(tmp_path)
    files = tuple(path for path in root.rglob("*") if path.is_file())
    metadata_bytes = sum(
        (root / name).stat().st_size
        for name in (scopes._OWNER, scopes._STORE_MARKER, scopes._STATE)
    )
    namespace_bytes = sum(
        (root.parent / name).stat().st_size for name in (scopes._FAMILY_MARKER, scopes._GUARD)
    )
    expected = namespace_bytes + metadata_bytes + sum(path.stat().st_size for path in files)
    report = scopes.sweep_managed_artifact_scopes(root.parent)
    assert report.removed_scopes == 1
    assert report.work_bytes == expected


def test_byte_limited_prefix_cleanup_keeps_ownership_for_next_sweep(tmp_path: Path) -> None:
    root = _dead_scope(tmp_path)
    metadata_bytes = sum(
        (root / name).stat().st_size
        for name in (scopes._OWNER, scopes._STORE_MARKER, scopes._STATE)
    )
    namespace_bytes = sum(
        (root.parent / name).stat().st_size for name in (scopes._FAMILY_MARKER, scopes._GUARD)
    )
    limit = namespace_bytes + metadata_bytes + 4
    report = scopes.sweep_managed_artifact_scopes(
        root.parent, limits=scopes.ScopeSweepLimits(max_bytes=limit)
    )
    assert report.stop_reason == "bytes"
    assert report.work_bytes <= limit
    assert report.removed_files == 1
    assert (root / scopes._OWNER).exists() and (root / scopes._STORE_MARKER).exists()
    remaining = scopes.sweep_managed_artifact_scopes(root.parent)
    assert remaining.removed_files == 4 and remaining.removed_scopes == 1


def test_entry_bound_counts_unknown_and_fixed_namespace_entries(tmp_path: Path) -> None:
    parent = tmp_path / "private/managed-artifacts"
    with scopes._namespace(parent):
        pass
    for index in range(70):
        (parent / f"unknown-{index}").mkdir()
    report = scopes.sweep_managed_artifact_scopes(parent)
    assert report.scanned_entries == 64
    assert report.retained_scopes <= 64
    assert report.stop_reason == "entries"
    assert len(tuple(parent.iterdir())) == 72


def test_entry_budget_never_fetches_a_sixty_fifth_namespace_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "managed-artifacts"
    with scopes._namespace(parent):
        pass
    for index in range(70):
        (parent / f"unknown-{index}").mkdir()
    real_scandir = os.scandir
    observed = 0

    class CountedScan:
        def __enter__(self) -> CountedScan:
            self.entries = real_scandir(parent)
            return self

        def __exit__(self, *args: object) -> None:
            self.entries.close()

        def __iter__(self) -> CountedScan:
            return self

        def __next__(self) -> os.DirEntry[str]:
            nonlocal observed
            result = next(self.entries)
            observed += 1
            return result

    monkeypatch.setattr(
        os,
        "scandir",
        lambda path: CountedScan() if Path(path) == parent else real_scandir(path),
    )
    report = scopes.sweep_managed_artifact_scopes(parent)
    assert observed == report.scanned_entries == 64


@pytest.mark.parametrize("stop", ["time", "cancelled", "bytes"])
def test_admission_stop_never_exceeds_bound_or_mutates_existing_scope(
    tmp_path: Path, stop: str
) -> None:
    root = _dead_scope(tmp_path)
    before = _snapshot(root)
    arguments = (
        {"clock": iter((0.0, 5.0)).__next__}
        if stop == "time"
        else {"should_cancel": lambda: True}
        if stop == "cancelled"
        else {"limits": scopes.ScopeSweepLimits(max_bytes=1)}
    )
    report = scopes.sweep_managed_artifact_scopes(root.parent, **arguments)
    assert report.stop_reason == stop
    assert report.work_bytes <= (1 if stop == "bytes" else 1280 * layout.MIB)
    assert _snapshot(root) == before


def test_interrupted_data_unlink_retains_remaining_restart_inspectable_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _dead_scope(tmp_path)
    real_unlink = scopes._unlink_exact
    calls = 0

    def interrupted_unlink(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture interruption")
        return real_unlink(*args, **kwargs)

    monkeypatch.setattr(scopes, "_unlink_exact", interrupted_unlink)
    report = scopes.sweep_managed_artifact_scopes(root.parent)
    assert report.removed_files == 1 and report.retained_scopes == 1
    assert (root / scopes._OWNER).exists() and (root / scopes._STORE_MARKER).exists()
    monkeypatch.setattr(scopes, "_unlink_exact", real_unlink)
    assert scopes.sweep_managed_artifact_scopes(root.parent).removed_scopes == 1


def test_empty_unmarked_remnant_is_not_name_only_cleanup_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _dead_scope(tmp_path)
    real_remove = scopes._remove_empty_directory

    def interrupted_remove(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == root:
            raise OSError("fixture interruption after metadata removal")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(scopes, "_remove_empty_directory", interrupted_remove)
    assert scopes.sweep_managed_artifact_scopes(root.parent).retained_scopes == 1
    assert root.exists() and not tuple(root.iterdir())
    monkeypatch.setattr(scopes, "_remove_empty_directory", real_remove)
    assert scopes.sweep_managed_artifact_scopes(root.parent).retained_scopes == 1
    assert root.exists()


def test_generic_explicit_store_does_not_adopt_owner_filename_or_wrong_descriptor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "generic"
    root.mkdir()
    owner = root / scopes._OWNER
    owner.write_bytes(scopes._owner_payload("g" * 32))
    with pytest.raises(ArtifactStoreError, match="foreign_store_root"):
        PrivateSegmentArtifactStore(root, policy=scopes._POLICY, clock_ms=lambda: 100)
    foreign = tmp_path / "foreign.lock"
    foreign.write_bytes(b"foreign")
    descriptor = os.open(foreign, os.O_RDONLY)
    try:
        with pytest.raises(ArtifactStoreError, match="unsafe_store_owner"):
            PrivateSegmentArtifactStore(
                root, policy=scopes._POLICY, clock_ms=lambda: 100, owner_lock=descriptor
            )
    finally:
        os.close(descriptor)
    assert not (root / scopes._STORE_MARKER).exists()


def test_first_use_failure_releases_only_unpublished_owner_and_retains_unknown_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private/managed-artifacts" / ("f" * 32)

    def failed_store(*args: Any, **kwargs: Any) -> NoReturn:
        raise ArtifactStoreError("fixture_initialization_failure")

    monkeypatch.setattr(scopes, "PrivateSegmentArtifactStore", failed_store)
    with pytest.raises(ArtifactStoreError, match="fixture_initialization_failure"):
        scopes.open_managed_artifact_store(root, policy=scopes._POLICY, clock_ms=lambda: 100)
    descriptor = scopes._acquire_lock(root / scopes._OWNER, scopes._owner_payload(root.name))
    os.close(descriptor)
    assert os.path.normcase(str(root)) not in scopes._STORES
    assert scopes.sweep_managed_artifact_scopes(root.parent).retained_scopes == 1
    with pytest.raises(ArtifactStoreError):
        scopes.open_managed_artifact_store(root, policy=scopes._POLICY, clock_ms=lambda: 100)


def test_aggregate_scope_inventory_limit_refuses_before_deleting_any_data(tmp_path: Path) -> None:
    root = _dead_scope(tmp_path)
    for index in range(124):
        (root / "artifacts" / f"extra-{index}.bin").write_bytes(b"x")
    before = _snapshot(root)
    report = scopes.sweep_managed_artifact_scopes(root.parent)
    assert report.removed_files == report.removed_scopes == 0
    assert report.retained_scopes == 1
    assert _snapshot(root) == before


def test_private_layout_reservations_are_disjoint_and_do_not_create_recovery_roots(
    tmp_path: Path,
) -> None:
    rows = layout.PRIVATE_STORAGE_SUBTREES
    assert len({row.name for row in rows}) == len(rows)
    assert layout.DURABLE_BYTES == 1680 * layout.MIB
    assert layout.RESERVED_DURABLE_BYTES == 640 * layout.MIB
    assert not next(row for row in rows if row.name == "workspace-state").reserved
    selected = [layout.private_subtree_path(tmp_path / "private", row.name) for row in rows]
    assert not any(path.exists() for path in selected)
    media = private_layout(tmp_path / "private").media_runtime
    assert media == layout.private_subtree_path(tmp_path / "private", "media-runtime")
    assert all(
        not media.is_relative_to(path) and not path.is_relative_to(media)
        for path in selected
        if path != media
    )
    root = _dead_scope(tmp_path)
    durable = root.parent.parent / "recovery-assets"
    durable.mkdir()
    (durable / "foreign.bin").write_bytes(b"durable")
    assert scopes.sweep_managed_artifact_scopes(root.parent).removed_scopes == 1
    assert (durable / "foreign.bin").read_bytes() == b"durable"


@pytest.mark.parametrize("placement", ["scope", "child", "private-prefix"])
def test_real_windows_junction_is_never_followed_or_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placement: str
) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction integration is qualified on Windows")
    root = _dead_scope(tmp_path)
    foreign = tmp_path / "foreign-target"
    foreign.mkdir()
    (foreign / "sentinel.bin").write_bytes(b"foreign")
    link = (
        root.parent / ("j" * 32)
        if placement == "scope"
        else root / "linked-child"
        if placement == "child"
        else tmp_path / "private-link"
    )
    # Both exact paths are task-owned workspace fixtures. Remove only the junction itself.
    assert link.is_relative_to(tmp_path) and foreign.is_relative_to(tmp_path)
    created = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(foreign)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert created.returncode == 0, created.stderr
    try:
        if placement == "private-prefix":
            host = _host_roots(tmp_path, private=link / "private")
            monkeypatch.setitem(sys.modules, "folder_paths", host)
            with pytest.raises(
                coordinator.SequenceCoordinatorError, match="host_storage_unavailable"
            ):
                coordinator._host_private_root()
        else:
            report = scopes.sweep_managed_artifact_scopes(root.parent)
            assert report.retained_scopes == 1
            if placement == "child":
                assert report.removed_files == 0
        assert (foreign / "sentinel.bin").read_bytes() == b"foreign"
        assert link.exists()
    finally:
        link.rmdir()
