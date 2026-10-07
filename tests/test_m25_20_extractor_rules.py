"""M25-20 post-closeout corrective: the independent extractor observes what it is told to look at.

Each test here pins one rule of `scripts/nle_semantic_conformance.py` that a real run reported as a
defect when it was missing, and each one is written to fail against the earlier behaviour:

- the primary's identity is read at the cells the declared placement prescribes, and its source
  time is the asset's own declared table applied to the index read -- not a colour search for the
  layer and not a time base the extractor invents;
- a colour sample is taken at the frame its own label names, not at the first point's frame;
- a fiducial is the largest connected group of pixels *nearest* the patch colour, so a resampled
  edge belongs to the patch it is mostly made of;
- a ramp's progress is one least-squares ratio over the three channels, not a mean of per-channel
  ratios that lets a channel which barely moved set the answer.

Nothing here executes a decoder: the probes and the decode stream are replaced with synthetic
documents and frames, so what is tested is the extraction rule and nothing else.
"""

from __future__ import annotations

import struct
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.core.semantic_conformance_expect import (
    GeometryTarget,
    IdentityRead,
    SamplePoint,
)
from comfyui_h3_context.core.semantic_conformance_media import (
    PRIMARY_ASSET,
    profile_for,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import nle_semantic_conformance as extractor  # noqa: E402

WIDTH, HEIGHT = 64, 48
FIELD = (64, 64, 64)
PATCH = (200, 40, 40)


def _frame(fill: tuple[int, int, int]) -> bytearray:
    return bytearray(bytes(fill) * (WIDTH * HEIGHT))


def _paint(frame: bytearray, left: int, top: int, size: int, colour: tuple[int, int, int]) -> None:
    for y in range(top, top + size):
        for x in range(left, left + size):
            offset = (y * WIDTH + x) * 3
            frame[offset : offset + 3] = bytes(colour)


def _probe() -> dict[str, Any]:
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": WIDTH,
                "height": HEIGHT,
                "r_frame_rate": "24/1",
                "sample_aspect_ratio": "1:1",
                "nb_frames": "8",
            }
        ],
    }


def _timing(count: int) -> dict[str, Any]:
    return {
        "streams": [{"codec_type": "video", "time_base": "1/12288"}],
        "packets": [
            {"pts": index * 512, "dts": index * 512, "duration": 512, "flags": "K__"}
            for index in range(count)
        ],
    }


def _observe(
    monkeypatch: pytest.MonkeyPatch, frames: list[bytearray], **arguments: Any
) -> dict[str, Any]:
    probes = iter([_probe(), _timing(len(frames))])
    monkeypatch.setattr(extractor, "run_probe", lambda *_args, **_kwargs: next(probes))

    def decode(*_args: Any, **_kwargs: Any) -> Iterator[bytes]:
        for frame in frames:
            yield bytes(frame)

    monkeypatch.setattr(extractor, "decode_stream", decode)
    return extractor.observe_artifact(
        Path("ffmpeg"), Path("ffprobe"), Path("artifact.mp4"), **arguments
    )


def test_the_identity_is_read_at_the_prescribed_cells_and_timed_by_the_declared_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The index comes from the pixels; the time comes from the asset's own table.

    The earlier extractor located the row by searching for the primary's corner colours and then
    stated the index it read at `1/24` second per frame. The corpus base declares a `1/12288`
    time base with 512 ticks per frame, so every one of its 48 frames mismatched on `source_pts`
    with the picture exactly right, and the colour search relocated the row onto a second clip's
    corners. The cells are prescribed; the table is declared; nothing here searches.
    """

    cells = tuple((4 + index * 6, 10) for index in range(8))
    frames = []
    for index in (5, 6):
        frame = _frame(FIELD)
        for bit, (x, y) in zip(format(index, "08b"), cells, strict=True):
            _paint(frame, x - 1, y - 1, 3, (255, 255, 255) if bit == "1" else (0, 0, 0))
        frames.append(frame)
    table = {5: 2_560, 6: 3_072}
    reads = [
        IdentityRead(
            output_frame=frame_index,
            cells=cells,
            pts_by_frame=table,
            time_base_num=1,
            time_base_den=12_288,
        )
        for frame_index in range(2)
    ]
    observed = _observe(monkeypatch, frames, identity_reads=reads)
    assert observed["source_mapping"] == [
        {
            "output_frame": 0,
            "source_frame": 5,
            "source_pts": 2_560,
            "source_time_base_num": 1,
            "source_time_base_den": 12_288,
        },
        {
            "output_frame": 1,
            "source_frame": 6,
            "source_pts": 3_072,
            "source_time_base_num": 1,
            "source_time_base_den": 12_288,
        },
    ]

    # An index the table does not name is still reported as the index it is, with no time
    # invented for it -- the join then reports the index mismatch rather than a fabricated time.
    shifted = [
        IdentityRead(
            output_frame=0, cells=cells, pts_by_frame={9: 0}, time_base_num=1, time_base_den=12_288
        )
    ]
    observed = _observe(monkeypatch, frames, identity_reads=shifted)
    assert observed["source_mapping"] == [
        {
            "output_frame": 0,
            "source_frame": 5,
            "source_pts": None,
            "source_time_base_num": 1,
            "source_time_base_den": 12_288,
        }
    ]

    # A frame with no prescribed read states nothing rather than a guess.
    assert _observe(monkeypatch, frames)["source_mapping"] == []


def test_each_colour_sample_is_taken_at_the_frame_its_own_label_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every `@frame` point was sampled at the first point's frame and compared against its own.

    Frames 0..3 are four flat colours; two points name frame 1 and one names frame 3. Sampling
    them all at frame 1 -- the earlier behaviour -- reports frame 1's colour for the frame-3 point.
    """

    colours = [(10, 10, 10), (60, 60, 60), (110, 110, 110), (160, 160, 160)]
    frames = [_frame(colour) for colour in colours]
    points = [
        SamplePoint(label="clip-main.field", output_frame=1, canvas_x=20, canvas_y=20),
        SamplePoint(label="canvas.center", output_frame=1, canvas_x=32, canvas_y=24),
        SamplePoint(label="clip-main@3.field", output_frame=3, canvas_x=20, canvas_y=20),
    ]
    observed = _observe(monkeypatch, frames, patch_points=points)
    by_label = {
        item["label"]: (item["red"], item["green"], item["blue"]) for item in observed["patches"]
    }
    assert by_label == {
        "clip-main.field": colours[1],
        "canvas.center": colours[1],
        "clip-main@3.field": colours[3],
    }
    assert observed["color_patches"] == observed["patches"]


