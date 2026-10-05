"""The synthetic media the semantic conformance corpus is measured against.

Three stages have to agree about the picture. The render stage builds these sources; the independent
extractor reads landmarks back out of the artifact rendered from them; and the join derives what
those landmarks should have been. If each held its own copy of the numbers, a corpus row could be
closed by three consistent mistakes, so the numbers live here once and every stage reads them.

Nothing here is a rendering rule or a tolerance. It is a description of a picture and a sound: what
a source frame looks like, how a frame states its own index, where the colour patches are, and where
the audio bursts fall. What counts as agreement stays in ``TOLERANCE_PROFILE``; what a composition
does to these marks stays in ``semantic_conformance_expect``.

The layout is not free. The corpus fixture composites three visual layers on one 320x180 canvas --
a full-size primary, a half-size video overlay offset to the upper right that screens over it for
twelve frames, and a full-size still that multiplies over everything at 30% for the whole duration
-- so a mark is only readable where the layers above it leave it alone:

* the frame-identity row sits in the flat band between the primary's two patch rows, at source
  x 29..97, y 52..57, which maps to canvas x 125..193, y 78..83 at the fixture's placement. It
  is placed there, rather than in a corner, so that the corpus's own edits leave it in the
  picture: a 1500-basis-point crop removes 19 source pixels from whichever edge it names, the
  interior transform rows shift the layer by tens of canvas pixels, and a corner row survived
  none of them. Where exactly it sits is fixed by three things at once. A row of cells is a run
  of transitions a few pixels apart, and a colour sample is stated only where no such pair lies
  within one coding block of its box
  (``semantic_conformance_expect.SAMPLE_POINT_NEEDS_WELL_SEPARATED_EDGES``), so the row starts
  right of the upper-left patch's sample columns and ends left of the upper-right's, and keeps
  27 px from the lower patches. It stays above the source's centre row, because the smallest
  admitted canvas shows only the source's central sixteen pixels and has to keep a flat point
  there. And it stays 7 px above the centre row so that, at the fixture's placement, its cells
  sit 13 px above the widest line box the text rows declare at the title's identity size, whose
  declared band (``semantic_conformance_expect._text_occlusion_bands``) no identity can be read
  through. Its last cell reaches under
  the video overlay's canvas footprint (x 185.6..249.6, y 36.4..100.4) at the fixture's own
  placement; through a ``screen`` blend the identity stays legible there -- a black cell stays
  dark and a white one stays white -- and ``semantic_conformance_expect.derive_source_mapping``
  states the identity only at the frames where the declared composite leaves every cell legible.
* each field sample sits in the flat band *above* the upper patches, which is the widest flat
  region that stays clear of the identity row, of every patch and of the still's mark by more
  than a coding block, and the sample's declared span is what survives a half-scale placement.
* the still is a white field, because ``multiply`` by white is the identity: the still is a real
  layer that really composites, and it still leaves every mark beneath it legible. Its one mark
  sits high in the centre column, over nothing but the primary's flat field, clear of the
  primary's identity row and field sample by more than a coding block.
* the identity cells are achromatic (pure black and pure white) on purpose: 4:2:0 encoding shares
  one chroma sample between neighbouring rows, and an achromatic cell near a coloured patch row
  borrows nothing from it that a luma threshold could misread.

GUARD: every constant here is load-bearing for a comparison somewhere, and several are fixed by the
frozen tolerance profile rather than chosen freely -- a patch must stay large enough for a 5x5
interior sample taken at least 4 px from its edge, and an identity cell must stay large enough to
survive 4:2:0 subsampling and a lossy encode. Changing a number here silently changes what every
rendering row asserts, and moving a mark can push it under a layer that was not covering it before.
Change the media, the expectations and the layout reasoning together, or not at all.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "AUDIO_BURST_SAMPLES",
    "AUDIO_BURST_STARTS",
    "AUDIO_KINDS",
    "AUDIO_SAMPLE_RATE",
    "AUDIO_TONE_HZ",
    "AUDIO_TONE_PEAK",
    "FRAME_ID_BITS",
    "FRAME_ID_CELL_PITCH_PX",
    "FRAME_ID_MARK_PX",
    "FRAME_ID_ONE_RGB",
    "FRAME_ID_ORIGIN_PX",
    "FRAME_ID_ZERO_RGB",
    "GEOMETRY_LUMA_THRESHOLD",
    "HIGH_RES_IMAGE_ASSET",
    "HIGH_RES_OVERLAY_ASSET",
    "HIGH_RES_PRIMARY_ASSET",
    "HIGH_RES_SCALE",
    "IMAGE_ASSET",
    "IMPORTED_SOURCE_FRAME_COUNT",
    "IMPORTED_SOURCE_PROFILE",
    "OVERLAY_ASSET",
    "OUTPUT_FRAME_TICKS",
    "PATCH_SIZE_PX",
    "PRIMARY_ASSET",
    "SOURCE_HEIGHT",
    "SOURCE_PROFILES",
    "SOURCE_TIME_BASE_DEN",
    "SOURCE_WIDTH",
    "TIMING_ASSET",
    "TIMING_VFR_START_FRAME",
    "TONE_ASSET",
    "TONE_SOURCE_FRAME_COUNT",
    "MediaPatch",
    "SourceProfile",
    "frame_id_bits",
    "imported_source",
    "imported_source_landmarks_match",
    "profile_for",
    "scaled",
    "source_colour",
]

#: Every synthetic source in the corpus is this size. It is a layer's size at 100% scale, because
#: the render graph scales a layer by its own `scale_*_bp` and never fits it to the output canvas,
#: so the geometry a transform row asserts is derived from these two numbers and the transform.
#: The plan's section 14.2 names 128x128 as the default synthetic case size.
SOURCE_WIDTH: Final = 128
SOURCE_HEIGHT: Final = 128

#: A pixel counts as part of a placed layer when its luma exceeds this. Every field below sits far
#: enough above it that 4:2:0 encoding and bilinear scaling cannot push an interior pixel under.
GEOMETRY_LUMA_THRESHOLD: Final = 24

#: How a source frame states which frame it is: a row of high-contrast cells encoding the source
#: frame index in binary, most significant bit first. Eight bits carry 0..255, which covers every
#: synthetic source in the corpus with room to spare. The row sits in the flat band between the two
#: patch rows, above the source's centre row and 10 px inside the 19 px a 1500-basis-point crop
#: removes from the left -- see the module docstring for why a corner placement was abandoned and
#: why the row keeps a coding block's distance from every colour sample.
FRAME_ID_BITS: Final = 8
FRAME_ID_ORIGIN_PX: Final = (29, 52)
FRAME_ID_CELL_PITCH_PX: Final = 9
FRAME_ID_MARK_PX: Final = 6
FRAME_ID_ONE_RGB: Final = (255, 255, 255)
FRAME_ID_ZERO_RGB: Final = (0, 0, 0)

#: A patch is 16 px so that the 5x5 interior sample the comparator insists on can be taken at least
#: 4 px from every edge (4 + 5 + 4 = 13 is the floor). Do not shrink it to fit a smaller source.
#: This is the default; a patch that has to survive a half-scale placement declares a larger
#: `MediaPatch.size` of its own (see the overlay profile).
PATCH_SIZE_PX: Final = 16

PRIMARY_ASSET: Final = "vid-primary"
OVERLAY_ASSET: Final = "vid-overlay"
IMAGE_ASSET: Final = "img-overlay"
#: The timing source: the primary's picture and sound, at a different frame rate for its first
#: three seconds and at unequal frame intervals for its last two. A clip that swaps the primary
#: for it is how the corpus's `timing.source_rate_different` and `timing.vfr_unequal_intervals`
#: rows are the case they name -- a source whose landmark table the product itself measures from
#: a real file, never a sparse table written onto the primary that no probe could ever produce
#: (B-30, B-58). One source rather than two because a composition carries at most three video
#: assets (`registry.MAX_REFERENCE_VIDEOS`), and the fixture already composites two.
TIMING_ASSET: Final = "vid-timing"
#: The tone source (M25-77): the primary's picture with its own patch labels and, in place of the
#: bursts, one steady tone for its whole length. A clip's audio adjustments scale a level, and a
#: burst-and-silence source has a level for 256 samples in every 48,000, so the `clip_audio` rows
#: play this source instead. It is the only source of the `clip_audio` base, which keeps the
#: corpus base and its 310 accepted rows exactly as they were.
TONE_ASSET: Final = "vid-tone"
#: 248 frames: the longest fade the contract admits is 240 frames, and a clip as long as its source
#: carries that fade with eight frames to spare -- enough for a fade-in and a fade-out whose sum
#: exceeds the clip (200 + 100) to be refused by the sum rule and by nothing else. It also stays
#: inside the eight-bit frame identity (`FRAME_ID_BITS` states 0..255).
TONE_SOURCE_FRAME_COUNT: Final = 248

#: What a source's embedded audio is, when it carries any: `bursts`, the onset-and-silence pattern
#: below (`AUDIO_BURST_STARTS`), or `tone`, a steady tone at `AUDIO_TONE_PEAK` whose level is what
#: a clip's audio adjustments change.
AUDIO_KINDS: Final = ("bursts", "tone")

#: Every video source's declared `source_time_base` is 1/12288 (the corpus fixture's, and the
#: container track timescale the render stage encodes at): 12288 = 24 * 512, so a 24 fps frame is
#: exactly 512 ticks and every other rate or interval the timing sources declare is an exact
#: integer of ticks too. The output grid is 24 fps by contract, so one output frame is 512 ticks
#: of source time.
SOURCE_TIME_BASE_DEN: Final = 12_288
OUTPUT_FRAME_TICKS: Final = 512


@dataclass(frozen=True, slots=True)
class MediaPatch:
    """One flat colour square drawn at a fixed place in every frame of one source."""

    label: str
    left: int
    top: int
    red: int
    green: int
    blue: int
    #: The square's side in source pixels. `PATCH_SIZE_PX` unless the patch must still carry an
    #: interior sample after the layer is scaled down.
    size: int = PATCH_SIZE_PX

    def rgb(self) -> tuple[int, int, int]:
        return (self.red, self.green, self.blue)

    def center(self) -> tuple[int, int]:
        half = self.size // 2
        return (self.left + half, self.top + half)


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """What one corpus source looks like: its flat field, its marks, and how it identifies
    itself."""

    asset_id: str
    field: tuple[int, int, int]
    patches: tuple[MediaPatch, ...]
    #: A source pixel guaranteed to be flat field: no identity cell, no patch, no edge. It is what a
    #: colour comparison samples when the layer's own patches are too small to sample after scaling
    #: -- a half-scale overlay's 16 px patch is 8 px on the canvas, and the comparator insists on a
    #: 5x5 interior sample taken at least 4 px from an edge, which needs 13. Never move it onto a
    #: mark, and never move it within 4 px of the frame's edge.
    field_sample: tuple[int, int]
    #: How many source pixels of uniform field surround that point, as a box side. It is what
    #: decides whether the point survives being scaled down: the overlay is placed at half scale,
    #: so only a flat region of at least 26 source pixels leaves a samplable 13 on the canvas.
    field_span: int
    carries_frame_id: bool
    carries_audio: bool
    #: Whether this source reaches the canvas through the browser's video decoder.
    #:
    #: GUARD (B-M2545-17): stated on the profile, never enumerated elsewhere. It decides whether
    #: the browser expectation applies `BROWSER_VIDEO_DECODE_CB_DEFICIT` -- Chromium's BT.709 blue
    #: channel falls short of the standard, by nine code values on the primary's blue patch -- and
    #: a list of asset ids kept somewhere else was missing the high-resolution sources, so their
    #: rows were held to the declared colour the presentation surface does not produce. A derived
    #: source inherits this through `scaled()`, which is the whole reason it lives here.
    decoded_by_browser: bool
    frame_count: int
    #: How many real source pixels one described pixel occupies, as a whole multiplier on every
    #: coordinate this profile states -- patches, the field sample and its span, and the identity
    #: row's origin, pitch and cell size. `1` is the corpus's original 128-square source and is
    #: what every row written before M25-45 uses; the high-resolution rows declare `4`, which is a
    #: 512-square placed 1:1 and centred in a 1280 x 720 composition -- exactly the base corpus's
    #: geometry at four times the size, since 320 x 180 times four is 1280 x 720 -- so their
    #: landmarks carry real detail at the backing they are sampled at rather than an upscaled
    #: 128-pixel picture.
    #:
    #: GUARD: a scaled profile is built by `scaled()`, never by hand. Multiplying some coordinates
    #: and not others moves a mark under a layer that was not covering it, which the layout
    #: reasoning in this module's docstring is what protects against.
    scale: int = 1
    #: How long the source's frames last, in ticks of `SOURCE_TIME_BASE_DEN`, as segments of
    #: `(frame count, repeating cycle of durations)`; empty means every frame lasts one output
    #: frame (`OUTPUT_FRAME_TICKS`, 24 fps). This is the timing the render stage encodes into the
    #: file and the product then measures back as the asset's landmark table, so `pts_table()` is
    #: what the corpus base declares for the source.
    timing_segments: tuple[tuple[int, tuple[int, ...]], ...] = ()
    #: What the embedded audio is when `carries_audio`; one of `AUDIO_KINDS`. The render stage
    #: synthesizes it and the extractor measures it accordingly: onsets and silences for bursts,
    #: window levels for a tone.
    audio_kind: str = "bursts"

    def __post_init__(self) -> None:
        if self.audio_kind not in AUDIO_KINDS:
            raise ValueError("a source declares one of the known kinds of sound")

    @property
    def width(self) -> int:
        return SOURCE_WIDTH * self.scale

    @property
    def height(self) -> int:
        return SOURCE_HEIGHT * self.scale

    @property
    def frame_id_origin_px(self) -> tuple[int, int]:
        return (FRAME_ID_ORIGIN_PX[0] * self.scale, FRAME_ID_ORIGIN_PX[1] * self.scale)

    @property
    def frame_id_cell_pitch_px(self) -> int:
        return FRAME_ID_CELL_PITCH_PX * self.scale

    @property
    def frame_id_mark_px(self) -> int:
        return FRAME_ID_MARK_PX * self.scale

    def durations(self) -> tuple[int, ...]:
        if not self.timing_segments:
            return (OUTPUT_FRAME_TICKS,) * self.frame_count
        durations: list[int] = []
        for count, cycle in self.timing_segments:
            durations.extend(cycle[index % len(cycle)] for index in range(count))
        if len(durations) != self.frame_count:
            raise ValueError("a source declares one duration per frame")
        return tuple(durations)

    def pts_table(self) -> tuple[tuple[int, int, int], ...]:
        """`(frame_index, pts, duration_ticks)` for every frame, from the declared durations."""

        rows: list[tuple[int, int, int]] = []
        pts = 0
        for index, duration in enumerate(self.durations()):
            rows.append((index, pts, duration))
            pts += duration
        return tuple(rows)

    def constant_rate(self) -> int | None:
        """The frame rate when every frame lasts the same whole number of ticks, else `None`."""

        durations = set(self.durations())
        if len(durations) != 1:
            return None
        (duration,) = durations
        return SOURCE_TIME_BASE_DEN // duration if SOURCE_TIME_BASE_DEN % duration == 0 else None


def scaled(profile: SourceProfile, factor: int, asset_id: str) -> SourceProfile:
    """The same picture at `factor` times the size, with every stated coordinate multiplied.

    The whole point of a scaled source is that nothing is stretched later: the high-resolution
    rows place a 512-square 1:1 and centred in a 1280 x 720 composition, which is the base corpus's
    own geometry at four times the size, so a landmark sampled at that backing is real detail
    rather than an upscaled 128-pixel picture.

    GUARD: multiply everything or nothing. The layout reasoning in this module's docstring is a set
    of clearances -- a mark stays a coding block away from a colour sample, a patch stays large
    enough for a 5x5 interior sample taken 4 px from its edge, the identity row stays out from
    under the layers above it -- and every one of them is stated in source pixels. Scaling a patch
    but not the identity row, or the field sample but not its span, silently moves a mark under a
    layer that was not covering it and turns a real conformance failure into a tolerance argument.
    """

    if factor < 1:
        raise ValueError("a source scale is a whole multiplier of at least one")
    if profile.scale != 1:
        raise ValueError("scale a source from its declared form, never from a scaled one")
    return SourceProfile(
        asset_id=asset_id,
        field=profile.field,
        patches=tuple(
            MediaPatch(
                patch.label,
                patch.left * factor,
                patch.top * factor,
                patch.red,
                patch.green,
                patch.blue,
                size=patch.size * factor,
            )
            for patch in profile.patches
        ),
        field_sample=(profile.field_sample[0] * factor, profile.field_sample[1] * factor),
        field_span=profile.field_span * factor,
        carries_frame_id=profile.carries_frame_id,
        carries_audio=profile.carries_audio,
        decoded_by_browser=profile.decoded_by_browser,
        frame_count=profile.frame_count,
        timing_segments=profile.timing_segments,
        scale=factor,
        audio_kind=profile.audio_kind,
    )


def _timing_variant(
    asset_id: str,
    label_prefix: str,
    segments: tuple[tuple[int, tuple[int, ...]], ...],
    *,
    audio_kind: str = "bursts",
) -> SourceProfile:
    """The primary's picture, with its own timing, its own patch labels and the given sound."""

    primary = _COMPOSITED_PROFILES[0]
    return SourceProfile(
        asset_id=asset_id,
        field=primary.field,
        patches=tuple(
            MediaPatch(
                label_prefix + patch.label.removeprefix("primary"),
                patch.left,
                patch.top,
                patch.red,
                patch.green,
                patch.blue,
                size=patch.size,
            )
            for patch in primary.patches
        ),
        field_sample=primary.field_sample,
        field_span=primary.field_span,
        carries_frame_id=True,
        carries_audio=True,
        decoded_by_browser=True,
        frame_count=sum(count for count, _ in segments),
        timing_segments=segments,
        audio_kind=audio_kind,
    )


