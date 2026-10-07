"""Native audio facts and canonical Authoring coverage remain distinct domains."""

from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_m25_authoring_video_facts import _FactsAdapter, _probe_wire
from test_m25_render_source_currentness import _video_source

from comfyui_h3_context.adapters import authoring_video_facts as facts_module
from comfyui_h3_context.adapters.authoring_generated_source import GeneratedAuthoringVideoSource
from comfyui_h3_context.adapters.authoring_render_source import claim_render_source
from comfyui_h3_context.adapters.authoring_video_facts import (
    AuthoringVideoFactsError,
    AuthoringVideoRational,
    EmbeddedAudioFacts,
    probe_authoring_video_facts,
)
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.core.av_reconstruction import qualified_av_limits


def _audio_wire(rate: int, *, end: int = 513, audio: bool = True) -> bytes:
    wire = json.loads(_probe_wire(audio=audio))
    if audio:
        wire["streams"][1].update(sample_rate=str(rate), time_base=f"1/{rate}")
        wire["frames"][-1]["duration"] = end
    return json.dumps(wire).encode()


@pytest.mark.parametrize("rate,expected", [(32_000, 2305), (48_000, 1537), (None, None)])
def test_native_effective_samples_map_to_both_public_asset_families(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rate: int | None, expected: int | None
) -> None:
    body = b"private-video"
    path, receipt, _source, _verification = _video_source(tmp_path, body)
    adapter = _FactsAdapter(_audio_wire(rate or 48_000, audio=rate is not None))
    facts = probe_authoring_video_facts(path.resolve(), adapter, time.monotonic() + 5)
    assert facts.embedded_audio.sample_rate == rate
    # Effective native coverage excludes the decoded AAC tail.
    assert facts.embedded_audio.sample_count == (1537 if rate is not None else None)
    assert facts.embedded_audio.canonical_sample_count == expected
    # Constructor-only oracle: these placeholders never acquire or claim artifact authority.
    generated = GeneratedAuthoringVideoSource(
        cast(Any, None), facts, cast(Any, None), cast(Any, None), lambda: True
    )
    assert generated.public_asset("video_1").source_sample_count == expected
    real_type_adapter = object.__new__(QualifiedAVMediaAdapter)
    monkeypatch.setattr(
        real_type_adapter, "probe_authoring_video_source", adapter.probe_authoring_video_source
    )
    monkeypatch.setattr(
        "comfyui_h3_context.adapters.comfyui_authoring_media_preview.current_authoring_media_preview_adapter",
        lambda: real_type_adapter,
    )
    try:
        claim = claim_render_source(receipt, "video_1")
        assert claim.asset.source_sample_count == expected
        assert claim.current()
        receipt.release()
        assert not claim.current()
    finally:
        receipt.release()


@pytest.mark.parametrize("rate", [8_000, 44_100, 96_000])
def test_other_native_rates_still_fail_closed(tmp_path: Path, rate: int) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")
    with pytest.raises(AuthoringVideoFactsError, match="video_facts_unsupported"):
        probe_authoring_video_facts(source, _FactsAdapter(_audio_wire(rate)), time.monotonic() + 5)
    with pytest.raises(AuthoringVideoFactsError, match="video_facts_invalid"):
        EmbeddedAudioFacts("present_bound", rate, 1, "mono", AuthoringVideoRational(1, rate), 1537)


def test_native_and_canonical_sample_caps_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = qualified_av_limits()
    for rate in (32_000, 48_000):
        cap = limits.max_audio_sample_frames * rate // 48_000
        facts = EmbeddedAudioFacts(
            "present_bound", rate, 2, "stereo", AuthoringVideoRational(1, rate), cap
        )
        assert facts.canonical_sample_count == limits.max_audio_sample_frames
        with pytest.raises(AuthoringVideoFactsError, match="video_facts_invalid"):
            replace(facts, sample_count=cap + 1)
    monkeypatch.setattr(
        facts_module, "qualified_av_limits", lambda: replace(limits, max_audio_sample_frames=2000)
    )
    with pytest.raises(AuthoringVideoFactsError):
        facts_module._parse_audio(
            json.loads(_audio_wire(32_000))["streams"][1],
            json.loads(_audio_wire(32_000))["frames"][-2:],
        )