def test_a_fiducial_is_the_largest_group_of_pixels_nearest_its_own_colour() -> None:
    """A resampled edge belongs to the patch it is mostly made of.

    The patch is 8x8 with a one-pixel ring that is 70% patch and 30% field -- what bilinear
    scaling leaves at a scaled patch's edge. Within a fixed tolerance of 10 the ring is neither
    patch nor field and the reported fiducial comes back a pixel short on every side, which under a
    2 px geometry bound reported right pictures as wrong placements; nearest-colour assignment puts
    the boundary where the edge is.
    """

    profile = profile_for(PRIMARY_ASSET)
    assert profile is not None
    frame = _frame(FIELD)
    ring = (
        round(0.7 * PATCH[0] + 0.3 * FIELD[0]),
        round(0.7 * PATCH[1] + 0.3 * FIELD[1]),
        round(0.7 * PATCH[2] + 0.3 * FIELD[2]),
    )
    _paint(frame, 19, 15, 10, ring)
    _paint(frame, 20, 16, 8, PATCH)
    # A stray pixel of the same colour elsewhere is not part of the fiducial.
    _paint(frame, 50, 40, 1, PATCH)
    landmarks = {
        item["label"]: (item["left"], item["top"], item["right"], item["bottom"])
        for item in extractor._geometry_for_frame(
            bytes(frame), WIDTH, HEIGHT, [("clip-main", profile)]
        )
    }
    assert landmarks["clip-main.primary_top_left"] == (19, 15, 29, 25)
    # The layer itself is the luma extent of the frame, confirmed present by its own colours.
    assert landmarks["clip-main"] == (0, 0, WIDTH, HEIGHT)
    # A patch colour that is nowhere in the frame is reported as absent, not as a guess.
    assert "clip-main.primary_top_right" not in landmarks


