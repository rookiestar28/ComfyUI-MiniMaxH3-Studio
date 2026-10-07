"""The encoder settings of the two encodes a user waits for.

A clip's proxy and an imported source's normalization each ran their encoder on one thread, and
the proxy used the encoder's default preset. Both follow one thread rule now -- the processor
count, capped at four -- and the proxy uses the `veryfast` preset. What defines a proxy's timing
and size is unchanged, the normalization keeps its preset and rate factor, the decorations keep
their single thread, and the profile fingerprint names the preset and the rule, so that a body
generated under another profile is never taken for one of this profile.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import comfyui_h3_context.adapters.authoring_derivative_generator as generator_module
import comfyui_h3_context.adapters.av_reconstruction_media as media_module
from comfyui_h3_context.adapters.authoring_derivative_generator import (
    VIDEO_PROXY_GOP,
    VIDEO_PROXY_MAX_BITS_PER_SECOND,
    VIDEO_PROXY_MAX_BYTES,
    AuthoringDerivativeGenerator,
    authoring_derivative_profile_fingerprint,
)
from comfyui_h3_context.adapters.authoring_video_facts import (
    AUTHORING_VIDEO_FACTS_SCHEMA,
    AuthoringVideoFacts,
    AuthoringVideoLandmark,
    AuthoringVideoRational,
    EmbeddedAudioFacts,
)
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.adapters.media_subprocess import (
    MediaProcessInvocation,
    ProcessCapture,
    ProcessStatus,
    SubprocessMediaRunner,
    create_output_lease,
)

FRAMES = 48
TICKS_PER_FRAME = 3_750  # 24 frames per second on the 1/90000 time base


@contextmanager
def _accepted_pin(_path: Path, _expected_sha256: str) -> Iterator[None]:
    yield


def _processors(monkeypatch: pytest.MonkeyPatch, count: int | None) -> None:
    # The rule reads the processor count through the `os` module, which is one object for
    # every importer.
    monkeypatch.setattr(os, "cpu_count", lambda: count)


def _facts() -> AuthoringVideoFacts:
    """Two seconds of 720p at 24 frames per second, without audio."""

    rows = tuple(
        AuthoringVideoLandmark(
            index, index * TICKS_PER_FRAME, index * TICKS_PER_FRAME, TICKS_PER_FRAME
        )
        for index in range(FRAMES)
    )
    return AuthoringVideoFacts(
        AUTHORING_VIDEO_FACTS_SCHEMA,
        "sha256:" + "1" * 64,
        1024,
        1280,
        720,
        "yuv420p",
        AuthoringVideoRational(1, 1),
        "tv",
        "bt709",
        "bt709",
        "bt709",
        AuthoringVideoRational(1, 90_000),
        len(rows),
        rows,
        EmbeddedAudioFacts("absent", None, None, None, None, None),
    )


def _generator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthoringDerivativeGenerator:
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")
    monkeypatch.setattr(generator_module, "_pin_exact_executable", _accepted_pin)
    return AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
    )


def _value(argv: tuple[str, ...], flag: str) -> str:
    assert argv.count(flag) == 1, (flag, argv.count(flag))
    return argv[argv.index(flag) + 1]


@pytest.mark.parametrize(
    ("processors", "threads"),
    [(None, 1), (1, 1), (2, 2), (3, 3), (4, 4), (5, 4), (32, 4)],
)
def test_the_thread_rule_is_the_processor_count_capped_at_four(
    monkeypatch: pytest.MonkeyPatch, processors: int | None, threads: int
) -> None:
    _processors(monkeypatch, processors)
    assert media_module.ENCODER_THREAD_RULE == "min_cpu_count_4"
    assert media_module.encoder_thread_count() == threads


@pytest.mark.parametrize(("processors", "threads"), [(1, "1"), (2, "2"), (32, "4")])
def test_the_proxy_encodes_with_the_fast_preset_and_the_thread_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, processors: int, threads: str
) -> None:
    _processors(monkeypatch, processors)
    generator = _generator(tmp_path, monkeypatch)
    scratch_root = generator._scratch_root
    assert scratch_root is not None
    source = create_output_lease(scratch_root, suffix=".mp4")
    output = create_output_lease(scratch_root, suffix=".mp4")
    try:
        argv = generator._proxy_invocation(
            source,
            output,
            _facts(),
            width=1280,
            height=720,
            include_audio=False,
            deadline=time.monotonic() + 5,
        ).argv
    finally:
        output.release()
        source.release()

    assert _value(argv, "-preset") == "veryfast"
    assert generator_module.VIDEO_PROXY_PRESET == "veryfast"
    assert _value(argv, "-threads") == threads
    # What defines the proxy's timing and its size is as it was.
    assert _value(argv, "-c:v") == "libx264"
    assert _value(argv, "-bf") == "0"
    assert _value(argv, "-g") == str(VIDEO_PROXY_GOP)
    # Two seconds: nine tenths of the byte ceiling would allow far more than the rate cap.
    assert 9 * VIDEO_PROXY_MAX_BYTES * 8 // (10 * 2) > VIDEO_PROXY_MAX_BITS_PER_SECOND
    assert _value(argv, "-b:v") == str(VIDEO_PROXY_MAX_BITS_PER_SECOND)
    assert _value(argv, "-maxrate") == str(VIDEO_PROXY_MAX_BITS_PER_SECOND)
    assert _value(argv, "-bufsize") == str(2 * VIDEO_PROXY_MAX_BITS_PER_SECOND)
    assert _value(argv, "-fps_mode") == "passthrough"
    assert _value(argv, "-enc_time_base") == "1:90000"
    assert _value(argv, "-video_track_timescale") == "90000"
    assert argv.count("-copyts") == 1
    graph = _value(argv, "-filter_complex")
    assert graph == "[0:v:0]scale=1280:720:flags=lanczos,format=yuv420p,setsar=1[v]"


def test_the_decorations_keep_their_single_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _processors(monkeypatch, 32)
    generator = _generator(tmp_path, monkeypatch)
    scratch_root = generator._scratch_root
    assert scratch_root is not None
    source = create_output_lease(scratch_root, suffix=".mp4")
    output = create_output_lease(scratch_root, suffix=".bin")
    try:
        thumbnail = generator._thumbnail_invocation(
            source, output, _facts(), 0, time.monotonic() + 5
        ).argv
        filmstrip = generator._filmstrip_invocation(
            source, output, _facts(), tile_count=4, deadline=time.monotonic() + 5
        ).argv
    finally:
        output.release()
        source.release()
    # A thumbnail decodes one frame and a filmstrip a handful: neither is the wait this change
    # is about, and both are in the profile as single-threaded.
    assert _value(thumbnail, "-threads") == "1"
    assert _value(filmstrip, "-threads") == "1"
    assert "-preset" not in thumbnail and "-preset" not in filmstrip


def _profile(monkeypatch: pytest.MonkeyPatch) -> tuple[str, dict[str, object]]:
    """The profile fingerprint and the payload it is the digest of."""

    seen: list[bytes] = []
    digest = generator_module._fingerprint

    def recording(body: bytes) -> str:
        seen.append(body)
        return digest(body)

    with monkeypatch.context() as patch:
        patch.setattr(generator_module, "_fingerprint", recording)
        fingerprint = authoring_derivative_profile_fingerprint()
    # The complete payload is the last body hashed; its parts are hashed before it.
    assert digest(seen[-1]) == fingerprint
    return fingerprint, json.loads(seen[-1])


def test_the_profile_fingerprint_names_the_preset_and_the_thread_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _processors(monkeypatch, 2)
    two, payload = _profile(monkeypatch)
    proxy = payload["video_proxy"]
    assert isinstance(proxy, dict)
    assert proxy["preset"] == "veryfast"
    assert proxy["encoder_threads"] == {"rule": "min_cpu_count_4", "value": 2}
    budgets = payload["budgets"]
    assert isinstance(budgets, dict) and budgets["concurrent_generators"] == 1
    # The members that define a proxy's timing and size are those of the profile before.
    assert (proxy["b_frames"], proxy["gop"], proxy["fps_mode"]) == (
        0,
        VIDEO_PROXY_GOP,
        "passthrough",
    )
    assert proxy["max_bits_per_second"] == VIDEO_PROXY_MAX_BITS_PER_SECOND
    assert proxy["max_bytes"] == VIDEO_PROXY_MAX_BYTES
    assert proxy["timing"] == "exact_source_rational_pts_dts_duration_order_count"
    thumbnail = payload["thumbnail"]
    filmstrip = payload["filmstrip"]
    assert isinstance(thumbnail, dict) and thumbnail["decoder_threads"] == 1
    assert isinstance(filmstrip, dict) and "threads_1" in str(filmstrip["encoder"])

    _processors(monkeypatch, 32)
    four, payload = _profile(monkeypatch)
    proxy = payload["video_proxy"]
    assert isinstance(proxy, dict)
    assert proxy["encoder_threads"] == {"rule": "min_cpu_count_4", "value": 4}
    # Another thread count is another recipe, so it is another profile and another cache key.
    assert two != four
    # The profile before this change recorded one thread and no preset: its payload, made from
    # this one, has another fingerprint, so no body of that profile is taken for one of this.
    before = dict(proxy, encoder_threads=1)
    del before["preset"]
    accepted = json.dumps(
        dict(payload, video_proxy=before), sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    assert generator_module._fingerprint(accepted) != four


def test_the_import_normalization_uses_the_thread_rule_and_keeps_its_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _processors(monkeypatch, 3)
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"synthetic ffmpeg")
    ffprobe.write_bytes(b"synthetic ffprobe")
    invocations: list[MediaProcessInvocation] = []

    def fake_run(
        _self: SubprocessMediaRunner,
        invocation: MediaProcessInvocation,
        cancellation: object = None,
    ) -> ProcessCapture:
        del cancellation
        invocations.append(invocation)
        (lease,) = invocation.output_leases
        lease.path.write_bytes(b"normalized")
        return ProcessCapture(status=ProcessStatus.SUCCEEDED, exit_code=0)

    monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
    monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
        clock_ms=lambda: 100,
    )
    source = (tmp_path / "generated.mp4").resolve()
    source.write_bytes(b"private generated mp4")
    output = create_output_lease(adapter._scratch_root, suffix=".mp4")
    try:
        adapter.normalize_generated_authoring_source(
            source_path=source, output=output, deadline=time.monotonic() + 30
        )
    finally:
        output.release()

    (invocation,) = invocations
    argv = invocation.argv
    assert _value(argv, "-threads") == "3"
    # The normalized file is the final render's source: its preset and rate factor stay.
    assert _value(argv, "-preset") == "medium"
    assert _value(argv, "-crf") == "18"
    assert _value(argv, "-bf") == "0"
    assert _value(argv, "-c:v") == "libx264"
