"""M17-11 exact-capability media inspection adapter tests."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
from array import array
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest
from test_av_reconstruction import _plan_kwargs

import comfyui_h3_context.adapters.av_reconstruction_media as media_module
from comfyui_h3_context.adapters.av_reconstruction_media import (
    AVMediaAdapterError,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.av_reconstruction_transport import AVTransportError
from comfyui_h3_context.adapters.media_subprocess import (
    MediaProcessInvocation,
    OwnedOutputLease,
    ProcessCapture,
    ProcessStatus,
    SubprocessMediaRunner,
    create_output_lease,
)
from comfyui_h3_context.core.av_reconstruction import (
    AVMediaDescriptor,
    AVOperation,
    AVRational,
    AVReconstructionApproval,
    AVReconstructionPlan,
    approve_av_reconstruction_plan,
    build_av_reconstruction_plan,
    qualified_av_limits,
    qualified_av_target_profile,
    qualified_ffmpeg_capability,
)
from comfyui_h3_context.core.segment_artifacts import SegmentArtifactReceipt


@pytest.mark.parametrize("durations", [(1,), (1, 1), (15, 15), (15, 1), (1, 2, 1)])
def test_real_aac_aggregate_keeps_exact_video_cadence(
    tmp_path: Path, durations: tuple[int, ...]
) -> None:
    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg = Path(ffmpeg_value).resolve(strict=True)
    ffprobe = Path(ffprobe_value).resolve(strict=True)
    target = qualified_av_target_profile()
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=tmp_path / "scratch",
        clock_ms=lambda: 1,
    )
    sources: list[Path] = []
    for index, seconds in enumerate(durations):
        color = ("red", "blue")[index % 2]
        source = tmp_path / f"normalized-{index}.mp4"
        subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:size={target.width}x{target.height}:rate=30:duration={seconds}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={400 + index * 400}:sample_rate=48000:duration={seconds}",
                "-ac",
                "2",
                "-c:v",
                "libx264",
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
                "-video_track_timescale",
                "30000",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-threads",
                "1",
                "-n",
                str(source),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        sources.append(source)
        source_capture = adapter._runner.run(adapter._probe_invocation(source))
        assert source_capture.status is ProcessStatus.SUCCEEDED
        source_probe = json.loads(source_capture.stdout)
        audio_frames = [row for row in source_probe["frames"] if row["media_type"] == "audio"]
        print(
            json.dumps(
                {
                    "source": index,
                    "seconds": seconds,
                    "audioEffectiveSamples": sum(row["duration"] for row in audio_frames),
                    "audioDecodedSamples": sum(row["nb_samples"] for row in audio_frames),
                    "lastAudioFrame": audio_frames[-1],
                }
            )
        )
    output = create_output_lease(tmp_path / "outputs", suffix=".mp4")
    try:
        expected_frames = sum(durations) * 30
        expected_samples = sum(durations) * 48_000
        seam_plan = None
        if len(durations) == 3:
            from test_av_seam_media import _seam_plan
            from test_av_seam_policy import _blend_spec

            from comfyui_h3_context.core.av_seam_policy import AVSeamOperation, AVSeamSpec

            seam_plan = _seam_plan(
                (AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN), _blend_spec(15)), count=3
            )
            expected_frames = seam_plan.total_output_frames
            expected_samples = seam_plan.total_output_samples
        # The old invocation remains executable during the controlled original-byte rollback.
        segment_kwargs: dict[str, Any] = {}
        if "segment_frames" in inspect.signature(adapter._aggregate_invocation).parameters:
            segment_kwargs = {
                "segment_frames": tuple(value * 30 for value in durations),
                "segment_samples": tuple(value * 48_000 for value in durations),
            }
        invocation = (
            adapter._seam_aggregate_invocation(
                tuple(sources), output, 64 * 1024 * 1024, seam_plan=seam_plan
            )
            if seam_plan is not None
            else adapter._aggregate_invocation(
                tuple(sources),
                output,
                64 * 1024 * 1024,
                expected_frames=expected_frames,
                expected_samples=expected_samples,
                **segment_kwargs,
            )
        )
        if len(sources) > 1:
            # PCM isolates the actual graph's direct join from a second lossy AAC encode.
            command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin"]
            for source in sources:
                command.extend(("-i", str(source)))
            command.extend(
                (
                    "-filter_complex",
                    invocation.argv[invocation.argv.index("-filter_complex") + 1],
                    "-map",
                    "[v]",
                    "-f",
                    "null",
                    str(tmp_path / "discard.video"),
                    "-map",
                    "[a]",
                    "-ac",
                    "1",
                    "-f",
                    "f32le",
                    "pipe:1",
                )
            )
            joined_pcm = subprocess.run(command, check=True, capture_output=True, timeout=60).stdout
            source_pcm = subprocess.run(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-i",
                    str(sources[1]),
                    "-vn",
                    "-ac",
                    "1",
                    "-f",
                    "f32le",
                    "pipe:1",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
            joined_samples, source_samples = array("f"), array("f")
            joined_samples.frombytes(joined_pcm)
            source_samples.frombytes(source_pcm)
            boundary = durations[0] * 48_000
            direct_join_error = max(
                abs(a - b)
                for a, b in zip(
                    joined_samples[boundary : boundary + 24_000],
                    source_samples[:24_000],
                    strict=True,
                )
            )
            print(json.dumps({"directJoinMaxSampleError": direct_join_error}))
            assert direct_join_error == 0
        adapter._run_ffmpeg(invocation, None)
        capture = adapter._runner.run(adapter._probe_invocation(output.path))
        assert capture.status is ProcessStatus.SUCCEEDED
        raw = json.loads(capture.stdout)
        video = next(row for row in raw["streams"] if row["codec_type"] == "video")
        frames = [row for row in raw["frames"] if row["media_type"] == "video"]
        print(
            json.dumps(
                {
                    "secondsPerSource": durations,
                    "averageRate": video["avg_frame_rate"],
                    "realRate": video["r_frame_rate"],
                    "videoDuration": video["duration"],
                    "frames": len(frames),
                    "boundaryTimestamps": [
                        row["best_effort_timestamp"]
                        for row in frames[durations[0] * 30 - 2 : durations[0] * 30 + 3]
                    ],
                    "bytes": output.path.stat().st_size,
                }
            )
        )
        descriptor = adapter._inspect_staged_output(
            output,
            segment_id="reconstruction.aggregate",
            authority_fingerprint=_fp(b"aggregate"),
            maximum_bytes=64 * 1024 * 1024,
            maximum_duration_ms=qualified_av_limits().max_total_duration_ms,
            cancellation=None,
        )
        assert descriptor.video is not None and descriptor.audio is not None
        assert descriptor.video.frame_rate == AVRational(30, 1)
        assert descriptor.video.decoded_frame_count == expected_frames
        assert descriptor.audio.decoded_sample_count == expected_samples
        assert descriptor.video.start_time == descriptor.audio.start_time == AVRational(0, 1)
        assert (
            descriptor.video.end_time.fraction
            == descriptor.audio.end_time.fraction
            == Fraction(expected_frames, 30)
        )
    finally:
        output.release()


def _fp(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _probe_payload(
    *,
    vfr: bool = False,
    extra_stream: bool = False,
    video_frame_count: int = 30,
    format_duration: str = "1.000000",
    video_duration: str = "1.000000",
    audio_duration: str = "1.000000",
    discontinuous_video: bool = False,
    video_codec: str = "h264",
    pixel_format: str = "yuv420p",
    audio_codec: str = "aac",
    sample_format: str = "fltp",
    missing_audio: bool = False,
    duplicate_audio: bool = False,
    start_seconds: int = 0,
    discontinuous_audio: bool = False,
    initial_padding: int | None = 0,
    trailing_padding: int | None = None,
    audio_frame_side_data: bool = False,
    video_frame_side_data_type: str | None = "H.26[45] User Data Unregistered SEI message",
    width: int = 512,
    sample_rate: int = 48_000,
    seconds: int = 1,
    frame_rate: int = 30,
) -> bytes:
    complete_audio_frames, final_audio_samples = divmod(sample_rate * seconds, 1024)
    audio_sizes = [1024] * complete_audio_frames
    if final_audio_samples:
        audio_sizes.append(final_audio_samples)
    audio_offset = 0
    audio_frames: list[dict[str, object]] = []
    for sample_count in audio_sizes:
        frame: dict[str, object] = {
            "media_type": "audio",
            "stream_index": 1,
            "best_effort_timestamp": (
                start_seconds * sample_rate
                + audio_offset
                + int(discontinuous_audio and audio_offset == audio_sizes[0] * 23)
            ),
            "duration": sample_count,
            "nb_samples": 1024,
        }
        if audio_frame_side_data and audio_offset == 0:
            frame["side_data_list"] = [
                {
                    "side_data_type": "Skip Samples",
                    "skip_samples": 1024,
                    "discard_padding": 0,
                }
            ]
        audio_frames.append(frame)
        audio_offset += sample_count
    video_frames = [
        {
            "media_type": "video",
            "stream_index": 0,
            "best_effort_timestamp": (
                start_seconds * 90_000
                + index * (90_000 // frame_rate)
                + int(discontinuous_video and index == 15)
            ),
            "duration": 90_000 // frame_rate,
        }
        for index in range(video_frame_count)
    ]
    if video_frame_side_data_type is not None:
        video_frames[0]["side_data_list"] = [{"side_data_type": video_frame_side_data_type}]
    streams: list[dict[str, object]] = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": video_codec,
            "width": width,
            "height": 512,
            "pix_fmt": pixel_format,
            "color_range": "tv",
            "color_space": "bt709",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
            "r_frame_rate": "60/1" if vfr else f"{frame_rate}/1",
            "avg_frame_rate": f"{frame_rate}/1",
            "time_base": "1/90000",
            "start_time": f"{start_seconds}.000000",
            "duration": video_duration,
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": audio_codec,
            "sample_fmt": sample_format,
            "sample_rate": str(sample_rate),
            "channels": 2,
            "channel_layout": "stereo",
            "time_base": f"1/{sample_rate}",
            "start_time": f"{start_seconds}.000000",
            "duration": audio_duration,
            "r_frame_rate": "0/0",
            "avg_frame_rate": "0/0",
        },
    ]
    if initial_padding is not None:
        streams[1]["initial_padding"] = initial_padding
    if trailing_padding is not None:
        streams[1]["trailing_padding"] = trailing_padding
    if missing_audio:
        streams = streams[:1]
        audio_frames = []
    elif duplicate_audio:
        streams.append({**streams[1], "index": 2})
    if extra_stream:
        streams.append({"index": 2, "codec_type": "subtitle", "codec_name": "mov_text"})
    return json.dumps(
        {
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": format_duration,
            },
            "streams": streams,
            "frames": [*video_frames, *audio_frames],
            "programs": [],
            "stream_groups": [],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _probe_with_unknown_member(location: str) -> bytes:
    value = cast(dict[str, Any], json.loads(_probe_payload()))
    if location == "root":
        value["unknown_root"] = True
    elif location == "format":
        cast(dict[str, Any], value["format"])["unknown_format"] = True
    elif location == "video_stream":
        cast(list[dict[str, Any]], value["streams"])[0]["unknown_video"] = True
    elif location == "frame":
        cast(list[dict[str, Any]], value["frames"])[0]["unknown_frame"] = True
    elif location == "programs":
        value["programs"] = [{"program_id": 1}]
    else:
        raise AssertionError("unknown probe fixture location")
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class _MutableCancellation:
    def __init__(self) -> None:
        self.cancelled = False
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.cancelled


@contextmanager
def _accepted_pin(_path: Path, _expected_sha256: str) -> Iterator[None]:
    yield


def _adapter(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe_payload: bytes,
    status: ProcessStatus = ProcessStatus.SUCCEEDED,
) -> tuple[QualifiedAVMediaAdapter, list[MediaProcessInvocation]]:
    ffmpeg = (root / "ffmpeg.exe").resolve()
    ffprobe = (root / "ffprobe.exe").resolve()
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
        return ProcessCapture(
            status=status,
            stdout=probe_payload if status is ProcessStatus.SUCCEEDED else b"",
            stderr=b"private diagnostic must not escape",
            exit_code=0 if status is ProcessStatus.SUCCEEDED else 1,
        )

    monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
    monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
    return (
        QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 100,
        ),
        invocations,
    )


def test_exact_probe_builds_closed_descriptor_and_keeps_locator_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter, invocations = _adapter(
            root,
            monkeypatch,
            probe_payload=_probe_payload(),
        )

        descriptor = adapter.inspect_payload(
            segment_id="segment.1",
            artifact_receipt_fingerprint=_fp(b"receipt"),
            artifact_output_fingerprint=_fp(payload),
            payload=payload,
        )

        assert descriptor.container == "mp4"
        assert descriptor.capability_fingerprint == qualified_ffmpeg_capability().fingerprint
        assert descriptor.warning_codes == ()
        assert descriptor.video is not None
        assert descriptor.video.frame_rate == AVRational(30, 1)
        assert descriptor.video.decoded_frame_count == 30
        assert descriptor.audio is not None
        assert descriptor.audio.decoded_sample_count == 48_000
        assert descriptor.audio.end_time == AVRational(1, 1)
        assert len(invocations) == 1
        invocation = invocations[0]
        assert invocation.executable == str((root / "ffprobe.exe").resolve())
        assert invocation.argv[0] == invocation.executable
        show_entries = invocation.argv[invocation.argv.index("-show_entries") + 1]
        assert "initial_padding" in show_entries
        assert "trailing_padding" in show_entries
        assert "seek_preroll" in show_entries
        assert "frame_side_data=side_data_type,skip_samples,discard_padding" in show_entries
        assert "-nostdin" not in invocation.argv
        assert invocation.argv.index("-show_format") < invocation.argv.index("-show_entries")
        assert invocation.argv.index("-show_streams") < invocation.argv.index("-show_entries")
        assert invocation.argv.index("-show_frames") < invocation.argv.index("-show_entries")
        public = json.dumps(invocation.to_public_dict(), sort_keys=True)
        assert str(root) not in public
        assert tuple((root / "scratch").iterdir()) == ()


def test_real_hash_replacement_is_rejected_before_process_use() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"wrong ffmpeg identity")
        ffprobe.write_bytes(b"wrong ffprobe identity")

        with pytest.raises(AVMediaAdapterError, match="adapter_capability_changed"):
            QualifiedAVMediaAdapter(
                ffmpeg_path=ffmpeg,
                ffprobe_path=ffprobe,
                scratch_root=(root / "scratch").resolve(),
                clock_ms=lambda: 100,
            )


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
@pytest.mark.parametrize("pin_kind", ("executable", "media"))
def test_windows_pin_denies_in_place_write_during_use(pin_kind: str) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = (Path(temporary) / "pinned.bin").resolve()
        original = b"A" * 4096
        replacement = b"B" * 4096
        path.write_bytes(original)

        def assert_write_denied() -> None:
            with pytest.raises(OSError):
                path.write_bytes(replacement)

        if pin_kind == "executable":
            with media_module._pin_exact_executable(
                path,
                hashlib.sha256(original).hexdigest(),
            ):
                assert_write_denied()
        else:
            with media_module._pin_regular_media(path, len(original)):
                assert_write_denied()

        assert path.read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_windows_read_pin_still_allows_exact_executable_spawn() -> None:
    executable = Path(sys.executable).resolve()
    expected = hashlib.sha256(executable.read_bytes()).hexdigest()

    with media_module._pin_exact_executable(executable, expected):
        completed = subprocess.run(  # noqa: S603
            (str(executable), "-c", "raise SystemExit(0)"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            shell=False,
            check=False,
        )

    assert completed.returncode == 0


@pytest.mark.parametrize(
    ("probe_payload", "expected"),
    (
        (_probe_payload(vfr=True), "inspection_invalid"),
        (_probe_payload(extra_stream=True), "inspection_invalid"),
        (_probe_payload(duplicate_audio=True), "inspection_invalid"),
        (_probe_payload(video_codec="vp8"), "inspection_invalid"),
        (_probe_payload(pixel_format="yuv444p"), "inspection_invalid"),
        (_probe_payload(audio_codec="mp3"), "inspection_invalid"),
        (_probe_payload(sample_format="s16"), "inspection_invalid"),
        (_probe_payload(discontinuous_audio=True), "inspection_invalid"),
        (_probe_payload(start_seconds=-1), "inspection_invalid"),
        (_probe_payload(initial_padding=1024), "inspection_invalid"),
        (_probe_payload(trailing_padding=1024), "inspection_invalid"),
        (_probe_payload(audio_frame_side_data=True), "inspection_invalid"),
        (
            _probe_payload(video_frame_side_data_type="Mastering display metadata"),
            "inspection_invalid",
        ),
        (_probe_with_unknown_member("root"), "inspection_invalid"),
        (_probe_with_unknown_member("format"), "inspection_invalid"),
        (_probe_with_unknown_member("video_stream"), "inspection_invalid"),
        (_probe_with_unknown_member("frame"), "inspection_invalid"),
        (_probe_with_unknown_member("programs"), "inspection_invalid"),
        (b'{"format":', "inspection_invalid"),
    ),
)
def test_untrusted_probe_variants_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    probe_payload: bytes,
    expected: str,
) -> None:
    payload = b"synthetic-private-segment"
    with tempfile.TemporaryDirectory() as temporary:
        adapter, _ = _adapter(
            Path(temporary),
            monkeypatch,
            probe_payload=probe_payload,
        )
        with pytest.raises(AVMediaAdapterError, match=expected):
            adapter.inspect_payload(
                segment_id="segment.1",
                artifact_receipt_fingerprint=_fp(b"receipt"),
                artifact_output_fingerprint=_fp(payload),
                payload=payload,
            )


@pytest.mark.parametrize(
    "probe_payload",
    (
        _probe_payload(format_duration="2.000000"),
        _probe_payload(video_duration="2.000000"),
        _probe_payload(audio_duration="2.000000"),
        _probe_payload(discontinuous_video=True),
    ),
)
def test_inconsistent_probe_timestamps_and_durations_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    probe_payload: bytes,
) -> None:
    payload = b"synthetic-private-segment"
    with tempfile.TemporaryDirectory() as temporary:
        adapter, _ = _adapter(
            Path(temporary),
            monkeypatch,
            probe_payload=probe_payload,
        )
        with pytest.raises(AVMediaAdapterError, match="inspection_invalid"):
            adapter.inspect_payload(
                segment_id="segment.1",
                artifact_receipt_fingerprint=_fp(b"receipt"),
                artifact_output_fingerprint=_fp(payload),
                payload=payload,
            )


@pytest.mark.parametrize("missing_audio", (False, True))
def test_audio_absence_and_nonzero_timestamps_are_explicit_facts(
    monkeypatch: pytest.MonkeyPatch,
    missing_audio: bool,
) -> None:
    payload = b"synthetic-private-segment"
    with tempfile.TemporaryDirectory() as temporary:
        adapter, _ = _adapter(
            Path(temporary),
            monkeypatch,
            probe_payload=_probe_payload(missing_audio=missing_audio, start_seconds=1),
        )
        descriptor = adapter.inspect_payload(
            segment_id="segment.1",
            artifact_receipt_fingerprint=_fp(b"receipt"),
            artifact_output_fingerprint=_fp(payload),
            payload=payload,
        )

        assert descriptor.video is not None
        assert descriptor.video.start_time == AVRational(1, 1)
        assert descriptor.video.end_time == AVRational(2, 1)
        assert descriptor.stream_count == (1 if missing_audio else 2)
        if missing_audio:
            assert descriptor.audio is None
        else:
            assert descriptor.audio is not None
            assert descriptor.audio.start_time == AVRational(1, 1)


def test_duplicate_probe_member_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"synthetic-private-segment"
    duplicate = _probe_payload().replace(
        b'{"format":',
        b'{"format":{"format_name":"mov","format_name":"mp4","duration":"1"},"ignored":',
        1,
    )
    with tempfile.TemporaryDirectory() as temporary:
        adapter, _ = _adapter(
            Path(temporary),
            monkeypatch,
            probe_payload=duplicate,
        )
        with pytest.raises(AVMediaAdapterError, match="inspection_invalid"):
            adapter.inspect_payload(
                segment_id="segment.1",
                artifact_receipt_fingerprint=_fp(b"receipt"),
                artifact_output_fingerprint=_fp(payload),
                payload=payload,
            )


def test_executable_replacement_at_use_time_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    pin_count = 0
    run_count = 0

    @contextmanager
    def replacement_pin(_path: Path, _expected_sha256: str) -> Iterator[None]:
        nonlocal pin_count
        pin_count += 1
        if pin_count >= 3:
            raise AVMediaAdapterError("adapter_capability_changed")
        yield

    def fake_run(
        _self: SubprocessMediaRunner,
        _invocation: MediaProcessInvocation,
        cancellation: object = None,
    ) -> ProcessCapture:
        nonlocal run_count
        del cancellation
        run_count += 1
        raise AssertionError("replacement must fail before spawn")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        monkeypatch.setattr(media_module, "_pin_exact_executable", replacement_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 100,
        )

        with pytest.raises(AVMediaAdapterError, match="adapter_capability_changed"):
            adapter.inspect_payload(
                segment_id="segment.1",
                artifact_receipt_fingerprint=_fp(b"receipt"),
                artifact_output_fingerprint=_fp(payload),
                payload=payload,
            )

        assert run_count == 0
        assert tuple((root / "scratch").iterdir()) == ()


def test_process_concurrency_is_shared_across_adapter_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    entered = threading.Event()
    release = threading.Event()

    def blocking_run(
        _self: SubprocessMediaRunner,
        _invocation: MediaProcessInvocation,
        cancellation: object = None,
    ) -> ProcessCapture:
        del cancellation
        entered.set()
        if not release.wait(timeout=5):
            raise AssertionError("test process slot was not released")
        return ProcessCapture(
            status=ProcessStatus.SUCCEEDED,
            stdout=_probe_payload(),
            exit_code=0,
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", blocking_run)
        adapters = tuple(
            QualifiedAVMediaAdapter(
                ffmpeg_path=ffmpeg,
                ffprobe_path=ffprobe,
                scratch_root=(root / f"scratch-{index}").resolve(),
                clock_ms=lambda: 100,
            )
            for index in range(2)
        )

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                adapters[0].inspect_payload,
                segment_id="segment.1",
                artifact_receipt_fingerprint=_fp(b"receipt"),
                artifact_output_fingerprint=_fp(payload),
                payload=payload,
            )
            assert entered.wait(timeout=5)
            with pytest.raises(AVMediaAdapterError, match="resource_limit"):
                adapters[1].inspect_payload(
                    segment_id="segment.1",
                    artifact_receipt_fingerprint=_fp(b"receipt"),
                    artifact_output_fingerprint=_fp(payload),
                    payload=payload,
                )
            release.set()
            first_descriptor = first.result(timeout=5)
            assert first_descriptor.video is not None
            assert first_descriptor.video.decoded_frame_count == 30


def test_inspection_pins_staged_input_across_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    active: set[str] = set()

    @contextmanager
    def tracked_pin(path: Path, maximum_bytes: int) -> Iterator[tuple[int, str]]:
        stored = path.read_bytes()
        assert len(stored) <= maximum_bytes
        key = str(path)
        active.add(key)
        try:
            yield len(stored), _fp(stored)
        finally:
            active.remove(key)

    def fake_run(
        _self: SubprocessMediaRunner,
        invocation: MediaProcessInvocation,
        cancellation: object = None,
    ) -> ProcessCapture:
        del cancellation
        assert invocation.argv[-1] in active
        return ProcessCapture(
            status=ProcessStatus.SUCCEEDED,
            stdout=_probe_payload(),
            exit_code=0,
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(media_module, "_pin_regular_media", tracked_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 100,
        )

        adapter.inspect_payload(
            segment_id="segment.1",
            artifact_receipt_fingerprint=_fp(b"receipt"),
            artifact_output_fingerprint=_fp(payload),
            payload=payload,
        )

        assert active == set()


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (ProcessStatus.OUTPUT_LIMIT, "resource_limit"),
        (ProcessStatus.TIMED_OUT, "timed_out"),
        (ProcessStatus.CANCELLED, "cancelled"),
        (ProcessStatus.TERMINATION_FAILED, "termination_failed"),
        (ProcessStatus.SPAWN_FAILED, "adapter_unavailable"),
    ),
)
def test_probe_process_statuses_map_without_raw_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    status: ProcessStatus,
    expected: str,
) -> None:
    payload = b"synthetic-private-segment"
    with tempfile.TemporaryDirectory() as temporary:
        adapter, _ = _adapter(
            Path(temporary),
            monkeypatch,
            probe_payload=b"",
            status=status,
        )
        with pytest.raises(AVMediaAdapterError) as raised:
            adapter.inspect_payload(
                segment_id="segment.1",
                artifact_receipt_fingerprint=_fp(b"receipt"),
                artifact_output_fingerprint=_fp(payload),
                payload=payload,
            )
        assert raised.value.code == expected
        assert "private diagnostic" not in str(raised.value)


def test_inspection_rechecks_cancellation_after_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    cancellation_state = _MutableCancellation()
    with tempfile.TemporaryDirectory() as temporary:
        adapter, _ = _adapter(
            Path(temporary),
            monkeypatch,
            probe_payload=_probe_payload(),
        )

        def cancel_after_probe(
            _self: SubprocessMediaRunner,
            _invocation: MediaProcessInvocation,
            cancellation: object = None,
        ) -> ProcessCapture:
            assert cancellation is cancellation_state
            cancellation_state.cancelled = True
            return ProcessCapture(
                status=ProcessStatus.SUCCEEDED,
                stdout=_probe_payload(),
                exit_code=0,
            )

        monkeypatch.setattr(SubprocessMediaRunner, "run", cancel_after_probe)
        with pytest.raises(AVMediaAdapterError, match="cancelled"):
            adapter.inspect_payload(
                segment_id="segment.1",
                artifact_receipt_fingerprint=_fp(b"receipt"),
                artifact_output_fingerprint=_fp(payload),
                payload=payload,
                cancellation=cancellation_state,
            )

        assert cancellation_state.calls >= 2


def _executable_plan(
    payload: bytes,
    *,
    missing_audio: bool,
) -> tuple[AVReconstructionPlan, AVReconstructionApproval]:
    kwargs = _plan_kwargs(count=1)
    receipt = replace(
        kwargs["artifact_receipts"][0],
        output_fingerprint=_fp(payload),
        byte_length=len(payload),
        receipt_fingerprint=None,
    )
    descriptor = replace(
        kwargs["media_descriptors"][0],
        artifact_receipt_fingerprint=receipt.fingerprint,
        artifact_output_fingerprint=receipt.output_fingerprint,
        artifact_byte_length=receipt.byte_length,
        stream_count=1 if missing_audio else 2,
        audio=None if missing_audio else kwargs["media_descriptors"][0].audio,
    )
    decision = replace(
        kwargs["selective_plan"].decisions[0],
        reused_receipt_fingerprint=receipt.fingerprint,
    )
    selective_plan = replace(
        kwargs["selective_plan"],
        decisions=(decision,),
        reusable_receipts=(receipt,),
        plan_fingerprint=None,
    )
    selective_approval = replace(
        kwargs["selective_approval"],
        plan_fingerprint=selective_plan.fingerprint,
        approval_fingerprint=None,
    )
    plan = build_av_reconstruction_plan(
        **{
            **kwargs,
            "selective_plan": selective_plan,
            "selective_approval": selective_approval,
            "artifact_receipts": (receipt,),
            "media_descriptors": (descriptor,),
        }
    )
    approval = approve_av_reconstruction_plan(
        plan,
        approved_at_ms=310,
        expires_at_ms=600,
    )
    return plan, approval


def _plan_from_inspected_descriptor(
    *,
    receipt: SegmentArtifactReceipt,
    descriptor: AVMediaDescriptor,
) -> tuple[AVReconstructionPlan, AVReconstructionApproval]:
    kwargs = _plan_kwargs(count=1)
    decision = replace(
        kwargs["selective_plan"].decisions[0],
        reused_receipt_fingerprint=receipt.fingerprint,
    )
    selective_plan = replace(
        kwargs["selective_plan"],
        decisions=(decision,),
        reusable_receipts=(receipt,),
        plan_fingerprint=None,
    )
    selective_approval = replace(
        kwargs["selective_approval"],
        plan_fingerprint=selective_plan.fingerprint,
        approval_fingerprint=None,
    )
    plan = build_av_reconstruction_plan(
        **{
            **kwargs,
            "selective_plan": selective_plan,
            "selective_approval": selective_approval,
            "artifact_receipts": (receipt,),
            "media_descriptors": (descriptor,),
        }
    )
    return plan, approve_av_reconstruction_plan(
        plan,
        approved_at_ms=310,
        expires_at_ms=600,
    )


def _qualified_binary_paths_from_environment() -> tuple[Path, Path]:
    ffmpeg_value = os.environ.get("H3_M17_11_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_M17_11_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact Gate-0 media paths were not explicitly supplied")
    return Path(ffmpeg_value).resolve(strict=True), Path(ffprobe_value).resolve(strict=True)


def test_exact_qualified_binary_executes_activated_profile_matrix() -> None:
    ffmpeg, ffprobe = _qualified_binary_paths_from_environment()
    cases = (
        ("passthrough", 512, 48_000, True, (AVOperation.PASSTHROUGH,)),
        ("normalize_video", 640, 48_000, True, (AVOperation.NORMALIZE_VIDEO,)),
        ("normalize_audio", 512, 96_000, True, (AVOperation.NORMALIZE_AUDIO,)),
        ("insert_silence", 512, 48_000, False, (AVOperation.INSERT_SILENCE,)),
    )
    workspace_tmp = (Path.cwd() / ".tmp").resolve()
    workspace_tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace_tmp) as temporary:
        root = Path(temporary).resolve()
        now_ms = [250]
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: now_ms[0],
        )
        output_root = root / "outputs"
        output_root.mkdir()

        for name, width, sample_rate, has_audio, expected_operations in cases:
            media_path = root / f"{name}.mp4"
            argv = [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={width}x512:r=30:d=1",
            ]
            if has_audio:
                argv.extend(
                    (
                        "-f",
                        "lavfi",
                        "-i",
                        f"anullsrc=r={sample_rate}:cl=stereo:d=1",
                    )
                )
            argv.extend(("-map", "0:v:0"))
            if has_audio:
                argv.extend(("-map", "1:a:0"))
            argv.extend(
                (
                    "-frames:v",
                    "30",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-x264-params",
                    "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
                )
            )
            if has_audio:
                argv.extend(
                    (
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-ar",
                        str(sample_rate),
                        "-ac",
                        "2",
                    )
                )
            argv.extend(
                (
                    "-map_metadata",
                    "-1",
                    "-map_chapters",
                    "-1",
                    "-metadata",
                    "encoder=",
                    "-color_range",
                    "tv",
                    "-colorspace",
                    "bt709",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    "-video_track_timescale",
                    "90000",
                    "-movflags",
                    "+faststart",
                    "-threads",
                    "1",
                    "-n",
                    str(media_path),
                )
            )
            generated = subprocess.run(  # noqa: S603
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                shell=False,
                check=False,
                timeout=30,
            )
            assert generated.returncode == 0
            payload = media_path.read_bytes()
            kwargs = _plan_kwargs(count=1)
            receipt = replace(
                kwargs["artifact_receipts"][0],
                output_fingerprint=_fp(payload),
                byte_length=len(payload),
                receipt_fingerprint=None,
            )
            assert receipt.output_fingerprint is not None
            now_ms[0] = 250
            descriptor = adapter.inspect_payload(
                segment_id=receipt.segment_id,
                artifact_receipt_fingerprint=receipt.fingerprint,
                artifact_output_fingerprint=receipt.output_fingerprint,
                payload=payload,
            )
            plan, approval = _plan_from_inspected_descriptor(
                receipt=receipt,
                descriptor=descriptor,
            )
            assert plan.segments[0].operations == expected_operations
            aggregate = create_output_lease(output_root, suffix=".mp4")
            derived = (
                None
                if expected_operations == (AVOperation.PASSTHROUGH,)
                else create_output_lease(output_root, suffix=".mp4")
            )
            now_ms[0] = 400
            result = adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((receipt.segment_id, payload),),
                aggregate_output=aggregate,
                derived_outputs=(() if derived is None else ((receipt.segment_id, derived),)),
            )
            media_module._assert_exact_output(
                result.aggregate_descriptor,
                expected_frames=30,
                expected_samples=48_000,
            )
            assert aggregate.path.is_file()
            aggregate.release()
            if derived is not None:
                assert derived.path.is_file()
                derived.release()

            # M20-08: the same real-binary case executed from an incremental source must be
            # observationally identical to the whole-bytes execution just performed.
            source = _ReplaySource(payload)
            source_aggregate = create_output_lease(output_root, suffix=".mp4")
            source_derived = (
                None
                if expected_operations == (AVOperation.PASSTHROUGH,)
                else create_output_lease(output_root, suffix=".mp4")
            )
            source_result = adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((receipt.segment_id, source),),
                aggregate_output=source_aggregate,
                derived_outputs=(
                    () if source_derived is None else ((receipt.segment_id, source_derived),)
                ),
            )
            assert source.stream_calls == (
                1 if expected_operations == (AVOperation.PASSTHROUGH,) else 2
            )
            assert source_result.execution_fingerprint == result.execution_fingerprint
            assert (
                source_result.aggregate_descriptor.to_wire()
                == result.aggregate_descriptor.to_wire()
            )
            source_aggregate.release()
            if source_derived is not None:
                source_derived.release()


def _executable_operation_plan(
    payload: bytes,
    *,
    operation: AVOperation,
    claimed_audio_codec: str = "aac",
) -> tuple[AVReconstructionPlan, AVReconstructionApproval]:
    kwargs = _plan_kwargs(count=1)
    receipt = replace(
        kwargs["artifact_receipts"][0],
        output_fingerprint=_fp(payload),
        byte_length=len(payload),
        receipt_fingerprint=None,
    )
    descriptor = replace(
        kwargs["media_descriptors"][0],
        artifact_receipt_fingerprint=receipt.fingerprint,
        artifact_output_fingerprint=receipt.output_fingerprint,
        artifact_byte_length=receipt.byte_length,
    )
    if operation is AVOperation.NORMALIZE_VIDEO:
        assert descriptor.video is not None
        descriptor = replace(descriptor, video=replace(descriptor.video, width=640))
    elif operation is AVOperation.NORMALIZE_AUDIO:
        assert descriptor.audio is not None
        descriptor = replace(
            descriptor,
            audio=replace(
                descriptor.audio,
                codec=claimed_audio_codec,
                sample_rate=96_000,
                time_base=AVRational(1, 96_000),
                decoded_sample_count=96_000,
            ),
        )
    else:
        raise AssertionError("unsupported operation fixture")
    decision = replace(
        kwargs["selective_plan"].decisions[0],
        reused_receipt_fingerprint=receipt.fingerprint,
    )
    selective_plan = replace(
        kwargs["selective_plan"],
        decisions=(decision,),
        reusable_receipts=(receipt,),
        plan_fingerprint=None,
    )
    selective_approval = replace(
        kwargs["selective_approval"],
        plan_fingerprint=selective_plan.fingerprint,
        approval_fingerprint=None,
    )
    plan = build_av_reconstruction_plan(
        **{
            **kwargs,
            "selective_plan": selective_plan,
            "selective_approval": selective_approval,
            "artifact_receipts": (receipt,),
            "media_descriptors": (descriptor,),
        }
    )
    assert plan.segments[0].operations == (operation,)
    return plan, approve_av_reconstruction_plan(
        plan,
        approved_at_ms=310,
        expires_at_ms=600,
    )


@pytest.mark.parametrize(
    ("missing_audio", "expected_operation", "expected_ffmpeg_calls"),
    (
        (False, AVOperation.PASSTHROUGH, 1),
        (True, AVOperation.INSERT_SILENCE, 2),
    ),
)
def test_execution_uses_approved_operations_and_reinspects_every_output(
    monkeypatch: pytest.MonkeyPatch,
    missing_audio: bool,
    expected_operation: AVOperation,
    expected_ffmpeg_calls: int,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=missing_audio)
    assert expected_operation in plan.segments[0].operations
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        output_root = root / "outputs"
        output_root.mkdir()
        aggregate = create_output_lease(output_root, suffix=".mp4")
        derived = create_output_lease(output_root, suffix=".mp4") if missing_audio else None
        invocations: list[MediaProcessInvocation] = []

        def fake_run(
            _self: SubprocessMediaRunner,
            invocation: MediaProcessInvocation,
            cancellation: object = None,
        ) -> ProcessCapture:
            del cancellation
            invocations.append(invocation)
            if invocation.tool == "ffmpeg":
                for lease in invocation.output_leases:
                    lease.path.write_bytes(b"synthetic encoded output")
                return ProcessCapture(
                    status=ProcessStatus.SUCCEEDED,
                    exit_code=0,
                    owned_output_bytes=len(b"synthetic encoded output"),
                    artifacts=invocation.output_leases,
                )
            return ProcessCapture(
                status=ProcessStatus.SUCCEEDED,
                stdout=_probe_payload(
                    missing_audio=(
                        missing_audio and not any(item.tool == "ffmpeg" for item in invocations)
                    )
                ),
                exit_code=0,
            )

        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )

        result = adapter.execute_plan(
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, payload),),
            aggregate_output=aggregate,
            derived_outputs=(() if derived is None else ((plan.segments[0].segment_id, derived),)),
        )

        ffmpeg_calls = [item for item in invocations if item.tool == "ffmpeg"]
        ffprobe_calls = [item for item in invocations if item.tool == "ffprobe"]
        assert len(ffmpeg_calls) == expected_ffmpeg_calls
        assert all(
            "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited" in item.argv
            for item in ffmpeg_calls
            if "libx264" in item.argv
        )
        assert all(item.argv[item.argv.index("-f") + 1] == "mp4" for item in ffmpeg_calls)
        aggregate_filter = ffmpeg_calls[-1].argv[ffmpeg_calls[-1].argv.index("-filter_complex") + 1]
        assert "trim=end_frame=30" in aggregate_filter
        assert "atrim=end_sample=48000" in aggregate_filter
        assert len(ffprobe_calls) == 2 + int(missing_audio)
        assert all(item.executable in {str(ffmpeg), str(ffprobe)} for item in invocations)
        assert result.aggregate_descriptor.video is not None
        assert result.aggregate_descriptor.video.decoded_frame_count == 30
        assert result.aggregate_descriptor.audio is not None
        assert result.aggregate_descriptor.audio.decoded_sample_count == 48_000
        assert len(result.derived_descriptors) == int(missing_audio)
        assert result.execution_fingerprint.startswith("sha256:")
        assert aggregate.path.is_file()
        if derived is not None:
            assert derived.path.is_file()
            transform_argv = " ".join(ffmpeg_calls[0].argv)
            assert "aformat" in transform_argv
            assert "atrim=end_sample=48000" in transform_argv
            assert "-y" not in ffmpeg_calls[0].argv

        aggregate.release()
        if derived is not None:
            derived.release()


def test_execution_rechecks_cancellation_before_result_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    cancellation = _MutableCancellation()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        output_root = root / "outputs"
        output_root.mkdir()
        aggregate = create_output_lease(output_root, suffix=".mp4")
        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )

        def fake_ffmpeg(
            _self: QualifiedAVMediaAdapter,
            _invocation: MediaProcessInvocation,
            _cancellation: object,
        ) -> None:
            return

        def inspect_then_cancel(
            _self: QualifiedAVMediaAdapter,
            _lease: OwnedOutputLease,
            **_kwargs: object,
        ) -> AVMediaDescriptor:
            cancellation.cancelled = True
            return plan.segments[0].descriptor

        monkeypatch.setattr(QualifiedAVMediaAdapter, "_run_ffmpeg", fake_ffmpeg)
        monkeypatch.setattr(
            QualifiedAVMediaAdapter,
            "_inspect_staged_output",
            inspect_then_cancel,
        )

        with pytest.raises(AVMediaAdapterError, match="cancelled"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, payload),),
                aggregate_output=aggregate,
                derived_outputs=(),
                cancellation=cancellation,
            )

        assert cancellation.calls >= 2
        assert aggregate.released


@pytest.mark.parametrize(
    ("operation", "required_argv", "forbidden_argv"),
    (
        (
            AVOperation.NORMALIZE_VIDEO,
            (
                "scale=512:512",
                "-c:v libx264",
                "-c:a copy",
                "-x264-params",
                "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
            ),
            ("aresample=",),
        ),
        (
            AVOperation.NORMALIZE_AUDIO,
            ("aresample=48000", "-c:v copy", "-c:a aac"),
            ("scale=",),
        ),
    ),
)
def test_each_activated_transform_maps_to_exact_approved_command(
    monkeypatch: pytest.MonkeyPatch,
    operation: AVOperation,
    required_argv: tuple[str, ...],
    forbidden_argv: tuple[str, ...],
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_operation_plan(payload, operation=operation)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        output_root = root / "outputs"
        output_root.mkdir()
        aggregate = create_output_lease(output_root, suffix=".mp4")
        derived = create_output_lease(output_root, suffix=".mp4")
        invocations: list[MediaProcessInvocation] = []

        def fake_run(
            _self: SubprocessMediaRunner,
            invocation: MediaProcessInvocation,
            cancellation: object = None,
        ) -> ProcessCapture:
            del cancellation
            invocations.append(invocation)
            if invocation.tool == "ffmpeg":
                invocation.output_leases[0].path.write_bytes(b"synthetic encoded output")
                return ProcessCapture(
                    status=ProcessStatus.SUCCEEDED,
                    exit_code=0,
                    owned_output_bytes=len(b"synthetic encoded output"),
                    artifacts=invocation.output_leases,
                )
            return ProcessCapture(
                status=ProcessStatus.SUCCEEDED,
                stdout=(
                    _probe_payload(width=640)
                    if operation is AVOperation.NORMALIZE_VIDEO
                    and not any(item.tool == "ffmpeg" for item in invocations)
                    else _probe_payload(sample_rate=96_000)
                    if operation is AVOperation.NORMALIZE_AUDIO
                    and not any(item.tool == "ffmpeg" for item in invocations)
                    else _probe_payload()
                ),
                exit_code=0,
            )

        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )

        adapter.execute_plan(
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, payload),),
            aggregate_output=aggregate,
            derived_outputs=((plan.segments[0].segment_id, derived),),
        )

        transform = next(item for item in invocations if item.tool == "ffmpeg")
        joined = " ".join(transform.argv)
        assert all(value in joined for value in required_argv)
        assert all(value not in joined for value in forbidden_argv)
        assert transform.max_owned_output_bytes <= plan.limits.max_aggregate_output_bytes
        aggregate.release()
        derived.release()


def test_execution_reinspects_every_source_before_transform_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_operation_plan(
        payload,
        operation=AVOperation.NORMALIZE_AUDIO,
        claimed_audio_codec="pcm_s16le",
    )
    assert plan.segments[0].descriptor.audio is not None
    assert plan.segments[0].descriptor.audio.codec == "pcm_s16le"
    invocations: list[MediaProcessInvocation] = []

    def fake_run(
        _self: SubprocessMediaRunner,
        invocation: MediaProcessInvocation,
        cancellation: object = None,
    ) -> ProcessCapture:
        del cancellation
        invocations.append(invocation)
        if invocation.tool == "ffmpeg":
            raise AssertionError("source facts must be reconciled before transform spawn")
        return ProcessCapture(
            status=ProcessStatus.SUCCEEDED,
            stdout=_probe_payload(),
            exit_code=0,
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        output_root = root / "outputs"
        output_root.mkdir()
        aggregate = create_output_lease(output_root, suffix=".mp4")
        derived = create_output_lease(output_root, suffix=".mp4")
        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )

        with pytest.raises(AVMediaAdapterError, match="inspection_stale"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, payload),),
                aggregate_output=aggregate,
                derived_outputs=((plan.segments[0].segment_id, derived),),
            )

        assert invocations
        assert all(item.tool == "ffprobe" for item in invocations)
        assert aggregate.released
        assert derived.released


def test_output_reinspection_mismatch_releases_all_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        output_root = root / "outputs"
        output_root.mkdir()
        aggregate = create_output_lease(output_root, suffix=".mp4")
        ffmpeg_seen = False

        def fake_run(
            _self: SubprocessMediaRunner,
            invocation: MediaProcessInvocation,
            cancellation: object = None,
        ) -> ProcessCapture:
            nonlocal ffmpeg_seen
            del cancellation
            if invocation.tool == "ffmpeg":
                ffmpeg_seen = True
                invocation.output_leases[0].path.write_bytes(b"mismatched output")
                return ProcessCapture(
                    status=ProcessStatus.SUCCEEDED,
                    exit_code=0,
                    owned_output_bytes=len(b"mismatched output"),
                    artifacts=invocation.output_leases,
                )
            return ProcessCapture(
                status=ProcessStatus.SUCCEEDED,
                stdout=_probe_payload(video_frame_count=29 if ffmpeg_seen else 30),
                exit_code=0,
            )

        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )

        with pytest.raises(AVMediaAdapterError, match="media_output_invalid"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, payload),),
                aggregate_output=aggregate,
                derived_outputs=(),
            )

        assert aggregate.released
        assert tuple((root / "scratch").iterdir()) == ()


def test_execution_pins_every_process_input_and_enforces_temp_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=True)
    active: dict[str, int] = {}

    @contextmanager
    def tracked_pin(path: Path, maximum_bytes: int) -> Iterator[tuple[int, str]]:
        stored = path.read_bytes()
        assert len(stored) <= maximum_bytes
        key = str(path)
        active[key] = active.get(key, 0) + 1
        try:
            yield len(stored), _fp(stored)
        finally:
            active[key] -= 1
            if active[key] == 0:
                del active[key]

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        output_root = root / "outputs"
        output_root.mkdir()
        aggregate = create_output_lease(output_root, suffix=".mp4")
        derived = create_output_lease(output_root, suffix=".mp4")
        ffmpeg_seen = False

        def fake_run(
            _self: SubprocessMediaRunner,
            invocation: MediaProcessInvocation,
            cancellation: object = None,
        ) -> ProcessCapture:
            nonlocal ffmpeg_seen
            del cancellation
            if invocation.tool == "ffmpeg":
                ffmpeg_seen = True
                input_paths = tuple(
                    invocation.argv[index + 1]
                    for index, value in enumerate(invocation.argv[:-1])
                    if value == "-i"
                )
                assert input_paths
                assert all(active.get(path, 0) > 0 for path in input_paths)
                invocation.output_leases[0].path.write_bytes(b"synthetic encoded output")
                return ProcessCapture(
                    status=ProcessStatus.SUCCEEDED,
                    exit_code=0,
                    owned_output_bytes=len(b"synthetic encoded output"),
                    artifacts=invocation.output_leases,
                )
            assert active.get(invocation.argv[-1], 0) > 0
            return ProcessCapture(
                status=ProcessStatus.SUCCEEDED,
                stdout=_probe_payload(missing_audio=not ffmpeg_seen),
                exit_code=0,
            )

        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(media_module, "_pin_regular_media", tracked_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )

        result = adapter.execute_plan(
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, payload),),
            aggregate_output=aggregate,
            derived_outputs=((plan.segments[0].segment_id, derived),),
        )

        assert result.aggregate_descriptor.video is not None
        assert active == {}
        aggregate.release()
        derived.release()


def test_temp_budget_exhaustion_prevents_process_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    run_count = 0

    def fake_run(
        _self: SubprocessMediaRunner,
        _invocation: MediaProcessInvocation,
        cancellation: object = None,
    ) -> ProcessCapture:
        nonlocal run_count
        del cancellation
        run_count += 1
        raise AssertionError("exhausted temp budget must prevent spawn")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        output_root = root / "outputs"
        output_root.mkdir()
        aggregate = create_output_lease(output_root, suffix=".mp4")
        constrained = replace(plan.limits, max_temp_bytes=len(payload) - 1)
        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(media_module, "qualified_av_limits", lambda: constrained)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )

        with pytest.raises(AVMediaAdapterError, match="resource_limit"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, payload),),
                aggregate_output=aggregate,
                derived_outputs=(),
            )

        assert run_count == 0
        assert aggregate.released


def test_scratch_cleanup_failure_releases_otherwise_successful_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    original_release = OwnedOutputLease.release
    injected = False
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        output_root = root / "outputs"
        output_root.mkdir()
        aggregate = create_output_lease(output_root, suffix=".mp4")

        def fake_run(
            _self: SubprocessMediaRunner,
            invocation: MediaProcessInvocation,
            cancellation: object = None,
        ) -> ProcessCapture:
            del cancellation
            if invocation.tool == "ffmpeg":
                invocation.output_leases[0].path.write_bytes(b"synthetic encoded output")
                return ProcessCapture(
                    status=ProcessStatus.SUCCEEDED,
                    exit_code=0,
                    owned_output_bytes=len(b"synthetic encoded output"),
                    artifacts=invocation.output_leases,
                )
            return ProcessCapture(
                status=ProcessStatus.SUCCEEDED,
                stdout=_probe_payload(),
                exit_code=0,
            )

        def fail_one_scratch_release(lease: OwnedOutputLease) -> None:
            nonlocal injected
            if not injected and str(lease.root).startswith(str(root / "scratch")):
                injected = True
                raise OSError("injected private cleanup failure")
            original_release(lease)

        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        monkeypatch.setattr(OwnedOutputLease, "release", fail_one_scratch_release)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )

        with pytest.raises(AVMediaAdapterError, match="cleanup_failed"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, payload),),
                aggregate_output=aggregate,
                derived_outputs=(),
            )

        assert injected
        assert aggregate.released


# ---------------------------------------------------------------------------
# M20-08: incremental source-backed inputs and the rolling assembly
# ---------------------------------------------------------------------------


class _ReplaySource:
    """Fake ``AVSegmentSource`` replaying one payload into every destination.

    ``report_declared=True`` models a lying source: it writes ``replay`` but reports the
    declared identity, so only the adapter's own pinned hash can catch the forgery.
    """

    def __init__(
        self,
        payload: bytes,
        *,
        declared: tuple[int, str] | None = None,
        replay: bytes | None = None,
        error: Exception | None = None,
        report_declared: bool = False,
        fail_on_call: int | None = None,
    ) -> None:
        self._replay = payload if replay is None else replay
        self._declared = (len(payload), _fp(payload)) if declared is None else declared
        self._error = error
        self._report_declared = report_declared
        self._fail_on_call = fail_on_call
        self.stream_calls = 0
        self.destinations: list[Path] = []
        self.prior_destination_alive_at_call: list[bool] = []

    @property
    def byte_length(self) -> int:
        return self._declared[0]

    @property
    def content_fingerprint(self) -> str:
        return self._declared[1]

    def stream_into(
        self,
        destination: Path,
        *,
        cancellation: object = None,
    ) -> tuple[int, str]:
        del cancellation
        self.stream_calls += 1
        self.prior_destination_alive_at_call.append(
            any(item.exists() for item in self.destinations)
        )
        self.destinations.append(Path(destination))
        if self._error is not None and self._fail_on_call in (None, self.stream_calls):
            raise self._error
        with open(destination, "xb") as handle:
            handle.write(self._replay)
        if self._report_declared:
            return self._declared
        return (len(self._replay), _fp(self._replay))


def _prepared_adapter(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    missing_audio: bool,
) -> tuple[QualifiedAVMediaAdapter, OwnedOutputLease, OwnedOutputLease | None]:
    root.mkdir(parents=True, exist_ok=True)
    ffmpeg = (root / "ffmpeg.exe").resolve()
    ffprobe = (root / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"synthetic ffmpeg")
    ffprobe.write_bytes(b"synthetic ffprobe")
    output_root = root / "outputs"
    output_root.mkdir()
    aggregate = create_output_lease(output_root, suffix=".mp4")
    derived = create_output_lease(output_root, suffix=".mp4") if missing_audio else None
    invocations: list[MediaProcessInvocation] = []

    def fake_run(
        _self: SubprocessMediaRunner,
        invocation: MediaProcessInvocation,
        cancellation: object = None,
    ) -> ProcessCapture:
        del cancellation
        invocations.append(invocation)
        if invocation.tool == "ffmpeg":
            for lease in invocation.output_leases:
                lease.path.write_bytes(b"synthetic encoded output")
            return ProcessCapture(
                status=ProcessStatus.SUCCEEDED,
                exit_code=0,
                owned_output_bytes=len(b"synthetic encoded output"),
                artifacts=invocation.output_leases,
            )
        return ProcessCapture(
            status=ProcessStatus.SUCCEEDED,
            stdout=_probe_payload(
                missing_audio=(
                    missing_audio and not any(item.tool == "ffmpeg" for item in invocations)
                )
            ),
            exit_code=0,
        )

    monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
    monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(root / "scratch").resolve(),
        clock_ms=lambda: 400,
    )
    return adapter, aggregate, derived


@pytest.mark.parametrize("missing_audio", (False, True))
def test_source_backed_execution_matches_bytes_execution_exactly(
    monkeypatch: pytest.MonkeyPatch,
    missing_audio: bool,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=missing_audio)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter, aggregate, derived = _prepared_adapter(
            monkeypatch, root / "bytes", missing_audio=missing_audio
        )
        baseline = adapter.execute_plan(
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, payload),),
            aggregate_output=aggregate,
            derived_outputs=(() if derived is None else ((plan.segments[0].segment_id, derived),)),
        )
        aggregate.release()
        if derived is not None:
            derived.release()

        source = _ReplaySource(payload)
        adapter, aggregate, derived = _prepared_adapter(
            monkeypatch, root / "src", missing_audio=missing_audio
        )
        incremental = adapter.execute_plan(
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source),),
            aggregate_output=aggregate,
            derived_outputs=(() if derived is None else ((plan.segments[0].segment_id, derived),)),
        )

        # Parity oracle: the incremental transport must be observationally identical.
        assert incremental.execution_fingerprint == baseline.execution_fingerprint
        assert incremental.aggregate_descriptor.to_wire() == baseline.aggregate_descriptor.to_wire()
        assert [item.to_wire() for item in incremental.derived_descriptors] == [
            item.to_wire() for item in baseline.derived_descriptors
        ]
        # Rolling assembly: a non-passthrough source streams once to verify and once to
        # transform, and the verified copy is already gone when the second stream begins.
        assert source.stream_calls == (2 if missing_audio else 1)
        if missing_audio:
            assert len(set(source.destinations)) == 2
            assert source.prior_destination_alive_at_call == [False, False]
        assert not any(item.exists() for item in source.destinations)
        assert tuple((root / "src" / "scratch").iterdir()) == ()
        aggregate.release()
        if derived is not None:
            derived.release()


def test_source_declared_identity_mismatch_fails_before_any_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter, aggregate, _ = _prepared_adapter(monkeypatch, root, missing_audio=False)
        source = _ReplaySource(payload, declared=(len(payload) + 1, _fp(payload)))

        with pytest.raises(AVMediaAdapterError, match="inspection_stale"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source),),
                aggregate_output=aggregate,
                derived_outputs=(),
            )

        assert source.stream_calls == 0
        assert aggregate.released


def test_lying_source_content_is_caught_by_the_adapters_own_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter, aggregate, _ = _prepared_adapter(monkeypatch, root, missing_audio=False)
        # Same length, different content, and the source reports the declared identity:
        # only the adapter's own pinned hash can catch it.
        source = _ReplaySource(
            payload,
            replay=b"synthetic-PRIVATE-segment",
            report_declared=True,
        )

        with pytest.raises(AVMediaAdapterError, match="inspection_stale"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source),),
                aggregate_output=aggregate,
                derived_outputs=(),
            )

        assert source.stream_calls == 1
        assert aggregate.released
        assert tuple((root / "scratch").iterdir()) == ()


@pytest.mark.parametrize(
    ("error", "expected_code"),
    (
        (RuntimeError("foreign source failure"), "inspection_unavailable"),
        (AVTransportError("transport_unavailable"), "inspection_unavailable"),
        (AVTransportError("transport_identity_mismatch"), "inspection_stale"),
        (AVTransportError("transport_cancelled"), "cancelled"),
    ),
)
def test_source_stream_failures_map_to_closed_adapter_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_code: str,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter, aggregate, _ = _prepared_adapter(monkeypatch, root, missing_audio=False)
        source = _ReplaySource(payload, error=error)

        with pytest.raises(AVMediaAdapterError, match=expected_code):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source),),
                aggregate_output=aggregate,
                derived_outputs=(),
            )

        assert source.stream_calls == 1
        assert aggregate.released
        assert tuple((root / "scratch").iterdir()) == ()


def test_a_payload_that_is_neither_bytes_nor_source_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter, aggregate, _ = _prepared_adapter(monkeypatch, root, missing_audio=False)

        with pytest.raises(AVMediaAdapterError, match="operation_unsupported"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, cast(bytes, bytearray(payload))),),
                aggregate_output=aggregate,
                derived_outputs=(),
            )

        assert aggregate.released


def _executable_cohort_plan(
    payloads: tuple[bytes, ...],
) -> tuple[AVReconstructionPlan, AVReconstructionApproval]:
    """A frozen bounded cohort: every segment misses audio, so every segment transforms."""

    kwargs = _plan_kwargs(count=len(payloads))
    receipts = tuple(
        replace(
            receipt,
            output_fingerprint=_fp(payload),
            byte_length=len(payload),
            receipt_fingerprint=None,
        )
        for receipt, payload in zip(kwargs["artifact_receipts"], payloads, strict=True)
    )
    descriptors = tuple(
        replace(
            descriptor,
            artifact_receipt_fingerprint=receipt.fingerprint,
            artifact_output_fingerprint=receipt.output_fingerprint,
            artifact_byte_length=receipt.byte_length,
            stream_count=1,
            audio=None,
        )
        for descriptor, receipt in zip(kwargs["media_descriptors"], receipts, strict=True)
    )
    decisions = tuple(
        replace(decision, reused_receipt_fingerprint=receipt.fingerprint)
        for decision, receipt in zip(kwargs["selective_plan"].decisions, receipts, strict=True)
    )
    selective_plan = replace(
        kwargs["selective_plan"],
        decisions=decisions,
        reusable_receipts=receipts,
        plan_fingerprint=None,
    )
    selective_approval = replace(
        kwargs["selective_approval"],
        plan_fingerprint=selective_plan.fingerprint,
        approval_fingerprint=None,
    )
    plan = build_av_reconstruction_plan(
        **{
            **kwargs,
            "selective_plan": selective_plan,
            "selective_approval": selective_approval,
            "artifact_receipts": receipts,
            "media_descriptors": descriptors,
        }
    )
    approval = approve_av_reconstruction_plan(
        plan,
        approved_at_ms=310,
        expires_at_ms=600,
    )
    return plan, approval


def test_bounded_cohort_peak_staged_copies_rolling_versus_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stress row: over a frozen 3-segment all-transform cohort, the bytes path holds every
    staged input for the whole run (peak == cohort size) while the rolling assembly keeps at
    most one source-backed staged copy alive at any sampled moment, at identical output."""

    payloads = tuple(f"synthetic-private-segment-{index}".encode("ascii") for index in range(3))
    plan, approval = _executable_cohort_plan(payloads)
    assert all(item.operations != (AVOperation.PASSTHROUGH,) for item in plan.segments)

    def _staged_media_count(root: Path) -> int:
        scratch = root / "scratch"
        if not scratch.is_dir():
            return 0
        return sum(
            1
            for child in scratch.iterdir()
            if child.is_dir()
            for item in child.iterdir()
            if item.suffix == ".media" and item.is_file()
        )

    def _run(
        sub_root: Path,
        entries: tuple[tuple[str, object], ...],
    ) -> tuple[Any, list[int]]:
        sub_root.mkdir(parents=True, exist_ok=True)
        ffmpeg = (sub_root / "ffmpeg.exe").resolve()
        ffprobe = (sub_root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        output_root = sub_root / "outputs"
        output_root.mkdir()
        aggregate = create_output_lease(output_root, suffix=".mp4")
        derived = tuple(
            (segment.segment_id, create_output_lease(output_root, suffix=".mp4"))
            for segment in plan.segments
        )
        invocations: list[MediaProcessInvocation] = []
        samples: list[int] = []

        def fake_run(
            _self: SubprocessMediaRunner,
            invocation: MediaProcessInvocation,
            cancellation: object = None,
        ) -> ProcessCapture:
            del cancellation
            samples.append(_staged_media_count(sub_root))
            invocations.append(invocation)
            if invocation.tool == "ffmpeg":
                for lease in invocation.output_leases:
                    lease.path.write_bytes(b"synthetic encoded output")
                return ProcessCapture(
                    status=ProcessStatus.SUCCEEDED,
                    exit_code=0,
                    owned_output_bytes=len(b"synthetic encoded output"),
                    artifacts=invocation.output_leases,
                )
            ffmpeg_calls = sum(1 for item in invocations if item.tool == "ffmpeg")
            if ffmpeg_calls == 0:
                stdout = _probe_payload(missing_audio=True)
            elif ffmpeg_calls <= len(plan.segments):
                stdout = _probe_payload()
            else:
                stdout = _probe_payload(
                    video_frame_count=90,
                    seconds=3,
                    format_duration="3.000000",
                    video_duration="3.000000",
                    audio_duration="3.000000",
                )
            return ProcessCapture(
                status=ProcessStatus.SUCCEEDED,
                stdout=stdout,
                exit_code=0,
            )

        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(sub_root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )
        result = adapter.execute_plan(
            plan=plan,
            approval=approval,
            input_payloads=cast(tuple[tuple[str, Any], ...], entries),
            aggregate_output=aggregate,
            derived_outputs=derived,
        )
        aggregate.release()
        for _, lease in derived:
            lease.release()
        return result, samples

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        baseline, byte_samples = _run(
            root / "bytes",
            tuple(
                (segment.segment_id, payload)
                for segment, payload in zip(plan.segments, payloads, strict=True)
            ),
        )
        sources = tuple(_ReplaySource(payload) for payload in payloads)
        incremental, source_samples = _run(
            root / "src",
            tuple(
                (segment.segment_id, source)
                for segment, source in zip(plan.segments, sources, strict=True)
            ),
        )

    # Identical outputs over the identical cohort: the parity oracle at cohort scale.
    assert incremental.execution_fingerprint == baseline.execution_fingerprint
    # The bytes path really held the complete duplicate staging set at its peak...
    assert max(byte_samples) == len(payloads)
    # ...and the rolling assembly never held more than one source-backed staged copy.
    assert max(source_samples) <= 1
    assert all(source.stream_calls == 2 for source in sources)


class _ChunkedFloodSource:
    """A cooperating but overlong source: declares the right identity, writes 4-byte chunks
    forever, and polls cancellation between chunks. Only the adapter's watchdog stops it."""

    def __init__(self, payload: bytes) -> None:
        self._declared = (len(payload), _fp(payload))
        self.bytes_written = 0

    @property
    def byte_length(self) -> int:
        return self._declared[0]

    @property
    def content_fingerprint(self) -> str:
        return self._declared[1]

    def stream_into(
        self,
        destination: Path,
        *,
        cancellation: Any = None,
    ) -> tuple[int, str]:
        # Unbuffered so every chunk lands on disk where the watchdog's lstat can see it.
        with open(destination, "xb", buffering=0) as handle:
            for _ in range(10_000):
                if cancellation is not None and cancellation.is_cancelled():
                    raise AVTransportError("transport_cancelled")
                handle.write(b"XXXX")
                self.bytes_written += 4
        return self._declared


def test_a_source_that_writes_more_than_declared_is_refused_before_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter, aggregate, _ = _prepared_adapter(monkeypatch, root, missing_audio=False)
        # Writes double the declared bytes in one call and reports the declared identity.
        source = _ReplaySource(payload, replay=payload * 2, report_declared=True)

        with pytest.raises(AVMediaAdapterError, match="inspection_stale"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source),),
                aggregate_output=aggregate,
                derived_outputs=(),
            )

        assert source.stream_calls == 1
        assert aggregate.released
        assert tuple((root / "scratch").iterdir()) == ()


