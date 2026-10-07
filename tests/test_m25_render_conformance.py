"""M25-20 semantic conformance: corpus closure, decoder-bound binding, and comparator sensitivity.

Three separable jobs live here, and the middle one is the reason this file exists at all.

1. **The corpus is closed.** Every accepted command has its own accepted and refused case, every
   property family expands to individually named boundary leaves, and the classes are counted
   separately so a UI invariant can never be reported as command coverage.
2. **The declared numeric domains are the product's actual bounds.** `semantic_conformance` restates
   the bounds that `composition_contract` enforces as literals inside its decoders. Restating is a
   liability unless something proves the restatement, so every domain is driven through the real
   decoder at its inclusive bound and one step past it. Move a product bound without moving the
   domain and these tests go red -- which is the point.
3. **The comparator is actually sensitive.** Each threshold is exercised at the bound and just
   beyond it, along with the failure shapes that a permissive comparator would wave through: a
   missing landmark, a wrong source frame inside a generous tolerance, a swapped before/after
   artifact, an edge pixel sampled as an interior patch, a receipt-shaped observation with nothing
   in it, and a uniformly black output.

These are fixture-driven comparator tests. They pin sensitivity; they are never a substitute for the
real runtime qualification, and nothing here executes a browser or a renderer.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from comfyui_h3_context.core.composition_contract import (
    NLE_OPERATION_IDS,
    CompositionContractError,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.semantic_conformance import (
    AUDIO_NEGATIVE_SURFACES,
    CASE_CLASSES,
    CLIP_AUDIO_DOMAINS,
    CORPUS_VERSION,
    CROP_DOMAIN,
    CROP_EDGES,
    DEFERRED_AUDIO_SEAMS,
    EDGE_TRIM_PROPERTY_IDS,
    EFFECT_DOMAINS,
    MAX_CORPUS_CASES,
    NAMED_REFUSAL_CASES,
    NON_DESTROY_CLOSE_REASONS,
    OPACITY_DOMAIN,
    REPORT_STATUSES,
    SIDEBAR_UI_INVARIANT_IDS,
    TEXT_LINE_HEIGHT_DOMAIN,
    TEXT_SIZE_DOMAIN,
    TOLERANCE_PROFILE,
    TOLERANCE_VERSION,
    TRANSFORM_DOMAINS,
    NumericDomain,
    SemanticConformanceError,
    build_corpus,
    join_control_manifest,
)
from comfyui_h3_context.core.semantic_conformance_compare import (
    OUTPUT_SAMPLE_RATE,
    AlphaSample,
    AudioOnset,
    AvPair,
    BrowserObservation,
    CaseOutcome,
    Expectation,
    FinalObservation,
    FrameGrid,
    GeometryLandmark,
    OutputMetadata,
    PatchSample,
    PresentedFrame,
    SilenceInterval,
    SourceLandmark,
    TextObservation,
    compare_case,
    summarize,
)
from comfyui_h3_context.core.semantic_conformance_extract import (
    ACCEPTED_COLOR_POLICY,
    MAX_TIMING_ENTRIES,
    PCM16_BYTES_PER_SAMPLE,
    UNKNOWN_COLOR_POLICY,
    detect_onsets,
    parse_frame_grid,
    parse_frame_timing,
    parse_output_metadata,
    patch_mean,
    pcm16_peak,
    read_frame_id_tile,
    silence_interval,
)
from scripts import nle_semantic_conformance as qualification
from scripts import nle_semantic_conformance_manifest as manifest_generator

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "m25_10_composition_contract_v1.json"
CONTROL_MANIFEST_PATH = ROOT / "governance" / "contracts" / "nle_control_coverage_manifest_v1.json"

TITLE_CLIP = 3
MAIN_CLIP = 0


# --------------------------------------------------------------------------------------------
# Decoder-bound binding
# --------------------------------------------------------------------------------------------


def _snapshot() -> dict[str, Any]:
    document = cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
    return cast(dict[str, Any], document["snapshot"])


def _resign(wire: dict[str, Any]) -> dict[str, Any]:
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return wire


def _decode_with(mutate: Callable[[dict[str, Any]], None]) -> None:
    wire = _snapshot()
    mutate(wire)
    decode_public_snapshot(_resign(wire))


def _clip_field_setter(clip: int, group: str, field: str) -> Callable[[int], Callable[..., None]]:
    def make(value: int) -> Callable[[dict[str, Any]], None]:
        def mutate(wire: dict[str, Any]) -> None:
            wire["clips"][clip][group][field] = value

        return mutate

    return make


def _assert_domain_matches_decoder(
    domain: NumericDomain, make: Callable[[int], Callable[[dict[str, Any]], None]]
) -> None:
    """The inclusive bounds are accepted and the first value past each is refused."""

    for admitted in (domain.identity, domain.interior, domain.lower, domain.upper):
        _decode_with(make(admitted))
    for rejected in (domain.underflow, domain.overflow):
        with pytest.raises(CompositionContractError):
            _decode_with(make(rejected))


@pytest.mark.parametrize("domain", TRANSFORM_DOMAINS, ids=lambda item: item.field)
def test_declared_transform_domain_is_the_decoder_boundary(domain: NumericDomain) -> None:
    _assert_domain_matches_decoder(domain, _clip_field_setter(MAIN_CLIP, "transform", domain.field))


@pytest.mark.parametrize("edge", CROP_EDGES)
def test_declared_crop_domain_is_the_decoder_boundary(edge: str) -> None:
    _assert_domain_matches_decoder(CROP_DOMAIN, _clip_field_setter(MAIN_CLIP, "crop", edge))


def test_crop_sum_constraints_are_two_separate_negatives() -> None:
    # A single "sum" case would be satisfied by whichever axis the decoder happens to test first.
    def set_crop(values: tuple[int, int, int, int]) -> Callable[[dict[str, Any]], None]:
        def mutate(wire: dict[str, Any]) -> None:
            wire["clips"][MAIN_CLIP]["crop"] = dict(zip(CROP_EDGES, values, strict=True))

        return mutate

    for values in ((5_000, 0, 5_000, 0), (0, 5_000, 0, 5_000)):
        with pytest.raises(CompositionContractError):
            _decode_with(set_crop(values))


def test_declared_opacity_domain_is_the_decoder_boundary() -> None:
    def make(value: int) -> Callable[[dict[str, Any]], None]:
        def mutate(wire: dict[str, Any]) -> None:
            wire["clips"][MAIN_CLIP]["opacity_bp"] = value

        return mutate

    _assert_domain_matches_decoder(OPACITY_DOMAIN, make)


@pytest.mark.parametrize(
    "domain", (TEXT_SIZE_DOMAIN, TEXT_LINE_HEIGHT_DOMAIN), ids=lambda item: item.field
)
def test_declared_text_domain_is_the_decoder_boundary(domain: NumericDomain) -> None:
    _assert_domain_matches_decoder(domain, _clip_field_setter(TITLE_CLIP, "text", domain.field))


@pytest.mark.parametrize("domain", EFFECT_DOMAINS, ids=lambda item: item.field)
def test_declared_effect_domain_is_the_decoder_boundary(domain: NumericDomain) -> None:
    def make(value: int) -> Callable[[dict[str, Any]], None]:
        def mutate(wire: dict[str, Any]) -> None:
            # Non-identity values require the color-adjust kind; `none` must stay at identity.
            effect = wire["clips"][MAIN_CLIP]["effect"]
            effect["kind"] = "color_adjust_v1"
            effect[domain.field] = value

        return mutate

    _assert_domain_matches_decoder(domain, make)


def test_identity_effect_kind_rejects_non_identity_values() -> None:
    with pytest.raises(CompositionContractError):
        _decode_with(
            lambda wire: wire["clips"][MAIN_CLIP]["effect"].update(
                kind="none", brightness_permille=250
            )
        )


# --------------------------------------------------------------------------------------------
# Corpus closure
# --------------------------------------------------------------------------------------------


def test_corpus_is_closed_bounded_and_counted_by_class() -> None:
    corpus = build_corpus()
    assert corpus.version == CORPUS_VERSION
    assert len(corpus.cases) <= MAX_CORPUS_CASES
    assert len(corpus.case_ids) == len(set(corpus.case_ids))
    counts = corpus.counts_by_class()
    assert set(counts) == set(CASE_CLASSES)
    # Class counts are reported separately and never summed into a coverage number.
    assert counts["command"] == 2 * len(NLE_OPERATION_IDS)
    assert counts["ui_invariant"] == len(SIDEBAR_UI_INVARIANT_IDS) - 1 + len(
        NON_DESTROY_CLOSE_REASONS
    )
    assert counts["import_integration"] == 2
    assert counts["deferred_negative"] == len(DEFERRED_AUDIO_SEAMS) + len(AUDIO_NEGATIVE_SURFACES)
    assert counts["property"] == len(corpus.cases) - sum(
        counts[name] for name in CASE_CLASSES if name != "property"
    )


def test_every_accepted_command_has_its_own_accepted_and_refused_case() -> None:
    corpus = build_corpus()
    assert sorted(corpus.commands()) == sorted(NLE_OPERATION_IDS)
    for command in NLE_OPERATION_IDS:
        assert f"command.{command}.accepted" in corpus.case_ids
        assert f"command.{command}.refused" in corpus.case_ids
    refusals = [
        case for case in corpus.cases if case.case_class == "command" and case.leaf == "refused"
    ]
    assert all(case.refusal_expected for case in refusals)


def test_edge_trim_rides_the_existing_trim_command_and_never_adds_command_ids() -> None:
    corpus = build_corpus()
    assert "trim_clip" in corpus.commands()
    for property_id in EDGE_TRIM_PROPERTY_IDS:
        case = next(item for item in corpus.cases if item.case_id == f"edge_trim.{property_id}")
        assert case.case_class == "property"
        assert case.command is None
    assert not any(item.startswith("command.edge_trim") for item in corpus.case_ids)


def test_ui_invariant_rows_can_never_stand_in_for_a_command_row() -> None:
    corpus = build_corpus()
    ui_cases = [case for case in corpus.cases if case.case_class == "ui_invariant"]
    assert {case.command for case in ui_cases} == {None}
    for reason in NON_DESTROY_CLOSE_REASONS:
        assert f"ui_invariant.overlay_close_return_focus.{reason}" in corpus.case_ids
    # `view_destroy` is its own row; it must not also appear as a return-focus close reason.
    assert "ui_invariant.overlay_close_return_focus.view_destroy" not in corpus.case_ids
    assert "ui_invariant.view_destroy_cleanup" in corpus.case_ids


def test_the_imported_source_case_keeps_the_name_the_plan_froze() -> None:
    ids = build_corpus().case_ids
    for gesture in ("pointer", "keyboard"):
        assert f"import_integration.generated_source.explicit_import_then_insert.{gesture}" in ids
    # Counted as its own acceptance row, never folded into command coverage.
    assert not any(item.startswith("command.generated_source") for item in ids)


def test_deferred_negatives_are_declared_unsupported_rather_than_absent() -> None:
    corpus = build_corpus()
    deferred = [case for case in corpus.cases if case.case_class == "deferred_negative"]
    assert deferred, "the deferred audio seams must be present as negative rows"
    assert all(case.supported is False for case in deferred)
    assert "deferred_negative.independent_audio_seam" in corpus.case_ids
    for surface in AUDIO_NEGATIVE_SURFACES:
        assert f"deferred_negative.surface_{surface}" in corpus.case_ids


def test_corpus_join_uses_the_backend_command_not_the_display_operation_id() -> None:
    manifest = cast(dict[str, Any], json.loads(CONTROL_MANIFEST_PATH.read_text(encoding="utf-8")))
    pairs = join_control_manifest(build_corpus(), manifest)
    assert len(pairs) == len(NLE_OPERATION_IDS)
    assert dict(pairs)["create_track"] == "track.add"
    # Joining on the display identifier would produce an empty intersection that reads as coverage.
    display_ids = {operation_id for _command, operation_id in pairs}
    assert not display_ids & set(NLE_OPERATION_IDS)


def test_join_rejects_a_manifest_that_drops_or_duplicates_a_command() -> None:
    manifest = cast(dict[str, Any], json.loads(CONTROL_MANIFEST_PATH.read_text(encoding="utf-8")))
    corpus = build_corpus()
    dropped = {**manifest, "command_rows": manifest["command_rows"][1:]}
    with pytest.raises(SemanticConformanceError):
        join_control_manifest(corpus, dropped)
    duplicated = {
        **manifest,
        "command_rows": [*manifest["command_rows"], manifest["command_rows"][0]],
    }
    with pytest.raises(SemanticConformanceError):
        join_control_manifest(corpus, duplicated)


#: The boundary rows that come from a declared `NumericDomain` rather than from a name. Built from
#: the same domain tables the corpus expands, so this enumerates rather than restates the rule.
NUMERIC_BOUNDARY_CASE_IDS = frozenset(
    f"{family}.{domain.field}.{leaf}"
    for family, domains in (
        ("transform", TRANSFORM_DOMAINS),
        ("effect", EFFECT_DOMAINS),
        ("text", (TEXT_SIZE_DOMAIN, TEXT_LINE_HEIGHT_DOMAIN)),
        ("clip_audio", CLIP_AUDIO_DOMAINS),
    )
    for domain in domains
    for leaf in ("underflow", "overflow")
)


def test_every_property_refusal_is_declared_or_a_numeric_domain_boundary() -> None:
    # Exact equality in both directions. A one-sided check is what let the suffix guess survive:
    # every declared refusal really was a refusal, and the eleven rows it silently dropped were
    # invisible because nothing asserted the other direction.
    observed = {
        case.case_id
        for case in build_corpus().cases
        if case.case_class == "property" and case.refusal_expected
    }
    assert observed == set(NAMED_REFUSAL_CASES) | NUMERIC_BOUNDARY_CASE_IDS


def test_the_timing_family_records_the_decoder_refusals_it_actually_has() -> None:
    # The regression this pins: `timing.negative_pts` and its siblings were expanded as accepted
    # cases because their names do not end in `_refused`.
    by_id = {case.case_id: case for case in build_corpus().cases}
    for leaf in (
        "negative_pts",
        "missing_landmark",
        "duplicate_landmark",
        "nonmonotonic_landmark",
        "source_range_unavailable",
    ):
        assert by_id[f"timing.{leaf}"].refusal_expected, leaf
    assert not by_id["timing.cfr_24"].refusal_expected
    assert not by_id["timing.vfr_unequal_intervals"].refusal_expected


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda landmark: landmark.update(pts=-1), "negative_timestamp"),
        (lambda landmark: landmark.update(frame_index=0), "invalid_timing"),
    ],
)
def test_the_decoder_really_refuses_the_timing_cases_the_corpus_declares(
    mutate: Callable[[dict[str, Any]], None], expected_code: str
) -> None:
    """The declaration above is only honest if the product actually refuses these."""

    def apply(wire: dict[str, Any]) -> None:
        asset = next(item for item in wire["assets"] if item.get("landmarks"))
        mutate(asset["landmarks"][1])

    with pytest.raises(CompositionContractError, match=expected_code):
        _decode_with(apply)


def test_tolerance_profile_is_inherited_and_frozen() -> None:
    assert TOLERANCE_PROFILE.version == TOLERANCE_VERSION
    # Inherited unchanged from the accepted M25-10 output profile.
    assert TOLERANCE_PROFILE.preview_max_av_drift_samples == 2_000
    assert TOLERANCE_PROFILE.final_impulse_tolerance_samples == 2_048
    assert TOLERANCE_PROFILE.patch_max_abs_channel == 8
    assert TOLERANCE_PROFILE.color_adjust_max_abs_channel == 12
    assert TOLERANCE_PROFILE.geometry_max_abs_pixels == 2


# --------------------------------------------------------------------------------------------
# Comparator sensitivity
# --------------------------------------------------------------------------------------------

CASE = "transform.position_x_bp.interior"
GRID = FrameGrid(width=320, height=180, frame_count=48, frame_rate_num=24, frame_rate_den=1)
METADATA = OutputMetadata(
    container="mp4",
    video_codec="h264",
    pixel_format="yuv420p",
    width=320,
    height=180,
    pixel_aspect_num=1,
    pixel_aspect_den=1,
    color_policy="bt709_sdr_limited_v1",
    frame_rate_num=24,
    frame_rate_den=1,
    audio_stream_count=1,
    audio_codec="aac",
    sample_rate=48_000,
    channels=1,
)
FIDUCIAL = GeometryLandmark(label="fiducial_tl", left=10, top=20, right=40, bottom=60)
PATCH = PatchSample(label="patch_a", size_px=5, edge_distance_px=6, red=200, green=100, blue=50)
LANDMARK = SourceLandmark(
    output_frame=0,
    source_frame=7,
    source_pts=3_584,
    source_time_base_num=1,
    source_time_base_den=12_288,
)
PRESENTED = PresentedFrame(
    output_frame=0, source_frame=LANDMARK.source_frame, source_time=LANDMARK.source_time()
)


def _expectation(**overrides: Any) -> Expectation:
    base: dict[str, Any] = {
        "case_id": CASE,
        "frame_grid": GRID,
        "source_mapping": (LANDMARK,),
        "geometry": (FIDUCIAL,),
        "patches": (PATCH,),
        "output_metadata": METADATA,
        "layer_order": ("clip-main", "clip-title"),
    }
    base.update(overrides)
    return Expectation(**base)


def _browser(**overrides: Any) -> BrowserObservation:
    base: dict[str, Any] = {
        "case_id": CASE,
        "canvas_width": 320,
        "canvas_height": 180,
        "source_mapping": (PRESENTED,),
        "geometry": (FIDUCIAL,),
        "patches": (PATCH,),
        "layer_order": ("clip-main", "clip-title"),
    }
    base.update(overrides)
    return BrowserObservation(**base)


def _final(**overrides: Any) -> FinalObservation:
    base: dict[str, Any] = {
        "case_id": CASE,
        "extractor_fingerprint": "sha256:" + "0" * 64,
        "frame_grid": GRID,
        "source_mapping": (LANDMARK,),
        "geometry": (FIDUCIAL,),
        "patches": (PATCH,),
        "output_metadata": METADATA,
        "layer_order": ("clip-main", "clip-title"),
    }
    base.update(overrides)
    return FinalObservation(**base)


def _compare(**overrides: Any) -> Any:
    expectation = overrides.pop("expectation", _expectation())
    browser = overrides.pop("browser", _browser())
    final = overrides.pop("final", _final())
    return compare_case(expectation, browser, final, TOLERANCE_PROFILE, **overrides)


def test_three_agreeing_observations_pass_without_byte_comparison() -> None:
    outcome = _compare()
    assert outcome.status == "PASS"
    assert outcome.mismatches == ()
    assert outcome.measurements, "a passing row still records what it measured"
    rendered = json.dumps(outcome.as_wire())
    for forbidden in ("bytes", "sha256_of_output", "byte_identical"):
        assert forbidden not in rendered


def test_geometry_passes_at_the_bound_and_fails_one_pixel_past_it() -> None:
    bound = TOLERANCE_PROFILE.geometry_max_abs_pixels
    at_bound = GeometryLandmark(
        label="fiducial_tl",
        left=FIDUCIAL.left + bound,
        top=FIDUCIAL.top,
        right=FIDUCIAL.right + bound,
        bottom=FIDUCIAL.bottom,
    )
    assert _compare(browser=_browser(geometry=(at_bound,))).status == "PASS"
    past = GeometryLandmark(
        label="fiducial_tl",
        left=FIDUCIAL.left + bound + 1,
        top=FIDUCIAL.top,
        right=FIDUCIAL.right + bound + 1,
        bottom=FIDUCIAL.bottom,
    )
    outcome = _compare(browser=_browser(geometry=(past,)))
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"geometry"}


def test_geometry_without_actual_backing_dimensions_cannot_pass() -> None:
    # CSS or screenshot coordinates scale with the viewport; a pixel bound applied to them is not
    # a bound at all, so the comparator refuses to evaluate geometry without real dimensions.
    outcome = _compare(browser=_browser(canvas_width=0, canvas_height=0))
    assert outcome.status == "MISMATCH"
    assert any(item.subject.endswith("canvas_dimensions") for item in outcome.mismatches)


def test_patch_bound_holds_at_eight_and_fails_at_nine() -> None:
    bound = TOLERANCE_PROFILE.patch_max_abs_channel
    at_bound = PatchSample(
        label="patch_a", size_px=5, edge_distance_px=6, red=PATCH.red + bound, green=100, blue=50
    )
    assert _compare(final=_final(patches=(at_bound,))).status == "PASS"
    past = PatchSample(
        label="patch_a",
        size_px=5,
        edge_distance_px=6,
        red=PATCH.red + bound + 1,
        green=100,
        blue=50,
    )
    outcome = _compare(final=_final(patches=(past,)))
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"patch"}


def test_an_edge_pixel_cannot_be_reported_as_an_interior_patch() -> None:
    too_close = PatchSample(
        label="patch_a",
        size_px=5,
        edge_distance_px=TOLERANCE_PROFILE.patch_min_edge_distance_px - 1,
        red=PATCH.red,
        green=PATCH.green,
        blue=PATCH.blue,
    )
    outcome = _compare(browser=_browser(patches=(too_close,)))
    assert outcome.status == "MISMATCH"
    assert any("edge_distance_px" in item.subject for item in outcome.mismatches)


def test_color_adjust_uses_the_wider_bound_without_widening_the_plain_patch_bound() -> None:
    colored = PatchSample(label="lut_a", size_px=5, edge_distance_px=8, red=120, green=60, blue=30)
    expectation = _expectation(patches=(), color_patches=(colored,))
    bound = TOLERANCE_PROFILE.color_adjust_max_abs_channel
    at_bound = PatchSample(
        label="lut_a", size_px=5, edge_distance_px=8, red=120 + bound, green=60, blue=30
    )
    assert (
        _compare(
            expectation=expectation,
            browser=_browser(patches=(), color_patches=(at_bound,)),
            final=_final(patches=(), color_patches=(at_bound,)),
        ).status
        == "PASS"
    )
    past = PatchSample(
        label="lut_a", size_px=5, edge_distance_px=8, red=120 + bound + 1, green=60, blue=30
    )
    outcome = _compare(
        expectation=expectation,
        browser=_browser(patches=(), color_patches=(past,)),
        final=_final(patches=(), color_patches=(past,)),
    )
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"color_adjust"}


def test_a_uniform_black_output_fails_every_visible_fixture() -> None:
    black = PatchSample(label="patch_a", size_px=5, edge_distance_px=6, red=0, green=0, blue=0)
    outcome = _compare(final=_final(patches=(black,)))
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"patch"}


def test_a_wrong_source_frame_is_never_admitted_by_the_half_tick_tolerance() -> None:
    # The adjacent source frame is one full tick away. A tolerance that admitted it would make the
    # whole VFR mapping check meaningless, so the boundary is half a tick and not a whole one.
    adjacent = PresentedFrame(
        output_frame=0, source_frame=8, source_time=LANDMARK.source_time() + LANDMARK.half_tick()
    )
    outcome = _compare(browser=_browser(source_mapping=(adjacent,)))
    assert outcome.status == "MISMATCH"
    assert any(item.mismatch_class == "source_mapping" for item in outcome.mismatches)


def test_presented_source_time_holds_at_half_a_tick_and_fails_immediately_past_it() -> None:
    # Deliberately literal. Deriving these from `half_tick()` would make the assertions move with
    # the boundary they exist to pin: widening the bound to a whole tick would then still pass.
    assert LANDMARK.source_time() == Fraction(7, 24)
    assert LANDMARK.half_tick() == Fraction(1, 24_576)
    at_bound = PresentedFrame(
        output_frame=0,
        source_frame=LANDMARK.source_frame,
        source_time=Fraction(7, 24) + Fraction(1, 24_576),
    )
    assert _compare(browser=_browser(source_mapping=(at_bound,))).status == "PASS"
    past = PresentedFrame(
        output_frame=0,
        source_frame=LANDMARK.source_frame,
        source_time=Fraction(7, 24) + Fraction(1, 24_576) + Fraction(1, 10**9),
    )
    outcome = _compare(browser=_browser(source_mapping=(past,)))
    assert outcome.status == "MISMATCH"
    assert any(item.subject.endswith("drift") for item in outcome.mismatches)


def test_a_whole_source_tick_of_presented_drift_is_refused() -> None:
    # One whole tick is the distance to the adjacent source frame. If this ever passes, the mapping
    # comparison can no longer distinguish the correct frame from its neighbour.
    a_full_tick_late = PresentedFrame(
        output_frame=0,
        source_frame=LANDMARK.source_frame,
        source_time=Fraction(7, 24) + Fraction(1, 12_288),
    )
    outcome = _compare(browser=_browser(source_mapping=(a_full_tick_late,)))
    assert outcome.status == "MISMATCH"
    assert any(item.subject.endswith("drift") for item in outcome.mismatches)


def test_the_final_artifact_source_pts_must_be_exact() -> None:
    assert LANDMARK.source_pts is not None
    off_by_one_tick = SourceLandmark(
        output_frame=0,
        source_frame=LANDMARK.source_frame,
        source_pts=LANDMARK.source_pts + 1,
        source_time_base_num=1,
        source_time_base_den=12_288,
    )
    outcome = _compare(final=_final(source_mapping=(off_by_one_tick,)))
    assert outcome.status == "MISMATCH"
    assert any(item.subject.endswith("source_pts") for item in outcome.mismatches)


@pytest.mark.parametrize("side", ["browser", "final"])
def test_a_missing_source_landmark_is_reported_rather_than_synthesized(side: str) -> None:
    observations: dict[str, Any] = (
        {"browser": _browser(source_mapping=())}
        if side == "browser"
        else {"final": _final(source_mapping=())}
    )
    outcome = _compare(**observations)
    assert outcome.status == "MISMATCH"
    assert any(f"{side}.source_mapping.frames" == item.subject for item in outcome.mismatches)


def test_frame_grid_and_duration_are_exact() -> None:
    short = FrameGrid(width=320, height=180, frame_count=47, frame_rate_num=24, frame_rate_den=1)
    outcome = _compare(final=_final(frame_grid=short))
    assert outcome.status == "MISMATCH"
    subjects = {item.subject for item in outcome.mismatches}
    assert "final.frame_grid.frame_count" in subjects
    assert "final.frame_grid.duration" in subjects


def test_omitted_output_metadata_is_a_mismatch_not_a_match_by_absence() -> None:
    unknown = OutputMetadata(
        container="mp4",
        video_codec="h264",
        pixel_format="yuv420p",
        width=320,
        height=180,
        pixel_aspect_num=1,
        pixel_aspect_den=1,
        color_policy="bt709_sdr_limited_v1",
        frame_rate_num=24,
        frame_rate_den=1,
        audio_stream_count=1,
        audio_codec=None,
        sample_rate=None,
        channels=None,
    )
    outcome = _compare(final=_final(output_metadata=unknown))
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"output_metadata"}


def test_layer_order_is_exact_even_when_every_patch_agrees() -> None:
    outcome = _compare(final=_final(layer_order=("clip-title", "clip-main")))
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"identity"}


def test_swapped_before_and_after_artifacts_are_caught_by_the_case_identity_guard() -> None:
    with pytest.raises(SemanticConformanceError):
        compare_case(
            _expectation(),
            _browser(),
            _final(case_id="transform.position_y_bp.interior"),
            TOLERANCE_PROFILE,
        )


def test_a_receipt_shaped_observation_with_no_content_blocks_rather_than_passes() -> None:
    outcome = _compare(browser=_browser(missing=("presented_frames",)))
    assert outcome.status == "BLOCKED"
    assert outcome.reason is not None and "presented_frames" in outcome.reason


def test_an_unidentified_extractor_blocks() -> None:
    outcome = _compare(final=_final(extractor_fingerprint=""))
    assert outcome.status == "BLOCKED"


def test_a_case_that_did_not_execute_is_not_run_rather_than_blocked() -> None:
    outcome = _compare(browser=_browser(executed=False))
    assert outcome.status == "NOT_RUN"


def test_av_drift_holds_at_two_thousand_samples_and_fails_past_it() -> None:
    bound = TOLERANCE_PROFILE.preview_max_av_drift_samples
    at_bound = AvPair(
        label="impulse_0",
        audio_time_ms=Fraction(0),
        visual_time_ms=Fraction(bound * 1000, OUTPUT_SAMPLE_RATE),
    )
    assert _compare(browser=_browser(av_pairs=(at_bound,))).status == "PASS"
    past = AvPair(
        label="impulse_0",
        audio_time_ms=Fraction(0),
        visual_time_ms=Fraction((bound + 1) * 1000, OUTPUT_SAMPLE_RATE),
    )
    outcome = _compare(browser=_browser(av_pairs=(past,)))
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"av_drift"}


def test_each_side_meets_its_own_onset_bound_independently() -> None:
    expectation = _expectation(audio_onsets=(AudioOnset("impulse_0", 4_800),))
    good_browser = _browser(audio_onsets=(AudioOnset("impulse_0", 4_800 + 2_000),))
    good_final = _final(audio_onsets=(AudioOnset("impulse_0", 4_800 + 2_048),))
    assert (
        _compare(expectation=expectation, browser=good_browser, final=good_final).status == "PASS"
    )
    # A final artifact well inside its own bound never excuses a preview outside the preview bound.
    bad_browser = _browser(audio_onsets=(AudioOnset("impulse_0", 4_800 + 2_001),))
    outcome = _compare(expectation=expectation, browser=bad_browser, final=good_final)
    assert outcome.status == "MISMATCH"
    assert [item.subject for item in outcome.mismatches] == ["browser.onset.impulse_0.residual"]


def test_a_silence_interval_shorter_than_its_exclusion_reports_an_empty_check() -> None:
    exclusion = TOLERANCE_PROFILE.codec_boundary_exclusion_samples
    declared = SilenceInterval("gap_0", start_sample=0, end_sample=2 * exclusion, abs_peak=0)
    expectation = _expectation(silences=(declared,))
    outcome = _compare(
        expectation=expectation,
        browser=_browser(silence_probes=(declared,)),
        final=_final(silences=(declared,)),
    )
    assert outcome.status == "MISMATCH"
    assert any("interior_samples" in item.subject for item in outcome.mismatches)


def test_a_measurable_silence_gap_passes_and_a_leaked_impulse_fails() -> None:
    exclusion = TOLERANCE_PROFILE.codec_boundary_exclusion_samples
    quiet = SilenceInterval("gap_0", start_sample=0, end_sample=6 * exclusion, abs_peak=0)
    expectation = _expectation(silences=(quiet,))
    assert (
        _compare(
            expectation=expectation,
            browser=_browser(silence_probes=(quiet,)),
            final=_final(silences=(quiet,)),
        ).status
        == "PASS"
    )
    leaked = SilenceInterval("gap_0", start_sample=0, end_sample=6 * exclusion, abs_peak=64)
    outcome = _compare(
        expectation=expectation,
        browser=_browser(silence_probes=(quiet,)),
        final=_final(silences=(leaked,)),
    )
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"audio_silence"}


def test_audio_extent_allows_one_sample_of_rational_rounding_and_no_more() -> None:
    expectation = _expectation(audio_extent_samples=96_000)
    assert (
        _compare(expectation=expectation, final=_final(audio_extent_samples=96_001)).status
        == "PASS"
    )
    outcome = _compare(expectation=expectation, final=_final(audio_extent_samples=96_002))
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"audio_extent"}


def test_text_content_is_exact_and_bounds_carry_only_a_pixel_tolerance() -> None:
    line = GeometryLandmark(label="line_0", left=40, top=10, right=280, bottom=54)
    text = TextObservation(
        content="Fixture title",
        font_identity="font-main@qualified",
        line_count=1,
        weight=700,
        style="normal",
        align="center",
        line_bounds=(line,),
        visible_glyph_ratio_milli=420,
    )
    expectation = _expectation(text=text)
    assert (
        _compare(
            expectation=expectation, browser=_browser(text=text), final=_final(text=text)
        ).status
        == "PASS"
    )
    nearly = TextObservation(
        content="Fixture titl",
        font_identity="font-main@qualified",
        line_count=1,
        weight=700,
        style="normal",
        align="center",
        line_bounds=(line,),
        visible_glyph_ratio_milli=420,
    )
    outcome = _compare(
        expectation=expectation, browser=_browser(text=nearly), final=_final(text=text)
    )
    assert outcome.status == "MISMATCH"
    assert [item.subject for item in outcome.mismatches] == ["browser.text.content"]


def test_invisible_text_cannot_pass_a_visible_fixture() -> None:
    text = TextObservation(
        content="Fixture title",
        font_identity="font-main@qualified",
        line_count=1,
        weight=700,
        style="normal",
        align="center",
        visible_glyph_ratio_milli=420,
    )
    blank = TextObservation(
        content="Fixture title",
        font_identity="font-main@qualified",
        line_count=1,
        weight=700,
        style="normal",
        align="center",
        visible_glyph_ratio_milli=0,
    )
    outcome = _compare(
        expectation=_expectation(text=text), browser=_browser(text=blank), final=_final(text=text)
    )
    assert outcome.status == "MISMATCH"
    assert any("visible_glyph_ratio" in item.subject for item in outcome.mismatches)


def test_alpha_progression_holds_at_three_percent_and_fails_past_it() -> None:
    bound = TOLERANCE_PROFILE.alpha_max_abs_error_milli
    declared = AlphaSample("dissolve", output_frame=2, alpha_milli=500)
    expectation = _expectation(alphas=(declared,))
    at_bound = AlphaSample("dissolve", output_frame=2, alpha_milli=500 + bound)
    assert (
        _compare(
            expectation=expectation,
            browser=_browser(alphas=(at_bound,)),
            final=_final(alphas=(at_bound,)),
        ).status
        == "PASS"
    )
    past = AlphaSample("dissolve", output_frame=2, alpha_milli=500 + bound + 1)
    outcome = _compare(
        expectation=expectation,
        browser=_browser(alphas=(past,)),
        final=_final(alphas=(declared,)),
    )
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"alpha"}


def test_a_refusal_case_proves_its_code_and_that_nothing_was_rendered() -> None:
    expectation = Expectation(case_id=CASE, refusal_code="invalid_contract")
    passing = compare_case(
        expectation,
        _browser(refusal_code="invalid_contract"),
        FinalObservation(case_id=CASE, extractor_fingerprint="sha256:" + "0" * 64),
        TOLERANCE_PROFILE,
    )
    assert passing.status == "PASS"
    rendered_anyway = compare_case(
        expectation,
        _browser(refusal_code="invalid_contract"),
        _final(),
        TOLERANCE_PROFILE,
    )
    assert rendered_anyway.status == "MISMATCH"
    assert any(item.subject == "final.artifact" for item in rendered_anyway.mismatches)
    wrong_code = compare_case(
        expectation,
        _browser(refusal_code="unsupported_output_profile"),
        FinalObservation(case_id=CASE, extractor_fingerprint="sha256:" + "0" * 64),
        TOLERANCE_PROFILE,
    )
    assert wrong_code.status == "MISMATCH"
    assert {item.mismatch_class for item in wrong_code.mismatches} == {"refusal_code"}


def test_a_declared_negative_still_has_to_execute() -> None:
    expectation = Expectation(case_id="deferred_negative.waveform")
    outcome = compare_case(
        expectation,
        BrowserObservation(case_id="deferred_negative.waveform"),
        FinalObservation(
            case_id="deferred_negative.waveform", extractor_fingerprint="sha256:" + "0" * 64
        ),
        TOLERANCE_PROFILE,
        supported=False,
        declared_unsupported_reason="no waveform control, command, lease or receipt exists",
    )
    assert outcome.status == "DECLARED_UNSUPPORTED"
    skipped = compare_case(
        expectation,
        BrowserObservation(case_id="deferred_negative.waveform", executed=False),
        FinalObservation(
            case_id="deferred_negative.waveform", extractor_fingerprint="sha256:" + "0" * 64
        ),
        TOLERANCE_PROFILE,
        supported=False,
        declared_unsupported_reason="no waveform control, command, lease or receipt exists",
    )
    assert skipped.status == "NOT_RUN"


def test_an_unsupported_case_without_a_declared_reason_is_rejected() -> None:
    with pytest.raises(SemanticConformanceError):
        compare_case(
            Expectation(case_id="deferred_negative.gain"),
            BrowserObservation(case_id="deferred_negative.gain"),
            FinalObservation(
                case_id="deferred_negative.gain", extractor_fingerprint="sha256:" + "0" * 64
            ),
            TOLERANCE_PROFILE,
            supported=False,
        )


def test_a_report_row_never_discloses_a_path_or_url() -> None:
    for leaked in ("C:/private/media.mp4", "https://host.example/output.mp4", "//share/clip.mov"):
        with pytest.raises(SemanticConformanceError):
            compare_case(
                Expectation(case_id="deferred_negative.pan"),
                BrowserObservation(case_id="deferred_negative.pan"),
                FinalObservation(
                    case_id="deferred_negative.pan", extractor_fingerprint="sha256:" + "0" * 64
                ),
                TOLERANCE_PROFILE,
                supported=False,
                declared_unsupported_reason=leaked,
            )


def test_summary_keeps_statuses_separate_and_refuses_to_hide_an_unexecuted_row() -> None:
    outcomes = [_compare(), _compare(browser=_browser(executed=False))]
    summary = summarize(outcomes, required_rows=2)
    assert set(summary.totals) == set(REPORT_STATUSES)
    assert summary.totals["PASS"] == 1
    assert summary.totals["NOT_RUN"] == 1
    assert summary.conformant() is False
    assert summarize([_compare()], required_rows=1).conformant() is True


# --------------------------------------------------------------------------------------------
# Independent extractor
# --------------------------------------------------------------------------------------------

PROBE: dict[str, Any] = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "width": 320,
            "height": 180,
            "r_frame_rate": "24/1",
            "sample_aspect_ratio": "1:1",
            "nb_frames": "999",
        },
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "48000",
            "channels": 1,
        },
    ],
}


def _rgb24(width: int, height: int, fill: tuple[int, int, int]) -> bytearray:
    return bytearray(bytes(fill) * (width * height))


def _set_pixel(frame: bytearray, width: int, x: int, y: int, colour: tuple[int, int, int]) -> None:
    offset = (y * width + x) * 3
    frame[offset : offset + 3] = bytes(colour)


def test_probe_metadata_is_read_from_the_artifact_not_a_receipt() -> None:
    metadata = parse_output_metadata(PROBE)
    # This is the literal `format_name` the pinned ffprobe reports for a real MP4. Its first
    # member is `mov`, so the container is resolved against the emitted vocabulary rather than
    # positionally.
    assert PROBE["format"]["format_name"].split(",")[0] == "mov"
    assert metadata.container == "mp4"
    assert metadata.video_codec == "h264"
    assert metadata.audio_stream_count == 1
    assert (metadata.sample_rate, metadata.channels) == (48_000, 1)
    assert (metadata.pixel_aspect_num, metadata.pixel_aspect_den) == (1, 1)
    # This fixture carries no colour tags, and no caller can supply one on its behalf any more.
    assert metadata.color_policy == UNKNOWN_COLOR_POLICY


def test_a_container_family_outside_the_emitted_vocabulary_is_reported_verbatim() -> None:
    # Reporting the raw family keeps the mismatch honest instead of coercing it into a known name.
    probe = {"format": {"format_name": "matroska,webm"}, "streams": PROBE["streams"]}
    assert parse_output_metadata(probe).container == "matroska,webm"


def test_an_unknown_pixel_aspect_is_refused_rather_than_assumed_square() -> None:
    probe = {**PROBE, "streams": [dict(PROBE["streams"][0]), PROBE["streams"][1]]}
    probe["streams"][0]["sample_aspect_ratio"] = "N/A"
    with pytest.raises(SemanticConformanceError):
        parse_output_metadata(probe)


def test_missing_probe_metadata_raises_instead_of_defaulting() -> None:
    probe = {**PROBE, "streams": [dict(PROBE["streams"][0]), PROBE["streams"][1]]}
    del probe["streams"][0]["codec_name"]
    with pytest.raises(SemanticConformanceError):
        parse_output_metadata(probe)


def test_an_artifact_without_an_audio_stream_reports_zero_rather_than_guessing() -> None:
    metadata = parse_output_metadata({**PROBE, "streams": [PROBE["streams"][0]]})
    assert metadata.audio_stream_count == 0
    assert metadata.audio_codec is None


def test_the_frame_grid_counts_decoded_frames_not_the_container_hint() -> None:
    # The container advertises 999. A grid built from that hint would agree with a truncated
    # artifact, which is exactly the failure the final observation exists to catch. The count now
    # has to survive a second, independent statement of it as well: the measured packet table.
    timing = parse_frame_timing(_timing_document(_uniform_pts(48)))
    grid = parse_frame_grid(PROBE, 48, timing)
    assert PROBE["streams"][0]["nb_frames"] == "999"
    assert grid.frame_count == 48
    assert grid.duration() == Fraction(2, 1)
    # And the rate is the one the packets actually have, not the one the container advertises.
    assert (grid.frame_rate_num, grid.frame_rate_den) == (24, 1)


def test_patch_mean_is_exact_and_records_its_measured_edge_distance() -> None:
    frame = _rgb24(32, 32, (10, 20, 30))
    for y in range(14, 19):
        for x in range(14, 19):
            _set_pixel(frame, 32, x, y, (200, 100, 50))
    patch = patch_mean(bytes(frame), 32, 32, label="p", center_x=16, center_y=16, size_px=5)
    assert (patch.red, patch.green, patch.blue) == (200, 100, 50)
    assert patch.size_px == 5
    assert patch.edge_distance_px == 13


def test_a_patch_that_does_not_fit_is_refused_rather_than_clamped() -> None:
    frame = bytes(_rgb24(8, 8, (0, 0, 0)))
    with pytest.raises(SemanticConformanceError):
        patch_mean(frame, 8, 8, label="p", center_x=1, center_y=4, size_px=5)


def test_a_frame_buffer_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(SemanticConformanceError):
        patch_mean(
            bytes(_rgb24(8, 8, (0, 0, 0)))[:-3], 8, 8, label="p", center_x=4, center_y=4, size_px=5
        )


@pytest.mark.parametrize("identifier", [0, 1, 7, 23, 63])
def test_the_frame_identifier_tile_round_trips(identifier: int) -> None:
    width, height, cell, bits = 64, 16, 4, 6
    frame = _rgb24(width, height, (0, 0, 0))
    for index in range(bits):
        if identifier & (1 << (bits - 1 - index)):
            for y in range(0, cell):
                for x in range(index * cell, (index + 1) * cell):
                    _set_pixel(frame, width, x, y, (255, 255, 255))
    assert (
        read_frame_id_tile(
            bytes(frame), width, height, origin_x=0, origin_y=0, cell_px=cell, bits=bits
        )
        == identifier
    )


def test_the_frame_identifier_tile_reads_cell_centres_so_edge_blending_cannot_flip_a_bit() -> None:
    width, height, cell, bits = 64, 16, 4, 6
    frame = _rgb24(width, height, (0, 0, 0))
    # Bit 0 set, with its cell's outermost column bled to mid grey by a scaling filter.
    for y in range(0, cell):
        for x in range(0, cell):
            _set_pixel(frame, width, x, y, (255, 255, 255))
        _set_pixel(frame, width, 0, y, (120, 120, 120))
        _set_pixel(frame, width, cell - 1, y, (120, 120, 120))
    assert read_frame_id_tile(
        bytes(frame), width, height, origin_x=0, origin_y=0, cell_px=cell, bits=bits
    ) == 1 << (bits - 1)


def test_silence_measurement_reports_the_peak_it_found() -> None:
    quiet = (0).to_bytes(2, "little", signed=True) * 4_096
    loud = quiet[: 2 * 100] + (900).to_bytes(2, "little", signed=True) + quiet[2 * 101 :]
    assert silence_interval(quiet, label="g", start_sample=0, end_sample=4_096).abs_peak == 0
    assert silence_interval(loud, label="g", start_sample=0, end_sample=4_096).abs_peak == 900


def test_onsets_are_labelled_in_order_and_a_count_mismatch_is_an_error() -> None:
    samples = bytearray((0).to_bytes(2, "little", signed=True) * 10_000)
    for position in (1_000, 5_000):
        samples[2 * position : 2 * position + 2] = (20_000).to_bytes(2, "little", signed=True)
    onsets = detect_onsets(
        bytes(samples),
        labels=("first", "second"),
        threshold_ratio=Fraction(1, 4),
        refractory_samples=480,
    )
    assert [(item.label, item.sample_index) for item in onsets] == [
        ("first", 1_000),
        ("second", 5_000),
    ]
    # Zipping the first N onsets to the first N labels would hide both a missing and an extra one.
    with pytest.raises(SemanticConformanceError):
        detect_onsets(
            bytes(samples),
            labels=("first",),
            threshold_ratio=Fraction(1, 4),
            refractory_samples=480,
        )


# --------------------------------------------------------------------------------------------
# Qualification entry point
# --------------------------------------------------------------------------------------------

#: Stand-ins for the tests that only assert the shape of a command line. These never reach the
#: filesystem, so a real tool would add nothing and a real path would be a machine-specific one.
PLACEHOLDER_FFMPEG = Path("ffmpeg.exe")
PLACEHOLDER_FFPROBE = Path("ffprobe.exe")


def authorized_decoders() -> tuple[Path, Path]:
    """The explicitly supplied decoder pair, or skip.

    CRITICAL: resolved from the environment rather than written down. An absolute tool path in
    tracked source is forbidden by `AGENTS.md` section 3, and it also makes the pinned-decoder
    claim true only on the machine that recorded it -- everywhere else the file check fails and the
    tests skip while appearing to have run. This is the same pair of variables, and the same skip
    wording, that every other real-media test in this repository uses.
    """

    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    return Path(ffmpeg_value).resolve(strict=True), Path(ffprobe_value).resolve(strict=True)


def test_a_decoder_binary_must_be_configured_and_is_never_discovered_on_path() -> None:
    with pytest.raises(qualification.QualificationError) as refusal:
        qualification.resolve_binary(None, "probe")
    assert "PATH discovery is refused" in str(refusal.value)


def test_a_decoder_binary_that_is_not_the_pinned_one_is_refused() -> None:
    ffmpeg, ffprobe = authorized_decoders()
    # The right family, the wrong role: FFmpeg's digest is not the probe's.
    with pytest.raises(qualification.QualificationError) as refusal:
        qualification.resolve_binary(ffmpeg, "probe")
    assert "does not match the accepted qualification digest" in str(refusal.value)
    assert qualification.resolve_binary(ffprobe, "probe") == ffprobe
    assert qualification.resolve_binary(ffmpeg, "renderer") == ffmpeg


def test_the_block_reader_refuses_a_truncated_unit_but_admits_a_short_final_block() -> None:
    # One second of mono 16-bit audio, plus 256 real samples: what a real decoder actually emits.
    block = qualification.audio_block_bytes()
    stream = io.BytesIO(b"\x00" * (block + 512))
    read = list(qualification.iter_blocks(stream, block, unit_bytes=PCM16_BYTES_PER_SAMPLE))
    assert [len(item) for item in read] == [block, 512]

    # A tail that stops mid-sample is still refused: padding it would manufacture an observation.
    odd = io.BytesIO(b"\x00" * (block + 511))
    with pytest.raises(qualification.QualificationError, match="partial block"):
        list(qualification.iter_blocks(odd, block, unit_bytes=PCM16_BYTES_PER_SAMPLE))

    # And a video frame keeps the strict rule, because its unit is the whole block.
    frame = qualification.video_frame_bytes(4, 4)
    with pytest.raises(qualification.QualificationError, match="partial block"):
        list(qualification.iter_blocks(io.BytesIO(b"\x00" * (frame + 1)), frame))


def test_the_entry_point_observes_a_real_artifact_with_the_pinned_decoder() -> None:
    """Drive the whole `--artifact` path against a real file, not a shaped fixture.

    This is the test whose absence let the block-reader defect ship: every part was covered
    separately and the assembled path had never been run.
    """

    ffmpeg, ffprobe = authorized_decoders()
    artifact = ROOT / "tests/fixtures/m25_12_runtime/cfr-primary.mp4"
    observed = qualification.observe_artifact(ffmpeg, ffprobe, artifact)

    assert observed["extractor_fingerprint"] == qualification.extractor_fingerprint()
    grid = cast(dict[str, Any], observed["frame_grid"])
    assert (grid["width"], grid["height"]) == (320, 180)
    # Counted from frames actually decoded, and the container's own `nb_frames` hint agrees here.
    assert grid["frame_count"] == 48
    assert (grid["frame_rate_num"], grid["frame_rate_den"]) == (24, 1)
    assert grid["duration_seconds"] == "2"

    # 96,000 samples is two nominal seconds; the pinned decoder emits 256 more because of AAC
    # priming. The exact number is asserted because the decoder is digest-pinned: a change here is
    # a change of experiment, not noise.
    assert observed["audio_extent_samples"] == 96256

    metadata = cast(dict[str, Any], observed["output_metadata"])
    assert metadata["container"] == "mp4"
    # Derived from the artifact: this fixture predates the accepted output profile and really does
    # carry no colour tags, so the only truthful answer is that it declared none.
    assert metadata["color_policy"] == UNKNOWN_COLOR_POLICY

    # The timing came from the packet table, in the artifact's own 1/12288 time base, and the grid
    # above was built from it rather than from `r_frame_rate`.
    timing = cast(dict[str, Any], observed["frame_timing"])
    assert timing["time_base"] == "1/12288"
    assert timing["packet_count"] == 48
    assert timing["first_pts_ticks"] == 0
    assert timing["last_pts_ticks"] == 47 * 512
    assert timing["uniform_step_seconds"] == "1/24"
    assert timing["duration_seconds"] == "2"
    assert timing["strictly_monotonic"] is True
    assert timing["duplicate_timestamps"] == []
    assert timing["negative_timestamp"] is False
    # `-bf 0`: no B-frames, so the decode order is the presentation order.
    assert timing["dts_policy"] == "equal_to_pts"


def test_the_probe_invocation_asks_for_both_documents_and_re_encodes_nothing() -> None:
    command = qualification.probe_command(PLACEHOLDER_FFPROBE, Path("artifact.mp4"))
    assert command[0] == str(PLACEHOLDER_FFPROBE)
    assert command[-1] == "artifact.mp4"
    # Banners on stdout would land inside the parsed JSON document.
    assert command[command.index("-v") + 1] == "error"
    assert command[command.index("-print_format") + 1] == "json"
    # Both are needed: the container facts live in the format document, the stream facts do not.
    assert {"-show_format", "-show_streams"} <= set(command)


def test_the_pcm_peak_is_absolute_bounded_by_the_range_and_refuses_a_backwards_one() -> None:
    quiet = (0).to_bytes(2, "little", signed=True)
    loud = (-3000).to_bytes(2, "little", signed=True)
    samples = quiet * 4 + loud + quiet * 3

    assert pcm16_peak(samples, 0, 4) == 0
    # Absolute, so a negative excursion is a peak rather than a minimum.
    assert pcm16_peak(samples, 0, 8) == 3000
    # Bounded by the requested range: a loud sample outside it must not leak in.
    assert pcm16_peak(samples, 5, 8) == 0
    # An empty range is silence, not an error.
    assert pcm16_peak(samples, 2, 2) == 0
    # Full-scale negative has no positive counterpart, so the peak is 32768 and not 32767.
    assert pcm16_peak((-32768).to_bytes(2, "little", signed=True), 0, 1) == 32768
    with pytest.raises(SemanticConformanceError):
        pcm16_peak(samples, 3, 1)


def test_the_decode_commands_never_re_encode_or_filter() -> None:
    video = qualification.decode_video_command(PLACEHOLDER_FFMPEG, Path("artifact.mp4"))
    assert "-f" in video and video[video.index("-f") + 1] == "rawvideo"
    assert video[video.index("-pix_fmt") + 1] == "rgb24"
    # A scaling or colour filter would make the decoded pixels a function of this script.
    assert not {"-vf", "-filter_complex", "-s"} & set(video)
    audio = qualification.decode_audio_command(PLACEHOLDER_FFMPEG, Path("artifact.mp4"))
    assert audio[audio.index("-ar") + 1] == str(OUTPUT_SAMPLE_RATE)
    assert audio[audio.index("-ac") + 1] == "1"
    assert "-af" not in audio


def test_a_partial_decode_block_is_refused_rather_than_padded() -> None:
    class _Truncated:
        def __init__(self) -> None:
            self.blocks = [b"\x00" * 6, b"\x00" * 3]

        def read(self, size: int) -> bytes:
            return self.blocks.pop(0) if self.blocks else b""

    with pytest.raises(qualification.QualificationError):
        list(qualification.iter_blocks(_Truncated(), 6))


def test_the_extractor_fingerprint_covers_both_halves_of_the_extractor() -> None:
    first = qualification.extractor_fingerprint()
    assert first.startswith("sha256:")
    # It is a function of the tooling and the pure module together, so it is stable across calls.
    assert first == qualification.extractor_fingerprint()


def test_the_report_counts_statuses_and_refuses_to_call_an_incomplete_run_conformant() -> None:
    report = qualification.build_report(
        [_compare()],
        candidate_tree="c43ea319",
        renderer_fingerprint="sha256:renderer",
        probe_fingerprint="sha256:probe",
        extractor_fingerprint="sha256:extractor",
        browser_profile_fingerprint="sha256:profile",
        chromium_version="151.0.7922.34",
    )
    assert report["required_rows"] == len(build_corpus().cases)
    assert report["executed_rows"] == 1
    # One passing row out of 306 required rows is not conformance.
    assert report["conformant"] is False
    assert report["totals"]["PASS"] == 1


# --------------------------------------------------------------------------------------------
# The frozen manifest
# --------------------------------------------------------------------------------------------

MANIFEST_PATH = ROOT / "governance" / "contracts" / "nle_semantic_conformance_manifest_v1.json"
MANIFEST_SCHEMA_PATH = (
    ROOT / "governance" / "contracts" / "nle_semantic_conformance_manifest_v1.schema.json"
)


def _manifest() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))


def test_the_manifest_validates_against_its_schema() -> None:
    schema = json.loads(MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(_manifest()),
        key=lambda error: list(error.path),
    )
    assert errors == [], [error.message for error in errors]


def test_the_manifest_is_the_generator_output_and_has_not_been_hand_edited() -> None:
    # A hand-edited membership is a membership that has stopped tracking the contract.
    assert MANIFEST_PATH.read_text(encoding="utf-8") == manifest_generator.render()


def test_every_manifest_row_class_matches_the_expanded_corpus() -> None:
    manifest = _manifest()
    corpus = build_corpus()
    assert manifest["counts_by_class"] == corpus.counts_by_class()
    ids = [
        row["case_id"]
        for key in (
            "command_rows",
            "property_rows",
            "ui_invariant_rows",
            "import_integration_rows",
            "deferred_negative_rows",
        )
        for row in manifest[key]
    ]
    assert ids == list(corpus.case_ids)
    assert len(ids) == len(set(ids))


def test_the_manifest_joins_on_the_backend_command_and_pins_what_it_joined_against() -> None:
    manifest = _manifest()
    by_command = {row["command"]: row["control_operation_id"] for row in manifest["command_rows"]}
    assert set(by_command) == set(NLE_OPERATION_IDS)
    assert by_command["create_track"] == "track.add"
    joined = manifest["joined_sources"]
    assert joined["control_coverage_manifest_sha256"] == (
        "sha256:" + hashlib.sha256(CONTROL_MANIFEST_PATH.read_bytes()).hexdigest()
    )
    shell_contract = ROOT / "frontend" / "src" / "contracts" / "sidebarEditorUiContract.ts"
    assert joined["sidebar_editor_ui_contract_sha256"] == (
        "sha256:" + hashlib.sha256(shell_contract.read_bytes()).hexdigest()
    )


def test_every_shell_invariant_row_cites_an_executable_case() -> None:
    # The citation is resolved against the real runners by
    # `frontend/tests/nleEvidenceCollection.test.ts`; this side only proves none is missing.
    rows = _manifest()["ui_invariant_rows"]
    assert len(rows) == len(SIDEBAR_UI_INVARIANT_IDS) - 1 + len(NON_DESTROY_CLOSE_REASONS)
    for row in rows:
        reference = row["evidence"]
        path, separator, title = reference.partition("::")
        assert separator == "::" and title.strip(), reference
        assert (ROOT / "frontend" / "tests" / path).is_file(), reference
    assert len({row["evidence"] for row in rows}) > 1, "one citation cannot prove seven rows"


def test_the_manifest_carries_no_runtime_pin_and_no_result() -> None:
    # Execution facts and dispositions belong to the report; a manifest that could hold a
    # disposition would be a place to write down a pass.
    rendered = json.dumps(_manifest())
    for forbidden in ("chromium", "PASS", "MISMATCH", "conformant", "extractor_fingerprint"):
        assert forbidden not in rendered


# --------------------------------------------------------------------------------------------
# Post-closeout corrective: report admission, derived colour, measured timing, real deadlines
#
# Each test below reproduces one blocking finding of
# `.planning/260909-M25-20_POST_CLOSEOUT_CODE_REVIEW_REPORT.md`. Where the reviewer executed a
# probe by hand, that probe is pinned here verbatim: a diagnostic that was run once and then
# described in prose is exactly the evidence shape this item was rejected for.
# --------------------------------------------------------------------------------------------


REPORT_PINS: dict[str, str] = {
    "candidate_tree": "2f1d0228014dc057a795f441c373aab553e4f56b",  # pragma: allowlist secret
    "renderer_fingerprint": "sha256:renderer",
    "probe_fingerprint": "sha256:probe",
    "extractor_fingerprint": "sha256:extractor",
    "browser_profile_fingerprint": "sha256:profile",
    "chromium_version": "151.0.7922.34",
}


def _report(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    return qualification.build_report(outcomes, **REPORT_PINS)


def _complete_outcomes() -> list[CaseOutcome]:
    """One eligible outcome per corpus row: supported rows pass, deferred rows declare."""

    return [
        CaseOutcome(case.case_id, "PASS" if case.supported else "DECLARED_UNSUPPORTED")
        for case in build_corpus().cases
    ]


def test_the_reviewers_duplicate_reproduction_is_no_longer_conformant() -> None:
    """F2, pinned verbatim: 306 copies of one case used to report `conformant: true`.

    `summarize` counts statuses, and counting alone cannot tell 306 rows from one row repeated 306
    times. A runner that duplicated an aggregation would have omitted 305 required cases and still
    emitted success.
    """

    corpus = build_corpus()
    rows = [CaseOutcome(corpus.cases[0].case_id, "PASS")] * len(corpus.cases)
    report = _report(rows)

    assert report["required_rows"] == len(corpus.cases)
    assert report["executed_rows"] == len(corpus.cases)
    assert len({row["case_id"] for row in report["rows"]}) == 1
    assert report["conformant"] is False
    admission = cast(dict[str, Any], report["admission"])
    assert admission["admitted"] is False
    assert admission["duplicates"] == [corpus.cases[0].case_id]
    assert len(admission["missing"]) == len(corpus.cases) - 1


def test_an_exact_eligible_set_is_admitted_and_conformant() -> None:
    report = _report(_complete_outcomes())
    admission = cast(dict[str, Any], report["admission"])
    assert admission == {
        "admitted": True,
        "duplicates": [],
        "missing": [],
        "unknown": [],
        "ineligible": [],
    }
    assert report["conformant"] is True
    assert report["counts_by_class"] == build_corpus().counts_by_class()


def test_an_unknown_case_identifier_is_named_rather_than_counted() -> None:
    outcomes = _complete_outcomes()
    outcomes[0] = CaseOutcome("command.invented_operation.accepted", "PASS")
    report = _report(outcomes)
    admission = cast(dict[str, Any], report["admission"])
    assert admission["unknown"] == ["command.invented_operation.accepted"]
    # The substitution is symmetric: an invented row arriving is also a required row missing.
    assert admission["missing"] == [build_corpus().cases[0].case_id]
    assert report["conformant"] is False


def test_a_missing_required_row_cannot_be_hidden_by_a_full_status_count() -> None:
    outcomes = _complete_outcomes()[:-1]
    report = _report(outcomes)
    assert report["executed_rows"] == len(build_corpus().cases) - 1
    assert cast(dict[str, Any], report["admission"])["missing"] == [
        build_corpus().cases[-1].case_id
    ]
    assert report["conformant"] is False


def test_a_supported_row_cannot_be_retired_as_declared_unsupported() -> None:
    """`DECLARED_UNSUPPORTED` belongs to a predeclared negative and to nothing else.

    Status counting cannot enforce that on its own: a runner that answered every hard supported row
    with `DECLARED_UNSUPPORTED` would produce no `MISMATCH`, no `BLOCKED` and no `NOT_RUN`.
    """

    corpus = build_corpus()
    supported = next(case for case in corpus.cases if case.supported)
    outcomes = [
        CaseOutcome(
            case.case_id,
            "DECLARED_UNSUPPORTED"
            if case.case_id == supported.case_id or not case.supported
            else "PASS",
        )
        for case in corpus.cases
    ]
    report = _report(outcomes)
    admission = cast(dict[str, Any], report["admission"])
    assert admission["ineligible"] == [[supported.case_id, "DECLARED_UNSUPPORTED"]]
    assert report["conformant"] is False


def test_a_declared_negative_cannot_be_reported_as_a_product_pass() -> None:
    corpus = build_corpus()
    deferred = next(case for case in corpus.cases if not case.supported)
    outcomes = [
        CaseOutcome(case.case_id, "PASS" if case.supported else "DECLARED_UNSUPPORTED")
        for case in corpus.cases
    ]
    outcomes = [
        CaseOutcome(deferred.case_id, "PASS") if item.case_id == deferred.case_id else item
        for item in outcomes
    ]
    admission = cast(dict[str, Any], _report(outcomes)["admission"])
    assert admission["ineligible"] == [[deferred.case_id, "PASS"]]


def test_an_incomplete_run_still_reports_every_row_it_did_execute() -> None:
    """Admission refuses the conformance claim; it never deletes the evidence."""

    outcomes = _complete_outcomes()[:10]
    report = _report(outcomes)
    assert len(cast(list[Any], report["rows"])) == 10
    assert report["conformant"] is False


# --------------------------------------------------------------------------------------------
# F3: the colour policy is derived from actual tags, and every audio stream is counted
# --------------------------------------------------------------------------------------------

#: What the accepted renderer actually writes: `-color_range tv -colorspace bt709
#: -color_primaries bt709 -color_trc bt709` in `authoring_render_graph.py`.
TAGGED_VIDEO: dict[str, Any] = {
    **cast(dict[str, Any], PROBE["streams"][0]),
    "color_space": "bt709",
    "color_transfer": "bt709",
    "color_primaries": "bt709",
    "color_range": "tv",
}


def _probe_with(video: dict[str, Any], audio: list[dict[str, Any]]) -> dict[str, Any]:
    return {"format": dict(cast(dict[str, Any], PROBE["format"])), "streams": [video, *audio]}


def test_the_colour_policy_is_derived_from_the_artifact_and_not_from_its_caller() -> None:
    metadata = parse_output_metadata(_probe_with(TAGGED_VIDEO, []))
    assert metadata.color_policy == ACCEPTED_COLOR_POLICY


def test_an_untagged_artifact_cannot_claim_the_accepted_colour_policy() -> None:
    """F3, pinned: the old signature returned whatever name it was handed.

    `tests/fixtures/m25_12_runtime/cfr-primary.mp4` really does carry no colour tags, so this is
    the shape a caller could previously have labelled `bt709_sdr_limited_v1` by omission.
    """

    untagged = cast(dict[str, Any], PROBE["streams"][0])
    assert not {"color_space", "color_transfer", "color_primaries", "color_range"} & set(untagged)
    metadata = parse_output_metadata(_probe_with(dict(untagged), []))
    assert metadata.color_policy == UNKNOWN_COLOR_POLICY
    assert metadata.color_policy != ACCEPTED_COLOR_POLICY


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("color_space", "bt470bg"),
        ("color_transfer", "smpte2084"),
        ("color_primaries", "bt2020"),
        ("color_range", "pc"),
    ],
)
def test_a_contradictory_colour_tag_is_reported_verbatim(key: str, value: str) -> None:
    """A wrong tag fails the comparison on the observed string, not on the extractor's opinion."""

    metadata = parse_output_metadata(_probe_with({**TAGGED_VIDEO, key: value}, []))
    assert metadata.color_policy != ACCEPTED_COLOR_POLICY
    assert metadata.color_policy != UNKNOWN_COLOR_POLICY
    assert f"{key}={value}" in metadata.color_policy