def test_missing_pixel_aspect_is_distinct_in_both_source_profile_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, receipt, _source, _verification = _video_source(tmp_path, b"private-video")
    generated_profiles = []
    path_profiles = []
    try:
        for omitted in (False, True):
            wire = json.loads(_audio_wire(32_000))
            if omitted:
                del wire["streams"][0]["sample_aspect_ratio"]
            adapter = _FactsAdapter(json.dumps(wire).encode())
            facts = probe_authoring_video_facts(_path.resolve(), adapter, time.monotonic() + 5)
            source = GeneratedAuthoringVideoSource(
                cast(Any, None), facts, cast(Any, None), cast(Any, None), lambda: True
            )
            generated_profiles.append(source.source_profile_fingerprint("video_1"))
            native_adapter = object.__new__(QualifiedAVMediaAdapter)
            monkeypatch.setattr(
                native_adapter, "probe_authoring_video_source", adapter.probe_authoring_video_source
            )

            def current_adapter(
                bound_adapter: QualifiedAVMediaAdapter = native_adapter,
            ) -> QualifiedAVMediaAdapter:
                return bound_adapter

            monkeypatch.setattr(
                "comfyui_h3_context.adapters.comfyui_authoring_media_preview.current_authoring_media_preview_adapter",
                current_adapter,
            )
            claim = claim_render_source(receipt, "video_1")
            path_profiles.append(claim.source_profile_fingerprint)
            assert claim.current()
            assert source.public_asset("video_1") == claim.asset
            assert (facts.pixel_aspect_ratio is None) is omitted
        assert generated_profiles[0] != generated_profiles[1]
        assert path_profiles[0] != path_profiles[1]
    finally:
        receipt.release()


def test_absent_audio_has_no_canonical_coverage() -> None:
    assert EmbeddedAudioFacts("absent", None, None, None, None, None).canonical_sample_count is None


@pytest.mark.parametrize("defect", ["pts", "nonfinal_padding", "excess_padding"])
def test_native_rate_admission_preserves_timestamp_and_padding_guards(
    tmp_path: Path, defect: str
) -> None:
    wire = json.loads(_audio_wire(32_000))
    if defect == "pts":
        wire["frames"][-1]["pts"] += 1
    elif defect == "nonfinal_padding":
        wire["frames"][-2]["nb_samples"] += 1
    else:
        wire["frames"][-1]["nb_samples"] += 1024
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")
    with pytest.raises(AuthoringVideoFactsError, match="video_facts_unsupported"):
        probe_authoring_video_facts(
            source, _FactsAdapter(json.dumps(wire).encode()), time.monotonic() + 5
        )


def _qualified_tools(tmp_path: Path) -> tuple[Path, Path, QualifiedAVMediaAdapter]:
    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg, ffprobe = (
        Path(ffmpeg_value).resolve(strict=True),
        Path(ffprobe_value).resolve(strict=True),
    )
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "qualified-scratch").resolve(),
        clock_ms=lambda: 1,
    )
    return ffmpeg, ffprobe, adapter


def _ffmpeg(ffmpeg: Path, *arguments: str) -> bytes:
    return subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    ).stdout


@pytest.mark.parametrize("rate", [32_000, 48_000])
@pytest.mark.parametrize("count", [1024, 1025, 32001])
def test_actual_resampler_does_not_grant_rounded_tail_coverage(
    tmp_path: Path, rate: int, count: int
) -> None:
    ffmpeg, _ffprobe, _adapter = _qualified_tools(tmp_path)
    pcm = _ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc=sin(2*PI*337*t):s={rate}",
        "-af",
        f"atrim=end_sample={count},asetpts=N/SR/TB,aresample=48000",
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    )
    audio = EmbeddedAudioFacts(
        "present_bound", rate, 1, "mono", AuthoringVideoRational(1, rate), count
    )
    actual_samples = len(pcm) // 4
    assert actual_samples == (count * 48_000 + rate - 1) // rate
    assert audio.canonical_sample_count == count * 48_000 // rate
    assert actual_samples - audio.canonical_sample_count == int(count * 48_000 % rate != 0)


def _tone_power(samples: tuple[float, ...], frequency: int) -> float:
    real = sum(
        value * math.cos(2 * math.pi * frequency * index / 48_000)
        for index, value in enumerate(samples)
    )
    imaginary = sum(
        value * math.sin(2 * math.pi * frequency * index / 48_000)
        for index, value in enumerate(samples)
    )
    return (real * real + imaginary * imaginary) / len(samples) ** 2


