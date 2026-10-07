"""The `clip_audio` corpus family: a clip's gain, mute and fades, measured as levels of a tone.

Every piece between a row and its verdict is held here without a renderer: the tone source and its
PCM, the base derived from the accepted one, the 22 rows and the edits that reach them, the windows
the expectation states and the level it states there, the comparator's bound, the extractor's
measurement of a window, the join's reading of it and the render stage's prescription. The rows
themselves are rendered by the render stage on the pinned pair; the levels a real program produces
are held to the definition in `tests/test_m25_77_clip_audio_amplitude.py`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import struct
from dataclasses import FrozenInstanceError
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest

from comfyui_h3_context.core import semantic_conformance_media as media
from comfyui_h3_context.core.clip_audio import clip_audio_factor
from comfyui_h3_context.core.composition_contract import (
    ClipAudio,
    CompositionContractError,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.semantic_conformance import (
    CLIP_AUDIO_CASES,
    CLIP_AUDIO_DOMAINS,
    MAX_CASE_OUTPUT_FRAMES,
    MAX_CASE_PAYLOAD_BYTES,
    MAX_CORPUS_CASES,
    MAX_REPORT_BYTES,
    MISMATCH_CLASSES,
    NAMED_REFUSAL_CASES,
    TOLERANCE_PROFILE,
    TRANSFORM_DOMAINS,
    NumericDomain,
    SemanticConformanceError,
    build_corpus,
)
from comfyui_h3_context.core.semantic_conformance_cases import (
    BASE_FIXTURE_NAMES,
    BASE_NAMES,
    BASE_PRESENTATION,
    CLIP_AUDIO_BASE,
    CORPUS_BASE,
    EDIT_TARGETS,
    HANDBACK_CLIP,
    HANDOFF_CLIP,
    CaseRecipe,
    SnapshotEdit,
    apply_edits,
    build_recipes,
)
from comfyui_h3_context.core.semantic_conformance_compare import (
    AudioLevel,
    BrowserObservation,
    CaseOutcome,
    Expectation,
    FinalObservation,
    Measurement,
    audio_level_bound_ppm,
    compare_case,
)
from comfyui_h3_context.core.semantic_conformance_drive import run_setup
from comfyui_h3_context.core.semantic_conformance_expect import (
    AUDIO_LEVEL_EDGE_OFFSET_SAMPLES,
    AUDIO_LEVEL_WINDOW_SAMPLES,
    BROWSER_LANDMARKS,
    BROWSER_UNOBSERVABLE,
    FAMILY_LANDMARKS,
    AudioLevelWindow,
    AudioOwnerRun,
    ExpectationError,
    _plays_a_tone,
    audio_level_windows,
    audio_owner_runs,
    derive_audio_levels,
    derive_audio_onsets,
    derive_browser_expectation,
    derive_expectation,
    derive_source_mapping,
    identity_reads,
    required_landmarks,
)
from comfyui_h3_context.core.semantic_conformance_extract import pcm16_rms
from comfyui_h3_context.core.semantic_conformance_media import (
    AUDIO_SAMPLE_RATE,
    AUDIO_TONE_HZ,
    AUDIO_TONE_PEAK,
    OUTPUT_FRAME_TICKS,
    PRIMARY_ASSET,
    SOURCE_PROFILES,
    TONE_ASSET,
    TONE_SOURCE_FRAME_COUNT,
    SourceProfile,
    profile_for,
    scaled,
)
from scripts import nle_semantic_conformance as extractor
from scripts.m25_77_clip_audio_base_fixture import BASE_PATH, TARGET_PATH, build
from scripts.nle_semantic_render import (
    BASE_IMAGE_ASSET,
    BASE_VIDEO_ASSETS,
    Prescription,
    _audio_owner_windows,
    _build_audio_pcm,
    _level_windows,
)
from scripts.nle_semantic_report import final_observation

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BOOK = build_recipes()
ROWS = tuple(recipe for recipe in BOOK.recipes if recipe.family == "clip_audio")
RENDERING = tuple(recipe for recipe in ROWS if recipe.renders)
#: The clip's length in output samples (2,000 per frame).
LENGTH = TONE_SOURCE_FRAME_COUNT * 2_000

#: Digests of the burst PCM the render stage synthesized before the tone existed, for the lengths
#: the accepted sources use. The bursts must come out byte for byte as they did: every accepted
#: onset and silence row was measured on them.
BURST_PCM_DIGESTS = {
    48_000: "c055d90582a8b56bf8c16a1a878e51e413998bae5d81dc59ef64a09f97274ff4",  # noqa: E501  # pragma: allowlist secret
    144_000: "8870a600c92efca22728e327d7f5eecb8b609104088f0edad6692c3d3ea5b788",  # noqa: E501  # pragma: allowlist secret
    240_000: "6c8d09f286c03783a102c060cf80076408962c9100a070fdd3352f0b7f1b8b1d",  # noqa: E501  # pragma: allowlist secret
}


def _base() -> dict[str, Any]:
    document = json.loads((FIXTURES / BASE_FIXTURE_NAMES[CLIP_AUDIO_BASE]).read_text("utf-8"))
    return cast(dict[str, Any], copy.deepcopy(document["snapshot"]))


def _composition(recipe: CaseRecipe) -> dict[str, Any]:
    state = run_setup(recipe, _base()).state
    assert state is not None, recipe.case_id
    return cast(dict[str, Any], dict(state.snapshot.to_wire()))


def _edited(recipe: CaseRecipe) -> dict[str, Any]:
    wire = _base()
    apply_edits(wire, recipe)
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return wire


# --------------------------------------------------------------------------------------------
# The tone source
# --------------------------------------------------------------------------------------------


def test_the_tone_source_is_the_primary_picture_with_a_steady_tone() -> None:
    tone = profile_for(TONE_ASSET)
    primary = profile_for(PRIMARY_ASSET)
    assert tone is not None and primary is not None
    assert tone.audio_kind == "tone"
    assert primary.audio_kind == "bursts"
    assert tone.carries_audio and tone.carries_frame_id and tone.decoded_by_browser
    assert tone.frame_count == TONE_SOURCE_FRAME_COUNT == 248
    assert tone.constant_rate() == 24
    assert tone.pts_table()[-1] == (247, 247 * OUTPUT_FRAME_TICKS, OUTPUT_FRAME_TICKS)
    assert tone.field == primary.field
    assert [patch.rgb() for patch in tone.patches] == [patch.rgb() for patch in primary.patches]
    # A layer is attributed by its labels, so no other source carries the tone's.
    others = {
        patch.label
        for profile in SOURCE_PROFILES
        if profile is not tone
        for patch in profile.patches
    }
    assert not others & {patch.label for patch in tone.patches}
    assert all(patch.label.startswith("tone_") for patch in tone.patches)
    # Declared last, so every profile declared before it keeps its place.
    assert SOURCE_PROFILES[-1] is tone


def test_the_media_module_declares_the_tone_names_public() -> None:
    """Every name the module lists as public is bound in it, once, and the tone's are among them."""

    declared = media.__all__
    assert all(isinstance(name, str) and hasattr(media, name) for name in declared)
    assert len(set(declared)) == len(declared)
    assert {
        "AUDIO_KINDS",
        "AUDIO_TONE_HZ",
        "AUDIO_TONE_PEAK",
        "TONE_ASSET",
        "TONE_SOURCE_FRAME_COUNT",
    } <= set(declared)


