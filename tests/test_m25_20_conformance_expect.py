"""The expectation half of the M25-20 three-way comparison.

An expectation is only worth having if it was written from the contract rather than from a run, so
these tests do two things. They pin what the derivation is allowed to state -- the artifact facts
the accepted output profile fixes, and nothing the compositor decides -- and they pin the two
places the derivation could quietly become worthless: an audio expectation that follows something
other than the primary policy, and a timing expectation written in ticks instead of rational time,
which would fail every real artifact because a container picks its own time base.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Iterable
from fractions import Fraction
from pathlib import Path
from typing import Any, Final, cast

import pytest

from comfyui_h3_context.core.semantic_conformance import TOLERANCE_PROFILE
from comfyui_h3_context.core.semantic_conformance_cases import (
    BASE_FIXTURE_NAMES,
    CORPUS_BASE,
    IMAGE_CLIP,
    OVERLAY_CLIP,
    PRIMARY_CLIP,
    CaseRecipe,
    apply_edits,
    build_recipes,
)
from comfyui_h3_context.core.semantic_conformance_compare import (
    BrowserObservation,
    FinalObservation,
    FrameTiming,
    PresentedFrame,
    compare_case,
)
from comfyui_h3_context.core.semantic_conformance_drive import run_setup
from comfyui_h3_context.core.semantic_conformance_expect import (
    ALPHA_CONTRAST_FLOOR,
    BROWSER_LANDMARKS,
    BROWSER_UNOBSERVABLE,
    CANVAS_SAMPLE_LABEL,
    CODEC_FRAME_SAMPLES,
    CODING_BLOCK_GUARD_PX,
    ExpectationError,
    SamplePoint,
    _audio_windows,
    _declares_a_ramp,
    _eq_plane,
    _layer_end_frames,
    _placed_corners,
    _placed_rectangle,
    _placement,
    _ramp_sample_frames,
    _visual_layers,
    alpha_sample_point,
    alpha_targets,
    browser_decoded_colour,
    browser_observable,
    carries_audio,
    composite_colour,
    derive_alphas,
    derive_audio_extent_samples,
    derive_audio_onsets,
    derive_browser_expectation,
    derive_expectation,
    derive_frame_timing,
    derive_geometry,
    derive_output_metadata,
    derive_silences,
    derive_source_mapping,
    geometry_sample_frame,
    geometry_targets,
    identity_reads,
    missing_landmarks,
    patch_sample_frame,
    patch_sample_points,
    preview_scale,
    quiet_sample_frame,
    required_landmarks,
    source_frame_at,
    source_start_tick,
)
from comfyui_h3_context.core.semantic_conformance_media import (
    AUDIO_BURST_SAMPLES,
    FRAME_ID_BITS,
    FRAME_ID_ONE_RGB,
    FRAME_ID_ZERO_RGB,
    IMAGE_ASSET,
    OUTPUT_FRAME_TICKS,
    OVERLAY_ASSET,
    PRIMARY_ASSET,
    TIMING_ASSET,
    TIMING_VFR_START_FRAME,
    SourceProfile,
    frame_id_bits,
    profile_for,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
BOOK = build_recipes()


def _base(base: str = CORPUS_BASE) -> dict[str, Any]:
    """A corpus base's snapshot; a row is always composed on the base its own recipe names."""

    document = json.loads((FIXTURES / BASE_FIXTURE_NAMES[base]).read_text(encoding="utf-8"))
    return cast(dict[str, Any], copy.deepcopy(document["snapshot"]))


def _profile(asset_id: str) -> SourceProfile:
    profile = profile_for(asset_id)
    assert profile is not None, asset_id
    return profile


def _clip(wire: dict[str, Any], clip_id: str) -> dict[str, Any]:
    for clip in wire["clips"]:
        if clip["clip_id"] == clip_id:
            return cast(dict[str, Any], clip)
    raise AssertionError(clip_id)


def _identity(recipe: CaseRecipe) -> str:
    return recipe.case_id


def _composed(recipe: CaseRecipe) -> dict[str, Any]:
    wire = _base(recipe.base)
    apply_edits(wire, recipe)
    return wire


def _declares_a_ramp_anywhere(wire: dict[str, Any]) -> bool:
    return any(_declares_a_ramp(clip) for clip in wire["clips"] if clip.get("enabled", True))


@pytest.mark.parametrize("recipe", list(BOOK.rendering()), ids=_identity)
def test_every_rendering_row_states_an_expectation(recipe: CaseRecipe) -> None:
    """A row that renders and expects nothing would pass by having asked nothing."""

    expectation = derive_expectation(recipe, _composed(recipe))
    assert expectation.case_id == recipe.case_id
    assert expectation.output_metadata is not None
    assert expectation.frame_grid is not None
    assert expectation.frame_timing is not None
    assert expectation.frame_timing.count() == expectation.frame_grid.frame_count


@pytest.mark.parametrize(
    "recipe",
    [item for item in BOOK.recipes if item.refusal_code is not None],
    ids=_identity,
)
def test_a_refusing_row_expects_its_code_and_no_artifact(recipe: CaseRecipe) -> None:
    expectation = derive_expectation(recipe, _base(recipe.base))
    assert expectation.refusal_code == recipe.refusal_code
    # The absence matters as much as the code: the comparator fails a refusal that still produced
    # an artifact, and it can only do that if the expectation declares no artifact facts.
    assert expectation.frame_grid is None
    assert expectation.output_metadata is None


def test_the_audio_extent_accounts_for_the_declared_codec_s_frame_granularity() -> None:
    """AAC decodes to whole 1024-sample frames, so a 2-second timeline is not 96,000 samples.

    The first derivation here said 96,000 -- the timeline duration -- and a real artifact decoded
    96,256. That is not drift and the fix is not a wider tolerance: AAC is a block codec, the output
    profile declares `aac`, and a stream of 2 seconds at 48 kHz is ceil(96000 / 1024) = 94 whole
    frames. The rule is derived from the declared codec and predicts every row's value; the frozen
    one-sample bound still holds the residual, which is what that bound is for. Absorbing a whole
    codec frame into the tolerance instead would have hidden a real drift of up to 1023 samples.
    """

    wire = _base()
    assert wire["output"]["audio_codec"] == "aac"
    assert wire["output"]["sample_rate"] == 48_000
    assert derive_audio_extent_samples(wire) == 94 * 1024 == 96_256

    # A codec with no declared block size is reported as the timeline extent, unrounded.
    unblocked = _base()
    unblocked["output"]["audio_codec"] = "pcm_s16le"
    assert derive_audio_extent_samples(unblocked) == 96_000


def test_audio_follows_the_primary_policy_and_not_any_available_audio() -> None:
    wire = _base()
    assert carries_audio(wire) is True

    # The overlay video's asset declares `excluded_overlay_policy`. Removing the primary clip must
    # therefore remove the audio stream entirely -- if an overlay could contribute audio, every
    # overlay row would fail on a stream the renderer is correct not to write.
    without_primary = _base()
    without_primary["clips"] = [
        clip for clip in without_primary["clips"] if clip["clip_id"] != PRIMARY_CLIP
    ]
    assert carries_audio(without_primary) is False
    assert derive_audio_extent_samples(without_primary) is None
    metadata = derive_output_metadata(without_primary)
    assert metadata.audio_stream_count == 0
    assert metadata.audio_codec is None
    assert metadata.sample_rate is None
    assert metadata.channels is None

    # A disabled primary clip is silent for the same reason a removed one is.
    disabled = _base()
    _clip(disabled, PRIMARY_CLIP)["enabled"] = False
    assert carries_audio(disabled) is False

    # An overlay video clip alone never introduces audio.
    overlay_only = _base()
    overlay_only["clips"] = [
        clip for clip in overlay_only["clips"] if clip["clip_id"] in {OVERLAY_CLIP, IMAGE_CLIP}
    ]
    assert carries_audio(overlay_only) is False


