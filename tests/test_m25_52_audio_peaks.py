from __future__ import annotations

import struct
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_m25_49_filmstrip import _filmstrip_request
from test_m25_media_derivative_generator import _fingerprint, _video_facts

import comfyui_h3_context.adapters.authoring_derivative_generator as generator_module
from comfyui_h3_context.adapters.authoring_derivative_generator import (
    AUDIO_PEAKS_MEDIA_TYPE,
    AUDIO_PEAKS_PROFILE_ID,
    AUDIO_PREVIEW_MAX_BYTES,
    AUDIO_PREVIEW_MEDIA_TYPE,
    AUDIO_PREVIEW_PROFILE_ID,
    AuthoringDerivativeGenerator,
    AuthoringDerivativeGeneratorError,
    qualified_authoring_derivative_binary_capability,
)
from comfyui_h3_context.adapters.authoring_video_facts import (
    AuthoringVideoRational,
    EmbeddedAudioFacts,
)
from comfyui_h3_context.adapters.media_subprocess import (
    ProcessCapture,
    ProcessStatus,
    SubprocessMediaRunner,
)
from comfyui_h3_context.core.authoring_audio_peaks import (
    AUDIO_PEAKS_HEADER_BYTES,
    AUDIO_PEAKS_MAX_ENVELOPE_BYTES,
    AUDIO_PEAKS_MAX_PAIRS,
    AUDIO_PEAKS_MAX_PCM_BYTES,
    AudioPeaksError,
    decode_audio_peaks_envelope,
    encode_audio_peaks_envelope,
)
from comfyui_h3_context.core.authoring_media import (
    MediaLeaseError,
    derivative_byte_limit,
    derivative_media_type,
    public_asset_manifest_fingerprint,
    validate_lease_snapshot,
)


def _pcm(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def test_audio_peaks_envelope_is_versioned_bounded_and_deterministic() -> None:
    body = encode_audio_peaks_envelope(_pcm(*([-32_768, -1, 0, 32_767] + [0] * 76), *([256] * 80)))

    decoded = decode_audio_peaks_envelope(body)

    assert body[:4] == b"H3AP"
    assert len(body) == AUDIO_PEAKS_HEADER_BYTES + 4
    assert decoded.version == 1
    assert decoded.pair_rate == 100
    assert decoded.sample_rate == 8_000
    assert decoded.sample_count == 160
    assert decoded.pairs == ((-128, 127), (1, 1))


def test_audio_peaks_envelope_accepts_silence_and_final_partial_bucket() -> None:
    one = decode_audio_peaks_envelope(encode_audio_peaks_envelope(_pcm(0)))
    decoded = decode_audio_peaks_envelope(encode_audio_peaks_envelope(_pcm(*([0] * 81))))

    assert one.sample_count == 1
    assert one.pairs == ((0, 0),)
    assert decoded.sample_count == 81
    assert decoded.pairs == ((0, 0), (0, 0))


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("empty", "invalid_request"),
        ("odd", "invalid_request"),
        ("overflow", "resource_limit"),
    ],
)
def test_audio_peaks_pcm_reader_fails_closed(case: str, code: str) -> None:
    body = {
        "empty": b"",
        "odd": b"\x00",
        "overflow": b"\x00" * (AUDIO_PEAKS_MAX_PCM_BYTES + 1),
    }[case]
    with pytest.raises(AudioPeaksError, match=code):
        encode_audio_peaks_envelope(body)


def test_audio_peaks_decoder_rejects_truncation_inverted_pairs_and_wrong_counts() -> None:
    body = bytearray(encode_audio_peaks_envelope(_pcm(*([0] * 80))))
    cases = [
        body[:-1],
        body + b"\x00",
        bytearray(body),
        bytearray(body),
    ]
    cases[2][16:20] = (2).to_bytes(4, "little")
    cases[3][-2:] = bytes((1, 255))

    for case in cases:
        with pytest.raises(AudioPeaksError, match="invalid_request"):
            decode_audio_peaks_envelope(case)


def test_audio_peaks_envelope_structural_max_stays_below_transport_ceiling() -> None:
    body = encode_audio_peaks_envelope(bytes(AUDIO_PEAKS_MAX_PCM_BYTES))
    decoded = decode_audio_peaks_envelope(body)

    assert AUDIO_PEAKS_MAX_PAIRS == 16_384
    assert AUDIO_PEAKS_MAX_ENVELOPE_BYTES == 32_788
    assert AUDIO_PEAKS_MAX_ENVELOPE_BYTES <= 64 * 1024
    assert len(body) == AUDIO_PEAKS_MAX_ENVELOPE_BYTES
    assert decoded.sample_count == AUDIO_PEAKS_MAX_PCM_BYTES // 2
    assert len(decoded.pairs) == AUDIO_PEAKS_MAX_PAIRS