def test_a_scaled_source_keeps_its_kind_of_sound_and_an_undeclared_kind_is_refused() -> None:
    tone = profile_for(TONE_ASSET)
    assert tone is not None
    assert scaled(tone, 2, "vid-tone-x2").audio_kind == "tone"
    with pytest.raises(ValueError):
        SourceProfile(
            asset_id="vid-noise",
            field=(0, 0, 0),
            patches=(),
            field_sample=(0, 0),
            field_span=1,
            carries_frame_id=False,
            carries_audio=True,
            decoded_by_browser=True,
            frame_count=1,
            audio_kind="noise",
        )


def test_the_tone_pcm_is_one_period_repeated_at_the_declared_level() -> None:
    period = AUDIO_SAMPLE_RATE // AUDIO_TONE_HZ
    assert period * AUDIO_TONE_HZ == AUDIO_SAMPLE_RATE == 48_000
    count = 4_800
    pcm = _build_audio_pcm(count, "tone")
    values = struct.unpack(f"<{count}h", pcm)
    assert values[:period] == values[period : 2 * period]
    assert max(abs(value) for value in values) == AUDIO_TONE_PEAK
    # 0.2 of full scale, so +12 dB (3.98x) still stays below it.
    assert AUDIO_TONE_PEAK == round(0.2 * 32_768)
    assert 3.99 * AUDIO_TONE_PEAK < 32_767
    rms = pcm16_rms(pcm, 960, AUDIO_LEVEL_WINDOW_SAMPLES)
    assert abs(rms - AUDIO_TONE_PEAK / math.sqrt(2)) < 0.5


@pytest.mark.parametrize("count", sorted(BURST_PCM_DIGESTS))
def test_the_burst_pcm_is_byte_for_byte_what_it_was(count: int) -> None:
    assert hashlib.sha256(_build_audio_pcm(count)).hexdigest() == BURST_PCM_DIGESTS[count]
    assert _build_audio_pcm(count, "bursts") == _build_audio_pcm(count)


# --------------------------------------------------------------------------------------------
# The base
# --------------------------------------------------------------------------------------------


def test_the_clip_audio_base_is_derived_from_the_accepted_base() -> None:
    accepted = json.loads(BASE_PATH.read_text(encoding="utf-8"))
    built = build(accepted)["snapshot"]
    stored = _base()
    assert built == stored
    assert TARGET_PATH.name == BASE_FIXTURE_NAMES[CLIP_AUDIO_BASE]
    decode_public_snapshot(copy.deepcopy(stored))
    assert stored["output"]["duration_frames"] == 248
    assert (stored["output"]["width"], stored["output"]["height"]) == (320, 180)
    assert stored["output"] == {**accepted["snapshot"]["output"], "duration_frames": 248}
    assert stored["public_fingerprint"] != accepted["snapshot"]["public_fingerprint"]
    assert stored["workspace_handle"] != accepted["snapshot"]["workspace_handle"]
    assert [asset["asset_id"] for asset in stored["assets"]] == [TONE_ASSET, "img-overlay"]
    assert [track["track_id"] for track in stored["tracks"]] == ["track-primary", "track-image"]
    clips = {clip["clip_id"]: clip for clip in stored["clips"]}
    assert set(clips) == {"clip-main", "clip-image"}
    assert (clips["clip-main"]["asset_id"], clips["clip-main"]["duration_frames"]) == (
        TONE_ASSET,
        248,
    )
    assert clips["clip-image"]["duration_frames"] == 248
    assert "audio" not in clips["clip-main"]