def test_a_partially_tagged_artifact_names_the_tag_it_is_missing() -> None:
    partial = {key: value for key, value in TAGGED_VIDEO.items() if key != "color_range"}
    policy = parse_output_metadata(_probe_with(partial, [])).color_policy
    assert "color_range=absent" in policy


def test_two_audio_streams_are_counted_as_two() -> None:
    """F3, pinned verbatim from the reviewer's probe: `0 if audio is None else 1` observed one.

    The accepted profile emits zero or one audio stream, so a second stream is exactly the
    unsupported layout the comparison exists to catch — and normalising it to one made the
    unsupported artifact indistinguishable from a conformant one.
    """

    audio = cast(dict[str, Any], PROBE["streams"][1])
    metadata = parse_output_metadata(_probe_with(TAGGED_VIDEO, [dict(audio), dict(audio)]))
    assert metadata.audio_stream_count == 2
    assert parse_output_metadata(_probe_with(TAGGED_VIDEO, [dict(audio)])).audio_stream_count == 1
    assert parse_output_metadata(_probe_with(TAGGED_VIDEO, [])).audio_stream_count == 0


# --------------------------------------------------------------------------------------------
# F4: the frame grid is measured from actual packet timing, not inferred from a nominal rate
# --------------------------------------------------------------------------------------------

