from __future__ import annotations

import copy
import importlib
import os
import pickle
import struct
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_m25_render_planner import _snapshot_wire
from test_m25_render_source_currentness import _video_source

from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest
from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool, OwnedImageSource
from comfyui_h3_context.adapters.authoring_render_source import (
    AuthoringRenderSourceClaim,
    BoundAuthoringRenderPlan,
    PreparedAuthoringHistory,
    SourceOrigin,
    claim_render_source,
    prepare_bound_render_plan,
)
from comfyui_h3_context.adapters.authoring_source_binding import (
    AuthoringSourceBindingReceipt,
    ProcessLocalAuthoringSourceBindingStore,
    RuntimeVideoCapability,
)
from comfyui_h3_context.core.authoring_render_jobs import RenderJobLimits
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.composition_contract import (
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.contracts import AssetRole, MediaKind
from comfyui_h3_context.core.registry import (
    ReferenceAsset,
    ReferenceRegistry,
    build_reference_registry,
)
from comfyui_h3_context.core.render_planner import source_facts_fingerprint


class ImageReceipt(AuthoringSourceBindingReceipt):
    def __init__(self, registry: ReferenceRegistry, generation: int, source: OwnedImageSource):
        super().__init__(exact_registry=registry, generation=generation)
        self.source = source

    def _capability_for(self, source_id: str) -> RuntimeVideoCapability:
        assert source_id == "img-overlay"
        return RuntimeVideoCapability.AVAILABLE

    def _claim_source(self, source_id: str) -> object:
        assert source_id == "img-overlay"
        return self.source

    def _release_sources(self) -> None:
        self.source.release()


class ImageFactory:
    """Fixture enters below tensor capture; it does not qualify a runtime IMAGE adapter."""

    def capture(
        self,
        *,
        exact_registry: ReferenceRegistry,
        generation: int,
        sources: tuple[tuple[str, MediaKind, object], ...],
    ) -> AuthoringSourceBindingReceipt:
        assert len(sources) == 1
        source = sources[0][2]
        assert type(source) is OwnedImageSource
        return ImageReceipt(exact_registry, generation, source)


def image_source(pool: ImageSourcePool, value: float = 0.25) -> OwnedImageSource:
    pixels = struct.pack("<12f", *([value] * 12))
    source = OwnedImageSource(pool, pixels, 2, 2, time.monotonic() + 900.0)
    pool._sources[id(source)] = source
    pool._live_bytes += len(pixels)
    return source


@pytest.fixture
def image_bound() -> Any:
    pool = ImageSourcePool()
    store = ProcessLocalAuthoringSourceBindingStore(factory=ImageFactory())
    registry = build_reference_registry(
        (ReferenceAsset("img-overlay", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
    )
    source = image_source(pool)
    receipt = store.capture(
        exact_registry=registry, sources=(("img-overlay", MediaKind.IMAGE, source),)
    )
    assert store.claim(registry) is receipt
    claim = claim_render_source(receipt, "img-overlay")
    wire = _snapshot_wire()
    wire["assets"] = [claim.asset.to_wire()]
    wire["tracks"] = [
        row for row in wire["tracks"] if row["kind"] in {"primary_video", "image_overlay"}
    ]
    wire["clips"] = [row for row in wire["clips"] if row["asset_id"] == "img-overlay"]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    history = PreparedAuthoringHistory(
        snapshot, receipt.generation, (claim,), load_packaged_font_manifest()
    )
    workspace_live = [True]
    bound = prepare_bound_render_plan(history, snapshot, lambda: workspace_live[0])
    yield pool, store, registry, receipt, source, bound, workspace_live
    receipt.release()
    store.close()
    pool.close()


def leases() -> Any:
    return importlib.import_module("comfyui_h3_context.adapters.authoring_render_leases")


def acquire(bound: BoundAuthoringRenderPlan, **kwargs: Any) -> Any:
    return leases().acquire_render_job_sources(bound, deadline=time.monotonic() + 60.0, **kwargs)


def test_running_image_survives_workspace_release_but_not_own_release(image_bound: Any) -> None:
    pool, _store, _registry, receipt, _source, bound, live = image_bound
    job = acquire(bound)
    try:
        before = job.read_source("img-overlay")
        live[0] = False
        receipt.release()
        assert pool.live_bytes == 0
        assert job.read_source("img-overlay") == before
        assert job.confirm_currentness() == bound.plan.source_currentness_claim
        assert job.source_bytes == 48
    finally:
        job.release()
    assert job.source_bytes == 0
    job.release()
    with pytest.raises(leases().RenderSourceLeaseError, match="source_released"):
        job.read_source("img-overlay")


def test_replacement_after_workspace_release_revokes_running_image(image_bound: Any) -> None:
    pool, store, registry, receipt, _source, bound, live = image_bound
    job = acquire(bound)
    try:
        live[0] = False
        receipt.release()
        replacement = store.capture(
            exact_registry=registry,
            sources=(("img-overlay", MediaKind.IMAGE, image_source(pool, 0.75)),),
        )
        assert replacement.generation != receipt.generation
        with pytest.raises(leases().RenderSourceLeaseError, match="source_replaced"):
            job.read_source("img-overlay")
    finally:
        job.release()


@pytest.mark.parametrize("event", ["cancel", "deadline", "store_close"])
def test_running_job_revocation_ends_future_reads(image_bound: Any, event: str) -> None:
    _pool, store, _registry, _receipt, _source, bound, _live = image_bound
    now = [time.monotonic()]
    cancelled = threading.Event()
    job = leases().acquire_render_job_sources(
        bound, deadline=now[0] + 2.0, clock=lambda: now[0], cancelled=cancelled
    )
    try:
        if event == "cancel":
            cancelled.set()
            reason = "cancelled"
        elif event == "deadline":
            now[0] += 2.0
            reason = "source_expired"
        else:
            store.close()
            reason = "service_closed"
        with pytest.raises(leases().RenderSourceLeaseError, match=reason):
            job.read_source("img-overlay")
        assert job.source_bytes == 0
    finally:
        job.release()


@pytest.mark.parametrize("case", ["workspace", "receipt", "cancel", "budget"])
def test_refused_handoff_never_borrows_source(image_bound: Any, case: str) -> None:
    _pool, _store, _registry, receipt, _source, bound, live = image_bound
    options: dict[str, Any] = {}
    if case == "workspace":
        live[0] = False
    elif case == "receipt":
        receipt.release()
    elif case == "cancel":
        cancel = threading.Event()
        cancel.set()
        options["cancelled"] = cancel
    else:
        options["limits"] = RenderJobLimits(max_source_bytes=47)
    with pytest.raises(leases().RenderSourceLeaseError):
        acquire(bound, **options)
    assert receipt._render_borrowers == 0


def test_modified_resolved_plan_cannot_borrow_a_valid_source_claim(image_bound: Any) -> None:
    _pool, _store, _registry, receipt, _source, bound, _live = image_bound
    forged = replace(bound, plan=replace(bound.plan, emits_audio_stream=True))
    with pytest.raises(leases().RenderSourceLeaseError, match="plan_mismatch"):
        acquire(forged)
    assert receipt._render_borrowers == 0


def test_job_lease_is_opaque_and_not_copyable(image_bound: Any) -> None:
    bound = image_bound[5]
    job = acquire(bound)
    try:
        assert repr(job) == "<AuthoringRenderJobSources opaque>"
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError):
                operation(job)
        with pytest.raises(leases().RenderSourceLeaseError, match="source_not_found"):
            job.read_source("arbitrary-private-locator")
    finally:
        job.release()


def video_bound(root: Path) -> tuple[Path, AuthoringSourceBindingReceipt, BoundAuthoringRenderPlan]:
    # Real bounded file authority/rehash, synthetic measured media facts. This unit fixture
    # deliberately cannot qualify a codec, source probe, renderer or final artifact.
    path, receipt, _source, verification = _video_source(root, b"synthetic-video-source")
    wire = _snapshot_wire()
    wire["assets"] = wire["assets"][:1]
    wire["assets"][0]["asset_id"] = "video_1"
    wire["tracks"] = wire["tracks"][:1]
    wire["clips"] = wire["clips"][:1]
    wire["clips"][0]["asset_id"] = "video_1"
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    profile = canonical_fingerprint({"unit": "video-profile"})
    color = canonical_fingerprint({"unit": "video-color"})
    size = path.stat().st_size
    claim = AuthoringRenderSourceClaim(
        snapshot.assets[0],
        SourceOrigin.CONTEXT_VIDEO,
        receipt.generation,
        verification.expected_fingerprint,
        source_facts_fingerprint(
            asset_id="video_1",
            source_id="video_1",
            origin="context_video",
            width=320,
            height=180,
            size_bytes=size,
            duration_milliseconds=2000,
            source_profile_fingerprint=profile,
            color_facts_fingerprint=color,
        ),
        profile,
        color,
        "unit-video-currentness",
        size,
        320,
        180,
        2000,
        verification,
        receipt,
    )
    history = PreparedAuthoringHistory(
        snapshot, receipt.generation, (claim,), load_packaged_font_manifest()
    )
    return path, receipt, prepare_bound_render_plan(history, snapshot, lambda: not receipt.released)


def test_video_survives_workspace_release_and_rehash_detects_same_stat_replacement(
    tmp_path: Path,
) -> None:
    path, receipt, bound = video_bound(tmp_path)
    job = acquire(bound)
    try:
        original = job.read_source("video_1")
        receipt.release()
        assert job.read_source("video_1") == original
        metadata = path.stat()
        path.write_bytes(b"x" * len(original))
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        with pytest.raises(leases().RenderSourceLeaseError, match="source_replaced"):
            job.read_source("video_1")
        assert job.source_bytes == 0
    finally:
        job.release()
        receipt.release()


@pytest.mark.parametrize("event", ["release", "cancel", "expiry"])
def test_video_revocation_does_not_open_the_source_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    from comfyui_h3_context.adapters import av_reconstruction_media

    _path, receipt, bound = video_bound(tmp_path)
    now = [time.monotonic()]
    cancelled = threading.Event()
    job = leases().acquire_render_job_sources(
        bound, deadline=now[0] + 2.0, clock=lambda: now[0], cancelled=cancelled
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("revoked lease opened source bytes")

    monkeypatch.setattr(av_reconstruction_media, "_read_regular_media_body", forbidden)
    try:
        if event == "release":
            job.release()
        elif event == "cancel":
            cancelled.set()
        else:
            now[0] += 2.0
        with pytest.raises(leases().RenderSourceLeaseError):
            job.read_source("video_1")
    finally:
        job.release()
        receipt.release()


def test_release_during_handoff_aborts_without_a_borrower(
    image_bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pool, _store, _registry, receipt, _source, bound, _live = image_bound
    original = leases()._transfer_source

    def transfer_then_release(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        receipt.release()
        return result

    monkeypatch.setattr(leases(), "_transfer_source", transfer_then_release)
    with pytest.raises(leases().RenderSourceLeaseError):
        acquire(bound)
    assert receipt._render_borrowers == 0


def test_failed_post_handoff_confirmation_releases_every_borrower(
    image_bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, bound = image_bound[3], image_bound[5]

    def fail(_self: Any) -> Any:
        raise RuntimeError("synthetic internal failure")

    monkeypatch.setattr(leases().AuthoringRenderJobSources, "confirm_currentness", fail)
    with pytest.raises(RuntimeError, match="synthetic internal failure"):
        acquire(bound)
    assert receipt._render_borrowers == 0