def test_the_tone_asset_declares_exactly_what_its_profile_encodes() -> None:
    tone = profile_for(TONE_ASSET)
    assert tone is not None
    asset = next(item for item in _base()["assets"] if item["asset_id"] == TONE_ASSET)
    assert asset["embedded_audio"] == "present_bound"
    assert asset["source_frame_count"] == tone.frame_count
    assert asset["source_sample_count"] == 248 * 2_000
    assert [
        (row["frame_index"], row["pts"], row["duration_ticks"]) for row in asset["landmarks"]
    ] == list(tone.pts_table())


def test_the_base_is_registered_with_every_stage_that_resolves_a_row() -> None:
    # By name: a recipe's wire and the report's records carry a row's base by this name.
    assert BASE_NAMES == ("corpus", "high_resolution", "clip_audio")
    assert BASE_PRESENTATION[CLIP_AUDIO_BASE] == BASE_PRESENTATION[CORPUS_BASE]
    assert BASE_VIDEO_ASSETS[CLIP_AUDIO_BASE] == (TONE_ASSET,)
    assert BASE_IMAGE_ASSET[CLIP_AUDIO_BASE] == "img-overlay"


# --------------------------------------------------------------------------------------------
# The rows
# --------------------------------------------------------------------------------------------


def test_the_family_expands_to_its_twenty_two_rows_on_its_own_base() -> None:
    corpus = build_corpus()
    # The 22 family rows and the two rows of the `set_clip_audio` command.
    assert len(corpus.cases) == 334
    leaves = [
        f"{domain.field}.{leaf}"
        for domain in CLIP_AUDIO_DOMAINS
        for leaf, _value in domain.distinct_boundaries()
    ]
    assert leaves == [
        "gain_mb.identity",
        "gain_mb.interior",
        "gain_mb.lower",
        "gain_mb.upper",
        "gain_mb.underflow",
        "gain_mb.overflow",
        "fade_in_frames.identity",
        "fade_in_frames.interior",
        "fade_in_frames.upper",
        "fade_in_frames.underflow",
        "fade_in_frames.overflow",
        "fade_out_frames.identity",
        "fade_out_frames.interior",
        "fade_out_frames.upper",
        "fade_out_frames.underflow",
        "fade_out_frames.overflow",
    ]
    expected = {f"clip_audio.{leaf}" for leaf in (*leaves, *CLIP_AUDIO_CASES)}
    in_corpus = {case.case_id for case in corpus.cases if case.family == "clip_audio"}
    assert in_corpus == expected
    assert {recipe.case_id for recipe in ROWS} == expected
    assert len(ROWS) == 22
    assert {recipe.base for recipe in ROWS} == {CLIP_AUDIO_BASE}
    assert len(RENDERING) == 14
    assert sum(recipe.identity_expected for recipe in ROWS) == 3
    assert {recipe.case_id for recipe in ROWS if recipe.refusal_code is not None} == {
        f"clip_audio.{field}.{leaf}"
        for field in ("gain_mb", "fade_in_frames", "fade_out_frames")
        for leaf in ("underflow", "overflow")
    } | {"clip_audio.fade_sum_overflow_refused", "clip_audio.unbound_audio_refused"}
    assert {
        "clip_audio.fade_sum_overflow_refused",
        "clip_audio.unbound_audio_refused",
    } <= NAMED_REFUSAL_CASES


def test_a_repeated_value_is_one_leaf_and_no_existing_family_repeats_one() -> None:
    fade = NumericDomain("fade_in_frames", 0, 240, 0, 12)
    assert [leaf for leaf, _ in fade.distinct_boundaries()] == [
        "identity",
        "interior",
        "upper",
        "underflow",
        "overflow",
    ]
    for domain in TRANSFORM_DOMAINS:
        assert domain.distinct_boundaries() == domain.boundaries(), domain.field


def test_the_domains_are_the_decoder_boundaries() -> None:
    """Each declared bound is admitted and the first value past it refused, by the real decoder."""

    def decode(field: str, value: int) -> None:
        wire = _base()
        audio = {"gain_mb": -600, "muted": False, "fade_in_frames": 0, "fade_out_frames": 0}
        audio[field] = value
        wire["clips"][0]["audio"] = audio
        wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
        decode_public_snapshot(wire)

    for domain in CLIP_AUDIO_DOMAINS:
        for value in (domain.interior, domain.lower, domain.upper):
            decode(domain.field, value)
        for value in (domain.underflow, domain.overflow):
            with pytest.raises(CompositionContractError) as refused:
                decode(domain.field, value)
            assert refused.value.code == "invalid_contract", (domain.field, value)


def test_every_refusing_row_is_refused_by_the_snapshot_decoder_with_its_code() -> None:
    for recipe in ROWS:
        if recipe.refusal_code is None:
            continue
        assert recipe.refusal_layer == "snapshot", recipe.case_id
        with pytest.raises(CompositionContractError) as refused:
            decode_public_snapshot(_edited(recipe))
        assert refused.value.code == recipe.refusal_code, recipe.case_id


def test_every_rendering_row_reaches_its_own_composition() -> None:
    base = _base()
    for recipe in RENDERING:
        wire = _composition(recipe)
        if recipe.identity_expected:
            assert wire["public_fingerprint"] == base["public_fingerprint"], recipe.case_id
        else:
            assert wire["public_fingerprint"] != base["public_fingerprint"], recipe.case_id