#: The literal shape the pinned ffprobe emits for a real 48-frame 24-fps artifact: a 1/12288
#: stream time base and a 512-tick packet step, which is exactly 1/24 s.
TIMING_TIME_BASE = "1/12288"
TIMING_STEP = 512


def _timing_document(
    pts: list[int], *, dts: list[int] | None = None, time_base: str = TIMING_TIME_BASE
) -> dict[str, Any]:
    stamps = pts if dts is None else dts
    return {
        "streams": [{"codec_type": "video", "time_base": time_base}],
        "packets": [
            {"pts": point, "dts": stamp, "duration": TIMING_STEP, "flags": "K__"}
            for point, stamp in zip(pts, stamps, strict=True)
        ],
    }


def _uniform_pts(count: int) -> list[int]:
    return [index * TIMING_STEP for index in range(count)]


def test_a_frame_grid_cannot_be_built_from_a_nominal_rate_alone() -> None:
    """F4, pinned: `parse_frame_grid(PROBE, 48).duration()` used to answer `2` with no timestamps.

    A correct nominal rate and a correct decoded count say nothing about where the frames actually
    sit, so the measured table is required rather than optional.
    """

    with pytest.raises(SemanticConformanceError, match="timing"):
        parse_frame_grid(PROBE, 48, None)