def test_timing_is_stated_in_rational_time_so_a_container_may_choose_its_own_base() -> None:
    """The output declares 1/24; a real mp4 uses 1/12288. Those must agree, exactly."""

    expected = derive_frame_timing(_base())
    assert expected.count() == 48
    assert expected.uniform_step() == Fraction(1, 24)
    assert expected.duration() == 2
    assert expected.dts_policy() == "equal_to_pts"

    ticks = tuple(index * 512 for index in range(48))
    container = FrameTiming(
        time_base_num=1,
        time_base_den=12_288,
        pts_ticks=ticks,
        dts_ticks=ticks,
        duration_ticks=tuple(512 for _ in ticks),
    )
    assert container.times() == expected.times()
    assert container.uniform_step() == expected.uniform_step()
    assert container.duration() == expected.duration()


def test_an_expectation_agreeing_with_an_artifact_passes_without_byte_comparison() -> None:
    """The derived expectation must be satisfiable by an artifact and a canvas that agree with it.

    Both observations have to be supplied, and that is the point rather than test scaffolding: the
    expectation declares source-frame identity, so an artifact with the right container, size and
    duration no longer satisfies it on its own. An empty observation is a disagreement, not a
    silence.
    """

    recipe = next(item for item in BOOK.rendering() if item.observation == "render_and_browser")
    wire = _composed(recipe)
    expectation = derive_expectation(recipe, wire)
    assert expectation.frame_timing is not None
    assert expectation.source_mapping, "a rendering row must declare where its frames come from"
    ticks = tuple(index * 512 for index in range(expectation.frame_timing.count()))
    final = FinalObservation(
        case_id=recipe.case_id,
        extractor_fingerprint="sha256:" + "1" * 64,
        frame_grid=expectation.frame_grid,
        frame_timing=FrameTiming(
            time_base_num=1,
            time_base_den=12_288,
            pts_ticks=ticks,
            dts_ticks=ticks,
            duration_ticks=tuple(512 for _ in ticks),
        ),
        output_metadata=expectation.output_metadata,
        audio_extent_samples=expectation.audio_extent_samples,
        source_mapping=expectation.source_mapping,
        geometry=expectation.geometry,
        patches=expectation.patches,
        color_patches=expectation.color_patches,
        alphas=expectation.alphas,
    )
    browser = BrowserObservation(
        case_id=recipe.case_id,
        # Real backing dimensions, because a geometry tolerance measured in CSS pixels is
        # meaningless and the comparator refuses one outright.
        canvas_width=expectation.frame_grid.width if expectation.frame_grid else 0,
        canvas_height=expectation.frame_grid.height if expectation.frame_grid else 0,
        source_mapping=tuple(
            PresentedFrame(
                output_frame=item.output_frame,
                source_frame=item.source_frame,
                source_time=item.source_time(),
            )
            for item in expectation.source_mapping
        ),
        geometry=expectation.geometry,
        patches=expectation.patches,
        color_patches=expectation.color_patches,
        alphas=expectation.alphas,
    )
    outcome = compare_case(expectation, browser, final, TOLERANCE_PROFILE)
    assert outcome.status == "PASS", outcome.mismatches


#: The rendering rows whose declared composition leaves the primary's identity row legible at no
#: frame at all, each with the mechanism that hides it. Every other rendering row states an
#: identity, and a row may join this list only with a mechanism the composition itself declares --
#: never because an observer happened to fail to read it.
IDENTITY_UNREADABLE_BY_CONSTRUCTION: Final[dict[str, str]] = {
    "transform.position_x_bp.lower": "the primary is placed entirely off the canvas",
    "transform.position_x_bp.upper": "the primary is placed entirely off the canvas",
    "transform.position_y_bp.lower": "the primary is placed entirely off the canvas",
    "transform.position_y_bp.upper": "the primary is placed entirely off the canvas",
    "transform.scale_x_bp.lower": "the scale collapses every identity cell onto one canvas pixel",
    "transform.scale_y_bp.lower": "the scale collapses every identity cell onto one canvas pixel",
    "transform.scale_x_bp.upper": "the scale pushes the identity row off the canvas",
    "effect.brightness_permille.upper": "full brightness lifts the black cells past the threshold",
    "effect.contrast_permille.lower": "zero contrast flattens both cell colours onto mid grey",
    "output.dimensions_square": "a 64-pixel canvas cuts the leftmost cell off the picture",
    "output.dimensions_minimum": "a 16-pixel canvas holds none of the identity row",
}


def test_a_rendering_row_declares_where_its_frames_come_from_wherever_that_is_legible() -> None:
    """Source identity is the landmark that separates "an artifact" from "the right artifact".

    Every other artifact-level fact -- container, codec, size, pixel aspect, colour tags, duration
    -- can be exactly right while the picture is the wrong frames or nothing at all. So every row
    states it at every frame where the declared composition leaves the identity row readable, and
    the rows where no frame is readable are pinned here by the mechanism that hides it. Such a row
    does not close on its container: `required_landmarks` substitutes the composited colour, and
    that substitute has to be non-empty.
    """

    unreadable: set[str] = set()
    for recipe in BOOK.rendering():
        wire = _composed(recipe)
        expectation = derive_expectation(recipe, wire)
        frames = [item.output_frame for item in expectation.source_mapping]
        # One answer per output frame. Two would make the comparison ambiguous rather than stricter.
        assert len(frames) == len(set(frames)), recipe.case_id
        if frames:
            assert recipe.case_id not in IDENTITY_UNREADABLE_BY_CONSTRUCTION, recipe.case_id
            continue
        unreadable.add(recipe.case_id)
        assert recipe.case_id in IDENTITY_UNREADABLE_BY_CONSTRUCTION, recipe.case_id
        required = required_landmarks(recipe, wire)
        assert "source_mapping" not in required and "patches" in required, recipe.case_id
        assert expectation.patches, recipe.case_id
    assert unreadable == set(IDENTITY_UNREADABLE_BY_CONSTRUCTION)


def test_the_identity_is_stated_only_where_every_cell_reads_its_own_mark() -> None:
    """A collapsed layer must not read as legible out of one cell.

    At the lower scale bound every identity cell rounds to the same canvas pixel. Source frame 0
    encodes as six black cells, so reading that one pixel six times gives the right answer for the
    wrong reason -- and only for that frame. The oracle maps the pixel back to the source and
    requires it to fall inside the cell it was read for, so the frame is not stated.
    """

    wire = _base()
    clip = _clip(wire, PRIMARY_CLIP)
    clip["transform"] = dict(clip["transform"], scale_x_bp=1)
    assert derive_source_mapping(wire) == ()
    clip["transform"] = dict(clip["transform"], scale_x_bp=10_000)
    assert [item.output_frame for item in derive_source_mapping(wire)] == list(range(48))


def test_the_identity_is_stated_only_where_no_layer_paints_over_its_cells() -> None:
    """An inserted opaque clip hides the identity row for exactly the frames it covers.

    This is the defect class `command.insert_asset_clip.accepted` reported: a full-size overlay
    inserted at frames 30..37 paints over the primary's identity cells, the oracle kept stating
    the primary's identity there, and the row compared a hidden row against a frame index no
    observer could read. The rule is a legibility rule on the declared composite, so it drops
    exactly those frames and keeps every other one.
    """

    wire = _base()
    overlay = _clip(wire, OVERLAY_CLIP)
    wire["clips"].append(
        {
            **overlay,
            "clip_id": "clip-inserted",
            "start_frame": 30,
            "duration_frames": 8,
            "opacity_bp": 10_000,
            "blend": "normal",
            "transform": dict(
                overlay["transform"],
                position_x_bp=0,
                position_y_bp=0,
                scale_x_bp=10_000,
                scale_y_bp=10_000,
            ),
            "transition": {"kind": "none", "duration_frames": 0},
        }
    )
    frames = [item.output_frame for item in derive_source_mapping(wire)]
    assert frames == [frame for frame in range(48) if not 30 <= frame < 38]