def test_an_audio_edit_writes_the_member_and_its_identity_as_absence() -> None:
    assert "clip.audio" in EDIT_TARGETS
    recipe = BOOK.by_id["clip_audio.combination"]
    wire = _edited(recipe)
    assert wire["clips"][0]["audio"] == {
        "gain_mb": -600,
        "muted": False,
        "fade_in_frames": 12,
        "fade_out_frames": 12,
    }
    back = CaseRecipe(
        case_id=recipe.case_id,
        case_class="property",
        family="clip_audio",
        observation="render_and_browser",
        base=CLIP_AUDIO_BASE,
        edits=(
            *recipe.edits,
            SnapshotEdit("clip.audio", "gain_mb", 0),
            SnapshotEdit("clip.audio", "fade_in_frames", 0),
            SnapshotEdit("clip.audio", "fade_out_frames", 0),
        ),
    )
    restored = _edited(back)
    assert "audio" not in restored["clips"][0]
    assert restored["public_fingerprint"] == _base()["public_fingerprint"]
    with pytest.raises(Exception, match="not a field"):
        apply_edits(
            _base(),
            CaseRecipe(
                case_id="clip_audio.muted",
                case_class="property",
                family="clip_audio",
                observation="render_and_browser",
                base=CLIP_AUDIO_BASE,
                edits=(SnapshotEdit("clip.audio", "pan", 1),),
            ),
        )


def test_only_an_audio_edit_is_applied_to_the_member() -> None:
    """An edit of any other target is still a plain field write, and a malformed member is named.

    An audio edit merges into the member the clip already carries, so a value there that is not an
    object is a malformed base, which the edit refuses rather than writes over.
    """

    def recipe(edit: SnapshotEdit) -> CaseRecipe:
        return CaseRecipe(
            case_id="clip_audio.muted",
            case_class="property",
            family="clip_audio",
            observation="render_and_browser",
            base=CLIP_AUDIO_BASE,
            edits=(edit,),
        )

    wire = _base()
    apply_edits(wire, recipe(SnapshotEdit("clip", "duration_frames", 200)))
    assert wire["clips"][0]["duration_frames"] == 200
    assert "audio" not in wire["clips"][0]
    malformed = _base()
    malformed["clips"][0]["audio"] = "loud"
    with pytest.raises(SemanticConformanceError, match="not an object"):
        apply_edits(malformed, recipe(SnapshotEdit("clip.audio", "gain_mb", -600)))


def test_the_hand_over_rows_place_their_second_clip_without_an_adjustment() -> None:
    handoff = _composition(BOOK.by_id["clip_audio.handoff_inside_fade_out"])
    clips = {clip["clip_id"]: clip for clip in handoff["clips"]}
    assert clips["clip-main"]["audio"]["fade_out_frames"] == 48
    assert "audio" not in clips[HANDOFF_CLIP]
    assert (clips[HANDOFF_CLIP]["start_frame"], clips[HANDOFF_CLIP]["duration_frames"]) == (
        224,
        24,
    )
    assert clips[HANDOFF_CLIP]["source_start_frame"] == 224
    runs = [
        (run.clip["clip_id"], run.start_frame, run.end_frame) for run in audio_owner_runs(handoff)
    ]
    assert runs == [("clip-main", 0, 224), (HANDOFF_CLIP, 224, 248)]

    handback = _composition(BOOK.by_id["clip_audio.handback_inside_fade_out"])
    clips = {clip["clip_id"]: clip for clip in handback["clips"]}
    assert "audio" not in clips[HANDBACK_CLIP]
    assert (clips[HANDBACK_CLIP]["start_frame"], clips[HANDBACK_CLIP]["duration_frames"]) == (
        160,
        52,
    )
    # The picture stays the base's: the second clip shows the source frame `clip-main` shows.
    assert clips[HANDBACK_CLIP]["source_start_frame"] == 160
    runs = [
        (run.clip["clip_id"], run.start_frame, run.end_frame) for run in audio_owner_runs(handback)
    ]
    # `clip-main` is heard again from frame 212, inside its 48-frame fade-out (200..248).
    assert runs == [("clip-main", 0, 160), (HANDBACK_CLIP, 160, 212), ("clip-main", 212, 248)]


def test_the_hand_over_rows_show_the_base_picture_once_per_frame() -> None:
    """The second clip changes who is heard and nothing that is seen.

    Over the frames the two primary clips share, both show one source frame, so the identity row
    reads legibly through both; the expectation states each output frame once, from the clip
    above, and the frames it states are the base's. The observers get one read per frame too.
    """

    base = derive_source_mapping(_base())
    assert len({item.output_frame for item in base}) == len(base) == 248
    for case_id in ("clip_audio.handoff_inside_fade_out", "clip_audio.handback_inside_fade_out"):
        wire = _composition(BOOK.by_id[case_id])
        assert derive_source_mapping(wire) == base, case_id
        reads = identity_reads(wire)
        assert [read.output_frame for read in reads] == list(range(248)), case_id
        second = HANDOFF_CLIP if case_id.endswith("handoff_inside_fade_out") else HANDBACK_CLIP
        clip = next(item for item in wire["clips"] if item["clip_id"] == second)
        shared = range(clip["start_frame"], clip["start_frame"] + clip["duration_frames"])
        assert {read.clip_id for read in reads if read.output_frame in shared} == {second}
        assert {read.clip_id for read in reads if read.output_frame not in shared} == {"clip-main"}
        # Each read names the asset whose landmark table states its index: both clips show the tone.
        assert {read.asset_id for read in reads} == {TONE_ASSET}, case_id