#: The three visual sources. Patch labels are unique across every profile on purpose: a landmark is
#: attributed to a layer by the colour it is drawn in, so two layers must never share a label.
_COMPOSITED_PROFILES: Final[tuple[SourceProfile, ...]] = (
    SourceProfile(
        asset_id=PRIMARY_ASSET,
        field=(64, 64, 64),
        patches=(
            MediaPatch("primary_top_left", 12, 32, 200, 40, 40),
            MediaPatch("primary_top_right", 100, 32, 40, 180, 60),
            MediaPatch("primary_bottom_left", 12, 84, 60, 80, 220),
            MediaPatch("primary_bottom_right", 100, 84, 220, 200, 40),
        ),
        # In the flat band above the upper patches (y 0..31), right of the upper-left patch
        # (x 12..27): the sample's 26 px box spans x 29..55 and y 1..27, clear of every mark. At
        # the fixture's placement it lands on canvas (138, 40), 14 px left of the still's mark
        # and far above the title clip's declared band, which begins at canvas row 101.4.
        field_sample=(42, 14),
        # 26 px so that a half-scale placement still leaves the 13 px the comparator's box and
        # its guard band need.
        field_span=26,
        carries_frame_id=True,
        carries_audio=True,
        decoded_by_browser=True,
        frame_count=72,
    ),
    SourceProfile(
        asset_id=OVERLAY_ASSET,
        field=(48, 48, 48),
        # The lower-right mark is 32 px and runs to the source's right edge. 32 px because this
        # source is placed at half scale: a 16 px patch is 8 output pixels, too small for the
        # comparator's interior sample, while 32 px leaves 16, which carries the sample, its guard
        # band and the lattice the alpha point is searched on. To the right edge because, at the
        # fixture's placement, the mark then lands past the primary's right edge (canvas
        # x 233.6..249.6, over nothing but the black canvas) with its own edge and the layer's
        # coinciding: one transition, not two a pixel apart, which is what the coding-block rule
        # (``semantic_conformance_expect.SAMPLE_POINT_NEEDS_WELL_SEPARATED_EDGES``) needs. A
        # dissolve ramp measured there moves the composite from black to nearly the mark's own
        # colour, which is the loud, flat travel a ramp needs
        # (``semantic_conformance_expect.ALPHA_MEASURED_WHERE_THE_RAMP_IS_LOUDEST``). The
        # upper-left mark stays 16 px and sits in the top band (canvas y 42.4..50.4 at the
        # fixture's placement), over the primary's flat field and a coding block clear of the
        # primary's upper-right patch, which begins at canvas row 59: at its earlier place
        # (source y 32) it lay over that patch's top rows and cost the primary its sample there.
        # The two fiducials remain asymmetric.
        patches=(
            MediaPatch("overlay_top_left", 12, 12, 230, 120, 30),
            MediaPatch("overlay_bottom_right", 96, 76, 30, 200, 200, size=32),
        ),
        # The flat band above the identity row, mid-width: the sample's 26 px box spans x 51..77
        # and y 3..29, clear of both marks and of the row (y 52). This
        # source is placed at half scale, so a 26 px flat region becomes 13 output pixels --
        # exactly the box and guard the comparator needs. At the fixture's placement it lands on
        # canvas (218, 44), over the primary's flat field and clear of the primary's upper-right
        # patch.
        field_sample=(64, 16),
        field_span=26,
        carries_frame_id=True,
        carries_audio=True,
        decoded_by_browser=True,
        frame_count=24,
    ),
    SourceProfile(
        asset_id=IMAGE_ASSET,
        # White, because the still multiplies over everything for the whole duration and multiply by
        # white is the identity. The layer is real and really composites; it just does not erase the
        # marks underneath it, which is what makes every other row's landmark readable at all.
        field=(255, 255, 255),
        # Its one mark sits high in the centre column, where the primary carries nothing but flat
        # field, so the still is observable without landing on any other layer's patch; at the
        # fixture's placement it lands on canvas x 156..171, y 34..49, a coding block clear of the
        # primary's field sample to its left.
        patches=(MediaPatch("image_mark", 60, 8, 90, 90, 255),),
        # Clear of the mark (x 60..75) and well inside the frame edge.
        field_sample=(20, 20),
        field_span=32,
        carries_frame_id=False,
        carries_audio=False,
        # A still reference: it reaches the canvas as a decoded PNG, never through the video path.
        decoded_by_browser=False,
        frame_count=1,
    ),
)