def test_audio_peaks_contract_requires_present_bound_video_asset_scope_and_whole_span() -> None:
    request, snapshot = _filmstrip_request()
    video = next(asset for asset in snapshot.assets if asset.kind == "video")
    video = replace(video, embedded_audio="present_bound", source_sample_count=48_000)
    snapshot = replace(
        snapshot,
        assets=tuple(
            video if asset.asset_id == video.asset_id else asset for asset in snapshot.assets
        ),
    )
    request = replace(
        request,
        derivative_kind="audio_peaks",
        manifest_fingerprint=public_asset_manifest_fingerprint(snapshot),
    )

    assert validate_lease_snapshot(request, snapshot) == video
    assert derivative_media_type("audio_peaks") == "application/octet-stream"
    assert derivative_byte_limit("audio_peaks") == 64 * 1024

    absent = replace(
        snapshot,
        assets=tuple(
            replace(video, embedded_audio="absent", source_sample_count=None)
            if asset.asset_id == video.asset_id
            else asset
            for asset in snapshot.assets
        ),
    )
    with pytest.raises(MediaLeaseError, match="unsupported"):
        validate_lease_snapshot(
            replace(request, manifest_fingerprint=public_asset_manifest_fingerprint(absent)), absent
        )
    with pytest.raises(MediaLeaseError, match="authority_mismatch"):
        validate_lease_snapshot(replace(request, source_end_frame=1), snapshot)


class _PcmRunner:
    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm
        self.argv: tuple[str, ...] = ()
        self.maximum = 0

    def run(self, invocation, cancellation=None):  # type: ignore[no-untyped-def]
        assert cancellation is None
        self.argv = invocation.argv
        self.maximum = invocation.max_stdout_bytes
        return ProcessCapture(status=ProcessStatus.SUCCEEDED, stdout=self.pcm)


class _StatusRunner:
    def __init__(self, status: ProcessStatus) -> None:
        self.status = status

    def run(self, invocation, cancellation=None):  # type: ignore[no-untyped-def]
        assert cancellation is None
        return ProcessCapture(status=self.status)


def test_audio_preview_preserves_channels_and_emits_one_canonical_pcm_wav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_body = b"bounded-private-video-with-stereo-audio"
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(source_body)
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    @contextmanager
    def pin(_path: Path, _fingerprint: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(generator_module, "_pin_exact_executable", pin)
    facts = _video_facts(
        source_body,
        audio=EmbeddedAudioFacts(
            "present_bound",
            48_000,
            2,
            "stereo",
            AuthoringVideoRational(1, 48_000),
            4,
        ),
    )
    pcm = _pcm(-32_768, 32_767, -1, 1, 2, -2, 3, -3)
    runner = _PcmRunner(pcm)
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
    )
    generator._runner = cast(SubprocessMediaRunner, runner)

    result = generator.generate_video_audio_preview(
        source,
        facts,
        expected_source_fingerprint=_fingerprint(source_body),
        deadline=time.monotonic() + 5.0,
    )

    assert result.profile_id == AUDIO_PREVIEW_PROFILE_ID
    assert result.media_type == AUDIO_PREVIEW_MEDIA_TYPE
    assert result.audio_disposition == "present_bound"
    assert bytes(result.body[:4]) == b"RIFF"
    assert bytes(result.body[8:16]) == b"WAVEfmt "
    assert struct.unpack("<HHIIHH", result.body[20:36]) == (1, 2, 48_000, 192_000, 4, 16)
    assert bytes(result.body[36:40]) == b"data"
    assert struct.unpack("<I", result.body[40:44]) == (len(pcm),)
    assert bytes(result.body[44:]) == pcm
    assert runner.maximum == AUDIO_PREVIEW_MAX_BYTES - 44
    assert "-ac" not in runner.argv
    assert runner.argv[runner.argv.index("-ar") + 1] == "48000"
    assert runner.argv[-1] == "pipe:1"
    result.clear()


def test_audio_preview_refuses_encoded_overflow_before_native_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_body = b"bounded-private-video-with-long-audio"
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(source_body)
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    @contextmanager
    def pin(_path: Path, _fingerprint: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(generator_module, "_pin_exact_executable", pin)
    frames = (AUDIO_PREVIEW_MAX_BYTES - 44) // (8 * 2) + 1
    facts = _video_facts(
        source_body,
        audio=EmbeddedAudioFacts(
            "present_bound",
            48_000,
            8,
            "7.1",
            AuthoringVideoRational(1, 48_000),
            frames,
        ),
    )
    runner = _PcmRunner(b"should-not-run")
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
    )
    generator._runner = cast(SubprocessMediaRunner, runner)

    with pytest.raises(AuthoringDerivativeGeneratorError, match="resource_limit"):
        generator.generate_video_audio_preview(
            source,
            facts,
            expected_source_fingerprint=_fingerprint(source_body),
            deadline=time.monotonic() + 5.0,
        )
    assert runner.argv == ()


