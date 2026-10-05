"""What each corpus row expects, derived from the accepted contract before anything runs.

`semantic_conformance` says which rows exist, `semantic_conformance_cases` says how to set each one
up, and this module says what the resulting composition *declares* about itself. The three
observations the comparator joins are then genuinely separate: the expectation comes from the
contract, the browser observation from the presented canvas, and the final observation from an
artifact decoded by the pinned tools. None of the three is derived from another.

The rule that shapes everything here is that an expectation may only state what the accepted
contract actually determines. The output profile fixes the container, the codec, the pixel format,
the frame size, the pixel aspect, the colour policy, the frame rate and the audio policy; the
composition fixes the frame count and whether any audio-bearing primary clip is present. Those are
declarations, so they can be asserted exactly, and the timing they imply -- a constant-rate grid
with one packet per output frame -- can be asserted exactly too.

What is deliberately *not* asserted is anything the compositor decides rather than the contract: a
patch colour, a blended pixel, a glyph raster. Deriving those would mean reimplementing the
compositor inside its own conformance corpus, and the reimplementation's bugs would arrive as
product mismatches. Those landmarks are measured on the browser side and retained as measurements;
they are not compared against a number this module invented.

One consequence worth stating plainly: the timing expectation is written in rational time, not in
ticks. The comparator compares `Fraction` times, so an artifact whose container uses a 1/12288 time
base agrees exactly with an expectation written at 1/24. A tick-level expectation would have failed
every real artifact for a reason that has nothing to do with conformance.
"""

from __future__ import annotations

import dataclasses
import math
import struct
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final

from .clip_audio import SAMPLES_PER_FRAME, clip_audio_factor
from .composition_contract import IDENTITY_CLIP_AUDIO, ClipAudio
from .semantic_conformance import TOLERANCE_PROFILE
from .semantic_conformance_cases import BASE_PRESENTATION, CORPUS_BASE, CaseRecipe
from .semantic_conformance_compare import (
    AlphaSample,
    AudioLevel,
    AudioOnset,
    Expectation,
    FrameGrid,
    FrameTiming,
    GeometryLandmark,
    OutputMetadata,
    PatchSample,
    SilenceInterval,
    SourceLandmark,
    TextObservation,
)
from .semantic_conformance_media import (
    AUDIO_BURST_SAMPLES,
    AUDIO_BURST_STARTS,
    FRAME_ID_BITS,
    SourceProfile,
    frame_id_bits,
    profile_for,
    source_colour,
)

#: An asset carrying primary embedded audio. `excluded_overlay_policy` and `absent` do not: the
#: accepted audio policy follows the primary video only, so an overlay's own audio never reaches
#: the output and must not be expected there.
AUDIO_BEARING: frozenset[str] = frozenset({"present_bound"})

#: The track kind whose clips can contribute audio under `primary_embedded_follow_video_v1`.
PRIMARY_TRACK_KIND = "primary_video"

#: AAC is a block codec: a stream decodes to a whole number of 1024-sample frames, so a
#: timeline that is not a multiple of that granularity decodes longer than it was. This is a
#: property of the codec the output profile declares, not a measurement -- the expectation has
#: to account for it or every audio-bearing row fails on the encoder's last frame.
CODEC_FRAME_SAMPLES: Final[dict[str, int]] = {"aac": 1024}


class ExpectationError(ValueError):
    """The composition does not declare enough to state an expectation."""


def _int(section: Mapping[str, Any], field: str) -> int:
    value = section.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ExpectationError(f"{field} is not a declared integer")
    return value


def _rational(section: Mapping[str, Any], field: str) -> tuple[int, int]:
    value = section.get(field)
    if not isinstance(value, Mapping):
        raise ExpectationError(f"{field} is not a declared rational")
    num, den = value.get("num"), value.get("den")
    if not isinstance(num, int) or not isinstance(den, int) or den == 0:
        raise ExpectationError(f"{field} is not a usable rational")
    return num, den


def carries_audio(wire: Mapping[str, Any]) -> bool:
    """Whether the composition declares an audio-bearing enabled primary clip.

    GUARD: the accepted policy is `primary_embedded_follow_video_v1`, so audio follows the primary
    video track and nothing else. Do not widen this to "any clip whose asset has audio" -- an
    overlay's audio is excluded by policy, and expecting it would make every overlay row fail on an
    audio stream the renderer is correct not to write.
    """

    tracks = wire.get("tracks")
    clips = wire.get("clips")
    assets = wire.get("assets")
    if not isinstance(tracks, Sequence) or not isinstance(clips, Sequence):
        raise ExpectationError("the composition declares no tracks or clips")
    if not isinstance(assets, Sequence):
        raise ExpectationError("the composition declares no assets")
    primary = {
        track["track_id"]
        for track in tracks
        if isinstance(track, Mapping) and track.get("kind") == PRIMARY_TRACK_KIND
    }
    audible = {
        asset["asset_id"]
        for asset in assets
        if isinstance(asset, Mapping) and asset.get("embedded_audio") in AUDIO_BEARING
    }
    return any(
        isinstance(clip, Mapping)
        and clip.get("enabled") is True
        and clip.get("track_id") in primary
        and clip.get("asset_id") in audible
        and _int(clip, "duration_frames") > 0
        for clip in clips
    )


def derive_output_metadata(wire: Mapping[str, Any]) -> OutputMetadata:
    """State the artifact facts the accepted output profile fixes."""

    output = wire.get("output")
    if not isinstance(output, Mapping):
        raise ExpectationError("the composition declares no output profile")
    rate_num, rate_den = _rational(output, "frame_rate")
    par_num, par_den = _rational(output, "pixel_aspect")
    audio = carries_audio(wire)
    return OutputMetadata(
        container=str(output["container"]),
        video_codec=str(output["video_codec"]),
        pixel_format=str(output["pixel_format"]),
        width=_int(output, "width"),
        height=_int(output, "height"),
        pixel_aspect_num=par_num,
        pixel_aspect_den=par_den,
        color_policy=str(output["color_policy"]),
        frame_rate_num=rate_num,
        frame_rate_den=rate_den,
        audio_stream_count=1 if audio else 0,
        audio_codec=str(output["audio_codec"]) if audio else None,
        sample_rate=_int(output, "sample_rate") if audio else None,
        channels=_int(output, "channels") if audio else None,
    )


def derive_frame_grid(wire: Mapping[str, Any]) -> FrameGrid:
    output = wire.get("output")
    if not isinstance(output, Mapping):
        raise ExpectationError("the composition declares no output profile")
    rate_num, rate_den = _rational(output, "frame_rate")
    return FrameGrid(
        width=_int(output, "width"),
        height=_int(output, "height"),
        frame_count=_int(output, "duration_frames"),
        frame_rate_num=rate_num,
        frame_rate_den=rate_den,
    )


def derive_frame_timing(wire: Mapping[str, Any]) -> FrameTiming:
    """One packet per output frame on a constant grid, written in the output's own time base.

    The declared per-packet duration is one tick, which is what makes `uniform_step()` return a
    step at all: a table whose packet durations disagree with its PTS deltas is refused rather than
    averaged, and an expectation that omitted the durations would be refused by its own rule.
    """

    output = wire.get("output")
    if not isinstance(output, Mapping):
        raise ExpectationError("the composition declares no output profile")
    base_num, base_den = _rational(output, "time_base")
    rate_num, rate_den = _rational(output, "frame_rate")
    frames = _int(output, "duration_frames")
    if frames <= 0:
        raise ExpectationError("a composition with no frames declares no timing")
    step_seconds = Fraction(rate_den, rate_num)
    step_ticks_exact = step_seconds / Fraction(base_num, base_den)
    if step_ticks_exact.denominator != 1:
        raise ExpectationError("the output frame rate is not representable in its own time base")
    step = int(step_ticks_exact)
    ticks = tuple(index * step for index in range(frames))
    return FrameTiming(
        time_base_num=base_num,
        time_base_den=base_den,
        pts_ticks=ticks,
        dts_ticks=ticks,
        duration_ticks=tuple(step for _ in ticks),
    )