#: Where the timing source's unequal intervals begin: its first 36 frames are 12 fps (every frame
#: lasts two output frames, 1024 ticks, so output frame k of a clip that starts at source frame 0
#: shows source frame k // 2), and its last 48 frames alternate 640 and 384 ticks: an odd source
#: frame begins 128 ticks after the output frame that would show it, so the preceding even frame
#: is selected there and every odd frame is never shown at all -- a mapping no constant-rate
#: source produces, which is what makes unequal intervals observable. A clip of 48 output frames
#: from source frame 36 runs exactly to the source's end.
TIMING_VFR_START_FRAME: Final = 36

#: M25-45 AC45-05: the same three composited sources at four times the size, for the high-resolution
#: rows whose composition is 1280 x 720. Four, because 320 x 180 times four is exactly 1280 x 720:
#: the composite is the base corpus's own geometry zoomed, so every clearance the module docstring
#: reasons about holds unchanged and every basis-point transform in the base lands on the same
#: relative place. They carry their own asset identifiers because `profile_for` is what tells the
#: render stage which picture to build, and a high-resolution row that borrowed `vid-primary` would
#: be handed a 128-square source and a 1280-wide canvas -- the upscaled picture AC45-05 refuses.
HIGH_RES_SCALE: Final = 4
HIGH_RES_PRIMARY_ASSET: Final = "vid-primary-hd"
HIGH_RES_OVERLAY_ASSET: Final = "vid-overlay-hd"
HIGH_RES_IMAGE_ASSET: Final = "img-overlay-hd"
_HIGH_RES_PROFILES: Final[tuple[SourceProfile, ...]] = tuple(
    scaled(profile, HIGH_RES_SCALE, asset_id)
    for profile, asset_id in zip(
        _COMPOSITED_PROFILES,
        (HIGH_RES_PRIMARY_ASSET, HIGH_RES_OVERLAY_ASSET, HIGH_RES_IMAGE_ASSET),
        strict=True,
    )
)