def test_measured_timing_pins_the_rational_pts_grid() -> None:
    timing = parse_frame_timing(_timing_document(_uniform_pts(48)))
    assert timing.count() == 48
    assert timing.time_at(0) == Fraction(0)
    assert timing.time_at(1) == Fraction(1, 24)
    assert timing.time_at(47) == Fraction(47, 24)
    assert timing.uniform_step() == Fraction(1, 24)
    assert timing.duration() == Fraction(2, 1)
    assert timing.is_strictly_monotonic() is True
    assert timing.dts_policy() == "equal_to_pts"

    grid = parse_frame_grid(PROBE, 48, timing)
    assert (grid.frame_rate_num, grid.frame_rate_den) == (24, 1)
    assert grid.frame_count == 48


def test_a_packet_table_that_disagrees_with_the_decoded_count_is_refused() -> None:
    # The container advertised 999 frames, the decoder produced 48, and the packet table has 47.
    # Any pair of those agreeing is not evidence that the third does.
    with pytest.raises(SemanticConformanceError, match="decoded frame count"):
        parse_frame_grid(PROBE, 48, parse_frame_timing(_timing_document(_uniform_pts(47))))


def test_irregular_spacing_yields_no_uniform_step_and_no_derived_duration() -> None:
    irregular = _uniform_pts(4)
    irregular[2] += 7
    timing = parse_frame_timing(_timing_document(irregular))
    assert timing.is_strictly_monotonic() is True
    assert timing.uniform_step() is None
    # Deriving a duration from a mean interval would smooth exactly the defect being looked for.
    assert timing.duration() is None
    with pytest.raises(SemanticConformanceError, match="uniform"):
        parse_frame_grid(PROBE, 4, timing)