def test_only_a_primary_clip_owns_the_audio() -> None:
    """A clip on another track never owns the sound, however late it starts."""

    wire = _base()
    image = next(clip for clip in wire["clips"] if clip["clip_id"] == "clip-image")
    image.update(start_frame=100, duration_frames=148)
    runs = [(run.clip["clip_id"], run.start_frame, run.end_frame) for run in audio_owner_runs(wire)]
    assert runs == [("clip-main", 0, 248)]


# --------------------------------------------------------------------------------------------
# What a row expects
# --------------------------------------------------------------------------------------------


def test_only_a_declared_tone_source_plays_a_tone() -> None:
    """A source no profile declares is not a tone source, and asking does not fail.

    The burst owners and the level windows both ask it of every audio-bearing primary clip, so a
    source the corpus did not synthesize must get an answer rather than an error.
    """

    assert _plays_a_tone({"asset_id": TONE_ASSET})
    assert not _plays_a_tone({"asset_id": PRIMARY_ASSET})
    assert profile_for("vid-undeclared") is None
    assert not _plays_a_tone({"asset_id": "vid-undeclared"})


def test_the_family_requires_levels_on_the_final_side_and_explains_the_browser() -> None:
    assert FAMILY_LANDMARKS["clip_audio"] == ("audio_levels",)
    assert "audio_levels" not in BROWSER_LANDMARKS
    assert BROWSER_UNOBSERVABLE["audio_levels"].strip()
    # B-M2577-01: the preview does construct an `AudioContext` now; the reason says what is true.
    onsets = BROWSER_UNOBSERVABLE["audio_onsets"]
    assert "constructs no `AudioContext`" not in onsets
    assert "AudioContext" in onsets and "AnalyserNode" in onsets
    for recipe in RENDERING:
        wire = _composition(recipe)
        assert "audio_levels" in required_landmarks(recipe, wire), recipe.case_id
        assert derive_audio_levels(wire), recipe.case_id
        # A tone has no bursts: no onset is stated for it.
        assert derive_audio_onsets(wire) == (), recipe.case_id
    # The whole expectation of a 248-frame composition takes seconds to derive, and the windows of
    # every row are held to the definition below, so the expectations are taken of one row with one
    # owner and one with three: the final side states the windows, the browser side none.
    for case_id in ("clip_audio.combination", "clip_audio.handback_inside_fade_out"):
        recipe = BOOK.by_id[case_id]
        wire = _composition(recipe)
        assert derive_expectation(recipe, wire).audio_levels == derive_audio_levels(wire)
        assert derive_browser_expectation(recipe, wire).audio_levels == (), case_id


def test_every_window_lies_inside_one_owner_run_clear_of_its_ends() -> None:
    clearance = TOLERANCE_PROFILE.codec_boundary_exclusion_samples
    for recipe in RENDERING:
        wire = _composition(recipe)
        runs = audio_owner_runs(wire)
        windows = audio_level_windows(wire)
        assert windows, recipe.case_id
        labels = [window.label for window in windows]
        assert len(labels) == len(set(labels)), recipe.case_id
        for window in windows:
            assert window.sample_count == AUDIO_LEVEL_WINDOW_SAMPLES
            owning = [
                run
                for run in runs
                if run.start_frame * 2_000 + clearance <= window.start_sample
                and window.start_sample + window.sample_count <= run.end_frame * 2_000 - clearance
            ]
            assert len(owning) == 1, (recipe.case_id, window.label)
            assert owning[0].clip["clip_id"] == window.clip_id, (recipe.case_id, window.label)
        # Every run is measured at its start and its end, where they fit.
        for run in runs:
            starts = {w.start_sample for w in windows if w.clip_id == run.clip["clip_id"]}
            assert run.start_frame * 2_000 + AUDIO_LEVEL_EDGE_OFFSET_SAMPLES in starts
            assert (
                run.end_frame * 2_000 - AUDIO_LEVEL_EDGE_OFFSET_SAMPLES - AUDIO_LEVEL_WINDOW_SAMPLES
                in starts
            )


def _levels(case_id: str) -> dict[int, int]:
    wire = _composition(BOOK.by_id[case_id])
    return {level.start_sample: level.level_ppm for level in derive_audio_levels(wire)}


@pytest.mark.parametrize(
    "member",
    ["loud", {"gain_mb": 0, "muted": "no", "fade_in_frames": 0, "fade_out_frames": 0}],
    ids=["not-an-object", "mute-not-a-boolean"],
)
def test_an_unreadable_audio_member_is_named_rather_than_read(member: object) -> None:
    wire = _base()
    wire["clips"][0]["audio"] = member
    with pytest.raises(ExpectationError, match="unreadable audio member"):
        derive_audio_levels(wire)


def test_no_level_is_stated_for_an_owner_whose_source_carries_no_sound() -> None:
    wire = _base()
    asset = next(item for item in wire["assets"] if item["asset_id"] == TONE_ASSET)
    asset["embedded_audio"] = "absent"
    # The clip still owns the frames; there is just no sound in them to have a level.
    assert [run.clip["clip_id"] for run in audio_owner_runs(wire)] == ["clip-main"]
    assert audio_level_windows(wire) == ()
    # Nor for an owner whose asset the wire does not declare: nothing is read off a missing row.
    missing = _base()
    missing["clips"][0]["asset_id"] = "vid-undeclared"
    assert audio_level_windows(missing) == ()


