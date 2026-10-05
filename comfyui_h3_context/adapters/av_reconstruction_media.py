"""Exact-path bounded FFmpeg inspection adapter for M17-11 private media."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import threading
import time
import wave
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import cast

from ..core.av_reconstruction import (
    AVAudioDescriptor,
    AVMediaDescriptor,
    AVOperation,
    AVRational,
    AVReconstructionApproval,
    AVReconstructionError,
    AVReconstructionPlan,
    AVSegmentPlan,
    AVVideoDescriptor,
    qualified_av_limits,
    qualified_av_target_profile,
    qualified_ffmpeg_capability,
)
from ..core.av_seam_policy import (
    TARGET_FRAMES_PER_SECOND,
    AVSeamApproval,
    AVSeamAudioPolicy,
    AVSeamOperation,
    AVSeamPlan,
    AVSeamPolicyError,
    qualified_seam_capability,
)
from ..core.canonical import canonical_fingerprint
from ..core.errors import MediaProcessError
from ..core.safe_paths import UnsafePathError, ensure_directory, validate_regular_file
from .av_reconstruction_store import PrivateAVReconstructionStore
from .av_reconstruction_transport import AVSegmentSource, AVTransportError
from .executable_admission import (
    ExecutableAdmissionError,
    pin_exact_executable,
)
from .executable_admission import windows_open_read_pin as _windows_open_read_pin
from .media_subprocess import (
    CancellationProbe,
    MediaProcessInvocation,
    OwnedOutputLease,
    ProcessStatus,
    SubprocessMediaRunner,
    create_output_lease,
)
from .segment_artifact_store import (
    ArtifactStoreError,
    _identity,
    _is_link_or_reparse,
    _validated_directories,
    _write_new_file,
)

_MAX_PROBE_STREAMS = 16
_MAX_PROBE_FRAMES = 131_072
_MAX_TEXT = 256
_ROOT_REQUIRED_FIELDS = frozenset({"format", "streams", "frames"})
_ROOT_FIELDS = _ROOT_REQUIRED_FIELDS | {"programs", "stream_groups"}
_FORMAT_FIELDS = frozenset({"format_name", "duration"})
_VIDEO_STREAM_FIELDS = frozenset(
    {
        "index",
        "codec_type",
        "codec_name",
        "width",
        "height",
        "pix_fmt",
        "color_range",
        "color_space",
        "color_primaries",
        "color_transfer",
        "r_frame_rate",
        "avg_frame_rate",
        "time_base",
        "start_time",
        "duration",
        "side_data_list",
    }
)
_AUDIO_STREAM_FIELDS = frozenset(
    {
        "index",
        "codec_type",
        "codec_name",
        "sample_fmt",
        "sample_rate",
        "channels",
        "channel_layout",
        "time_base",
        "start_time",
        "duration",
        "r_frame_rate",
        "avg_frame_rate",
        "initial_padding",
        "trailing_padding",
        "seek_preroll",
    }
)
_VIDEO_FRAME_FIELDS = frozenset(
    {"media_type", "stream_index", "best_effort_timestamp", "duration", "side_data_list"}
)
_AUDIO_FRAME_FIELDS = _VIDEO_FRAME_FIELDS | {"nb_samples"}
_FRAME_SIDE_DATA_FIELDS = frozenset({"side_data_type", "skip_samples", "discard_padding"})
_VIDEO_ACCOUNTING_NEUTRAL_SIDE_DATA = frozenset({"H.26[45] User Data Unregistered SEI message"})
# CRITICAL: this limit is capability-wide, not adapter-instance-local; otherwise callers can
# bypass the qualified process ceiling by constructing another adapter or scratch root.
_PROCESS_SLOTS = threading.BoundedSemaphore(qualified_av_limits().process_concurrency)
_PREVIEW_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_PREVIEW_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_PREVIEW_MAX_DURATION_MS = 30_000
# IMPORTANT: the two encodes a user waits for -- the import normalization here and the
# derivative generator's proxy -- take their thread count from this one rule, and the
# generator's profile fingerprint, which keys its cache, names the rule and its value. The
# cap leaves a many-core host its other processors while one encode runs.
# With more than one thread the rate-capped proxy encode is not reproducible: one source
# gives different bytes on every run (measured; it is reproducible on one thread, and on
# four when the rate control has no cap). A body's fingerprint is the digest of its own
# bytes, so identity stays exact; what must never be assumed is that a body generated again
# equals the one generated before. The normalization has no rate cap and gives the same
# bytes on every run at each count from one to four.
ENCODER_THREAD_RULE = "min_cpu_count_4"


def encoder_thread_count() -> int:
    """Threads for one encode on the user's path: the processor count, four at most."""

    return min(os.cpu_count() or 1, 4)