def derive_audio_extent_samples(wire: Mapping[str, Any]) -> int | None:
    """The sample count the output timeline implies, or `None` when no audio is expected.

    The extent is the *output's* duration rather than the primary clip's: the accepted policy
    writes one continuous stream for the composition, so a shorter primary clip leaves silence
    inside the stream rather than shortening it.
    """

    if not carries_audio(wire):
        return None
    output = wire.get("output")
    if not isinstance(output, Mapping):
        raise ExpectationError("the composition declares no output profile")
    rate_num, rate_den = _rational(output, "frame_rate")
    frames = _int(output, "duration_frames")
    sample_rate = _int(output, "sample_rate")
    seconds = Fraction(frames * rate_den, rate_num)
    samples = seconds * sample_rate
    if samples.denominator != 1:
        raise ExpectationError("the output duration is not a whole number of samples")
    timeline_samples = int(samples)
    granularity = CODEC_FRAME_SAMPLES.get(str(output.get("audio_codec") or ""))
    if granularity is None:
        return timeline_samples
    # Round up to the codec's own frame boundary. The residual is then held to the frozen
    # one-sample rational-conversion bound, which is what that bound is actually about; widening it
    # to absorb a whole codec frame would have hidden a real drift of up to 1023 samples.
    whole_frames = -(-timeline_samples // granularity)
    return whole_frames * granularity


def _enabled_tracks(wire: Mapping[str, Any]) -> set[object]:
    tracks = wire.get("tracks")
    if not isinstance(tracks, Sequence):
        raise ExpectationError("the composition declares no tracks")
    return {
        track["track_id"]
        for track in tracks
        if isinstance(track, Mapping) and track.get("enabled") is not False
    }


def _enabled_clips(wire: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Every clip that actually contributes: enabled, and on a track that is enabled too.

    GUARD: both halves are required. A clip on a disabled track renders nothing, so an expectation
    derived from it would describe a picture nobody can observe, and the row would report a
    disagreement against a correct run. `command.set_track_enabled.accepted` is exactly that case.
    """

    clips = wire.get("clips")
    if not isinstance(clips, Sequence):
        raise ExpectationError("the composition declares no clips")
    tracks = _enabled_tracks(wire)
    return tuple(
        clip
        for clip in clips
        if isinstance(clip, Mapping)
        and clip.get("enabled") is True
        and clip.get("track_id") in tracks
    )


def _asset(wire: Mapping[str, Any], asset_id: object) -> Mapping[str, Any] | None:
    assets = wire.get("assets")
    if not isinstance(assets, Sequence):
        return None
    for asset in assets:
        if isinstance(asset, Mapping) and asset.get("asset_id") == asset_id:
            return asset
    return None


#: The contract's own rule for which source frame an output frame shows, restated here
#: analytically from the declared tables and never by calling the resolver: the clip's
#: `source_start_frame` names a landmark, the output frame's source time is that landmark's `pts`
#: plus the elapsed output frames converted into the source's time base, and the frame shown is
#: the last landmark whose `pts` does not exceed that time (`composition_contract._source_at_frame`
#: floors to the preceding admitted packet). For a 24 fps source every frame lasts one output frame
#: and this is the identity; for the timing source it is what makes a different rate and unequal
#: intervals observable (B-58). A time at or past the source's declared end is the source's last
#: frame, which is what the render path presents rather than failing.
SOURCE_FRAME_FOLLOWS_THE_LANDMARK_TABLE: Final = (
    "the source frame an output frame shows is the last landmark whose pts does not exceed the "
    "clip's source start pts plus the elapsed output time in source ticks"
)


def _landmark_table(asset: Mapping[str, Any]) -> tuple[tuple[int, int, int], ...]:
    """`(frame_index, pts, duration_ticks)` rows of a video asset's declared table, in order."""

    table = asset.get("landmarks")
    if not isinstance(table, Sequence):
        return ()
    rows = [
        (_int(entry, "frame_index"), _int(entry, "pts"), _int(entry, "duration_ticks"))
        for entry in table
        if isinstance(entry, Mapping)
    ]
    return tuple(sorted(rows, key=lambda row: row[1]))


def source_start_tick(wire: Mapping[str, Any], clip: Mapping[str, Any]) -> int | None:
    """The source pts at which a clip's declared `source_start_frame` lands, if the table has it."""

    asset = _asset(wire, clip.get("asset_id"))
    if asset is None:
        return None
    start = _int(clip, "source_start_frame")
    return next((pts for index, pts, _ in _landmark_table(asset) if index == start), None)


def source_ticks_per_output_frame(wire: Mapping[str, Any], asset: Mapping[str, Any]) -> Fraction:
    """How many ticks of this source's time base one output frame lasts."""

    rate_num, rate_den = _rational(_output(wire), "frame_rate")
    base_num, base_den = _rational(asset, "source_time_base")
    return Fraction(rate_den * base_den, rate_num * base_num)


def source_frame_at(
    wire: Mapping[str, Any], clip: Mapping[str, Any], output_frame: int
) -> tuple[int, int] | None:
    """`(source_frame, source_pts)` a clip shows at one output frame, or `None` without a table.

    See `SOURCE_FRAME_FOLLOWS_THE_LANDMARK_TABLE`; the output frame is not checked against the
    clip's own span here, the callers decide what their landmark covers.
    """

    asset = _asset(wire, clip.get("asset_id"))
    if asset is None or asset.get("kind") != "video":
        return None
    table = _landmark_table(asset)
    base_pts = source_start_tick(wire, clip)
    if not table or base_pts is None:
        return None
    elapsed = output_frame - _int(clip, "start_frame")
    target = base_pts + elapsed * source_ticks_per_output_frame(wire, asset)
    selected: tuple[int, int] | None = None
    for index, pts, _ in table:
        if pts > target:
            break
        selected = (index, pts)
    return selected


#: Why the primary's source identity is stated only at the frames where its identity row can
#: actually be read out of the declared composite.
#:
#: GUARD: this is a legibility rule, not a convenience, and it is the only reason an accepted
#: command whose overlay covers the primary's identity cells does not read as a wrong picture. The
#: identity is a row of black and white cells burned into the source; an observer recovers it by
#: reading each cell's luma out of the composite and thresholding it. A cell that another enabled
#: layer paints over -- an inserted full-size overlay, an opaque still, the title's half-alpha box
#: -- or that the clip's own crop removes, or that its transform pushes off the canvas, is not in
#: the picture at all, so an expectation naming a frame identity there is stating something no
#: observer can produce: an expectation confirming itself, the class of defect this corpus exists
#: to refuse. The rule decides *whether* a frame is stated; the frame index it states still comes
#: from the declared landmark table and never from the composite. Never widen the luma margin to
#: state more frames, and never state a frame whose cells are partly legible -- a half-read
#: identity is a different number, not a weaker measurement.
IDENTITY_STATED_ONLY_WHERE_LEGIBLE: Final = (
    "the primary's source identity is read from its burned-in identity row, so it is stated only "
    "at the frames where the declared composite leaves every cell of that row on the canvas, "
    "uncropped, unpainted by any layer above it, and on the correct side of the observer's luma "
    "threshold with a margin a lossy encode cannot cross"
)

#: How far from the observer's threshold (128 on the 0..255 luma scale) a cell's composited luma
#: must sit before the identity is stated. A cell exactly at the threshold would be decided by
#: encoder noise; this margin is what makes a stated frame a claim about the picture rather than
#: about the codec.
IDENTITY_LUMA_MARGIN: Final = 40
_IDENTITY_LUMA_THRESHOLD: Final = 128


def _luma(rgb: tuple[int, int, int]) -> float:
    return (rgb[0] + rgb[1] + rgb[2]) / 3


def _identity_cell_pixels(profile: SourceProfile) -> tuple[tuple[int, int], ...]:
    """The source pixel at the centre of each identity cell, most significant bit first.

    GUARD: read the geometry off the profile, never off the module constants. A high-resolution
    source states the same row four times larger, and centres computed from the 128-square
    constants would land in its flat field -- which reads as a legible identity of the wrong value
    rather than as an error.
    """

    origin_x, origin_y = profile.frame_id_origin_px
    pitch = profile.frame_id_cell_pitch_px
    half = profile.frame_id_mark_px // 2
    return tuple((origin_x + cell * pitch + half, origin_y + half) for cell in range(FRAME_ID_BITS))


def _identity_legible(
    wire: Mapping[str, Any],
    layers: tuple[VisualLayer, ...],
    layer: VisualLayer,
    output_frame: int,
    source_index: int,
) -> bool:
    """Whether every cell of `layer`'s identity row reads as `source_index` in the composite."""

    output = _output(wire)
    canvas_width, canvas_height = _int(output, "width"), _int(output, "height")
    half = layer.profile.frame_id_mark_px // 2
    for (source_x, source_y), bit in zip(
        _identity_cell_pixels(layer.profile), frame_id_bits(source_index), strict=True
    ):
        canvas = layer.placement.to_canvas(source_x, source_y)
        if canvas is None:
            return False
        x, y = round(canvas[0]), round(canvas[1])
        if not (0 <= x < canvas_width and 0 <= y < canvas_height):
            return False
        # GUARD: the observer reads one whole canvas pixel, so that pixel has to lie inside this
        # cell's own mark when mapped back to the source. A scale that collapses the row makes
        # every cell round to the same canvas pixel, and a frame whose index is all zeros would
        # then read "correctly" out of one black cell six times over. Never drop this round trip.
        back = layer.placement.to_source(x, y)
        if back is None:
            return False
        if not (
            source_x - half <= back[0] < source_x + half
            and source_y - half <= back[1] < source_y + half
        ):
            return False
        try:
            luma = _luma(_composite_at(wire, layers, output_frame, x, y))
        except ExpectationError:
            return False
        if bit and luma < _IDENTITY_LUMA_THRESHOLD + IDENTITY_LUMA_MARGIN:
            return False
        if not bit and luma > _IDENTITY_LUMA_THRESHOLD - IDENTITY_LUMA_MARGIN:
            return False
    return True


@dataclass(frozen=True, slots=True)
class IdentityRead:
    """Where an observer reads the primary's identity row at one output frame.

    `cells` is the canvas pixel at each cell's centre, most significant bit first, mapped through
    the clip's declared placement -- a prescribed point, exactly like a `SamplePoint`, so the
    observer cannot relocate the row by searching for it. `pts_by_frame` is the asset's own
    declared landmark table, carried so that the index the observer reads can be stated in the
    asset's declared time base rather than in a time base the observer would otherwise invent.
    """

    output_frame: int
    cells: tuple[tuple[int, int], ...]
    pts_by_frame: Mapping[int, int]
    time_base_num: int
    time_base_den: int
    #: The clip and asset whose identity row this is. The browser reports the presented source
    #: time from the clock of the element leased for *this clip*: a primary track may hand over
    #: from one asset to another mid-timeline, and two clips may lease the same asset at once
    #: with different clocks, so neither a fixed element nor the asset alone names it.
    clip_id: str = ""
    asset_id: str = ""


def identity_reads(wire: Mapping[str, Any]) -> tuple[IdentityRead, ...]:
    """The identity-row read for every output frame a primary video clip places on the canvas.

    The observers import this so that all of them read the same pixels. Every frame whose cells
    all land on the canvas is listed, whether or not the composite leaves the row legible there:
    what is *compared* is decided by `derive_source_mapping`, and an observer is not told which
    frames those are. GUARD: this reads the declared placement and never a render, and it must
    stay the only source of the cell pixels -- an observer that located the row by colour would
    read the right index out of a misplaced layer, which is the geometry landmark's question and
    must remain distinguishable from this one.
    """

    reads: dict[int, IdentityRead] = {}
    output = _output(wire)
    canvas_width, canvas_height = _int(output, "width"), _int(output, "height")
    duration_frames = _int(output, "duration_frames")
    primary = _primary_track_ids(wire)
    # One read per output frame, the uppermost primary clip's: see `derive_source_mapping`.
    for layer in _visual_layers(wire):
        clip = layer.clip
        if clip.get("track_id") not in primary:
            continue
        asset = _asset(wire, clip.get("asset_id"))
        if asset is None or asset.get("kind") != "video":
            continue
        table = asset.get("landmarks")
        base = asset.get("source_time_base")
        if not isinstance(table, Sequence) or not isinstance(base, Mapping):
            continue
        if not layer.profile.carries_frame_id:
            continue
        base_num, base_den = _rational(asset, "source_time_base")
        pts_by_frame = {
            _int(entry, "frame_index"): _int(entry, "pts")
            for entry in table
            if isinstance(entry, Mapping)
        }
        cells: list[tuple[int, int]] = []
        for source_x, source_y in _identity_cell_pixels(layer.profile):
            canvas = layer.placement.to_canvas(source_x, source_y)
            if canvas is None:
                break
            x, y = round(canvas[0]), round(canvas[1])
            if not (0 <= x < canvas_width and 0 <= y < canvas_height):
                break
            cells.append((x, y))
        if len(cells) != FRAME_ID_BITS:
            continue
        start = _int(clip, "start_frame")
        end = min(start + _int(clip, "duration_frames"), duration_frames)
        for output_frame in range(start, end):
            reads[output_frame] = IdentityRead(
                output_frame=output_frame,
                cells=tuple(cells),
                pts_by_frame=pts_by_frame,
                time_base_num=base_num,
                time_base_den=base_den,
                clip_id=str(clip.get("clip_id")),
                asset_id=str(clip.get("asset_id")),
            )
    return tuple(reads[frame] for frame in sorted(reads))


def derive_source_mapping(wire: Mapping[str, Any]) -> tuple[SourceLandmark, ...]:
    """Which source frame each of the declared landmark output frames must resolve to.

    This is the landmark that answers "is this the right picture at all". Dimensions, duration,
    codec and colour can all be correct while the artifact shows the wrong frames, or nothing; only
    the source identity of a presented frame rules that out.

    It is analytic and literal, never a second resolver: the asset declares a table of
    `frame_index -> pts`, the clip declares where it starts and which source frame it starts from,
    and every output frame inside the clip's span shows the frame
    `SOURCE_FRAME_FOLLOWS_THE_LANDMARK_TABLE` names -- the identity for a 24 fps source, and the
    different-rate or unequal-interval mapping for the timing source. Frames where the identity
    row is not legible in the declared composite are dropped (`IDENTITY_STATED_ONLY_WHERE_LEGIBLE`),
    never extrapolated. An empty result is therefore a true statement that the composition shows
    no readable identity anywhere, and `required_landmarks` substitutes the composited colour for
    it rather than letting the row close on nothing.
    """

    landmarks: dict[int, SourceLandmark] = {}
    duration_frames = _int(_output(wire), "duration_frames")
    tracks = wire.get("tracks")
    if not isinstance(tracks, Sequence):
        raise ExpectationError("the composition declares no tracks")
    primary = {
        track["track_id"]
        for track in tracks
        if isinstance(track, Mapping) and track.get("kind") == PRIMARY_TRACK_KIND
    }
    layers = _visual_layers(wire)
    # GUARD: primary track only. An overlay occupying the same output frame would give that frame a
    # second source landmark, and the comparator addresses landmarks by output frame -- two answers
    # for one frame is not a stricter test, it is an ambiguous one. The primary track is what "the
    # right picture" means here; an overlay's own identity is the overlay families' subject.
    # GUARD: and one primary clip per frame, the uppermost: the layers run bottom-most first and a
    # later one overwrites the frame. Two primary clips can share a frame (the `clip_audio` owner
    # hand-over rows lay a second clip over `clip-main` showing the same source frame), and then
    # the identity row reads legibly through both, so stating every legible clip states the frame
    # twice. `identity_reads` keeps the uppermost clip by the same order, so where two clips share
    # a frame the observers read the row this states rather than the one it covers.
    for layer in layers:
        clip = layer.clip
        if clip.get("track_id") not in primary:
            continue
        asset = _asset(wire, clip.get("asset_id"))
        if asset is None or asset.get("kind") != "video":
            continue
        table = asset.get("landmarks")
        base = asset.get("source_time_base")
        if not isinstance(table, Sequence) or not isinstance(base, Mapping):
            continue
        if not layer.profile.carries_frame_id:
            continue
        base_num, base_den = _rational(asset, "source_time_base")
        start = _int(clip, "start_frame")
        end = min(start + _int(clip, "duration_frames"), duration_frames)
        for output_frame in range(start, end):
            shown = source_frame_at(wire, clip, output_frame)
            if shown is None:
                continue
            index, pts = shown
            if not _identity_legible(wire, layers, layer, output_frame, index):
                continue
            landmarks[output_frame] = SourceLandmark(
                output_frame=output_frame,
                source_frame=index,
                source_pts=pts,
                source_time_base_num=base_num,
                source_time_base_den=base_den,
            )
    return tuple(landmarks[frame] for frame in sorted(landmarks))


#: Why a clip's alpha is stated at no frame at all unless it declares an active cross-dissolve.
#:
#: GUARD: this is a measurability rule, not a convenience. A layer's alpha is recovered from the
#: composite as a ratio between two states of the same layer, so a clip whose opacity never changes
#: offers exactly one state for its whole lifetime and there is no second in-render reference to
#: take the ratio against. Stating its alpha anyway would require reading `opacity_bp` back with no
#: pixel evidence in the loop -- an expectation confirming itself -- or reimplementing the blend
#: equation inside the extractor, which is a second copy of the rules being tested. The visible
#: effect of a flat opacity is not lost: it is what the `patches` landmark measures.
ALPHA_MEASURABLE_ONLY_WITH_A_TRANSITION: Final = (
    "a layer's alpha is recovered as a ratio between two states of that layer, so a clip that "
    "declares no active cross-dissolve anywhere in its span presents one state for its whole "
    "lifetime and no observer has a second reference to measure against"
)


def _declares_a_ramp(clip: Mapping[str, Any]) -> bool:
    transition = clip.get("transition")
    if not isinstance(transition, Mapping) or transition.get("kind") == "none":
        return False
    return _int(transition, "duration_frames") > 0


def _alpha_frames(clip: Mapping[str, Any]) -> tuple[int, ...]:
    """The frames a clip's alpha is asserted at: its steady state, and its whole ramp.

    GUARD: the ramp frames are what make a transition row assert its transition. Sampling only the
    clip's midpoint compares the steady-state opacity, which every cross-dissolve row shares, so a
    one-frame dissolve, a four-frame one and an eight-frame one would state the same expectation and
    the duration boundaries would be untested. The first and last included frames and the first
    frame after the ramp are sampled explicitly, because those are the boundaries the corpus names.
    """

    start = _int(clip, "start_frame")
    span = _int(clip, "duration_frames")
    frames = {start + span // 2}
    transition = clip.get("transition")
    if isinstance(transition, Mapping) and transition.get("kind") != "none":
        ramp = _int(transition, "duration_frames")
        if ramp > 0:
            frames.update({start, start + ramp // 2, start + ramp - 1, start + ramp})
    return tuple(sorted(frame for frame in frames if start <= frame < start + span))


#: The least Euclidean distance, over the three channels, between the composite at a ramp's first
#: included frame and at its first steady frame for a point to carry the ramp's alpha. The
#: observer recovers the ramp as the least-squares projection of the sampled pixel's travel onto
#: the full travel, so encoder noise of `n` code values (as a vector) costs at most `|n| / |travel|`
#: of the ratio, and the frozen bound is 0.03.
#:
#: GUARD: the value is empirical, and it is the pinned encoder's. On the ramp's midpoint frame --
#: which the encoder codes as a B-frame, at its coarsest quantiser -- a flat 5x5 mean over the
#: black canvas measured up to 3.7 code values of projected error (the overlay's dark field over
#: black, travel 76, read 497 where the ramp stood at 425), and the same frame over the overlay's
#: loud mark (travel 259) measured 3.1. This floor keeps 4.5 code values -- a fifth more than the
#: worst measured -- inside the bound. It is not derived from the flat-patch allowance the
#: comparator grants (8 code values per channel): under that allowance no ramp in this corpus is
#: measurable at all, and the allowance is a bound on a single channel's mean, not on the
#: projection. Re-measure before trusting this number on any other encoder or encoder setting,
#: and never lower it to state a ramp a point cannot carry.
ALPHA_CONTRAST_FLOOR: Final = 150
#: The alpha point is searched for on this lattice over the ramping layer's footprint; four
#: pixels is finer than any margin the flatness rule can leave, so no qualifying region is missed.
_ALPHA_LATTICE_PX: Final = 4

#: Why a ramp's alpha is measured at the one point where the ramp moves the composite most, and
#: not at whichever sample point happens to carry the clip's label.
#:
#: GUARD: an alpha is recovered as `(observed - base) / (steady - base)`, a ratio of two pixel
#: differences, so its precision is set entirely by how far the ramp moves the pixel. The
#: overlay's flat field screened over the primary's flat field moves it by 34 code values per
#: channel, and the same field over the black canvas by 43; the overlay's 32 px mark over the
#: black canvas moves it by 259 as a vector. At the first two points a code value of noise is
#: 2-3% of the ratio, which is the whole frozen bound; at the third it is under 0.4%. A ramp
#: measured at a low-contrast point therefore fails or passes on the codec, not on the product,
#: and reporting that as a transition mismatch was a real, reported defect
#: (`opacity_blend.blend.normal`, 374 against 425 at the field over the primary, then 497 against
#: 425 at the field over black). The point is chosen where the declared ramp is
#: loudest and only where nothing else changes between the ramp's two ends, and a ramp with no
#: such point states no alpha at all -- `required_landmarks` substitutes the composited colour,
#: exactly as it does for a clip with no ramp. Never fall back to the clip's field point, and
#: never lower the floor to state an alpha the ramp cannot carry.
ALPHA_MEASURED_WHERE_THE_RAMP_IS_LOUDEST: Final = (
    "a cross-dissolve's alpha is measured at the sample point where the declared composite "
    "travels furthest between the ramp's first included frame and its first steady frame, with "
    "everything under the ramping layer unchanged between those two frames, because the ratio "
    "an observer recovers is only as precise as that travel is long"
)


@dataclass(frozen=True, slots=True)
class AlphaPoint:
    """Where and between which two frames one clip's cross-dissolve ramp is measured."""

    clip_id: str
    canvas_x: int
    canvas_y: int
    base_frame: int
    steady_frame: int
    opacity_bp: int
    contrast: int


def _ramp_ends(clip: Mapping[str, Any]) -> tuple[int, int] | None:
    if not _declares_a_ramp(clip):
        return None
    transition = clip["transition"]
    if not isinstance(transition, Mapping):  # `_declares_a_ramp` already established this.
        return None
    start = _int(clip, "start_frame")
    ramp = _int(transition, "duration_frames")
    if ramp >= _int(clip, "duration_frames"):
        return None
    return (start, start + ramp)


def alpha_sample_point(wire: Mapping[str, Any], clip_id: str) -> AlphaPoint | None:
    """The point at which one clip's ramp is measured, or `None` when no point can carry it.

    The candidates are a lattice over the ramping layer's footprint on the canvas. A candidate
    qualifies when the comparator's box is flat and clear of nearby edges at both ends of the ramp
    (the same two rules a colour sample obeys) and the composite of everything *under* the ramping
    layer is identical at those two ends -- so the ratio has one moving part -- and the point is
    chosen by the declared travel between them. The observers import this so that all of them
    measure the same point; the alpha itself is still a ratio of measured pixels, never this
    module's prediction.
    """

    layers = _visual_layers(wire)
    ramping = next((layer for layer in layers if str(layer.clip.get("clip_id")) == clip_id), None)
    if ramping is None:
        return None
    ends = _ramp_ends(ramping.clip)
    if ends is None:
        return None
    base_frame, steady_frame = ends
    output = _output(wire)
    canvas_width, canvas_height = _int(output, "width"), _int(output, "height")
    beneath = tuple(layer for layer in layers if layer is not ramping)
    footprint = _placed_rectangle(ramping.clip, ramping.profile, canvas_width, canvas_height)
    left = max(FLAT_SAMPLE_HALF_SPAN_PX, math.floor(footprint[0]))
    top = max(FLAT_SAMPLE_HALF_SPAN_PX, math.floor(footprint[1]))
    right = min(canvas_width - FLAT_SAMPLE_HALF_SPAN_PX, math.ceil(footprint[2]))
    bottom = min(canvas_height - FLAT_SAMPLE_HALF_SPAN_PX, math.ceil(footprint[3]))
    best: AlphaPoint | None = None
    for y in range(top, bottom, _ALPHA_LATTICE_PX):
        for x in range(left, right, _ALPHA_LATTICE_PX):
            if ramping.placement.to_source(x, y) is None:
                continue
            try:
                ground_base = _composite_at(wire, beneath, base_frame, x, y)
                ground_steady = _composite_at(wire, beneath, steady_frame, x, y)
                base = _composite_at(wire, layers, base_frame, x, y)
                steady = _composite_at(wire, layers, steady_frame, x, y)
            except ExpectationError:
                continue
            if ground_base != ground_steady:
                continue
            contrast = round(math.hypot(*(s - b for s, b in zip(steady, base, strict=True))))
            if contrast < ALPHA_CONTRAST_FLOOR or (best is not None and contrast <= best.contrast):
                continue
            if not all(
                _flat_composite(wire, layers, frame, x, y)
                and _well_separated_edges(wire, layers, frame, x, y)
                for frame in (base_frame, steady_frame)
            ):
                continue
            best = AlphaPoint(
                clip_id=clip_id,
                canvas_x=x,
                canvas_y=y,
                base_frame=base_frame,
                steady_frame=steady_frame,
                opacity_bp=_int(ramping.clip, "opacity_bp"),
                contrast=contrast,
            )
    return best


def alpha_sample_points(wire: Mapping[str, Any]) -> tuple[AlphaPoint, ...]:
    """One measurement point per enabled clip whose ramp can be measured at all."""

    points: list[AlphaPoint] = []
    for clip in _enabled_clips(wire):
        if _int(clip, "duration_frames") <= 0 or not _declares_a_ramp(clip):
            continue
        point = alpha_sample_point(wire, str(clip["clip_id"]))
        if point is not None:
            points.append(point)
    return tuple(points)


@dataclass(frozen=True, slots=True)
class AlphaTarget:
    """Where one clip's ramp is measured, its two reference frames, and the frames to report.

    Every value is a literal field on the clip (`start_frame`, `duration_frames`, `opacity_bp`,
    `transition.duration_frames`) or the canvas point `alpha_sample_point` chose for this exact
    wire. This states where and when to look and the clip's declared opacity ceiling, never what
    alpha should measure there; both observers (`scripts/nle_semantic_conformance.py` and the
    browser journey's `nleSemanticCanvasExtraction.ts`) recover the ramp from their own pixels at
    these frames by the same projection.
    """

    label: str
    canvas_x: int
    canvas_y: int
    opacity_bp: int
    base_frame: int
    steady_frame: int
    sample_frames: tuple[int, ...]


def alpha_targets(wire: Mapping[str, Any]) -> tuple[AlphaTarget, ...]:
    """One target per enabled clip whose ramp can be measured, with the frames to report it at.

    The reported frames are the ramp's ends, its midpoint, the frame before it plateaus and the
    clip's own duration midpoint -- the frames `_alpha_frames` states the expectation at -- so an
    observer that visits exactly these frames answers every alpha claim the row makes.
    """

    clips = {
        str(clip.get("clip_id")): clip
        for clip in wire.get("clips", ())
        if isinstance(clip, Mapping)
    }
    targets: list[AlphaTarget] = []
    for point in alpha_sample_points(wire):
        clip = clips.get(point.clip_id)
        if clip is None:
            continue
        start = clip.get("start_frame")
        span = clip.get("duration_frames")
        if not isinstance(start, int) or not isinstance(span, int):
            continue
        ramp = point.steady_frame - point.base_frame
        sample_frames = {
            point.base_frame,
            point.base_frame + ramp // 2,
            point.steady_frame - 1,
            point.steady_frame,
            start + span // 2,
        }
        targets.append(
            AlphaTarget(
                label=point.clip_id,
                canvas_x=point.canvas_x,
                canvas_y=point.canvas_y,
                opacity_bp=point.opacity_bp,
                base_frame=point.base_frame,
                steady_frame=point.steady_frame,
                sample_frames=tuple(sorted(sample_frames)),
            )
        )
    return tuple(targets)


def derive_alphas(wire: Mapping[str, Any]) -> tuple[AlphaSample, ...]:
    """The opacity each enabled clip must present, at every frame its own case is about.

    Opacity is declared in basis points and compared in thousandths, so the steady-state conversion
    is exact and there is nothing to infer. A cross-dissolve multiplies it by the ramp's own
    progression, which is likewise declared rather than measured. A clip with no ramp states
    nothing (`ALPHA_MEASURABLE_ONLY_WITH_A_TRANSITION`), and so does a ramp no sample point can
    carry (`ALPHA_MEASURED_WHERE_THE_RAMP_IS_LOUDEST`).
    """

    samples: list[AlphaSample] = []
    measurable = {point.clip_id for point in alpha_sample_points(wire)}
    for clip in _enabled_clips(wire):
        if _int(clip, "duration_frames") <= 0:
            continue
        if not _declares_a_ramp(clip):
            # See ALPHA_MEASURABLE_ONLY_WITH_A_TRANSITION: no observer can measure this one.
            continue
        if str(clip["clip_id"]) not in measurable:
            continue
        for frame in _alpha_frames(clip):
            samples.append(
                AlphaSample(
                    label=str(clip["clip_id"]),
                    output_frame=frame,
                    alpha_milli=round(_layer_alpha(clip, frame) * 1_000),
                )
            )
    return tuple(samples)


def derive_text(wire: Mapping[str, Any]) -> TextObservation | None:
    """The resolved text facts the composition declares, for the one text clip that carries them."""

    for clip in _enabled_clips(wire):
        text_section = clip.get("text")
        if not isinstance(text_section, Mapping):
            continue
        content = str(text_section.get("content", ""))
        style = text_section.get("style")
        style_map = style if isinstance(style, Mapping) else text_section
        return TextObservation(
            content=content,
            font_identity=str(style_map.get("font_asset_id", "")),
            # Line count is a property of the declared content, not of the raster: the contract
            # decides where the lines are, so counting them here is reading the declaration.
            line_count=content.count("\n") + 1,
            weight=_int(style_map, "weight"),
            style=str(style_map.get("style", "")),
            align=str(style_map.get("align", "")),
            # A positive ratio asserts that glyphs were actually drawn. The comparator only checks
            # that the observed ratio is above zero, so this never becomes a raster comparison.
            visible_glyph_ratio_milli=1,
        )
    return None


#: The declared placement rule, transcribed from the accepted composition contract so that the
#: expectation is an independent statement of where a layer belongs rather than a call into the
#: thing being measured.
#:
#: GUARD: this is deliberately a second copy of the arithmetic the render graph performs -- crop in
#: source pixels, scale to the layer size, rotate to a bounding box, then place the anchor at the
#: canvas position. That duplication is the whole point: an oracle that asks the renderer where it
#: put the layer cannot catch the renderer putting it in the wrong place. If the contract's
#: placement rule ever changes, this has to be changed deliberately and separately, and a run where
#: only one of the two was updated is supposed to go red.
BASIS_POINTS: Final = 10_000
MILLIDEGREES_PER_DEGREE: Final = 1_000


def _placed_rectangle(
    clip: Mapping[str, Any],
    profile: SourceProfile,
    canvas_width: int,
    canvas_height: int,
) -> tuple[float, float, float, float]:
    """Where one layer's rectangle lands on the canvas, in output pixels, before clipping."""

    crop = clip.get("crop")
    transform = clip.get("transform")
    if not isinstance(crop, Mapping) or not isinstance(transform, Mapping):
        raise ExpectationError("the clip declares no placement")
    # GUARD: the source's own size, from the profile. A crop is a fraction of the picture, and
    # measuring it against the 128-square constants would place every landmark of a
    # high-resolution source in the top-left sixteenth of it.
    source_width, source_height = profile.width, profile.height
    left = source_width * _int(crop, "left_bp") // BASIS_POINTS
    top = source_height * _int(crop, "top_bp") // BASIS_POINTS
    right = (source_width * (BASIS_POINTS - _int(crop, "right_bp")) + BASIS_POINTS - 1) // (
        BASIS_POINTS
    )
    bottom = (source_height * (BASIS_POINTS - _int(crop, "bottom_bp")) + BASIS_POINTS - 1) // (
        BASIS_POINTS
    )
    scaled_width = max(1, ((right - left) * _int(transform, "scale_x_bp") + 5_000) // BASIS_POINTS)
    scaled_height = max(1, ((bottom - top) * _int(transform, "scale_y_bp") + 5_000) // BASIS_POINTS)

    rotation = _int(transform, "rotation_mdeg")
    angle = math.radians(rotation / MILLIDEGREES_PER_DEGREE)
    cosine, sine = math.cos(angle), math.sin(angle)
    if rotation:
        # A rotated layer is padded out to its own bounding box, which is what an observer sees.
        placed_width = abs(scaled_width * cosine) + abs(scaled_height * sine)
        placed_height = abs(scaled_width * sine) + abs(scaled_height * cosine)
    else:
        placed_width, placed_height = float(scaled_width), float(scaled_height)

    offset_x = scaled_width * (_int(transform, "anchor_x_bp") / BASIS_POINTS - 0.5)
    offset_y = scaled_height * (_int(transform, "anchor_y_bp") / BASIS_POINTS - 0.5)
    anchor_x = offset_x * cosine - offset_y * sine
    anchor_y = offset_x * sine + offset_y * cosine
    center_x = canvas_width / 2 + canvas_width * _int(transform, "position_x_bp") / BASIS_POINTS
    center_y = canvas_height / 2 + canvas_height * _int(transform, "position_y_bp") / BASIS_POINTS
    origin_x = center_x - anchor_x - placed_width / 2
    origin_y = center_y - anchor_y - placed_height / 2
    return (origin_x, origin_y, origin_x + placed_width, origin_y + placed_height)


def _spans_whole_output(clip: Mapping[str, Any], duration_frames: int) -> bool:
    start = _int(clip, "start_frame")
    return start <= 0 and start + _int(clip, "duration_frames") >= duration_frames


def _placed_corners(
    clip: Mapping[str, Any],
    profile: SourceProfile,
    canvas_width: int,
    canvas_height: int,
) -> tuple[tuple[float, float], ...]:
    """The four corners of one layer's scaled, rotated rectangle on the canvas, in order."""

    placement = _placement(clip, profile, canvas_width, canvas_height)
    half_width, half_height = placement.scaled_width / 2, placement.scaled_height / 2
    cosine, sine = math.cos(placement.angle), math.sin(placement.angle)
    return tuple(
        (
            placement.center_x + x * cosine - y * sine,
            placement.center_y + x * sine + y * cosine,
        )
        for x, y in (
            (-half_width, -half_height),
            (half_width, -half_height),
            (half_width, half_height),
            (-half_width, half_height),
        )
    )


def _clip_polygon(
    polygon: Sequence[tuple[float, float]], canvas_width: int, canvas_height: int
) -> list[tuple[float, float]]:
    """The part of a convex polygon inside the canvas (Sutherland-Hodgman, one edge at a time)."""

    def against(
        points: Sequence[tuple[float, float]],
        axis: int,
        bound: float,
        keep_at_or_above: bool,
    ) -> list[tuple[float, float]]:
        def inside(point: tuple[float, float]) -> bool:
            return point[axis] >= bound if keep_at_or_above else point[axis] <= bound

        def crossing(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
            share = (bound - start[axis]) / (end[axis] - start[axis])
            return (
                start[0] + share * (end[0] - start[0]),
                start[1] + share * (end[1] - start[1]),
            )

        kept: list[tuple[float, float]] = []
        for index, current in enumerate(points):
            previous = points[index - 1]
            if inside(current):
                if not inside(previous):
                    kept.append(crossing(previous, current))
                kept.append(current)
            elif inside(previous):
                kept.append(crossing(previous, current))
        return kept

    points: list[tuple[float, float]] = list(polygon)
    for axis, bound, keep_at_or_above in (
        (0, 0.0, True),
        (0, float(canvas_width), False),
        (1, 0.0, True),
        (1, float(canvas_height), False),
    ):
        if not points:
            break
        points = against(points, axis, bound, keep_at_or_above)
    return points


def _visible_layer_box(
    clip: Mapping[str, Any],
    profile: SourceProfile,
    canvas_width: int,
    canvas_height: int,
) -> tuple[int, int, int, int] | None:
    """The bounding box of the part of a layer an observer can actually see, or `None`.

    GUARD: the box of the *clipped polygon*, not the clipped box of the whole layer. A rotated
    layer's bounding box reaches to the corner that rotation swung furthest, and when that corner
    is off the canvas nothing is painted at that x or y: `command.set_visual_transform` claimed a
    right edge of 278 where the picture ended at 259. For an unrotated layer the polygon is the
    rectangle and the two agree exactly.
    """

    points = _clip_polygon(
        _placed_corners(clip, profile, canvas_width, canvas_height), canvas_width, canvas_height
    )
    if not points:
        return None
    return _visible_box(
        (
            min(x for x, _y in points),
            min(y for _x, y in points),
            max(x for x, _y in points),
            max(y for _x, y in points),
        ),
        canvas_width,
        canvas_height,
    )


def _visible_box(
    box: tuple[float, float, float, float], canvas_width: int, canvas_height: int
) -> tuple[int, int, int, int] | None:
    """The part of a rectangle an observer can actually see, or `None` when none of it is."""

    left = max(0, round(box[0]))
    top = max(0, round(box[1]))
    right = min(canvas_width, round(box[2]))
    bottom = min(canvas_height, round(box[3]))
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


#: Why a layer whose field is the identity element of its own blend mode states no geometry.
#:
#: GUARD: geometry is recovered from the composite as an edge -- the boundary where a layer's
#: pixels stop changing what is underneath them. A white field composited with `multiply`, or a
#: black one with `screen`, changes nothing at any opacity: multiply by 255/255 and screen with 0
#: are the identity, so the layer's rectangle has no edge anywhere in the picture, by the arithmetic
#: of its own declared blend. The corpus's still is white under `multiply` on purpose, so that it
#: composites without erasing the marks beneath it. Its placement is not unasserted: its one
#: coloured mark is a `patches` landmark at the point the declared placement predicts, and a
#: misplaced still puts the plain field there instead. Never restore the rectangle by searching
#: for the still's mark and never widen the observer's luma threshold to manufacture an edge.
GEOMETRY_NEEDS_A_VISIBLE_EDGE: Final = (
    "a layer states geometry only when its declared field colour is not the identity element of "
    "its declared blend mode, because a white field under multiply or a black field under screen "
    "leaves the composite unchanged at every opacity and so has no edge an observer can find"
)


def _has_a_visible_edge(clip: Mapping[str, Any], profile: SourceProfile) -> bool:
    blend = clip.get("blend")
    if _int(clip, "opacity_bp") <= 0:
        return False
    if blend == "multiply" and profile.field == (255, 255, 255):
        return False
    return not (blend == "screen" and profile.field == (0, 0, 0))


@dataclass(frozen=True, slots=True)
class GeometryTarget:
    """One layer an observer must locate, and the frame at which to look for it."""

    clip_id: str
    asset_id: str
    sample_frame: int


def geometry_sample_frame(wire: Mapping[str, Any], clip_id: str) -> int | None:
    """The first frame in a clip's span at which the fewest other enabled clips are active.

    GUARD: a layer's rectangle is recovered from the composite as a luma edge and its fiducials by
    colour, and both are spoiled by whatever else is painted over them. Frame 0 is not that frame
    for every composition: a command that inserts an opaque clip at frame 0, or a title that
    starts there, covers a corner mark exactly where the old fixed choice looked, and the row then
    reported the layer three pixels short for a picture that was right. The frame is chosen from
    the declared spans alone.
    """

    clips = _enabled_clips(wire)
    subject = next((clip for clip in clips if str(clip.get("clip_id")) == clip_id), None)
    if subject is None:
        return None
    duration = _int(_output(wire), "duration_frames")
    start = _int(subject, "start_frame")
    end = min(start + _int(subject, "duration_frames"), duration)
    best: int | None = None
    fewest: int | None = None
    for frame in range(start, end):
        others = sum(1 for clip in clips if clip is not subject and _covers(clip, frame))
        if fewest is None or others < fewest:
            best, fewest = frame, others
    return best


def geometry_targets(wire: Mapping[str, Any]) -> tuple[GeometryTarget, ...]:
    """Which layers the observers look for, and where in time -- one per layer `derive_geometry`
    states, so a layer with no rectangle to observe is not searched for either."""

    stated = {landmark.label.partition(".")[0] for landmark in derive_geometry(wire)}
    targets: list[GeometryTarget] = []
    for clip in _enabled_clips(wire):
        clip_id = str(clip.get("clip_id"))
        if clip_id not in stated:
            continue
        frame = geometry_sample_frame(wire, clip_id)
        if frame is None:
            continue
        targets.append(
            GeometryTarget(clip_id=clip_id, asset_id=str(clip.get("asset_id")), sample_frame=frame)
        )
    return tuple(targets)


def derive_geometry(wire: Mapping[str, Any]) -> tuple[GeometryLandmark, ...]:
    """Where each full-duration visual layer, and each of its fiducials, must appear.

    Only layers that cover the whole output are stated. A `GeometryLandmark` carries no frame index,
    so a layer that exists for part of the timeline could not be compared unambiguously against one
    -- and every row whose subject is geometry (the transform and crop families) edits a layer that
    does span the output, so nothing the corpus actually asserts is lost by the restriction.

    GUARD: the per-patch fiducials are not decoration. The layer rectangle alone cannot tell a left
    crop from a right one, or an interior scale from one the canvas clamps: both leave the identical
    rectangle behind, and the corpus has boundary rows for each side. What separates them is where
    the source's own marks land inside that rectangle, so each placed patch is stated as its own
    landmark. Removing them silently turns eight boundary rows back into four.

    A layer or a fiducial that lands entirely outside the canvas contributes nothing: there is no
    rectangle to observe, and inventing one would turn an off-canvas placement into a comparison
    against something no observer can see.
    """

    output = _output(wire)
    canvas_width, canvas_height = _int(output, "width"), _int(output, "height")
    duration_frames = _int(output, "duration_frames")
    landmarks: list[GeometryLandmark] = []
    for clip in _enabled_clips(wire):
        profile = profile_for(str(clip.get("asset_id")))
        if profile is None or not _spans_whole_output(clip, duration_frames):
            continue
        if not _has_a_visible_edge(clip, profile):
            # See GEOMETRY_NEEDS_A_VISIBLE_EDGE: there is no rectangle to observe.
            continue
        label = str(clip["clip_id"])
        visible = _visible_layer_box(clip, profile, canvas_width, canvas_height)
        if visible is None:
            continue
        landmarks.append(
            GeometryLandmark(
                label=label,
                left=visible[0],
                top=visible[1],
                right=visible[2],
                bottom=visible[3],
            )
        )
        placement = _placement(clip, profile, canvas_width, canvas_height)
        for patch in profile.patches:
            corners = [
                placement.to_canvas(x, y)
                for x, y in (
                    (patch.left, patch.top),
                    (patch.left + patch.size - 1, patch.top),
                    (patch.left, patch.top + patch.size - 1),
                    (patch.left + patch.size - 1, patch.top + patch.size - 1),
                )
            ]
            seen = [corner for corner in corners if corner is not None]
            if len(seen) != len(corners):
                # Partly cropped away: an observer would measure the surviving sliver, and this
                # module cannot say which sliver without modelling the crop edge as well.
                continue
            box = (
                min(corner[0] for corner in seen),
                min(corner[1] for corner in seen),
                max(corner[0] for corner in seen) + 1,
                max(corner[1] for corner in seen) + 1,
            )
            fiducial = _visible_box(box, canvas_width, canvas_height)
            if fiducial is None:
                continue
            landmarks.append(
                GeometryLandmark(
                    label=f"{label}.{patch.label}",
                    left=fiducial[0],
                    top=fiducial[1],
                    right=fiducial[2],
                    bottom=fiducial[3],
                )
            )
    return tuple(landmarks)


# ---------------------------------------------------------------------------------------------
# The colour oracle.
#
# A patch comparison asks what colour a named point on the canvas must show, and for this corpus
# that is a composite of up to three layers: a primary, a half-size video overlay that blends over
# it for twelve frames, and a still that multiplies over everything. The plan's section 14.3 admits
# small analytic fixture formulas as test oracles, and this is one: it models the declared
# composition rules -- crop, scale, rotate, place, colour-adjust, blend, opacity -- over the
# declared synthetic media, and nothing else.
#
# GUARD: this is a second, independent statement of the compositing rules, not a call into the
# renderer, for the same reason the placement arithmetic above is. It also deliberately samples at
# *predicted* points: where a layer landed is what the geometry landmark asserts, and a patch
# landmark asserts what colour is there. Do not merge the two by searching for the patch in the
# frame -- a search would make a misplaced layer look correct as long as its colour was right.
# ---------------------------------------------------------------------------------------------

#: A sample must be a flat region: the comparator takes a 5x5 interior mean at least 4 px from an
#: edge, so a labelled point is only offered when at least this many output pixels of uniform
#: colour surround it.
MINIMUM_SAMPLE_SPAN_PX: Final = 13


@dataclass(frozen=True, slots=True)
class LayerPlacement:
    """The declared mapping between one layer's source pixels and the output canvas."""

    crop_left: int
    crop_top: int
    crop_width: int
    crop_height: int
    scaled_width: int
    scaled_height: int
    angle: float
    center_x: float
    center_y: float

    def _rotate(self, x: float, y: float, sign: int) -> tuple[float, float]:
        cosine, sine = math.cos(self.angle), sign * math.sin(self.angle)
        return (x * cosine - y * sine, x * sine + y * cosine)

    def to_canvas(self, source_x: int, source_y: int) -> tuple[float, float] | None:
        """Where a source pixel's centre lands on the canvas, or `None` when it is cropped."""

        cropped_x = source_x - self.crop_left
        cropped_y = source_y - self.crop_top
        if not (0 <= cropped_x < self.crop_width and 0 <= cropped_y < self.crop_height):
            return None
        width, height = self.scaled_width, self.scaled_height
        scaled_x = (cropped_x + 0.5) * width / self.crop_width - width / 2
        scaled_y = (cropped_y + 0.5) * height / self.crop_height - height / 2
        rotated_x, rotated_y = self._rotate(scaled_x, scaled_y, 1)
        return (self.center_x + rotated_x, self.center_y + rotated_y)

    def to_source(self, canvas_x: float, canvas_y: float) -> tuple[int, int] | None:
        """Which source pixel a canvas point comes from, `None` when the layer misses it."""

        rotated_x, rotated_y = self._rotate(canvas_x - self.center_x, canvas_y - self.center_y, -1)
        scaled_x = rotated_x + self.scaled_width / 2
        scaled_y = rotated_y + self.scaled_height / 2
        if not (0 <= scaled_x < self.scaled_width and 0 <= scaled_y < self.scaled_height):
            return None
        cropped_x = scaled_x * self.crop_width / self.scaled_width
        cropped_y = scaled_y * self.crop_height / self.scaled_height
        return (self.crop_left + int(cropped_x), self.crop_top + int(cropped_y))

    def scale_x(self) -> float:
        return self.scaled_width / self.crop_width

    def scale_y(self) -> float:
        return self.scaled_height / self.crop_height


def _placement(
    clip: Mapping[str, Any],
    profile: SourceProfile,
    canvas_width: int,
    canvas_height: int,
) -> LayerPlacement:
    crop = clip.get("crop")
    transform = clip.get("transform")
    if not isinstance(crop, Mapping) or not isinstance(transform, Mapping):
        raise ExpectationError("the clip declares no placement")
    # GUARD: the source's own size, from the profile. A crop is a fraction of the picture, and
    # measuring it against the 128-square constants would place every landmark of a
    # high-resolution source in the top-left sixteenth of it.
    source_width, source_height = profile.width, profile.height
    left = source_width * _int(crop, "left_bp") // BASIS_POINTS
    top = source_height * _int(crop, "top_bp") // BASIS_POINTS
    right = (source_width * (BASIS_POINTS - _int(crop, "right_bp")) + BASIS_POINTS - 1) // (
        BASIS_POINTS
    )
    bottom = (source_height * (BASIS_POINTS - _int(crop, "bottom_bp")) + BASIS_POINTS - 1) // (
        BASIS_POINTS
    )
    crop_width, crop_height = right - left, bottom - top
    scaled_width = max(1, (crop_width * _int(transform, "scale_x_bp") + 5_000) // BASIS_POINTS)
    scaled_height = max(1, (crop_height * _int(transform, "scale_y_bp") + 5_000) // BASIS_POINTS)
    angle = math.radians(_int(transform, "rotation_mdeg") / MILLIDEGREES_PER_DEGREE)
    cosine, sine = math.cos(angle), math.sin(angle)
    offset_x = scaled_width * (_int(transform, "anchor_x_bp") / BASIS_POINTS - 0.5)
    offset_y = scaled_height * (_int(transform, "anchor_y_bp") / BASIS_POINTS - 0.5)
    anchor_x = offset_x * cosine - offset_y * sine
    anchor_y = offset_x * sine + offset_y * cosine
    return LayerPlacement(
        crop_left=left,
        crop_top=top,
        crop_width=crop_width,
        crop_height=crop_height,
        scaled_width=scaled_width,
        scaled_height=scaled_height,
        angle=angle,
        center_x=(
            canvas_width / 2
            + canvas_width * _int(transform, "position_x_bp") / BASIS_POINTS
            - anchor_x
        ),
        center_y=(
            canvas_height / 2
            + canvas_height * _int(transform, "position_y_bp") / BASIS_POINTS
            - anchor_y
        ),
    )


@dataclass(frozen=True, slots=True)
class VisualLayer:
    """One enabled visual clip, its source profile and where it lands, bottom-most first."""

    clip: Mapping[str, Any]
    profile: SourceProfile
    placement: LayerPlacement


def _visual_layers(wire: Mapping[str, Any]) -> tuple[VisualLayer, ...]:
    """Every enabled clip with a synthetic source, ordered from the bottom of the stack upward."""

    output = _output(wire)
    canvas_width, canvas_height = _int(output, "width"), _int(output, "height")
    tracks = wire.get("tracks")
    if not isinstance(tracks, Sequence):
        raise ExpectationError("the composition declares no tracks")
    order = {
        track["track_id"]: _int(track, "order")
        for track in tracks
        if isinstance(track, Mapping) and track["track_id"] in _enabled_tracks(wire)
    }
    layers: list[tuple[int, VisualLayer]] = []
    for clip in _enabled_clips(wire):
        profile = profile_for(str(clip.get("asset_id")))
        if profile is None or clip.get("track_id") not in order:
            continue
        layers.append(
            (
                order[clip["track_id"]],
                VisualLayer(
                    clip=clip,
                    profile=profile,
                    placement=_placement(clip, profile, canvas_width, canvas_height),
                ),
            )
        )
    return tuple(layer for _rank, layer in sorted(layers, key=lambda item: item[0]))


def _covers(clip: Mapping[str, Any], output_frame: int) -> bool:
    start = _int(clip, "start_frame")
    return start <= output_frame < start + _int(clip, "duration_frames")


def _layer_alpha(clip: Mapping[str, Any], output_frame: int) -> float:
    """The layer's opacity at one frame, including a cross-dissolve's own ramp."""

    alpha = _int(clip, "opacity_bp") / BASIS_POINTS
    transition = clip.get("transition")
    if isinstance(transition, Mapping) and transition.get("kind") != "none":
        frames = _int(transition, "duration_frames")
        offset = output_frame - _int(clip, "start_frame")
        if frames > 0 and 0 <= offset < frames:
            alpha *= offset / frames
    return alpha


def _clamp8(value: float) -> int:
    return max(0, min(255, round(value)))


#: The `eq` filter's fixed-point arithmetic, as the pinned renderer executes it on x86: contrast
#: is carried as a 12-bit fraction, brightness as an integer offset derived through a percentage,
#: and each plane value is `floor(value * contrast) + offset`, saturated to 8 bits.
_EQ_CONTRAST_SCALE: Final = 4096


def _as_float32(value: float) -> float:
    return float(struct.unpack("f", struct.pack("f", value))[0])


def _eq_plane(value: int, contrast: float, brightness: float) -> int:
    """One 8-bit plane value through the pinned renderer's `eq` filter.

    GUARD: this is the SIMD path of FFmpeg's `vf_eq` (`process_MMXEXT`), not the C reference LUT:
    `contrast_fixed = (short)(contrast * 256 * 16)`, `brightness_fixed = ((short)(100 *
    brightness + 100) * 511) / 200 - 128 - contrast_fixed / 32` in C integer arithmetic, and
    per pixel `((value << 4) * contrast_fixed >> 16) + brightness_fixed`, saturated. It was fit
    against the pinned binary's measured lookup tables for every plane, not derived from the
    documentation: the reference formula `256 * (contrast * (v - 0.5) + 0.5 + brightness)` is one
    or two code values high on almost every entry, which is enough to move a colour-adjust
    expectation past its bound. `100 * brightness + 100` must be evaluated in floating point and
    truncated toward zero exactly as C does -- `-0.2` becomes 79, not 80.
    """

    # The filter clips both options through single-precision floats before any arithmetic, so a
    # declared -0.2 is really -0.20000000298 and `100 * b + 100` lands just under 80, not on it.
    contrast = _as_float32(contrast)
    brightness = _as_float32(brightness)
    if contrast == 1.0 and brightness == 0.0:
        # `check_values`: a plane whose parameters are all identity is copied, not adjusted. The
        # fixed-point path below would otherwise subtract one from every value, so an effect
        # declared at its identity values must take this branch to leave the picture alone.
        return value
    contrast_fixed = int(contrast * _EQ_CONTRAST_SCALE)
    percent = int(100.0 * brightness + 100.0)
    brightness_fixed = int(percent * 511 / 200) - 128 - int(contrast_fixed / 32)
    scaled = ((value << 4) * contrast_fixed) >> 16
    return max(0, min(255, scaled + brightness_fixed))


def _to_yuv(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Full-range BT.709 8-bit planes, rounded as the renderer's colour conversion rounds."""

    red, green, blue = rgb
    luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    cb = (blue - luma) / 1.8556 + 128
    cr = (red - luma) / 1.5748 + 128
    return (_clamp8(luma), _clamp8(cb), _clamp8(cr))


def _from_yuv(yuv: tuple[int, int, int]) -> tuple[float, float, float]:
    luma, cb, cr = yuv
    red = luma + 1.5748 * (cr - 128)
    blue = luma + 1.8556 * (cb - 128)
    green = luma - 0.1873 * (cb - 128) - 0.4681 * (cr - 128)
    return (
        min(255.0, max(0.0, red)),
        min(255.0, max(0.0, green)),
        min(255.0, max(0.0, blue)),
    )


def _adjusted(rgb: tuple[int, int, int], effect: object) -> tuple[float, float, float]:
    """The declared colour adjustment, as the render graph performs it.

    The graph converts the layer to full-range BT.709 `yuv444p`, runs FFmpeg's `eq` with the
    declared brightness, contrast and saturation, and converts back; brightness and contrast act
    on the luma plane and saturation is the contrast of the two chroma planes. Each plane is an
    8-bit integer between those steps, so a brightness that lifts luma past white saturates the
    luma plane and leaves the chroma where it was -- a bright red comes back as (255, 221, 221),
    not (255, 255, 255), and an expectation that clamped in RGB stated the wrong picture.
    """

    if not isinstance(effect, Mapping) or effect.get("kind") != "color_adjust_v1":
        return (float(rgb[0]), float(rgb[1]), float(rgb[2]))
    brightness = _int(effect, "brightness_permille") / 1000
    contrast = _int(effect, "contrast_permille") / 1000
    saturation = _int(effect, "saturation_permille") / 1000
    luma, cb, cr = _to_yuv(rgb)
    return _from_yuv(
        (
            _eq_plane(luma, contrast, brightness),
            _eq_plane(cb, saturation, 0.0),
            _eq_plane(cr, saturation, 0.0),
        )
    )


def _blended(
    base: tuple[float, float, float], top: tuple[float, float, float], mode: object
) -> tuple[float, float, float]:
    if mode == "multiply":
        return (base[0] * top[0] / 255, base[1] * top[1] / 255, base[2] * top[2] / 255)
    if mode == "screen":
        return tuple(255 - (255 - b) * (255 - t) / 255 for b, t in zip(base, top, strict=True))  # type: ignore[return-value]
    return top


def _source_index(wire: Mapping[str, Any], layer: VisualLayer, output_frame: int) -> int:
    """Which source frame this layer shows at one output frame.

    `SOURCE_FRAME_FOLLOWS_THE_LANDMARK_TABLE` when the asset declares a table. GUARD: clamped to
    the source's own range, because that is what the render path does -- a layer whose range runs
    past its source holds the final frame rather than failing. Without the clamp the oracle asks
    for a frame the source never had, the identity row cannot state it, and the expectation raises
    instead of describing the picture that was actually produced.
    """

    if not layer.profile.carries_frame_id:
        # A still has one frame and states no identity, so every output frame reads the same one.
        return 0
    shown = source_frame_at(wire, layer.clip, output_frame)
    if shown is not None:
        return max(0, min(layer.profile.frame_count - 1, shown[0]))
    index = _int(layer.clip, "source_start_frame") + (
        output_frame - _int(layer.clip, "start_frame")
    )
    return max(0, min(layer.profile.frame_count - 1, index))


def _text_occlusion_bands(
    wire: Mapping[str, Any], output_frame: int
) -> tuple[tuple[float, float], ...]:
    """The canvas rows a text layer may have painted over at one frame, conservatively bounded.

    GUARD: this exists because the oracle cannot describe those rows and must say so instead of
    guessing. A text clip paints a background box and glyphs whose extent comes from font metrics --
    glyph advances, ascent, descent -- that no analytic formula over the declared wire has, which is
    the same wall that leaves fifteen `text.*` rows indistinguishable. `_visual_layers` skips a text
    clip because `profile_for(None)` is `None`, so `composite_colour` was silently describing those
    pixels as if the text were absent. That is a *wrong* expectation rather than a missing one, and
    it is exactly how the fixture's two bottom patches drifted out of the frozen 8-unit bound: the
    title's half-alpha black box sits over canvas row 118 and both of them are sampled there.

    The band is derived from the declaration, not fitted to a measurement. Vertically a text layer
    is bounded by its own line box -- `lines * size_px * line_height_bp / 10000`, scaled by the
    layer's scale and centred at its declared position -- because that is what the contract says
    the lines occupy. The uncertainty font metrics introduce is horizontal, in glyph advances, so
    the band spans the full canvas width and takes the leading as its vertical margin, on the
    assumption that a renderer may distribute leading outside the box rather than inside. Never
    narrow it to fit a sample point back in: the correct response to a point inside the band is to
    stop sampling there, not to claim the oracle can see it.
    """

    output = _output(wire)
    height = _int(output, "height")
    bands: list[tuple[float, float]] = []
    for clip in _enabled_clips(wire):
        section = clip.get("text")
        if not isinstance(section, Mapping) or not _covers(clip, output_frame):
            continue
        transform = clip.get("transform")
        if not isinstance(transform, Mapping):
            raise ExpectationError(f"{clip.get('clip_id')} declares text but no transform")
        lines = str(section.get("content", "")).count("\n") + 1
        size = _int(section, "size_px")
        scale_y = _int(transform, "scale_y_bp") / BASIS_POINTS
        box = lines * size * (_int(section, "line_height_bp") / BASIS_POINTS) * scale_y
        centre = height / 2 + height * (_int(transform, "position_y_bp") / BASIS_POINTS)
        centre -= box * (_int(transform, "anchor_y_bp") / BASIS_POINTS - 0.5)
        # The leading may fall outside the line box rather than inside it, so it is the margin.
        margin = box - lines * size * scale_y
        bands.append((centre - box / 2 - margin, centre + box / 2 + margin))
    return tuple(bands)


def _text_occluded(wire: Mapping[str, Any], output_frame: int, canvas_y: float) -> bool:
    bands = _text_occlusion_bands(wire, output_frame)
    return any(top <= canvas_y <= bottom for top, bottom in bands)


def _composite_at(
    wire: Mapping[str, Any],
    layers: Sequence[VisualLayer],
    output_frame: int,
    canvas_x: float,
    canvas_y: float,
) -> tuple[int, int, int]:
    """`composite_colour` over an already-built layer stack, for callers that sample many points."""

    if _text_occluded(wire, output_frame, canvas_y):
        raise ExpectationError(
            f"canvas row {canvas_y} at frame {output_frame} may be painted by a text layer, "
            "whose extent this oracle cannot derive"
        )
    accumulated: tuple[float, float, float] = (0.0, 0.0, 0.0)
    for layer in layers:
        if not _covers(layer.clip, output_frame):
            continue
        point = layer.placement.to_source(canvas_x, canvas_y)
        if point is None:
            continue
        colour = source_colour(layer.profile, _source_index(wire, layer, output_frame), *point)
        if _PRESENTATION.get() == "browser":
            colour = browser_decoded_colour(layer.profile, colour)
        top = _adjusted(colour, layer.clip.get("effect"))
        mixed = _blended(accumulated, top, layer.clip.get("blend"))
        alpha = _layer_alpha(layer.clip, output_frame)
        accumulated = tuple(  # type: ignore[assignment]
            base * (1 - alpha) + value * alpha
            for base, value in zip(accumulated, mixed, strict=True)
        )
    return (_clamp8(accumulated[0]), _clamp8(accumulated[1]), _clamp8(accumulated[2]))


#: How far Chromium's BT.709 decode of a video source falls short on the blue channel, per code
#: value of chroma-blue away from neutral.
#:
#: The browser side observes the product's monitor canvas, onto which the product draws a decoded
#: `<video>` frame. Chromium decodes the corpus's BT.709 limited-range sources through libyuv,
#: whose fixed-point BT.709 constants carry a Cb-to-blue coefficient of 2.0 in place of the
#: standard 2.112 (the 8-bit table cannot hold 2.112 * 64). Measured on the pinned Chromium
#: against ffmpeg's decode of the same file: red and green agree to a code value, blue falls
#: short by 0.112 * (Cb - 128) -- seven code values on the primary's blue patch, eight the other
#: way on its yellow one, past the comparator's eight-value bound. This is the presentation
#: surface's own arithmetic, not the compositor's, so the browser expectation states the colour
#: Chromium presents and the final expectation states the colour the artifact carries. Only video
#: sources pass through that decoder; the image reference, text and the canvas ground do not.
#: Never widen the patch bound to cover this instead: the bound is what catches a wrong layer.
BROWSER_VIDEO_DECODE_CB_DEFICIT: Final = 2.112 - 2.0
BROWSER_VIDEO_DECODES_THROUGH_LIBYUV_BT709: Final = (
    "the browser side is held to the colour Chromium's BT.709 decode presents, whose blue channel "
    "falls short of the standard by 0.112 per code value of chroma-blue; only video sources are "
    "affected, and the final artifact is held to the declared colour"
)

_PRESENTATION: ContextVar[str] = ContextVar("semantic_conformance_presentation", default="final")


@contextmanager
def presented_by_browser() -> Iterator[None]:
    """Derive expectations for the browser's presentation of the composition.

    Inside this context `composite_colour` reads every video source through
    `browser_decoded_colour`; everything else the oracle states is unchanged.
    """

    token = _PRESENTATION.set("browser")
    try:
        yield
    finally:
        _PRESENTATION.reset(token)


def derive_browser_expectation(recipe: CaseRecipe, wire: Mapping[str, Any]) -> Expectation:
    """What the browser side is held to: the composition as the preview presents it.

    The colours are the ones Chromium's decoder presents (`presented_by_browser`), and the
    landmarks are only those the preview can carry: every landmark `BROWSER_UNOBSERVABLE`
    explains is left unstated, so the comparator asks the canvas for nothing it cannot show and
    the final side is still held to all of it. A row the preview scales down is compared on the
    final side alone (`BROWSER_OBSERVES_ONLY_AN_UNSCALED_PREVIEW`), which the join decides.
    """

    with presented_by_browser():
        expectation = derive_expectation(recipe, wire)
    unstated: dict[str, Any] = {}
    for landmark in BROWSER_UNOBSERVABLE:
        value = getattr(expectation, landmark)
        unstated[landmark] = () if isinstance(value, tuple) else None
    return dataclasses.replace(expectation, **unstated)


def browser_decoded_colour(
    profile: SourceProfile, colour: tuple[int, int, int]
) -> tuple[int, int, int]:
    """The colour Chromium presents for one declared source pixel."""

    if not profile.decoded_by_browser:
        return colour
    red, green, blue = colour
    luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    chroma_blue = 224 * ((blue - luma) / 1.8556) / 255
    return (red, green, _clamp8(blue - BROWSER_VIDEO_DECODE_CB_DEFICIT * chroma_blue))


def composite_colour(
    wire: Mapping[str, Any], output_frame: int, canvas_x: float, canvas_y: float
) -> tuple[int, int, int]:
    """The colour the declared composition puts at one canvas point, over the transparent ground.

    Raises `ExpectationError` for a point a text layer may have painted over, because the oracle
    has no font metrics and would otherwise describe it as if the text were absent.
    """

    return _composite_at(wire, _visual_layers(wire), output_frame, canvas_x, canvas_y)


#: The comparator samples a 5x5 mean, so a point is a sample point only when the declared
#: composite is one flat colour over that box and a guard band around it: the pinned encoder
#: subsamples chroma 2:1 and its deblocking reaches a further pixel, so a colour edge inside the
#: band would bleed into the mean. Half of five is two; three more is the band; every pixel of the
#: resulting 11x11 box is checked, not a lattice, so no mark can hide between checks.
PATCH_MEAN_HALF_SPAN_PX: Final = 2
ENCODE_BLEED_GUARD_PX: Final = 3
FLAT_SAMPLE_HALF_SPAN_PX: Final = PATCH_MEAN_HALF_SPAN_PX + ENCODE_BLEED_GUARD_PX

#: Why a sample point is offered only where the whole composite is flat around it, not merely the
#: layer it belongs to.
#:
#: GUARD: the layer a point is named for can be perfectly flat there while the composite is not.
#: A still's field sample sits over the primary, and a row that scales or crops the primary moves
#: one of the primary's own patch edges under that point; a point named for the primary's field
#: can end up 2 px from a crop edge, so the comparator's 5x5 box straddles the transparent ground.
#: In every such case the observer measures a mean of two things and the expectation states one,
#: and the row reports the renderer wrong for a picture that is right. The composite, not the
#: layer, is what the observer samples, so the composite is what has to be flat. Never shrink the
#: box below the comparator's own interior rule to keep a point, and never replace this with a
#: per-layer check.
SAMPLE_POINT_NEEDS_A_FLAT_COMPOSITE: Final = (
    "a colour sample is stated only where the declared composite of every layer is one flat "
    "colour over the comparator's whole interior box, because the observer measures the "
    "composite and a box that straddles any edge measures a mean the expectation never stated"
)


#: How far two composite transitions must be from each other, along any row or column through
#: the comparator's box, out to the width of one of the pinned encoder's coding blocks on either
#: side. The encoder codes chroma in blocks that span sixteen output pixels; a block holding one
#: straight edge is coded faithfully (measured: a colour bar's mean is exact to within a code value
#: right up to the edge), but a block holding two transitions a few pixels apart is not (measured:
#: an overlay whose edge fell 2.4 px inside a patch's own edge moved the patch's blue mean by ten
#: code values eight pixels away).
CODING_BLOCK_GUARD_PX: Final = 8

#: Why a sample point also refuses two nearby transitions, and not just an edge inside its box.
#:
#: GUARD: the flatness rule above keeps the comparator's box clear of edges, and the pinned
#: encoder honours that: a single edge is coded to the pixel. What it does not honour is two
#: transitions inside one coding block. A layer edge that lands a few pixels inside another
#: layer's mark makes exactly that, and the block's quantised chroma then smears across the whole
#: box -- `transform.position_x_bp.interior` reported the primary's top-right patch ten code
#: values low in blue for a picture that was right. A mark's own two edges are sixteen pixels
#: apart and are not the case; two transitions closer than one block are. Never loosen this to
#: "one edge inside the window": the mark's own edges would fail it and every patch would go.
#:
#: The window is two coding blocks on either side of the box, not one: the smear reaches a block
#: from the *pair*, and a pair whose nearer edge sits a block from the box has its farther edge
#: beyond it. Scanning one block missed exactly that -- a cropped layer edge 5.5 px past the
#: patch's own edge, the patch edge 7.5 px from the sample point -- and stated the sample.
#:
#: A transition here is a change of *chroma*. What smears is the chroma plane: it is coded at
#: half resolution in each direction and quantised more coarsely than luma, so two chroma edges
#: in one block share one poorly coded chroma sample; luma is coded at full resolution and a
#: black-and-white edge pair is coded to the pixel (measured: the canvas centre five pixels below
#: the primary's identity row -- a run of achromatic cells six pixels tall -- read exact in
#: every row that stated it). Counting luma edges would drop every sample near the identity row,
#: including the centre of the smallest canvas, for a smear that does not occur.
SAMPLE_POINT_NEEDS_WELL_SEPARATED_EDGES: Final = (
    "a colour sample is stated only where, along every row and column through the comparator's "
    "box and out to two coding blocks on either side, no two chroma transitions of the declared "
    "composite closer together than one coding block have their nearer transition within one "
    "coding block of the box, because the pinned encoder codes a single edge faithfully and "
    "smears the shared chroma of two"
)

#: The least change of chroma, as `|dCb| + |dCr|` on the full-range BT.709 planes, that counts
#: as a transition; a change below it is rounding, not an edge.
_CHROMA_TRANSITION_FLOOR_PX: Final = 2


def _well_separated_edges(
    wire: Mapping[str, Any],
    layers: Sequence[VisualLayer],
    output_frame: int,
    canvas_x: int,
    canvas_y: int,
) -> bool:
    """See SAMPLE_POINT_NEEDS_WELL_SEPARATED_EDGES."""

    output = _output(wire)
    canvas_width, canvas_height = _int(output, "width"), _int(output, "height")
    half = PATCH_MEAN_HALF_SPAN_PX
    near = half + CODING_BLOCK_GUARD_PX
    reach = near + CODING_BLOCK_GUARD_PX

    def separated(points: Sequence[tuple[int, int]]) -> bool:
        # `points` runs from `-reach` to `+reach` around the sample; index `reach` is the sample.
        last_change: int | None = None
        previous: tuple[int, int] | None = None
        for index, (x, y) in enumerate(points):
            if not (0 <= x < canvas_width and 0 <= y < canvas_height):
                previous = None
                continue
            try:
                _luma, cb, cr = _to_yuv(_composite_at(wire, layers, output_frame, x, y))
            except ExpectationError:
                # A pixel the composite cannot describe (a text layer's band) within a block of
                # the box refuses the sample, as it always has; one further out is simply
                # unknown territory, and no transition is counted across it.
                if abs(index - reach) <= near:
                    raise
                previous = None
                continue
            chroma = (cb, cr)
            if previous is not None and (
                abs(chroma[0] - previous[0]) + abs(chroma[1] - previous[1])
                >= _CHROMA_TRANSITION_FLOOR_PX
            ):
                if last_change is not None and index - last_change < CODING_BLOCK_GUARD_PX:
                    nearer = min(abs(last_change - reach), abs(index - reach))
                    if nearer <= near:
                        return False
                last_change = index
            previous = chroma
        return True

    try:
        for offset in range(-half, half + 1):
            row = [(canvas_x + step, canvas_y + offset) for step in range(-reach, reach + 1)]
            column = [(canvas_x + offset, canvas_y + step) for step in range(-reach, reach + 1)]
            if not separated(row) or not separated(column):
                return False
    except ExpectationError:
        return False
    return True


def _flat_composite(
    wire: Mapping[str, Any],
    layers: Sequence[VisualLayer],
    output_frame: int,
    canvas_x: int,
    canvas_y: int,
) -> bool:
    """Whether the declared composite is one colour over the interior box around this point."""

    output = _output(wire)
    canvas_width, canvas_height = _int(output, "width"), _int(output, "height")
    half = FLAT_SAMPLE_HALF_SPAN_PX
    if not (half <= canvas_x < canvas_width - half and half <= canvas_y < canvas_height - half):
        return False
    try:
        centre = _composite_at(wire, layers, output_frame, canvas_x, canvas_y)
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                sample = _composite_at(wire, layers, output_frame, canvas_x + dx, canvas_y + dy)
                if sample != centre:
                    return False
    except ExpectationError:
        return False
    return True


def patch_sample_frame(wire: Mapping[str, Any]) -> int:
    """The one frame every patch landmark is sampled at.

    A `PatchSample` carries no frame index, so the frame has to be a property of the composition
    rather than of the landmark. It is the midpoint of the shortest visual layer's span, because
    that is the frame at which every layer the composition declares is actually present -- sampling
    where a layer is absent would quietly turn a blend comparison into a single-layer one.
    """

    layers = _visual_layers(wire)
    if not layers:
        return 0
    shortest = min(layers, key=lambda item: _int(item.clip, "duration_frames"))
    span = _int(shortest.clip, "duration_frames")
    frame = _int(shortest.clip, "start_frame") + span // 2
    return max(0, min(_int(_output(wire), "duration_frames") - 1, frame))


@dataclass(frozen=True, slots=True)
class SamplePoint:
    """One labelled canvas point an observer must measure and this module can state."""

    label: str
    output_frame: int
    canvas_x: int
    canvas_y: int


def _ramp_sample_frames(wire: Mapping[str, Any]) -> tuple[int, ...]:
    """The mid-ramp frames a declared cross-dissolve makes visible in the composited colour.

    GUARD: without these the steady-state sample frame is the only one compared, and it lies after
    every ramp this corpus declares -- the sample frame is the shortest visual layer's midpoint, and
    the shortest layer is the one that dissolves. A row that removes or shortens a ramp then states
    exactly what an unrelated identity row states, which is how `transition.none_identity` came to
    be indistinguishable from four `history_currentness` rows. The ramp frame is the one place the
    transition is a fact about pixels rather than about a declaration, which matters because a flat
    layer's alpha is not measurable at all (`ALPHA_MEASURABLE_ONLY_WITH_A_TRANSITION`).
    """

    frames: set[int] = set()
    for clip in _enabled_clips(wire):
        if not _declares_a_ramp(clip):
            continue
        transition = clip["transition"]
        if not isinstance(transition, Mapping):  # `_declares_a_ramp` already established this.
            continue
        frames.add(_int(clip, "start_frame") + _int(transition, "duration_frames") // 2)
    duration = _int(_output(wire), "duration_frames")
    return tuple(sorted(frame for frame in frames if 0 <= frame < duration))


#: The one sample point that belongs to the canvas rather than to a layer. A row that shrinks the
#: output until no layer's own field or mark lands on it (`output.dimensions_minimum` is sixteen
#: pixels square under a 128-pixel primary) still has a picture, and its centre is where every
#: full-canvas layer this corpus declares is present; stating its composite is what keeps such a
#: row from closing on its container alone.
CANVAS_SAMPLE_LABEL: Final = "canvas"


def _points_at_frame(
    wire: Mapping[str, Any], frame: int, suffix: str, *, absent: Collection[str] = ()
) -> tuple[SamplePoint, ...]:
    """The points stated at one frame; `absent` names layers whose points are stated even where
    the layer is not on the canvas at this frame (see EVERY_PARTIAL_LAYER_IS_SAMPLED_AT_ITS_ENDS).
    """

    output = _output(wire)
    canvas_width, canvas_height = _int(output, "width"), _int(output, "height")
    layers = _visual_layers(wire)
    points: list[SamplePoint] = []
    centre = (canvas_width // 2, canvas_height // 2)
    if _flat_composite(wire, layers, frame, *centre) and _well_separated_edges(
        wire, layers, frame, *centre
    ):
        points.append(
            SamplePoint(
                label=f"{CANVAS_SAMPLE_LABEL}{suffix}.center",
                output_frame=frame,
                canvas_x=centre[0],
                canvas_y=centre[1],
            )
        )
    for layer in layers:
        if not _covers(layer.clip, frame) and str(layer.clip["clip_id"]) not in absent:
            continue
        label_prefix = f"{layer.clip['clip_id']}{suffix}"
        candidates: list[tuple[str, tuple[int, int], int]] = [
            (f"{label_prefix}.field", layer.profile.field_sample, layer.profile.field_span)
        ]
        candidates += [
            (f"{label_prefix}.{patch.label}", patch.center(), patch.size)
            for patch in layer.profile.patches
        ]
        for label, source_point, span in candidates:
            scale = min(layer.placement.scale_x(), layer.placement.scale_y())
            if span * scale < MINIMUM_SAMPLE_SPAN_PX:
                # Too small on the canvas for an interior sample the comparator would accept.
                continue
            canvas = layer.placement.to_canvas(*source_point)
            if canvas is None:
                continue
            x, y = round(canvas[0]), round(canvas[1])
            if not (0 <= x < canvas_width and 0 <= y < canvas_height):
                continue
            if not _flat_composite(wire, layers, frame, x, y):
                # See SAMPLE_POINT_NEEDS_A_FLAT_COMPOSITE. This also covers a point a text layer
                # may have painted: the composite raises there, and the point is dropped rather
                # than compared against a colour derived as if the text were absent. Dropping is
                # safe; the remaining points still discriminate.
                continue
            if not _well_separated_edges(wire, layers, frame, x, y):
                continue  # See SAMPLE_POINT_NEEDS_WELL_SEPARATED_EDGES.
            points.append(SamplePoint(label=label, output_frame=frame, canvas_x=x, canvas_y=y))
    return tuple(points)


def patch_sample_points(wire: Mapping[str, Any]) -> tuple[SamplePoint, ...]:
    """Where a colour comparison samples, for both the expectation and the observers.

    The observers import this so that all three sides measure the same points; the values are still
    measured, never predicted, which is what independence means here.

    The steady-state frame is sampled unsuffixed, so the points a composition without a ramp states
    are exactly the points it has always stated. A composition that declares a ramp states those
    same points and, in addition, the same layers at its ramp's midpoint -- see
    `_ramp_sample_frames` for why a transition is otherwise unobservable here.
    """

    steady = patch_sample_frame(wire)
    frames: dict[int, set[str]] = {steady: set()}
    for frame in (*_ramp_sample_frames(wire), quiet_sample_frame(wire)):
        frames.setdefault(frame, set())
    for frame, clip_ids in _layer_end_frames(wire).items():
        frames.setdefault(frame, set()).update(clip_ids)
    points = list(_points_at_frame(wire, steady, "", absent=frames[steady]))
    for frame in sorted(frames):
        if frame == steady:
            continue
        points.extend(_points_at_frame(wire, frame, f"@{frame}", absent=frames[frame]))
    return tuple(points)


#: Why a layer that is not on the canvas for the whole output is sampled at both of its ends.
#:
#: GUARD: the steady frame is a layer's midpoint and the quiet frame is where the fewest clips are
#: active, and a clip moved by one frame leaves both untouched -- `command.move_clip` closed with
#: its two phases stating the same colours, so the move was never observed. Every visual layer
#: whose span is shorter than the output is therefore also sampled at its first and last frames
#: and at the frame just outside each end, and at those outside frames the layer's own points are
#: still stated, carrying what lies beneath: the layer's absence is then a colour a comparison
#: can hold, and a one-frame move states a different colour at the frame it vacated or took. Do
#: not drop the outside frames, and do not restrict the outside points to layers that happen to
#: be present there.
EVERY_PARTIAL_LAYER_IS_SAMPLED_AT_ITS_ENDS: Final = (
    "a visual layer whose span is shorter than the output is sampled at its first and last "
    "frames and at the frame just outside each end, where its own points state what lies beneath"
)


def _layer_end_frames(wire: Mapping[str, Any]) -> dict[int, set[str]]:
    """The end frames of every partial visual layer, each mapped to the layers stated there even
    when absent -- see EVERY_PARTIAL_LAYER_IS_SAMPLED_AT_ITS_ENDS."""

    duration = _int(_output(wire), "duration_frames")
    ends: dict[int, set[str]] = {}
    for layer in _visual_layers(wire):
        start = _int(layer.clip, "start_frame")
        span = _int(layer.clip, "duration_frames")
        if span <= 0 or (start <= 0 and start + span >= duration):
            continue
        clip_id = str(layer.clip["clip_id"])
        for frame in (start - 1, start, start + span - 1, start + span):
            if 0 <= frame < duration:
                ends.setdefault(frame, set()).add(clip_id)
    return ends


def quiet_sample_frame(wire: Mapping[str, Any]) -> int:
    """The first frame at which the fewest enabled clips are active.

    GUARD: the steady-state frame is chosen so that every layer is present, which is right for a
    blend or an effect and wrong for a picture the title's declared line box covers entirely. On a
    small canvas the band the oracle refuses to describe (`_text_occlusion_bands`) spans every
    row, so a row sampled only where the title is active states no colour at all and would close
    on its container. The quietest frame is where the fewest clips are active, which in this
    corpus is where the title and the overlay have both ended; it is derived from the declared
    spans alone and never from what a render showed.
    """

    duration = _int(_output(wire), "duration_frames")
    clips = _enabled_clips(wire)
    quietest = 0
    fewest: int | None = None
    for frame in range(duration):
        active = sum(1 for clip in clips if _covers(clip, frame))
        if fewest is None or active < fewest:
            quietest, fewest = frame, active
    return quietest


def derive_patch_samples(wire: Mapping[str, Any]) -> tuple[PatchSample, ...]:
    """The colour each declared sample point must show once the composition is composited."""

    return tuple(
        PatchSample(
            label=point.label,
            size_px=TOLERANCE_PROFILE.patch_size_px,
            edge_distance_px=TOLERANCE_PROFILE.patch_min_edge_distance_px,
            red=colour[0],
            green=colour[1],
            blue=colour[2],
        )
        for point in patch_sample_points(wire)
        for colour in (composite_colour(wire, point.output_frame, point.canvas_x, point.canvas_y),)
    )


def _primary_track_ids(wire: Mapping[str, Any]) -> set[object]:
    tracks = wire.get("tracks")
    if not isinstance(tracks, Sequence):
        raise ExpectationError("the composition declares no tracks")
    return {
        track["track_id"]
        for track in tracks
        if isinstance(track, Mapping) and track.get("kind") == PRIMARY_TRACK_KIND
    }


def _audio_clips(wire: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Every primary clip whose embedded bursts the output carries, in timeline order.

    GUARD: all of them, not the first. The declared audio policy resolves an owner per output frame
    among the active primary clips, so a composition with two or three primary clips -- a hard cut,
    a gap, an owner-range trim, a disabled tail -- has more than one owner across its timeline.
    Stating only the first clip's bursts is not a wrong expectation, it is a silent partial one: the
    intervals nobody claims are never judged, so an onset or a signal there passes unexamined.

    A tone source (M25-77) has no bursts to place, so its clips are not among these: its sound is
    stated as window levels (`derive_audio_levels`), and the render stage prescribes no onset scan
    for it either (`scripts/nle_semantic_render.py`'s `_audio_owner_windows`).
    """

    primary = _primary_track_ids(wire)
    owners = [
        clip
        for clip in _enabled_clips(wire)
        if clip.get("track_id") in primary
        and (asset := _asset(wire, clip.get("asset_id"))) is not None
        and asset.get("embedded_audio") in AUDIO_BEARING
        and _int(clip, "duration_frames") > 0
        and not _plays_a_tone(clip)
    ]
    return tuple(sorted(owners, key=lambda clip: (_int(clip, "start_frame"), str(clip["clip_id"]))))


def _plays_a_tone(clip: Mapping[str, Any]) -> bool:
    profile = profile_for(str(clip.get("asset_id")))
    return profile is not None and profile.audio_kind == "tone"


def _samples_per_frame(wire: Mapping[str, Any]) -> Fraction:
    output = _output(wire)
    num, den = _rational(output, "frame_rate")
    return Fraction(_int(output, "sample_rate") * den, num)


@dataclass(frozen=True, slots=True)
class AudioWindow:
    """One audio owner's span on the output timeline, in output samples."""

    clip: Mapping[str, Any]
    start_sample: int
    end_sample: int
    shift_samples: int


def _audio_windows(wire: Mapping[str, Any]) -> tuple[AudioWindow, ...]:
    per_frame = _samples_per_frame(wire)
    sample_rate = _int(_output(wire), "sample_rate")
    windows: list[AudioWindow] = []
    for clip in _audio_clips(wire):
        start = _int(clip, "start_frame")
        span = _int(clip, "duration_frames")
        # The clip's source audio begins at the source *time* its start frame lands on -- the
        # landmark's pts, not the frame count times an output frame -- so a source at another
        # rate (B-58) shifts its bursts by its own clock, as the contract's audio owner does.
        asset = _asset(wire, clip.get("asset_id"))
        start_tick = source_start_tick(wire, clip)
        if asset is None or start_tick is None:
            raise ExpectationError(f"{clip.get('clip_id')} starts at an undeclared source frame")
        base_num, base_den = _rational(asset, "source_time_base")
        source_start_sample = Fraction(start_tick * base_num * sample_rate, base_den)
        windows.append(
            AudioWindow(
                clip=clip,
                start_sample=int(start * per_frame),
                end_sample=int((start + span) * per_frame),
                shift_samples=int(start * per_frame - source_start_sample),
            )
        )
    return tuple(windows)


def derive_audio_onsets(wire: Mapping[str, Any]) -> tuple[AudioOnset, ...]:
    """Where each source audio burst lands on the output timeline.

    The declared policy is that embedded audio follows its video, so a burst's output position is
    its source position shifted by the clip's own mapping of source frames onto output frames. The
    arithmetic is exact: the sample rate is a whole multiple of the frame rate, so no burst is
    placed on a fractional sample and no rounding rule has to be invented.
    """

    return tuple(onset for onset, _ in _placed_bursts(wire))


def _placed_bursts(wire: Mapping[str, Any]) -> tuple[tuple[AudioOnset, int], ...]:
    """Every placed burst with the source sample it comes from, in output order."""

    if not carries_audio(wire):
        return ()
    placed_bursts: list[tuple[AudioOnset, int]] = []
    for owner, window in enumerate(_audio_windows(wire)):
        for index, source_start in enumerate(AUDIO_BURST_STARTS):
            placed = source_start + window.shift_samples
            if placed < window.start_sample or placed >= window.end_sample:
                continue
            # The label carries the owner as well as the burst: two owners can present the same
            # source burst at two output positions, and one label for both would be ambiguous.
            onset = AudioOnset(label=f"owner{owner}_burst_{index}", sample_index=placed)
            placed_bursts.append((onset, source_start))
    return tuple(sorted(placed_bursts, key=lambda item: item[0].sample_index))


def derive_silences(wire: Mapping[str, Any]) -> tuple[SilenceInterval, ...]:
    """The intervals between the placed bursts, where the output must carry no signal.

    `abs_peak` is zero here because an expectation states what should be true, not what was found;
    the comparator holds the *observed* peak to the frozen bound. Only gaps with a real interior
    left after the codec-boundary exclusion are declared -- a gap shorter than that has nothing to
    inspect, and declaring it would produce a check that passes on an empty interval.
    """

    placed_bursts = _placed_bursts(wire)
    windows = _audio_windows(wire)
    if not placed_bursts or not windows:
        return ()
    # The gaps run between consecutive placed bursts and then to the end of the last owner's span,
    # so a composition with several owners still states one continuous chain of silent intervals.
    last = max(window.end_sample for window in windows)
    first_frame = CODEC_FRAME_SAMPLES.get(str(_output(wire).get("audio_codec") or ""), 0)
    boundaries = [
        # GUARD: a burst inside its *source's* first codec frame is spread by that frame's long
        # window: the source encoder has nothing before the attack to switch to short windows on,
        # so the source itself rings past the burst (measured on the fixture sources: above the
        # bound until 2048), and the product's re-encode carries that tail one frame further.
        # Wherever the clip places that burst on the output, the artifact rings above the bound
        # until the burst's *source-relative* first frame end plus the frozen exclusion (measured:
        # 3072 for the burst placed at output sample 0, B-45; the same source-start burst placed
        # at output sample 24000 by a second owner rang to 4 inside an interior that began at
        # 26304, B-61). A burst with run-up in its source is confined wherever it lands. The gap
        # after a source-start burst therefore begins at the owner's source origin on the output
        # plus one codec frame, never at the *stream's* first frame -- the earlier rule keyed on
        # the output position and was right only for the first owner. Never move the gap start
        # for a burst that has run-up, and never widen the exclusion.
        max(onset.sample_index + AUDIO_BURST_SAMPLES, onset.sample_index - source + first_frame)
        if source < first_frame
        else onset.sample_index + AUDIO_BURST_SAMPLES
        for onset, source in placed_bursts
    ]
    ends = [onset.sample_index for onset, _ in placed_bursts[1:]] + [last]
    silences: list[SilenceInterval] = []
    for index, (start, end) in enumerate(zip(boundaries, ends, strict=True)):
        if end - start <= 2 * TOLERANCE_PROFILE.codec_boundary_exclusion_samples:
            continue
        silences.append(
            SilenceInterval(label=f"gap_{index}", start_sample=start, end_sample=end, abs_peak=0)
        )
    return tuple(silences)


# ---------------------------------------------------------------------------------------------
# M25-77: the level a clip's audio adjustments put on each owner run of a tone source
# ---------------------------------------------------------------------------------------------

#: Where a window's level is measured: 960 samples, twenty whole periods of the tone
#: (`semantic_conformance_media.AUDIO_TONE_HZ`), so the tone's own RMS over it is exact.
AUDIO_LEVEL_WINDOW_SAMPLES: Final = 960
#: A run's first window starts this far into the run, and its last ends this far before its end.
AUDIO_LEVEL_EDGE_OFFSET_SAMPLES: Final = 2_560

#: Why a level is stated only well inside an owner run.
#:
#: GUARD: a window lies inside one run of one owner and at least
#: `TOLERANCE_PROFILE.codec_boundary_exclusion_samples` (2,048) samples from both ends of it. An
#: owner switch is a cut in the program's sound and the stream's two ends are edges of it; the AAC
#: frames around either carry both sides' energy, so a window nearer than that measures the codec
#: at the cut, not the level the clip's adjustments define. A window that cannot keep the distance
#: is not stated at all -- never moved, and never stated with a wider bound. The level stated is
#: `sqrt(mean factor(k)^2)` over the window's clip-relative samples, from the one definition
#: (`clip_audio.clip_audio_factor`): the factor is the contract, and the final render's filters are
#: the implementation the rows judge.
AUDIO_LEVEL_STATED_ONLY_INSIDE_AN_OWNER_RUN: Final = (
    "a window's level is stated only inside one owner run of a tone source, at least the codec "
    "boundary exclusion from both of the run's ends, as the root mean square of the defined "
    "factor over the window"
)


@dataclass(frozen=True, slots=True)
class AudioOwnerRun:
    """A maximal run of output frames over which one primary clip is the audio owner."""

    clip: Mapping[str, Any]
    start_frame: int
    end_frame: int


def audio_owner_runs(wire: Mapping[str, Any]) -> tuple[AudioOwnerRun, ...]:
    """Each run of output frames one primary clip owns, in timeline order.

    The contract's own rule, restated from the wire (`composition_contract`'s resolver): at every
    output frame the owner is the active primary clip with the greatest `(start_frame, clip_id)`,
    whether or not its source carries sound. A clip is heard again after another clip's run as a
    second run of its own.
    """

    duration = _int(_output(wire), "duration_frames")
    primary = _primary_track_ids(wire)
    clips = [clip for clip in _enabled_clips(wire) if clip.get("track_id") in primary]
    runs: list[AudioOwnerRun] = []
    owner: Mapping[str, Any] | None = None
    run_start = 0
    for frame in range(duration + 1):
        active = [
            clip
            for clip in clips
            if frame < duration
            and _int(clip, "start_frame")
            <= frame
            < _int(clip, "start_frame") + _int(clip, "duration_frames")
        ]
        current = max(
            active,
            key=lambda clip: (_int(clip, "start_frame"), str(clip["clip_id"])),
            default=None,
        )
        if current is owner:
            continue
        if owner is not None:
            runs.append(AudioOwnerRun(clip=owner, start_frame=run_start, end_frame=frame))
        owner, run_start = current, frame
    return tuple(runs)


def _clip_audio_member(clip: Mapping[str, Any]) -> ClipAudio:
    member = clip.get("audio")
    if member is None:
        return IDENTITY_CLIP_AUDIO
    if not isinstance(member, Mapping) or not isinstance(member.get("muted"), bool):
        raise ExpectationError(f"{clip.get('clip_id')} declares an unreadable audio member")
    return ClipAudio(
        _int(member, "gain_mb"),
        member["muted"],
        _int(member, "fade_in_frames"),
        _int(member, "fade_out_frames"),
    )


@dataclass(frozen=True, slots=True)
class AudioLevelWindow:
    """Where an observer measures one window's level: prescribed, exactly like a `SamplePoint`.

    `start_sample` is on the output timeline; `clip_sample` is the same sample counted from the
    owner clip's own start, where the clip's adjustments are defined.
    """

    label: str
    start_sample: int
    sample_count: int
    clip_id: str
    clip_sample: int


def audio_level_windows(wire: Mapping[str, Any]) -> tuple[AudioLevelWindow, ...]:
    """Every window whose level is stated, in output order.

    For each owner run of a tone source: the run's start and end windows
    (`AUDIO_LEVEL_EDGE_OFFSET_SAMPLES`), its middle, and the quarter points of each fade of the
    owner clip -- each kept only where `AUDIO_LEVEL_STATED_ONLY_INSIDE_AN_OWNER_RUN` admits it.
    Observers import this, so the final extractor measures where the expectation is stated.
    """

    if _samples_per_frame(wire) != SAMPLES_PER_FRAME:
        raise ExpectationError("the clip audio definition is stated at 2,000 samples per frame")
    clearance = TOLERANCE_PROFILE.codec_boundary_exclusion_samples
    size = AUDIO_LEVEL_WINDOW_SAMPLES
    half = size // 2
    windows: list[AudioLevelWindow] = []
    for run in audio_owner_runs(wire):
        clip = run.clip
        asset = _asset(wire, clip.get("asset_id"))
        if asset is None or asset.get("embedded_audio") not in AUDIO_BEARING:
            continue
        if not _plays_a_tone(clip):
            continue
        clip_start = _int(clip, "start_frame") * SAMPLES_PER_FRAME
        length = _int(clip, "duration_frames") * SAMPLES_PER_FRAME
        first = run.start_frame * SAMPLES_PER_FRAME - clip_start
        last = run.end_frame * SAMPLES_PER_FRAME - clip_start
        audio = _clip_audio_member(clip)
        fade_in = audio.fade_in_frames * SAMPLES_PER_FRAME
        fade_out = audio.fade_out_frames * SAMPLES_PER_FRAME
        starts = {
            first + AUDIO_LEVEL_EDGE_OFFSET_SAMPLES,
            (first + last) // 2 - half,
            last - AUDIO_LEVEL_EDGE_OFFSET_SAMPLES - size,
        }
        for share in (1, 2, 3):
            if fade_in:
                starts.add(fade_in * share // 4 - half)
            if fade_out:
                starts.add(length - fade_out + fade_out * share // 4 - half)
        for start in sorted(starts):
            if start < first + clearance or start + size > last - clearance:
                continue
            output_start = clip_start + start
            windows.append(
                AudioLevelWindow(
                    label=f"{clip['clip_id']}@{output_start}",
                    start_sample=output_start,
                    sample_count=size,
                    clip_id=str(clip["clip_id"]),
                    clip_sample=start,
                )
            )
    return tuple(sorted(windows, key=lambda window: window.start_sample))


def derive_audio_levels(wire: Mapping[str, Any]) -> tuple[AudioLevel, ...]:
    """The level each window must measure, in millionths of the tone's own level."""

    clips = {str(clip.get("clip_id")): clip for clip in _enabled_clips(wire)}
    levels: list[AudioLevel] = []
    for window in audio_level_windows(wire):
        clip = clips[window.clip_id]
        audio = _clip_audio_member(clip)
        duration = _int(clip, "duration_frames")
        energy = math.fsum(
            clip_audio_factor(audio, duration, k) ** 2
            for k in range(window.clip_sample, window.clip_sample + window.sample_count)
        )
        levels.append(
            AudioLevel(
                label=window.label,
                start_sample=window.start_sample,
                sample_count=window.sample_count,
                level_ppm=round(math.sqrt(energy / window.sample_count) * 1_000_000),
            )
        )
    return tuple(levels)


def _output(wire: Mapping[str, Any]) -> Mapping[str, Any]:
    output = wire.get("output")
    if not isinstance(output, Mapping):
        raise ExpectationError("the composition declares no output profile")
    return output


def derive_expectation(recipe: CaseRecipe, wire: Mapping[str, Any]) -> Expectation:
    """State what one row expects, from the composition its recipe produced.

    A refusing row expects exactly its declared code and no artifact; the comparator enforces the
    second half, which is the part a code-only assertion cannot see. A rendering row expects the
    artifact-level facts the contract fixes. Nothing here reads a receipt, a render plan or an
    observation.
    """

    if recipe.refusal_code is not None:
        return Expectation(case_id=recipe.case_id, refusal_code=recipe.refusal_code)
    required = set(required_landmarks(recipe, wire))
    return Expectation(
        case_id=recipe.case_id,
        frame_grid=derive_frame_grid(wire),
        frame_timing=derive_frame_timing(wire),
        output_metadata=derive_output_metadata(wire),
        audio_extent_samples=derive_audio_extent_samples(wire),
        source_mapping=derive_source_mapping(wire),
        geometry=derive_geometry(wire) if "geometry" in required else (),
        patches=derive_patch_samples(wire) if "patches" in required else (),
        color_patches=derive_patch_samples(wire) if "color_patches" in required else (),
        alphas=derive_alphas(wire) if "alphas" in required else (),
        text=derive_text(wire) if "text" in required else None,
        audio_onsets=derive_audio_onsets(wire) if "audio_onsets" in required else (),
        silences=derive_silences(wire) if "silences" in required else (),
        audio_levels=derive_audio_levels(wire) if "audio_levels" in required else (),
    )


# ---------------------------------------------------------------------------------------------
# What each row must actually observe
# ---------------------------------------------------------------------------------------------

#: Landmarks every rendering row must carry, whatever it is about. They are the artifact-level
#: facts the output profile fixes plus the source identity of the picture: an artifact with the
#: right container, size and duration can still be the wrong frames, or black.
UNIVERSAL_LANDMARKS: Final = ("frame_grid", "frame_timing", "output_metadata", "source_mapping")

#: What each corpus family must observe *in addition*, because that is what the family is about.
#: A row whose subject is opacity and which compares only its container has not been tested; this
#: table is what stops the join from calling such a row conformant.
#:
#: GUARD: never widen an entry to make a row pass. The entries are read from the plan's section 14.3
#: comparison list -- source-frame identity, geometry, patch/blend/effect colour, transition alpha,
#: glyph/text resolution, and the embedded-audio onsets, silences and extent -- and a row that
#: cannot produce its landmarks is `BLOCKED`, which is a true statement, rather than passing on the
#: landmarks it happened to have.
FAMILY_LANDMARKS: Final[dict[str, tuple[str, ...]]] = {
    # Geometry alone cannot separate a left crop from a right one, or an interior scale from a
    # clamped one: both leave the same visible rectangle on the canvas. The colour samples move
    # with the source region, so they are what tells those boundaries apart.
    "transform": ("geometry", "patches"),
    "crop": ("geometry", "patches"),
    "opacity_blend": ("patches", "alphas"),
    "effect": ("color_patches",),
    "transition": ("alphas",),
    "text": ("text",),
    "timing": (),
    "output": (),
    # An edge trim moves where the primary's audio starts and ends, and the source's own bursts are
    # the only landmark fine-grained enough to see a one-frame move: the source declares three frame
    # landmarks, so `source_mapping` alone leaves fifteen trim rows stating the same thing.
    "edge_trim": ("audio_onsets", "silences"),
    "embedded_audio": ("audio_onsets", "silences", "audio_extent_samples"),
    # A history position is only observable through what the composition at that position renders.
    # Selection and locking are invisible by construction; the rest of these rows reach their
    # position by changing an opacity or an effect, which the composited colour samples state.
    # AC45-05: a high-resolution row leaves exactly one visual layer enabled and asks what that
    # layer looks like at 1280 x 720. Its rectangle and its composited colour are the whole claim,
    # and the one row whose layer is the title states the title as well -- see the conditional
    # substitution in `required_landmarks`, which is scoped to this family alone.
    "high_resolution": ("geometry", "patches", "text"),
    "history_currentness": ("patches",),
    "command": (),
    "import_integration": (),
    # M25-77: what a clip's gain, mute and fades change is the level of its sound, window by window.
    "clip_audio": ("audio_levels",),
}

#: What an accepted command must additionally make observable, keyed by the command it runs.
#:
#: GUARD: every command row belongs to the one corpus family `command`, so the family table above
#: says nothing about what any particular command changed. Without this table an accepted command
#: row would be judged on its container alone -- and the container is exactly what a command like
#: `set_opacity_blend` or `set_text_content` does not change, so the row would close while its
#: entire subject went unobserved. Add a command here whenever it has a rendered consequence, and
#: leave it out only when the command genuinely changes nothing an artifact can show -- selection
#: and locking, for instance.
#:
#: GUARD: a command that changes *which clip is present at some frame*, or *what a present clip
#: shows*, states `patches`: the composited colour at every layer's sample points, including the
#: ends of every partial layer (EVERY_PARTIAL_LAYER_IS_SAMPLED_AT_ITS_ENDS). `source_mapping`
#: alone did not see these -- `set_track_enabled` switched the overlay track off and the row
#: closed, because the identity row is the primary's and the overlay's absence changes nothing the
#: identity states. The commands absent from this table with a rendered consequence of their own
#: are pinned, with the reason each cannot move the picture, in
#: `tests/test_m25_20_conformance_cases.py::COMMANDS_INVISIBLE_BY_CONSTRUCTION`.
COMMAND_LANDMARKS: Final[dict[str, tuple[str, ...]]] = {
    "set_track_enabled": ("patches",),
    "insert_asset_clip": ("patches",),
    "replace_clip_asset": ("patches",),
    "remove_clip": ("patches",),
    "move_clip": ("patches",),
    "move_group": ("patches",),
    "trim_clip": ("patches",),
    "insert_range": ("patches",),
    "overwrite_range": ("patches",),
    "ripple_delete": ("patches",),
    "ripple_trim": ("patches",),
    "slip_clip": ("patches",),
    "set_clip_enabled": ("patches",),
    "set_visual_transform": ("geometry",),
    "set_crop": ("geometry",),
    "set_opacity_blend": ("alphas",),
    "set_transition": ("alphas",),
    "set_effect": ("color_patches",),
    # What a clip's gain, mute and fades change is the level of its sound, window by window.
    "set_clip_audio": ("audio_levels",),
    "set_text_content": ("text",),
    "set_text_style": ("text",),
    "insert_title_clip": ("text",),
}

#: The product's own bounds on the preview surface (`frontend/src/runtime/visualCompositor.ts`,
#: `computePreviewSize`). Before M25-45 there was one bound: the output scaled so that neither
#: side exceeded 320 px and the area did not exceed 102,400 px, never scaled up. M25-45 keeps that
#: as the floor -- an unmeasured picture box still presents exactly it -- and adds the ceiling its
#: measured matrix froze. Mirrored here, from the same four numbers, so the join can say from the
#: declared output and the prescribed picture box alone what surface the preview presents.
#:
#: GUARD: these four numbers and the rule below are one contract with the TypeScript native path,
#: and `tests/test_m25_45_preview_size_parity.py` holds them together. Do not copy the lower
#: software-rung ceiling here: this mirror decides whether native browser landmarks are observable.
#: A drift silently labels a row "not browser observable" and drops the checks that would catch a
#: native-resolution regression.
PREVIEW_LEGACY_MAX_SIDE_PX: Final = 320
PREVIEW_LEGACY_MAX_PIXELS: Final = 102_400
PREVIEW_MAX_SIDE_PX: Final = 1_920
PREVIEW_MAX_PIXELS: Final = 2_073_600


def _scale_within(width: int, height: int, max_side: int, max_pixels: int) -> float:
    return min(
        1.0,
        max_side / width,
        max_side / height,
        math.sqrt(max_pixels / (width * height)),
    )


def preview_scale(
    wire: Mapping[str, Any],
    *,
    pane_css_width: float = 0.0,
    pane_css_height: float = 0.0,
    device_pixel_ratio: float = 1.0,
) -> float:
    """The factor the product scales the output by to present it on the monitor canvas.

    Without a prescribed picture box this is the pre-M25-45 bound, which is also the product's
    floor, so every row whose output the legacy surface already held presents unchanged.
    """

    output = _output(wire)
    width, height = _int(output, "width"), _int(output, "height")
    floor = _scale_within(width, height, PREVIEW_LEGACY_MAX_SIDE_PX, PREVIEW_LEGACY_MAX_PIXELS)
    ceiling = _scale_within(width, height, PREVIEW_MAX_SIDE_PX, PREVIEW_MAX_PIXELS)
    measured = (
        math.isfinite(pane_css_width)
        and math.isfinite(pane_css_height)
        and pane_css_width > 0
        and pane_css_height > 0
        and math.isfinite(device_pixel_ratio)
        and device_pixel_ratio > 0
    )
    requested = (
        min(
            pane_css_width * device_pixel_ratio / width,
            pane_css_height * device_pixel_ratio / height,
        )
        if measured
        else 0.0
    )
    return min(ceiling, max(floor, min(1.0, requested)))


def preview_size(
    wire: Mapping[str, Any],
    *,
    pane_css_width: float = 0.0,
    pane_css_height: float = 0.0,
    device_pixel_ratio: float = 1.0,
) -> tuple[int, int]:
    """The backing store the product creates for that scale, quantized as the product quantizes."""

    output = _output(wire)
    width, height = _int(output, "width"), _int(output, "height")
    scale = preview_scale(
        wire,
        pane_css_width=pane_css_width,
        pane_css_height=pane_css_height,
        device_pixel_ratio=device_pixel_ratio,
    )

    def even(value: float, limit: int) -> int:
        # `Math.round` rounds a half away from zero towards positive infinity; Python's `round`
        # rounds a half to even. The product's quantization is the JavaScript one.
        return max(2, min(limit, 2 * math.floor(value / 2 + 0.5)))

    return even(width * scale, width), even(height * scale, height)


#: Why the browser side of a row whose preview is scaled down observes no landmark at all.
#:
#: GUARD: every browser landmark is a claim at output precision -- a rectangle to two output
#: pixels, a 5x5 interior mean of a flat region eleven output pixels wide, an identity cell six
#: output pixels wide -- and the preview of a 1920x1080 output is 320x180: a sixth of that. Two
#: output pixels are a third of a preview pixel, the flat region is two preview pixels wide and
#: an identity cell is one. Nothing on that surface can carry those claims, and measuring them
#: anyway would report the resampler, not the product. So the browser side of such a row presents
#: the composition (its fingerprint is still confirmed) and observes nothing, the join compares the
#: final artifact alone, and the row's report says so. The rule is the product's declared preview
#: bound above, not a row list: a new row with an output the preview cannot hold at scale falls on
#: the same side of it. Never "observe" a scaled preview by dividing the bounds by the scale.
BROWSER_OBSERVES_ONLY_AN_UNSCALED_PREVIEW: Final = (
    "the browser side observes a row's landmarks only when the product presents the output at "
    "its own scale; a preview the product scales down cannot carry a two-pixel rectangle, an "
    "interior colour sample or an identity cell, and such a row is judged on its final artifact "
    "alone"
)


def browser_observable(wire: Mapping[str, Any], base: str = CORPUS_BASE) -> bool:
    """Whether the preview presents this composition at output scale, as its base is presented.

    GUARD: the base is a parameter, not a default to leave alone. A composition larger than the
    legacy floor is presented at output scale only in the picture box and device pixel ratio its
    base declares (`BASE_PRESENTATION`); ask without them and the answer is "scaled down" for every
    high-resolution row, which silently removes the browser side of exactly the rows that exist to
    be observed at full size.
    """

    return preview_scale(wire, **BASE_PRESENTATION[base]) == 1.0


#: The landmarks the browser side is expected to present. The independent extractor decodes an
#: artifact and the browser presents a canvas, so neither can answer for the other.
#:
#: GUARD: narrowing this tuple is exactly how a fail-closed check becomes vacuous again, so a
#: landmark leaves it only by moving to `BROWSER_UNOBSERVABLE` with a reason naming the surface that
#: cannot produce it, and the final side goes on requiring it either way. Never drop a landmark here
#: because a collector found it hard; drop it only when the preview surface genuinely has no honest
#: source for it, and say which surface.
BROWSER_LANDMARKS: Final = (
    "source_mapping",
    "geometry",
    "patches",
    "color_patches",
    # A ramp's progress is the projection of the sampled pixel's travel onto its full travel
    # between the ramp's two reference frames (`alpha_targets`), which needs no isolated layer
    # and no blend arithmetic: the browser journey measures it on the canvas exactly as the
    # extractor measures it on the artifact.
    "alphas",
)

#: Why each remaining required landmark has no browser observation. Every landmark a family or a
#: command requires is in exactly one of these two, and a reason is a statement about the product
#: surface that can be checked against the code, not a note that the work was not done.
BROWSER_UNOBSERVABLE: Final = {
    "frame_grid": (
        "a container fact. The preview presents a canvas, which has no frame count, and the grid "
        "exists only in the encoded artifact the extractor decodes."
    ),
    "frame_timing": (
        "a container fact. Presentation timestamps, decode timestamps and durations are the "
        "artifact's, and the canvas carries no timeline of its own to read them from."
    ),
    "output_metadata": (
        "a container fact. Codec, pixel format, colour signalling and stream layout are decided at "
        "encode time and are not represented anywhere in the preview."
    ),
    "audio_extent_samples": (
        "a container fact, and unobservable for the same reason as the onsets below: the sample "
        "count of an output the preview never encodes."
    ),
    "text": (
        "`frontend/src/runtime/visualCompositor.ts` draws text with a single `context.fillText` "
        "onto the canvas and mirrors nothing about it in the DOM. The inspector's clip form does "
        "mirror `content` and `weight`, but `font_identity`, `style`, `align` and per-line "
        "geometry have no rendered control anywhere, and the comparator forbids `content` by OCR "
        "or pixel similarity -- so three of a `TextEntry`'s six fields would have to be invented."
    ),
    "audio_onsets": (
        "this stage observes the canvas, which carries no sound. The preview does play sound -- "
        "`frontend/src/runtime/embeddedAudioFollower.ts` plays each owner's decoded audio through "
        "a buffer source and a gain node in its own `AudioContext`, with every media element "
        "muted -- but nothing in `frontend/src` taps that graph (no `AnalyserNode` and no other "
        "reader), so the page exposes no sample of what it plays, and there is no instantaneous "
        "seek-and-sample gesture for sound the way `transport.seek` yields a video frame. What "
        "the preview plays is captured from the device by the audio observer journeys instead."
    ),
    "silences": (
        "the absence of sound is measured by the same means as its onsets, which the preview does "
        "not have."
    ),
    "audio_levels": (
        "a level is a measurement of sound, and the reason stated for the onsets applies "
        "unchanged: the page exposes no sample of what the preview plays. The decoded artifact "
        "states every window."
    ),
}


def required_landmarks(recipe: CaseRecipe, wire: Mapping[str, Any]) -> tuple[str, ...]:
    """Every landmark this row has to observe before it may be judged.

    A refusing row observes none: its whole claim is the refusal code and the absence of an
    artifact, and requiring a picture of something that was refused would be incoherent.

    The composition is a parameter because one requirement depends on it. `alphas` is only a
    landmark where the composition declares an active cross-dissolve -- see
    `ALPHA_MEASURABLE_ONLY_WITH_A_TRANSITION` -- so a composition with no ramp anywhere observes
    `patches` in its place. That substitution is a requirement, never a removal: the row still has
    to state what its family is about, in the terms the composition actually makes measurable.
    """

    if recipe.refusal_code is not None or not recipe.renders:
        return ()
    family = FAMILY_LANDMARKS.get(recipe.family)
    if family is None:
        raise ExpectationError(f"{recipe.family} declares no required observations")
    landmarks = UNIVERSAL_LANDMARKS + family
    if recipe.command is not None:
        landmarks += tuple(
            item for item in COMMAND_LANDMARKS.get(recipe.command.kind, ()) if item not in landmarks
        )
    # GUARD: substitute, never drop. A transition row whose whole subject is that no ramp exists
    # still has to observe the composited colour at the frames a ramp would have moved, or it
    # would close on its container alone -- which is the vacuity this corpus exists to refuse.
    # `patches` is what those frames actually show. The same substitution answers a ramp no point
    # can carry (`ALPHA_MEASURED_WHERE_THE_RAMP_IS_LOUDEST`), a composition whose primary
    # identity is legible at no frame at all (`IDENTITY_STATED_ONLY_WHERE_LEGIBLE`), and a
    # composition with no rectangle to observe: a boundary row that pushes the primary off the
    # canvas proves the blank picture it declares through the colour of what remains, never
    # through an identity nobody can read or a rectangle nobody can see.
    unobservable: set[str] = set()
    if "alphas" in landmarks and not derive_alphas(wire):
        unobservable.add("alphas")
    if "source_mapping" in landmarks and not derive_source_mapping(wire):
        unobservable.add("source_mapping")
    if "geometry" in landmarks and not derive_geometry(wire):
        # A position bound that pushes the whole layer off the canvas, or a scale bound that
        # collapses it below a pixel, leaves no rectangle (see `derive_geometry`); the colour of
        # what remains is what proves the layer really went.
        unobservable.add("geometry")
    # GUARD: scoped to this one family on purpose. The four high-resolution rows each enable a
    # different single layer, so exactly one of them contains the title and the other three cannot
    # observe text that is not there. The `text` family's own rows are *about* the title, and a
    # general "substitute an absent text" rule would let one of those close on colour alone -- the
    # precise vacuity this corpus exists to refuse. Widen this only with a family named here.
    if recipe.family == "high_resolution" and "text" in landmarks and derive_text(wire) is None:
        unobservable.add("text")
    if unobservable:
        substituted: list[str] = []
        for item in landmarks:
            replacement = "patches" if item in unobservable else item
            if replacement not in substituted:
                substituted.append(replacement)
        landmarks = tuple(substituted)
    return landmarks


def missing_landmarks(
    recipe: CaseRecipe, observation: object, wire: Mapping[str, Any], *, side: str
) -> tuple[str, ...]:
    """Name the required landmarks this observation does not carry.

    An empty tuple or `None` is *absent*, not "measured as empty": a collector that returned
    nothing and a scene that genuinely contains nothing are indistinguishable at this boundary, so
    the row blocks and says which landmark is missing rather than passing on the ambiguity.
    """

    absent: list[str] = []
    if side == "browser" and not browser_observable(wire, recipe.base):
        return ()  # See BROWSER_OBSERVES_ONLY_AN_UNSCALED_PREVIEW.
    for landmark in required_landmarks(recipe, wire):
        if side == "browser" and landmark not in BROWSER_LANDMARKS:
            continue
        value = getattr(observation, landmark, None)
        if value is None or (isinstance(value, tuple) and not value):
            absent.append(f"{side}.{landmark}")
    return tuple(absent)
