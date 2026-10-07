"""Synthetic closed-envelope tests; native qualification is recorded separately."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def probe_module() -> Any:
    return importlib.import_module("comfyui_h3_context.adapters.authoring_render_probe")


def envelopes(extent: int = 48, *, audio: bool = True) -> dict[str, bytes]:
    video = {
        "index": 0,
        "codec_name": "h264",
        "codec_type": "video",
        "width": 64,
        "height": 64,
        "sample_aspect_ratio": "1:1",
        "pix_fmt": "yuv420p",
        "color_range": "tv",
        "color_space": "bt709",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "r_frame_rate": "24/1",
        "avg_frame_rate": "24/1",
        "time_base": "1/24",
        "start_pts": 0,
        "duration_ts": extent,
        "nb_frames": str(extent),
    }
    streams: list[dict[str, object]] = [video]
    samples = extent * 2000
    packets = (samples + 1023) // 1024
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_name": "aac",
                "codec_type": "audio",
                "sample_rate": "48000",
                "channels": 1,
                "r_frame_rate": "0/0",
                "avg_frame_rate": "0/0",
                "time_base": "1/48000",
                "start_pts": 0,
                "duration_ts": samples,
                "nb_frames": str(packets + 1),
            }
        )

    def rows(values: Sequence[object], *, side: bool = False) -> bytes:
        return "".join(
            f"{value}{',' if side and index == 0 else ''}\r\n" for index, value in enumerate(values)
        ).encode("ascii")

    result = {
        "metadata": json.dumps(
            {
                "programs": [],
                "stream_groups": [],
                "streams": streams,
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "size": "100000"},
            }
        ).encode(),
        "frames": rows([f"{n},{n},1" for n in range(extent)], side=True),
        "video_packets": rows([f"{n},{n},1" for n in range(extent)]),
    }
    for field in ("color_range", "color_space", "color_primaries", "color_transfer"):
        result[field] = rows([video[field]] * extent, side=True)
    if audio:
        positions = [n * 1024 for n in range(packets)]
        durations = [1024] * (packets - 1) + [samples - positions[-1]]
        result.update(
            {
                "audio_pts": b"-1024,Skip Samples,1024,0,0,0\r\n" + rows(positions),
                "audio_dts": rows([-1024, *positions], side=True),
                "audio_durations": rows([1024, *durations], side=True),
                "audio_frame_pts": rows(positions),
                "audio_frame_samples": rows([1024] * packets),
                "audio_frame_duration": rows(durations),
            }
        )
    return result


def parse(values: dict[str, bytes]) -> Any:
    return probe_module().parse_render_probe(
        values, output_fingerprint="sha256:" + "1" * 64, byte_length=100000
    )


@pytest.mark.parametrize("extent", [1, 2, 48, 3600])
@pytest.mark.parametrize("audio", [False, True])
def test_complete_probe_bounds_and_effective_samples(extent: int, audio: bool) -> None:
    values = envelopes(extent, audio=audio)
    assert all(len(value) <= 65536 for value in values.values())
    observed = parse(values)
    assert observed.frame_count == extent
    assert observed.audio_effective_samples == (extent * 2000 if audio else None)


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("root", "unknown", []),
        ("root", "programs", [{}]),
        ("format", "filename", "private-sentinel"),
        ("video", "unknown", 1),
        ("video", "r_frame_rate", "0/0"),
        ("video", "duration_ts", True),
        ("video", "nb_frames", "048"),
        ("video", "color_primaries", "unknown"),
        ("audio", "color_space", "bt709"),
        ("audio", "r_frame_rate", "24/1"),
        ("audio", "sample_rate", 48000),
        ("audio", "duration_ts", 95999),
    ],
)
def test_unknown_keys_and_wrong_stream_sentinels_rejected(
    target: str, key: str, value: object
) -> None:
    values = envelopes()
    metadata = json.loads(values["metadata"])
    subject = {
        "root": metadata,
        "format": metadata["format"],
        "video": metadata["streams"][0],
        "audio": metadata["streams"][1],
    }[target]
    subject[key] = value
    values["metadata"] = json.dumps(metadata).encode()
    with pytest.raises(probe_module().RenderProbeError, match="output_invalid") as caught:
        parse(values)
    assert "private-sentinel" not in str(caught.value)


@pytest.mark.parametrize(
    "field,payload",
    [
        ("frames", b"0,0,1,\r\n"),
        ("frames", b"0,0,1,unknown\r\n"),
        ("frames", b"0,0,1,\r\n\r\n"),
        ("video_packets", b"0,1,1\r\n"),
        ("color_transfer", b"bt709\r\n"),
        ("color_range", b"pc\r\n" * 48),
        ("audio_pts", b"-1024,Skip Samples,0,0,0,0\r\n"),
        ("audio_dts", b"0\r\n"),
        ("audio_frame_samples", b"0\r\n" * 94),
        ("audio_frame_duration", b"1024\r\n" * 94),
        ("metadata", b'{"streams":[],"streams":[]}'),
        ("metadata", b"{}" * 40000),
    ],
    ids=[
        "frame-short",
        "frame-extra",
        "frame-empty",
        "packet-dts",
        "color-short",
        "wrong-range",
        "skip-wrong",
        "audio-dts-short",
        "audio-samples-zero",
        "audio-tail",
        "duplicate-json",
        "envelope-overflow",
    ],
)
def test_incomplete_or_unrecognized_complete_artifact_evidence_rejected(
    field: str, payload: bytes
) -> None:
    values = envelopes()
    values[field] = payload
    with pytest.raises(probe_module().RenderProbeError, match="output_invalid"):
        parse(values)


def test_missing_color_metadata_never_inferred_from_other_fields() -> None:
    values = envelopes()
    metadata = json.loads(values["metadata"])
    del metadata["streams"][0]["color_primaries"]
    values["metadata"] = json.dumps(metadata).encode()
    with pytest.raises(probe_module().RenderProbeError, match="output_invalid"):
        parse(values)


def test_silent_output_must_not_carry_audio_envelopes() -> None:
    values = envelopes(audio=False)
    values["audio_pts"] = b""
    with pytest.raises(probe_module().RenderProbeError, match="output_invalid"):
        parse(values)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only private file enforcement")
@pytest.mark.parametrize("fault", ["none", "diagnostic", "exit", "not_mp4", "active"])
def test_private_extraction_pins_file_and_refuses_partial_process_results(
    tmp_path: Path, fault: str
) -> None:
    body = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"synthetic-not-real-media"
    path = tmp_path / "output.mp4"
    path.write_bytes(body if fault != "not_mp4" else b"x" * len(body))
    values = envelopes()
    metadata = json.loads(values["metadata"])
    metadata["format"]["size"] = str(len(body))
    values["metadata"] = json.dumps(metadata).encode()
    responses = iter(values.values())
    calls: list[bool] = []

    class SyntheticExtractionSession:
        def run(self, *_args: object, **_kwargs: object) -> Any:
            calls.append(True)
            with pytest.raises(PermissionError):
                path.write_bytes(b"replacement")
            return SimpleNamespace(
                stdout=next(responses),
                exit_code=int(fault == "exit"),
                diagnostic_bytes=int(fault == "diagnostic"),
                active_processes=int(fault == "active"),
                limits_verified=True,
            )

    control = SimpleNamespace(deadline=time.monotonic() + 10, is_cancelled=lambda: False)
    if fault == "none":
        observed = probe_module().measure_render_output(
            path=path, probe=object(), session=SyntheticExtractionSession(), control=control
        )
        assert observed.output_fingerprint == "sha256:" + hashlib.sha256(body).hexdigest()
        assert len(calls) == 13
        assert path.read_bytes() == body
    else:
        with pytest.raises(probe_module().RenderProbeError):
            probe_module().measure_render_output(
                path=path, probe=object(), session=SyntheticExtractionSession(), control=control
            )
        assert len(calls) == (0 if fault == "not_mp4" else 1)
    # Closing extraction releases only its own denial handle, including every failure path.
    path.write_bytes(b"after-extraction")


@pytest.mark.parametrize("field", ["frames", "video_packets", "color_primaries", "audio_frame_pts"])
def test_last_observation_in_maximum_extent_is_not_sampled_away(field: str) -> None:
    values = envelopes(3600)
    rows = values[field].splitlines(keepends=True)
    rows[-1] = b"invalid-last-observation\r\n"
    values[field] = b"".join(rows)
    with pytest.raises(probe_module().RenderProbeError):
        parse(values)