def test_the_watchdog_stops_a_flooding_source_within_one_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter, aggregate, _ = _prepared_adapter(monkeypatch, root, missing_audio=False)
        source = _ChunkedFloodSource(payload)

        with pytest.raises(AVMediaAdapterError, match="inspection_stale"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source),),
                aggregate_output=aggregate,
                derived_outputs=(),
            )

        # Tripped within one 4-byte chunk of the declared length, not at the 40 KB flood.
        assert source.bytes_written <= len(payload) + 8
        assert aggregate.released
        assert tuple((root / "scratch").iterdir()) == ()


def test_transform_time_restream_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(payload, missing_audio=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter, aggregate, derived = _prepared_adapter(monkeypatch, root, missing_audio=True)
        assert derived is not None
        # The verification pass (first stream) succeeds; the transform-time re-stream fails.
        source = _ReplaySource(
            payload,
            error=RuntimeError("foreign source failure"),
            fail_on_call=2,
        )

        with pytest.raises(AVMediaAdapterError, match="inspection_unavailable"):
            adapter.execute_plan(
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source),),
                aggregate_output=aggregate,
                derived_outputs=((plan.segments[0].segment_id, derived),),
            )

        assert source.stream_calls == 2
        assert aggregate.released
        assert derived.released
        assert tuple((root / "scratch").iterdir()) == ()