def test_a_layer_is_searched_for_at_the_frame_its_target_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frame 0 is black here; the layer is present only at frame 2, and that is where it is."""

    profile = profile_for(PRIMARY_ASSET)
    assert profile is not None
    present = _frame((0, 0, 0))
    _paint(present, 8, 8, 32, FIELD)
    frames = [_frame((0, 0, 0)), _frame((0, 0, 0)), present]
    target = GeometryTarget(clip_id="clip-main", asset_id=PRIMARY_ASSET, sample_frame=2)
    observed = _observe(monkeypatch, frames, geometry_targets=[target])
    assert observed["geometry"] == [
        {"label": "clip-main", "left": 8, "top": 8, "right": 40, "bottom": 40}
    ]
    earlier = GeometryTarget(clip_id="clip-main", asset_id=PRIMARY_ASSET, sample_frame=0)
    assert _observe(monkeypatch, frames, geometry_targets=[earlier])["geometry"] == []


def test_a_ramp_s_progress_is_one_least_squares_ratio_over_the_three_channels() -> None:
    """A channel that barely moved must not set the answer.

    Red travels 100 code values across the ramp and green 2. At the midpoint red has moved 50 and
    green, under one code value of encoder noise, 0. The per-channel mean reports 250; the
    projection of the observed travel onto the full travel reports the 500 the picture shows.
    """

    base = _frame((100, 100, 100))
    mixed = _frame((200, 102, 100))
    midpoint = _frame((150, 100, 100))
    samples = {12: bytes(base), 14: bytes(midpoint), 16: bytes(mixed)}
    alphas = extractor._alphas_for_clip(
        "clip-video-overlay", (32, 24), samples, WIDTH, HEIGHT, 12, 16, 10_000, [12, 14, 16]
    )
    by_frame = {item["output_frame"]: item["alpha_milli"] for item in alphas}
    assert by_frame == {12: 0, 14: 500, 16: 1000}

    # And the declared opacity scales the measured progress, never the other way round.
    scaled = extractor._alphas_for_clip(
        "clip-video-overlay", (32, 24), samples, WIDTH, HEIGHT, 12, 16, 8_500, [14, 16]
    )
    assert {item["output_frame"]: item["alpha_milli"] for item in scaled} == {14: 425, 16: 850}

    # A point the ramp barely moves reports nothing rather than a ratio of noise.
    still = {12: bytes(base), 14: bytes(base), 16: bytes(_frame((101, 100, 100)))}
    assert (
        extractor._alphas_for_clip(
            "clip-video-overlay", (32, 24), still, WIDTH, HEIGHT, 12, 16, 10_000, [14]
        )
        == []
    )


def test_a_gap_after_a_burst_inside_the_first_codec_frame_starts_at_that_frame_s_end() -> None:
    """The extractor measures the gap the expectation declares, from the same boundary.

    A burst detected inside the stream's first codec frame is coded without run-up and rings past
    the exclusion counted from its own end (`semantic_conformance_expect.derive_silences`); the
    gap after it begins at the end of that frame. A burst detected anywhere later begins its gap
    at its own end, exactly as before.
    """

    total = 12_000
    values = [0] * total
    for start in (0, 6_000):
        for offset in range(256):
            values[start + offset] = 30_000
    for index in range(2_304, 2_800):
        values[index] = 9  # ringing past the exclusion counted from the burst's end
    samples = struct.pack(f"<{total}h", *values)

    silences = extractor._measure_silences(
        samples,
        [0, 6_000],
        total,
        burst_samples=256,
        boundary_exclusion=2_048,
        first_frame_samples=1_024,
    )
    by_label = {item["label"]: item for item in silences}
    assert by_label["gap_0"]["start_sample"] == 1_024
    assert by_label["gap_0"]["abs_peak"] == 0
    assert by_label["gap_1"]["start_sample"] == 6_256

    # With no first-frame rule the same ringing is inside the interior and is reported.
    plain = extractor._measure_silences(
        samples, [0, 6_000], total, burst_samples=256, boundary_exclusion=2_048
    )
    assert plain[0]["start_sample"] == 256
    assert plain[0]["abs_peak"] == 9


def test_a_source_start_burst_placed_mid_stream_measures_its_gap_from_its_source_origin() -> None:
    """The first-frame rule is about the burst's source, not the output stream (B-61).

    A second owner that starts at output sample 6000 reading its source from sample 0 places that
    source's sample-0 burst at 6000; the artifact rings past 6000 + 256 + 2048 exactly as the
    stream-start burst does, because the ringing is the source's. Given the owner window, the
    extractor begins that gap at 6000 + 1024, the boundary the expectation declares; without the
    window it would begin at 6256 and report the ringing as a finding against the product.
    """

    total = 20_000
    values = [0] * total
    for start in (0, 6_000):
        for offset in range(256):
            values[start + offset] = 30_000
    for index in range(2_304, 2_800):
        values[index] = 9
    for index in range(8_304, 8_800):
        values[index] = 9
    samples = struct.pack(f"<{total}h", *values)
    windows = [
        {"start_sample": 0, "end_sample": 6_000, "shift_samples": 0},
        {"start_sample": 6_000, "end_sample": total, "shift_samples": 6_000},
    ]
    silences = extractor._measure_silences(
        samples,
        [0, 6_000],
        total,
        burst_samples=256,
        boundary_exclusion=2_048,
        first_frame_samples=1_024,
        owner_windows=windows,
    )
    by_label = {item["label"]: item for item in silences}
    assert by_label["gap_1"]["start_sample"] == 6_000 + 1_024
    assert by_label["gap_1"]["abs_peak"] == 0
    # The same owner reading from a later source sample has run-up: its gap starts at its end.
    windows[1]["shift_samples"] = 6_000 - 48_000
    run_up = extractor._measure_silences(
        samples,
        [0, 6_000],
        total,
        burst_samples=256,
        boundary_exclusion=2_048,
        first_frame_samples=1_024,
        owner_windows=windows,
    )
    assert {item["label"]: item for item in run_up}["gap_1"]["start_sample"] == 6_256
    assert {item["label"]: item for item in run_up}["gap_1"]["abs_peak"] == 9
