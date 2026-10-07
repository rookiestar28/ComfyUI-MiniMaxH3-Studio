"""The final render's program applies a clip's audio adjustments at the definition's level.

`tests/test_m25_77_clip_audio_render.py` holds the filters to the definition through the per-sample
gains the pinned build's filters were measured to apply. Here the whole product program runs on the
pinned pair: a real encoded source with a steady tone is imported and placed, and one clip of it is
rendered through `run_prepared_render` without adjustments and with each member below. In windows
of the decoded outputs, the adjusted render's level divided by the unadjusted one's is the
definition's level over the same window. Skipped unless the exact media tools are supplied.
"""

from __future__ import annotations

import json
import math
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_m25_authoring_audio_rate_compatibility import _ffmpeg, _qualified_tools

from comfyui_h3_context.core.clip_audio import SAMPLES_PER_FRAME, clip_audio_factor
from comfyui_h3_context.core.composition_contract import (
    IDENTITY_CLIP_AUDIO,
    ClipAudio,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)

FRAMES = 48
LENGTH = FRAMES * SAMPLES_PER_FRAME
WINDOW = 960
# The source tone's level, on one channel as in the corpus's sources. The ratios do not depend on
# it while the loudest member stays below full scale: +12 dB gives 0.80. (Two equal channels would
# be summed to 1.41 times the level by the program's mono downmix, and +12 dB would clip.)
TONE = 0.2

# Measured on the pinned pair, twice with the same values: the largest error of a window is 1.8 % of
# its level (two different AAC encodes, quantised over 960 samples). A window passes within 3 % of
# its level, or within 0.001 where the level is near zero. A fade one frame too long or too short
# moves the level at a quarter of a 12-frame fade by 7.7 %.
RELATIVE_BOUND = 0.03
FLOOR = 0.001

MEMBERS = {
    "gain_down": ClipAudio(-600, False, 0, 0),
    "gain_up": ClipAudio(1200, False, 0, 0),
    "fades": ClipAudio(0, False, 12, 12),
    "combination": ClipAudio(-600, False, 12, 12),
    "muted": ClipAudio(0, True, 12, 12),
}