def test_duplicate_timestamps_are_observed_rather_than_deduplicated() -> None:
    timing = parse_frame_timing(_timing_document([0, TIMING_STEP, TIMING_STEP, 3 * TIMING_STEP]))
    assert timing.count() == 4
    assert timing.is_strictly_monotonic() is False
    assert timing.duplicate_timestamps() == (TIMING_STEP,)


def test_a_nonmonotonic_table_is_reported_as_nonmonotonic() -> None:
    timing = parse_frame_timing(_timing_document([0, 2 * TIMING_STEP, TIMING_STEP]))
    assert timing.is_strictly_monotonic() is False
    assert timing.duplicate_timestamps() == ()


def test_a_negative_timestamp_is_carried_into_the_observation() -> None:
    timing = parse_frame_timing(_timing_document([-TIMING_STEP, 0, TIMING_STEP]))
    assert timing.has_negative_timestamp() is True
    assert timing.time_at(0) == Fraction(-1, 24)


def test_the_declared_dts_policy_is_observed_rather_than_assumed() -> None:
    equal = parse_frame_timing(_timing_document(_uniform_pts(3)))
    assert equal.dts_policy() == "equal_to_pts"

    leading = parse_frame_timing(
        _timing_document(_uniform_pts(3), dts=[-TIMING_STEP, 0, TIMING_STEP])
    )
    assert leading.dts_policy() == "monotonic_leading"

    backwards = parse_frame_timing(
        _timing_document(_uniform_pts(3), dts=[0, 2 * TIMING_STEP, TIMING_STEP])
    )
    assert backwards.dts_policy() == "nonmonotonic"

    absent = _timing_document(_uniform_pts(3))
    for packet in cast(list[dict[str, Any]], absent["packets"]):
        del packet["dts"]
    assert parse_frame_timing(absent).dts_policy() == "absent"


