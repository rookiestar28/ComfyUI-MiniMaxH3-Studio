"""M25-45 AC45-05: the 1280 x 720 corpus rows, and what must never close one of them.

The four high-resolution rows exist to answer a question the accepted 320 x 180 corpus cannot: what
a layer looks like when the preview presents its own shipped cap with sources that carry real
detail at that size. A row that quietly answers a smaller question -- a legacy 320 px backing, a
landmark the collector skipped, an expectation derived from the other base -- would look exactly
like a pass, so each of those is proved here to be something else.

The stage artifacts are synthetic, as in `tests/test_m25_20_conformance_join.py`: the subject is
what the corpus and the join do with an observation, not a second copy of the runners' own tests.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

from test_m25_20_conformance_join import _landmark_wire, _observation

from comfyui_h3_context.core.semantic_conformance import (
    ACCEPTED_ARTIFACT_RETRIEVAL,
    build_corpus,
)
from comfyui_h3_context.core.semantic_conformance_cases import (
    BASE_FIXTURE_NAMES,
    BASE_PRESENTATION,
    CORPUS_BASE,
    HIGH_RESOLUTION_BASE,
    CaseRecipe,
    build_recipes,
)
from comfyui_h3_context.core.semantic_conformance_drive import run_setup
from comfyui_h3_context.core.semantic_conformance_expect import (
    browser_observable,
    derive_browser_expectation,
    derive_expectation,
    preview_scale,
    required_landmarks,
)
from scripts.m25_45_highres_base_fixture import build as build_high_resolution_base
from scripts.nle_semantic_browser_wires import build_prescription
from scripts.nle_semantic_report import join_row

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
#: The accepted base's own size, and the legacy cap a downscaled presentation would report.
LEGACY_WIDTH, LEGACY_HEIGHT = 320, 180
#: The shipped preview's cap, which is what these rows are about.
CAP_WIDTH, CAP_HEIGHT = 1280, 720
#: How much larger the high-resolution composition is than the accepted one, in each dimension.
SCALE = CAP_WIDTH // LEGACY_WIDTH

BOOK = build_recipes()
CORPUS = build_corpus()
HIGH_RESOLUTION_ROWS = tuple(item for item in BOOK.recipes if item.base == HIGH_RESOLUTION_BASE)


def _snapshot(base: str) -> dict[str, Any]:
    document = json.loads((FIXTURES / BASE_FIXTURE_NAMES[base]).read_text(encoding="utf-8"))
    return cast(dict[str, Any], copy.deepcopy(document["snapshot"]))


def _composition(recipe: CaseRecipe, base: str | None = None) -> dict[str, Any]:
    """The composition this row actually renders, with the fingerprint its edits produce.

    `run_setup` rather than `apply_edits` alone: the join checks a rendered phase against the
    fingerprint of the composition the row's own setup reaches, so a wire whose fingerprint is
    still the base's would fail every row for the wrong reason.
    """

    state = run_setup(recipe, _snapshot(base or recipe.base)).state
    assert state is not None, recipe.case_id
    return cast(dict[str, Any], dict(state.snapshot.to_wire()))


def _render_record(recipe: CaseRecipe) -> dict[str, Any]:
    wire = _composition(recipe)
    expectation = derive_expectation(recipe, wire)
    return {
        "case_id": recipe.case_id,
        "status": "OBSERVED",
        "blocked_code": None,
        "phases": [
            {
                "phase": "subject",
                "status": "OBSERVED",
                "artifact_retrieval": ACCEPTED_ARTIFACT_RETRIEVAL,
                "public_fingerprint": str(wire["public_fingerprint"]),
                "observation": _observation(expectation),
            }
        ],
    }


def _browser_row(recipe: CaseRecipe, **overrides: Any) -> dict[str, Any]:
    wire = _composition(recipe)
    row: dict[str, Any] = {
        "case_id": recipe.case_id,
        "executed": True,
        "missing": [],
        "canvas_width": CAP_WIDTH,
        "canvas_height": CAP_HEIGHT,
        "facts": {"base": recipe.base},
        **_landmark_wire(derive_browser_expectation(recipe, wire), side="browser"),
    }
    row.update(overrides)
    return row


def _joined(recipe: CaseRecipe, *, base: str | None = None, **overrides: Any) -> Any:
    return join_row(
        recipe,
        _snapshot(base or recipe.base),
        {},
        {recipe.case_id: _render_record(recipe)},
        {recipe.case_id: _browser_row(recipe, **overrides)},
    )


def test_the_high_resolution_base_is_the_accepted_base_at_four_times_the_size() -> None:
    """The fixture is derived, so nothing in it is a number somebody typed."""

    accepted = json.loads((FIXTURES / BASE_FIXTURE_NAMES[CORPUS_BASE]).read_text(encoding="utf-8"))
    built = build_high_resolution_base(accepted)["snapshot"]
    stored = _snapshot(HIGH_RESOLUTION_BASE)
    assert built == stored
    assert [stored["output"]["width"], stored["output"]["height"]] == [CAP_WIDTH, CAP_HEIGHT]
    assert accepted["snapshot"]["output"]["width"] * SCALE == CAP_WIDTH
    assert stored["public_fingerprint"] != accepted["snapshot"]["public_fingerprint"]


def test_every_visual_layer_class_has_a_row_with_full_size_landmarks() -> None:
    """Each row observes its own layer, and observes it at the cap rather than near the origin."""

    assert len(HIGH_RESOLUTION_ROWS) == 4
    assert {item.case_id for item in HIGH_RESOLUTION_ROWS} == {
        f"high_resolution.layer.{leaf}"
        for leaf in ("primary_video", "video_overlay", "image_overlay", "text_overlay")
    }
    assert {item.case_id for item in HIGH_RESOLUTION_ROWS} <= {
        case.case_id for case in CORPUS.cases
    }
    for recipe in HIGH_RESOLUTION_ROWS:
        wire = _composition(recipe)
        expectation = derive_expectation(recipe, wire)
        required = required_landmarks(recipe, wire)
        assert required, recipe.case_id
        for landmark in required:
            value = getattr(expectation, landmark, None)
            assert value is not None and value != (), f"{recipe.case_id}: {landmark}"
        # Detail at the cap, not a 320 px picture enlarged: every rectangle stays inside the
        # composition, and at least one landmark lies beyond where the accepted base's canvas ends.
        for item in expectation.geometry:
            assert 0 <= item.left <= item.right <= CAP_WIDTH, recipe.case_id
            assert 0 <= item.top <= item.bottom <= CAP_HEIGHT, recipe.case_id
        reach = max(
            [item.right for item in expectation.geometry]
            + [item.x for item in expectation.patches if hasattr(item, "x")]
        )
        assert reach > LEGACY_WIDTH, recipe.case_id


def test_a_row_with_every_observation_present_passes() -> None:
    """The positive the three negatives below are measured against."""

    for recipe in HIGH_RESOLUTION_ROWS:
        outcome = _joined(recipe)
        assert outcome.status == "PASS", (recipe.case_id, outcome.reason, outcome.mismatches)


def test_a_presentation_capped_at_the_legacy_backing_is_a_mismatch_not_a_pass() -> None:
    """The failure this row exists to catch: the monitor answered at 320 x 180.

    A legacy-capped presentation is self-consistent -- every landmark is where it belongs on a
    smaller canvas -- so nothing but the arithmetic can tell it from the real thing. The join
    compares in composition coordinates, so a quarter-size observation misses by hundreds of
    pixels rather than by a tolerance.
    """

    for recipe in HIGH_RESOLUTION_ROWS:
        expectation = derive_browser_expectation(recipe, _composition(recipe))
        if not expectation.geometry:
            continue
        downscaled = [
            {
                "label": item.label,
                "left": item.left / SCALE,
                "top": item.top / SCALE,
                "right": item.right / SCALE,
                "bottom": item.bottom / SCALE,
            }
            for item in expectation.geometry
        ]
        outcome = _joined(
            recipe,
            canvas_width=LEGACY_WIDTH,
            canvas_height=LEGACY_HEIGHT,
            geometry=downscaled,
        )
        assert outcome.status == "MISMATCH", recipe.case_id
        assert any("geometry" in item.mismatch_class for item in outcome.mismatches), recipe.case_id


def test_a_skipped_landmark_blocks_instead_of_passing() -> None:
    """A collector that returned nothing is missing evidence, never an empty scene."""

    for recipe in HIGH_RESOLUTION_ROWS:
        outcome = _joined(recipe, geometry=[])
        assert outcome.status == "BLOCKED", recipe.case_id
        assert "browser.geometry" in str(outcome.reason), recipe.case_id


def test_a_row_judged_against_the_other_base_cannot_pass() -> None:
    """`recipe.base` is load-bearing, not documentation.

    Every stage resolves a row's composition through the base its recipe names. Were the join to
    fall back to the accepted base, these rows would be compared against a 320 x 180 composition
    while their artifacts and their presentation were 1280 x 720 -- which is the legacy-cap failure
    above, arrived at from the other end.
    """

    for recipe in HIGH_RESOLUTION_ROWS:
        outcome = _joined(recipe, base=CORPUS_BASE)
        assert outcome.status != "PASS", recipe.case_id


def test_a_high_resolution_row_is_observable_only_in_the_presentation_its_base_declares() -> None:
    """B-M2545-13's regression: the browser side of these rows must not disappear.

    Asked without a picture box, the product's own floor says a 1280 x 720 composition is presented
    at a quarter scale, and a row the preview scales down is judged on its final artifact alone.
    That is the correct rule and the wrong question: these rows are presented at ratio 2 in a
    640 x 360 picture area, where the scale is exactly 1.
    """

    for recipe in HIGH_RESOLUTION_ROWS:
        wire = _composition(recipe)
        assert not browser_observable(wire), recipe.case_id
        assert preview_scale(wire) < 1.0, recipe.case_id
        assert browser_observable(wire, recipe.base), recipe.case_id
        assert preview_scale(wire, **BASE_PRESENTATION[recipe.base]) == 1.0, recipe.case_id


def test_the_browser_stage_prescribes_full_size_landmarks_for_every_high_resolution_row() -> None:
    """A prescription is what the journey actually visits; an empty one observes nothing."""

    for recipe in HIGH_RESOLUTION_ROWS:
        wire = _composition(recipe)
        prescription = build_prescription(wire, recipe.base)
        assert prescription["preview_scale"] == 1.0, recipe.case_id
        presentation = prescription["presentation"]
        assert presentation["device_pixel_ratio"] == 2.0, recipe.case_id
        assert [presentation["backing_width"], presentation["backing_height"]] == [
            CAP_WIDTH,
            CAP_HEIGHT,
        ], recipe.case_id
        assert presentation["pane_css_width"] * presentation["device_pixel_ratio"] >= CAP_WIDTH, (
            recipe.case_id
        )
        for key in ("frames", "patch_points", "identity_reads", "geometry_targets"):
            assert prescription[key], f"{recipe.case_id}: {key}"
        # Every prescribed point is a point on the full-size canvas, and at least one lies beyond
        # where the accepted base's canvas would have ended.
        xs = [float(point["canvas_x"]) for point in prescription["patch_points"]]
        assert all(0 <= value < CAP_WIDTH for value in xs), recipe.case_id
        assert max(xs) > LEGACY_WIDTH, recipe.case_id


def test_the_accepted_base_is_presented_exactly_as_it_always_was() -> None:
    """M25-45 adds a presentation; it does not change the one 306 accepted rows were observed in."""

    assert BASE_PRESENTATION[CORPUS_BASE]["device_pixel_ratio"] == 1.0
    assert BASE_PRESENTATION[CORPUS_BASE]["pane_css_width"] == 0.0
    accepted = _snapshot(CORPUS_BASE)
    assert browser_observable(accepted) == browser_observable(accepted, CORPUS_BASE)
    assert preview_scale(accepted) == preview_scale(accepted, **BASE_PRESENTATION[CORPUS_BASE])
    prescription = build_prescription(accepted, CORPUS_BASE)
    assert prescription["presentation"]["backing_width"] == 0
    assert prescription["preview_scale"] == 1.0