SOURCE_PROFILES: Final[tuple[SourceProfile, ...]] = (
    *_COMPOSITED_PROFILES,
    _timing_variant(
        TIMING_ASSET,
        "timing",
        ((TIMING_VFR_START_FRAME, (2 * OUTPUT_FRAME_TICKS,)), (48, (640, 384))),
    ),
    *_HIGH_RES_PROFILES,
    # Last, so that every profile declared before it keeps its place in what the stages read.
    _timing_variant(
        TONE_ASSET,
        "tone",
        ((TONE_SOURCE_FRAME_COUNT, (OUTPUT_FRAME_TICKS,)),),
        audio_kind="tone",
    ),
)

#: The embedded audio: full-scale bursts separated by exact silence. A burst is 256 samples rather
#: than a single impulse because AAC is lossy and a one-sample impulse does not survive it as a
#: locatable onset; 256 samples is a quarter of one AAC frame, so the burst still starts inside one
#: coding block and the onset stays well inside the frozen 2,048-sample bound. The gaps between
#: bursts are far longer than twice the 2,048-sample codec-boundary exclusion, so a silence check
#: over a gap interior can never be empty.
AUDIO_SAMPLE_RATE: Final = 48_000
AUDIO_BURST_SAMPLES: Final = 256
AUDIO_BURST_STARTS: Final = (0, 48_000, 96_000)