def test_a_timing_document_without_a_packet_table_is_missing_evidence() -> None:
    with pytest.raises(SemanticConformanceError, match="packet"):
        parse_frame_timing({"streams": [{"codec_type": "video", "time_base": TIMING_TIME_BASE}]})


def test_a_timing_document_without_a_time_base_cannot_be_made_rational() -> None:
    document = _timing_document(_uniform_pts(2))
    del cast(list[dict[str, Any]], document["streams"])[0]["time_base"]
    with pytest.raises(SemanticConformanceError, match="time_base"):
        parse_frame_timing(document)


def test_the_packet_table_is_bounded_so_a_probe_cannot_become_an_unbounded_read() -> None:
    oversized = _timing_document(_uniform_pts(MAX_TIMING_ENTRIES + 1))
    with pytest.raises(SemanticConformanceError, match="bound"):
        parse_frame_timing(oversized)


def test_the_timing_probe_asks_for_packets_and_selects_only_the_video_stream() -> None:
    command = qualification.timing_probe_command(PLACEHOLDER_FFPROBE, Path("artifact.mp4"))
    assert command[0] == str(PLACEHOLDER_FFPROBE)
    assert command[-1] == "artifact.mp4"
    assert command[command.index("-select_streams") + 1] == "v:0"
    entries = command[command.index("-show_entries") + 1]
    assert "packet=" in entries and "pts" in entries and "dts" in entries
    assert "stream=" in entries and "time_base" in entries
    # A decode would make the observation a function of this script rather than of the artifact.
    assert not {"-f", "-pix_fmt", "-vf"} & set(command)


