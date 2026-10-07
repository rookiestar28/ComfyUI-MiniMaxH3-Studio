"""An accepted clip audio edit costs the media lease authority nothing, counted per phase.

A clip's gain, mute and fades are applied by the final render and by the preview's own gain
node; neither derivative -- the video proxy or the audio preview -- carries them, and the
derivative key holds no clip-wire member. So `set_clip_audio` must leave every derivative as it
was: no generation, no spawn, no executable hash, the same derivative fingerprints. These tests
drive the accepted modules with the counters of `test_m25_74_derivative_rework_counts`, once for
each source origin, with the audio edit where that file makes a picture edit.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from test_m25_29_production_authoring_import import (
    _authoring_action,
    _import_request,
    _insert_asset_transaction,
    _registries_with_ready_output,
)
from test_m25_74_derivative_rework_counts import (  # noqa: F401 -- `counts` is a fixture
    _AFTER_EDIT,
    _COLD_AUDIO,
    _COLD_VIDEO,
    FRAMES,
    HEIGHT,
    PATH_BACKED_ORIGIN,
    WIDTH,
    Table,
    _Counts,
    _create,
    _create_request,
    _encode_source,
    _product_authority,
    counts,
)
from test_m25_authoring_audio_rate_compatibility import _qualified_tools

from comfyui_h3_context.adapters.authoring_derivative_generator import AuthoringDerivativeGenerator
from comfyui_h3_context.adapters.authoring_derivative_source import AuthoringDerivativeClaim
from comfyui_h3_context.core.authoring_media import (
    RUNTIME_PROFILE_FINGERPRINT,
    CreateLeaseRequest,
    DerivativeKind,
    public_asset_manifest_fingerprint,
)
from comfyui_h3_context.core.composition_contract import (
    ClipAudio,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)

AUDIO_EDIT = {"gain_mb": -600, "muted": False, "fade_in_frames": 12, "fade_out_frames": 12}

GENERATED_ORIGIN: Table = {
    "cold video create": _COLD_VIDEO,
    "cold audio create": _COLD_AUDIO,
    # Two leases, each admitted by one confirmation before and one after the cache lookup.
    "creates after an audio edit": {**_AFTER_EDIT, "artifact.inspect": 4},
}


def test_generated_source_audio_edit_repeats_no_media_work(
    tmp_path: Path,
    counts: _Counts,  # noqa: F811 -- the imported fixture
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
    sequence = iter(range(1, 1000))

    def read_state() -> dict[str, Any]:
        reply = authoring.dispatch(
            _authoring_action(
                f"read.{next(sequence)}", "read_timeline_history", workspace_handle=handle
            )
        )
        assert reply.body is not None
        return cast(dict[str, Any], reply.body["authoring"])

    def apply(request_id: str, commands: list[dict[str, object]] | None = None) -> Any:
        state = read_state()
        if commands is None:
            primary = next(track for track in state["tracks"] if track["kind"] == "primary_video")
            payload = _insert_asset_transaction(
                authoring_state=state,
                asset_id=asset_id,
                track_id=primary["track_id"],
                request_id=request_id,
                duration_frames=FRAMES,
            )
        else:
            payload = _insert_asset_transaction(
                authoring_state=state, asset_id="unused", track_id="unused", request_id=request_id
            )
            payload["commands"] = commands
        action = _authoring_action(request_id, "apply_timeline_transaction")
        action["payload"] = payload
        reply = authoring.dispatch(action)
        assert reply.status == 200 and reply.body is not None, reply.body
        return decode_public_snapshot(reply.body["render_snapshot"])

    snapshot = apply("generated")
    clip_id = "clip.generated"
    # The edit below is only admitted on a clip whose source carries bound audio.
    asset = next(item for item in snapshot.assets if item.asset_id == asset_id)
    assert (asset.kind, asset.embedded_audio) == ("video", "present_bound")

    def admit(request: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        return authoring.admit_media_derivative(request, generator=generator, media_adapter=adapter)

    def request(issued: Any, kind: str, request_id: str) -> CreateLeaseRequest:
        return _create_request(issued, handle, asset_id, clip_id, kind, request_id)

    authority = _product_authority(admit)
    try:
        with counts.phase("cold video create"):
            video, _ = _create(authority, request(snapshot, "video_proxy", "v.1"))
        with counts.phase("cold audio create"):
            audio, _ = _create(authority, request(snapshot, "audio_preview", "a.1"))

        edited = apply(
            "clip-audio",
            [{"kind": "set_clip_audio", "payload": {"clip_id": clip_id, **AUDIO_EDIT}}],
        )
        assert edited.public_fingerprint != snapshot.public_fingerprint
        assert next(clip for clip in edited.clips if clip.clip_id == clip_id).audio == ClipAudio(
            -600, False, 12, 12
        )
        with counts.phase("creates after an audio edit"):
            video_again, _ = _create(authority, request(edited, "video_proxy", "v.2"))
            audio_again, _ = _create(authority, request(edited, "audio_preview", "a.2"))
        counts.require(GENERATED_ORIGIN)
        assert video_again["derivativeFingerprint"] == video["derivativeFingerprint"]
        assert audio_again["derivativeFingerprint"] == audio["derivativeFingerprint"]
    finally:
        authority.close()
    assert authority.resources() == {
        "leases": 0,
        "cacheEntries": 0,
        "cacheBytes": 0,
        "activeReads": 0,
    }


def test_path_backed_source_audio_edit_repeats_no_media_work(
    tmp_path: Path,
    counts: _Counts,  # noqa: F811 -- the imported fixture
    monkeypatch: Any,
) -> None:
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
    _encode_source(ffmpeg, root / "source.mp4")
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
        duration_probe=lambda _source: FRAMES * 1000 // 24,
    ).capture(
        exact_registry=build_reference_registry(
            (ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 1),)
        ),
        generation=1,
        sources=(("video_1", MediaKind.VIDEO, _file_video("source.mp4")),),
    )
    source_claim = claim_render_source(binding, "video_1")
    assert source_claim.asset.embedded_audio == "present_bound"

    def snapshot_with(audio: dict[str, object] | None) -> Any:
        wire = fixture_snapshot()
        wire["assets"] = [source_claim.asset.to_wire()]
        wire["tracks"] = [wire["tracks"][0]]
        clip = wire["clips"][0]
        # The clip is one frame long, so the edit is a gain and a mute without fades.
        clip.update(asset_id="video_1", duration_frames=1, source_start_frame=0)
        if audio is not None:
            clip["audio"] = audio
        wire["clips"] = [clip]
        wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
        return decode_public_snapshot(wire)

    # As in the counted test this follows: `accepted` stands for the snapshot the workspace holds,
    # and replacing it is what an accepted edit does to a claim.
    accepted = [snapshot_with(None)]
    fonts = load_packaged_font_manifest()

    def admit(request: CreateLeaseRequest) -> AuthoringDerivativeClaim:
        issued = accepted[0]
        return AuthoringDerivativeClaim(
            request,
            issued,
            PreparedAuthoringHistory(issued, binding.generation, (source_claim,), fonts),
            binding,
            lambda: accepted[0].public_fingerprint == issued.public_fingerprint,
            generator,
            adapter,
        )

    def request(kind: DerivativeKind, request_id: str) -> CreateLeaseRequest:
        snapshot = accepted[0]
        clip_id = snapshot.clips[0].clip_id
        return CreateLeaseRequest(
            request_id,
            snapshot.workspace_handle,
            snapshot.workspace_revision,
            snapshot.timeline_revision,
            snapshot.public_fingerprint,
            public_asset_manifest_fingerprint(snapshot),
            RUNTIME_PROFILE_FINGERPRINT,
            "clip",
            clip_id,
            "video_1",
            kind,
            clip_id,
            1,
            0,
            FRAMES,
        )

    authority = _product_authority(admit)
    try:
        with counts.phase("cold video create"):
            video, _ = _create(authority, request("video_proxy", "path.v.1"))
        with counts.phase("cold audio create"):
            audio, _ = _create(authority, request("audio_preview", "path.a.1"))

        accepted[0] = snapshot_with(
            {"gain_mb": -600, "muted": True, "fade_in_frames": 0, "fade_out_frames": 0}
        )
        assert accepted[0].clips[0].audio == ClipAudio(-600, True, 0, 0)
        with counts.phase("creates after an edit"):
            video_again, _ = _create(authority, request("video_proxy", "path.v.2"))
            audio_again, _ = _create(authority, request("audio_preview", "path.a.2"))
        counts.require(PATH_BACKED_ORIGIN)
        assert video_again["derivativeFingerprint"] == video["derivativeFingerprint"]
        assert audio_again["derivativeFingerprint"] == audio["derivativeFingerprint"]
    finally:
        authority.close()
        binding.release()