#: The tone source's sound: a 1 kHz sine at 0.2 of full scale (PCM16 peak 6,554) for the whole
#: source. A period is exactly 48 samples, so a window of whole periods measures an RMS of exactly
#: the peak over the square root of two, the level a window's measurement is stated against.
#:
#: GUARD: 0.2, not full scale. The `clip_audio` rows raise this by up to +12 dB, a factor of 3.98,
#: and an encode carries the declared level only below full scale: 0.2 reaches 0.80 there, while
#: the 0.5 the plan first named would clip at 1.99 and every upper-bound row would measure the
#: limiter rather than the product.
AUDIO_TONE_HZ: Final = 1_000
AUDIO_TONE_PEAK: Final = 6_554


def frame_id_bits(frame_index: int) -> tuple[int, ...]:
    """The cell values a source frame's identity row carries, most significant bit first."""

    if frame_index < 0 or frame_index >= 1 << FRAME_ID_BITS:
        raise ValueError("the frame identity row cannot state this frame index")
    return tuple((frame_index >> shift) & 1 for shift in reversed(range(FRAME_ID_BITS)))


def source_colour(profile: SourceProfile, frame_index: int, x: int, y: int) -> tuple[int, int, int]:
    """The colour one source pixel carries: an identity cell, a patch, or the flat field.

    This is the whole of what a synthetic frame contains, so it is also the whole of what an oracle
    has to model to say what a composite should look like.
    """

    if profile.carries_frame_id:
        origin_x, origin_y = profile.frame_id_origin_px
        mark = profile.frame_id_mark_px
        if origin_y <= y < origin_y + mark:
            offset = x - origin_x
            cell, within = divmod(offset, profile.frame_id_cell_pitch_px)
            if 0 <= cell < FRAME_ID_BITS and 0 <= within < mark:
                bit = frame_id_bits(frame_index)[cell]
                return FRAME_ID_ONE_RGB if bit else FRAME_ID_ZERO_RGB
    for patch in profile.patches:
        if patch.left <= x < patch.left + patch.size and patch.top <= y < patch.top + patch.size:
            return patch.rgb()
    return profile.field