def test_the_comparator_fails_a_final_observation_whose_measured_timing_disagrees() -> None:
    expected = parse_frame_timing(_timing_document(_uniform_pts(4)))
    drifted = _uniform_pts(4)
    drifted[3] += 1
    outcome = _compare(
        expectation=_expectation(frame_grid=FrameGrid(320, 180, 4, 24, 1), frame_timing=expected),
        final=_final(
            frame_grid=FrameGrid(320, 180, 4, 24, 1),
            frame_timing=parse_frame_timing(_timing_document(drifted)),
        ),
    )
    assert outcome.status == "MISMATCH"
    assert {item.mismatch_class for item in outcome.mismatches} == {"frame_grid"}
    assert any("frame_timing" in item.subject for item in outcome.mismatches)


def test_a_final_observation_without_measured_timing_blocks_rather_than_passing() -> None:
    outcome = _compare(
        expectation=_expectation(
            frame_grid=FrameGrid(320, 180, 4, 24, 1),
            frame_timing=parse_frame_timing(_timing_document(_uniform_pts(4))),
        ),
        final=_final(frame_grid=FrameGrid(320, 180, 4, 24, 1), missing=("frame_timing",)),
    )
    assert outcome.status == "BLOCKED"


# --------------------------------------------------------------------------------------------
# F5: one wall deadline over startup, every read and teardown
# --------------------------------------------------------------------------------------------


