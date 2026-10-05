"""Independent landmark extraction from a decoded final artifact.

Everything here reads bytes that a decoder produced and turns them into the landmark types the
comparator consumes. Nothing here talks to the render planner, the render graph,
``measure_render_output`` or a render receipt: the receipt's only role in this item is to say
*which* artifact is under test, never what is in it. Sharing a pinned decoder binary with the
product is not a claim of implementing a second FFmpeg -- the independence being asserted is from
this repository's own render-planning and receipt-assertion path, and that independence is
structural, because this module cannot reach those code paths at all.

The functions are pure and take buffers, so the parsing and landmark derivation are testable in the
ordinary backend suite while the process invocation stays in the tooling entry point under
``scripts/``. Buffers are consumed a frame or a second at a time by the caller; nothing here retains
a whole decoded video.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Final

from .semantic_conformance import SemanticConformanceError
from .semantic_conformance_compare import (
    AudioOnset,
    FrameGrid,
    FrameTiming,
    OutputMetadata,
    PatchSample,
    SilenceInterval,
)

#: The containers the accepted output profile can emit. `format_name` is a demuxer *family* list --
#: a real MP4 probes as `mov,mp4,m4a,3gp,3g2,mj2`, whose first member is `mov` -- so the concrete
#: container is resolved by intersecting that family with this closed vocabulary. Taking the first
#: member instead would report every conformant artifact as a `mov`. This is not the extractor
#: consuming an expectation: the vocabulary is what the product is able to write, and a probe whose
#: family contains none of it is reported verbatim so the comparison fails on the observed string.
EMITTED_CONTAINERS: Final = ("mp4",)

#: The colour tags the accepted output profile actually writes. `authoring_render_graph.py` encodes
#: with `-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709`, and ffprobe
#: reports those four under these keys.
COLOR_TAG_KEYS: Final = ("color_space", "color_transfer", "color_primaries", "color_range")
ACCEPTED_COLOR_TAGS: Final = {
    "color_space": "bt709",
    "color_transfer": "bt709",
    "color_primaries": "bt709",
    "color_range": "tv",
}
#: The profile identifier those four tags, and only those four, denote.
ACCEPTED_COLOR_POLICY: Final = "bt709_sdr_limited_v1"
#: What a fully untagged artifact observes as. It is a distinct answer from a wrong tag: one is an
#: artifact that said nothing, the other is an artifact that said something else.
UNKNOWN_COLOR_POLICY: Final = "unknown"

#: The packet table is bounded so a probe of a pathological artifact cannot become an unbounded
#: read. The plan caps an ordinary temporal case at 384 output frames; this leaves an order of
#: magnitude of headroom and still refuses rather than truncating.
MAX_TIMING_ENTRIES: Final = 4_096

#: Decoded pixel format the extractor asks for. Three bytes per pixel, no padding, so a patch mean
#: is an exact integer average rather than a colour-space guess.
RGB24_BYTES_PER_PIXEL: Final = 3
#: Decoded audio format: signed 16-bit little-endian mono, matching the accepted output profile.
PCM16_BYTES_PER_SAMPLE: Final = 2
PCM16_FULL_SCALE: Final = 32_768


def _reject(message: str) -> SemanticConformanceError:
    return SemanticConformanceError(message)


def _require(document: Mapping[str, object], key: str, name: str) -> object:
    if key not in document:
        # Absent metadata is never treated as agreement; the caller reports it as a mismatch.
        raise _reject(f"{name} is missing from the probe output")
    return document[key]


def _rational(value: object, name: str) -> tuple[int, int]:
    if not isinstance(value, str) or "/" not in value:
        raise _reject(f"{name} is not a rational string")
    left, _, right = value.partition("/")
    try:
        numerator, denominator = int(left), int(right)
    except ValueError as exc:
        raise _reject(f"{name} is not a rational string") from exc
    if denominator <= 0:
        raise _reject(f"{name} has a non-positive denominator")
    reduced = Fraction(numerator, denominator)
    return reduced.numerator, reduced.denominator


def _dimension(stream: Mapping[str, object], key: str) -> int:
    value = _require(stream, key, f"video.{key}")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _reject(f"video.{key} is not a positive integer")
    return value


def _streams(document: Mapping[str, object], codec_type: str) -> tuple[Mapping[str, object], ...]:
    streams = document.get("streams")
    if not isinstance(streams, list):
        raise _reject("probe output has no stream table")
    return tuple(
        stream
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == codec_type
    )


def _stream(document: Mapping[str, object], codec_type: str) -> Mapping[str, object] | None:
    found = _streams(document, codec_type)
    return found[0] if found else None


def derive_color_policy(video: Mapping[str, object]) -> str:
    """Resolve the colour profile from the artifact's own tags.

    CRITICAL: this used to be a caller-supplied string returned verbatim, so an untagged or
    wrongly tagged artifact could be reported as the accepted profile by naming it on the command
    line. The three answers are kept apart on purpose: the accepted identifier when every tag
    matches, `unknown` when the artifact carries none of them, and otherwise the observed tags
    written out, so the comparison fails on what was actually measured rather than on this
    function's opinion of it.
    """

    observed = {
        key: value
        for key in COLOR_TAG_KEYS
        if isinstance(value := video.get(key), str) and value not in ("", "unknown")
    }
    if not observed:
        return UNKNOWN_COLOR_POLICY
    if observed == ACCEPTED_COLOR_TAGS:
        return ACCEPTED_COLOR_POLICY
    return ";".join(f"{key}={observed.get(key, 'absent')}" for key in COLOR_TAG_KEYS)


def parse_output_metadata(probe: Mapping[str, object]) -> OutputMetadata:
    """Read container/codec/colour facts out of a probe document.

    Every value here is derived from the document. Nothing is accepted from the caller, because a
    caller-supplied fact is a fact about the caller.
    """

    video = _stream(probe, "video")
    if video is None:
        raise _reject("the artifact carries no video stream")
    audio_streams = _streams(probe, "audio")
    audio = audio_streams[0] if audio_streams else None
    container_format = probe.get("format")
    if not isinstance(container_format, Mapping):
        raise _reject("probe output has no format table")
    container = _require(container_format, "format_name", "format.format_name")
    if not isinstance(container, str):
        raise _reject("format.format_name is not a string")
    frame_rate_num, frame_rate_den = _rational(
        _require(video, "r_frame_rate", "video.r_frame_rate"), "video.r_frame_rate"
    )
    aspect = video.get("sample_aspect_ratio")
    if aspect is None or aspect == "N/A":
        # A missing pixel aspect is 1:1 only when the probe says so; "unknown" is not "square".
        raise _reject("video.sample_aspect_ratio is unknown")
    aspect_num, aspect_den = _rational(str(aspect).replace(":", "/"), "video.sample_aspect_ratio")
    family = tuple(member.strip() for member in container.split(","))
    resolved = next((member for member in EMITTED_CONTAINERS if member in family), container)
    return OutputMetadata(
        container=resolved,
        video_codec=str(_require(video, "codec_name", "video.codec_name")),
        pixel_format=str(_require(video, "pix_fmt", "video.pix_fmt")),
        width=_dimension(video, "width"),
        height=_dimension(video, "height"),
        pixel_aspect_num=aspect_num,
        pixel_aspect_den=aspect_den,
        color_policy=derive_color_policy(video),
        frame_rate_num=frame_rate_num,
        frame_rate_den=frame_rate_den,
        # CRITICAL: every audio stream is counted. The accepted profile emits zero or one, so a
        # second stream is exactly the unsupported layout this comparison exists to catch --
        # and reporting `1` for it made an unsupported artifact indistinguishable from a
        # conformant one.
        audio_stream_count=len(audio_streams),
        audio_codec=None if audio is None else str(audio.get("codec_name")),
        sample_rate=None if audio is None else int(str(audio.get("sample_rate"))),
        channels=None if audio is None else int(str(audio.get("channels"))),
    )


def _timestamp(packet: Mapping[str, object], key: str) -> int | None:
    value = packet.get(key)
    if value is None or value == "N/A":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject(f"packet.{key} is not an integer tick count")
    return value


def parse_frame_timing(document: Mapping[str, object]) -> FrameTiming:
    """Read the measured packet timestamps out of a bounded timing probe.

    This is the observation that a nominal frame rate cannot stand in for. It raises rather than
    guesses whenever the document cannot support a rational timeline at all -- no packet table, no
    time base, a non-integer tick, a partially reported decode timestamp -- because each of those
    is missing evidence, and the caller turns missing evidence into `BLOCKED`. What it does *not*
    do is refuse an irregular, duplicated, backwards or negative table: those are real properties
    of a real artifact, and reporting them is the entire job.
    """

    packets = document.get("packets")
    if not isinstance(packets, list):
        raise _reject("the timing probe returned no packet table")
    if len(packets) > MAX_TIMING_ENTRIES:
        raise _reject(f"the packet table exceeds the {MAX_TIMING_ENTRIES}-entry bound")
    video = _stream(document, "video")
    if video is None:
        raise _reject("the timing probe returned no video stream")
    time_base_num, time_base_den = _rational(
        _require(video, "time_base", "video.time_base"), "video.time_base"
    )
    presentation: list[int] = []
    decode: list[int | None] = []
    durations: list[int | None] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise _reject("a packet entry is not a mapping")
        point = _timestamp(packet, "pts")
        if point is None:
            raise _reject("a packet carries no presentation timestamp")
        presentation.append(point)
        decode.append(_timestamp(packet, "dts"))
        durations.append(_timestamp(packet, "duration"))
    reported = sum(1 for stamp in decode if stamp is not None)
    if reported not in (0, len(decode)):
        raise _reject("the packet table reports a decode timestamp for only some packets")
    return FrameTiming(
        time_base_num=time_base_num,
        time_base_den=time_base_den,
        pts_ticks=tuple(presentation),
        dts_ticks=tuple(decode),
        duration_ticks=tuple(durations),
    )


def parse_frame_grid(
    probe: Mapping[str, object], frame_count: int, timing: FrameTiming | None
) -> FrameGrid:
    """Build the frame grid from probed geometry, a counted frame total and measured timing.

    `frame_count` is the number of frames the extractor decoded, not the container's `nb_frames`
    hint: a container can advertise a count it does not deliver.

    CRITICAL: `timing` is required, and the frame rate comes from it rather than from
    `r_frame_rate`. The grid used to be built from the nominal rate and the decoded count alone,
    which makes `duration()` the arithmetic identity `count / rate` -- it agreed with itself for
    any artifact whatsoever. Three independent statements are cross-checked here instead: the
    decoded count, the packet count, and the spacing the packets actually have. Do not reintroduce
    a fallback to the nominal rate when the timing is absent or irregular; an unmeasurable grid is
    missing evidence, and the caller reports it as `BLOCKED`.
    """

    video = _stream(probe, "video")
    if video is None:
        raise _reject("the artifact carries no video stream")
    if frame_count < 0:
        raise _reject("a decoded frame count cannot be negative")
    if timing is None:
        raise _reject("a frame grid needs measured packet timing, not a nominal rate")
    if timing.count() != frame_count:
        raise _reject(
            f"the packet table has {timing.count()} entries "
            f"and the decoded frame count is {frame_count}"
        )
    step = timing.uniform_step()
    if step is None or step <= 0:
        raise _reject("the measured packet spacing is not uniform, so there is no frame rate")
    rate = 1 / step
    return FrameGrid(
        width=_dimension(video, "width"),
        height=_dimension(video, "height"),
        frame_count=frame_count,
        frame_rate_num=rate.numerator,
        frame_rate_den=rate.denominator,
    )


def _pixel(frame: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * width + x) * RGB24_BYTES_PER_PIXEL
    return frame[offset], frame[offset + 1], frame[offset + 2]


def patch_mean(
    frame: bytes,
    width: int,
    height: int,
    *,
    label: str,
    center_x: int,
    center_y: int,
    size_px: int,
) -> PatchSample:
    """Average an odd-sized interior patch and record how far it sits from the nearest edge.

    The edge distance is measured and carried rather than assumed, because the comparator refuses a
    patch that reaches an antialiased boundary. A patch that silently drifted to an edge would
    otherwise turn every colour comparison into a comparison of edge blending.
    """

    if size_px < 1 or size_px % 2 == 0:
        raise _reject("a patch size must be a positive odd number of pixels")
    radius = size_px // 2
    if not (radius <= center_x < width - radius and radius <= center_y < height - radius):
        raise _reject(f"patch {label} does not fit inside the decoded frame")
    expected = width * height * RGB24_BYTES_PER_PIXEL
    if len(frame) != expected:
        raise _reject("the decoded frame is not the declared rgb24 size")
    totals = [0, 0, 0]
    for y in range(center_y - radius, center_y + radius + 1):
        for x in range(center_x - radius, center_x + radius + 1):
            for channel, value in enumerate(_pixel(frame, width, x, y)):
                totals[channel] += value
    count = size_px * size_px
    edge_distance = min(
        center_x - radius,
        center_y - radius,
        width - 1 - center_x - radius,
        height - 1 - center_y - radius,
    )
    return PatchSample(
        label=label,
        size_px=size_px,
        edge_distance_px=edge_distance,
        red=totals[0] // count,
        green=totals[1] // count,
        blue=totals[2] // count,
    )


def read_frame_id_tile(
    frame: bytes,
    width: int,
    height: int,
    *,
    origin_x: int,
    origin_y: int,
    cell_px: int,
    bits: int,
    threshold: int = 128,
) -> int:
    """Recover the source frame identifier a fixture burned into a coarse binary tile.

    The final artifact does not carry the source PTS of the frame it was built from, so the fixture
    encodes it as a row of high-contrast cells. Reading it back is what lets the final observation
    state which *source* frame each output frame actually used, instead of assuming the mapping the
    resolver intended. Cells are sampled at their centre so a scaling filter's edge blending cannot
    flip a bit.
    """

    if bits < 1 or bits > 32:
        raise _reject("a frame identifier tile must carry between 1 and 32 bits")
    value = 0
    for index in range(bits):
        centre_x = origin_x + index * cell_px + cell_px // 2
        centre_y = origin_y + cell_px // 2
        if not (0 <= centre_x < width and 0 <= centre_y < height):
            raise _reject("the frame identifier tile falls outside the decoded frame")
        red, green, blue = _pixel(frame, width, centre_x, centre_y)
        # Most significant bit first, so the tile reads left to right like the number it encodes.
        if (red + green + blue) // 3 >= threshold:
            value |= 1 << (bits - 1 - index)
    return value


def pcm16_peak(samples: bytes, start_sample: int, end_sample: int) -> int:
    """Absolute PCM16 peak over a half-open sample range."""

    if start_sample < 0 or end_sample < start_sample:
        raise _reject("an invalid sample range was requested")
    first = start_sample * PCM16_BYTES_PER_SAMPLE
    last = min(end_sample * PCM16_BYTES_PER_SAMPLE, len(samples))
    peak = 0
    for offset in range(first, last - 1, PCM16_BYTES_PER_SAMPLE):
        value = int.from_bytes(samples[offset : offset + 2], "little", signed=True)
        peak = max(peak, abs(value))
    return peak


def pcm16_rms(samples: bytes, start_sample: int, sample_count: int) -> float:
    """Root mean square of PCM16 values over `sample_count` samples from `start_sample`.

    The whole window or nothing: a window the buffer does not wholly contain is refused rather
    than measured over the samples that happen to be there, which would report a shortened window
    under the full one's name.
    """

    if start_sample < 0 or sample_count <= 0:
        raise _reject("an invalid sample range was requested")
    first = start_sample * PCM16_BYTES_PER_SAMPLE
    last = first + sample_count * PCM16_BYTES_PER_SAMPLE
    if last > len(samples):
        raise _reject("the window extends past the decoded samples")
    energy = 0
    for offset in range(first, last, PCM16_BYTES_PER_SAMPLE):
        value = int.from_bytes(samples[offset : offset + 2], "little", signed=True)
        energy += value * value
    return math.sqrt(energy / sample_count)


def silence_interval(
    samples: bytes, *, label: str, start_sample: int, end_sample: int
) -> SilenceInterval:
    """Measure a declared silent interval without trimming it to make the check succeed."""

    return SilenceInterval(
        label=label,
        start_sample=start_sample,
        end_sample=end_sample,
        abs_peak=pcm16_peak(samples, start_sample, end_sample),
    )


def detect_onsets(
    samples: bytes,
    *,
    labels: Sequence[str],
    threshold_ratio: Fraction,
    refractory_samples: int,
) -> tuple[AudioOnset, ...]:
    """Find synthetic impulse onsets, labelling them in the order the fixture declared.

    The threshold is a fraction of full scale and the refractory window suppresses the decay tail of
    an impulse already reported. Neither is fitted to the signal: an onset detector tuned per case
    would report whatever count the fixture expected, which is the opposite of an observation.
    """

    if threshold_ratio <= 0 or threshold_ratio > 1:
        raise _reject("an onset threshold must be a fraction of full scale in (0, 1]")
    if refractory_samples < 0:
        raise _reject("a refractory window cannot be negative")
    threshold = int(threshold_ratio * PCM16_FULL_SCALE)
    found: list[int] = []
    index = 0
    total = len(samples) // PCM16_BYTES_PER_SAMPLE
    while index < total:
        offset = index * PCM16_BYTES_PER_SAMPLE
        value = int.from_bytes(samples[offset : offset + 2], "little", signed=True)
        if abs(value) >= threshold:
            found.append(index)
            index += refractory_samples + 1
            continue
        index += 1
    if len(found) != len(labels):
        # A count mismatch is reported as an error rather than zipped away: silently pairing the
        # first N onsets with the first N labels would hide both a missing and an extra impulse.
        raise _reject(
            f"expected {len(labels)} impulses in the decoded audio and measured {len(found)}"
        )
    return tuple(
        AudioOnset(label=label, sample_index=position)
        for label, position in zip(labels, found, strict=True)
    )