def _windows(audio: ClipAudio) -> list[int]:
    """Window starts: 1/4, 1/2 and 3/4 of each fade and the steady middle, centred."""

    centres = [LENGTH // 2]
    fade_in = audio.fade_in_frames * SAMPLES_PER_FRAME
    fade_out = audio.fade_out_frames * SAMPLES_PER_FRAME
    centres += [fade_in * share // 4 for share in (1, 2, 3)] if fade_in else []
    centres += (
        [LENGTH - fade_out + fade_out * share // 4 for share in (1, 2, 3)] if fade_out else []
    )
    return sorted(centre - WINDOW // 2 for centre in centres)


def _level(samples: tuple[float, ...], start: int) -> float:
    window = samples[start : start + WINDOW]
    return math.sqrt(sum(value * value for value in window) / WINDOW)


def _expected(audio: ClipAudio, start: int) -> float:
    factors = (clip_audio_factor(audio, FRAMES, k) for k in range(start, start + WINDOW))
    return math.sqrt(sum(factor * factor for factor in factors) / WINDOW)


def _renders(tmp_path: Path) -> dict[str, tuple[float, ...]]:
    """The clip rendered by the product program once per member, decoded to mono samples."""

    from test_m25_29_production_authoring_import import (
        _authoring_action,
        _import_request,
        _place_imported_asset_for_render,
        _registries_with_ready_output,
    )

    from comfyui_h3_context.adapters.authoring_render_executor import (
        prepare_render_assets,
        run_prepared_render,
    )
    from comfyui_h3_context.adapters.authoring_render_leases import acquire_render_job_sources
    from comfyui_h3_context.adapters.authoring_render_probe import measure_render_output
    from comfyui_h3_context.adapters.authoring_render_process import (
        WindowsRenderProcessSession,
        pin_render_executable,
    )
    from comfyui_h3_context.adapters.authoring_render_source import prepare_bound_render_plan
    from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore
    from comfyui_h3_context.core.av_reconstruction import qualified_ffmpeg_capability

    ffmpeg, ffprobe, adapter = _qualified_tools(tmp_path)
    encoded = (tmp_path / "original.mp4").resolve()
    _ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=24:duration=4",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc='{TONE}*sin(2*PI*1000*t)':s=48000:d=4",
        "-c:v",
        "libx264",
        "-bf",
        "0",
        "-vf",
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-movflags",
        "+faststart",
        "-y",
        str(encoded),
    )
    body = encoded.read_bytes()
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path, artifact_bodies=(body,), frame_count=96
    )
    request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 30,
    )
    asset_id = response.receipt.rows[0].asset_id
    _place_imported_asset_for_render(
        authoring,
        workspace_handle=request.authoring_workspace_handle,
        asset_id=asset_id,
        request_id="clip-audio-amplitude",
    )
    original = authoring.prepare_render_plan(request.authoring_workspace_handle)
    snapshot = original._history.snapshot
    assert snapshot is not None
    fixture = Path(__file__).parent / "fixtures/m25_10_composition_contract_v1.json"
    template = json.loads(fixture.read_text(encoding="utf-8"))["snapshot"]["clips"][0]
    output_root = tmp_path / "renders"
    output_root.mkdir()
    render_store = RenderOutputStore(output_root)
    capability = qualified_ffmpeg_capability()
    decoded: dict[str, tuple[float, ...]] = {}
    try:
        for index, (name, audio) in enumerate({"identity": IDENTITY_CLIP_AUDIO, **MEMBERS}.items()):
            wire = cast(dict[str, Any], snapshot.to_wire())
            wire["output"].update(width=64, height=64, duration_frames=FRAMES)
            wire["tracks"] = [
                dict(track_id="primary", kind="primary_video", order=0, enabled=True, locked=False)
            ]
            clip = dict(template)
            # One second into the source, where its tone is steady.
            clip.update(
                clip_id="tone-window",
                asset_id=asset_id,
                track_id="primary",
                start_frame=0,
                duration_frames=FRAMES,
                source_start_frame=24,
            )
            if audio != IDENTITY_CLIP_AUDIO:
                clip["audio"] = audio.to_wire()
            wire["clips"] = [clip]
            wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
            bound = prepare_bound_render_plan(
                original._history, decode_public_snapshot(wire), lambda: True
            )
            assert len(bound.plan.clip_audio) == (audio != IDENTITY_CLIP_AUDIO)
            sources = acquire_render_job_sources(bound, deadline=time.monotonic() + 30)
            control = SimpleNamespace(deadline=time.monotonic() + 60, is_cancelled=lambda: False)
            stage = None
            try:
                stage = render_store.begin(
                    "render-" + f"{index:x}" * 32, "sha256:" + f"{index:x}" * 64
                )
                prepared = prepare_render_assets(
                    plan=bound.plan, sources=sources, stage=stage, control=control
                )
                with (
                    pin_render_executable(
                        ffmpeg, "sha256:" + capability.ffmpeg_sha256.lower(), control=control
                    ) as renderer,
                    pin_render_executable(
                        ffprobe, "sha256:" + capability.ffprobe_sha256.lower(), control=control
                    ) as probe,
                    WindowsRenderProcessSession() as session,
                ):
                    run_prepared_render(
                        plan=bound.plan,
                        prepared=prepared,
                        renderer=renderer,
                        session=session,
                        control=control,
                    )
                    measured = measure_render_output(
                        path=stage.output_path,
                        probe=probe,
                        session=session,
                        control=control,
                        check_staging=stage.check_budget,
                    )
                    assert measured.frame_count == FRAMES
                    assert measured.audio_effective_samples == LENGTH
                    assert session.active_processes == 0
                pcm = _ffmpeg(
                    ffmpeg,
                    "-i",
                    str(stage.output_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-af",
                    f"atrim=end_sample={LENGTH}",
                    "-c:a",
                    "pcm_f32le",
                    "-f",
                    "f32le",
                    "pipe:1",
                )
                assert len(pcm) == LENGTH * 4
                decoded[name] = struct.unpack(f"<{LENGTH}f", pcm)
            finally:
                # The store holds a bounded number of stages; each render's is discarded once its
                # output is decoded.
                if stage is not None:
                    render_store.discard(stage)
                sources.release()
    finally:
        render_store.close()
        authoring.dispatch(
            _authoring_action(
                "authoring.release.audio",
                "release_workspace",
                workspace_handle=request.authoring_workspace_handle,
            )
        )
    assert list((tmp_path / "qualified-scratch").iterdir()) == []
    return decoded


@pytest.mark.skipif(sys.platform != "win32", reason="actual renderer uses the Windows job boundary")
def test_the_rendered_level_of_every_window_is_the_definition_level(tmp_path: Path) -> None:
    decoded = _renders(tmp_path)
    identity = decoded["identity"]
    rows = []
    for name, audio in MEMBERS.items():
        for start in _windows(audio):
            expected = _expected(audio, start)
            measured = _level(decoded[name], start) / _level(identity, start)
            rows.append((name, start, expected, measured, measured - expected))
    report = "\n".join(
        f"{name} @{start}: expected {expected:.6f} measured {measured:.6f} error {error:+.6f}"
        for name, start, expected, measured, error in rows
    )
    assert all(
        abs(error) <= max(FLOOR, RELATIVE_BOUND * expected) for _, _, expected, _, error in rows
    ), report
    # `volume=0` alone silences a muted clip: every decoded sample is zero.
    assert not any(decoded["muted"])
