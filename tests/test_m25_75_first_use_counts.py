"""What the first clip of a prepared asset costs, counted, and what playback does to a preparation.

A catalog video asset's proxy and audio preview are generated before any clip uses the asset: the
page asks for them through the authoring-bound asset request and ends each lease unopened. These
tests drive the accepted modules -- the workspace registries, the import service, the lease
authority and its route, the derivative claim and generator, the pinned FFmpeg pair -- and count
the media work per phase, as the edit-path counts do. The preparation pays one generation per
kind; the first clip-scope create of each kind then performs none and returns the body that was
prepared. A clip whose sound is not played -- on an overlay track, or disabled -- is given neither
prepared body. The last test has clip playback take the route's single worker from a preparation
while its encoder is running.
"""

from __future__ import annotations

import asyncio
import subprocess  # noqa: S404 -- only subclassed here, to see the processes the product starts
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from test_m25_29_production_authoring_import import (
    _authoring_action,
    _import_request,
    _insert_asset_transaction,
    _registries_with_ready_output,
)
from test_m25_45_lease_cross_boundary import release_command
from test_m25_74_derivative_rework_counts import (  # noqa: F401 -- `counts` is a fixture
    _AFTER_EDIT,
    _COLD_AUDIO,
    _COLD_VIDEO,
    FRAMES,
    HEIGHT,
    WIDTH,
    Table,
    _Counts,
    _create,
    _create_request,
    _encode_source,
    _product_authority,
    counts,
)
from test_m25_75_asset_preparation import _authoring_wire, _decode
from test_m25_authoring_audio_rate_compatibility import _qualified_tools

from comfyui_h3_context.adapters.authoring_derivative_generator import AuthoringDerivativeGenerator
from comfyui_h3_context.adapters.authoring_derivative_source import AuthoringDerivativeClaim
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.core.authoring_media import (
    NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
    RUNTIME_PROFILE_FINGERPRINT,
    CreateLeaseRequest,
    DerivativeKind,
    MediaLeaseError,
    public_asset_manifest_fingerprint,
)
from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.nle_authoring_contract import (
    NleAuthoringState,
    create_nle_authoring_state,
)

# One generation per kind when the asset is prepared, as a first use cost before.
_PREPARED = {"prepare the picture": _COLD_VIDEO, "prepare the sound": _COLD_AUDIO}
PREPARED_GENERATED_ORIGIN: Table = {
    **_PREPARED,
    # Two leases, each admitted by one confirmation before and one after the cache lookup.
    "first clip creates": {**_AFTER_EDIT, "artifact.inspect": 4},
}
PREPARED_PATH_BACKED_ORIGIN: Table = {**_PREPARED, "first clip creates": _AFTER_EDIT}

NOTHING_LEASED = {"leases": 0, "cacheEntries": 2}
# Twenty seconds of 720p: an encode long enough for playback to arrive while it runs.
LONG_WIDTH, LONG_HEIGHT, LONG_FRAMES = 1280, 720, 480
MEDIA_TOOLS = ("ffmpeg", "ffprobe")


def _prepare(
    authority: MediaLeaseAuthority,
    counted: _Counts,
    request: Callable[[DerivativeKind], CreateLeaseRequest],
) -> dict[str, dict[str, object]]:
    """What the page does for a catalog asset: one create per kind, each lease ended unopened."""

    receipts: dict[str, dict[str, object]] = {}
    phases: tuple[tuple[str, DerivativeKind], ...] = (
        ("prepare the picture", "video_proxy"),
        ("prepare the sound", "audio_preview"),
    )
    for phase, kind in phases:
        with counted.phase(phase):
            receipt, capability = _create(authority, request(kind))
        authority.release(release_command(receipt), capability)
        receipts[kind] = receipt
    resources = authority.resources()
    # No lease is held for a prepared asset; the two bodies are what the clip will be given.
    assert {key: resources[key] for key in NOTHING_LEASED} == NOTHING_LEASED
    return receipts