def _stalling_command(emit_bytes: int, hold_seconds: int) -> tuple[str, ...]:
    """A child that writes less than one block and then holds stdout open without closing it."""

    return (
        sys.executable,
        "-c",
        (
            "import sys, time\n"
            f"sys.stdout.buffer.write(b'\\x00' * {emit_bytes})\n"
            "sys.stdout.buffer.flush()\n"
            f"time.sleep({hold_seconds})\n"
        ),
    )


def test_a_decoder_that_stalls_with_stdout_open_is_killed_at_the_deadline() -> None:
    """F5: the wait timeout could never fire, because the blocking read came first.

    `yield from iter_blocks(...)` ran before `process.wait(timeout=...)`, so a child that stopped
    writing without closing stdout blocked the read forever and the `finally` never ran either.
    """

    started = time.monotonic()
    with pytest.raises(qualification.QualificationError, match="deadline"):
        list(qualification.decode_stream(_stalling_command(4, 120), 4096, deadline_seconds=2))
    elapsed = time.monotonic() - started
    # The bound is the assertion. Ten seconds is generous for a two-second deadline and still far
    # below the 120-second per-artifact limit the old control flow could not enforce at all.
    assert elapsed < 10


def test_the_deadline_covers_teardown_and_leaves_no_child_running() -> None:
    process_ids: list[int] = []
    original = subprocess.Popen

    def record(*args: Any, **kwargs: Any) -> Any:
        child = original(*args, **kwargs)
        process_ids.append(child.pid)
        return child

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess, "Popen", record)
        with pytest.raises(qualification.QualificationError):
            list(qualification.decode_stream(_stalling_command(4, 120), 4096, deadline_seconds=2))
    assert len(process_ids) == 1
    # `Popen.poll` on a reaped child is not enough; ask the operating system.
    assert not qualification.process_is_running(process_ids[0])


def test_a_prompt_decoder_still_streams_its_blocks_inside_the_deadline() -> None:
    command = (
        sys.executable,
        "-c",
        "import sys\nsys.stdout.buffer.write(b'\\x01' * 12)\n",
    )
    assert list(qualification.decode_stream(command, 4, deadline_seconds=30)) == [
        b"\x01" * 4,
        b"\x01" * 4,
        b"\x01" * 4,
    ]


def test_a_decoder_that_exits_nonzero_is_still_a_failure_not_a_deadline() -> None:
    command = (sys.executable, "-c", "raise SystemExit(3)")
    with pytest.raises(qualification.QualificationError, match="failure status"):
        list(qualification.decode_stream(command, 4, deadline_seconds=30))


def _live_readers() -> list[threading.Thread]:
    """Every reader thread this module started that is still alive."""

    return [
        thread
        for thread in threading.enumerate()
        if thread.is_alive()
        and getattr(getattr(thread, "_target", None), "__name__", "") == "_pump_blocks"
    ]


def _writing_command(total_bytes: int) -> tuple[str, ...]:
    """A child that writes a fixed number of bytes and exits."""

    return (
        sys.executable,
        "-c",
        (f"import sys\nsys.stdout.buffer.write(bytes({total_bytes}))\nsys.stdout.buffer.flush()\n"),
    )


def test_a_consumer_that_stops_early_does_not_leave_a_reader_blocked_in_put() -> None:
    """The teardown released the reader exactly once, and it blocked again on the next block.

    The queue holds one item. A consumer that stops after the first block leaves the producer
    holding the second and blocked in `put`; killing the child and closing its stdout cannot
    release a thread that is not blocked in `read`, and the single drain released it only to have
    it block again. One daemon thread and its buffer then survived per aborted artifact -- nothing
    visible in a single decode, and unbounded across a full qualification run.
    """

    before = len(_live_readers())
    stream = qualification.decode_stream(_writing_command(64), 1, deadline_seconds=5)
    assert next(stream) == b"\x00"
    time.sleep(0.1)  # let the producer fill the queue and block on the block after it
    stream.close()
    deadline = time.monotonic() + 5
    while len(_live_readers()) > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_live_readers()) == before


def test_a_consumer_that_raises_mid_stream_leaves_no_reader_behind() -> None:
    before = len(_live_readers())
    with pytest.raises(RuntimeError):
        for _ in qualification.decode_stream(_writing_command(64), 1, deadline_seconds=5):
            raise RuntimeError("the caller failed while consuming")
    deadline = time.monotonic() + 5
    while len(_live_readers()) > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_live_readers()) == before


def test_a_deadline_that_fires_leaves_no_reader_behind_either() -> None:
    before = len(_live_readers())
    with pytest.raises(qualification.QualificationError, match="deadline"):
        list(qualification.decode_stream(_stalling_command(4, 120), 4096, deadline_seconds=2))
    deadline = time.monotonic() + 10
    while len(_live_readers()) > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_live_readers()) == before


def test_a_completed_decode_leaves_no_reader_behind() -> None:
    before = len(_live_readers())
    assert (
        len(list(qualification.decode_stream(_writing_command(64), 64, deadline_seconds=10))) == 1
    )
    deadline = time.monotonic() + 5
    while len(_live_readers()) > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_live_readers()) == before