def test_a_wash_that_lifts_the_black_cells_past_the_threshold_hides_the_identity() -> None:
    """A 60% white still in normal blend composites a black cell to 153, over the threshold."""

    wire = _base()
    still = _clip(wire, IMAGE_CLIP)
    still["opacity_bp"] = 6_000
    still["blend"] = "normal"
    assert derive_source_mapping(wire) == ()
    still["opacity_bp"] = 3_000
    still["blend"] = "multiply"
    assert len(derive_source_mapping(wire)) == 48


def test_a_composition_that_declares_no_output_states_no_expectation() -> None:
    """An expectation invented for a composition that declares nothing would be a fabrication."""

    recipe = next(item for item in BOOK.rendering())
    with pytest.raises(ExpectationError):
        derive_expectation(recipe, {"tracks": [], "clips": [], "assets": []})


def test_every_rendering_row_states_every_landmark_its_family_requires() -> None:
    """A required landmark with no expectation behind it is a row that compares nothing.

    This is the closing half of the fail-closed rule in the join. The join blocks a row whose
    landmarks are absent, which keeps the report honest; this keeps the corpus useful, by refusing
    to let a family declare a subject that the expectation cannot state. A new family entry has to
    arrive with the derivation that answers it.
    """

    for recipe in BOOK.rendering():
        wire = _composed(recipe)
        expectation = derive_expectation(recipe, wire)
        for landmark in required_landmarks(recipe, wire):
            value = getattr(expectation, landmark, None)
            assert value is not None, f"{recipe.case_id}.{landmark}"
            assert not (isinstance(value, tuple) and not value), f"{recipe.case_id}.{landmark}"


def test_the_audio_expectation_spans_every_primary_clip_that_owns_audio() -> None:
    """One owner's bursts are not the whole timeline's claim.

    The declared policy resolves an audio owner per output frame among the active primary clips, so
    a composition that cuts, leaves a gap, or trims an owner range has more than one owner across
    its timeline. An expectation that stated only the first owner's bursts would leave every
    interval the later owners cover unclaimed -- and an unclaimed interval is never judged, so a
    spurious onset or a doubled signal there would pass unexamined. That is a quieter failure than
    a wrong expectation and it is the one this pins.
    """

    multi_owner = []
    for recipe in BOOK.rendering():
        if recipe.family != "embedded_audio":
            continue
        state = run_setup(recipe, _base()).state
        assert state is not None, recipe.case_id
        wire = dict(state.snapshot.to_wire())
        owners = {item.label.split("_burst_")[0] for item in derive_audio_onsets(wire)}
        if len(owners) > 1:
            multi_owner.append((recipe.case_id, wire, owners))

    assert multi_owner, (
        "the corpus is supposed to carry compositions with more than one audio owner"
    )
    for case_id, wire, owners in multi_owner:
        assert len(owners) > 1, case_id
        onsets = derive_audio_onsets(wire)
        silences = derive_silences(wire)
        assert len({item.label for item in onsets}) == len(onsets), case_id
        # The silent chain has to reach past the first owner, or the later owners' intervals are
        # the ones nobody claimed.
        first_owner_end = min(
            item.sample_index for item in onsets if item.label.startswith("owner1")
        )
        assert max(item.end_sample for item in silences) >= first_owner_end, case_id


def test_a_burst_at_the_stream_s_start_declares_its_gap_from_the_first_codec_frame_s_end() -> None:
    """The codec's first window has no run-up, and the gap after a burst inside it starts later.

    The primary's first burst lies at source sample 0, and the fixture places it at output sample
    0: inside the stream's first AAC frame, where the encoder has nothing before the attack to
    switch to short windows on, so the burst is spread across the frame's long window. The
    rendered artifact rings above the silence bound until three frames in (1024 + 2048), which is
    the frozen exclusion counted from the end of that first frame rather than from the burst's
    end; the gap is declared from there. The ringing is the burst's place in its *source's* first
    frame, not the stream's (B-61): the same source-start burst placed later on the output still
    rings, and its gap begins one codec frame after its source origin lands. A burst with run-up
    in its source -- the primary's second burst -- declares its gap from its own end wherever it
    is placed.
    """

    wire = _base()
    silences = {item.label: item for item in derive_silences(wire)}
    onsets = {item.sample_index: item for item in derive_audio_onsets(wire)}
    assert 0 in onsets
    assert silences["gap_0"].start_sample == CODEC_FRAME_SAMPLES["aac"]
    assert silences["gap_0"].start_sample > AUDIO_BURST_SAMPLES

    # The same source-start burst placed after the first frame still rings from where its source
    # origin lands; the second burst, which has run-up, declares its gap from its own end.
    shifted = _base()
    _clip(shifted, PRIMARY_CLIP)["start_frame"] = 2
    _clip(shifted, PRIMARY_CLIP)["duration_frames"] = 46
    shifted_onsets = derive_audio_onsets(shifted)
    first = min(item.sample_index for item in shifted_onsets)
    assert first >= CODEC_FRAME_SAMPLES["aac"]
    shifted_gap = next(item for item in derive_silences(shifted) if item.label == "gap_0")
    assert shifted_gap.start_sample == first + CODEC_FRAME_SAMPLES["aac"]
    run_up = _base()
    _clip(run_up, PRIMARY_CLIP)["source_start_frame"] = 24
    run_up_onsets = {item.label: item.sample_index for item in derive_audio_onsets(run_up)}
    assert run_up_onsets["owner0_burst_1"] == 0
    run_up_gap = next(item for item in derive_silences(run_up) if item.label == "gap_0")
    assert run_up_gap.start_sample == AUDIO_BURST_SAMPLES


def test_a_source_start_burst_rings_wherever_its_owner_places_it() -> None:
    """The ringing belongs to the burst's place in its source, not to the stream's start.

    Two rows (`embedded_audio.primary_without_audio` and the after phase of
    `command.slip_clip.accepted`) put a second owner on the primary track at frame 12 reading its
    source from frame 0, so that source's first burst lands at output sample 24000 -- and the
    artifact rang to 4 inside a gap interior that began at 26304, while every burst with run-up
    in its source was silent past its own exclusion (B-61, r5 and r6). The gap after such a burst
    begins one codec frame after the owner's source origin on the output, exactly as B-45 already
    declared for the first owner at sample 0, whose source origin happens to be the stream's start.
    """

    wire = _base()
    primary = _clip(wire, PRIMARY_CLIP)
    primary["duration_frames"] = 12
    wire["clips"].append(
        {
            **copy.deepcopy(primary),
            "clip_id": "clip-second-owner",
            "asset_id": OVERLAY_ASSET,
            "start_frame": 12,
            "duration_frames": 24,
            "source_start_frame": 0,
        }
    )
    per_frame = wire["output"]["sample_rate"] // wire["output"]["frame_rate"]["num"]
    onsets = {item.label: item.sample_index for item in derive_audio_onsets(wire)}
    assert onsets["owner1_burst_0"] == 12 * per_frame
    silences = {item.label: item for item in derive_silences(wire)}
    assert silences["gap_0"].start_sample == CODEC_FRAME_SAMPLES["aac"]
    assert silences["gap_1"].start_sample == 12 * per_frame + CODEC_FRAME_SAMPLES["aac"]
    assert silences["gap_1"].start_sample > onsets["owner1_burst_0"] + AUDIO_BURST_SAMPLES

    # An owner reading the primary from its second burst instead has run-up, and its gap begins
    # at the burst's own end.
    wire["clips"][-1]["asset_id"] = PRIMARY_ASSET
    wire["clips"][-1]["source_start_frame"] = 24
    onsets = {item.label: item.sample_index for item in derive_audio_onsets(wire)}
    assert onsets["owner1_burst_1"] == 12 * per_frame
    silences = {item.label: item for item in derive_silences(wire)}
    assert silences["gap_1"].start_sample == 12 * per_frame + AUDIO_BURST_SAMPLES