def test_a_prepared_generated_asset_costs_its_first_clip_no_media_work(
    tmp_path: Path,
    counts: _Counts,  # noqa: F811
) -> None:
    ffmpeg, ffprobe, adapter = _qualified_tools(tmp_path)
    encoded = (tmp_path / "original.mp4").resolve()
    _encode_source(ffmpeg, encoded)
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "proxy-scratch").resolve(),
    )
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path,
        frame_count=FRAMES,
        artifact_bodies=(encoded.read_bytes(),),
        width=WIDTH,
        height=HEIGHT,
    )
    import_request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        import_request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 300,
    )
    handle = import_request.authoring_workspace_handle
    asset_id = response.receipt.rows[0].asset_id

    def catalog() -> NleAuthoringState:
        held = authoring._entries[handle].timeline_history_v2
        assert held is not None
        return held.authoring

    def admit(request: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        return authoring.admit_media_derivative(request, generator=generator, media_adapter=adapter)

    authority = _product_authority(admit)
    try:
        # The asset is in the catalog and on no track.
        assert catalog().clips == ()
        prepared = _prepare(
            authority,
            counts,
            lambda kind: _decode(
                _authoring_wire(catalog(), asset_id, kind, FRAMES, request_id=f"prepare-{kind}")
            ),
        )

        reply = authoring.dispatch(
            _authoring_action("read.1", "read_timeline_history", workspace_handle=handle)
        )
        assert reply.body is not None
        state = cast(dict[str, Any], reply.body["authoring"])
        primary = next(track for track in state["tracks"] if track["kind"] == "primary_video")
        action = _authoring_action("placed", "apply_timeline_transaction")
        action["payload"] = _insert_asset_transaction(
            authoring_state=state,
            asset_id=asset_id,
            track_id=primary["track_id"],
            request_id="placed",
            duration_frames=FRAMES,
        )
        accepted = authoring.dispatch(action)
        assert accepted.status == 200 and accepted.body is not None, accepted.body
        snapshot = decode_public_snapshot(accepted.body["render_snapshot"])

        with counts.phase("first clip creates"):
            video, _ = _create(
                authority,
                _create_request(snapshot, handle, asset_id, "clip.placed", "video_proxy", "v.1"),
            )
            audio, _ = _create(
                authority,
                _create_request(snapshot, handle, asset_id, "clip.placed", "audio_preview", "a.1"),
            )
        counts.require(PREPARED_GENERATED_ORIGIN)
        assert video["derivativeFingerprint"] == prepared["video_proxy"]["derivativeFingerprint"]
        assert audio["derivativeFingerprint"] == prepared["audio_preview"]["derivativeFingerprint"]
    finally:
        authority.close()


OVERLAY_CLIP = "clip-on-the-overlay"
DISABLED_CLIP = "clip-disabled"


@dataclass(frozen=True)
class _PathBacked:
    """One path-backed video source: in the catalog without a clip, and placed on the primary."""

    admit: Callable[[CreateLeaseRequest], AuthoringDerivativeClaim]
    asset_request: Callable[[DerivativeKind], CreateLeaseRequest]
    clip_request: Callable[[DerivativeKind, str], CreateLeaseRequest]
    # For a clip named by its id: the two clips `sound_excluded` adds beside the primary one.
    request_for: Callable[[str, DerivativeKind, str], CreateLeaseRequest]
    release: Callable[[], None]


def _path_backed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    frames: int = FRAMES,
    sound_excluded: bool = False,
) -> _PathBacked:
    from test_m25_composition_contract import fixture_snapshot
    from test_m25_render_source_currentness import TYPE_AUTHORITY, _file_video

    from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest
    from comfyui_h3_context.adapters.authoring_render_source import (
        PreparedAuthoringHistory,
        claim_render_source,
    )
    from comfyui_h3_context.adapters.authoring_source_binding import (
        PathBackedComfyVideoFromFileV1Factory,
    )
    from comfyui_h3_context.core.contracts import AssetRole, MediaKind
    from comfyui_h3_context.core.registry import ReferenceAsset, build_reference_registry

    ffmpeg, ffprobe, adapter = _qualified_tools(tmp_path)
    root = (tmp_path / "input").resolve()
    root.mkdir()
    _encode_source(ffmpeg, root / "source.mp4", width=width, height=height, frames=frames)
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "proxy-scratch").resolve(),
    )
    monkeypatch.setattr(
        "comfyui_h3_context.adapters.comfyui_authoring_media_preview."
        "current_authoring_media_preview_adapter",
        lambda: adapter,
    )
    binding = PathBackedComfyVideoFromFileV1Factory(
        type_authority=TYPE_AUTHORITY,
        input_root_factory=lambda: root,
        duration_probe=lambda _source: frames * 1000 // 24,
    ).capture(
        exact_registry=build_reference_registry(
            (ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 1),)
        ),
        generation=1,
        sources=(("video_1", MediaKind.VIDEO, _file_video("source.mp4")),),
    )
    source_claim = claim_render_source(binding, "video_1")

    wire = fixture_snapshot()
    wire["assets"] = [source_claim.asset.to_wire()]
    wire["tracks"] = wire["tracks"][: 2 if sound_excluded else 1]
    clip = wire["clips"][0]
    clip.update(asset_id="video_1", duration_frames=1, source_start_frame=0)
    wire["clips"] = [clip]
    if sound_excluded:
        # The same asset twice more, where its sound is not played: disabled on the primary
        # track, and on the video overlay track.
        assert [track["kind"] for track in wire["tracks"]] == ["primary_video", "video_overlay"]
        wire["clips"] = [
            clip,
            {**clip, "clip_id": DISABLED_CLIP, "start_frame": 1, "enabled": False},
            {**clip, "clip_id": OVERLAY_CLIP, "track_id": wire["tracks"][1]["track_id"]},
        ]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    placed: PublicCompositionSnapshot = decode_public_snapshot(wire)
    # The same workspace before the clip was placed: the asset is in the catalog only.
    catalog = create_nle_authoring_state(
        project_id=placed.project_id,
        workspace_handle=placed.workspace_handle,
        workspace_revision=placed.workspace_revision,
        timeline_revision=placed.timeline_revision,
        edit_capacity_frames=3_600,
        assets=placed.assets,
        tracks=placed.tracks,
        clips=(),
        audio_extension=placed.audio_extension,
        blockers=placed.blockers,
    )
    fonts = load_packaged_font_manifest()

    def admit(request: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        # The workspace registry is not part of this construction; the two histories stand for
        # what it holds before and after the clip is placed, over the one source binding.
        if request.request_schema == NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA:
            return AuthoringDerivativeClaim(
                request,
                None,
                PreparedAuthoringHistory(
                    None, binding.generation, (source_claim,), fonts, authoring_state=catalog
                ),
                binding,
                lambda: True,
                generator,
                adapter,
                authoring_state=catalog,
            )
        return AuthoringDerivativeClaim(
            request,
            placed,
            PreparedAuthoringHistory(placed, binding.generation, (source_claim,), fonts),
            binding,
            lambda: True,
            generator,
            adapter,
        )

    def request_for(clip_id: str, kind: DerivativeKind, request_id: str) -> CreateLeaseRequest:
        assert clip_id in {row.clip_id for row in placed.clips}
        return CreateLeaseRequest(
            request_id,
            placed.workspace_handle,
            placed.workspace_revision,
            placed.timeline_revision,
            placed.public_fingerprint,
            public_asset_manifest_fingerprint(placed),
            RUNTIME_PROFILE_FINGERPRINT,
            "clip",
            clip_id,
            "video_1",
            kind,
            clip_id,
            1,
            0,
            frames,
        )

    return _PathBacked(
        admit,
        lambda kind: _decode(
            _authoring_wire(catalog, "video_1", kind, frames, request_id=f"prepare-{kind}")
        ),
        lambda kind, request_id: request_for(str(clip["clip_id"]), kind, request_id),
        request_for,
        binding.release,
    )


def test_a_prepared_path_backed_asset_costs_its_first_clip_no_media_work(
    tmp_path: Path,
    counts: _Counts,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _path_backed(tmp_path, monkeypatch)
    authority = _product_authority(source.admit)
    try:
        prepared = _prepare(authority, counts, source.asset_request)
        with counts.phase("first clip creates"):
            video, _ = _create(authority, source.clip_request("video_proxy", "path.v.1"))
            audio, _ = _create(authority, source.clip_request("audio_preview", "path.a.1"))
        counts.require(PREPARED_PATH_BACKED_ORIGIN)
        assert video["derivativeFingerprint"] == prepared["video_proxy"]["derivativeFingerprint"]
        assert audio["derivativeFingerprint"] == prepared["audio_preview"]["derivativeFingerprint"]
    finally:
        authority.close()
        source.release()


def test_a_clip_whose_sound_is_not_played_takes_neither_prepared_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _path_backed(tmp_path, monkeypatch, sound_excluded=True)
    try:
        picture = source.admit(source.asset_request("video_proxy"))
        sound = source.admit(source.asset_request("audio_preview"))
        # The clip the bodies are prepared for: enabled, on the enabled primary track.
        primary = source.admit(source.clip_request("video_proxy", "primary.v"))
        assert primary.cache_key == picture.cache_key
        primary = source.admit(source.clip_request("audio_preview", "primary.a"))
        assert primary.cache_key == sound.cache_key

        keys = []
        for clip_id in (OVERLAY_CLIP, DISABLED_CLIP):
            # Its proxy is encoded without the sound, so it is another body under another key,
            # and it has no audio preview at all.
            other = source.admit(source.request_for(clip_id, "video_proxy", f"{clip_id}.v"))
            assert other.cache_key != picture.cache_key, clip_id
            keys.append(other.cache_key)
            with pytest.raises(MediaLeaseError, match="unsupported"):
                source.admit(source.request_for(clip_id, "audio_preview", f"{clip_id}.a"))
        # One body serves both: what excludes the sound is not in the key, only that it is.
        assert keys[0] == keys[1]
    finally:
        source.release()


def test_clip_playback_takes_the_worker_from_a_preparation_that_is_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = pytest.importorskip("aiohttp.web")
    http_test = pytest.importorskip("aiohttp.test_utils")
    from test_m25_media_derivative_routes import same_origin

    from comfyui_h3_context.adapters.comfyui_authoring_media_leases import (
        _PLAYBACK_HANDOFF_SECONDS,
        LEASE_ROUTE,
        MediaLeaseRouteService,
    )

    source = _path_backed(
        tmp_path, monkeypatch, width=LONG_WIDTH, height=LONG_HEIGHT, frames=LONG_FRAMES
    )

    @dataclass(frozen=True)
    class Spawn:
        tool: str
        at: float
        process: subprocess.Popen[bytes]
        # Whether each process started before this one had exited when this one started.
        earlier_exited: tuple[bool, ...]

    spawned: list[Spawn] = []

    class Recorded(subprocess.Popen[bytes]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            argv = args[0] if args else kwargs["args"]
            exited = tuple(spawn.process.poll() is not None for spawn in spawned)
            spawned.append(Spawn(Path(argv[0]).stem, time.monotonic(), self, exited))

    # Every process the product starts from here on is the lease worker's: the source was
    # encoded and bound above.
    monkeypatch.setattr(subprocess, "Popen", Recorded)

    async def run() -> None:
        authority = _product_authority(source.admit)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        try:
            async with http_test.TestClient(http_test.TestServer(app)) as client:
                headers = same_origin(client)
                preparation = asyncio.create_task(
                    client.post(
                        LEASE_ROUTE,
                        json=source.asset_request("video_proxy").to_wire(),
                        headers=headers,
                    )
                )
                encoder: Spawn | None = None
                cutoff = time.monotonic() + 60
                while encoder is None and not preparation.done() and time.monotonic() < cutoff:
                    encoder = next((spawn for spawn in spawned if spawn.tool == "ffmpeg"), None)
                    await asyncio.sleep(0.01)
                assert encoder is not None, "the preparation started no encoder"
                assert encoder.process.poll() is None, "the encoder had ended before playback came"
                position = spawned.index(encoder)

                # The page aborts its preparation when playback needs the worker, and asks for
                # the clip's sound at once.
                preparation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await preparation
                started = len(spawned)
                asked = time.monotonic()
                admitted = await client.post(
                    LEASE_ROUTE,
                    json=source.clip_request("audio_preview", "playback").to_wire(),
                    headers=headers,
                )
                assert admitted.status == 200, await admitted.text()
                receipt = await admitted.json()
                assert receipt["requestId"] == "playback"
                assert receipt["derivativeKind"] == "audio_preview"

                # The clip was given the worker within the hand-off bound, and its own first
                # media process started only after the preparation's encoder had exited. (On
                # Windows the runner ends a process tree with a helper process of its own, which
                # is started while the encoder is still alive and is not a media process.)
                own = next(spawn for spawn in spawned[started:] if spawn.tool in MEDIA_TOOLS)
                waited = own.at - asked
                print(f"hand-off to clip playback: {waited:.3f} s")
                assert waited <= _PLAYBACK_HANDOFF_SECONDS
                assert own.earlier_exited[position]
                assert encoder.process.poll() is not None
                # The clip holds the only lease and the only body: the preparation left nothing.
                resources = authority.resources()
                assert (resources["leases"], resources["cacheEntries"]) == (1, 1)
        finally:
            service.close()

    try:
        asyncio.run(run())
    finally:
        source.release()
