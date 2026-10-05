"""Closed pinned-FFprobe observations for complete fixed-profile private artifacts.

Parsing supplied envelopes is not native measurement or renderer qualification. The caller must
hold the exact artifact stable throughout extraction and independently measure its bytes/hash.
No receipt, plan, first-frame sample, or requested render flag substitutes for an observation.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from ..core.authoring_render_jobs import RenderJobLimits
from ..core.authoring_render_receipts import MeasuredRenderOutput, render_video_timing_fingerprint
from ..core.safe_paths import validate_regular_file
from .authoring_render_process import (
    PinnedRenderExecutable,
    ProcessControl,
    RenderProcessError,
    WindowsRenderProcessSession,
    _guard,
    _kernel,
)
from .segment_artifact_store import _msvcrt_open_osfhandle, _validated_directories

_MAX_ENVELOPE = 65536
_COLORS = {
    "color_range": "tv",
    "color_space": "bt709",
    "color_primaries": "bt709",
    "color_transfer": "bt709",
}
_VIDEO_KEYS = frozenset(
    {
        "index",
        "codec_name",
        "codec_type",
        "width",
        "height",
        "sample_aspect_ratio",
        "pix_fmt",
        *_COLORS,
        "r_frame_rate",
        "avg_frame_rate",
        "time_base",
        "start_pts",
        "duration_ts",
        "nb_frames",
    }
)
_AUDIO_KEYS = frozenset(
    {
        "index",
        "codec_name",
        "codec_type",
        "sample_rate",
        "channels",
        "r_frame_rate",
        "avg_frame_rate",
        "time_base",
        "start_pts",
        "duration_ts",
        "nb_frames",
    }
)
_VIDEO_ENVELOPES = frozenset({"metadata", "frames", "video_packets", *_COLORS})
_AUDIO_ENVELOPES = frozenset(
    {
        "audio_pts",
        "audio_dts",
        "audio_durations",
        "audio_frame_pts",
        "audio_frame_samples",
        "audio_frame_duration",
    }
)
_UNSIGNED = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
_DEFAULT_LIMITS = RenderJobLimits()
_MP4_HEADER = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
_METADATA_QUERY = (
    "-show_entries",
    "stream=index,codec_type,codec_name,width,height,pix_fmt,sample_aspect_ratio,"
    "color_range,color_space,color_primaries,color_transfer,r_frame_rate,avg_frame_rate,"
    "time_base,start_pts,duration_ts,nb_frames,sample_rate,channels:format=format_name,size:"
    "stream_disposition=:stream_tags=",
    "-of",
    "json",
)


def _field_query(stream: str, section: str, fields: str, side_fields: str = "") -> tuple[str, ...]:
    return (
        "-select_streams",
        stream,
        "-show_entries",
        f"{section}={fields}:{section}_side_data={side_fields}",
        "-of",
        "csv=p=0",
    )


def _queries(audio: bool) -> dict[str, tuple[str, ...]]:
    result = {
        "frames": _field_query("v:0", "frame", "pts,pkt_dts,duration"),
        "video_packets": _field_query("v:0", "packet", "pts,dts,duration"),
        **{field: _field_query("v:0", "frame", field) for field in _COLORS},
    }
    if audio:
        result.update(
            {
                "audio_pts": _field_query(
                    "a:0",
                    "packet",
                    "pts",
                    "side_data_type,skip_samples,discard_padding,skip_reason,discard_reason",
                ),
                "audio_dts": _field_query("a:0", "packet", "dts"),
                "audio_durations": _field_query("a:0", "packet", "duration"),
                "audio_frame_pts": _field_query("a:0", "frame", "pts"),
                "audio_frame_samples": _field_query("a:0", "frame", "nb_samples"),
                "audio_frame_duration": _field_query("a:0", "frame", "duration"),
            }
        )
    return result


class RenderProbeError(RuntimeError):
    def __init__(self) -> None:
        self.code = "output_invalid"
        super().__init__(self.code)


def _object(value: object, keys: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise RenderProbeError()
    return value


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise RenderProbeError()
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise RenderProbeError()


def _json(payload: bytes) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise RenderProbeError() from None


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RenderProbeError()
    return value


def _number_string(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not str or _UNSIGNED.fullmatch(value) is None:
        raise RenderProbeError()
    return _integer(int(value), minimum, maximum)


def _equals(subject: dict[str, Any], expected: dict[str, object]) -> None:
    for key, value in expected.items():
        actual = subject.get(key)
        if type(actual) is not type(value) or actual != value:
            raise RenderProbeError()


def _rows(payload: bytes) -> list[str]:
    try:
        # Accept the pinned native CRLF and LF writers only, not splitlines' other controls.
        text = payload.decode("ascii").replace("\r\n", "\n")
    except UnicodeError:
        raise RenderProbeError() from None
    if "\r" in text or not text.endswith("\n"):
        raise RenderProbeError()
    lines = text[:-1].split("\n")
    if not 1 <= len(lines) <= 7033 or any(not line for line in lines):
        raise RenderProbeError()
    return lines


def _expected_rows(payload: bytes, expected: list[str], *, empty_first_side: bool = False) -> None:
    rows = _rows(payload)
    # IMPORTANT: the pinned CSV writer emits one empty first-frame/packet side-data column.
    # Only this exact envelope is admitted; trimming commas or unknown columns hides corruption.
    if empty_first_side and rows and rows[0] == expected[0] + ",":
        rows[0] = expected[0]
    if rows != expected:
        raise RenderProbeError()


def _metadata(payload: bytes, byte_length: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
    metadata = _object(
        _json(payload), frozenset({"programs", "stream_groups", "streams", "format"})
    )
    if metadata["programs"] != [] or metadata["stream_groups"] != []:
        raise RenderProbeError()
    format_facts = _object(metadata["format"], frozenset({"format_name", "size"}))
    _equals(format_facts, {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "size": str(byte_length)})
    streams = metadata["streams"]
    if type(streams) is not list or not 1 <= len(streams) <= 2:
        raise RenderProbeError()
    video = _object(streams[0], _VIDEO_KEYS)
    _equals(
        video,
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "sample_aspect_ratio": "1:1",
            "pix_fmt": "yuv420p",
            **_COLORS,
            "r_frame_rate": "24/1",
            "avg_frame_rate": "24/1",
            "time_base": "1/24",
            "start_pts": 0,
        },
    )
    width = _integer(video["width"], 2, 1920)
    height = _integer(video["height"], 2, 1080)
    extent = _integer(video["duration_ts"], 1, 3600)
    if width % 2 or height % 2 or _number_string(video["nb_frames"], 1, 3600) != extent:
        raise RenderProbeError()
    audio = _object(streams[1], _AUDIO_KEYS) if len(streams) == 2 else None
    if audio is not None:
        # Audio's zero frame-rate sentinel is valid here only; no generic 0/0 coercion.
        _equals(
            audio,
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
                "duration_ts": extent * 2000,
            },
        )
        _number_string(audio["nb_frames"], 2, 7033)
    return video, audio


def parse_render_probe(
    envelopes: dict[str, bytes], *, output_fingerprint: str, byte_length: int
) -> MeasuredRenderOutput:
    """Validate every selected field, full packet sequence and complete decoded-frame sequence.

    This parser's container value requires the extraction boundary's independent MP4-brand check;
    FFprobe's shared mov/mp4 demuxer name alone does not distinguish those containers.
    """
    if (
        type(envelopes) is not dict
        or not 7 <= len(envelopes) <= 13
        or any(
            type(key) is not str or type(value) is not bytes or not 1 <= len(value) <= _MAX_ENVELOPE
            for key, value in envelopes.items()
        )
        or "metadata" not in envelopes
        or type(output_fingerprint) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", output_fingerprint) is None
    ):
        raise RenderProbeError()
    _integer(byte_length, 1, 512 * 1024 * 1024)
    video, audio = _metadata(envelopes["metadata"], byte_length)
    expected_keys = _VIDEO_ENVELOPES | (_AUDIO_ENVELOPES if audio is not None else frozenset())
    if set(envelopes) != expected_keys:
        raise RenderProbeError()
    extent = video["duration_ts"]
    timing_rows = [f"{n},{n},1" for n in range(extent)]
    _expected_rows(envelopes["frames"], timing_rows, empty_first_side=True)
    _expected_rows(envelopes["video_packets"], timing_rows)
    for field, value in _COLORS.items():
        _expected_rows(envelopes[field], [value] * extent, empty_first_side=True)
    effective_samples: int | None = None
    if audio is not None:
        samples = audio["duration_ts"]
        frame_count = (samples + 1023) // 1024
        if int(audio["nb_frames"]) != frame_count + 1:
            raise RenderProbeError()
        positions = [str(n * 1024) for n in range(frame_count)]
        durations = ["1024"] * (frame_count - 1) + [str(samples - (frame_count - 1) * 1024)]
        _expected_rows(envelopes["audio_pts"], ["-1024,Skip Samples,1024,0,0,0", *positions])
        _expected_rows(envelopes["audio_dts"], ["-1024", *positions], empty_first_side=True)
        _expected_rows(envelopes["audio_durations"], ["1024", *durations], empty_first_side=True)
        _expected_rows(envelopes["audio_frame_pts"], positions)
        _expected_rows(envelopes["audio_frame_samples"], ["1024"] * frame_count)
        _expected_rows(envelopes["audio_frame_duration"], durations)
        # CRITICAL: AAC decoding yields a padded final 1024-sample frame. Container duration
        # clips its playable tail; adding nb_samples alone lengthens the output past the NLE.
        # Prove that clip against every packet and frame, including the 1024-sample priming skip.
        effective_samples = sum(int(value) for value in _rows(envelopes["audio_frame_duration"]))
    return MeasuredRenderOutput(
        output_fingerprint=output_fingerprint,
        byte_length=byte_length,
        container="mp4",
        video_codec=video["codec_name"],
        video_streams=1,
        other_streams=0,
        width=video["width"],
        height=video["height"],
        frame_count=extent,
        frame_rate_num=24,
        frame_rate_den=1,
        time_base_num=1,
        time_base_den=24,
        pixel_format=video["pix_fmt"],
        pixel_aspect_num=1,
        pixel_aspect_den=1,
        color_range=video["color_range"],
        color_space=video["color_space"],
        color_primaries=video["color_primaries"],
        color_transfer=video["color_transfer"],
        video_timing_fingerprint=render_video_timing_fingerprint(
            tuple(range(extent)),
            tuple(range(extent)),
            (1,) * extent,
        ),
        audio_streams=int(audio is not None),
        audio_codec=audio["codec_name"] if audio else None,
        audio_sample_rate=int(audio["sample_rate"]) if audio else None,
        audio_channels=audio["channels"] if audio else None,
        audio_effective_samples=effective_samples,
    )


def measure_render_output(
    *,
    path: Path,
    probe: PinnedRenderExecutable,
    session: WindowsRenderProcessSession,
    control: ProcessControl,
    limits: RenderJobLimits = _DEFAULT_LIMITS,
    check_staging: Callable[[], object] | None = None,
) -> MeasuredRenderOutput:
    """Extract a fresh observation under one file identity and the caller's cumulative job.

    No process session is created here: rendering and all verification passes share its budgets.
    The trusted caller supplies a private stage/artifact locator, never an external user path.
    """
    if sys.platform != "win32" or type(limits) is not RenderJobLimits:
        raise RenderProbeError()
    try:
        _guard(control)
        admitted = validate_regular_file(path, maximum_bytes=limits.max_output_bytes)
        with ExitStack() as scope:
            scope.enter_context(_validated_directories(admitted.parent))
            kernel = _kernel()
            # CRITICAL: keep the output deny-write/delete handle through all decoder passes.
            # Rechecking size/mtime after separate opens misses same-stat replacement races.
            handle = kernel.CreateFileW(str(admitted), 0x80000000, 0x1, None, 3, 0x00200080, None)
            if not handle or handle == ctypes.c_void_p(-1).value:
                raise RenderProbeError()
            try:
                descriptor = _msvcrt_open_osfhandle(
                    int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
                )
            except BaseException:
                kernel.CloseHandle(handle)
                raise
            scope.callback(os.close, descriptor)
            metadata = os.fstat(descriptor)
            expected = admitted.lstat()
            identity = (metadata.st_dev, metadata.st_ino, metadata.st_size)
            if (
                identity != (expected.st_dev, expected.st_ino, expected.st_size)
                or metadata.st_nlink != 1
            ):
                raise RenderProbeError()
            _integer(metadata.st_size, 32, limits.max_output_bytes)
            if os.read(descriptor, 32) != _MP4_HEADER:
                raise RenderProbeError()

            def fingerprint() -> str:
                digest = hashlib.sha256()
                os.lseek(descriptor, 0, os.SEEK_SET)
                total = 0
                while True:
                    _guard(control)
                    block = os.read(descriptor, 1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > limits.max_output_bytes:
                        raise RenderProbeError()
                    digest.update(block)
                if total != metadata.st_size:
                    raise RenderProbeError()
                return "sha256:" + digest.hexdigest()

            def extract(query: tuple[str, ...]) -> bytes:
                measured = session.run(
                    probe,
                    (
                        "-v",
                        "error",
                        "-threads",
                        "2",
                        "-err_detect",
                        "explode",
                        "-max_alloc",
                        "67108864",
                        "-protocol_whitelist",
                        "file",
                        "-format_whitelist",
                        "mov",
                        "-enable_drefs",
                        "0",
                        "-use_absolute_path",
                        "0",
                        *query,
                        str(admitted),
                    ),
                    cwd=admitted.parent,
                    control=control,
                    maximum_stdout=limits.max_probe_bytes,
                    check_staging=check_staging,
                )
                # FFprobe can exit0 after logging decode errors. Never admit that partial result.
                if (
                    measured.exit_code
                    or measured.diagnostic_bytes
                    or measured.active_processes
                    or not measured.limits_verified
                ):
                    raise RenderProbeError()
                return measured.stdout

            before = fingerprint()
            values = {"metadata": extract(_METADATA_QUERY)}
            if len(values["metadata"]) > limits.max_probe_bytes:
                raise RenderProbeError()
            _, audio = _metadata(values["metadata"], metadata.st_size)
            for name, query in _queries(audio is not None).items():
                values[name] = extract(query)
            observed = parse_render_probe(
                values, output_fingerprint=before, byte_length=metadata.st_size
            )
            if fingerprint() != before:
                raise RenderProbeError()
            current = validate_regular_file(admitted, maximum_bytes=limits.max_output_bytes).lstat()
            if (current.st_dev, current.st_ino, current.st_size) != identity:
                raise RenderProbeError()
            _guard(control)
            return observed
    except (RenderProbeError, RenderProcessError):
        raise
    except Exception:
        raise RenderProbeError() from None