class AVMediaAdapterError(RuntimeError):
    """Content-free qualified-adapter failure with one closed code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AuthoringVideoProbePayload:
    """Bounded ffprobe bytes paired only with facts about the exact staged source bytes."""

    probe_payload: bytes = dataclass_field(default=b"", repr=False)
    content_fingerprint: str = ""
    byte_length: int = 0

    def __post_init__(self) -> None:
        digest = self.content_fingerprint.removeprefix("sha256:")
        if (
            not isinstance(self.probe_payload, bytes)
            or not self.probe_payload
            or type(self.content_fingerprint) is not str
            or not self.content_fingerprint.startswith("sha256:")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or type(self.byte_length) is not int
            or self.byte_length <= 0
        ):
            raise AVMediaAdapterError("media_output_invalid")


@dataclass(frozen=True, slots=True)
class AVMediaExecutionResult:
    """Process-local verified execution facts; output leases remain caller-owned."""

    execution_fingerprint: str
    aggregate_descriptor: AVMediaDescriptor
    derived_descriptors: tuple[AVMediaDescriptor, ...]

    def __post_init__(self) -> None:
        if (
            type(self.execution_fingerprint) is not str
            or not self.execution_fingerprint.startswith("sha256:")
            or type(self.aggregate_descriptor) is not AVMediaDescriptor
            or type(self.derived_descriptors) is not tuple
            or not all(type(item) is AVMediaDescriptor for item in self.derived_descriptors)
        ):
            raise AVMediaAdapterError("media_output_invalid")


@dataclass(frozen=True, slots=True)
class AVSeamExecutionResult:
    """Process-local verified seam execution facts; the lease stays caller-owned."""

    execution_fingerprint: str
    aggregate_descriptor: AVMediaDescriptor

    def __post_init__(self) -> None:
        if (
            type(self.execution_fingerprint) is not str
            or not self.execution_fingerprint.startswith("sha256:")
            or type(self.aggregate_descriptor) is not AVMediaDescriptor
        ):
            raise AVMediaAdapterError("media_output_invalid")


def _exact_seam_seconds(frames: int) -> str:
    """Render a grid-aligned frame count as an exact decimal seconds literal."""

    quotient, remainder = divmod(frames, TARGET_FRAMES_PER_SECOND)
    if remainder == 0:
        return str(quotient)
    if remainder * 2 == TARGET_FRAMES_PER_SECOND:
        return f"{quotient}.5"
    raise AVMediaAdapterError("seam_duration_not_exact")


def _seam_filter_graph(seam_plan: AVSeamPlan) -> str:
    """Compile the seam aggregate filter graph from validated integers and enums.

    Every crossfade is decomposed into integer-exact regions (`split`+`trim` by
    frame index, `asplit`+`atrim` by sample index) so the only time literals the
    graph carries are grid-exact `xfade`/`acrossfade` durations at offset zero.
    No caller-supplied text enters the graph.
    """

    capability = qualified_seam_capability()
    filters: list[str] = []
    # CRITICAL: mixed direct/crossfade graphs also decode AAC tails. Bound every input before
    # joining; trimming only the final output moves later content and the intended seam region.
    for index, (frames, samples) in enumerate(
        zip(seam_plan.segment_emitted_frames, seam_plan.segment_emitted_samples, strict=True)
    ):
        filters.append(
            f"[{index}:v:0]trim=end_frame={frames},"
            f"setpts=N/({TARGET_FRAMES_PER_SECOND}*TB)[vi{index}]"
        )
        filters.append(f"[{index}:a:0]atrim=end_sample={samples},asetpts=N/SR/TB[ai{index}]")
    accumulated_video = "vi0"
    accumulated_audio = "ai0"
    accumulated_frames = seam_plan.segment_emitted_frames[0]
    accumulated_samples = seam_plan.segment_emitted_samples[0]
    for seam in seam_plan.seams:
        index = seam.boundary_index
        next_video = f"vi{index + 1}"
        next_audio = f"ai{index + 1}"
        next_frames = seam_plan.segment_emitted_frames[index + 1]
        next_samples = seam_plan.segment_emitted_samples[index + 1]
        if seam.operation is AVSeamOperation.DIRECT_JOIN:
            filters.append(f"[{accumulated_video}][{next_video}]concat=n=2:v=1:a=0[vj{index}]")
            filters.append(f"[{accumulated_audio}][{next_audio}]concat=n=2:v=0:a=1[aj{index}]")
            accumulated_video = f"vj{index}"
            accumulated_audio = f"aj{index}"
            accumulated_frames += next_frames
            accumulated_samples += next_samples
            continue
        overlap_frames = seam.overlap_frames
        overlap_samples = seam.overlap_samples
        duration = _exact_seam_seconds(overlap_frames)
        exclusive_frames = accumulated_frames - overlap_frames
        if (
            exclusive_frames < 1 or next_frames <= overlap_frames
        ):  # pragma: no cover - unreachable for validated plans (disjoint-region
            # and per-segment bound checks in AVSeamPlan); kept as defense in depth.
            raise AVMediaAdapterError("seam_graph_invalid")
        # CRITICAL: xfade rejects mixed timebases after concat; normalize every
        # blend region to exact AVTB before resetting its presentation timestamp.
        filters.append(f"[{accumulated_video}]split=2[vpa{index}][vpb{index}]")
        filters.append(
            f"[vpa{index}]trim=end_frame={exclusive_frames},"
            f"settb=AVTB,setpts=PTS-STARTPTS[vex{index}]"
        )
        filters.append(
            f"[vpb{index}]trim=start_frame={exclusive_frames},"
            f"settb=AVTB,setpts=PTS-STARTPTS[vtl{index}]"
        )
        filters.append(f"[{next_video}]split=2[vna{index}][vnb{index}]")
        filters.append(
            f"[vna{index}]trim=end_frame={overlap_frames},"
            f"settb=AVTB,setpts=PTS-STARTPTS[vhd{index}]"
        )
        filters.append(
            f"[vnb{index}]trim=start_frame={overlap_frames},"
            f"settb=AVTB,setpts=PTS-STARTPTS[vrs{index}]"
        )
        filters.append(
            f"[vtl{index}][vhd{index}]xfade=transition={capability.video_transition}:"
            f"duration={duration}:offset=0[vbl{index}]"
        )
        filters.append(f"[vex{index}][vbl{index}][vrs{index}]concat=n=3:v=1:a=0[vj{index}]")
        accumulated_video = f"vj{index}"
        exclusive_samples = accumulated_samples - overlap_samples
        if seam.audio_policy is AVSeamAudioPolicy.PREDECESSOR:
            filters.append(
                f"[{next_audio}]atrim=start_sample={overlap_samples},"
                f"asetpts=PTS-STARTPTS[ard{index}]"
            )
            filters.append(f"[{accumulated_audio}][ard{index}]concat=n=2:v=0:a=1[aj{index}]")
        elif seam.audio_policy is AVSeamAudioPolicy.SUCCESSOR:
            filters.append(
                f"[{accumulated_audio}]atrim=end_sample={exclusive_samples},"
                f"asetpts=PTS-STARTPTS[atr{index}]"
            )
            filters.append(f"[atr{index}][{next_audio}]concat=n=2:v=0:a=1[aj{index}]")
        elif seam.audio_policy is AVSeamAudioPolicy.BLEND:
            filters.append(f"[{accumulated_audio}]asplit=2[apa{index}][apb{index}]")
            filters.append(
                f"[apa{index}]atrim=end_sample={exclusive_samples},asetpts=PTS-STARTPTS[aex{index}]"
            )
            filters.append(
                f"[apb{index}]atrim=start_sample={exclusive_samples},"
                f"asetpts=PTS-STARTPTS[atl{index}]"
            )
            filters.append(f"[{next_audio}]asplit=2[ana{index}][anb{index}]")
            filters.append(
                f"[ana{index}]atrim=end_sample={overlap_samples},asetpts=PTS-STARTPTS[ahd{index}]"
            )
            filters.append(
                f"[anb{index}]atrim=start_sample={overlap_samples},asetpts=PTS-STARTPTS[ars{index}]"
            )
            filters.append(
                f"[atl{index}][ahd{index}]acrossfade=d={duration}:"
                f"c1={capability.audio_blend_curve}:c2={capability.audio_blend_curve}[abl{index}]"
            )
            filters.append(f"[aex{index}][abl{index}][ars{index}]concat=n=3:v=0:a=1[aj{index}]")
        else:  # pragma: no cover - closed enum revalidated by the seam plan
            raise AVMediaAdapterError("seam_graph_invalid")
        accumulated_audio = f"aj{index}"
        accumulated_frames += next_frames - overlap_frames
        accumulated_samples += next_samples - overlap_samples
    if (
        accumulated_frames != seam_plan.total_output_frames
        or accumulated_samples != seam_plan.total_output_samples
    ):
        raise AVMediaAdapterError("seam_graph_invalid")
    filters.append(
        f"[{accumulated_video}]trim=end_frame={seam_plan.total_output_frames},"
        "setpts=PTS-STARTPTS[v]"
    )
    filters.append(
        f"[{accumulated_audio}]atrim=end_sample={seam_plan.total_output_samples},"
        "asetpts=PTS-STARTPTS[a]"
    )
    return ";".join(filters)


@contextmanager
def _pin_exact_executable(path: Path, expected_sha256: str) -> Iterator[None]:
    """Pin and hash one exact Windows executable across process spawn/use."""

    # The admission itself lives in `executable_admission`, shared with media runtime discovery;
    # this wrapper keeps the adapter's closed error type and codes unchanged.
    try:
        with pin_exact_executable(path, expected_sha256, passthrough=(AVMediaAdapterError,)):
            yield
    except ExecutableAdmissionError as exc:
        raise AVMediaAdapterError(exc.code) from exc.__cause__


def _preview_io_remaining(deadline: float | None, clock: Callable[[], float]) -> None:
    if deadline is None:
        return
    if type(deadline) is not float or not callable(clock):
        raise AVMediaAdapterError("preview_deadline")
    try:
        remaining = deadline - clock()
    except Exception as exc:
        raise AVMediaAdapterError("preview_deadline") from exc
    if remaining <= 0:
        raise AVMediaAdapterError("preview_deadline")


@contextmanager
def _pin_regular_media(
    path: Path,
    maximum_bytes: int,
    *,
    deadline: float | None = None,
    cancellation: CancellationProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[tuple[int, str]]:
    """Hash and pin one staged output across its independent ffprobe read."""

    descriptor: int | None = None
    try:
        with _validated_directories(path.parent):
            admitted = validate_regular_file(path, maximum_bytes=maximum_bytes)
            before = admitted.lstat()
            if before.st_nlink != 1 or _is_link_or_reparse(admitted, before):
                raise AVMediaAdapterError("media_output_invalid")
            descriptor = _windows_open_read_pin(admitted)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _identity(opened) != _identity(before)
            ):
                raise AVMediaAdapterError("media_output_invalid")
            digest = hashlib.sha256()
            total = 0
            while True:
                _preview_io_remaining(deadline, clock)
                _raise_if_cancelled(cancellation)
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise AVMediaAdapterError("resource_limit")
                digest.update(chunk)
            fingerprint = "sha256:" + digest.hexdigest()
            _preview_io_remaining(deadline, clock)
            _raise_if_cancelled(cancellation)
            yield total, fingerprint
            final = admitted.lstat()
            if (
                final.st_nlink != 1
                or _is_link_or_reparse(admitted, final)
                or _identity(final) != _identity(opened)
                or _identity(os.fstat(descriptor)) != _identity(opened)
            ):
                raise AVMediaAdapterError("media_output_invalid")
    except AVMediaAdapterError:
        raise
    except (ArtifactStoreError, UnsafePathError, OSError, RuntimeError) as exc:
        raise AVMediaAdapterError("media_output_invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_media_body(
    path: Path,
    maximum_bytes: int,
    *,
    deadline: float,
    cancellation: CancellationProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bytearray, str]:
    """Read one pinned proxy into one bounded response accumulator under the work cutoff."""

    try:
        with _validated_directories(path.parent):
            admitted = validate_regular_file(path, maximum_bytes=maximum_bytes)
            before = admitted.lstat()
            if before.st_nlink != 1 or _is_link_or_reparse(admitted, before):
                raise AVMediaAdapterError("media_output_invalid")
            size = before.st_size
            if not 1 <= size <= maximum_bytes:
                raise AVMediaAdapterError("resource_limit")
            body = bytearray(size)
            digest = hashlib.sha256()
            with admitted.open("rb", buffering=0) as source:
                opened = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or _identity(opened) != _identity(before)
                ):
                    raise AVMediaAdapterError("media_output_invalid")
                offset = 0
                while offset < size:
                    _preview_io_remaining(deadline, clock)
                    _raise_if_cancelled(cancellation)
                    end = min(size, offset + 1024 * 1024)
                    view = memoryview(body)[offset:end]
                    count = source.readinto(view)
                    if count is None or count <= 0:
                        raise AVMediaAdapterError("media_output_invalid")
                    digest.update(view[:count])
                    offset += count
                _preview_io_remaining(deadline, clock)
                _raise_if_cancelled(cancellation)
                if source.read(1):
                    raise AVMediaAdapterError("resource_limit")
                after_handle = os.fstat(source.fileno())
            after_path = admitted.lstat()
            if (
                after_path.st_nlink != 1
                or _is_link_or_reparse(admitted, after_path)
                or _identity(after_handle) != _identity(opened)
                or _identity(after_path) != _identity(opened)
            ):
                raise AVMediaAdapterError("media_output_invalid")
            return body, "sha256:" + digest.hexdigest()
    except AVMediaAdapterError:
        raise
    except (ArtifactStoreError, UnsafePathError, OSError, RuntimeError) as exc:
        raise AVMediaAdapterError("media_output_invalid") from exc


def _decode_probe(payload: bytes) -> Mapping[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AVMediaAdapterError("inspection_invalid")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except AVMediaAdapterError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError) as exc:
        raise AVMediaAdapterError("inspection_invalid") from exc
    if type(value) is not dict:
        raise AVMediaAdapterError("inspection_invalid")
    return cast(Mapping[str, object], value)


def _mapping(value: object) -> Mapping[str, object]:
    if type(value) is not dict:
        raise AVMediaAdapterError("inspection_invalid")
    return cast(Mapping[str, object], value)


def _sequence(value: object, *, maximum: int) -> Sequence[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise AVMediaAdapterError("inspection_invalid")
    return value


def _require_members(
    value: Mapping[str, object],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
) -> None:
    members = frozenset(value)
    if not required.issubset(members) or not members.issubset(allowed):
        raise AVMediaAdapterError("inspection_invalid")


def _text(value: object, *, maximum: int = _MAX_TEXT) -> str:
    if type(value) is not str or not value or len(value) > maximum or "\x00" in value:
        raise AVMediaAdapterError("inspection_invalid")
    return value


def _integer(value: object, *, minimum: int = 0, maximum: int) -> int:
    if type(value) is int:
        result = value
    elif type(value) is str and value.isascii() and value.isdigit():
        result = int(value)
    else:
        raise AVMediaAdapterError("inspection_invalid")
    if not minimum <= result <= maximum:
        raise AVMediaAdapterError("inspection_invalid")
    return result


def _fraction(value: object, *, allow_zero: bool = True) -> Fraction:
    text = _text(value, maximum=64)
    try:
        result = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise AVMediaAdapterError("inspection_invalid") from exc
    if result < 0 or (not allow_zero and result == 0):
        raise AVMediaAdapterError("inspection_invalid")
    return result


def _rational(value: Fraction) -> AVRational:
    try:
        return AVRational(value.numerator, value.denominator)
    except AVReconstructionError as exc:
        raise AVMediaAdapterError("inspection_invalid") from exc


def _duration_matches(observed: Fraction, expected: Fraction, time_base: Fraction) -> bool:
    tolerance = max(time_base, Fraction(1, 1_000_000))
    return abs(observed - expected) <= tolerance


def _rotation(stream: Mapping[str, object]) -> int:
    raw = stream.get("side_data_list")
    if raw is None:
        return 0
    entries = _sequence(raw, maximum=8)
    rotations: list[int] = []
    for item in entries:
        side_data = _mapping(item)
        _require_members(
            side_data,
            allowed=frozenset({"rotation"}),
            required=frozenset({"rotation"}),
        )
        value = side_data["rotation"]
        if type(value) is int:
            rotation = value
        elif type(value) is str and value.lstrip("-").isdigit():
            rotation = int(value)
        else:
            raise AVMediaAdapterError("inspection_invalid")
        rotations.append(rotation % 360)
    if len(rotations) > 1 or (rotations and rotations[0] not in {0, 90, 180, 270}):
        raise AVMediaAdapterError("inspection_invalid")
    return 0 if not rotations else rotations[0]


def _validate_frame_side_data(frame: Mapping[str, object], *, media_type: str) -> None:
    raw = frame.get("side_data_list")
    if raw is None:
        return
    entries = _sequence(raw, maximum=8)
    for item in entries:
        side_data = _mapping(item)
        _require_members(
            side_data,
            allowed=_FRAME_SIDE_DATA_FIELDS,
            required=frozenset({"side_data_type"}),
        )
        side_data_type = _text(side_data["side_data_type"])
        for name in ("skip_samples", "discard_padding"):
            if name in side_data:
                _integer(side_data[name], maximum=1_000_000)
        if (
            media_type == "video"
            and side_data_type in _VIDEO_ACCOUNTING_NEUTRAL_SIDE_DATA
            and frozenset(side_data) == {"side_data_type"}
        ):
            continue
        # SECURITY: audio skip/discard and unqualified side data change accounting semantics.
        raise AVMediaAdapterError("inspection_invalid")


def _parse_descriptor(
    payload: bytes,
    *,
    segment_id: str,
    artifact_receipt_fingerprint: str,
    artifact_output_fingerprint: str,
    artifact_byte_length: int,
    inspected_at_ms: int,
    maximum_duration_ms: int | None = None,
) -> AVMediaDescriptor:
    root = _decode_probe(payload)
    _require_members(root, allowed=_ROOT_FIELDS, required=_ROOT_REQUIRED_FIELDS)
    for structural_name in ("programs", "stream_groups"):
        if structural_name in root:
            _sequence(root[structural_name], maximum=0)
    format_value = _mapping(root["format"])
    _require_members(format_value, allowed=_FORMAT_FIELDS, required=_FORMAT_FIELDS)
    format_names = tuple(
        item.strip().casefold()
        for item in _text(format_value.get("format_name"), maximum=512).split(",")
        if item.strip()
    )
    capability = qualified_ffmpeg_capability()
    container = next(
        (item for item in capability.input_containers if item in format_names),
        None,
    )
    if container is None:
        raise AVMediaAdapterError("inspection_invalid")
    format_duration = _fraction(format_value.get("duration"), allow_zero=False)
    limits = qualified_av_limits()
    duration_limit = (
        limits.max_duration_ms_per_segment if maximum_duration_ms is None else maximum_duration_ms
    )
    if format_duration * 1000 > duration_limit:
        raise AVMediaAdapterError("resource_limit")

    streams = _sequence(root["streams"], maximum=_MAX_PROBE_STREAMS)
    if not streams:
        raise AVMediaAdapterError("inspection_invalid")
    video_streams: list[Mapping[str, object]] = []
    audio_streams: list[Mapping[str, object]] = []
    stream_indexes: set[int] = set()
    for raw_stream in streams:
        stream = _mapping(raw_stream)
        stream_index = _integer(
            stream.get("index"),
            maximum=_MAX_PROBE_STREAMS - 1,
        )
        if stream_index in stream_indexes:
            raise AVMediaAdapterError("inspection_invalid")
        stream_indexes.add(stream_index)
        media_type = _text(stream.get("codec_type"), maximum=32).casefold()
        if media_type == "video":
            _require_members(
                stream,
                allowed=_VIDEO_STREAM_FIELDS,
                required=_VIDEO_STREAM_FIELDS - {"side_data_list"},
            )
            video_streams.append(stream)
        elif media_type == "audio":
            _require_members(
                stream,
                allowed=_AUDIO_STREAM_FIELDS,
                required=_AUDIO_STREAM_FIELDS
                - {"initial_padding", "trailing_padding", "seek_preroll"},
            )
            audio_streams.append(stream)
        else:
            raise AVMediaAdapterError("inspection_invalid")
    if len(video_streams) != 1 or len(audio_streams) > 1:
        raise AVMediaAdapterError("inspection_invalid")

    video_stream = video_streams[0]
    video_index = _integer(video_stream.get("index"), maximum=_MAX_PROBE_STREAMS - 1)
    video_codec = _text(video_stream.get("codec_name"))
    video_pixel_format = _text(video_stream.get("pix_fmt"))
    if (
        video_codec not in capability.video_decoders
        or video_pixel_format not in capability.input_pixel_formats
    ):
        raise AVMediaAdapterError("inspection_invalid")
    average_rate = _fraction(video_stream.get("avg_frame_rate"), allow_zero=False)
    real_rate = _fraction(video_stream.get("r_frame_rate"), allow_zero=False)
    if average_rate != real_rate or average_rate > limits.max_input_fps:
        raise AVMediaAdapterError("inspection_invalid")
    video_start = _fraction(video_stream.get("start_time"))
    video_time_base = _fraction(video_stream.get("time_base"), allow_zero=False)
    video_frame_ticks = Fraction(1, 1) / average_rate / video_time_base
    video_start_ticks = video_start / video_time_base
    if video_frame_ticks.denominator != 1 or video_start_ticks.denominator != 1:
        raise AVMediaAdapterError("inspection_invalid")

    audio_stream: Mapping[str, object] | None = None
    audio_index: int | None = None
    sample_rate: int | None = None
    audio_start: Fraction | None = None
    audio_time_base: Fraction | None = None
    audio_start_ticks: Fraction | None = None
    audio_codec: str | None = None
    audio_sample_format: str | None = None
    if audio_streams:
        audio_stream = audio_streams[0]
        audio_index = _integer(audio_stream.get("index"), maximum=_MAX_PROBE_STREAMS - 1)
        audio_codec = _text(audio_stream.get("codec_name"))
        audio_sample_format = _text(audio_stream.get("sample_fmt"))
        if (
            _text(audio_stream.get("r_frame_rate"), maximum=64) != "0/0"
            or _text(audio_stream.get("avg_frame_rate"), maximum=64) != "0/0"
        ):
            raise AVMediaAdapterError("inspection_invalid")
        if (
            audio_codec not in capability.audio_decoders
            or audio_sample_format not in capability.input_sample_formats
        ):
            raise AVMediaAdapterError("inspection_invalid")
        sample_rate = _integer(
            audio_stream.get("sample_rate"),
            minimum=1,
            maximum=limits.max_input_sample_rate,
        )
        audio_start = _fraction(audio_stream.get("start_time"))
        audio_time_base = _fraction(audio_stream.get("time_base"), allow_zero=False)
        audio_start_ticks = audio_start / audio_time_base
        if audio_start_ticks.denominator != 1:
            raise AVMediaAdapterError("inspection_invalid")
        for padding_name in ("initial_padding", "trailing_padding", "seek_preroll"):
            if (
                padding_name in audio_stream
                and _integer(audio_stream[padding_name], maximum=1_000_000) != 0
            ):
                raise AVMediaAdapterError("inspection_invalid")

    frames = _sequence(root["frames"], maximum=_MAX_PROBE_FRAMES)
    video_frame_count = 0
    audio_sample_count = 0
    audio_frame_padding: list[int] = []
    for raw_frame in frames:
        frame = _mapping(raw_frame)
        media_type = _text(frame.get("media_type"), maximum=32).casefold()
        frame_index = _integer(frame.get("stream_index"), maximum=_MAX_PROBE_STREAMS - 1)
        timestamp = _integer(frame.get("best_effort_timestamp"), maximum=1 << 54)
        timeline_duration = _integer(
            frame.get("duration"),
            minimum=1,
            maximum=1 << 54,
        )
        if media_type == "video":
            _require_members(
                frame,
                allowed=_VIDEO_FRAME_FIELDS,
                required=_VIDEO_FRAME_FIELDS - {"side_data_list"},
            )
            _validate_frame_side_data(frame, media_type=media_type)
            if (
                frame_index != video_index
                or timestamp
                != video_start_ticks.numerator + video_frame_count * video_frame_ticks.numerator
                or timeline_duration != video_frame_ticks.numerator
            ):
                raise AVMediaAdapterError("inspection_invalid")
            video_frame_count += 1
        elif media_type == "audio":
            _require_members(
                frame,
                allowed=frozenset(_AUDIO_FRAME_FIELDS),
                required=frozenset(_AUDIO_FRAME_FIELDS - {"side_data_list"}),
            )
            _validate_frame_side_data(frame, media_type=media_type)
            if (
                audio_index is None
                or sample_rate is None
                or audio_time_base is None
                or audio_start_ticks is None
                or frame_index != audio_index
            ):
                raise AVMediaAdapterError("inspection_invalid")
            decoded_sample_count = _integer(
                frame.get("nb_samples"),
                minimum=1,
                maximum=1_000_000,
            )
            timeline_samples = Fraction(timeline_duration, 1) * audio_time_base * sample_rate
            if timeline_samples.denominator != 1 or timeline_samples <= 0:
                raise AVMediaAdapterError("inspection_invalid")
            sample_count = timeline_samples.numerator
            decoded_padding = decoded_sample_count - sample_count
            if not 0 <= decoded_padding < 1024:
                raise AVMediaAdapterError("inspection_invalid")
            audio_frame_padding.append(decoded_padding)
            sample_ticks = Fraction(sample_count, sample_rate) / audio_time_base
            offset_ticks = Fraction(audio_sample_count, sample_rate) / audio_time_base
            if (
                sample_ticks.denominator != 1
                or offset_ticks.denominator != 1
                or timestamp != audio_start_ticks.numerator + offset_ticks.numerator
                or timeline_duration != sample_ticks.numerator
            ):
                raise AVMediaAdapterError("inspection_invalid")
            audio_sample_count += sample_count
        else:
            raise AVMediaAdapterError("inspection_invalid")
    if not 1 <= video_frame_count <= limits.max_video_frames:
        raise AVMediaAdapterError("resource_limit")
    if audio_sample_count > limits.max_audio_sample_frames:
        raise AVMediaAdapterError("resource_limit")
    if audio_frame_padding and (
        any(audio_frame_padding[:-1]) or (audio_frame_padding[-1] != 0 and audio_codec != "aac")
    ):
        raise AVMediaAdapterError("inspection_invalid")

    video_duration = Fraction(video_frame_count, 1) / average_rate
    if video_duration * 1000 > duration_limit:
        raise AVMediaAdapterError("resource_limit")
    if not _duration_matches(
        _fraction(video_stream.get("duration"), allow_zero=False),
        video_duration,
        video_time_base,
    ):
        raise AVMediaAdapterError("inspection_invalid")
    width = _integer(video_stream.get("width"), minimum=1, maximum=limits.max_width)
    height = _integer(video_stream.get("height"), minimum=1, maximum=limits.max_height)
    try:
        video = AVVideoDescriptor(
            codec=video_codec,
            width=width,
            height=height,
            pixel_format=video_pixel_format,
            color_range=_text(video_stream.get("color_range")),
            color_space=_text(video_stream.get("color_space")),
            color_primaries=_text(video_stream.get("color_primaries")),
            color_transfer=_text(video_stream.get("color_transfer")),
            rotation_degrees=_rotation(video_stream),
            frame_rate=_rational(average_rate),
            time_base=_rational(video_time_base),
            start_time=_rational(video_start),
            end_time=_rational(video_start + video_duration),
            decoded_frame_count=video_frame_count,
        )
    except AVReconstructionError as exc:
        raise AVMediaAdapterError("inspection_invalid") from exc

    audio: AVAudioDescriptor | None = None
    audio_duration: Fraction | None = None
    if audio_stream is not None:
        if audio_sample_count <= 0:
            raise AVMediaAdapterError("inspection_invalid")
        if (
            sample_rate is None
            or audio_start is None
            or audio_time_base is None
            or audio_codec is None
            or audio_sample_format is None
        ):
            raise AVMediaAdapterError("inspection_invalid")
        channels = _integer(
            audio_stream.get("channels"),
            minimum=1,
            maximum=limits.max_input_channels,
        )
        audio_duration = Fraction(audio_sample_count, sample_rate)
        if not _duration_matches(
            _fraction(audio_stream.get("duration"), allow_zero=False),
            audio_duration,
            audio_time_base,
        ):
            raise AVMediaAdapterError("inspection_invalid")
        try:
            audio = AVAudioDescriptor(
                codec=audio_codec,
                sample_format=audio_sample_format,
                sample_rate=sample_rate,
                channels=channels,
                channel_layout=_text(audio_stream.get("channel_layout")),
                time_base=_rational(audio_time_base),
                start_time=_rational(audio_start),
                end_time=_rational(audio_start + audio_duration),
                decoded_sample_count=audio_sample_count,
            )
        except AVReconstructionError as exc:
            raise AVMediaAdapterError("inspection_invalid") from exc

    expected_format_duration = max(
        video_duration,
        Fraction(0, 1) if audio_duration is None else audio_duration,
    )
    format_tolerance = max(
        video_time_base,
        Fraction(0, 1) if audio_time_base is None else audio_time_base,
    )
    if not _duration_matches(format_duration, expected_format_duration, format_tolerance):
        raise AVMediaAdapterError("inspection_invalid")

    try:
        return AVMediaDescriptor(
            segment_id=segment_id,
            artifact_receipt_fingerprint=artifact_receipt_fingerprint,
            artifact_output_fingerprint=artifact_output_fingerprint,
            artifact_byte_length=artifact_byte_length,
            capability_fingerprint=capability.fingerprint,
            container=container,
            stream_count=len(streams),
            video=video,
            audio=audio,
            subtitle_stream_count=0,
            data_stream_count=0,
            attachment_stream_count=0,
            inspected_at_ms=inspected_at_ms,
            expires_at_ms=inspected_at_ms + limits.inspection_ttl_ms,
            warning_codes=(),
        )
    except AVReconstructionError as exc:
        raise AVMediaAdapterError("inspection_invalid") from exc


def _probe_failure(status: ProcessStatus, *, cleanup_succeeded: bool) -> AVMediaAdapterError:
    if not cleanup_succeeded:
        return AVMediaAdapterError("cleanup_failed")
    code = {
        ProcessStatus.OUTPUT_LIMIT: "resource_limit",
        ProcessStatus.TIMED_OUT: "timed_out",
        ProcessStatus.CANCELLED: "cancelled",
        ProcessStatus.TERMINATION_FAILED: "termination_failed",
        ProcessStatus.SPAWN_FAILED: "adapter_unavailable",
    }.get(status, "media_process_failed")
    return AVMediaAdapterError(code)


def _raise_if_cancelled(cancellation: CancellationProbe | None) -> None:
    if cancellation is None:
        return
    try:
        cancelled = cancellation.is_cancelled()
    except Exception as exc:
        raise AVMediaAdapterError("cancellation_failed") from exc
    if type(cancelled) is not bool:
        raise AVMediaAdapterError("cancellation_failed")
    if cancelled:
        raise AVMediaAdapterError("cancelled")


def _silence_wav(sample_count: int) -> bytes:
    limits = qualified_av_limits()
    if not 1 <= sample_count <= limits.max_audio_sample_frames:
        raise AVMediaAdapterError("resource_limit")
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(limits.target_channels)
        writer.setsampwidth(2)
        writer.setframerate(limits.target_sample_rate)
        writer.writeframes(b"\x00" * (sample_count * limits.target_channels * 2))
    return payload.getvalue()


class _StreamSizeWatchdog:
    """M20-08: a cancellation probe that also trips once the destination outgrows the
    declared length, bounding how much a misbehaving source can write into owned scratch.

    The protocol boundary cannot force a foreign ``AVSegmentSource`` to respect ceilings; this
    probe stops any cooperating implementation within one chunk of the declared size, and the
    post-stream size check refuses the copy regardless.
    """

    __slots__ = ("_inner", "_maximum_bytes", "_path", "tripped")

    def __init__(
        self,
        path: Path,
        maximum_bytes: int,
        inner: CancellationProbe | None,
    ) -> None:
        self._path = path
        self._maximum_bytes = maximum_bytes
        self._inner = inner
        self.tripped = False

    def is_cancelled(self) -> bool:
        if self._inner is not None:
            try:
                inner_cancelled = self._inner.is_cancelled()
            except Exception:  # noqa: BLE001 - a broken probe must stop the stream, not leak.
                return True
            if type(inner_cancelled) is not bool or inner_cancelled:
                return True
        try:
            size = self._path.lstat().st_size
        except OSError:
            return False
        if size > self._maximum_bytes:
            self.tripped = True
            return True
        return False


def _assert_exact_output(
    descriptor: AVMediaDescriptor,
    *,
    expected_frames: int,
    expected_samples: int,
) -> None:
    target_profile = qualified_av_target_profile()
    video = descriptor.video
    audio = descriptor.audio
    duration = Fraction(expected_frames, 1) / target_profile.frame_rate.fraction
    if not (
        descriptor.container == target_profile.container
        and descriptor.stream_count == 2
        and descriptor.warning_codes == ()
        and video is not None
        and video.codec == target_profile.video_codec
        and video.width == target_profile.width
        and video.height == target_profile.height
        and video.pixel_format == target_profile.pixel_format
        and video.color_range == target_profile.color_range
        and video.color_space == target_profile.color_space
        and video.color_primaries == target_profile.color_primaries
        and video.color_transfer == target_profile.color_transfer
        and video.rotation_degrees == target_profile.rotation_degrees
        and video.frame_rate == target_profile.frame_rate
        and video.time_base == target_profile.video_time_base
        and video.start_time == AVRational(0, 1)
        and video.end_time == _rational(duration)
        and video.decoded_frame_count == expected_frames
        and audio is not None
        and audio.codec == target_profile.audio_codec
        and audio.sample_format == target_profile.sample_format
        and audio.sample_rate == target_profile.sample_rate
        and audio.channels == target_profile.channels
        and audio.channel_layout == target_profile.channel_layout
        and audio.time_base == target_profile.audio_time_base
        and audio.start_time == AVRational(0, 1)
        and audio.end_time == _rational(duration)
        and audio.decoded_sample_count == expected_samples
    ):
        raise AVMediaAdapterError("media_output_invalid")


def _descriptor_fact_projection(descriptor: AVMediaDescriptor) -> dict[str, object]:
    value = descriptor.to_wire()
    value.pop("inspected_at_ms", None)
    value.pop("expires_at_ms", None)
    return value


def _normalize_preview_probe_wire(value: object) -> dict[str, object]:
    """Reduce the qualified ffprobe envelope to the existing closed preview payload."""

    if type(value) is not dict:
        raise AVMediaAdapterError("media_output_invalid")
    required = {"format", "streams"}
    optional_empty = {"programs", "stream_groups"}
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(required | optional_empty):
        raise AVMediaAdapterError("media_output_invalid")
    for member in optional_empty:
        if member in value and (type(value[member]) is not list or value[member]):
            raise AVMediaAdapterError("media_output_invalid")
    # IMPORTANT: current qualified ffprobe emits these two empty envelopes even with a closed
    # show_entries request. Discard only known empty arrays; accepting content or unknown keys
    # would silently widen the media parser boundary.
    return {"format": value["format"], "streams": value["streams"]}


def _normalize_preview_audio_stream_wire(
    value: dict[str, object],
) -> dict[str, object] | None:
    """Remove only ffprobe's shared-selection zero-rate audio sentinel."""

    expected = {
        "codec_type",
        "codec_name",
        "sample_rate",
        "channels",
        "channel_layout",
    }
    keys = set(value)
    if keys == expected:
        return dict(value)
    if keys != expected | {"avg_frame_rate"} or value.get("avg_frame_rate") != "0/0":
        return None
    # IMPORTANT: a shared stream show_entries selection adds avg_frame_rate=0/0 to audio.
    # Remove only that exact sentinel; stripping another value or unknown key would qualify an
    # unowned ffprobe shape and could incorrectly map audio into the preview.
    return {key: value[key] for key in expected}