#: The generated source the two `import_integration` rows import through the real M25-29
#: service (B-65). It is the primary's picture and sound with its own patch labels and a constant
#: 24 fps timing, at exactly the frame count the accepted M25-29 fixture declares for a segment
#: (`scripts/m25_16_import_fixture.py`'s `FRAME_COUNT`): the shipped frontend codec refuses a
#: ready output whose delivered frame count differs from its segment's declared duration, so a
#: source of any other length would never be offered for import. It is deliberately NOT in
#: `SOURCE_PROFILES`: no corpus base composition declares it, and no row reaches it except by
#: importing it. The product mints the asset's identifier itself (`generated.<hash>`), so the
#: oracle learns which asset embodies this profile only through `imported_source()`, and only
#: after `imported_source_landmarks_match()` has checked that the product's own probe of the
#: imported bytes measured exactly this profile's frame table -- never by renaming the asset.
IMPORTED_SOURCE_FRAME_COUNT: Final = 124
IMPORTED_SOURCE_PROFILE: Final[SourceProfile] = _timing_variant(
    "generated-import", "imported", ((IMPORTED_SOURCE_FRAME_COUNT, (OUTPUT_FRAME_TICKS,)),)
)

_NO_ALIASES: Final[Mapping[str, SourceProfile]] = MappingProxyType({})
_IMPORTED_ALIASES: ContextVar[Mapping[str, SourceProfile]] = ContextVar(
    "nle_semantic_imported_aliases", default=_NO_ALIASES
)