def _single_layer_wire(source_start_frame: int) -> dict[str, Any]:
    """The fixture reduced to the primary clip alone, reading from a chosen point in its source."""

    wire = _base()
    wire["clips"] = [item for item in wire["clips"] if item["clip_id"] == PRIMARY_CLIP]
    clip = wire["clips"][0]
    clip["source_start_frame"] = source_start_frame
    clip["start_frame"] = 0
    clip["duration_frames"] = 48
    return wire


def test_a_layer_that_outruns_its_source_holds_the_source_s_last_frame() -> None:
    """The oracle must state the picture the render path produces, including past the source end.

    A layer whose range runs past its source holds the final frame rather than failing. The claim is
    not merely that the oracle returns *a* colour -- it is that the colour is the last source
    frame's, identity marks included. So this compares two compositions that must describe the same
    picture: one reading 60..107 from a 72-frame source, which runs eight frames past the end, and
    one reading 24..71, which ends exactly on it. Their last output frame is the same source frame,
    so every sampled pixel has to agree. An oracle that passed the unclamped index straight through
    would state frame 107's identity marks in the first composition and frame 71's in the second,
    and the frame-identity band is where the two disagree.
    """

    assert frame_id_bits(71) != frame_id_bits(107), (
        "the two indices must encode different identities or this pins nothing"
    )

    outruns = _single_layer_wire(60)
    exact = _single_layer_wire(24)

    seen: set[tuple[int, int, int]] = set()
    for y in range(0, 180, 2):
        for x in range(0, 320, 2):
            held = composite_colour(outruns, 47, x, y)
            ended = composite_colour(exact, 47, x, y)
            assert held == ended, (x, y, held, ended)
            seen.add(held)

    assert FRAME_ID_ONE_RGB in seen and FRAME_ID_ZERO_RGB in seen, (
        "the sampled band must cross the frame-identity marks, or the comparison sees nothing"
    )


def _timing_layer_wire(source_start_frame: int) -> dict[str, Any]:
    """The primary clip alone, reading the timing source from a chosen frame."""

    wire = _single_layer_wire(source_start_frame)
    wire["clips"][0]["asset_id"] = TIMING_ASSET
    return wire