def _preview_seconds_expression(frames: int, frames_per_second: int) -> str:
    """Render one bounded frame time in ffmpeg's accepted decimal-seconds grammar."""

    if (
        type(frames) is not int
        or frames < 0
        or type(frames_per_second) is not int
        or frames_per_second <= 0
    ):
        raise AVMediaAdapterError("preview_source_unsupported")
    seconds = (Decimal(frames) / Decimal(frames_per_second)).quantize(Decimal("0.000000001"))
    return format(seconds, "f").rstrip("0").rstrip(".") or "0"


@dataclass(frozen=True, slots=True)
class _OwnedM26DerivedSource:
    lease: OwnedOutputLease
    declared_byte_length: int
    declared_content_fingerprint: str

    @property
    def byte_length(self) -> int:
        return self.declared_byte_length

    @property
    def content_fingerprint(self) -> str:
        return self.declared_content_fingerprint

    def stream_into(
        self,
        destination: Path,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[int, str]:
        if (
            self.lease.released
            or not isinstance(destination, Path)
            or not destination.is_absolute()
        ):
            raise AVTransportError("transport_unavailable")
        deadline = time.monotonic() + qualified_av_limits().process_timeout_ms / 1000
        try:
            body, fingerprint = _read_regular_media_body(
                self.lease.path,
                self.declared_byte_length,
                deadline=deadline,
                cancellation=cancellation,
            )
            if len(body) != self.declared_byte_length or fingerprint != (
                self.declared_content_fingerprint
            ):
                raise AVTransportError("transport_identity_mismatch")
            _write_new_file(destination, bytes(body))
            body.clear()
            return self.declared_byte_length, fingerprint
        except AVTransportError:
            raise
        except AVMediaAdapterError as exc:
            code = "transport_cancelled" if exc.code == "cancelled" else "transport_unavailable"
            raise AVTransportError(code) from exc
        except Exception as exc:  # noqa: BLE001 - expose only a closed transport code.
            raise AVTransportError("transport_unavailable") from exc


@dataclass(slots=True)
class M26DerivedMediaLease:
    """One job-owned cropped file plus its verified original descriptor."""

    _adapter: QualifiedAVMediaAdapter
    _lease: OwnedOutputLease
    original_descriptor: AVMediaDescriptor
    byte_length: int
    content_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self._lease, OwnedOutputLease)
            or type(self.original_descriptor) is not AVMediaDescriptor
            or type(self.byte_length) is not int
            or self.byte_length <= 0
            or type(self.content_fingerprint) is not str
        ):
            raise AVMediaAdapterError("m26_derived_lease_invalid")

    @property
    def source(self) -> AVSegmentSource:
        return _OwnedM26DerivedSource(
            self._lease,
            self.byte_length,
            self.content_fingerprint,
        )

    def inspect(
        self,
        *,
        segment_id: str,
        derived_receipt_fingerprint: str,
        cancellation: CancellationProbe | None = None,
    ) -> AVMediaDescriptor:
        return self._adapter._inspect_staged_output(
            self._lease,
            segment_id=segment_id,
            authority_fingerprint=derived_receipt_fingerprint,
            maximum_bytes=qualified_av_limits().max_input_bytes_per_segment,
            maximum_duration_ms=qualified_av_limits().max_duration_ms_per_segment,
            cancellation=cancellation,
        )

    def release(self) -> None:
        self._lease.release()