def imported_source_landmarks_match(asset: Mapping[str, Any]) -> bool:
    """Whether a product asset entry is the imported source, judged on what the product measured.

    The product's asset wire carries the frame table its own probe measured from the imported
    bytes (`landmarks`: `frame_index`/`pts`), the frame count and the time base. They must equal
    `IMPORTED_SOURCE_PROFILE.pts_table()` exactly; a different table means a different file was
    imported, and the row is then judged on nothing (BLOCKED) rather than on a borrowed profile.
    """

    if asset.get("kind") != "video":
        return False
    if asset.get("source_frame_count") != IMPORTED_SOURCE_FRAME_COUNT:
        return False
    base = asset.get("source_time_base")
    if not isinstance(base, Mapping) or (base.get("num"), base.get("den")) != (
        1,
        SOURCE_TIME_BASE_DEN,
    ):
        return False
    table = asset.get("landmarks")
    if not isinstance(table, Sequence) or len(table) != IMPORTED_SOURCE_FRAME_COUNT:
        return False
    expected = IMPORTED_SOURCE_PROFILE.pts_table()
    for row, (index, pts, _duration) in zip(table, expected, strict=True):
        if not isinstance(row, Mapping):
            return False
        if row.get("frame_index") != index or row.get("pts") != pts:
            return False
    return True


@contextmanager
def imported_source(asset_id: str) -> Iterator[None]:
    """Let `profile_for` answer the product-minted asset id with the imported profile.

    Enter it only after `imported_source_landmarks_match()` held for that asset's wire entry;
    the caller is asserting an identity the product already measured, not assigning one.
    """

    token = _IMPORTED_ALIASES.set({**_IMPORTED_ALIASES.get(), asset_id: IMPORTED_SOURCE_PROFILE})
    try:
        yield
    finally:
        _IMPORTED_ALIASES.reset(token)


def profile_for(asset_id: str) -> SourceProfile | None:
    """The declared profile for this asset, or `None` when the asset is not a synthetic source."""

    aliased = _IMPORTED_ALIASES.get().get(asset_id)
    if aliased is not None:
        return aliased
    for profile in SOURCE_PROFILES:
        if profile.asset_id == asset_id:
            return profile
    return None