def test_no_level_is_stated_at_a_rate_the_definition_does_not_state() -> None:
    """The windows are laid out in samples of 2,000 per frame; any other rate is refused."""

    wire = _base()
    wire["output"]["sample_rate"] = 44_100
    with pytest.raises(ExpectationError, match="2,000 samples per frame"):
        derive_audio_levels(wire)


def test_a_run_and_a_window_are_frozen_values() -> None:
    wire = _composition(BOOK.by_id["clip_audio.combination"])
    run = audio_owner_runs(wire)[0]
    with pytest.raises(FrozenInstanceError):
        run.end_frame = 0  # type: ignore[misc]
    # Both are built by name only, so the slots are compared as sets: their order is not a claim.
    assert set(AudioOwnerRun.__slots__) == {"clip", "start_frame", "end_frame"}
    window = audio_level_windows(wire)[0]
    with pytest.raises(FrozenInstanceError):
        window.clip_sample = 0  # type: ignore[misc]
    assert set(AudioLevelWindow.__slots__) == {
        "label",
        "start_sample",
        "sample_count",
        "clip_id",
        "clip_sample",
    }


def test_a_short_fade_keeps_its_windows_clear_of_the_run_ends() -> None:
    """The corpus fades are long; a fade of a few frames puts quarter points beside the run's ends.

    A 4-frame fade-out's third quarter starts 2,480 samples before the end, so its window would end
    1,520 before it, inside the codec boundary exclusion: it is not stated, and neither is any
    window of any fade from 1 to 12 frames that would reach into the exclusion at either end.
    """

    clearance = TOLERANCE_PROFILE.codec_boundary_exclusion_samples
    for frames in range(1, 13):
        for field in ("fade_in_frames", "fade_out_frames"):
            wire = _base()
            audio = {"gain_mb": 0, "muted": False, "fade_in_frames": 0, "fade_out_frames": 0}
            audio[field] = frames
            wire["clips"][0]["audio"] = audio
            for window in audio_level_windows(wire):
                assert window.start_sample >= clearance, (field, frames, window)
                assert window.start_sample + window.sample_count <= LENGTH - clearance, (
                    field,
                    frames,
                    window,
                )


def test_the_stated_level_is_the_definition_over_the_window() -> None:
    # A constant gain is the same level everywhere: -6 dB, +12 dB, -60 dB and the identity.
    assert set(_levels("clip_audio.gain_mb.interior").values()) == {501_187}
    assert set(_levels("clip_audio.gain_mb.upper").values()) == {3_981_072}
    assert set(_levels("clip_audio.gain_mb.lower").values()) == {1_000}
    assert set(_levels("clip_audio.gain_mb.identity").values()) == {1_000_000}
    assert set(_levels("clip_audio.muted").values()) == {0}
    # A fade's quarter points sit at a quarter, a half and three quarters of the ramp.
    fade_in = _levels("clip_audio.fade_in_frames.interior")
    assert [fade_in[start] for start in (5_520, 11_520, 17_520)] == [250_246, 500_112, 750_068]
    fade_out = _levels("clip_audio.fade_out_frames.interior")
    assert [fade_out[start] for start in (477_520, 483_520, 489_520)] == [750_110, 500_154, 250_287]
    combination = _levels("clip_audio.combination")
    assert combination[247_520] == 501_187
    audio = ClipAudio(-600, False, 12, 12)
    for start, level in combination.items():
        expected = math.sqrt(
            sum(clip_audio_factor(audio, 248, k) ** 2 for k in range(start, start + 960)) / 960
        )
        assert level == round(expected * 1_000_000), start


def test_a_hand_over_states_each_owner_at_its_own_clip_relative_level() -> None:
    handoff = _levels("clip_audio.handoff_inside_fade_out")
    # `clip-main` is cut inside its fade-out, where the ramp stands; the new owner is unadjusted.
    assert handoff[423_520] == 750_011
    assert handoff[444_480] == 531_680
    assert [handoff[start] for start in (450_560, 471_520, 492_480)] == [1_000_000] * 3
    handback = _levels("clip_audio.handback_inside_fade_out")
    # `clip-main` resumes at frame 212 (sample 424,000) mid-ramp, not from the ramp's start.
    assert [handback[start] for start in (322_560, 371_520, 420_480)] == [1_000_000] * 3
    assert [handback[start] for start in (426_560, 447_520, 459_520, 471_520, 492_480)] == [
        718_344,
        500_014,
        375_016,
        250_022,
        31_803,
    ]


# --------------------------------------------------------------------------------------------
# The comparator
# --------------------------------------------------------------------------------------------

LEVEL = AudioLevel("clip-main@2560", 2_560, 960, 501_187)


def _compare(*observed: AudioLevel) -> Any:
    expectation = Expectation(case_id="clip_audio.gain_mb.interior", audio_levels=(LEVEL,))
    final = FinalObservation(
        case_id="clip_audio.gain_mb.interior",
        extractor_fingerprint="sha256:" + "0" * 64,
        audio_levels=observed,
    )
    browser = BrowserObservation(case_id="clip_audio.gain_mb.interior")
    return compare_case(expectation, browser, final, TOLERANCE_PROFILE, sides=("final",))


def test_a_level_is_one_frozen_value() -> None:
    with pytest.raises(FrozenInstanceError):
        LEVEL.level_ppm = 0  # type: ignore[misc]
    assert AudioLevel.__slots__ == ("label", "start_sample", "sample_count", "level_ppm")