def test_generator_decodes_one_bounded_pcm_pipe_and_emits_no_visual_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_body = b"bounded-private-video-with-audio"
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(source_body)
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    @contextmanager
    def pin(_path: Path, _fingerprint: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(generator_module, "_pin_exact_executable", pin)
    facts = _video_facts(
        source_body,
        audio=EmbeddedAudioFacts(
            "present_bound",
            48_000,
            1,
            "mono",
            AuthoringVideoRational(1, 48_000),
            480,
        ),
    )
    runner = _PcmRunner(_pcm(*([-32_768, 32_767] + [0] * 78)))
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
    )
    generator._runner = cast(SubprocessMediaRunner, runner)

    result = generator.generate_video_audio_peaks(
        source,
        facts,
        expected_source_fingerprint=_fingerprint(source_body),
        deadline=time.monotonic() + 5.0,
    )

    assert result.profile_id == AUDIO_PEAKS_PROFILE_ID
    assert result.media_type == AUDIO_PEAKS_MEDIA_TYPE
    assert result.audio_disposition == "present_bound"
    assert decode_audio_peaks_envelope(result.body).pairs == ((-128, 127),)
    assert runner.maximum == AUDIO_PEAKS_MAX_PCM_BYTES
    assert runner.argv[-1] == "pipe:1"
    assert runner.argv[runner.argv.index("-t") + 1] == "0.01"
    assert runner.argv[runner.argv.index("-ac") + 1] == "1"
    assert runner.argv[runner.argv.index("-ar") + 1] == "8000"
    capability = qualified_authoring_derivative_binary_capability()
    assert "pipe" in capability.protocols
    assert "s16le" in capability.output_muxers
    assert "pcm_s16le" in capability.output_encoders
    assert not any((tmp_path / "scratch").glob("h3-media-*"))
    result.clear()

    absent = replace(
        facts, embedded_audio=EmbeddedAudioFacts("absent", None, None, None, None, None)
    )
    with pytest.raises(AuthoringDerivativeGeneratorError, match="unsupported"):
        generator.generate_video_audio_peaks(
            source,
            absent,
            expected_source_fingerprint=_fingerprint(source_body),
            deadline=time.monotonic() + 5.0,
        )


def test_generator_refuses_audio_beyond_envelope_before_native_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_body = b"bounded-private-video-with-long-audio"
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(source_body)
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    @contextmanager
    def pin(_path: Path, _fingerprint: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(generator_module, "_pin_exact_executable", pin)
    facts = _video_facts(
        source_body,
        audio=EmbeddedAudioFacts(
            "present_bound",
            48_000,
            1,
            "mono",
            AuthoringVideoRational(1, 48_000),
            (AUDIO_PEAKS_MAX_PCM_BYTES // 2 + 1) * 6,
        ),
    )
    runner = _PcmRunner(b"should-not-run")
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
    )
    generator._runner = cast(SubprocessMediaRunner, runner)

    with pytest.raises(AuthoringDerivativeGeneratorError, match="resource_limit"):
        generator.generate_video_audio_peaks(
            source,
            facts,
            expected_source_fingerprint=_fingerprint(source_body),
            deadline=time.monotonic() + 5.0,
        )
    assert runner.argv == ()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (ProcessStatus.CANCELLED, "cancelled"),
        (ProcessStatus.TIMED_OUT, "timeout"),
        (ProcessStatus.OUTPUT_LIMIT, "resource_limit"),
    ],
)
def test_generator_preserves_pcm_process_disposition_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: ProcessStatus,
    code: str,
) -> None:
    source_body = b"bounded-private-video-with-audio"
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(source_body)
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    @contextmanager
    def pin(_path: Path, _fingerprint: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(generator_module, "_pin_exact_executable", pin)
    facts = _video_facts(
        source_body,
        audio=EmbeddedAudioFacts(
            "present_bound",
            48_000,
            1,
            "mono",
            AuthoringVideoRational(1, 48_000),
            480,
        ),
    )
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(tmp_path / "scratch").resolve(),
    )
    generator._runner = cast(SubprocessMediaRunner, _StatusRunner(status))

    with pytest.raises(AuthoringDerivativeGeneratorError, match=code):
        generator.generate_video_audio_peaks(
            source,
            facts,
            expected_source_fingerprint=_fingerprint(source_body),
            deadline=time.monotonic() + 5.0,
        )
    assert not any((tmp_path / "scratch").glob("h3-media-*"))
