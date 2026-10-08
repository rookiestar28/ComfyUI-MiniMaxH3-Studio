"""M25-17 exact file-backed authoring VIDEO fact qualification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from media_pin_fixture import semantic_media_pin

import comfyui_h3_context.adapters.av_reconstruction_media as media_module
from comfyui_h3_context.adapters.authoring_video_facts import (
    AUTHORING_VIDEO_FACTS_SCHEMA,
    AuthoringVideoFactsError,
    probe_authoring_video_facts,
)
from comfyui_h3_context.adapters.av_reconstruction_media import (
    AuthoringVideoProbePayload,
    AVMediaAdapterError,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.media_subprocess import (
    MediaProcessInvocation,
    ProcessCapture,
    ProcessStatus,
    SubprocessMediaRunner,
)
from comfyui_h3_context.core.composition_contract import MAX_LANDMARKS_PER_ASSET


def _fingerprint(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _probe_wire(*, frames: int = 3, audio: bool = True) -> bytes:
    video_frames = [
        {
            "media_type": "video",
            "stream_index": 0,
            "pts": pts,
            "pkt_dts": pts,
            "duration": duration,
        }
        for pts, duration in ((0, 3_000), (3_000, 6_000), (9_000, 3_000))[:frames]
    ]
    streams: list[dict[str, object]] = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 320,
            "height": 240,
            "pix_fmt": "yuv420p",
            "sample_aspect_ratio": "1:1",
            "color_range": "tv",
            "color_space": "bt709",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
            "time_base": "1/90000",
            "start_pts": 0,
        }
    ]
    audio_frames: list[dict[str, object]] = []
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_fmt": "fltp",
                "sample_rate": "48000",
                "channels": 1,
                "channel_layout": "mono",
                "time_base": "1/48000",
                "start_pts": 0,
            }
        )
        audio_frames = [
            {
                "media_type": "audio",
                "stream_index": 1,
                "pts": 0,
                "pkt_dts": 0,
                "duration": 1024,
                "nb_samples": 1024,
            },
            {
                "media_type": "audio",
                "stream_index": 1,
                "pts": 1024,
                "pkt_dts": 1024,
                "duration": 512,
                "nb_samples": 1024,
            },
        ]
    return json.dumps(
        {
            "streams": streams,
            "frames": [*video_frames, *audio_frames],
            "programs": [],
            "stream_groups": [],
        },
        separators=(",", ":"),
    ).encode("utf-8")


class _FactsAdapter(QualifiedAVMediaAdapter):
    def __init__(self, payload: bytes, source: bytes = b"private-video") -> None:
        self.payload = payload
        self.source = source

    def probe_authoring_video_source(
        self,
        *,
        source_path: Path,
        deadline: float,
        cancellation: object | None = None,
    ) -> AuthoringVideoProbePayload:
        assert source_path.is_absolute()
        assert deadline > time.monotonic()
        assert cancellation is None
        return AuthoringVideoProbePayload(
            probe_payload=self.payload,
            content_fingerprint=_fingerprint(self.source),
            byte_length=len(self.source),
        )


def test_omitted_pixel_aspect_remains_unknown_without_changing_measured_facts(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")
    declared = probe_authoring_video_facts(
        source, _FactsAdapter(_probe_wire()), time.monotonic() + 5
    )
    wire = json.loads(_probe_wire())
    del wire["streams"][0]["sample_aspect_ratio"]
    unknown = probe_authoring_video_facts(
        source, _FactsAdapter(json.dumps(wire).encode()), time.monotonic() + 5
    )
    assert unknown.schema_version == "h3.authoring.video_source_facts.v2"
    assert unknown.pixel_aspect_ratio is None
    assert unknown.to_wire() == {**declared.to_wire(), "pixel_aspect_ratio": None}
    assert declared.pixel_aspect_ratio is not None
    assert declared.pixel_aspect_ratio.to_wire() == {"num": 1, "den": 1}


def test_probe_returns_complete_exact_vfr_and_measured_audio_facts(tmp_path: Path) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")

    facts = probe_authoring_video_facts(
        source,
        _FactsAdapter(_probe_wire()),
        time.monotonic() + 5.0,
    )

    assert facts.schema_version == AUTHORING_VIDEO_FACTS_SCHEMA
    assert facts.content_fingerprint == _fingerprint(b"private-video")
    assert facts.byte_length == len(b"private-video")
    assert (facts.width, facts.height, facts.pixel_format) == (320, 240, "yuv420p")
    assert facts.pixel_aspect_ratio is not None
    assert facts.pixel_aspect_ratio.to_wire() == {"num": 1, "den": 1}
    assert facts.source_time_base.to_wire() == {"num": 1, "den": 90_000}
    assert (
        facts.color_range,
        facts.color_space,
        facts.color_primaries,
        facts.color_transfer,
    ) == ("tv", "bt709", "bt709", "bt709")
    assert facts.frame_count == 3
    assert [item.to_wire() for item in facts.landmarks] == [
        {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 3_000},
        {"frame_index": 1, "pts": 3_000, "dts": 3_000, "duration_ticks": 6_000},
        {"frame_index": 2, "pts": 9_000, "dts": 9_000, "duration_ticks": 3_000},
    ]
    assert facts.embedded_audio.to_wire() == {
        "disposition": "present_bound",
        "sample_rate": 48_000,
        "channels": 1,
        "channel_layout": "mono",
        "time_base": {"num": 1, "den": 48_000},
        "sample_count": 1_536,
    }
    assert "private-video" not in repr(facts)


def test_probe_admits_an_exact_video_without_embedded_audio(tmp_path: Path) -> None:
    source = (tmp_path / "silent.mp4").resolve()
    source.write_bytes(b"private-video")

    facts = probe_authoring_video_facts(
        source,
        _FactsAdapter(_probe_wire(audio=False)),
        time.monotonic() + 5.0,
    )

    assert facts.embedded_audio.to_wire() == {
        "disposition": "absent",
        "sample_rate": None,
        "channels": None,
        "channel_layout": None,
        "time_base": None,
        "sample_count": None,
    }


def test_probe_admits_only_the_empty_ffprobe_side_data_suppression_marker(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")
    wire = cast(dict[str, object], json.loads(_probe_wire()))
    frames = cast(list[dict[str, object]], wire["frames"])
    frames[0]["side_data_list"] = [{}]

    facts = probe_authoring_video_facts(
        source,
        _FactsAdapter(json.dumps(wire, separators=(",", ":")).encode()),
        time.monotonic() + 5.0,
    )

    assert facts.frame_count == 3
    assert "side_data" not in repr(facts)


@pytest.mark.parametrize(
    ("marker", "code"),
    (
        ([], "video_facts_invalid"),
        ([{"side_data_type": "untrusted"}], "video_facts_invalid"),
        ([{}] * 17, "resource_limit"),
    ),
)
def test_probe_rejects_malformed_value_bearing_or_overflowing_side_data_markers(
    tmp_path: Path,
    marker: object,
    code: str,
) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")
    wire = cast(dict[str, object], json.loads(_probe_wire()))
    frames = cast(list[dict[str, object]], wire["frames"])
    frames[0]["side_data_list"] = marker

    with pytest.raises(AuthoringVideoFactsError) as caught:
        probe_authoring_video_facts(
            source,
            _FactsAdapter(json.dumps(wire, separators=(",", ":")).encode()),
            time.monotonic() + 5.0,
        )

    assert caught.value.code == code


@pytest.mark.parametrize(
    "payload",
    (
        b"not-json",
        b'{"streams":[],"streams":[],"frames":[]}',
        b"[]",
    ),
)
def test_malformed_probe_payload_is_a_content_free_typed_blocker(
    tmp_path: Path, payload: bytes
) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")

    with pytest.raises(AuthoringVideoFactsError) as caught:
        probe_authoring_video_facts(
            source,
            _FactsAdapter(payload),
            time.monotonic() + 5.0,
        )

    assert caught.value.code == "video_facts_invalid"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda wire: wire.update({"private_path": "must-not-pass"}), "video_facts_invalid"),
        (
            lambda wire: cast(list[dict[str, object]], wire["streams"])[0].pop("time_base"),
            "video_facts_unavailable",
        ),
        (
            lambda wire: cast(list[dict[str, object]], wire["streams"])[0].update(
                {"sample_aspect_ratio": "0:1"}
            ),
            "video_facts_unsupported",
        ),
        (
            lambda wire: cast(list[dict[str, object]], wire["streams"])[0].update(
                {"color_space": "bt2020nc"}
            ),
            "video_facts_unsupported",
        ),
        (
            lambda wire: cast(list[dict[str, object]], wire["frames"])[0].pop("pts"),
            "video_facts_unavailable",
        ),
        (
            lambda wire: cast(list[dict[str, object]], wire["frames"])[0].update({"pts": 1}),
            "video_facts_unsupported",
        ),
        (
            lambda wire: cast(list[dict[str, object]], wire["frames"])[1].update({"pts": 0}),
            "video_facts_unsupported",
        ),
        (
            lambda wire: cast(list[dict[str, object]], wire["frames"])[0].update({"duration": 0}),
            "video_facts_unsupported",
        ),
        (
            lambda wire: cast(list[dict[str, object]], wire["streams"])[1].update(
                {"sample_rate": "44100"}
            ),
            "video_facts_unsupported",
        ),
        (
            lambda wire: cast(list[dict[str, object]], wire["frames"])[3].update(
                {"duration": 1000}
            ),
            "video_facts_unsupported",
        ),
    ],
)
def test_probe_fails_closed_without_fabricating_source_facts(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], object],
    code: str,
) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")
    wire = cast(dict[str, object], json.loads(_probe_wire()))
    mutate(wire)

    with pytest.raises(AuthoringVideoFactsError) as caught:
        probe_authoring_video_facts(
            source,
            _FactsAdapter(json.dumps(wire, separators=(",", ":")).encode()),
            time.monotonic() + 5.0,
        )

    assert caught.value.code == code
    assert str(caught.value) == code
    assert "private" not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_probe_rejects_more_frames_than_the_public_landmark_ceiling(tmp_path: Path) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")
    wire = cast(dict[str, object], json.loads(_probe_wire()))
    frames = cast(list[dict[str, object]], wire["frames"])
    audio_frames = [row for row in frames if row["media_type"] == "audio"]
    wire["frames"] = [
        {
            "media_type": "video",
            "stream_index": 0,
            "pts": index * 1000,
            "pkt_dts": index * 1000,
            "duration": 1000,
        }
        for index in range(MAX_LANDMARKS_PER_ASSET + 1)
    ] + audio_frames

    with pytest.raises(AuthoringVideoFactsError) as caught:
        probe_authoring_video_facts(
            source,
            _FactsAdapter(json.dumps(wire, separators=(",", ":")).encode()),
            time.monotonic() + 5.0,
        )

    assert caught.value.code == "resource_limit"


@contextmanager
def _accepted_executable_pin(_path: Path, _sha256: str) -> Iterator[None]:
    yield


def test_adapter_stages_pins_probes_and_cleans_private_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_body = b"private-video"
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(source_body)
    scratch = (tmp_path / "scratch").resolve()
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"qualified")
    ffprobe.write_bytes(b"qualified")
    observed: list[MediaProcessInvocation] = []

    def fake_run(
        _self: SubprocessMediaRunner,
        invocation: MediaProcessInvocation,
        cancellation: object = None,
    ) -> ProcessCapture:
        del cancellation
        observed.append(invocation)
        staged = Path(invocation.argv[-1])
        assert staged != source
        assert staged.read_bytes() == source_body
        return ProcessCapture(
            status=ProcessStatus.SUCCEEDED,
            stdout=_probe_wire(),
            exit_code=0,
        )

    monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_executable_pin)
    monkeypatch.setattr(media_module, "_pin_regular_media", semantic_media_pin)
    monkeypatch.setattr(SubprocessMediaRunner, "run", fake_run)
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=scratch,
        clock_ms=lambda: 1,
    )

    payload = adapter.probe_authoring_video_source(
        source_path=source,
        deadline=time.monotonic() + 5.0,
    )

    assert payload.content_fingerprint == _fingerprint(source_body)
    assert payload.byte_length == len(source_body)
    assert payload.probe_payload == _probe_wire()
    assert len(observed) == 1
    invocation = observed[0]
    assert invocation.executable == str(ffprobe)
    assert invocation.argv[0] == str(ffprobe)
    assert "-nostdin" not in invocation.argv
    show_entries = invocation.argv[invocation.argv.index("-show_entries") + 1]
    assert "sample_aspect_ratio" in show_entries
    assert "frame=media_type,stream_index,pts,pkt_dts,duration,nb_samples" in show_entries
    assert tuple(scratch.iterdir()) == ()
    assert str(source) not in repr(payload)


@pytest.mark.parametrize(
    ("capture", "code"),
    [
        (
            ProcessCapture(status=ProcessStatus.FAILED, stderr=b"private path", exit_code=1),
            "media_process_failed",
        ),
        (
            ProcessCapture(
                status=ProcessStatus.CLEANUP_FAILED,
                exit_code=1,
                cleanup_succeeded=False,
                reaped=False,
            ),
            "cleanup_failed",
        ),
    ],
)
def test_adapter_process_failure_is_content_free_and_releases_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: ProcessCapture,
    code: str,
) -> None:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"private-video")
    scratch = (tmp_path / "scratch").resolve()
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffprobe = (tmp_path / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"qualified")
    ffprobe.write_bytes(b"qualified")

    monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_executable_pin)
    monkeypatch.setattr(media_module, "_pin_regular_media", semantic_media_pin)
    monkeypatch.setattr(SubprocessMediaRunner, "run", lambda *_args, **_kwargs: capture)
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=scratch,
        clock_ms=lambda: 1,
    )

    with pytest.raises(AVMediaAdapterError) as caught:
        adapter.probe_authoring_video_source(
            source_path=source,
            deadline=time.monotonic() + 5.0,
        )

    assert caught.value.code == code
    assert str(source) not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert tuple(scratch.iterdir()) == ()


def test_adapter_rejects_source_over_budget_before_process_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (tmp_path / "large.mp4").resolve()
    with source.open("wb") as handle:
        handle.seek(64 * 1024 * 1024)
        handle.write(b"x")
    scratch = (tmp_path / "scratch").resolve()
    adapter = object.__new__(QualifiedAVMediaAdapter)
    adapter._scratch_root = scratch
    scratch.mkdir()
    adapter._process_slots = threading.BoundedSemaphore(1)
    adapter._runner = type(
        "ForbiddenRunner", (), {"run": lambda *_args, **_kwargs: pytest.fail("spawned")}
    )()

    with pytest.raises(AVMediaAdapterError) as caught:
        adapter.probe_authoring_video_source(
            source_path=source,
            deadline=time.monotonic() + 5.0,
        )

    assert caught.value.code == "resource_limit"
    assert tuple(scratch.iterdir()) == ()


def test_real_qualified_encoded_video_probe_when_tools_are_explicitly_supplied(
    tmp_path: Path,
) -> None:
    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg = Path(ffmpeg_value).resolve(strict=True)
    ffprobe = Path(ffprobe_value).resolve(strict=True)
    source = (tmp_path / "actual-encoded.mp4").resolve()
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
            "testsrc2=size=320x240:rate=12:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-bf",
            "0",
            # IMPORTANT: output metadata flags alone do not write all H.264 VUI fields; without
            # setparams ffprobe omits primaries/transfer and the qualified color facts fail closed.
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
            "-shortest",
            "-y",
            str(source),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    scratch = (tmp_path / "scratch").resolve()
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=scratch,
        clock_ms=lambda: 1,
    )

    facts = probe_authoring_video_facts(
        source,
        adapter,
        time.monotonic() + 30.0,
    )

    assert facts.frame_count == 24
    assert len(facts.landmarks) == 24
    assert facts.landmarks[0].pts == 0
    assert facts.embedded_audio.disposition == "present_bound"
    assert facts.embedded_audio.sample_rate == 48_000
    assert facts.embedded_audio.sample_count == 96_000
    assert tuple(scratch.iterdir()) == ()