class QualifiedAVMediaAdapter:
    """Lazy exact-capability adapter; executable locators remain process-private."""

    def __init__(
        self,
        *,
        ffmpeg_path: Path,
        ffprobe_path: Path,
        scratch_root: Path,
        clock_ms: Callable[[], int],
    ) -> None:
        if (
            not isinstance(ffmpeg_path, Path)
            or not isinstance(ffprobe_path, Path)
            or not isinstance(scratch_root, Path)
            or not ffmpeg_path.is_absolute()
            or not ffprobe_path.is_absolute()
            or not scratch_root.is_absolute()
            or not callable(clock_ms)
        ):
            raise AVMediaAdapterError("adapter_unavailable")
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._clock_ms = clock_ms
        capability = qualified_ffmpeg_capability()
        with _pin_exact_executable(ffmpeg_path, capability.ffmpeg_sha256):
            pass
        with _pin_exact_executable(ffprobe_path, capability.ffprobe_sha256):
            pass
        try:
            self._scratch_root = ensure_directory(scratch_root)
        except (UnsafePathError, OSError) as exc:
            raise AVMediaAdapterError("adapter_unavailable") from exc
        self._process_slots = _PROCESS_SLOTS
        self._runner = SubprocessMediaRunner()

    def _now_ms(self) -> int:
        try:
            value = self._clock_ms()
        except Exception as exc:
            raise AVMediaAdapterError("inspection_unavailable") from exc
        if type(value) is not int or value <= 0:
            raise AVMediaAdapterError("inspection_unavailable")
        return value

    def _probe_invocation(self, input_path: Path) -> MediaProcessInvocation:
        capability = qualified_ffmpeg_capability()
        limits = qualified_av_limits()
        executable = str(self._ffprobe_path)
        entries = (
            "format=format_name,duration:format_tags=:"
            "stream=index,codec_type,codec_name,width,height,pix_fmt,color_range,color_space,"
            "color_primaries,color_transfer,r_frame_rate,avg_frame_rate,time_base,start_time,"
            "duration,sample_fmt,sample_rate,channels,channel_layout,initial_padding,"
            "trailing_padding,seek_preroll:"
            "stream_tags=:stream_disposition=:"
            "stream_side_data=rotation:"
            "frame=media_type,stream_index,best_effort_timestamp,duration,nb_samples:"
            "frame_tags=:"
            "frame_side_data=side_data_type,skip_samples,discard_padding"
        )
        codecs = tuple(dict.fromkeys((*capability.video_decoders, *capability.audio_decoders)))
        argv = (
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            ",".join(capability.demuxers),
            "-codec_whitelist",
            ",".join(codecs),
            "-show_format",
            "-show_streams",
            "-show_frames",
            "-show_entries",
            entries,
            "-of",
            "json",
            "-i",
            str(input_path),
        )
        return MediaProcessInvocation(
            tool="ffprobe",
            executable=executable,
            argv=argv,
            protocol_whitelist=("file",),
            format_whitelist=capability.demuxers,
            codec_whitelist=codecs,
            timeout_seconds=Decimal(limits.process_timeout_ms) / Decimal(1000),
            max_stdout_bytes=limits.max_probe_stdout_bytes,
            max_stderr_bytes=limits.max_probe_stderr_bytes,
            start_new_session=False,
        )

    def _authoring_video_probe_invocation(
        self,
        input_path: Path,
        *,
        deadline: float,
    ) -> MediaProcessInvocation:
        capability = qualified_ffmpeg_capability()
        limits = qualified_av_limits()
        executable = str(self._ffprobe_path)
        codecs = tuple(dict.fromkeys((*capability.video_decoders, *capability.audio_decoders)))
        entries = (
            "stream=index,codec_type,codec_name,width,height,pix_fmt,sample_aspect_ratio,"
            "color_range,color_space,color_primaries,color_transfer,time_base,start_pts,"
            "sample_fmt,sample_rate,channels,channel_layout:"
            "stream_tags=:stream_disposition=:stream_side_data=:"
            "frame=media_type,stream_index,pts,pkt_dts,duration,nb_samples:"
            "frame_tags=:frame_side_data="
        )
        return MediaProcessInvocation(
            tool="ffprobe",
            executable=executable,
            argv=(
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                # IMPORTANT: ffprobe rejects ffmpeg's `-nostdin` before probing; the subprocess
                # runner already closes interactive input with stdin=DEVNULL.
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                ",".join(capability.demuxers),
                "-codec_whitelist",
                ",".join(codecs),
                "-show_streams",
                "-show_frames",
                "-show_entries",
                entries,
                "-of",
                "json",
                "-i",
                str(input_path),
            ),
            protocol_whitelist=("file",),
            format_whitelist=capability.demuxers,
            codec_whitelist=codecs,
            timeout_seconds=Decimal(str(self._preview_remaining(deadline))),
            max_stdout_bytes=limits.max_probe_stdout_bytes,
            max_stderr_bytes=limits.max_probe_stderr_bytes,
            start_new_session=False,
        )

    @staticmethod
    def _preview_remaining(deadline: float) -> float:
        if type(deadline) is not float:
            raise AVMediaAdapterError("preview_deadline")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AVMediaAdapterError("preview_deadline")
        return min(30.0, remaining)

    def _preview_probe_invocation(
        self,
        input_path: Path,
        *,
        deadline: float,
    ) -> MediaProcessInvocation:
        capability = qualified_ffmpeg_capability()
        executable = str(self._ffprobe_path)
        return MediaProcessInvocation(
            tool="ffprobe",
            executable=executable,
            argv=(
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                ",".join(capability.demuxers),
                "-codec_whitelist",
                ",".join((*capability.video_decoders, *capability.audio_decoders)),
                "-show_entries",
                "format=format_name,duration:stream=codec_type,codec_name,width,height,pix_fmt,"
                "avg_frame_rate,sample_rate,channels,channel_layout",
                "-of",
                "json",
                "-i",
                str(input_path),
            ),
            protocol_whitelist=("file",),
            format_whitelist=capability.demuxers,
            codec_whitelist=tuple(
                dict.fromkeys((*capability.video_decoders, *capability.audio_decoders))
            ),
            timeout_seconds=Decimal(str(self._preview_remaining(deadline))),
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=256 * 1024,
            start_new_session=False,
        )

    def _preview_probe(
        self,
        path: Path,
        *,
        maximum_bytes: int,
        deadline: float,
        cancellation: CancellationProbe | None,
    ) -> dict[str, object]:
        _raise_if_cancelled(cancellation)
        with _pin_regular_media(
            path,
            maximum_bytes,
            deadline=deadline,
            cancellation=cancellation,
        ):
            invocation = self._preview_probe_invocation(path, deadline=deadline)
            capability = qualified_ffmpeg_capability()
            with _pin_exact_executable(self._ffprobe_path, capability.ffprobe_sha256):
                capture = self._runner.run(invocation, cancellation=cancellation)
        if cast(ProcessStatus, capture.status) is not ProcessStatus.SUCCEEDED:
            raise _probe_failure(
                cast(ProcessStatus, capture.status),
                cleanup_succeeded=capture.cleanup_succeeded,
            )
        if not capture.cleanup_succeeded:
            raise AVMediaAdapterError("cleanup_failed")
        try:
            value = json.loads(capture.stdout.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise AVMediaAdapterError("media_output_invalid") from exc
        return _normalize_preview_probe_wire(value)

    @staticmethod
    def _validate_preview_probe(
        value: dict[str, object],
        *,
        source: bool,
    ) -> int:
        format_value = value.get("format")
        streams = value.get("streams")
        if type(format_value) is not dict or type(streams) is not list or len(streams) != 2:
            raise AVMediaAdapterError("media_output_invalid")
        if set(format_value) != {"format_name", "duration"}:
            raise AVMediaAdapterError("media_output_invalid")
        if format_value["format_name"] != "mov,mp4,m4a,3gp,3g2,mj2":
            raise AVMediaAdapterError("media_output_invalid")
        try:
            duration = Fraction(str(format_value["duration"]))
        except (ValueError, ZeroDivisionError) as exc:
            raise AVMediaAdapterError("media_output_invalid") from exc
        if duration <= 0 or duration * 1000 > _PREVIEW_MAX_DURATION_MS:
            raise AVMediaAdapterError("media_output_invalid")
        by_type = {item.get("codec_type"): item for item in streams if type(item) is dict}
        video = by_type.get("video")
        audio = by_type.get("audio")
        if type(video) is not dict or type(audio) is not dict or len(by_type) != 2:
            raise AVMediaAdapterError("media_output_invalid")
        # IMPORTANT: the shared ffprobe selection adds audio avg_frame_rate=0/0; retain only
        # its exact known sentinel or valid aggregate/proxy media is rejected before playback.
        audio = _normalize_preview_audio_stream_wire(audio)
        if audio is None:
            raise AVMediaAdapterError("media_output_invalid")
        expected_video_keys = {
            "codec_type",
            "codec_name",
            "width",
            "height",
            "pix_fmt",
            "avg_frame_rate",
        }
        expected_audio_keys = {
            "codec_type",
            "codec_name",
            "sample_rate",
            "channels",
            "channel_layout",
        }
        if set(video) != expected_video_keys or set(audio) != expected_audio_keys:
            raise AVMediaAdapterError("media_output_invalid")
        try:
            frame_rate = Fraction(str(video["avg_frame_rate"]))
            width = int(cast(str | int, video["width"]))
            height = int(cast(str | int, video["height"]))
            sample_rate = int(cast(str | int, audio["sample_rate"]))
            channels = int(cast(str | int, audio["channels"]))
        except (ValueError, ZeroDivisionError, TypeError) as exc:
            raise AVMediaAdapterError("media_output_invalid") from exc
        if (
            video["codec_name"] != "h264"
            or video["pix_fmt"] != "yuv420p"
            or audio["codec_name"] != "aac"
            or sample_rate != 48_000
            or (
                source
                and (
                    width != 512
                    or height != 512
                    or frame_rate != 30
                    or channels != 2
                    or audio["channel_layout"] != "stereo"
                )
            )
            or (
                not source
                and (
                    width > 320
                    or height > 320
                    or frame_rate != 15
                    or channels != 1
                    or audio["channel_layout"] != "mono"
                )
            )
        ):
            raise AVMediaAdapterError("media_output_invalid")
        milliseconds = duration * 1000
        if milliseconds.denominator != 1:
            raise AVMediaAdapterError("media_output_invalid")
        return int(milliseconds)

    def _preview_transcode_invocation(
        self,
        source: Path,
        output: OwnedOutputLease,
        *,
        deadline: float,
        source_duration_ms: int,
    ) -> MediaProcessInvocation:
        capability = qualified_ffmpeg_capability()
        executable = str(self._ffmpeg_path)
        codecs = tuple(
            dict.fromkeys(
                (
                    *capability.video_decoders,
                    *capability.audio_decoders,
                    "libx264",
                    "aac",
                )
            )
        )
        if (
            type(source_duration_ms) is not int
            or not 0 < source_duration_ms <= _PREVIEW_MAX_DURATION_MS
        ):
            raise AVMediaAdapterError("preview_source_unavailable")
        # IMPORTANT: trim decoded AAC padding to the verified source extent before re-encoding;
        # otherwise a valid 30-second source produces a proxy beyond the unchanged preview cap.
        audio_filter = (
            "aformat=sample_rates=48000:channel_layouts=mono,aresample=48000,"
            f"atrim=end_sample={source_duration_ms * 48},asetpts=N/SR/TB"
        )
        # IMPORTANT: every selected member, including segment exports, must be re-encoded with
        # faststart; stream copy can preserve keyframe drift and produce an invalid clip proxy.
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=executable,
            argv=(
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                ",".join(capability.demuxers),
                "-codec_whitelist",
                ",".join(codecs),
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-vf",
                "scale=w='min(320,iw)':h='min(320,ih)':force_original_aspect_ratio=decrease,fps=15",
                "-af",
                audio_filter,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                "1500k",
                "-maxrate",
                "1500k",
                "-bufsize",
                "3000k",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-ar",
                "48000",
                "-ac",
                "1",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-sn",
                "-dn",
                "-movflags",
                "+faststart",
                "-n",
                str(output.path),
            ),
            protocol_whitelist=("file",),
            format_whitelist=capability.demuxers,
            codec_whitelist=codecs,
            timeout_seconds=Decimal(str(self._preview_remaining(deadline))),
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=256 * 1024,
            output_leases=(output,),
            max_owned_output_bytes=_PREVIEW_MAX_OUTPUT_BYTES,
        )

    @staticmethod
    def _authoring_preview_source_facts(
        value: dict[str, object],
        *,
        source_fps: int,
        source_start_frame: int,
        frames: int,
    ) -> tuple[int, str]:
        format_value = value.get("format")
        streams = value.get("streams")
        if (
            type(format_value) is not dict
            or set(format_value) != {"format_name", "duration"}
            or format_value.get("format_name") != "mov,mp4,m4a,3gp,3g2,mj2"
            or type(streams) is not list
            or not 1 <= len(streams) <= 2
            or not all(type(item) is dict for item in streams)
        ):
            raise AVMediaAdapterError("preview_source_unsupported")
        try:
            duration = Fraction(str(format_value["duration"]))
        except (ValueError, ZeroDivisionError, TypeError) as exc:
            raise AVMediaAdapterError("preview_source_unsupported") from exc
        if duration <= 0 or duration * 1000 > _PREVIEW_MAX_DURATION_MS:
            raise AVMediaAdapterError("preview_source_too_long")
        typed_streams = cast(list[dict[str, object]], streams)
        videos = [item for item in typed_streams if item.get("codec_type") == "video"]
        audios = [item for item in typed_streams if item.get("codec_type") == "audio"]
        if len(videos) != 1 or len(videos) + len(audios) != len(typed_streams):
            raise AVMediaAdapterError("preview_source_unsupported")
        video = videos[0]
        expected_video = {
            "codec_type",
            "codec_name",
            "width",
            "height",
            "pix_fmt",
            "avg_frame_rate",
        }
        if set(video) != expected_video:
            raise AVMediaAdapterError("preview_source_unsupported")
        try:
            width = int(cast(str | int, video["width"]))
            height = int(cast(str | int, video["height"]))
            frame_rate = Fraction(str(video["avg_frame_rate"]))
        except (ValueError, ZeroDivisionError, TypeError) as exc:
            raise AVMediaAdapterError("preview_source_unsupported") from exc
        limits = qualified_av_limits()
        if (
            video.get("codec_name") != "h264"
            or video.get("pix_fmt") != "yuv420p"
            or not 1 <= width <= limits.max_width
            or not 1 <= height <= limits.max_height
            or frame_rate != source_fps
            or frame_rate > limits.max_input_fps
            or source_start_frame + frames > duration * source_fps
        ):
            raise AVMediaAdapterError("preview_source_unsupported")
        if not audios:
            return int(duration * 1000), "absent"
        if len(audios) != 1:
            return int(duration * 1000), "unavailable"
        audio = _normalize_preview_audio_stream_wire(audios[0])
        if audio is None:
            return int(duration * 1000), "unavailable"
        expected_audio = {
            "codec_type",
            "codec_name",
            "sample_rate",
            "channels",
            "channel_layout",
        }
        try:
            sample_rate = int(cast(str | int, audio.get("sample_rate", 0)))
            channels = int(cast(str | int, audio.get("channels", 0)))
        except (ValueError, TypeError):
            return int(duration * 1000), "unavailable"
        qualified = (
            set(audio) == expected_audio
            and audio.get("codec_name") == "aac"
            and 1 <= sample_rate <= limits.max_input_sample_rate
            and 1 <= channels <= limits.max_input_channels
            and type(audio.get("channel_layout")) is str
        )
        return int(duration * 1000), "present_bound" if qualified else "unavailable"

    def _authoring_preview_invocation(
        self,
        source: Path,
        output: OwnedOutputLease,
        *,
        source_start_frame: int,
        frames: int,
        source_fps: int,
        audio_disposition: str,
        deadline: float,
    ) -> MediaProcessInvocation:
        capability = qualified_ffmpeg_capability()
        executable = str(self._ffmpeg_path)
        codecs = tuple(
            dict.fromkeys(
                (*capability.video_decoders, *capability.audio_decoders, "libx264", "aac")
            )
        )
        # IMPORTANT: atrim duration fields do not accept ffmpeg rational slash syntax. Keep video
        # on exact frame indices, but render the equivalent audio times as canonical decimals or
        # the valid embedded-audio branch fails before producing any preview.
        start = _preview_seconds_expression(source_start_frame, source_fps)
        duration = _preview_seconds_expression(frames, source_fps)
        filters = [
            "[0:v:0]"
            f"trim=start_frame={source_start_frame}:end_frame={source_start_frame + frames},"
            "setpts=PTS-STARTPTS,"
            "scale=w='min(320,iw)':h='min(320,ih)':force_original_aspect_ratio=decrease,"
            "fps=15,format=yuv420p[v]"
        ]
        if audio_disposition == "present_bound":
            filters.append(
                f"[0:a:0]atrim=start={start}:duration={duration},asetpts=PTS-STARTPTS,"
                "aformat=sample_rates=48000:channel_layouts=mono,aresample=48000[a]"
            )
        argv: list[str] = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            ",".join(capability.demuxers),
            "-codec_whitelist",
            ",".join(codecs),
            "-i",
            str(source),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            "1500k",
            "-maxrate",
            "1500k",
            "-bufsize",
            "3000k",
        ]
        if audio_disposition == "present_bound":
            argv.extend(("-map", "[a]", "-c:a", "aac", "-b:a", "64k", "-ar", "48000", "-ac", "1"))
        else:
            argv.append("-an")
        argv.extend(
            (
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-sn",
                "-dn",
                "-movflags",
                "+faststart",
                "-n",
                str(output.path),
            )
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=executable,
            argv=tuple(argv),
            protocol_whitelist=("file",),
            format_whitelist=capability.demuxers,
            codec_whitelist=codecs,
            timeout_seconds=Decimal(str(self._preview_remaining(deadline))),
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=256 * 1024,
            output_leases=(output,),
            max_owned_output_bytes=_PREVIEW_MAX_OUTPUT_BYTES,
        )

    def execute_authoring_preview(
        self,
        *,
        source_path: Path,
        source_start_frame: int,
        frames: int,
        source_fps: int,
        deadline: float,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[bytearray, str]:
        """Stage one private file authority and derive its exact bounded half-open clip."""

        # IMPORTANT: Path() materializes as WindowsPath/PosixPath. Exact `type(...) is Path`
        # rejects every real host path and disables Authoring preview on both native platforms.
        if (
            not isinstance(source_path, Path)
            or not source_path.is_absolute()
            or type(source_start_frame) is not int
            or source_start_frame < 0
            or type(frames) is not int
            or not 1 <= frames <= 900
            or type(source_fps) is not int
            or not 1 <= source_fps <= qualified_av_limits().max_input_fps
            or frames * 1000 > _PREVIEW_MAX_DURATION_MS * source_fps
        ):
            raise AVMediaAdapterError("preview_source_unsupported")
        _preview_io_remaining(deadline, time.monotonic)
        _raise_if_cancelled(cancellation)
        if not self._process_slots.acquire(blocking=False):
            raise AVMediaAdapterError("preview_busy")
        staged: OwnedOutputLease | None = None
        output: OwnedOutputLease | None = None
        try:
            staged = create_output_lease(self._scratch_root, suffix=".mp4")
            output = create_output_lease(self._scratch_root, suffix=".mp4")
            body, _fingerprint = _read_regular_media_body(
                source_path,
                _PREVIEW_MAX_SOURCE_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            _write_new_file(staged.path, bytes(body))
            body.clear()
            source_probe = self._preview_probe(
                staged.path,
                maximum_bytes=_PREVIEW_MAX_SOURCE_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            _duration, audio_disposition = self._authoring_preview_source_facts(
                source_probe,
                source_fps=source_fps,
                source_start_frame=source_start_frame,
                frames=frames,
            )
            invocation = self._authoring_preview_invocation(
                staged.path,
                output,
                source_start_frame=source_start_frame,
                frames=frames,
                source_fps=source_fps,
                audio_disposition=audio_disposition,
                deadline=deadline,
            )
            self._run_ffmpeg(invocation, cancellation)
            output_probe = self._preview_probe(
                output.path,
                maximum_bytes=_PREVIEW_MAX_OUTPUT_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            output_duration = self._validate_authoring_preview_output(
                output_probe,
                audio_disposition=audio_disposition,
            )
            expected_ms = Fraction(frames * 1000, source_fps)
            if abs(Fraction(output_duration) - expected_ms) > 100:
                raise AVMediaAdapterError("media_output_invalid")
            response, _ = _read_regular_media_body(
                output.path,
                _PREVIEW_MAX_OUTPUT_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            return response, audio_disposition
        finally:
            cleanup_error: Exception | None = None
            for lease in (output, staged):
                if lease is None:
                    continue
                try:
                    lease.release()
                except (OSError, RuntimeError, MediaProcessError) as exc:
                    cleanup_error = cleanup_error or exc
            self._process_slots.release()
            if cleanup_error is not None:
                raise AVMediaAdapterError("cleanup_failed") from cleanup_error

    def probe_authoring_video_source(
        self,
        *,
        source_path: Path,
        deadline: float,
        cancellation: CancellationProbe | None = None,
    ) -> AuthoringVideoProbePayload:
        """Copy and fully probe one private source under the qualified process boundary."""

        if not isinstance(source_path, Path) or not source_path.is_absolute():
            raise AVMediaAdapterError("preview_source_unsupported")
        try:
            if source_path.lstat().st_size > _PREVIEW_MAX_SOURCE_BYTES:
                raise AVMediaAdapterError("resource_limit")
        except AVMediaAdapterError:
            raise
        except OSError:
            raise AVMediaAdapterError("media_output_invalid") from None
        _preview_io_remaining(deadline, time.monotonic)
        _raise_if_cancelled(cancellation)
        if not self._process_slots.acquire(blocking=False):
            raise AVMediaAdapterError("preview_busy")
        staged: OwnedOutputLease | None = None
        try:
            staged = create_output_lease(self._scratch_root, suffix=".mp4")
            body, fingerprint = _read_regular_media_body(
                source_path,
                _PREVIEW_MAX_SOURCE_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            byte_length = len(body)
            # SECURITY: probe only the byte-for-byte owned copy and hold its file identity pinned;
            # probing the caller path would let replacement race the returned source fingerprint.
            _write_new_file(staged.path, bytes(body))
            body.clear()
            with _pin_regular_media(
                staged.path,
                _PREVIEW_MAX_SOURCE_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            ) as (staged_size, staged_fingerprint):
                if staged_size != byte_length or staged_fingerprint != fingerprint:
                    raise AVMediaAdapterError("media_output_invalid")
                invocation = self._authoring_video_probe_invocation(
                    staged.path,
                    deadline=deadline,
                )
                capability = qualified_ffmpeg_capability()
                with _pin_exact_executable(
                    self._ffprobe_path,
                    capability.ffprobe_sha256,
                ):
                    capture = self._runner.run(invocation, cancellation=cancellation)
            status = cast(ProcessStatus, capture.status)
            if status is not ProcessStatus.SUCCEEDED:
                raise _probe_failure(status, cleanup_succeeded=capture.cleanup_succeeded)
            if not capture.cleanup_succeeded:
                raise AVMediaAdapterError("cleanup_failed")
            _raise_if_cancelled(cancellation)
            return AuthoringVideoProbePayload(capture.stdout, fingerprint, byte_length)
        except AVMediaAdapterError:
            raise
        except (
            ArtifactStoreError,
            UnsafePathError,
            OSError,
            RuntimeError,
            MediaProcessError,
        ):
            raise AVMediaAdapterError("media_output_invalid") from None
        finally:
            cleanup_error: Exception | None = None
            if staged is not None:
                try:
                    staged.release()
                except (OSError, RuntimeError, MediaProcessError) as exc:
                    cleanup_error = exc
            self._process_slots.release()
            if cleanup_error is not None:
                raise AVMediaAdapterError("cleanup_failed") from None

    def normalize_generated_authoring_source(
        self,
        *,
        source_path: Path,
        output: OwnedOutputLease,
        deadline: float,
        cancellation: CancellationProbe | None = None,
    ) -> None:
        """Convert one private generated MP4 into the strict Authoring source profile."""

        if (
            not isinstance(source_path, Path)
            or not source_path.is_absolute()
            or not isinstance(output, OwnedOutputLease)
            or output.root != self._scratch_root
        ):
            raise AVMediaAdapterError("source_unavailable")
        _preview_io_remaining(deadline, time.monotonic)
        _raise_if_cancelled(cancellation)
        if not self._process_slots.acquire(blocking=False):
            raise AVMediaAdapterError("preview_busy")
        try:
            # SECURITY: decode only the already verified private copy. The filter reads its
            # declared transfer and converts pixels; relabelling metadata would admit wrong color.
            with _pin_regular_media(
                source_path,
                _PREVIEW_MAX_SOURCE_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            ):
                _preview_io_remaining(deadline, time.monotonic)
                remaining = deadline - time.monotonic()
                capability = qualified_ffmpeg_capability()
                target = qualified_av_target_profile()
                limits = qualified_av_limits()
                executable = str(self._ffmpeg_path)
                codecs = tuple(
                    dict.fromkeys((*capability.video_decoders, *capability.audio_decoders))
                )
                invocation = MediaProcessInvocation(
                    tool="ffmpeg",
                    executable=executable,
                    argv=(
                        executable,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-protocol_whitelist",
                        "file",
                        "-format_whitelist",
                        ",".join(capability.demuxers),
                        "-codec_whitelist",
                        ",".join(codecs),
                        "-i",
                        str(source_path),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-vf",
                        "colorspace=all=bt709,setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "medium",
                        "-crf",
                        "18",
                        "-bf",
                        "0",
                        "-pix_fmt",
                        target.pixel_format,
                        "-color_range",
                        target.color_range,
                        "-colorspace",
                        target.color_space,
                        "-color_primaries",
                        target.color_primaries,
                        "-color_trc",
                        target.color_transfer,
                        "-c:a",
                        "aac",
                        "-ar",
                        "48000",
                        "-ac",
                        "2",
                        "-map_metadata",
                        "-1",
                        "-map_chapters",
                        "-1",
                        "-movflags",
                        "+faststart",
                        # IMPORTANT: only the thread count follows the rule. The preset and
                        # the rate factor stay: this file is the final render's source, and
                        # at this rate factor every faster preset either lowers its quality
                        # or grows it toward `_PREVIEW_MAX_SOURCE_BYTES` (measured).
                        "-threads",
                        str(encoder_thread_count()),
                        "-f",
                        "mp4",
                        "-n",
                        str(output.path),
                    ),
                    protocol_whitelist=("file",),
                    format_whitelist=capability.demuxers,
                    codec_whitelist=codecs,
                    timeout_seconds=Decimal(str(min(remaining, limits.process_timeout_ms / 1000))),
                    max_stdout_bytes=limits.max_probe_stdout_bytes,
                    max_stderr_bytes=limits.max_probe_stderr_bytes,
                    output_leases=(output,),
                    max_owned_output_bytes=_PREVIEW_MAX_SOURCE_BYTES,
                )
                self._run_ffmpeg(invocation, cancellation)
            status, size = output.inspect_artifact(_PREVIEW_MAX_SOURCE_BYTES)
            if status != "ok" or size <= 0:
                raise AVMediaAdapterError("media_output_invalid")
        except AVMediaAdapterError:
            raise
        except (UnsafePathError, OSError, RuntimeError, MediaProcessError):
            raise AVMediaAdapterError("media_output_invalid") from None
        finally:
            self._process_slots.release()

    def probe_authoring_source_duration(
        self,
        *,
        source_path: Path,
        deadline: float,
        cancellation: CancellationProbe | None = None,
    ) -> int:
        """Copy and probe one private source, returning only its bounded duration."""

        if not isinstance(source_path, Path) or not source_path.is_absolute():
            raise AVMediaAdapterError("preview_source_unsupported")
        _preview_io_remaining(deadline, time.monotonic)
        _raise_if_cancelled(cancellation)
        if not self._process_slots.acquire(blocking=False):
            raise AVMediaAdapterError("preview_busy")
        staged: OwnedOutputLease | None = None
        try:
            staged = create_output_lease(self._scratch_root, suffix=".mp4")
            body, _fingerprint = _read_regular_media_body(
                source_path,
                _PREVIEW_MAX_SOURCE_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            _write_new_file(staged.path, bytes(body))
            body.clear()
            value = self._preview_probe(
                staged.path,
                maximum_bytes=_PREVIEW_MAX_SOURCE_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            streams = value.get("streams")
            videos = (
                [
                    item
                    for item in streams
                    if type(item) is dict and item.get("codec_type") == "video"
                ]
                if type(streams) is list
                else []
            )
            try:
                frame_rate = Fraction(str(videos[0]["avg_frame_rate"]))
            except (IndexError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                raise AVMediaAdapterError("preview_source_unsupported") from exc
            if frame_rate.denominator != 1:
                raise AVMediaAdapterError("preview_source_unsupported")
            duration, _audio = self._authoring_preview_source_facts(
                value,
                source_fps=int(frame_rate),
                source_start_frame=0,
                frames=1,
            )
            return duration
        finally:
            cleanup_error: Exception | None = None
            if staged is not None:
                try:
                    staged.release()
                except (OSError, RuntimeError, MediaProcessError) as exc:
                    cleanup_error = exc
            self._process_slots.release()
            if cleanup_error is not None:
                raise AVMediaAdapterError("cleanup_failed") from cleanup_error

    @staticmethod
    def _validate_authoring_preview_output(
        value: dict[str, object],
        *,
        audio_disposition: str,
    ) -> int:
        format_value = value.get("format")
        streams = value.get("streams")
        expected_count = 2 if audio_disposition == "present_bound" else 1
        if (
            type(format_value) is not dict
            or set(format_value) != {"format_name", "duration"}
            or format_value.get("format_name") != "mov,mp4,m4a,3gp,3g2,mj2"
            or type(streams) is not list
            or len(streams) != expected_count
        ):
            raise AVMediaAdapterError("media_output_invalid")
        by_type = {item.get("codec_type"): item for item in streams if type(item) is dict}
        video = by_type.get("video")
        audio = by_type.get("audio")
        if type(video) is not dict:
            raise AVMediaAdapterError("media_output_invalid")
        video_wire = cast(dict[str, object], video)
        try:
            duration = Fraction(str(format_value["duration"]))
            width = int(cast(str | int, video_wire["width"]))
            height = int(cast(str | int, video_wire["height"]))
            frame_rate = Fraction(str(video_wire["avg_frame_rate"]))
        except (KeyError, ValueError, ZeroDivisionError, TypeError) as exc:
            raise AVMediaAdapterError("media_output_invalid") from exc
        if (
            video.get("codec_name") != "h264"
            or video.get("pix_fmt") != "yuv420p"
            or not 1 <= width <= 320
            or not 1 <= height <= 320
            or frame_rate != 15
            or duration <= 0
            or duration * 1000 > _PREVIEW_MAX_DURATION_MS
        ):
            raise AVMediaAdapterError("media_output_invalid")
        if audio_disposition == "present_bound":
            if type(audio) is not dict:
                raise AVMediaAdapterError("media_output_invalid")
            try:
                sample_rate = int(cast(str | int, audio.get("sample_rate", 0)))
                channels = int(cast(str | int, audio.get("channels", 0)))
            except (ValueError, TypeError) as exc:
                raise AVMediaAdapterError("media_output_invalid") from exc
            if (
                audio.get("codec_name") != "aac"
                or sample_rate != 48_000
                or channels != 1
                or audio.get("channel_layout") != "mono"
            ):
                raise AVMediaAdapterError("media_output_invalid")
        elif audio is not None:
            raise AVMediaAdapterError("media_output_invalid")
        milliseconds = duration * 1000
        if milliseconds.denominator != 1:
            raise AVMediaAdapterError("media_output_invalid")
        return int(milliseconds)

    def execute_preview(
        self,
        *,
        store: PrivateAVReconstructionStore,
        receipt: object,
        output_handle: str,
        deadline: float,
        cancellation: CancellationProbe | None = None,
    ) -> bytearray:
        """Create one verified selected-member derivative inside the qualified adapter."""

        from ..core.av_reconstruction import AVReconstructionReceipt

        if (
            type(store) is not PrivateAVReconstructionStore
            or type(receipt) is not AVReconstructionReceipt
        ):
            raise AVMediaAdapterError("preview_source_unavailable")
        # CRITICAL: reject before allocating private scratch, and own the first lease before
        # attempting the second so every partial-admission failure is cleaned exactly once.
        _preview_io_remaining(deadline, time.monotonic)
        _raise_if_cancelled(cancellation)
        source = create_output_lease(self._scratch_root, suffix=".mp4")
        output: OwnedOutputLease | None = None
        try:
            output = create_output_lease(self._scratch_root, suffix=".mp4")
            store.copy_selected_output(
                receipt,
                output_handle,
                source,
                maximum_bytes=_PREVIEW_MAX_SOURCE_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            source_probe = self._preview_probe(
                source.path,
                maximum_bytes=_PREVIEW_MAX_SOURCE_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            source_duration = self._validate_preview_probe(source_probe, source=True)
            invocation = self._preview_transcode_invocation(
                source.path,
                output,
                deadline=deadline,
                source_duration_ms=source_duration,
            )
            self._run_ffmpeg(invocation, cancellation)
            output_probe = self._preview_probe(
                output.path,
                maximum_bytes=_PREVIEW_MAX_OUTPUT_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            output_duration = self._validate_preview_probe(output_probe, source=False)
            if abs(output_duration - source_duration) > 100:
                raise AVMediaAdapterError("media_output_invalid")
            body, _ = _read_regular_media_body(
                output.path,
                _PREVIEW_MAX_OUTPUT_BYTES,
                deadline=deadline,
                cancellation=cancellation,
            )
            return body
        finally:
            cleanup_error: Exception | None = None
            leases = (output, source) if output is not None else (source,)
            for lease in leases:
                try:
                    lease.release()
                except (OSError, RuntimeError, MediaProcessError) as exc:
                    cleanup_error = cleanup_error or exc
            if cleanup_error is not None:
                raise AVMediaAdapterError("cleanup_failed") from cleanup_error

    def _run_ffmpeg(
        self,
        invocation: MediaProcessInvocation,
        cancellation: CancellationProbe | None,
    ) -> None:
        _raise_if_cancelled(cancellation)
        capability = qualified_ffmpeg_capability()
        with _pin_exact_executable(self._ffmpeg_path, capability.ffmpeg_sha256):
            capture = self._runner.run(invocation, cancellation=cancellation)
        status = cast(ProcessStatus, capture.status)
        if status is not ProcessStatus.SUCCEEDED:
            raise _probe_failure(status, cleanup_succeeded=capture.cleanup_succeeded)
        if not capture.cleanup_succeeded:
            raise AVMediaAdapterError("cleanup_failed")
        _raise_if_cancelled(cancellation)

    def _m26_crop_invocation(
        self,
        source: Path,
        output: OwnedOutputLease,
        *,
        contribution_frames: int,
        contribution_samples: int,
    ) -> MediaProcessInvocation:
        capability = qualified_ffmpeg_capability()
        limits = qualified_av_limits()
        executable = str(self._ffmpeg_path)
        # IMPORTANT: ffmpeg applies codec_whitelist to both decode and encode sides. Omitting the
        # qualified encoders makes the real crop fail even though a mock process runner succeeds.
        codecs = tuple(
            dict.fromkeys(
                (
                    *capability.video_decoders,
                    *capability.audio_decoders,
                    *capability.video_encoders,
                    *capability.audio_encoders,
                )
            )
        )
        filters = (
            "[0:v:0]"
            f"trim=start_frame=0:end_frame={contribution_frames},"
            "setpts=PTS-STARTPTS,format=yuv420p[v];"
            "[0:a:0]"
            f"atrim=start_sample=0:end_sample={contribution_samples},"
            "asetpts=PTS-STARTPTS,aresample=32000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo[a]"
        )
        argv = (
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            ",".join(capability.demuxers),
            "-codec_whitelist",
            ",".join(codecs),
            "-i",
            str(source),
            "-filter_complex",
            filters,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-r",
            "24",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "32000",
            "-ac",
            "2",
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
            "-f",
            "mp4",
            "-n",
            str(output.path),
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=executable,
            argv=argv,
            protocol_whitelist=("file",),
            format_whitelist=capability.demuxers,
            codec_whitelist=codecs,
            timeout_seconds=Decimal(limits.process_timeout_ms) / Decimal(1000),
            max_stdout_bytes=limits.max_probe_stdout_bytes,
            max_stderr_bytes=limits.max_process_stderr_bytes,
            output_leases=(output,),
            max_owned_output_bytes=limits.max_input_bytes_per_segment,
        )

    def derive_m26_input(
        self,
        *,
        segment_id: str,
        original_receipt_fingerprint: str,
        original_source: AVSegmentSource,
        contribution_frames: int,
        contribution_samples: int,
        cancellation: CancellationProbe | None = None,
    ) -> M26DerivedMediaLease:
        """Verify one original 24/32 artifact and crop only its sampled lattice tail."""

        limits = qualified_av_limits()
        if (
            type(segment_id) is not str
            or type(original_receipt_fingerprint) is not str
            or not isinstance(original_source, AVSegmentSource)
            or type(contribution_frames) is not int
            or type(contribution_samples) is not int
            or not 1 <= contribution_frames <= limits.max_video_frames
            or not 1 <= contribution_samples <= limits.max_audio_sample_frames
            or contribution_frames * 32_000 != contribution_samples * 24
        ):
            raise AVMediaAdapterError("m26_crop_policy_invalid")
        _raise_if_cancelled(cancellation)
        if not self._process_slots.acquire(blocking=False):
            raise AVMediaAdapterError("resource_limit")
        staged: OwnedOutputLease | None = None
        derived: OwnedOutputLease | None = None
        success = False
        try:
            staged = create_output_lease(self._scratch_root, suffix=".media")
            length, fingerprint = original_source.stream_into(
                staged.path,
                cancellation=cancellation,
            )
            if (
                length != original_source.byte_length
                or fingerprint != original_source.content_fingerprint
            ):
                raise AVMediaAdapterError("inspection_stale")
            original = self._inspect_staged_output(
                staged,
                segment_id=segment_id,
                authority_fingerprint=original_receipt_fingerprint,
                maximum_bytes=limits.max_input_bytes_per_segment,
                maximum_duration_ms=limits.max_duration_ms_per_segment,
                cancellation=cancellation,
            )
            video = original.video
            audio = original.audio
            target = qualified_av_target_profile()
            if (
                original.artifact_output_fingerprint != original_source.content_fingerprint
                or original.artifact_byte_length != original_source.byte_length
                or original.container != "mp4"
                or original.warning_codes
                or video is None
                or audio is None
                or video.codec != "h264"
                or video.pixel_format != "yuv420p"
                or video.color_range != target.color_range
                or video.color_space != target.color_space
                or video.color_primaries != target.color_primaries
                or video.color_transfer != target.color_transfer
                or video.rotation_degrees != 0
                or video.frame_rate != AVRational(24, 1)
                or video.decoded_frame_count < contribution_frames
                or audio.codec != "aac"
                or audio.sample_format != "fltp"
                or audio.sample_rate != 32_000
                or audio.channels != 2
                or audio.channel_layout != "stereo"
                or audio.decoded_sample_count < contribution_samples
            ):
                raise AVMediaAdapterError("m26_source_contribution_invalid")
            # CRITICAL: crop endpoints are exact zero-based frame/sample counts. Converting this
            # boundary to rounded seconds can remove requested content or retain lattice padding.
            derived = create_output_lease(self._scratch_root, suffix=".mp4")
            self._run_ffmpeg(
                self._m26_crop_invocation(
                    staged.path,
                    derived,
                    contribution_frames=contribution_frames,
                    contribution_samples=contribution_samples,
                ),
                cancellation,
            )
            with _pin_regular_media(
                derived.path,
                limits.max_input_bytes_per_segment,
                cancellation=cancellation,
            ) as (derived_length, derived_fingerprint):
                if derived_length <= 0:
                    raise AVMediaAdapterError("media_output_invalid")
            success = True
            return M26DerivedMediaLease(
                self,
                derived,
                original,
                derived_length,
                derived_fingerprint,
            )
        except AVTransportError as exc:
            code = "cancelled" if exc.code == "transport_cancelled" else "inspection_unavailable"
            raise AVMediaAdapterError(code) from exc
        finally:
            cleanup_error: Exception | None = None
            if staged is not None:
                try:
                    staged.release()
                except Exception as exc:  # noqa: BLE001 - translated without a private path.
                    cleanup_error = cleanup_error or exc
            if derived is not None and not success:
                try:
                    derived.release()
                except Exception as exc:  # noqa: BLE001 - translated without a private path.
                    cleanup_error = cleanup_error or exc
            self._process_slots.release()
            if cleanup_error is not None:
                raise AVMediaAdapterError("cleanup_failed") from cleanup_error

    def _inspect_staged_output(
        self,
        lease: OwnedOutputLease,
        *,
        segment_id: str,
        authority_fingerprint: str,
        maximum_bytes: int,
        maximum_duration_ms: int,
        cancellation: CancellationProbe | None,
    ) -> AVMediaDescriptor:
        _raise_if_cancelled(cancellation)
        with _pin_regular_media(lease.path, maximum_bytes) as (size, fingerprint):
            invocation = self._probe_invocation(lease.path)
            capability = qualified_ffmpeg_capability()
            with _pin_exact_executable(
                self._ffprobe_path,
                capability.ffprobe_sha256,
            ):
                capture = self._runner.run(invocation, cancellation=cancellation)
            status = cast(ProcessStatus, capture.status)
            if status is not ProcessStatus.SUCCEEDED:
                raise _probe_failure(status, cleanup_succeeded=capture.cleanup_succeeded)
            if not capture.cleanup_succeeded:
                raise AVMediaAdapterError("cleanup_failed")
            _raise_if_cancelled(cancellation)
            try:
                descriptor = _parse_descriptor(
                    capture.stdout,
                    segment_id=segment_id,
                    artifact_receipt_fingerprint=authority_fingerprint,
                    artifact_output_fingerprint=fingerprint,
                    artifact_byte_length=size,
                    inspected_at_ms=self._now_ms(),
                    maximum_duration_ms=maximum_duration_ms,
                )
                _raise_if_cancelled(cancellation)
                return descriptor
            except AVMediaAdapterError as exc:
                if exc.code == "inspection_invalid":
                    raise AVMediaAdapterError("media_output_invalid") from exc
                raise

    def _transform_invocation(
        self,
        segment: AVSegmentPlan,
        input_path: Path,
        output: OwnedOutputLease,
        silence_path: Path | None,
        maximum_output_bytes: int,
    ) -> MediaProcessInvocation:
        capability = qualified_ffmpeg_capability()
        limits = qualified_av_limits()
        target = qualified_av_target_profile()
        operations = set(segment.operations)
        if (
            AVOperation.PASSTHROUGH in operations
            or not operations
            or type(maximum_output_bytes) is not int
            or maximum_output_bytes <= 0
            or maximum_output_bytes > limits.max_aggregate_output_bytes
        ):
            raise AVMediaAdapterError("operation_unsupported")
        if any(item not in capability.operations for item in operations):
            raise AVMediaAdapterError("operation_unsupported")
        executable = str(self._ffmpeg_path)
        argv: list[str] = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            ",".join(capability.demuxers),
            "-codec_whitelist",
            ",".join((*capability.video_decoders, *capability.audio_decoders)),
            "-i",
            str(input_path),
        ]
        if AVOperation.INSERT_SILENCE in operations:
            if silence_path is None:
                raise AVMediaAdapterError("operation_unsupported")
            argv.extend(("-i", str(silence_path)))
        elif silence_path is not None:
            raise AVMediaAdapterError("operation_unsupported")

        filters: list[str] = []
        if AVOperation.NORMALIZE_VIDEO in operations:
            filters.append(
                "[0:v:0]"
                f"scale={target.width}:{target.height}:flags=lanczos,"
                f"fps={target.frame_rate.numerator}/{target.frame_rate.denominator},"
                f"format={target.pixel_format},"
                f"trim=end_frame={segment.accounting.emitted_frames},"
                "setpts=PTS-STARTPTS[v]"
            )
        audio_source: str | None = None
        if AVOperation.INSERT_SILENCE in operations:
            audio_source = "1:a:0"
        elif AVOperation.NORMALIZE_AUDIO in operations:
            audio_source = "0:a:0"
        if audio_source is not None:
            filters.append(
                f"[{audio_source}]"
                f"aresample={target.sample_rate},"
                f"aformat=sample_fmts={target.sample_format}:"
                f"channel_layouts={target.channel_layout},"
                f"atrim=end_sample={segment.accounting.emitted_samples},"
                "asetpts=PTS-STARTPTS[a]"
            )
        if filters:
            argv.extend(("-filter_complex", ";".join(filters)))

        if AVOperation.NORMALIZE_VIDEO in operations:
            argv.extend(
                (
                    "-map",
                    "[v]",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-x264-params",
                    "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
                )
            )
        else:
            argv.extend(("-map", "0:v:0", "-c:v", "copy"))
        if audio_source is not None:
            argv.extend(("-map", "[a]", "-c:a", "aac", "-b:a", "192k"))
        else:
            argv.extend(("-map", "0:a:0", "-c:a", "copy"))
        argv.extend(
            (
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-metadata",
                "encoder=",
                "-color_range",
                target.color_range,
                "-colorspace",
                target.color_space,
                "-color_primaries",
                target.color_primaries,
                "-color_trc",
                target.color_transfer,
                "-video_track_timescale",
                str(target.video_time_base.denominator),
                "-movflags",
                "+faststart",
                "-threads",
                "1",
                "-f",
                target.container,
                "-n",
                str(output.path),
            )
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=executable,
            argv=tuple(argv),
            protocol_whitelist=("file",),
            format_whitelist=capability.demuxers,
            codec_whitelist=tuple(
                dict.fromkeys((*capability.video_decoders, *capability.audio_decoders))
            ),
            timeout_seconds=Decimal(limits.process_timeout_ms) / Decimal(1000),
            max_stdout_bytes=limits.max_probe_stdout_bytes,
            max_stderr_bytes=limits.max_process_stderr_bytes,
            output_leases=(output,),
            max_owned_output_bytes=maximum_output_bytes,
        )

    def _aggregate_invocation(
        self,
        inputs: tuple[Path, ...],
        output: OwnedOutputLease,
        maximum_output_bytes: int,
        *,
        expected_frames: int,
        expected_samples: int,
        segment_frames: tuple[int, ...],
        segment_samples: tuple[int, ...],
    ) -> MediaProcessInvocation:
        if not inputs or len(inputs) > qualified_av_limits().max_segments:
            raise AVMediaAdapterError("resource_limit")
        limits = qualified_av_limits()
        if (
            type(maximum_output_bytes) is not int
            or maximum_output_bytes <= 0
            or maximum_output_bytes > limits.max_aggregate_output_bytes
            or type(expected_frames) is not int
            or not 1 <= expected_frames <= limits.max_video_frames
            or type(expected_samples) is not int
            or not 1 <= expected_samples <= limits.max_audio_sample_frames
            or type(segment_frames) is not tuple
            or type(segment_samples) is not tuple
            or len(segment_frames) != len(inputs)
            or len(segment_samples) != len(inputs)
            or any(type(value) is not int or value <= 0 for value in segment_frames)
            or any(type(value) is not int or value <= 0 for value in segment_samples)
            or sum(segment_frames) != expected_frames
            or sum(segment_samples) != expected_samples
        ):
            raise AVMediaAdapterError("resource_limit")
        target = qualified_av_target_profile()
        executable = str(self._ffmpeg_path)
        argv: list[str] = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "mov",
            "-codec_whitelist",
            "h264,aac",
        ]
        for path in inputs:
            argv.extend(("-i", str(path)))
        # CRITICAL: AAC decoding includes each input's encoder tail. Trim by that segment's
        # accepted accounting before concat; a final-only trim shifts later audio and lets
        # concat's longest-stream rule put gaps in video timestamps.
        cadence = f"N*{target.frame_rate.denominator}/({target.frame_rate.numerator}*TB)"
        prepared = "".join(
            f"[{index}:v:0]trim=end_frame={frames},setpts={cadence}[v{index}];"
            f"[{index}:a:0]atrim=end_sample={samples},asetpts=N/SR/TB[a{index}];"
            for index, (frames, samples) in enumerate(
                zip(segment_frames, segment_samples, strict=True)
            )
        )
        labels = "".join(f"[v{index}][a{index}]" for index in range(len(inputs)))
        filter_value = (
            f"{prepared}{labels}concat=n={len(inputs)}:v=1:a=1[concat_v][concat_a];"
            f"[concat_v]trim=end_frame={expected_frames},setpts={cadence}[v];"
            f"[concat_a]atrim=end_sample={expected_samples},asetpts=N/SR/TB[a]"
        )
        argv.extend(
            (
                "-filter_complex",
                filter_value,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-x264-params",
                "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-metadata",
                "encoder=",
                "-color_range",
                target.color_range,
                "-colorspace",
                target.color_space,
                "-color_primaries",
                target.color_primaries,
                "-color_trc",
                target.color_transfer,
                "-video_track_timescale",
                str(target.video_time_base.denominator),
                "-movflags",
                "+faststart",
                "-threads",
                "1",
                "-f",
                target.container,
                "-n",
                str(output.path),
            )
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=executable,
            argv=tuple(argv),
            protocol_whitelist=("file",),
            format_whitelist=("mov",),
            codec_whitelist=("h264", "aac"),
            timeout_seconds=Decimal(limits.process_timeout_ms) / Decimal(1000),
            max_stdout_bytes=limits.max_probe_stdout_bytes,
            max_stderr_bytes=limits.max_process_stderr_bytes,
            output_leases=(output,),
            max_owned_output_bytes=maximum_output_bytes,
        )

    def _seam_aggregate_invocation(
        self,
        inputs: tuple[Path, ...],
        output: OwnedOutputLease,
        maximum_output_bytes: int,
        *,
        seam_plan: AVSeamPlan,
    ) -> MediaProcessInvocation:
        if len(inputs) != len(seam_plan.segment_ids):
            raise AVMediaAdapterError("seam_graph_invalid")
        if seam_plan.crossfade_count == 0:
            # D5: the all-direct seam graph is the legacy aggregate, byte for byte.
            return self._aggregate_invocation(
                inputs,
                output,
                maximum_output_bytes,
                expected_frames=seam_plan.total_output_frames,
                expected_samples=seam_plan.total_output_samples,
                segment_frames=seam_plan.segment_emitted_frames,
                segment_samples=seam_plan.segment_emitted_samples,
            )
        limits = qualified_av_limits()
        if (
            not inputs
            or len(inputs) > limits.max_segments
            or type(maximum_output_bytes) is not int
            or maximum_output_bytes <= 0
            or maximum_output_bytes > limits.max_aggregate_output_bytes
        ):
            raise AVMediaAdapterError("resource_limit")
        target = qualified_av_target_profile()
        executable = str(self._ffmpeg_path)
        argv: list[str] = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "mov",
            "-codec_whitelist",
            "h264,aac",
        ]
        for path in inputs:
            argv.extend(("-i", str(path)))
        argv.extend(
            (
                "-filter_complex",
                _seam_filter_graph(seam_plan),
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-x264-params",
                "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-metadata",
                "encoder=",
                "-color_range",
                target.color_range,
                "-colorspace",
                target.color_space,
                "-color_primaries",
                target.color_primaries,
                "-color_trc",
                target.color_transfer,
                "-video_track_timescale",
                str(target.video_time_base.denominator),
                "-movflags",
                "+faststart",
                "-threads",
                "1",
                "-f",
                target.container,
                "-n",
                str(output.path),
            )
        )
        return MediaProcessInvocation(
            tool="ffmpeg",
            executable=executable,
            argv=tuple(argv),
            protocol_whitelist=("file",),
            format_whitelist=("mov",),
            codec_whitelist=("h264", "aac"),
            timeout_seconds=Decimal(limits.process_timeout_ms) / Decimal(1000),
            max_stdout_bytes=limits.max_probe_stdout_bytes,
            max_stderr_bytes=limits.max_process_stderr_bytes,
            output_leases=(output,),
            max_owned_output_bytes=maximum_output_bytes,
        )

    def _stage_into(
        self,
        segment: AVSegmentPlan,
        payload: bytes | AVSegmentSource,
        staged: OwnedOutputLease,
        cancellation: CancellationProbe | None,
    ) -> int:
        """Materialize one admitted input into a fresh staged scratch file.

        A complete ``bytes`` payload keeps the original whole-buffer semantics (the rollback
        oracle); an ``AVSegmentSource`` streams one bounded chunk at a time and is verified
        content-addressed against the plan descriptor.
        """

        if type(payload) is bytes:
            if (
                not payload
                or len(payload) != segment.descriptor.artifact_byte_length
                or "sha256:" + hashlib.sha256(payload).hexdigest()
                != segment.descriptor.artifact_output_fingerprint
            ):
                raise AVMediaAdapterError("inspection_stale")
            _write_new_file(staged.path, payload)
            return len(payload)
        if not isinstance(payload, AVSegmentSource):
            raise AVMediaAdapterError("operation_unsupported")
        return self._stream_source(segment, payload, staged, cancellation)

    def _stream_source(
        self,
        segment: AVSegmentPlan,
        source: AVSegmentSource,
        staged: OwnedOutputLease,
        cancellation: CancellationProbe | None,
    ) -> int:
        declared_length = segment.descriptor.artifact_byte_length
        if (
            source.byte_length != declared_length
            or source.content_fingerprint != segment.descriptor.artifact_output_fingerprint
        ):
            raise AVMediaAdapterError("inspection_stale")
        watchdog = _StreamSizeWatchdog(staged.path, declared_length, cancellation)
        try:
            length, fingerprint = source.stream_into(staged.path, cancellation=watchdog)
        except AVMediaAdapterError:
            raise
        except AVTransportError as exc:
            if watchdog.tripped:
                raise AVMediaAdapterError("inspection_stale") from exc
            if exc.code == "transport_cancelled":
                raise AVMediaAdapterError("cancelled") from exc
            if exc.code == "transport_identity_mismatch":
                raise AVMediaAdapterError("inspection_stale") from exc
            raise AVMediaAdapterError("inspection_unavailable") from exc
        except Exception as exc:  # noqa: BLE001 - a foreign source must fail closed, not leak.
            if watchdog.tripped:
                raise AVMediaAdapterError("inspection_stale") from exc
            raise AVMediaAdapterError("inspection_unavailable") from exc
        # SECURITY: the copy is untrusted until its size and pinned hash match the declared
        # identity; the size check runs first so an overlong copy never reaches the prober.
        try:
            staged_size = staged.path.lstat().st_size
        except OSError as exc:
            raise AVMediaAdapterError("inspection_unavailable") from exc
        if staged_size != declared_length:
            raise AVMediaAdapterError("inspection_stale")
        if (
            type(length) is not int
            or type(fingerprint) is not str
            or length != declared_length
            or fingerprint != segment.descriptor.artifact_output_fingerprint
        ):
            raise AVMediaAdapterError("inspection_stale")
        return length

    def execute_plan(
        self,
        *,
        plan: AVReconstructionPlan,
        approval: AVReconstructionApproval,
        input_payloads: tuple[tuple[str, bytes | AVSegmentSource], ...],
        aggregate_output: OwnedOutputLease,
        derived_outputs: tuple[tuple[str, OwnedOutputLease], ...],
        cancellation: CancellationProbe | None = None,
    ) -> AVMediaExecutionResult:
        if (
            type(plan) is not AVReconstructionPlan
            or type(approval) is not AVReconstructionApproval
            or type(input_payloads) is not tuple
            or type(derived_outputs) is not tuple
            or not isinstance(aggregate_output, OwnedOutputLease)
        ):
            raise AVMediaAdapterError("operation_unsupported")
        _raise_if_cancelled(cancellation)
        now = self._now_ms()
        try:
            approval.assert_executable(plan, now_ms=now)
        except AVReconstructionError as exc:
            raise AVMediaAdapterError("approval_stale") from exc
        limits = qualified_av_limits()
        if tuple(item[0] for item in input_payloads) != tuple(
            item.segment_id for item in plan.segments
        ):
            raise AVMediaAdapterError("inspection_stale")
        input_by_id = dict(input_payloads)
        if len(input_by_id) != len(input_payloads):
            raise AVMediaAdapterError("inspection_stale")
        required_derived = tuple(
            item.segment_id
            for item in plan.segments
            if item.operations != (AVOperation.PASSTHROUGH,)
        )
        if tuple(item[0] for item in derived_outputs) != required_derived:
            raise AVMediaAdapterError("operation_unsupported")
        derived_by_id = dict(derived_outputs)
        if len(derived_by_id) != len(derived_outputs) or not all(
            isinstance(item, OwnedOutputLease) for item in derived_by_id.values()
        ):
            raise AVMediaAdapterError("operation_unsupported")
        output_paths = [aggregate_output.path, *(item.path for item in derived_by_id.values())]
        if len({os.path.normcase(str(item)) for item in output_paths}) != len(output_paths):
            raise AVMediaAdapterError("operation_unsupported")
        if not self._process_slots.acquire(blocking=False):
            raise AVMediaAdapterError("resource_limit")

        scratch_leases: list[OwnedOutputLease] = []
        outputs = (aggregate_output, *derived_by_id.values())
        source_pins = ExitStack()
        success = False
        try:
            staged_by_id: dict[str, OwnedOutputLease] = {}
            total_input_bytes = 0
            temp_bytes = 0
            # SECURITY: public typed descriptors are assertions, not executable provenance. Every
            # exact staged source must be freshly probed and reconciled before the first transform.
            # M20-08: verification is one pass per segment (stage, pin, probe, reconcile); a
            # source-backed non-passthrough staged copy is released as soon as it verifies, so at
            # most one such copy is alive at a time. Its transform later re-streams the same
            # content-addressed bytes, and the fresh copy's hash transfers this pass's probe
            # verdict to it. Complete-payload ``bytes`` inputs keep their staged copy for the
            # whole run, exactly as before -- that path is the rollback oracle.
            for segment in plan.segments:
                payload = input_by_id[segment.segment_id]
                staged = create_output_lease(self._scratch_root, suffix=".media")
                scratch_leases.append(staged)
                staged_length = self._stage_into(segment, payload, staged, cancellation)
                total_input_bytes += staged_length
                if total_input_bytes > plan.limits.max_aggregate_input_bytes:
                    raise AVMediaAdapterError("resource_limit")
                temp_bytes += staged_length
                if temp_bytes > limits.max_temp_bytes:
                    raise AVMediaAdapterError("resource_limit")
                rolling = type(payload) is not bytes and segment.operations != (
                    AVOperation.PASSTHROUGH,
                )
                verify_pins = ExitStack() if rolling else source_pins
                try:
                    pinned_size, pinned_fingerprint = verify_pins.enter_context(
                        _pin_regular_media(
                            staged.path,
                            plan.limits.max_input_bytes_per_segment,
                        )
                    )
                    if (
                        pinned_size != segment.descriptor.artifact_byte_length
                        or pinned_fingerprint != segment.descriptor.artifact_output_fingerprint
                    ):
                        raise AVMediaAdapterError("inspection_stale")
                    try:
                        fresh_descriptor = self._inspect_staged_output(
                            staged,
                            segment_id=segment.segment_id,
                            authority_fingerprint=segment.artifact_receipt_fingerprint,
                            maximum_bytes=plan.limits.max_input_bytes_per_segment,
                            maximum_duration_ms=plan.limits.max_duration_ms_per_segment,
                            cancellation=cancellation,
                        )
                    except AVMediaAdapterError as exc:
                        if exc.code == "media_output_invalid":
                            raise AVMediaAdapterError("inspection_stale") from exc
                        raise
                    if _descriptor_fact_projection(fresh_descriptor) != _descriptor_fact_projection(
                        segment.descriptor
                    ):
                        raise AVMediaAdapterError("inspection_stale")
                finally:
                    if rolling:
                        verify_pins.close()
                if rolling:
                    staged.release()
                else:
                    staged_by_id[segment.segment_id] = staged

            aggregate_sources: list[Path] = []
            derived_descriptors: list[AVMediaDescriptor] = []
            for segment in plan.segments:
                if segment.operations == (AVOperation.PASSTHROUGH,):
                    aggregate_sources.append(staged_by_id[segment.segment_id].path)
                    continue
                silence_lease: OwnedOutputLease | None = None
                if AVOperation.INSERT_SILENCE in segment.operations:
                    silence_lease = create_output_lease(self._scratch_root, suffix=".wav")
                    scratch_leases.append(silence_lease)
                    silence_payload = _silence_wav(segment.accounting.emitted_samples)
                    if temp_bytes + len(silence_payload) > limits.max_temp_bytes:
                        raise AVMediaAdapterError("resource_limit")
                    _write_new_file(silence_lease.path, silence_payload)
                    temp_bytes += len(silence_payload)
                    silence_size, silence_fingerprint = source_pins.enter_context(
                        _pin_regular_media(
                            silence_lease.path,
                            limits.max_temp_bytes,
                        )
                    )
                    if (
                        silence_size != len(silence_payload)
                        or silence_fingerprint
                        != "sha256:" + hashlib.sha256(silence_payload).hexdigest()
                    ):
                        raise AVMediaAdapterError("media_output_invalid")
                derived = derived_by_id[segment.segment_id]
                payload = input_by_id[segment.segment_id]
                transform_pins = ExitStack()
                transform_staged: OwnedOutputLease | None = None
                try:
                    if segment.segment_id in staged_by_id:
                        transform_source = staged_by_id[segment.segment_id].path
                    else:
                        # M20-08 rolling assembly: re-stream the verified content-addressed
                        # bytes for this one transform; the hash equality below carries the
                        # verification pass's probe verdict onto the fresh copy.
                        transform_staged = create_output_lease(self._scratch_root, suffix=".media")
                        scratch_leases.append(transform_staged)
                        if not isinstance(payload, AVSegmentSource):
                            raise AVMediaAdapterError("operation_unsupported")
                        staged_length = self._stream_source(
                            segment, payload, transform_staged, cancellation
                        )
                        temp_bytes += staged_length
                        if temp_bytes > limits.max_temp_bytes:
                            raise AVMediaAdapterError("resource_limit")
                        pinned_size, pinned_fingerprint = transform_pins.enter_context(
                            _pin_regular_media(
                                transform_staged.path,
                                plan.limits.max_input_bytes_per_segment,
                            )
                        )
                        if (
                            pinned_size != segment.descriptor.artifact_byte_length
                            or pinned_fingerprint != segment.descriptor.artifact_output_fingerprint
                        ):
                            raise AVMediaAdapterError("inspection_stale")
                        transform_source = transform_staged.path
                    remaining_temp_bytes = limits.max_temp_bytes - temp_bytes
                    if remaining_temp_bytes <= 0:
                        raise AVMediaAdapterError("resource_limit")
                    invocation = self._transform_invocation(
                        segment,
                        transform_source,
                        derived,
                        None if silence_lease is None else silence_lease.path,
                        min(limits.max_aggregate_output_bytes, remaining_temp_bytes),
                    )
                    self._run_ffmpeg(invocation, cancellation)
                    descriptor = self._inspect_staged_output(
                        derived,
                        segment_id=segment.segment_id,
                        authority_fingerprint=plan.fingerprint,
                        maximum_bytes=plan.limits.max_aggregate_output_bytes,
                        maximum_duration_ms=plan.limits.max_duration_ms_per_segment,
                        cancellation=cancellation,
                    )
                finally:
                    transform_pins.close()
                    if transform_staged is not None:
                        transform_staged.release()
                _assert_exact_output(
                    descriptor,
                    expected_frames=segment.accounting.emitted_frames,
                    expected_samples=segment.accounting.emitted_samples,
                )
                derived_descriptors.append(descriptor)
                temp_bytes += descriptor.artifact_byte_length
                if temp_bytes > limits.max_temp_bytes:
                    raise AVMediaAdapterError("resource_limit")
                pinned_size, pinned_fingerprint = source_pins.enter_context(
                    _pin_regular_media(
                        derived.path,
                        limits.max_aggregate_output_bytes,
                    )
                )
                if (
                    pinned_size != descriptor.artifact_byte_length
                    or pinned_fingerprint != descriptor.artifact_output_fingerprint
                ):
                    raise AVMediaAdapterError("media_output_invalid")
                aggregate_sources.append(derived.path)

            remaining_temp_bytes = limits.max_temp_bytes - temp_bytes
            if remaining_temp_bytes <= 0:
                raise AVMediaAdapterError("resource_limit")
            aggregate_invocation = self._aggregate_invocation(
                tuple(aggregate_sources),
                aggregate_output,
                min(limits.max_aggregate_output_bytes, remaining_temp_bytes),
                expected_frames=sum(item.accounting.emitted_frames for item in plan.segments),
                expected_samples=sum(item.accounting.emitted_samples for item in plan.segments),
                segment_frames=tuple(item.accounting.emitted_frames for item in plan.segments),
                segment_samples=tuple(item.accounting.emitted_samples for item in plan.segments),
            )
            self._run_ffmpeg(aggregate_invocation, cancellation)
            aggregate_descriptor = self._inspect_staged_output(
                aggregate_output,
                segment_id="reconstruction.aggregate",
                authority_fingerprint=plan.fingerprint,
                maximum_bytes=plan.limits.max_aggregate_output_bytes,
                maximum_duration_ms=plan.limits.max_total_duration_ms,
                cancellation=cancellation,
            )
            expected_frames = sum(item.accounting.emitted_frames for item in plan.segments)
            expected_samples = sum(item.accounting.emitted_samples for item in plan.segments)
            _assert_exact_output(
                aggregate_descriptor,
                expected_frames=expected_frames,
                expected_samples=expected_samples,
            )
            temp_bytes += aggregate_descriptor.artifact_byte_length
            if temp_bytes > limits.max_temp_bytes:
                raise AVMediaAdapterError("resource_limit")
            execution_fingerprint = canonical_fingerprint(
                {
                    "plan_fingerprint": plan.fingerprint,
                    "approval_fingerprint": approval.fingerprint,
                    "capability_fingerprint": plan.capability.fingerprint,
                    "aggregate": aggregate_descriptor.to_wire(),
                    "derived": [item.to_wire() for item in derived_descriptors],
                }
            )
            result = AVMediaExecutionResult(
                execution_fingerprint=execution_fingerprint,
                aggregate_descriptor=aggregate_descriptor,
                derived_descriptors=tuple(derived_descriptors),
            )
            _raise_if_cancelled(cancellation)
            success = True
            return result
        except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError) as exc:
            if isinstance(exc, AVMediaAdapterError):
                raise
            raise AVMediaAdapterError("media_process_failed") from exc
        finally:
            cleanup_failed = False
            try:
                source_pins.close()
            except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError):
                cleanup_failed = True
            for lease in reversed(scratch_leases):
                try:
                    lease.release()
                except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError):
                    cleanup_failed = True
            if not success or cleanup_failed:
                for lease in reversed(outputs):
                    try:
                        lease.release()
                    except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError):
                        cleanup_failed = True
            self._process_slots.release()
            if cleanup_failed:
                raise AVMediaAdapterError("cleanup_failed")

    def execute_seam_plan(
        self,
        *,
        plan: AVReconstructionPlan,
        approval: AVReconstructionApproval,
        seam_plan: AVSeamPlan,
        seam_approval: AVSeamApproval,
        input_payloads: tuple[tuple[str, bytes | AVSegmentSource], ...],
        aggregate_output: OwnedOutputLease,
        cancellation: CancellationProbe | None = None,
    ) -> AVSeamExecutionResult:
        """Execute one approved seam plan into a single aggregate output.

        Per-segment normalization follows the base plan's exact operations into
        scratch leases (never published); only the seam aggregate leaves this
        method.  Staged copies are whole-run here — the M20-08 rolling release
        is an optimization of the legacy path, not part of its contract — and
        every ceiling from the base plan's limits still applies.
        """

        if (
            type(plan) is not AVReconstructionPlan
            or type(approval) is not AVReconstructionApproval
            or type(seam_plan) is not AVSeamPlan
            or type(seam_approval) is not AVSeamApproval
            or type(input_payloads) is not tuple
            or not isinstance(aggregate_output, OwnedOutputLease)
        ):
            raise AVMediaAdapterError("operation_unsupported")
        _raise_if_cancelled(cancellation)
        now = self._now_ms()
        try:
            approval.assert_executable(plan, now_ms=now)
        except AVReconstructionError as exc:
            raise AVMediaAdapterError("approval_stale") from exc
        try:
            seam_approval.assert_executable(
                seam_plan,
                base_plan=plan,
                base_approval=approval,
                now_ms=now,
            )
        except AVSeamPolicyError as exc:
            raise AVMediaAdapterError(f"seam_admission:{exc.args[0]}") from exc
        if (
            seam_plan.segment_ids != tuple(item.segment_id for item in plan.segments)
            or seam_plan.segment_emitted_frames
            != tuple(item.accounting.emitted_frames for item in plan.segments)
            or seam_plan.segment_emitted_samples
            != tuple(item.accounting.emitted_samples for item in plan.segments)
        ):
            raise AVMediaAdapterError("seam_admission:seam_binding_mismatch")
        limits = qualified_av_limits()
        if tuple(item[0] for item in input_payloads) != seam_plan.segment_ids:
            raise AVMediaAdapterError("inspection_stale")
        input_by_id = dict(input_payloads)
        if len(input_by_id) != len(input_payloads):
            raise AVMediaAdapterError("inspection_stale")
        if not self._process_slots.acquire(blocking=False):
            raise AVMediaAdapterError("resource_limit")

        scratch_leases: list[OwnedOutputLease] = []
        source_pins = ExitStack()
        success = False
        try:
            staged_by_id: dict[str, OwnedOutputLease] = {}
            total_input_bytes = 0
            temp_bytes = 0
            # SECURITY: descriptors are assertions; every staged source is freshly
            # probed and reconciled before the first transform, as in execute_plan.
            for segment in plan.segments:
                payload = input_by_id[segment.segment_id]
                staged = create_output_lease(self._scratch_root, suffix=".media")
                scratch_leases.append(staged)
                staged_length = self._stage_into(segment, payload, staged, cancellation)
                total_input_bytes += staged_length
                if total_input_bytes > plan.limits.max_aggregate_input_bytes:
                    raise AVMediaAdapterError("resource_limit")
                temp_bytes += staged_length
                if temp_bytes > limits.max_temp_bytes:
                    raise AVMediaAdapterError("resource_limit")
                pinned_size, pinned_fingerprint = source_pins.enter_context(
                    _pin_regular_media(
                        staged.path,
                        plan.limits.max_input_bytes_per_segment,
                    )
                )
                if (
                    pinned_size != segment.descriptor.artifact_byte_length
                    or pinned_fingerprint != segment.descriptor.artifact_output_fingerprint
                ):
                    raise AVMediaAdapterError("inspection_stale")
                try:
                    fresh_descriptor = self._inspect_staged_output(
                        staged,
                        segment_id=segment.segment_id,
                        authority_fingerprint=segment.artifact_receipt_fingerprint,
                        maximum_bytes=plan.limits.max_input_bytes_per_segment,
                        maximum_duration_ms=plan.limits.max_duration_ms_per_segment,
                        cancellation=cancellation,
                    )
                except AVMediaAdapterError as exc:
                    if exc.code == "media_output_invalid":
                        raise AVMediaAdapterError("inspection_stale") from exc
                    raise
                if _descriptor_fact_projection(fresh_descriptor) != _descriptor_fact_projection(
                    segment.descriptor
                ):
                    raise AVMediaAdapterError("inspection_stale")
                staged_by_id[segment.segment_id] = staged

            aggregate_sources: list[Path] = []
            for segment in plan.segments:
                if segment.operations == (AVOperation.PASSTHROUGH,):
                    aggregate_sources.append(staged_by_id[segment.segment_id].path)
                    continue
                silence_lease: OwnedOutputLease | None = None
                if AVOperation.INSERT_SILENCE in segment.operations:
                    silence_lease = create_output_lease(self._scratch_root, suffix=".wav")
                    scratch_leases.append(silence_lease)
                    silence_payload = _silence_wav(segment.accounting.emitted_samples)
                    if temp_bytes + len(silence_payload) > limits.max_temp_bytes:
                        raise AVMediaAdapterError("resource_limit")
                    _write_new_file(silence_lease.path, silence_payload)
                    temp_bytes += len(silence_payload)
                    silence_size, silence_fingerprint = source_pins.enter_context(
                        _pin_regular_media(
                            silence_lease.path,
                            limits.max_temp_bytes,
                        )
                    )
                    if (
                        silence_size != len(silence_payload)
                        or silence_fingerprint
                        != "sha256:" + hashlib.sha256(silence_payload).hexdigest()
                    ):
                        raise AVMediaAdapterError("media_output_invalid")
                normalized = create_output_lease(self._scratch_root, suffix=".media")
                scratch_leases.append(normalized)
                remaining_temp_bytes = limits.max_temp_bytes - temp_bytes
                if remaining_temp_bytes <= 0:
                    raise AVMediaAdapterError("resource_limit")
                invocation = self._transform_invocation(
                    segment,
                    staged_by_id[segment.segment_id].path,
                    normalized,
                    None if silence_lease is None else silence_lease.path,
                    min(limits.max_aggregate_output_bytes, remaining_temp_bytes),
                )
                self._run_ffmpeg(invocation, cancellation)
                descriptor = self._inspect_staged_output(
                    normalized,
                    segment_id=segment.segment_id,
                    authority_fingerprint=plan.fingerprint,
                    maximum_bytes=plan.limits.max_aggregate_output_bytes,
                    maximum_duration_ms=plan.limits.max_duration_ms_per_segment,
                    cancellation=cancellation,
                )
                _assert_exact_output(
                    descriptor,
                    expected_frames=segment.accounting.emitted_frames,
                    expected_samples=segment.accounting.emitted_samples,
                )
                temp_bytes += descriptor.artifact_byte_length
                if temp_bytes > limits.max_temp_bytes:
                    raise AVMediaAdapterError("resource_limit")
                pinned_size, pinned_fingerprint = source_pins.enter_context(
                    _pin_regular_media(
                        normalized.path,
                        limits.max_aggregate_output_bytes,
                    )
                )
                if (
                    pinned_size != descriptor.artifact_byte_length
                    or pinned_fingerprint != descriptor.artifact_output_fingerprint
                ):
                    raise AVMediaAdapterError("media_output_invalid")
                aggregate_sources.append(normalized.path)

            remaining_temp_bytes = limits.max_temp_bytes - temp_bytes
            if remaining_temp_bytes <= 0:
                raise AVMediaAdapterError("resource_limit")
            aggregate_invocation = self._seam_aggregate_invocation(
                tuple(aggregate_sources),
                aggregate_output,
                min(limits.max_aggregate_output_bytes, remaining_temp_bytes),
                seam_plan=seam_plan,
            )
            self._run_ffmpeg(aggregate_invocation, cancellation)
            aggregate_descriptor = self._inspect_staged_output(
                aggregate_output,
                segment_id="seam_reconstruction.aggregate",
                authority_fingerprint=seam_plan.fingerprint,
                maximum_bytes=plan.limits.max_aggregate_output_bytes,
                maximum_duration_ms=plan.limits.max_total_duration_ms,
                cancellation=cancellation,
            )
            _assert_exact_output(
                aggregate_descriptor,
                expected_frames=seam_plan.total_output_frames,
                expected_samples=seam_plan.total_output_samples,
            )
            temp_bytes += aggregate_descriptor.artifact_byte_length
            if temp_bytes > limits.max_temp_bytes:
                raise AVMediaAdapterError("resource_limit")
            execution_fingerprint = canonical_fingerprint(
                {
                    "plan_fingerprint": plan.fingerprint,
                    "approval_fingerprint": approval.fingerprint,
                    "seam_plan_fingerprint": seam_plan.fingerprint,
                    "seam_approval_fingerprint": seam_approval.fingerprint,
                    "capability_fingerprint": plan.capability.fingerprint,
                    "seam_capability_fingerprint": seam_plan.seam_capability_fingerprint,
                    "aggregate": aggregate_descriptor.to_wire(),
                }
            )
            result = AVSeamExecutionResult(
                execution_fingerprint=execution_fingerprint,
                aggregate_descriptor=aggregate_descriptor,
            )
            _raise_if_cancelled(cancellation)
            success = True
            return result
        except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError) as exc:
            if isinstance(exc, AVMediaAdapterError):
                raise
            raise AVMediaAdapterError("media_process_failed") from exc
        finally:
            cleanup_failed = False
            try:
                source_pins.close()
            except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError):
                cleanup_failed = True
            for lease in reversed(scratch_leases):
                try:
                    lease.release()
                except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError):
                    cleanup_failed = True
            if not success or cleanup_failed:
                try:
                    aggregate_output.release()
                except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError):
                    cleanup_failed = True
            self._process_slots.release()
            if cleanup_failed:
                raise AVMediaAdapterError("cleanup_failed")

    def inspect_payload(
        self,
        *,
        segment_id: str,
        artifact_receipt_fingerprint: str,
        artifact_output_fingerprint: str,
        payload: bytes,
        cancellation: CancellationProbe | None = None,
    ) -> AVMediaDescriptor:
        limits = qualified_av_limits()
        if type(payload) is not bytes or not payload:
            raise AVMediaAdapterError("inspection_invalid")
        if len(payload) > limits.max_input_bytes_per_segment:
            raise AVMediaAdapterError("resource_limit")
        actual_fingerprint = "sha256:" + hashlib.sha256(payload).hexdigest()
        if actual_fingerprint != artifact_output_fingerprint:
            raise AVMediaAdapterError("inspection_stale")
        _raise_if_cancelled(cancellation)
        if not self._process_slots.acquire(blocking=False):
            raise AVMediaAdapterError("resource_limit")
        lease = None
        try:
            try:
                lease = create_output_lease(self._scratch_root, suffix=".media")
                _write_new_file(lease.path, payload)
            except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError) as exc:
                raise AVMediaAdapterError("inspection_unavailable") from exc
            invocation = self._probe_invocation(lease.path)
            capability = qualified_ffmpeg_capability()
            with _pin_regular_media(
                lease.path,
                limits.max_input_bytes_per_segment,
            ) as (staged_size, staged_fingerprint):
                if staged_size != len(payload) or staged_fingerprint != actual_fingerprint:
                    raise AVMediaAdapterError("inspection_stale")
                with _pin_exact_executable(
                    self._ffprobe_path,
                    capability.ffprobe_sha256,
                ):
                    capture = self._runner.run(invocation, cancellation=cancellation)
            status = cast(ProcessStatus, capture.status)
            if status is not ProcessStatus.SUCCEEDED:
                raise _probe_failure(status, cleanup_succeeded=capture.cleanup_succeeded)
            if not capture.cleanup_succeeded:
                raise AVMediaAdapterError("cleanup_failed")
            _raise_if_cancelled(cancellation)
            descriptor = _parse_descriptor(
                capture.stdout,
                segment_id=segment_id,
                artifact_receipt_fingerprint=artifact_receipt_fingerprint,
                artifact_output_fingerprint=artifact_output_fingerprint,
                artifact_byte_length=len(payload),
                inspected_at_ms=self._now_ms(),
            )
            _raise_if_cancelled(cancellation)
            return descriptor
        finally:
            try:
                if lease is not None:
                    lease.release()
            except (ArtifactStoreError, MediaProcessError, OSError, RuntimeError) as exc:
                raise AVMediaAdapterError("cleanup_failed") from exc
            finally:
                self._process_slots.release()


__all__ = [
    "ENCODER_THREAD_RULE",
    "AVMediaAdapterError",
    "AVMediaExecutionResult",
    "AVSeamExecutionResult",
    "M26DerivedMediaLease",
    "QualifiedAVMediaAdapter",
    "encoder_thread_count",
]