def test_the_source_frame_an_output_frame_shows_follows_the_declared_landmark_table() -> None:
    """A different source rate and unequal intervals are observable only through the table.

    `vid-timing` is 12 fps for its first 36 frames and alternates 640 and 384 ticks for its last
    48. So a clip from source frame 0 shows source frame k // 2 at output frame k, and a clip from
    frame 36 shows only the even frames of the unequal part -- an odd frame begins 128 ticks after
    the output frame that would show it. An oracle that added the elapsed output frames to the
    start frame (the identity, right for every 24 fps source) states the wrong picture on both,
    and that is the mapping the first corpus stated for rows named after these very cases (B-58).
    """

    from_start = _timing_layer_wire(0)
    clip = from_start["clips"][0]
    for k in range(48):
        shown = source_frame_at(from_start, clip, k)
        assert shown is not None
        assert shown[0] == k // 2, k
        assert shown[1] == (k // 2) * 2 * OUTPUT_FRAME_TICKS, k

    from_vfr = _timing_layer_wire(TIMING_VFR_START_FRAME)
    clip = from_vfr["clips"][0]
    assert source_start_tick(from_vfr, clip) == TIMING_VFR_START_FRAME * 2 * OUTPUT_FRAME_TICKS
    for k in range(48):
        shown = source_frame_at(from_vfr, clip, k)
        assert shown is not None
        assert shown[0] == TIMING_VFR_START_FRAME + 2 * (k // 2), k
    # The mapping is what the identity landmarks state, frame for frame.
    landmarks = {item.output_frame: item.source_frame for item in derive_source_mapping(from_vfr)}
    assert landmarks, "the timing source's identity row is legible on a bare primary"
    assert all(landmarks[k] == TIMING_VFR_START_FRAME + 2 * (k // 2) for k in landmarks)
    # The composite reads the same frame the rule names: the timing source carries the primary's
    # picture, so output frame 1 of a clip from frame 36 (source frame 36, the odd frame 37 being
    # 128 ticks late) is pixel for pixel the primary read from frame 35 one frame in.
    seen: set[tuple[int, int, int]] = set()
    same_picture = _single_layer_wire(35)
    for y in range(0, 180, 2):
        for x in range(0, 320, 2):
            timed = composite_colour(from_vfr, 1, x, y)
            assert timed == composite_colour(same_picture, 1, x, y), (x, y)
            seen.add(timed)
    assert FRAME_ID_ONE_RGB in seen and FRAME_ID_ZERO_RGB in seen
    # The 24 fps primary is the identity, so nothing already pinned moves.
    base = _single_layer_wire(24)
    clip = base["clips"][0]
    assert [source_frame_at(base, clip, k) for k in range(48)] == [
        (24 + k, (24 + k) * OUTPUT_FRAME_TICKS) for k in range(48)
    ]


def test_the_audio_owner_s_shift_follows_source_time_not_source_frame_count() -> None:
    """A clip from frame 36 of the 12 fps source starts its audio three seconds in, not 1.5.

    The audio window's shift is the difference between where the clip starts on the output and
    where its source start frame lands in source *time*; counting frames as output frames would
    place the timing source's bursts at half their true offset (B-58).
    """

    rate = _int_output(_timing_layer_wire(0), "sample_rate")
    (window,) = _audio_windows(_timing_layer_wire(TIMING_VFR_START_FRAME))
    assert window.shift_samples == -3 * rate
    (window,) = _audio_windows(_timing_layer_wire(0))
    assert window.shift_samples == 0
    (window,) = _audio_windows(_single_layer_wire(24))
    assert window.shift_samples == -rate


def _int_output(wire: dict[str, Any], field: str) -> int:
    return int(wire["output"][field])


def test_every_required_landmark_is_either_browser_observable_or_reasoned() -> None:
    """A landmark the browser does not answer must say which surface cannot answer it.

    The browser side is narrower than the final side by construction -- a canvas has no container
    and no encoder -- but "narrower" is also what a fail-closed check looks like just before it
    stops checking anything. So the two sets have to partition every landmark any family or command
    requires, with no landmark in both and none in neither, and every exemption has to carry a
    reason about the product surface rather than about the work.
    """

    required: set[str] = set()
    for recipe in BOOK.rendering():
        required.update(required_landmarks(recipe, _composed(recipe)))
    assert required, "the corpus is supposed to require landmarks"

    observable = set(BROWSER_LANDMARKS)
    exempt = set(BROWSER_UNOBSERVABLE)
    assert not observable & exempt, sorted(observable & exempt)
    assert required <= observable | exempt, sorted(required - (observable | exempt))

    for landmark in sorted(required & exempt):
        reason = BROWSER_UNOBSERVABLE[landmark]
        assert isinstance(reason, str) and reason.strip(), landmark


def test_the_final_side_still_requires_what_the_browser_cannot_observe() -> None:
    """Exempting the browser must not exempt the artifact.

    Text and sound are the two landmarks the preview surface genuinely cannot produce, and the
    reason they may be dropped from the browser side at all is that the decoded artifact still has
    to state them. If a landmark could be dropped from both, the row would be back to passing on
    evidence nobody supplied -- which is the finding this whole corrective exists to close.
    """

    for landmark in ("text", "audio_onsets", "silences", "output_metadata"):
        assert landmark in BROWSER_UNOBSERVABLE, landmark

    families = {recipe.family: recipe for recipe in BOOK.rendering()}
    text_row = families["text"]
    audio_row = families["embedded_audio"]

    class _Nothing:
        pass

    for recipe, landmark in ((text_row, "text"), (audio_row, "audio_onsets")):
        wire = _composed(recipe)
        assert landmark in required_landmarks(recipe, wire), recipe.case_id
        final = missing_landmarks(recipe, _Nothing(), wire, side="final")
        assert f"final.{landmark}" in final, (recipe.case_id, final)
        browser = missing_landmarks(recipe, _Nothing(), wire, side="browser")
        assert f"browser.{landmark}" not in browser, (recipe.case_id, browser)
        assert browser, "the browser side still has to require what a canvas can show"


def test_the_oracle_refuses_a_canvas_row_a_text_layer_may_have_painted() -> None:
    """A wrong expectation is worse than a missing one, and this was a wrong one.

    `_visual_layers` skips a text clip, because a text clip has no synthetic source and
    `profile_for(None)` is `None`. So the colour oracle was describing the rows the title paints as
    if the title were absent -- not declining to answer, answering incorrectly. The fixture's two
    bottom patches sit at canvas row 118, under the title's half-alpha black box, and that is why
    the first real renders disagreed with the oracle there by up to 26 units against a frozen bound
    of 8, while every patch outside the band agreed within a few.

    The oracle cannot describe those rows, because a text layer's extent comes from font metrics no
    analytic formula over the wire has. So it says so.
    """

    wire = _base()
    frame = patch_sample_frame(wire)
    title = _clip(wire, "clip-title")
    span = int(title["start_frame"]), int(title["start_frame"]) + int(title["duration_frames"])
    assert span[0] <= frame < span[1], "the fixture's sample frame is under the title"

    with pytest.raises(ExpectationError):
        composite_colour(wire, frame, 160, 118)
    outside = composite_colour(wire, frame, 160, 66)
    assert len(outside) == 3

    sampled = {point.label for point in patch_sample_points(wire)}
    assert "clip-main.primary_bottom_left" not in sampled
    assert "clip-main.primary_bottom_right" not in sampled
    # The remaining points still discriminate; dropping is not the same as blinding the row.
    assert "clip-main.primary_top_left" in sampled
    assert "clip-main.field" in sampled

    # And the band is the text layer's doing, not an unconditional hole in the canvas.
    without_text = _base()
    without_text["clips"] = [
        clip for clip in without_text["clips"] if clip["clip_id"] != "clip-title"
    ]
    restored = {point.label for point in patch_sample_points(without_text)}
    assert "clip-main.primary_bottom_left" in restored
    assert composite_colour(without_text, frame, 160, 118)


def test_only_a_declared_ramp_states_an_alpha_at_all() -> None:
    """An alpha nobody can measure is a declaration reading itself back.

    A layer's alpha is recovered from the composite as a ratio between two states of that layer, so
    a clip whose opacity never changes offers one state for its whole lifetime and no observer has a
    second reference. The render stage reported exactly this from the other side: the three
    flat-opacity clips' alpha samples were absent from every artifact it decoded, while the
    dissolving clip's were present. The expectation now states what an observer can produce.
    """

    wire = _base()
    stated = {sample.label for sample in derive_alphas(wire)}
    assert stated == {"clip-video-overlay"}, sorted(stated)

    flat = _base()
    _clip(flat, "clip-video-overlay")["transition"] = {"kind": "none", "duration_frames": 0}
    assert derive_alphas(flat) == ()

    # And the ramping clip is stated across its ramp, not only at its midpoint, or a one-frame
    # dissolve and a four-frame one would say the same thing.
    frames = sorted({sample.output_frame for sample in derive_alphas(wire)})
    assert len(frames) > 1, frames


def test_a_composition_with_no_ramp_must_observe_the_colour_instead() -> None:
    """Substitution, not removal: the row still has to observe what its family is about."""

    transition_rows = [item for item in BOOK.rendering() if item.family == "transition"]
    ramped = next(item for item in transition_rows if _declares_a_ramp_anywhere(_composed(item)))
    flat = next(item for item in transition_rows if not _declares_a_ramp_anywhere(_composed(item)))

    assert "alphas" in required_landmarks(ramped, _composed(ramped))
    flat_required = required_landmarks(flat, _composed(flat))
    assert "alphas" not in flat_required, flat_required
    assert "patches" in flat_required, flat_required


def test_a_row_that_drops_its_ramp_does_not_state_what_an_untouched_row_states() -> None:
    """The removed dissolve has to reach the pixels, or the row is judged on its container.

    The steady-state sample frame is the shortest visual layer's midpoint, and in this corpus the
    shortest layer is the one that dissolves -- so its midpoint lies after the ramp has finished and
    a removed ramp leaves that frame untouched. Sampling the ramp's own midpoint as well is what
    keeps `transition.none_identity` from stating exactly what an unrelated identity row states.
    """

    flat = next(
        item
        for item in BOOK.rendering()
        if item.family == "transition" and not _declares_a_ramp_anywhere(_composed(item))
    )
    untouched = next(item for item in BOOK.rendering() if item.family == "history_currentness")

    flat_expectation = derive_expectation(flat, _composed(flat))
    untouched_expectation = derive_expectation(untouched, _composed(untouched))
    assert flat_expectation.patches != untouched_expectation.patches

    # The difference is the ramp frame's own samples, and they are additive: the steady-state points
    # a composition has always stated are still stated, under the same labels.
    steady = {sample.label for sample in flat_expectation.patches}
    both = {sample.label for sample in untouched_expectation.patches}
    assert steady < both, sorted(steady - both)
    assert all("@" in label for label in both - steady), sorted(both - steady)


def _labels(points: Iterable[SamplePoint]) -> set[str]:
    return {point.label for point in points}


def test_a_sample_point_is_stated_only_where_the_whole_composite_is_flat_around_it() -> None:
    """A point whose box straddles an edge measures a mean the expectation never stated.

    The overlay's field sample sits over the primary's right edge at the fixture's placement: the
    comparator's 5x5 box is clear of it, but a wider crop of the primary that moves its edge under
    the box turns the sample into a mean of overlay-over-primary and overlay-over-ground. Both
    layers are flat there; the composite is not. The rule reads the composite, so the point is
    dropped rather than compared against one of the two colours.
    """

    wire = _base()
    labels = _labels(patch_sample_points(wire))
    assert "clip-video-overlay.field" in labels, sorted(labels)

    edged = _base()
    _clip(edged, PRIMARY_CLIP)["crop"] = {
        "left_bp": 0,
        "top_bp": 0,
        "right_bp": 500,
        "bottom_bp": 0,
    }
    edged_labels = _labels(patch_sample_points(edged))
    assert "clip-video-overlay.field" not in edged_labels, sorted(edged_labels)
    # The rule is about the composite, not about the overlay: its own layer is still flat there.
    overlay = next(
        layer for layer in _visual_layers(edged) if layer.clip["clip_id"] == "clip-video-overlay"
    )
    assert overlay.placement.to_source(218, 71) is not None


def test_a_white_still_under_multiply_states_no_rectangle() -> None:
    """Multiply by white is the identity, so the still has no edge for any observer to find.

    Its placement is still asserted: the still's centre mark is a colour landmark at the point
    the declared placement predicts.
    """

    wire = _base()
    labels = {landmark.label for landmark in derive_geometry(wire)}
    assert "clip-main" in labels and IMAGE_CLIP not in labels, sorted(labels)
    assert f"{IMAGE_CLIP}.image_mark" in _labels(patch_sample_points(wire))

    visible = _base()
    _clip(visible, IMAGE_CLIP)["blend"] = "normal"
    assert IMAGE_CLIP in {landmark.label for landmark in derive_geometry(visible)}


def test_geometry_is_sampled_inside_each_layer_s_own_span_where_the_fewest_others_cover_it() -> (
    None
):
    """The frame a layer's rectangle is read at comes from its declared span, never a fixed frame.

    A rectangle is a luma edge and its fiducials are colours, and both are spoiled by whatever is
    painted over them, so the frame is the first in the layer's own span at which the fewest other
    clips are active. With the title moved to frame 0 the old fixed choice would read the primary
    under the title; the rule reads it at the first frame where only the still remains. A partial
    layer is read inside its own span, and a clip the composition does not have has no frame.
    """

    wire = _base()
    _clip(wire, "clip-title")["start_frame"] = 0  # title 0..19, overlay 12..23, still throughout
    assert geometry_sample_frame(wire, PRIMARY_CLIP) == 24
    assert geometry_sample_frame(wire, OVERLAY_CLIP) == 20
    assert geometry_sample_frame(wire, "clip-nowhere") is None
    stated = {target.clip_id: target.sample_frame for target in geometry_targets(wire)}
    assert stated[PRIMARY_CLIP] == 24
    # On the base itself frame 0 happens to be quietest for the primary; that is a fact of this
    # composition, not the rule.
    assert geometry_sample_frame(_base(), PRIMARY_CLIP) == 0


def test_the_browser_expectation_states_chromium_s_decode_of_every_video_source() -> None:
    """The browser side is held to the colour the preview's decoder presents.

    Measured on the pinned Chromium against ffmpeg's decode of the same BT.709 file: red and green
    agree to a code value and blue falls short by 0.112 per code value of chroma-blue away from
    neutral (libyuv's 2.0 in place of 2.112). The primary's blue patch presents at 211 for a
    declared 220 and its yellow patch at 50 for a declared 40 -- past the eight-value bound in
    both directions -- and the browser expectation says so. The image reference, text and the
    ground are not decoded and keep their declared colour; the final expectation is untouched.
    """

    primary = profile_for(PRIMARY_ASSET)
    image = profile_for(IMAGE_ASSET)
    assert primary is not None and image is not None
    # Chromium measured: (60, 79, 211), (219, 200, 50), (40, 181, 64), (200, 41, 42).
    assert browser_decoded_colour(primary, (60, 80, 220)) == (60, 80, 213)
    assert browser_decoded_colour(primary, (220, 200, 40)) == (220, 200, 48)
    assert browser_decoded_colour(primary, (40, 180, 60)) == (40, 180, 64)
    assert browser_decoded_colour(primary, (200, 40, 40)) == (200, 40, 42)
    assert browser_decoded_colour(primary, (64, 64, 64)) == (64, 64, 64)
    assert browser_decoded_colour(image, (90, 90, 255)) == (90, 90, 255)

    recipe = BOOK.by_id["transform.rotation_mdeg.interior"]
    wire = _base()
    apply_edits(wire, recipe)
    final = derive_expectation(recipe, wire)
    browser = derive_browser_expectation(recipe, wire)
    by_label = {item.label: item for item in browser.patches}
    for item in final.patches:
        presented = by_label[item.label]
        assert (presented.red, presented.green) == (item.red, item.green), item.label
        if item.label.endswith("primary_bottom_right"):
            assert (item.blue, presented.blue) == (40, 48), item.label
        if item.label.endswith("primary_bottom_left"):
            assert (item.blue, presented.blue) == (220, 213), item.label
        if item.label.endswith("image_mark") or item.label.endswith("center"):
            assert presented.blue == item.blue, item.label
    # Everything but the colours is the same statement.
    assert browser.source_mapping == final.source_mapping
    assert browser.geometry == final.geometry
    assert browser.case_id == final.case_id
    # Deriving the browser expectation leaves the ordinary derivation unchanged afterwards.
    assert derive_expectation(recipe, wire) == final


def test_the_browser_expectation_states_only_what_the_preview_can_carry() -> None:
    """Every landmark `BROWSER_UNOBSERVABLE` explains is unstated on the browser side.

    The comparator asks the canvas for nothing it cannot show -- text metrics, onsets, silences,
    container facts -- while the final expectation still states all of them. A landmark that is
    both observed by the browser and explained away would be a contradiction, and none is.
    """

    assert not set(BROWSER_LANDMARKS) & set(BROWSER_UNOBSERVABLE)
    for case_id in ("text.size_px.interior", "embedded_audio.primary_audible"):
        recipe = BOOK.by_id[case_id]
        wire = _base()
        apply_edits(wire, recipe)
        final = derive_expectation(recipe, wire)
        browser = derive_browser_expectation(recipe, wire)
        for landmark in BROWSER_UNOBSERVABLE:
            value = getattr(browser, landmark)
            assert value is None or value == (), (case_id, landmark)
        for landmark in BROWSER_LANDMARKS:
            assert bool(getattr(browser, landmark)) == bool(getattr(final, landmark)), landmark
    text_row = BOOK.by_id["text.size_px.interior"]
    wire = _base()
    apply_edits(wire, text_row)
    assert derive_expectation(text_row, wire).text is not None
    audio_row = BOOK.by_id["embedded_audio.primary_audible"]
    wire = _base()
    apply_edits(wire, audio_row)
    assert derive_expectation(audio_row, wire).audio_onsets


def test_an_alpha_target_names_every_frame_the_expectation_states() -> None:
    """`alpha_targets` is the one statement of where a ramp is read, on the artifact and the canvas.

    The render stage shapes it for the extractor and the wire generator hands it to the browser
    journey, so its frames have to cover every frame `derive_alphas` states for the clip, and its
    point has to be the ramp's own sample point. The browser side is held to alpha like the final
    side: a transition row whose browser observation carries no alpha is not judged.
    """

    recipe = BOOK.by_id["transition.alpha_progression"]
    wire = _base()
    apply_edits(wire, recipe)
    targets = {target.label: target for target in alpha_targets(wire)}
    stated = derive_alphas(wire)
    assert stated
    for sample in stated:
        target = targets[sample.label]
        assert sample.output_frame in target.sample_frames, sample
    for target in targets.values():
        point = alpha_sample_point(wire, target.label)
        assert point is not None
        assert (target.canvas_x, target.canvas_y) == (point.canvas_x, point.canvas_y)
        assert (target.base_frame, target.steady_frame) == (point.base_frame, point.steady_frame)
        assert target.opacity_bp == point.opacity_bp
        assert {target.base_frame, target.steady_frame} <= set(target.sample_frames)

    assert "alphas" in BROWSER_LANDMARKS
    gaps = missing_landmarks(
        recipe, BrowserObservation(case_id=recipe.case_id), wire, side="browser"
    )
    assert "browser.alphas" in gaps


def test_the_alpha_is_measured_where_the_declared_ramp_moves_the_pixel_most() -> None:
    """The ratio an observer recovers is only as precise as the ramp's travel is long.

    At the overlay's field point a normal-blend ramp moves the composite by a few tens of code
    values per channel, and one code value of encoder noise there is the whole frozen bound; this
    is how `opacity_blend.blend.normal` reported 374 against 425. The point is chosen by declared
    travel, above a floor, and never falls back to the field.
    """

    wire = _base()
    _clip(wire, OVERLAY_CLIP)["blend"] = "normal"
    point = alpha_sample_point(wire, OVERLAY_CLIP)
    assert point is not None
    assert point.contrast >= ALPHA_CONTRAST_FLOOR
    field = next(
        item for item in patch_sample_points(wire) if item.label == "clip-video-overlay.field"
    )
    assert (point.canvas_x, point.canvas_y) != (field.canvas_x, field.canvas_y)
    layers = _visual_layers(wire)
    travel = math.hypot(
        *(
            steady - base
            for steady, base in zip(
                composite_colour(wire, point.steady_frame, point.canvas_x, point.canvas_y),
                composite_colour(wire, point.base_frame, point.canvas_x, point.canvas_y),
                strict=True,
            )
        )
    )
    assert round(travel) == point.contrast
    # The loudest point in the base composition is the overlay's 32 px mark over the black canvas,
    # never a field: the mark moves the composite by more than two hundred code values as a
    # vector, the fields by a few tens.
    assert point.contrast > 200
    assert len(layers) == 3

    # A ramp that no point can carry states no alpha and substitutes the colour instead.
    faint = _base()
    _clip(faint, OVERLAY_CLIP)["opacity_bp"] = 200
    assert alpha_sample_point(faint, OVERLAY_CLIP) is None
    assert derive_alphas(faint) == ()
    ramped = next(item for item in BOOK.rendering() if item.family == "transition")
    faint_required = required_landmarks(ramped, faint)
    assert "alphas" not in faint_required and "patches" in faint_required, faint_required


def test_a_rotated_layer_that_runs_off_the_canvas_states_the_box_of_what_remains() -> None:
    """The observer sees the clipped polygon, not the clipped bounding box of the whole layer.

    A layer rotated by thirty degrees and pushed up so that a corner leaves the canvas keeps its
    rightmost point above the top edge; clipping the layer's *bounding box* to the canvas keeps
    that x as the right edge, but nothing is painted there. The rectangle stated is the bounding
    box of the polygon that survives the canvas, which `command.set_visual_transform` reported at
    259 against a claimed 278 -- nineteen pixels of nothing.
    """

    wire = _base()
    wire["clips"] = [item for item in wire["clips"] if item["clip_id"] == PRIMARY_CLIP]
    clip = _clip(wire, PRIMARY_CLIP)
    clip["transform"] = {
        **clip["transform"],
        "position_x_bp": 1_250,
        "position_y_bp": -3_750,
        "scale_x_bp": 6_000,
        "scale_y_bp": 14_000,
        "rotation_mdeg": 30_000,
    }
    landmark = next(item for item in derive_geometry(wire) if item.label == PRIMARY_CLIP)
    corners = _placed_corners(clip, _profile(str(clip["asset_id"])), 320, 180)
    assert max(y for _x, y in corners) > 0 > min(y for _x, y in corners)
    assert landmark.top == 0
    assert landmark.right == 257
    assert landmark.left == 122 and landmark.bottom == 119

    # Unrotated, the polygon is the rectangle and nothing changes.
    clip["transform"]["rotation_mdeg"] = 0
    unrotated = next(item for item in derive_geometry(wire) if item.label == PRIMARY_CLIP)
    box = _placed_rectangle(clip, _profile(str(clip["asset_id"])), 320, 180)
    assert (unrotated.left, unrotated.right) == (round(box[0]), round(box[2]))


#: What the pinned renderer's `eq` filter actually produces, read off its lookup tables for a
#: 0..255 ramp on one plane (`ffmpeg -f rawvideo -pix_fmt yuv444p ... -vf eq=...`), at a handful of
#: entries per setting. Measured on the pinned binary, never derived from the documentation.
EQ_LUT_PINS: Final[tuple[tuple[float, float, tuple[tuple[int, int], ...]], ...]] = (
    (
        1.0,
        -0.2,
        ((0, 0), (16, 0), (64, 9), (127, 72), (128, 73), (200, 145), (235, 180), (255, 200)),
    ),
    (1.0, 0.15, ((0, 37), (16, 53), (64, 101), (127, 164), (128, 165), (200, 237), (255, 255))),
    (1.4, 0.0, ((0, 0), (16, 0), (64, 37), (127, 125), (128, 127), (200, 227), (235, 255))),
    (0.6, 0.0, ((0, 51), (16, 60), (64, 89), (127, 127), (128, 127), (200, 170), (255, 203))),
    (1.1, 0.05, ((0, 0), (16, 17), (64, 70), (127, 139), (128, 140), (200, 219), (235, 255))),
    # Saturation is the same fixed-point path on a chroma plane.
    (0.7, 0.0, ((0, 38), (16, 49), (64, 82), (127, 126), (128, 127), (200, 177), (255, 216))),
    (1.3, 0.0, ((0, 0), (16, 0), (64, 44), (127, 126), (128, 127), (200, 220), (235, 255))),
)


def test_the_colour_adjust_model_is_the_renderer_s_own_fixed_point_eq_path() -> None:
    """A plane value goes through `eq` the way the pinned binary's SIMD path computes it.

    The documented formula `256 * (contrast * (v - 0.5) + 0.5 + brightness)` is one or two code
    values high on almost every entry, which moved `effect.brightness_permille.upper` past its
    bound; the model is the filter's fixed-point arithmetic, options first clipped through
    single-precision floats, and it reproduces every measured table entry exactly.
    """

    for contrast, brightness, pins in EQ_LUT_PINS:
        for value, expected in pins:
            assert _eq_plane(value, contrast, brightness) == expected, (contrast, brightness, value)
    # And identity parameters copy the plane, as the filter's own `check_values` does.
    assert all(_eq_plane(value, 1.0, 0.0) == value for value in range(256))


def test_a_colour_sample_needs_its_chroma_edges_a_coding_block_apart() -> None:
    """Two chroma transitions within one coding block of the sample smear it; no sample there.

    Measured on the pinned encoder: a single edge is coded cleanly, and two chroma edges within
    eight pixels of each other smear the chroma between them by about ten code values for up to
    eight pixels -- more than the frozen patch bound. The still's blue mark is slid across the
    primary's upper-right patch: with the mark's edge a few pixels inside the patch's edge, or a
    few pixels past it, the patch loses its sample; with the two edges coincident there is one
    transition, and a coding block apart there are two well separated ones, and the sample stands.
    A luma-only edge pair (the identity row's black and white cells) never counts: luma is coded
    at full resolution, and the canvas centre five pixels below the row reads exact.
    """

    mark = _profile(IMAGE_ASSET).patches[0]
    patch = next(
        item for item in _profile(PRIMARY_ASSET).patches if item.label == "primary_top_right"
    )
    expected = {53: False, 56: True, 60: False, 64: True, 68: True}
    for shift_x, stated in expected.items():
        wire = _base()
        wire["clips"] = [
            item for item in wire["clips"] if item["clip_id"] in (PRIMARY_CLIP, IMAGE_CLIP)
        ]
        still = _clip(wire, IMAGE_CLIP)
        still["transform"] = {
            **still["transform"],
            "position_x_bp": round(shift_x / 320 * 10_000),
            "position_y_bp": round(26 / 180 * 10_000),
        }
        mark_left = _placement(still, _profile(str(still["asset_id"])), 320, 180).to_canvas(
            mark.left, mark.top
        )
        patch_right = _placement(
            _clip(wire, PRIMARY_CLIP), _profile(PRIMARY_ASSET), 320, 180
        ).to_canvas(patch.left + patch.size, patch.top)
        assert mark_left is not None and patch_right is not None
        gap = mark_left[0] - patch_right[0]
        assert stated == (gap == 0 or gap >= CODING_BLOCK_GUARD_PX), (shift_x, gap)
        labels = {item.label for item in patch_sample_points(wire)}
        assert (f"{PRIMARY_CLIP}.primary_top_right" in labels) == stated, (shift_x, gap)

    base = _base()
    assert f"{CANVAS_SAMPLE_LABEL}.center" in {item.label for item in patch_sample_points(base)}


def test_the_alpha_contrast_floor_is_where_the_measured_codec_noise_meets_the_bound() -> None:
    """A ramp whose loudest point travels less than the floor states no alpha at all.

    The floor is empirical (see `ALPHA_CONTRAST_FLOOR`): below it the ramp's midpoint frame, a
    B-frame on the pinned encoder, was read tens of thousandths off. Halving the overlay's
    opacity brings its loudest point just over the floor; a little less and there is no point.
    """

    just_loud_enough = _base()
    _clip(just_loud_enough, OVERLAY_CLIP)["opacity_bp"] = 5_000
    point = alpha_sample_point(just_loud_enough, OVERLAY_CLIP)
    assert point is not None
    assert ALPHA_CONTRAST_FLOOR <= point.contrast < ALPHA_CONTRAST_FLOOR + 10

    too_faint = _base()
    _clip(too_faint, OVERLAY_CLIP)["opacity_bp"] = 4_500
    assert alpha_sample_point(too_faint, OVERLAY_CLIP) is None
    assert derive_alphas(too_faint) == ()


def test_a_partial_layer_is_sampled_where_it_arrives_and_where_it_leaves() -> None:
    """A one-frame move of the overlay must move a stated colour, so the ends are where we look.

    The steady frame is a layer's midpoint and the quiet frame is where the fewest clips are, and a
    clip moved by one frame leaves both untouched; `command.move_clip` closed with its two phases
    stating the same colours. So every visual layer whose span is shorter than the output is also
    sampled at its first and last frames and at the frame just outside each end -- and at those
    outside frames the layer's own points are still stated, carrying what lies beneath, so that
    the layer's absence there is a colour a comparison can hold.
    """

    wire = _base()
    overlay = _clip(wire, OVERLAY_CLIP)
    start = int(overlay["start_frame"])
    end = start + int(overlay["duration_frames"])
    points = patch_sample_points(wire)
    by_frame = {
        frame: [item for item in points if item.output_frame == frame]
        for frame in (start - 1, start, end - 1, end)
    }
    for frame, stated in by_frame.items():
        labels = {item.label for item in stated}
        assert f"{OVERLAY_CLIP}@{frame}.field" in labels, (frame, sorted(labels))
    # Outside the span the overlay's own point states the ground: the primary's field alone.
    outside = next(item for item in by_frame[end] if item.label == f"{OVERLAY_CLIP}@{end}.field")
    inside = next(
        item for item in by_frame[end - 1] if item.label == f"{OVERLAY_CLIP}@{end - 1}.field"
    )
    assert (outside.canvas_x, outside.canvas_y) == (inside.canvas_x, inside.canvas_y)
    ground = composite_colour(wire, end, outside.canvas_x, outside.canvas_y)
    lit = composite_colour(wire, end - 1, inside.canvas_x, inside.canvas_y)
    assert ground != lit
    absent = copy.deepcopy(wire)
    absent["clips"] = [item for item in absent["clips"] if item["clip_id"] != OVERLAY_CLIP]
    assert composite_colour(absent, end, outside.canvas_x, outside.canvas_y) == ground

    # And the move itself now moves a stated colour: the overlay shifted one frame later states
    # the lit colour at the frame the unmoved composition states as ground, under the same label.
    moved = copy.deepcopy(wire)
    _clip(moved, OVERLAY_CLIP)["start_frame"] = start + 1
    taken = next(
        item for item in patch_sample_points(moved) if item.label == f"{OVERLAY_CLIP}@{end}.field"
    )
    assert (taken.canvas_x, taken.canvas_y) == (outside.canvas_x, outside.canvas_y)
    assert composite_colour(moved, end, taken.canvas_x, taken.canvas_y) == lit

    # A layer that spans the whole output has no ends to sample.
    assert not any(PRIMARY_CLIP in named for named in _layer_end_frames(wire).values())
    assert set(_layer_end_frames(wire)) == {start - 1, start, end - 1, end}


def test_a_row_whose_preview_the_product_scales_down_requires_no_browser_landmark() -> None:
    """The preview bound is the product's, and the browser side follows it exactly.

    `computePreviewSize` scales an output so that neither side exceeds 320 px and the area stays
    under 102,400 px. Every rendering row but one presents at scale 1; `output.dimensions_maximum`
    presents 1920x1080 at a sixth, where a two-pixel rectangle and a five-pixel interior sample
    do not exist. That row's browser side requires nothing, and every other row's requires what
    it always did.
    """

    assert preview_scale(_base()) == 1.0
    for case_id, scale in (
        ("output.dimensions_square", 1.0),
        ("output.dimensions_nonsquare", 1.0),
        ("output.dimensions_minimum", 1.0),
        ("output.dimensions_maximum", 1 / 6),
    ):
        recipe = BOOK.by_id[case_id]
        wire = _base()
        apply_edits(wire, recipe)
        assert abs(preview_scale(wire) - scale) < 1e-9, case_id
        assert browser_observable(wire) == (scale == 1.0), case_id
        empty = BrowserObservation(case_id=case_id)
        gaps = missing_landmarks(recipe, empty, wire, side="browser")
        if scale == 1.0:
            assert gaps, case_id
        else:
            assert gaps == (), case_id
        # The final side is never excused.
        assert missing_landmarks(
            recipe, FinalObservation(case_id=case_id, extractor_fingerprint="x"), wire, side="final"
        )


def test_a_tiny_canvas_is_still_observed_at_its_centre_on_its_quietest_frame() -> None:
    """A row that shrinks the output until no layer mark lands on it still has a picture.

    On a 16-pixel canvas the title's declared line box spans every row, so the steady-state frame
    has no flat point at all; the quietest frame, where the title and overlay have both ended, and
    the canvas centre are what keep the row from closing on its container.
    """

    wire = _base()
    wire["output"] = dict(wire["output"], width=16, height=16)
    points = patch_sample_points(wire)
    quiet = quiet_sample_frame(wire)
    assert quiet not in {patch_sample_frame(wire), *_ramp_sample_frames(wire)}
    assert points, "no sample point on the tiny canvas"
    assert all(point.output_frame == quiet for point in points), points
    assert f"{CANVAS_SAMPLE_LABEL}@{quiet}.center" in _labels(points)
    # And on the fixture's own canvas the quiet frame's points are additive, under their own frame.
    full = patch_sample_points(_base())
    assert f"{CANVAS_SAMPLE_LABEL}.center" in _labels(full)
    assert any(point.output_frame == quiet_sample_frame(_base()) for point in full)


def test_the_identity_reads_prescribe_the_declared_cells_and_carry_the_declared_table() -> None:
    """An observer is told where the row is, never where to search for it."""

    wire = _base()
    reads = identity_reads(wire)
    assert [read.output_frame for read in reads] == list(range(48))
    first = reads[0]
    assert len(first.cells) == FRAME_ID_BITS
    assert first.time_base_den == 12_288
    assert first.pts_by_frame[1] == 512
    stated = {item.output_frame: item for item in derive_source_mapping(wire)}
    assert stated[1].source_pts == first.pts_by_frame[1]

    # Reads are listed wherever the cells land on the canvas, legible or not: what is compared is
    # the oracle's decision, and the observer is not told which frames those are.
    washed = _base()
    _clip(washed, IMAGE_CLIP)["opacity_bp"] = 6_000
    _clip(washed, IMAGE_CLIP)["blend"] = "normal"
    assert len(identity_reads(washed)) == 48
    assert derive_source_mapping(washed) == ()

    gone = _base()
    _clip(gone, PRIMARY_CLIP)["transform"] = dict(
        _clip(gone, PRIMARY_CLIP)["transform"], position_x_bp=-40_000
    )
    assert identity_reads(gone) == ()


def test_a_layer_is_searched_for_at_its_quietest_frame() -> None:
    """Frame 0 is not the right frame for a composition whose title starts there."""

    wire = _base()
    assert [item.sample_frame for item in geometry_targets(wire)] == [0]
    early = _base()
    _clip(early, "clip-title")["start_frame"] = 0
    _clip(early, OVERLAY_CLIP)["start_frame"] = 0
    targets = geometry_targets(early)
    assert [item.clip_id for item in targets] == [PRIMARY_CLIP]
    assert targets[0].sample_frame == 20
