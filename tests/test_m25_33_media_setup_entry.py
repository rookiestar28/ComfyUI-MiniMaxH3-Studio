"""Offline manual wire parity and explicit earlier-runtime recovery."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from comfyui_h3_context.adapters import comfyui_media_runtime_setup as setup
from comfyui_h3_context.adapters import media_runtime_leftovers as leftovers
from comfyui_h3_context.adapters.executable_admission import ExecutableAdmissionError
from comfyui_h3_context.adapters.media_runtime_manager import FEATURES, MediaRuntimeManager
from comfyui_h3_context.adapters.media_runtime_pin import media_runtime_pin
from comfyui_h3_context.adapters.media_runtime_resolution import (
    MediaRuntimePrivateLayout,
    MediaRuntimeResolution,
    MediaRuntimeResolver,
    ResolutionReason,
    ResolutionState,
    SourceKind,
    private_layout,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "frontend/tests/fixtures/media_runtime_wire_v4.json"
JOB = "a" * 32
# IMPORTANT: reclaiming earlier runtimes is Windows-only (the install lock is `msvcrt`); elsewhere
# `reclaim_parked_runtimes` refuses with `unsupported_platform` before touching anything. The
# Windows Full Gate cannot see a missing marker here, only the Linux Hosted backend can, so every
# reclaim test carries `windows_only` and the refusal itself is pinned off Windows below.
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only runtime reclaim")


def wire_fixture() -> dict[str, object]:
    samples = {}
    for name, kind, available, parked in (
        ("status_setup_required", SourceKind.NONE, False, False),
        ("status_override", SourceKind.EXPLICIT_OVERRIDE, True, False),
        ("status_ready_recovery", SourceKind.MANAGED, True, True),
    ):
        resolution = MediaRuntimeResolution(
            ResolutionState.LOCATED if available else ResolutionState.UNAVAILABLE,
            ResolutionReason.PAIR_ADMITTED
            if available
            else ResolutionReason.SUPPORTED_PAIR_MISSING,
            kind,
            ffmpeg_path=REPO / ".tmp/wire-root/bin/ffmpeg.exe" if available else None,
            ffprobe_path=REPO / ".tmp/wire-root/bin/ffprobe.exe" if available else None,
            scratch_root=REPO / ".tmp/wire-root/scratch" if available else None,
            ffmpeg_identity=(1, 2, 3, 4) if available else None,
            ffprobe_identity=(1, 2, 3, 5) if available else None,
        )
        resolver = SimpleNamespace(
            resolve=lambda resolution=resolution, **_: resolution,
            read_config=lambda: None,
            private_layout=lambda: private_layout(REPO / ".tmp/wire-root"),
        )
        manager = SimpleNamespace(
            current=lambda: object(),
            ensure=lambda: None,
            readiness=lambda available=available: {
                feature: {
                    "state": "ready" if available else "setup_required",
                    "reason": None if available else "supported_pair_missing",
                }
                for feature in FEATURES
            },
        )
        service = setup.MediaRuntimeSetupService(
            resolver=cast(MediaRuntimeResolver, resolver),
            manager=cast(MediaRuntimeManager, manager),
            parked_scanner=lambda *_, parked=parked: (Path("parked"),) if parked else (),
            abandoned_scanner=lambda *_: (),
        )
        samples[name] = service.status()
    return {
        "schema": "h3.context.media_runtime_wire_fixture.v4",
        "vocabularies": {
            "actions": list(setup.ACTIONS),
            "feature_states": list(setup.FEATURE_STATES),
            "feature_reasons": sorted(setup.FEATURE_REASONS),
            "recovery_states": list(setup.RECOVERY_STATES),
            "refusal_codes": sorted(setup.REFUSAL_CODES),
        },
        "samples": samples,
    }


def test_wire_fixture_is_the_servers_own_output() -> None:
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == wire_fixture()


def park(root: Path, *, tree: str = "retired") -> tuple[MediaRuntimePrivateLayout, Path]:
    layout = private_layout(root)
    job = layout.media_runtime / "staging" / JOB
    tools = job / tree / "bin"
    tools.mkdir(parents=True)
    (tools / "ffmpeg.exe").write_bytes(b"old tool")
    return layout, job


@contextmanager
def admitted(*_args: object) -> Iterator[object]:
    yield object()


@windows_only
def test_status_scans_never_clean_leftovers_and_unknown_files_survive(tmp_path: Path) -> None:
    layout, job = park(tmp_path)
    (job / "unknown.txt").write_text("keep")
    manifest = media_runtime_pin()
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert leftovers.parked_runtime_trees(layout, manifest) == (job,)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
    layout.managed_bin.mkdir(parents=True)
    assert leftovers.reclaim_parked_runtimes(layout, manifest, pin=admitted) == 1
    assert (job / "unknown.txt").read_text() == "keep"
    assert not (job / "retired/bin/ffmpeg.exe").exists()


@pytest.mark.skipif(os.name == "nt", reason="the refusal path exists off Windows only")
def test_reclaim_is_refused_off_windows_and_leaves_leftovers_untouched(tmp_path: Path) -> None:
    layout, job = park(tmp_path)
    (job / "unknown.txt").write_text("keep")
    layout.managed_bin.mkdir(parents=True)
    manifest = media_runtime_pin()
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert leftovers.parked_runtime_trees(layout, manifest) == (job,)
    with pytest.raises(leftovers.RecoveryError, match="unsupported_platform"):
        leftovers.reclaim_parked_runtimes(layout, manifest, pin=admitted)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
    assert (job / "retired/bin/ffmpeg.exe").read_bytes() == b"old tool"


@windows_only
def test_only_parked_copy_is_kept_without_a_verified_replacement(tmp_path: Path) -> None:
    layout, job = park(tmp_path)
    with pytest.raises(leftovers.RecoveryError, match="reclaim_unsafe"):
        leftovers.reclaim_parked_runtimes(layout, media_runtime_pin(), pin=admitted)
    assert (job / "retired/bin/ffmpeg.exe").read_bytes() == b"old tool"


@windows_only
def test_tampered_replacement_keeps_parked_copy(tmp_path: Path) -> None:
    layout, job = park(tmp_path)
    layout.managed_bin.mkdir(parents=True)

    def refused(*_args: object) -> None:
        raise ExecutableAdmissionError("digest_mismatch")

    with pytest.raises(leftovers.RecoveryError, match="reclaim_unsafe"):
        leftovers.reclaim_parked_runtimes(layout, media_runtime_pin(), pin=refused)
    assert (job / "retired/bin/ffmpeg.exe").exists()


@windows_only
def test_abandoned_archive_and_named_tree_are_explicitly_reclaimed(tmp_path: Path) -> None:
    layout, job = park(tmp_path, tree="tree")
    (job / "archive.zip.part").write_bytes(b"partial")
    (job / "tree/keep.txt").write_text("keep")
    pin = media_runtime_pin()
    assert leftovers.abandoned_setup_jobs(layout, pin) == (job,)
    assert leftovers.reclaim_parked_runtimes(layout, pin) == 1
    assert (job / "tree/keep.txt").exists()
    assert not (job / "archive.zip.part").exists()


@windows_only
def test_old_process_lock_refuses_cleanup_without_waiting(tmp_path: Path) -> None:
    layout, job = park(tmp_path, tree="tree")
    descriptor = leftovers._acquire_install_lock(job.parent)
    try:
        with pytest.raises(leftovers.RecoveryError, match="setup_busy"):
            leftovers.reclaim_parked_runtimes(layout, media_runtime_pin())
        assert (job / "tree/bin/ffmpeg.exe").exists()
    finally:
        leftovers._release_install_lock(descriptor)


@windows_only
def test_linked_tree_is_neither_scanned_nor_deleted(tmp_path: Path) -> None:
    layout, job = park(tmp_path, tree="tree")
    external = tmp_path / "keep"
    external.mkdir()
    (external / "ffprobe.exe").write_bytes(b"keep")
    try:
        (job / "tree/bin/ffprobe.exe").symlink_to(external / "ffprobe.exe")
    except OSError:
        pytest.skip("symlink privilege unavailable")
    leftovers.reclaim_parked_runtimes(layout, media_runtime_pin())
    assert (external / "ffprobe.exe").read_bytes() == b"keep"
    assert (job / "tree/bin/ffprobe.exe").is_symlink()