@pytest.mark.skipif(sys.platform != "win32", reason="actual renderer uses the Windows job boundary")
@pytest.mark.parametrize("rate", [32_000, 48_000])
@pytest.mark.parametrize("unspecified_aspect", [False, True])
def test_real_original_import_renders_the_asymmetric_nonzero_audio_window(
    tmp_path: Path, rate: int, unspecified_aspect: bool
) -> None:
    from test_m25_29_production_authoring_import import (
        _authoring_action,
        _import_request,
        _place_imported_asset_for_render,
        _registries_with_ready_output,
    )

    from comfyui_h3_context.adapters.authoring_derivative_generator import (
        AuthoringDerivativeGenerator,
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
    from comfyui_h3_context.core.composition_contract import (
        decode_public_snapshot,
        public_snapshot_fingerprint,
    )

    ffmpeg, ffprobe, adapter = _qualified_tools(tmp_path)
    encoded = (tmp_path / "original.mp4").resolve()
    # Different tones before, within both halves, and after the chosen [2, 3) seconds.
    frequency = "if(lt(t,2),337,if(lt(t,2.5),701,if(lt(t,3),1103,1601)))"
    _ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=24:duration=4",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc='0.3*sin(2*PI*({frequency})*t)|0.1*sin(2*PI*({frequency})*t)':s={rate}:d=4",
        "-c:v",
        "libx264",
        "-bf",
        "0",
        "-vf",
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        + (",setsar=0" if unspecified_aspect else ""),
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
        str(rate),
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-y",
        str(encoded),
    )
    body = encoded.read_bytes()
    native_facts = probe_authoring_video_facts(encoded, adapter, time.monotonic() + 30)
    assert native_facts.embedded_audio.sample_rate == rate
    assert (native_facts.pixel_aspect_ratio is None) is unspecified_aspect
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "proxy-scratch").resolve(),
    )
    proxy = generator.generate_video_proxy(
        encoded,
        native_facts,
        expected_source_fingerprint=native_facts.content_fingerprint,
        include_embedded_audio=True,
        deadline=time.monotonic() + 30,
    )
    try:
        proxy_path = (tmp_path / "proxy.mp4").resolve()
        proxy_path.write_bytes(proxy.body)
        proxy_facts = probe_authoring_video_facts(proxy_path, adapter, time.monotonic() + 30)
        assert proxy_facts.pixel_aspect_ratio == AuthoringVideoRational(1, 1)
        assert proxy_facts.frame_count == native_facts.frame_count == 96
        assert proxy_facts.embedded_audio == native_facts.embedded_audio
        assert encoded.read_bytes() == body
        assert (native_facts.pixel_aspect_ratio is None) is unspecified_aspect
    finally:
        proxy.clear()
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path,
        artifact_bodies=(body,),
        frame_count=96,
    )
    request = _import_request(projection, before, history)
    response = authoring.import_production_outputs(
        request,
        production_registry=production,
        media_adapter=adapter,
        deadline=time.monotonic() + 30,
    )
    asset_id = response.receipt.rows[0].asset_id
    imported = authoring._entries[request.authoring_workspace_handle].imported_outputs
    source = next(iter(imported.values())).source
    assert source.facts.embedded_audio.sample_rate == rate
    assert source.facts.embedded_audio.sample_count == 4 * rate
    assert source.public_asset(asset_id).source_sample_count == 192_000
    # IMPORTANT: an asset-only V2 workspace has no render snapshot. Place the imported source
    # through the real transaction contract before deriving this test's custom render window.
    _place_imported_asset_for_render(
        authoring,
        workspace_handle=request.authoring_workspace_handle,
        asset_id=asset_id,
        request_id=f"audio-rate-{rate}-{int(unspecified_aspect)}",
    )
    original = authoring.prepare_render_plan(request.authoring_workspace_handle)
    snapshot = original._history.snapshot
    assert snapshot is not None
    wire = cast(dict[str, Any], snapshot.to_wire())
    wire["output"].update(width=64, height=64, duration_frames=24)
    wire["tracks"] = [
        dict(track_id="primary", kind="primary_video", order=0, enabled=True, locked=False)
    ]
    fixture = Path(__file__).parent / "fixtures/m25_10_composition_contract_v1.json"
    clip = json.loads(fixture.read_text(encoding="utf-8"))["snapshot"]["clips"][0]
    clip.update(
        clip_id="original-window",
        asset_id=asset_id,
        track_id="primary",
        start_frame=0,
        duration_frames=24,
        source_start_frame=48,
    )
    wire["clips"] = [clip]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    bound = prepare_bound_render_plan(original._history, decode_public_snapshot(wire), lambda: True)
    sources = acquire_render_job_sources(bound, deadline=time.monotonic() + 30)
    output_root = tmp_path / "renders"
    output_root.mkdir()
    render_store = RenderOutputStore(output_root)
    control = SimpleNamespace(deadline=time.monotonic() + 30, is_cancelled=lambda: False)
    capability = qualified_ffmpeg_capability()
    try:
        assert sources.read_source(asset_id) == body
        stage = render_store.begin("render-" + "e" * 32, "sha256:" + "e" * 64)
        prepared = prepare_render_assets(
            plan=bound.plan, sources=sources, stage=stage, control=control
        )
        # This executes the actual pinned encoder and verifier, not a fabricated qualification
        # certificate or public service admission; full qualification remains a separate sweep.
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
            assert measured.frame_count == 24
            assert measured.audio_effective_samples == 48_000
            assert session.active_processes == 0
        pcm = _ffmpeg(
            ffmpeg,
            "-i",
            str(stage.output_path),
            "-vn",
            "-af",
            "atrim=end_sample=48000",
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "pipe:1",
        )
        assert len(pcm) == 48_000 * 4
        samples = struct.unpack("<48000f", pcm)
        for window, expected in ((samples[4800:19200], 701), (samples[28800:43200], 1103)):
            powers = {tone: _tone_power(window, tone) for tone in (337, 701, 1103, 1601)}
            assert powers[expected] > 0.001
            assert powers[expected] > 20 * max(
                power for tone, power in powers.items() if tone != expected
            )
        assert sources.read_source(asset_id) == body
    finally:
        sources.release()
        render_store.close()
        authoring.dispatch(
            _authoring_action(
                "authoring.release.audio",
                "release_workspace",
                workspace_handle=request.authoring_workspace_handle,
            )
        )
    assert source.lease.released
    assert list((tmp_path / "qualified-scratch").iterdir()) == []