def test_m26_media_bridge_crops_exact_frame_and_sample_tail_then_releases_only_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_payload = b"synthetic-m26-original-artifact"
    derived_payload = b"synthetic-m26-derived-input"
    original_probe = _probe_payload(
        video_frame_count=362,
        format_duration="15.083333",
        video_duration="15.083333",
        audio_duration="15.000000",
        sample_rate=32_000,
        seconds=15,
        frame_rate=24,
    )
    derived_probe = _probe_payload(
        video_frame_count=360,
        format_duration="15.000000",
        video_duration="15.000000",
        audio_duration="15.000000",
        sample_rate=32_000,
        seconds=15,
        frame_rate=24,
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ffmpeg = (root / "ffmpeg.exe").resolve()
        ffprobe = (root / "ffprobe.exe").resolve()
        ffmpeg.write_bytes(b"synthetic ffmpeg")
        ffprobe.write_bytes(b"synthetic ffprobe")
        invocations: list[MediaProcessInvocation] = []
        probe_calls = 0

        def fake_run(
            _self: SubprocessMediaRunner,
            invocation: MediaProcessInvocation,
            cancellation: object = None,
        ) -> ProcessCapture:
            nonlocal probe_calls
            del cancellation
            invocations.append(invocation)
            if invocation.tool == "ffmpeg":
                invocation.output_leases[0].path.write_bytes(derived_payload)
                return ProcessCapture(
                    status=ProcessStatus.SUCCEEDED,
                    exit_code=0,
                    owned_output_bytes=len(derived_payload),
                    artifacts=invocation.output_leases,
                )
            probe_calls += 1
            return ProcessCapture(
                status=ProcessStatus.SUCCEEDED,
                stdout=original_probe if probe_calls == 1 else derived_probe,
                exit_code=0,
            )

        monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
        monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: 400,
        )
        source = _ReplaySource(original_payload)
        lease = adapter.derive_m26_input(
            segment_id="segment.m26.crop",
            original_receipt_fingerprint=_fp(b"m26-original-receipt"),
            original_source=source,
            contribution_frames=360,
            contribution_samples=480_000,
        )

        assert lease.original_descriptor.video is not None
        assert lease.original_descriptor.video.decoded_frame_count == 362
        assert lease.original_descriptor.audio is not None
        assert lease.original_descriptor.audio.decoded_sample_count == 480_000
        assert source.stream_calls == 1
        assert len(source.destinations) == 1
        assert not source.destinations[0].exists()
        crop = next(item for item in invocations if item.tool == "ffmpeg")
        arguments = " ".join(crop.argv)
        assert "trim=start_frame=0:end_frame=360" in arguments
        assert "atrim=start_sample=0:end_sample=480000" in arguments
        assert "libx264" in crop.codec_whitelist
        assert "aac" in crop.codec_whitelist
        assert crop.argv[crop.argv.index("-codec_whitelist") + 1] == ",".join(crop.codec_whitelist)
        assert "apad" not in arguments
        assert "tpad" not in arguments
        descriptor = lease.inspect(
            segment_id="segment.m26.crop",
            derived_receipt_fingerprint=_fp(b"m26-derived-receipt"),
        )
        assert descriptor.video is not None
        assert descriptor.video.decoded_frame_count == 360
        assert descriptor.audio is not None
        assert descriptor.audio.decoded_sample_count == 480_000
        assert descriptor.artifact_output_fingerprint == _fp(derived_payload)
        assert not lease._lease.released
        lease.release()
        assert lease._lease.released
        assert tuple((root / "scratch").iterdir()) == ()