def test_a_level_passes_at_its_bound_and_fails_one_millionth_past_it() -> None:
    assert "audio_level" in MISMATCH_CLASSES
    bound = audio_level_bound_ppm(LEVEL.level_ppm, TOLERANCE_PROFILE)
    assert bound == max(
        Fraction(TOLERANCE_PROFILE.audio_level_floor_ppm),
        Fraction(LEVEL.level_ppm * TOLERANCE_PROFILE.audio_level_relative_error_ppm, 1_000_000),
    )
    inside = math.floor(LEVEL.level_ppm + bound)
    outside = math.floor(LEVEL.level_ppm + bound) + 1
    passed = _compare(AudioLevel(LEVEL.label, 2_560, 960, inside))
    assert passed.status == "PASS"
    # A passing level is kept with its bound, as every measured fact is.
    assert [(item.subject, item.value, item.bound) for item in passed.measurements] == [
        ("final.audio_level.clip-main@2560", str(inside), str(bound))
    ]
    failed = _compare(AudioLevel(LEVEL.label, 2_560, 960, outside))
    assert failed.status == "MISMATCH"
    assert [item.mismatch_class for item in failed.mismatches] == ["audio_level"]
    below = math.ceil(LEVEL.level_ppm - bound) - 1
    assert _compare(AudioLevel(LEVEL.label, 2_560, 960, below)).status == "MISMATCH"


def test_near_silence_the_floor_holds_and_silence_is_not_a_level() -> None:
    quiet = AudioLevel("clip-main@2560", 2_560, 960, 1_000)
    floor = TOLERANCE_PROFILE.audio_level_floor_ppm
    assert audio_level_bound_ppm(0, TOLERANCE_PROFILE) == floor
    assert audio_level_bound_ppm(quiet.level_ppm, TOLERANCE_PROFILE) == floor
    expectation = Expectation(case_id="clip_audio.muted", audio_levels=(quiet,))

    def judged(level_ppm: int) -> str:
        final = FinalObservation(
            case_id="clip_audio.muted",
            extractor_fingerprint="sha256:" + "0" * 64,
            audio_levels=(AudioLevel(quiet.label, 2_560, 960, level_ppm),),
        )
        return compare_case(
            expectation,
            BrowserObservation(case_id="clip_audio.muted"),
            final,
            TOLERANCE_PROFILE,
            sides=("final",),
        ).status

    assert judged(quiet.level_ppm + floor) == "PASS"
    assert judged(quiet.level_ppm + floor + 1) == "MISMATCH"
    # A unity window where the clip should be nearly silent is far outside the floor, and so is a
    # silent one where the clip should play at -60 dB: the floor is below the quietest level.
    assert judged(1_000_000) == "MISMATCH"
    assert judged(0) == "MISMATCH"


def test_an_absent_window_or_one_measured_elsewhere_is_a_mismatch() -> None:
    absent = _compare()
    assert absent.status == "MISMATCH"
    # Recorded as every family records a landmark it did not observe.
    assert [
        (item.mismatch_class, item.subject, item.expected, item.observed)
        for item in absent.mismatches
    ] == [("audio_level", "final.audio_level.clip-main@2560", "present", "absent")]
    # A level measured over another window is not judged as this window's level at all: only the
    # window is reported, however far the level is from this window's.
    moved = _compare(AudioLevel(LEVEL.label, 3_000, 960, 0))
    assert moved.status == "MISMATCH"
    assert [
        (item.mismatch_class, item.subject, item.expected, item.observed)
        for item in moved.mismatches
    ] == [("audio_level", "final.audio_level.clip-main@2560.window", "2560+960", "3000+960")]
    shorter = _compare(AudioLevel(LEVEL.label, 2_560, 480, LEVEL.level_ppm))
    assert shorter.status == "MISMATCH"


def test_the_tolerance_profile_carries_both_level_bounds() -> None:
    wire = TOLERANCE_PROFILE.as_wire()
    assert (
        wire["audio_level_relative_error_ppm"] == TOLERANCE_PROFILE.audio_level_relative_error_ppm
    )
    assert wire["audio_level_floor_ppm"] == TOLERANCE_PROFILE.audio_level_floor_ppm
    # Frozen from the calibration on the pinned pair (see `ToleranceProfile`): 5 % and 100 ppm.
    assert TOLERANCE_PROFILE.audio_level_relative_error_ppm == 50_000
    assert TOLERANCE_PROFILE.audio_level_floor_ppm == 100


# --------------------------------------------------------------------------------------------
# The extractor, the join and the render stage's prescription
# --------------------------------------------------------------------------------------------


def test_the_extractor_measures_a_window_against_the_declared_tone() -> None:
    tone = _build_audio_pcm(9_600, "tone")
    half = struct.pack("<9600h", *(round(value / 2) for value in struct.unpack("<9600h", tone)))
    windows = [
        {"label": "a@960", "start_sample": 960, "sample_count": 960},
        {"label": "b@9000", "start_sample": 9_000, "sample_count": 960},
    ]
    full = extractor._audio_levels(tone, windows)
    # The second window runs past the decoded samples and is absent, not shortened.
    assert [entry["label"] for entry in full] == ["a@960"]
    assert abs(full[0]["level_ppm"] - 1_000_000) <= 100
    assert full[0]["start_sample"] == 960 and full[0]["sample_count"] == 960
    halved = extractor._audio_levels(half, windows[:1])
    assert abs(halved[0]["level_ppm"] - 500_000) <= 100
    with pytest.raises(Exception, match="past the decoded samples"):
        pcm16_rms(tone, 9_000, 960)
    # The buffer's own first and last windows are whole windows, and are measured.
    for start in (0, 9_600 - 960):
        assert abs(pcm16_rms(tone, start, 960) - AUDIO_TONE_PEAK / math.sqrt(2)) < 0.5, start
    # So is a one-sample window: its level is that sample's magnitude.
    assert pcm16_rms(tone, 12, 1) == abs(struct.unpack("<9600h", tone)[12])
    # A range that names no window is refused, never measured as an empty or a negative one.
    for start, count in ((-1, 960), (0, 0), (0, -960)):
        with pytest.raises(SemanticConformanceError, match="invalid sample range"):
            pcm16_rms(tone, start, count)


def test_the_join_reads_every_measured_window() -> None:
    observation = {
        "frame_grid": {
            "width": 320,
            "height": 180,
            "frame_count": 248,
            "frame_rate_num": 24,
            "frame_rate_den": 1,
        },
        "output_metadata": {
            "container": "mp4",
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "width": 320,
            "height": 180,
            "pixel_aspect_num": 1,
            "pixel_aspect_den": 1,
            "color_policy": "bt709_sdr_limited_v1",
            "frame_rate_num": 24,
            "frame_rate_den": 1,
            "audio_stream_count": 1,
            "audio_codec": "aac",
            "sample_rate": 48_000,
            "channels": 1,
        },
        "extractor_fingerprint": "sha256:" + "0" * 64,
        "audio_levels": [
            {"label": "clip-main@2560", "start_sample": 2_560, "sample_count": 960, "level_ppm": 7}
        ],
    }
    record = {"status": "OBSERVED", "phases": [{"status": "OBSERVED", "observation": observation}]}
    final = final_observation("clip_audio.muted", record)
    assert final.audio_levels == (AudioLevel("clip-main@2560", 2_560, 960, 7),)


def _levels_only(wire: dict[str, Any]) -> Prescription:
    """A prescription of level windows alone; the picture's points are another test's subject."""

    return Prescription(
        points=(), reads=(), targets=(), alpha_targets=(), level_windows=tuple(_level_windows(wire))
    )


def test_the_render_stage_prescribes_the_expectation_windows_and_no_onset_scan() -> None:
    for recipe in RENDERING:
        wire = _composition(recipe)
        assert [
            (window["label"], window["start_sample"], window["sample_count"])
            for window in _level_windows(wire)
        ] == [
            (level.label, level.start_sample, level.sample_count)
            for level in derive_audio_levels(wire)
        ], recipe.case_id
        assert _audio_owner_windows(wire) == [], recipe.case_id
    # The prescription the stage renders with carries exactly those windows.
    wire = _composition(BOOK.by_id["clip_audio.handback_inside_fade_out"])
    assert Prescription.for_wire(wire).level_windows == tuple(_level_windows(wire))


def test_a_command_phase_is_measured_at_the_other_phase_windows_too() -> None:
    before = _levels_only(_composition(BOOK.by_id["clip_audio.gain_mb.identity"]))
    after = _levels_only(_composition(BOOK.by_id["clip_audio.combination"]))
    joined = before.joined_with(after)
    labels = [window["label"] for window in joined.level_windows]
    assert len(labels) == len(set(labels))
    assert set(labels) == {
        window["label"] for window in (*before.level_windows, *after.level_windows)
    }
    assert labels[: len(before.level_windows)] == [w["label"] for w in before.level_windows]


def test_the_burst_sources_are_prescribed_and_stated_exactly_as_before() -> None:
    """The corpus base is untouched: its owners keep their onset windows and state no levels."""

    document = json.loads((FIXTURES / BASE_FIXTURE_NAMES[CORPUS_BASE]).read_text("utf-8"))
    wire = document["snapshot"]
    assert audio_level_windows(wire) == ()
    assert len(_audio_owner_windows(wire)) == 1
    assert derive_audio_onsets(wire)


def test_the_case_payload_budget_holds_a_row_at_the_frame_ceiling() -> None:
    """The join refuses the whole report when one row outgrows `MAX_CASE_PAYLOAD_BYTES`.

    A render row records three source-mapping measurements per output frame, so the budget has to
    hold a row at the corpus's own frame ceiling. 65,536 did not even hold the 248-frame
    `clip_audio` base's rows (69,472 to 70,245 bytes, every one PASS), and the join stopped. Long
    values stand in for the observed ones, and 8 KiB stands in for the fixed measurements (about
    7 KB on those rows). The two ceilings are sized together: every admitted case at its budget
    still fits the report.
    """

    measurements: list[Measurement] = []
    for frame in range(MAX_CASE_OUTPUT_FRAMES):
        measurements += [
            Measurement(f"final.source_mapping.{frame}.source_frame", "65535"),
            Measurement(f"browser.source_mapping.{frame}.source_frame", "65535"),
            Measurement(
                f"browser.source_mapping.{frame}.drift", "99999/99999999", "99999/99999999"
            ),
        ]
    fixed = 0
    while fixed < 8_192:
        item = Measurement(f"final.fixed.{fixed}", "v" * 40, "b" * 40)
        measurements.append(item)
        fixed += len(json.dumps(item.as_wire()).encode("utf-8"))
    row = CaseOutcome(
        "clip_audio.handback_inside_fade_out", "PASS", measurements=tuple(measurements)
    ).as_wire()
    encoded = json.dumps(row, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= MAX_CASE_PAYLOAD_BYTES
    assert MAX_CORPUS_CASES * MAX_CASE_PAYLOAD_BYTES <= MAX_REPORT_BYTES
