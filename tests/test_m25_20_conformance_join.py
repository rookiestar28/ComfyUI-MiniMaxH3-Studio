"""Joining the three M25-20 stages into one closed report.

The join is where a conformance claim is most easily faked, so these tests are about what it must
refuse to do rather than about the happy path. A row whose stage produced nothing must not quietly
inherit another row's evidence or another stage's answer; a disagreement between the contract and a
measured artifact must survive as `MISMATCH` rather than being reconciled; and a declared negative
must still have executed. The stage artifacts here are synthetic on purpose: the point is the join's
behaviour, not a second copy of the runners' own tests.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest

from comfyui_h3_context.core.semantic_conformance import (
    ACCEPTED_ARTIFACT_RETRIEVAL,
    AUDIO_SURFACE_NEGATIVE_FACTS,
    IMPORT_EFFECT_FACTS,
    IMPORT_IDENTITY_FACTS,
    TOLERANCE_PROFILE,
    SemanticConformanceError,
    build_corpus,
)
from comfyui_h3_context.core.semantic_conformance_cases import (
    BASE_FIXTURE_NAMES,
    CORPUS_BASE,
    CaseRecipe,
    apply_edits,
    build_recipes,
)
from comfyui_h3_context.core.semantic_conformance_compare import (
    BrowserObservation,
    FinalObservation,
    compare_case,
)
from comfyui_h3_context.core.semantic_conformance_drive import command_phases, run_setup
from comfyui_h3_context.core.semantic_conformance_expect import (
    derive_browser_expectation,
    derive_expectation,
    required_landmarks,
)
from comfyui_h3_context.core.semantic_conformance_media import (
    IMPORTED_SOURCE_FRAME_COUNT,
    IMPORTED_SOURCE_PROFILE,
    SOURCE_TIME_BASE_DEN,
    imported_source,
    profile_for,
)
from scripts.nle_semantic_import_scenario import inserted_clip_wire
from scripts.nle_semantic_report import (
    JoinError,
    join,
    join_row,
    read_render,
    read_render_bases,
)
from scripts.nle_semantic_report import (
    _fiducial_absent_from_an_examined_layer as excused,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "m25_20_semantic_corpus_base_v1.json"
BACKEND_PATH = (
    ROOT / ".planning" / "evidence" / "260909-M25-20-corrective" / "backend-qualification.json"
)
BOOK = build_recipes()
CORPUS = build_corpus()


def _base(base: str = CORPUS_BASE) -> dict[str, Any]:
    """A corpus base's snapshot; a row is joined against the base its own recipe names."""

    path = FIXTURE_PATH.parent / BASE_FIXTURE_NAMES[base]
    document = json.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, Any], copy.deepcopy(document["snapshot"]))


def _recipe(observation: str) -> CaseRecipe:
    return next(item for item in BOOK.recipes if item.observation == observation)


def _observation(expectation: Any, *, width: int | None = None) -> dict[str, Any]:
    """What an independent extractor would report for an artifact of this expectation."""

    assert expectation.output_metadata is not None
    assert expectation.frame_grid is not None
    assert expectation.frame_timing is not None
    metadata = dict(expectation.output_metadata.as_pairs())
    grid = {
        "width": expectation.frame_grid.width,
        "height": expectation.frame_grid.height,
        "frame_count": expectation.frame_grid.frame_count,
        "frame_rate_num": expectation.frame_grid.frame_rate_num,
        "frame_rate_den": expectation.frame_grid.frame_rate_den,
    }
    if width is not None:
        metadata["width"] = width
        grid["width"] = width
    count = expectation.frame_timing.count()
    return {
        "extractor_fingerprint": "sha256:" + "1" * 64,
        "output_metadata": metadata,
        "frame_grid": grid,
        # A real mp4 chooses its own time base; the join compares rational times, so a 1/12288
        # table has to agree with an expectation written at 1/24.
        "frame_timing_table": {
            "time_base_num": 1,
            "time_base_den": 12_288,
            "pts_ticks": [index * 512 for index in range(count)],
            "dts_ticks": [index * 512 for index in range(count)],
            "duration_ticks": [512 for _ in range(count)],
        },
        "audio_extent_samples": expectation.audio_extent_samples,
        **_landmark_wire(expectation, side="final"),
    }


def _render_record(recipe: CaseRecipe, *, width: int | None = None) -> dict[str, Any]:
    """A render record that agrees with the row's own expectation, or disagrees by `width`."""

    state = run_setup(recipe, _base(recipe.base)).state
    assert state is not None, recipe.case_id
    wire = dict(state.snapshot.to_wire())
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
                # The row renders one composition and has to say which; the join checks it against
                # the composition the row's own setup reaches.
                "public_fingerprint": str(wire["public_fingerprint"]),
                "observation": _observation(expectation, width=width),
            }
        ],
    }


def _command_recipe(case_id: str = "command.trim_clip.accepted") -> CaseRecipe:
    return BOOK.by_id[case_id]


def _command_record(recipe: CaseRecipe) -> dict[str, Any]:
    """The two artifacts an accepted command row must have rendered, both agreeing with it.

    Each phase is observed at the union of both phases' points, as the render stage does, and
    where only the other phase names a point this artifact shows something the other expectation
    rejects -- which is what a command that really changed the picture leaves behind.
    """

    wires = command_phases(recipe, _base(recipe.base)).wires()
    expectations = [derive_expectation(recipe, wire) for wire in wires]
    return {
        "case_id": recipe.case_id,
        "status": "OBSERVED",
        "blocked_code": None,
        "phases": [
            {
                "phase": name,
                "status": "OBSERVED",
                "artifact_retrieval": ACCEPTED_ARTIFACT_RETRIEVAL,
                "public_fingerprint": wire["public_fingerprint"],
                "observation": _observed_at_both_phases_points(
                    expectations[index], expectations[1 - index], disagree=True
                ),
            }
            for index, (name, wire) in enumerate(zip(("before", "after"), wires, strict=True))
        ],
    }


def _command_backend(recipe: CaseRecipe) -> dict[str, Any]:
    return {
        recipe.case_id: {
            "case_id": recipe.case_id,
            "status": "PASS",
            "reason": None,
            "measurements": [{"subject": "timeline_fingerprint_changed", "value": "True"}],
        }
    }


def _landmark_wire(expectation: Any, *, side: str) -> dict[str, Any]:
    """Serialize the expectation's own landmarks as an observer would report them.

    The synthetic observers agree with the expectation on purpose: these tests are about what the
    join does with an observation, not about whether a real runtime is correct. Every test that
    cares about disagreement perturbs one field of the result.
    """

    wire: dict[str, Any] = {}
    if expectation.source_mapping:
        if side == "final":
            wire["source_mapping"] = [
                {
                    "output_frame": item.output_frame,
                    "source_frame": item.source_frame,
                    "source_pts": item.source_pts,
                    "source_time_base_num": item.source_time_base_num,
                    "source_time_base_den": item.source_time_base_den,
                }
                for item in expectation.source_mapping
            ]
        else:
            wire["source_mapping"] = [
                {
                    "output_frame": item.output_frame,
                    "source_frame": item.source_frame,
                    "source_time": str(item.source_time()),
                }
                for item in expectation.source_mapping
            ]
    if expectation.geometry:
        wire["geometry"] = [
            {
                "label": item.label,
                "left": item.left,
                "top": item.top,
                "right": item.right,
                "bottom": item.bottom,
            }
            for item in expectation.geometry
        ]
    for key in ("patches", "color_patches"):
        samples = getattr(expectation, key)
        if samples:
            wire[key] = [
                {
                    "label": item.label,
                    "size_px": item.size_px,
                    "edge_distance_px": item.edge_distance_px,
                    "red": item.red,
                    "green": item.green,
                    "blue": item.blue,
                }
                for item in samples
            ]
    if expectation.audio_onsets:
        wire["audio_onsets"] = [
            {"label": item.label, "sample_index": item.sample_index}
            for item in expectation.audio_onsets
        ]
    if expectation.silences:
        # A silence observation reports the peak that was measured inside the gap; an expectation
        # that a gap is silent is met by a measured zero, never by the absence of a measurement.
        wire["silences" if side == "final" else "silence_probes"] = [
            {
                "label": item.label,
                "start_sample": item.start_sample,
                "end_sample": item.end_sample,
                "abs_peak": 0,
            }
            for item in expectation.silences
        ]
    if expectation.audio_levels and side == "final":
        # Only the decoded artifact states a level (`BROWSER_UNOBSERVABLE["audio_levels"]`).
        wire["audio_levels"] = [
            {
                "label": item.label,
                "start_sample": item.start_sample,
                "sample_count": item.sample_count,
                "level_ppm": item.level_ppm,
            }
            for item in expectation.audio_levels
        ]
    if expectation.alphas:
        wire["alphas"] = [
            {
                "label": item.label,
                "output_frame": item.output_frame,
                "alpha_milli": item.alpha_milli,
            }
            for item in expectation.alphas
        ]
    if expectation.text is not None:
        wire["text"] = {
            "content": expectation.text.content,
            "font_identity": expectation.text.font_identity,
            "line_count": expectation.text.line_count,
            "weight": expectation.text.weight,
            "style": expectation.text.style,
            "align": expectation.text.align,
            "visible_glyph_ratio_milli": 1_000,
        }
    return wire


def _browser_row(
    case_id: str,
    *,
    executed: bool = True,
    missing: list[str] | None = None,
    recipe: CaseRecipe | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "case_id": case_id,
        "executed": executed,
        "missing": missing or [],
        "canvas_width": 320,
        "canvas_height": 180,
        "facts": {"overlayPresent": True},
    }
    if recipe is not None:
        wire = _base(recipe.base)
        apply_edits(wire, recipe)
        row.update(_landmark_wire(derive_expectation(recipe, wire), side="browser"))
    return row


def _fully_observed(recipe: CaseRecipe) -> tuple[dict[str, Any], dict[str, Any]]:
    """A render record and a browser row that between them carry every required landmark."""

    return _render_record(recipe), _browser_row(recipe.case_id, recipe=recipe)


def test_the_join_reports_every_corpus_row_exactly_once_and_invents_none() -> None:
    outcomes = join({}, {}, {})
    assert [item.case_id for item in outcomes] == [case.case_id for case in CORPUS.cases]
    # With no stage at all, nothing may pass.
    assert {item.status for item in outcomes} <= {"NOT_RUN", "BLOCKED"}


@pytest.mark.skipif(not BACKEND_PATH.is_file(), reason="the backend stage has not been executed")
def test_the_backend_stage_alone_is_admitted_but_never_conformant() -> None:
    """Admission is about identity; conformance is about evidence -- not the same question."""

    from scripts.nle_semantic_conformance import build_report
    from scripts.nle_semantic_report import read_backend

    outcomes = join(read_backend(BACKEND_PATH), {}, {})
    report = build_report(
        outcomes,
        candidate_tree="sha256:" + "0" * 64,
        renderer_fingerprint="sha256:" + "0" * 64,
        probe_fingerprint="sha256:" + "0" * 64,
        extractor_fingerprint="sha256:" + "0" * 64,
        browser_profile_fingerprint="sha256:" + "0" * 64,
        chromium_version="0",
    )
    assert report["required_rows"] == 334
    assert report["admission"]["admitted"] is True
    # Every row is present and unique, and the report still refuses to call itself conformant while
    # 177 rows have no evidence. A report that passed here would be the original defect.
    assert report["conformant"] is False
    assert report["totals"]["NOT_RUN"] > 0


def _landmarked_recipe(family: str | None = None) -> CaseRecipe:
    """A rendering row this join can state and observe every required landmark for."""

    return next(
        item
        for item in BOOK.rendering()
        if item.observation == "render_and_browser" and (family is None or item.family == family)
    )


def test_a_rendering_row_passes_only_when_all_three_observations_are_present() -> None:
    recipe = _landmarked_recipe()
    render_record, browser_row = _fully_observed(recipe)
    render = {recipe.case_id: render_record}
    browser = {recipe.case_id: browser_row}

    outcome = join_row(recipe, _base(), {}, render, browser)
    assert outcome.status == "PASS", (outcome.reason, outcome.mismatches)

    # Drop the browser side and the row must stop passing, not pass on the artifact alone.
    assert join_row(recipe, _base(), {}, render, {}).status == "NOT_RUN"
    # Drop the artifact and the same is true in the other direction.
    assert join_row(recipe, _base(), {}, {}, browser).status == "NOT_RUN"


def test_a_row_whose_required_landmarks_are_absent_blocks_instead_of_passing() -> None:
    """The review's admission counterexample: dimensions and duration are not the picture.

    A row was closed by an artifact with the right container, size, codecs and duration and a
    browser row carrying only its canvas dimensions. Nothing in that says the frames came from the
    right source, or that anything was drawn at all -- the same artifact with every frame black
    would have passed identically. The join now names what is missing and blocks.
    """

    recipe = _landmarked_recipe()
    render_record, browser_row = _fully_observed(recipe)
    del render_record["phases"][0]["observation"]["source_mapping"]
    browser_row.pop("source_mapping", None)
    browser_row["facts"] = {"flat_black": True, "source_frame_id": -999}

    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "BLOCKED"
    assert outcome.reason is not None
    assert "final.source_mapping" in outcome.reason
    assert "browser.source_mapping" in outcome.reason
    # The facts the browser did measure are still retained: a blocked row keeps its evidence.
    assert any(item.subject == "browser.flat_black" for item in outcome.measurements)


def test_a_wrong_source_frame_is_a_mismatch_not_a_pass() -> None:
    """The landmark exists to catch exactly this: right container, wrong picture."""

    recipe = _landmarked_recipe()
    render_record, browser_row = _fully_observed(recipe)
    for entry in render_record["phases"][0]["observation"]["source_mapping"]:
        entry["source_frame"] += 1
        entry["source_pts"] += 512

    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "MISMATCH"
    assert any("source_mapping" in item.subject for item in outcome.mismatches)


def test_a_measured_disagreement_survives_as_a_mismatch() -> None:
    """The join must not reconcile the contract with what was measured."""

    recipe = _landmarked_recipe()
    render = {recipe.case_id: _render_record(recipe, width=321)}
    browser = {recipe.case_id: _browser_row(recipe.case_id, recipe=recipe)}
    outcome = join_row(recipe, _base(), {}, render, browser)
    assert outcome.status == "MISMATCH"
    subjects = {item.subject for item in outcome.mismatches}
    assert "final.output_metadata.width" in subjects


def test_a_blocked_render_names_what_blocked_it_and_does_not_pass() -> None:
    recipe = _landmarked_recipe()
    render = {
        recipe.case_id: {
            "case_id": recipe.case_id,
            "status": "BLOCKED",
            "blocked_code": "render_deadline_exceeded",
            "phases": [],
        }
    }
    outcome = join_row(
        recipe, _base(), {}, render, {recipe.case_id: _browser_row(recipe.case_id, recipe=recipe)}
    )
    assert outcome.status == "BLOCKED"
    assert outcome.reason is not None
    assert "render_deadline_exceeded" in outcome.reason


def test_a_declared_negative_still_has_to_execute() -> None:
    recipe = _recipe("declared_negative")
    assert recipe.supported is False

    unexecuted = join_row(
        recipe, _base(), {}, {}, {recipe.case_id: _browser_row(recipe.case_id, executed=False)}
    )
    assert unexecuted.status == "NOT_RUN"

    # B-66: a seam row is a contract-and-decoder claim that only the backend can prove. A browser
    # row that merely executed does not close it -- that was exactly the vacuity in which a
    # declared negative "passed" on an `overlayPresent` fact that says nothing about audio.
    executed = join_row(recipe, _base(), {}, {}, {recipe.case_id: _browser_row(recipe.case_id)})
    assert executed.status == "NOT_RUN"


def _surface_recipe(surface: str = "compact") -> CaseRecipe:
    return BOOK.by_id[f"deferred_negative.surface_{surface}"]


def _surface_row(surface: str = "compact", **overrides: object) -> dict[str, Any]:
    facts: dict[str, object] = {key: "0" for key in AUDIO_SURFACE_NEGATIVE_FACTS}
    facts["surfaceStatus"] = "compact_ready"
    facts.update(overrides)
    return {
        "case_id": f"deferred_negative.surface_{surface}",
        "executed": True,
        "missing": [],
        "canvas_width": 0,
        "canvas_height": 0,
        "refusal_code": None,
        "facts": facts,
    }


def test_a_surface_negative_is_closed_by_the_browser_only_when_every_count_is_zero() -> None:
    """B-66: the three audio-surface rows are UI claims, each proven by looking at the surface."""

    for surface in ("compact", "expanded", "fallback"):
        recipe = _surface_recipe(surface)
        assert recipe.supported is False and recipe.observation == "declared_negative"
        # Nothing looked: not run, never a free DECLARED_UNSUPPORTED.
        assert join_row(recipe, _base(), {}, {}, {}).status == "NOT_RUN"
        # A backend record cannot close a surface row: it has no way of seeing a control.
        backend_only: dict[str, dict[str, Any]] = {
            recipe.case_id: {
                "case_id": recipe.case_id,
                "status": "DECLARED_UNSUPPORTED",
                "reason": recipe.unsupported_reason,
                "measurements": [],
            }
        }
        assert join_row(recipe, _base(), backend_only, {}, {}).status == "NOT_RUN"
        closed = join_row(recipe, _base(), {}, {}, {recipe.case_id: _surface_row(surface)})
        assert closed.status == "DECLARED_UNSUPPORTED"
        assert closed.reason == recipe.unsupported_reason
        # Every counted negative is retained as a measurement, not summarised away.
        subjects = {item.subject for item in closed.measurements}
        assert {f"browser.{key}" for key in AUDIO_SURFACE_NEGATIVE_FACTS} <= subjects


def test_a_surface_that_exposes_an_audio_control_is_a_mismatch_not_a_negative() -> None:
    recipe = _surface_recipe("expanded")
    exposed = join_row(
        recipe,
        _base(),
        {},
        {},
        {recipe.case_id: _surface_row("expanded", audio_word_controls="2")},
    )
    assert exposed.status == "MISMATCH"
    assert [m.subject for m in exposed.mismatches] == ["browser.audio_word_controls"]
    assert (exposed.mismatches[0].expected, exposed.mismatches[0].observed) == ("0", "2")
    assert exposed.mismatches[0].mismatch_class == "audio_surface"

    shortcut = join_row(
        recipe,
        _base(),
        {},
        {},
        {recipe.case_id: _surface_row("expanded", mute_shortcut_changed_elements="1")},
    )
    assert shortcut.status == "MISMATCH"


def test_a_surface_row_missing_a_required_count_blocks_instead_of_closing() -> None:
    recipe = _surface_recipe("fallback")
    row = _surface_row("fallback")
    del row["facts"]["video_elements_with_controls"]
    absent = join_row(recipe, _base(), {}, {}, {recipe.case_id: row})
    assert absent.status == "BLOCKED"
    assert absent.reason is not None and "video_elements_with_controls" in absent.reason

    unreadable = join_row(
        recipe,
        _base(),
        {},
        {},
        {recipe.case_id: _surface_row("fallback", native_controls_elements="none")},
    )
    assert unreadable.status == "BLOCKED"

    gap = join_row(
        recipe,
        _base(),
        {},
        {},
        {recipe.case_id: {**_surface_row("fallback"), "missing": ["surface not reached"]}},
    )
    assert gap.status == "BLOCKED"

    unexecuted = join_row(
        recipe,
        _base(),
        {},
        {},
        {recipe.case_id: {**_surface_row("fallback"), "executed": False}},
    )
    assert unexecuted.status == "NOT_RUN"


def test_a_shell_row_is_closed_by_the_browser_and_by_nothing_else() -> None:
    recipe = _recipe("shell_invariant")
    assert join_row(recipe, _base(), {}, {}, {}).status == "NOT_RUN"

    blocked = join_row(
        recipe, _base(), {}, {}, {recipe.case_id: _browser_row(recipe.case_id, missing=["focus"])}
    )
    assert blocked.status == "BLOCKED"
    assert blocked.reason is not None and "focus" in blocked.reason

    passed = join_row(recipe, _base(), {}, {}, {recipe.case_id: _browser_row(recipe.case_id)})
    assert passed.status == "PASS"
    # The measured facts are retained, not summarised away: they are the only record that the
    # interaction happened at all.
    assert any(item.subject == "browser.overlayPresent" for item in passed.measurements)


def test_a_refusal_row_takes_the_answer_of_the_authority_that_refuses_it() -> None:
    """A refusal has one authority. A synthesised browser observation would invent evidence."""

    recipe = _recipe("command_refusal")
    backend = {
        recipe.case_id: {
            "case_id": recipe.case_id,
            "status": "PASS",
            "reason": None,
            "measurements": [{"subject": "observed_code", "value": recipe.refusal_code}],
        }
    }
    outcome = join_row(recipe, _base(), backend, {}, {})
    assert outcome.status == "PASS"
    assert [item.value for item in outcome.measurements] == [recipe.refusal_code]

    # Without the backend stage the row is blocked, never inferred from the recipe's declaration.
    assert join_row(recipe, _base(), {}, {}, {}).status == "BLOCKED"


def test_an_accepted_command_row_is_not_closed_by_the_timeline_moving_alone() -> None:
    """The compositions an accepted command spans have to have been rendered too.

    A row that passed on the timeline fingerprint alone would prove that the decoder accepted
    something nobody ever made an artifact from, which is the same substitution in a smaller place.
    """

    recipe = _command_recipe()
    assert recipe.renders is True
    backend = _command_backend(recipe)

    blocked = join_row(recipe, _base(), backend, {}, {})
    assert blocked.status == "BLOCKED"
    assert blocked.reason is not None

    passed = join_row(recipe, _base(), backend, {recipe.case_id: _command_record(recipe)}, {})
    assert passed.status == "PASS", (passed.reason, passed.mismatches)
    # The artifact facts are retained beside the timeline evidence, not instead of it.
    subjects = {item.subject for item in passed.measurements}
    assert "timeline_fingerprint_changed" in subjects
    assert "before.final.output_metadata.container" in subjects
    assert "after.final.output_metadata.container" in subjects
    assert "before.public_fingerprint" in subjects
    assert "after.public_fingerprint" in subjects


def test_an_accepted_command_row_needs_both_phases_in_order_and_once_each() -> None:
    """The review's reproduction was a phase *list*, so the list itself is what is checked.

    Dropping the "after", repeating the "before", or swapping the two all describe a different
    experiment from the one the row declares, and none of them is a weaker version of it.
    """

    recipe = _command_recipe()
    backend = _command_backend(recipe)

    for mutate, expected in (
        (lambda phases: phases[:1], "['before']"),
        (lambda phases: [phases[0], phases[0]], "['before', 'before']"),
        (lambda phases: [phases[1], phases[0]], "['after', 'before']"),
        (lambda phases: [*phases, phases[1]], "['before', 'after', 'after']"),
    ):
        record = _command_record(recipe)
        record["phases"] = mutate(record["phases"])
        outcome = join_row(recipe, _base(), backend, {recipe.case_id: record}, {})
        assert outcome.status == "BLOCKED", expected
        assert outcome.reason is not None and expected in outcome.reason


def test_a_blocked_phase_under_an_observed_record_does_not_close_the_row() -> None:
    """The review's exact counterexample, in its own words.

    `command.create_track.accepted` was closed by a record whose wrapper said `OBSERVED` while its
    single `before` phase said `BLOCKED` and reported `width: -1`, with no `after` phase and no
    browser row at all. A wrapper's status is a claim about the run, never about a phase.
    """

    recipe = _command_recipe("command.create_track.accepted")
    backend = _command_backend(recipe)
    phases: list[dict[str, Any]] = [
        {
            "phase": "before",
            "status": "BLOCKED",
            "blocked_code": "render_deadline_exceeded",
            "observation": {"output_metadata": {"width": -1}},
        }
    ]
    record: dict[str, Any] = {
        "case_id": recipe.case_id,
        "status": "OBSERVED",
        "blocked_code": None,
        "phases": phases,
    }
    outcome = join_row(recipe, _base(), backend, {recipe.case_id: record}, {})
    assert outcome.status == "BLOCKED"
    assert outcome.reason is not None and "['before']" in outcome.reason

    # And with the "after" restored the phase's own BLOCKED status still decides it.
    phases.append(
        {
            "phase": "after",
            "status": "OBSERVED",
            "public_fingerprint": "sha256:" + "2" * 64,
            "observation": {"output_metadata": {"width": -1}},
        }
    )
    outcome = join_row(recipe, _base(), backend, {recipe.case_id: record}, {})
    assert outcome.status == "BLOCKED"
    assert outcome.reason is not None and "render_deadline_exceeded" in outcome.reason


def test_a_phase_that_rendered_another_composition_is_a_mismatch() -> None:
    """A phase's identity is decided by the contract, never by the phase's own label."""

    recipe = _command_recipe()
    record = _command_record(recipe)
    # The stage rendered the "before" composition twice and labelled the second one "after".
    record["phases"][1]["public_fingerprint"] = record["phases"][0]["public_fingerprint"]

    outcome = join_row(recipe, _base(), _command_backend(recipe), {recipe.case_id: record}, {})
    assert outcome.status == "MISMATCH"
    assert any(item.subject == "after.public_fingerprint" for item in outcome.mismatches)


def test_a_phase_reporting_no_composition_at_all_blocks() -> None:
    """An unidentified artifact cannot be bound to a phase, so it is missing evidence."""

    recipe = _command_recipe()
    record = _command_record(recipe)
    del record["phases"][1]["public_fingerprint"]

    outcome = join_row(recipe, _base(), _command_backend(recipe), {recipe.case_id: record}, {})
    assert outcome.status == "BLOCKED"
    assert outcome.reason is not None
    assert "did not report the composition it rendered" in outcome.reason


def test_a_phase_whose_artifact_disagrees_with_its_own_composition_is_a_mismatch() -> None:
    """Each phase is compared against the expectation derived from *its* composition."""

    recipe = _command_recipe()
    wires = command_phases(recipe, _base()).wires()
    record = _command_record(recipe)
    record["phases"][1]["observation"] = _observation(
        derive_expectation(recipe, wires[1]), width=321
    )

    outcome = join_row(recipe, _base(), _command_backend(recipe), {recipe.case_id: record}, {})
    assert outcome.status == "MISMATCH"
    assert any(item.subject.startswith("after.final.") for item in outcome.mismatches)


def _observed_at_both_phases_points(own: Any, other: Any, *, disagree: bool) -> dict[str, Any]:
    """What the render stage reports for one phase of a command row.

    Each phase is observed at the union of both phases' prescribed points, so the observation
    carries its own expectation's landmarks and, under the labels the other phase alone names,
    what the same artifact measured there. With `disagree` those extra measurements are what an
    artifact of *this* phase would plausibly show -- something the other expectation rejects --
    and without it they echo the other expectation, which is what an unchanged picture shows.
    """

    observation = _observation(own)
    extra = _landmark_wire(other, side="final")
    frames = {item["output_frame"] for item in observation.get("source_mapping", ())}
    for entry in extra.get("source_mapping", ()):
        if entry["output_frame"] in frames:
            continue
        if disagree:
            entry = {**entry, "source_frame": entry["source_frame"] + 1}
        observation.setdefault("source_mapping", []).append(entry)
    for key in ("patches", "color_patches"):
        labels = {item["label"] for item in observation.get(key, ())}
        for entry in extra.get(key, ()):
            if entry["label"] not in labels:
                if disagree:
                    entry = {**entry, "red": (entry["red"] + 128) % 256}
                observation.setdefault(key, []).append(entry)
    labels = {item["label"] for item in observation.get("geometry", ())}
    for entry in extra.get("geometry", ()):
        if entry["label"] not in labels:
            if disagree:
                entry = {**entry, "left": entry["left"] + 10, "right": entry["right"] + 10}
            observation.setdefault("geometry", []).append(entry)
    keyed = {(item["label"], item["output_frame"]) for item in observation.get("alphas", ())}
    for entry in extra.get("alphas", ()):
        if (entry["label"], entry["output_frame"]) not in keyed:
            if disagree:
                entry = {**entry, "alpha_milli": (entry["alpha_milli"] + 500) % 1000}
            observation.setdefault("alphas", []).append(entry)
    return observation


def test_the_change_is_proved_by_a_measurement_and_never_by_a_landmark_the_after_lacks() -> None:
    """Each artifact differs from the other phase's expectation by what it measured, or not at all.

    The two phases prescribe different points: the trimmed primary states no identity at the two
    frames it vacated. An after artifact observed only at its own points then "fails" the before
    expectation by absence at exactly those frames -- and a stage that rendered the same picture
    twice would fail it the same way, so that is no proof. The row blocks, naming where an
    artifact was not observed; observed everywhere and found unchanged both ways it mismatches;
    found changed in either direction it passes.
    """

    recipe = _command_recipe("command.trim_clip.accepted")
    wires = command_phases(recipe, _base()).wires()
    before, after = (derive_expectation(recipe, wire) for wire in wires)
    vacated = sorted(
        {item.output_frame for item in before.source_mapping}
        - {item.output_frame for item in after.source_mapping}
    )
    assert vacated, "this row is chosen because the trim vacates identity frames"

    only_own_points = _command_record(recipe)
    for phase, own in zip(only_own_points["phases"], (before, after), strict=True):
        phase["observation"] = _observation(own)
    outcome = join_row(
        recipe, _base(), _command_backend(recipe), {recipe.case_id: only_own_points}, {}
    )
    assert outcome.status == "BLOCKED", (outcome.status, outcome.reason)
    assert outcome.reason is not None
    assert "not observed where the other phase's expectation claims" in outcome.reason
    assert "after:final.source_mapping" in outcome.reason

    unchanged = _command_record(recipe)
    unchanged["phases"][0]["observation"] = _observed_at_both_phases_points(
        before, after, disagree=False
    )
    unchanged["phases"][1]["observation"] = _observed_at_both_phases_points(
        after, before, disagree=False
    )
    outcome = join_row(recipe, _base(), _command_backend(recipe), {recipe.case_id: unchanged}, {})
    assert outcome.status == "MISMATCH", (outcome.status, outcome.reason)
    assert any(item.subject == "after.observable_effect" for item in outcome.mismatches)

    # Either direction is proof: a before artifact the after expectation rejects closes the row
    # even when the after artifact satisfies every claim the before expectation makes.
    one_way = _command_record(recipe)
    one_way["phases"][1]["observation"] = _observed_at_both_phases_points(
        after, before, disagree=False
    )
    outcome = join_row(recipe, _base(), _command_backend(recipe), {recipe.case_id: one_way}, {})
    assert outcome.status == "PASS", (outcome.status, outcome.reason, outcome.mismatches)

    changed = _command_record(recipe)
    outcome = join_row(recipe, _base(), _command_backend(recipe), {recipe.case_id: changed}, {})
    assert outcome.status == "PASS", (outcome.status, outcome.reason, outcome.mismatches)


def test_a_fiducial_the_command_moves_off_the_canvas_is_a_finding_and_not_a_gap() -> None:
    """`set_visual_transform` rotates the primary's top-left mark above the frame.

    The before expectation places that fiducial; the after artifact, whose layer rectangle the
    extractor did observe, carries no such mark. That absence is what the picture shows, not an
    unvisited point, so it must not block a change the layer's own rectangle measured. The
    absence alone is still never the proof (the trim row above pins that).
    """

    recipe = _command_recipe("command.set_visual_transform.accepted")
    wires = command_phases(recipe, _base()).wires()
    before, after = (derive_expectation(recipe, wire) for wire in wires)
    gone = {item.label for item in before.geometry} - {item.label for item in after.geometry}
    assert "clip-main.primary_top_left" in gone, gone
    assert any(item.label == "clip-main" for item in after.geometry)

    # Each phase observed at the union of points; the after artifact reports the layer rectangle
    # and every fiducial the after expectation states, and the moved-off mark is simply not among
    # them. The change is measured by the rectangle and the remaining fiducials themselves.
    record = _command_record(recipe)
    record["phases"][0]["observation"] = _observed_at_both_phases_points(
        before, after, disagree=True
    )
    after_observation = _observed_at_both_phases_points(after, before, disagree=True)
    after_observation["geometry"] = [
        item for item in after_observation["geometry"] if item["label"] not in gone
    ]
    record["phases"][1]["observation"] = after_observation
    outcome = join_row(recipe, _base(), _command_backend(recipe), {recipe.case_id: record}, {})
    assert outcome.status == "PASS", (outcome.status, outcome.reason, outcome.mismatches)

    # The rectangle is what makes the frame "examined": an after artifact without it fails its
    # own expectation before any cross-phase question is asked.
    unexamined = _command_record(recipe)
    unexamined["phases"][0]["observation"] = _observed_at_both_phases_points(
        before, after, disagree=True
    )
    bare = _observed_at_both_phases_points(after, before, disagree=True)
    bare["geometry"] = [item for item in bare["geometry"] if item["label"] != "clip-main"]
    unexamined["phases"][1]["observation"] = bare
    outcome = join_row(recipe, _base(), _command_backend(recipe), {recipe.case_id: unexamined}, {})
    assert outcome.status == "MISMATCH", (outcome.status, outcome.reason)
    assert any(item.subject == "after.final.geometry.clip-main" for item in outcome.mismatches)


def test_only_a_fiducial_of_an_observed_layer_is_excused_and_never_the_layer_itself() -> None:
    """The rule behind the fiducial finding, stated on its own.

    A patch fiducial is excused only for a layer whose own rectangle the artifact carries; the
    rectangle itself is never excused, and nothing outside geometry is.
    """

    assert excused("final.geometry.clip-main.primary_top_left", {"clip-main"})
    assert not excused("final.geometry.clip-main.primary_top_left", set())
    assert not excused("final.geometry.clip-main.primary_top_left", {"clip-image"})
    assert not excused("final.geometry.clip-main", {"clip-main"})
    assert not excused("final.patch.clip-main@0.primary_top_left", {"clip-main"})
    assert not excused("final.source_mapping.3.source_frame", {"clip-main"})


def test_a_command_that_changes_nothing_visible_keeps_its_two_identical_artifacts() -> None:
    """The no-change distinction is preserved, not collapsed into "something must differ".

    `select_clips` writes only the selection, which no artifact can show. Requiring a visible
    difference there would fail a correct run, so the row is judged on the phases agreeing with
    their own compositions and nothing more.
    """

    recipe = _command_recipe("command.select_clips.accepted")
    wires = command_phases(recipe, _base()).wires()
    before, after = (derive_expectation(recipe, wire) for wire in wires)
    assert before == after

    outcome = join_row(
        recipe, _base(), _command_backend(recipe), {recipe.case_id: _command_record(recipe)}, {}
    )
    assert outcome.status == "PASS", (outcome.reason, outcome.mismatches)
    assert any(
        item.subject == "phases.expects_visible_change" and item.value == "False"
        for item in outcome.measurements
    )


def test_a_command_row_whose_landmarks_were_not_observed_blocks_rather_than_passing() -> None:
    """A command with a rendered consequence nobody measured is not conformant.

    `set_effect` changes colour and `set_visual_transform` moves the picture; neither changes the
    container. A record that carries the container but not the landmark must therefore say which
    landmark is missing and block, per phase, rather than reach `PASS` on facts that are not the
    command's subject.
    """

    for case_id, landmark in (
        ("command.set_effect.accepted", "color_patches"),
        ("command.set_visual_transform.accepted", "geometry"),
    ):
        recipe = _command_recipe(case_id)
        wire = _base()
        apply_edits(wire, recipe)
        assert landmark in required_landmarks(recipe, wire)

        # A record that measured everything closes the row, so the block below is about the
        # missing landmark and not about the guard refusing every command.
        complete = join_row(
            recipe,
            _base(),
            _command_backend(recipe),
            {recipe.case_id: _command_record(recipe)},
            {},
        )
        assert complete.status == "PASS", (case_id, complete.reason, complete.mismatches)

        stripped = _command_record(recipe)
        for phase in stripped["phases"]:
            phase["observation"].pop(landmark, None)
        outcome = join_row(
            recipe, _base(), _command_backend(recipe), {recipe.case_id: stripped}, {}
        )
        assert outcome.status == "BLOCKED", case_id
        assert outcome.reason is not None
        assert f"final.before.{landmark}" in outcome.reason, outcome.reason


def test_the_comparator_refuses_to_drop_the_only_side_a_case_has() -> None:
    """Narrowing the compared sides must never be able to compare nothing."""

    recipe = _command_recipe()
    wire = _base()
    apply_edits(wire, recipe)
    expectation = derive_expectation(recipe, wire)
    for sides in ((), ("browser",)):
        with pytest.raises(SemanticConformanceError):
            compare_case(
                expectation,
                BrowserObservation(case_id=recipe.case_id, executed=True),
                FinalObservation(
                    case_id=recipe.case_id,
                    extractor_fingerprint="sha256:" + "1" * 64,
                    executed=True,
                ),
                TOLERANCE_PROFILE,
                sides=sides,
            )


def test_no_accepted_command_row_claims_a_browser_observation() -> None:
    """The narrowing is legitimate only because the corpus gives these rows no canvas.

    If a command row ever acquires a browser observation, `sides=("final",)` in the join stops being
    "the case has no browser side" and becomes "the join declined to look at one".
    """

    for recipe in BOOK.recipes:
        if recipe.case_class == "command" and recipe.renders:
            assert recipe.observation == "command_effect", recipe.case_id


def test_a_refusal_that_produced_an_artifact_is_a_mismatch() -> None:
    """A code-only assertion cannot see this, which is exactly why the join checks it."""

    recipe = _recipe("command_refusal")
    assert recipe.renders is False
    backend = {
        recipe.case_id: {
            "case_id": recipe.case_id,
            "status": "PASS",
            "reason": None,
            "measurements": [{"subject": "observed_code", "value": recipe.refusal_code}],
        }
    }
    outcome = join_row(
        recipe,
        _base(),
        backend,
        {recipe.case_id: {"case_id": recipe.case_id, "status": "OBSERVED", "phases": []}},
        {},
    )
    assert outcome.status == "MISMATCH"
    assert [item.subject for item in outcome.mismatches] == ["final.artifact"]


def test_a_backend_provable_negative_is_taken_from_the_backend_and_the_rest_are_not() -> None:
    """The ten seam rows are a contract-and-decoder claim; the three surface rows are a UI claim."""

    recipe = _recipe("declared_negative")
    backend = {
        recipe.case_id: {
            "case_id": recipe.case_id,
            "status": "DECLARED_UNSUPPORTED",
            "reason": recipe.unsupported_reason,
            "measurements": [
                {"subject": "forged_audio_command", "value": "audio_editing_deferred"}
            ],
        }
    }
    outcome = join_row(recipe, _base(), backend, {}, {})
    assert outcome.status == "DECLARED_UNSUPPORTED"
    assert any(item.subject == "forged_audio_command" for item in outcome.measurements)

    # A backend row that says it did not run must not close the row either; it falls through to the
    # browser side, which has nothing, so the row stays NOT_RUN rather than becoming a free pass.
    not_run = {
        recipe.case_id: {
            "case_id": recipe.case_id,
            "status": "NOT_RUN",
            "reason": "browser scope",
            "measurements": [],
        }
    }
    assert join_row(recipe, _base(), not_run, {}, {}).status == "NOT_RUN"


def test_the_browser_is_held_to_the_decoded_colours_and_the_artifact_to_the_declared() -> None:
    """The one place the two sides are held to different numbers, and why.

    A browser row that reports the primary's yellow mark with the blue channel Chromium presents
    (50 for a declared 40) passes; the same reading on the final side is a mismatch, because the
    artifact is held to the declared colour. The browser side is compared against
    `derive_browser_expectation`, never against a widened bound.
    """

    recipe = BOOK.by_id["transform.rotation_mdeg.interior"]
    wire = _base()
    apply_edits(wire, recipe)
    presented = _landmark_wire(derive_browser_expectation(recipe, wire), side="browser")
    yellow = [
        item for item in presented["patches"] if item["label"].endswith("primary_bottom_right")
    ]
    assert yellow and all(item["blue"] == 48 for item in yellow), yellow
    for item in yellow:
        item["blue"] = 50  # what the pinned Chromium actually presents
    browser_row = _browser_row(recipe.case_id)
    browser_row.update(presented)
    render = {recipe.case_id: _render_record(recipe)}
    outcome = join_row(recipe, _base(), {}, render, {recipe.case_id: browser_row})
    assert outcome.status == "PASS", (outcome.status, outcome.reason, outcome.mismatches)

    # The artifact is not excused by the same model.
    record = _render_record(recipe)
    for item in record["phases"][0]["observation"]["patches"]:
        if item["label"].endswith("primary_bottom_right"):
            item["blue"] = 50
    outcome = join_row(recipe, _base(), {}, {recipe.case_id: record}, {recipe.case_id: browser_row})
    assert outcome.status == "MISMATCH", (outcome.status, outcome.reason)
    assert any(
        item.subject.startswith("final.patch.")
        and item.subject.endswith("primary_bottom_right.blue")
        for item in outcome.mismatches
    )


def test_the_browser_is_asked_for_nothing_the_preview_cannot_carry() -> None:
    """A text row and an audio row pass with a browser row that carries neither.

    `BROWSER_UNOBSERVABLE` explains why the canvas has no text metrics, onsets or silences; the
    comparator has to honour that on the browser side while the final side is still held to all
    of them, or every text and audio row would mismatch on `browser.text` / `browser.onset.*`.
    """

    for case_id in ("text.size_px.interior", "embedded_audio.primary_audible"):
        recipe = BOOK.by_id[case_id]
        render_record, browser_row = _fully_observed(recipe)
        for key in ("text", "audio_onsets", "silence_probes"):
            browser_row.pop(key, None)
        outcome = join_row(
            recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
        )
        assert outcome.status == "PASS", (case_id, outcome.reason, outcome.mismatches)
        # The final side is not excused: strip the same landmarks there and the row mismatches.
        observation = render_record["phases"][0]["observation"]
        for key in ("text", "audio_onsets", "silences"):
            observation.pop(key, None)
        outcome = join_row(
            recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
        )
        assert outcome.status != "PASS", case_id


def test_a_row_the_preview_scales_down_is_judged_on_its_artifact_and_says_so() -> None:
    """The browser side of `output.dimensions_maximum` is its presentation, nothing more.

    The product presents a 1920x1080 output on a 320x180 canvas
    (`BROWSER_OBSERVES_ONLY_AN_UNSCALED_PREVIEW`), so the row is compared on its final artifact
    alone, with a browser row that executed and confirmed the composition but carries no
    landmark, and the outcome records the scale. A browser row that did not execute still
    blocks the row: the composition has to have been presented.
    """

    recipe = BOOK.by_id["output.dimensions_maximum"]
    render_record = _render_record(recipe)
    presented = _browser_row(recipe.case_id)
    presented["canvas_width"], presented["canvas_height"] = 320, 180
    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: presented}
    )
    assert outcome.status == "PASS", (outcome.reason, outcome.mismatches)
    assert any(
        item.subject == "browser.preview_scale" and item.value == "0.166667"
        for item in outcome.measurements
    )
    absent = join_row(
        recipe,
        _base(),
        {},
        {recipe.case_id: render_record},
        {recipe.case_id: _browser_row(recipe.case_id, executed=False)},
    )
    assert absent.status != "PASS"

    # An unscaled row is not excused by the same path.
    unscaled = _landmarked_recipe()
    bare = _browser_row(unscaled.case_id)
    outcome = join_row(
        unscaled,
        _base(),
        {},
        {unscaled.case_id: _render_record(unscaled)},
        {unscaled.case_id: bare},
    )
    assert outcome.status == "BLOCKED", (outcome.status, outcome.reason)


def test_a_malformed_browser_stage_is_refused_rather_than_read_as_a_gap(tmp_path: Path) -> None:
    """A broken collector and an honest gap must stay distinguishable; only one is a finding."""

    from scripts.nle_semantic_report import BROWSER_STAGE_SCHEMA, JoinError, read_browser

    wrong_schema = tmp_path / "wrong-schema.json"
    wrong_schema.write_text(json.dumps({"schema": "something.else", "rows": []}), encoding="utf-8")
    with pytest.raises(JoinError):
        read_browser(wrong_schema)

    # A row without `executed` would otherwise read as unexecuted and be reported `NOT_RUN`, which
    # looks exactly like a row the browser honestly could not reach.
    missing_field = tmp_path / "missing-field.json"
    missing_field.write_text(
        json.dumps(
            {
                "schema": BROWSER_STAGE_SCHEMA,
                "rows": [{"case_id": "x", "missing": [], "canvas_width": 0, "canvas_height": 0}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(JoinError):
        read_browser(missing_field)

    duplicated = tmp_path / "duplicated.json"
    row = {
        "case_id": "x",
        "executed": True,
        "missing": [],
        "canvas_width": 1,
        "canvas_height": 1,
    }
    duplicated.write_text(
        json.dumps({"schema": BROWSER_STAGE_SCHEMA, "rows": [row, row]}), encoding="utf-8"
    )
    with pytest.raises(JoinError):
        read_browser(duplicated)


def test_a_duplicated_measured_timestamp_is_not_laundered_into_a_clean_grid() -> None:
    """Rebuilding timing from a count and a step would make every anomaly disappear.

    This is the exact failure F4 was raised for, one layer further out: the extractor now measures
    the packet table honestly, and the join must carry it through rather than reconstructing a
    sequence that is monotonic by arithmetic.
    """

    recipe = _landmarked_recipe()
    record = _render_record(recipe)
    table = record["phases"][0]["observation"]["frame_timing_table"]
    # One packet repeats the previous presentation stamp. Count, step and duration all still look
    # right; only the sequence itself shows it.
    table["pts_ticks"][7] = table["pts_ticks"][6]
    table["dts_ticks"][7] = table["dts_ticks"][6]

    outcome = join_row(
        recipe,
        _base(),
        {},
        {recipe.case_id: record},
        {recipe.case_id: _browser_row(recipe.case_id, recipe=recipe)},
    )
    assert outcome.status == "MISMATCH"
    subjects = {item.subject for item in outcome.mismatches}
    assert subjects & {
        "final.frame_timing.duplicate_timestamps",
        "final.frame_timing.strictly_monotonic",
        "final.frame_timing.uniform_step",
    }


def test_a_record_without_a_measured_table_blocks_rather_than_being_reconstructed() -> None:
    recipe = _landmarked_recipe()
    record = _render_record(recipe)
    del record["phases"][0]["observation"]["frame_timing_table"]
    outcome = join_row(
        recipe,
        _base(),
        {},
        {recipe.case_id: record},
        {recipe.case_id: _browser_row(recipe.case_id, recipe=recipe)},
    )
    assert outcome.status == "BLOCKED"
    assert outcome.reason is not None
    assert "packet timing table" in outcome.reason


def test_the_join_checks_the_declared_base_and_never_derives_from_it(
    tmp_path: Path,
) -> None:
    """The render stage says which base it used so the join can check it, not so it can supply it.

    This replaces an earlier rule that had the join *derive* from the declared base. That rule was
    wrong, and wrong in the exact way this corrective exists to close: the render stage rewrote the
    fixture's declared source facts to match the media it happened to build -- a source time base
    of 1/24 where the corpus declares 1/12288, and an overlay audio declaration that decides which
    clips own audio at all. Every expectation was then derived from the renderer's own account of
    its own media, and nothing caught it, because none of those fields is hashed into
    `public_fingerprint`: both bases carried the same one. (The landmark-density half of that
    divergence was resolved differently, by giving the corpus a base fixture that declares what a
    real source actually produces; see the fixture this suite loads.)

    A stage that cannot render what the corpus declares has to fail and say so. It must not restate
    the corpus and be believed.
    """

    corpus = _base()
    accepted = join({}, {}, {}, declared_bases={CORPUS_BASE: corpus})
    assert accepted, "the corpus base itself must be accepted"

    rewritten = copy.deepcopy(corpus)
    rewritten["assets"][0]["landmarks"] = [
        {"frame_index": index, "pts": index} for index in range(72)
    ]
    with pytest.raises(JoinError) as landmarks_refused:
        join({}, {}, {}, declared_bases={CORPUS_BASE: rewritten})
    assert "landmarks" in str(landmarks_refused.value)

    retimed = copy.deepcopy(corpus)
    retimed["assets"][0]["source_time_base"] = {"num": 1, "den": 24}
    with pytest.raises(JoinError) as timing_refused:
        join({}, {}, {}, declared_bases={CORPUS_BASE: retimed})
    assert "source_time_base" in str(timing_refused.value)

    reaudioed = copy.deepcopy(corpus)
    reaudioed["assets"][1]["embedded_audio"] = "excluded_overlay_policy"
    reaudioed["assets"][1]["source_sample_count"] = None
    with pytest.raises(JoinError) as audio_refused:
        join({}, {}, {}, declared_bases={CORPUS_BASE: reaudioed})
    assert "embedded_audio" in str(audio_refused.value)

    # The header is still read, and is still never joined as a row.
    artifact = tmp_path / "render.jsonl"
    artifact.write_text(
        json.dumps({"record": "base_wire", "base_wire": corpus})
        + "\n"
        + json.dumps(_render_record(_recipe("render_and_browser")))
        + "\n",
        encoding="utf-8",
    )
    bases = read_render_bases(artifact)
    assert set(bases) == {"corpus"}
    assert bases["corpus"]["assets"][0]["asset_id"] == corpus["assets"][0]["asset_id"]
    assert list(read_render(artifact)) == [_recipe("render_and_browser").case_id]

    bare = tmp_path / "bare.jsonl"
    bare.write_text(json.dumps(_render_record(_command_recipe())) + "\n", encoding="utf-8")
    with pytest.raises(JoinError):
        read_render_bases(bare)


def test_a_black_picture_does_not_satisfy_a_row_that_declares_a_coloured_one() -> None:
    """The plan's black-output negative, at the join boundary.

    An artifact can carry the right container, the right duration, the right frame timing and the
    right source-frame identity and still be a picture of nothing. The colour samples are what
    separate "an artifact was produced" from "the declared picture was produced".
    """

    recipe = _landmarked_recipe("effect")
    render_record, browser_row = _fully_observed(recipe)
    observation = render_record["phases"][0]["observation"]
    assert observation["color_patches"], "this row's subject is colour"
    for entry in observation["color_patches"]:
        entry["red"] = entry["green"] = entry["blue"] = 0
    for entry in browser_row["color_patches"]:
        entry["red"] = entry["green"] = entry["blue"] = 0

    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "MISMATCH"
    subjects = {item.subject for item in outcome.mismatches}
    assert any(subject.startswith("final.patch.") for subject in subjects)
    assert any(subject.startswith("browser.patch.") for subject in subjects)


def test_an_onset_outside_its_bound_does_not_satisfy_a_row_that_declares_the_audio() -> None:
    """Wrong audio is a finding, not a rounding difference.

    The bound is the accepted output profile's own 2,048-sample final impulse tolerance, so an
    onset a whole frame late is outside it by a wide margin and must be reported rather than
    absorbed.
    """

    recipe = _landmarked_recipe("embedded_audio")
    render_record, browser_row = _fully_observed(recipe)
    onsets = render_record["phases"][0]["observation"]["audio_onsets"]
    assert onsets, "this row's subject is the embedded audio"
    for entry in onsets:
        entry["sample_index"] += 8_000

    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "MISMATCH"
    assert any("onset" in item.subject for item in outcome.mismatches)


def test_a_gap_that_is_not_silent_does_not_satisfy_a_row_that_declares_silence() -> None:
    """A measured peak inside a declared gap is the ownership failure the corpus exists to find."""

    recipe = _landmarked_recipe("embedded_audio")
    render_record, browser_row = _fully_observed(recipe)
    silences = render_record["phases"][0]["observation"]["silences"]
    assert silences, "this row declares gaps between its bursts"
    silences[0]["abs_peak"] = 4_096

    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "MISMATCH"
    assert any("silence" in item.subject for item in outcome.mismatches)


def test_a_layer_placed_somewhere_else_does_not_satisfy_a_geometry_row() -> None:
    """A transform row is about where the picture is, and two pixels is the whole tolerance."""

    recipe = _landmarked_recipe("transform")
    render_record, browser_row = _fully_observed(recipe)
    for section in (render_record["phases"][0]["observation"], browser_row):
        for entry in section["geometry"]:
            entry["left"] += 9
            entry["right"] += 9

    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "MISMATCH"
    assert any("geometry" in item.subject for item in outcome.mismatches)


def test_every_rendering_family_can_be_closed_by_observations_that_agree() -> None:
    """The fail-closed rule must not have made a conformant run unreachable.

    Blocking on an absent landmark is only honest if a complete observation can still pass. This
    walks one row of every rendering family through the join with observers that agree with it.

    GUARD: keep this alongside the blocking tests above. A requirement added to `FAMILY_LANDMARKS`
    with no derivation and no reader behind it makes every row of that family block forever, which
    reads in a report exactly like a stage that has not run yet. This is the test that notices.
    """

    # The `command` family has no `render_and_browser` rows: an accepted command is judged on
    # its own before/after pair, which its own tests above walk end to end.
    families = sorted(
        {item.family for item in BOOK.rendering() if item.observation == "render_and_browser"}
    )
    assert len(families) >= 8, families
    for family in families:
        recipe = _landmarked_recipe(family)
        render_record, browser_row = _fully_observed(recipe)
        outcome = join_row(
            recipe,
            _base(recipe.base),
            {},
            {recipe.case_id: render_record},
            {recipe.case_id: browser_row},
        )
        assert outcome.status == "PASS", (
            family,
            recipe.case_id,
            outcome.reason,
            outcome.mismatches,
        )


def test_the_report_admits_only_an_artifact_the_accepted_transport_returned() -> None:
    """Reading the renderer's own staging file is not proof that the transport returns it.

    Two different claims hide behind "the artifact was observed": that the encoder produced the
    right bytes, and that the accepted retrieval path hands those bytes back unchanged. The render
    stage labels each phase with the one it has; the join admits only the verified download
    (`ACCEPTED_ARTIFACT_RETRIEVAL`), records it, and blocks anything else by name -- a staging
    read, a route the stage invented, or no label at all.
    """

    recipe = _landmarked_recipe()
    render_record, browser_row = _fully_observed(recipe)
    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "PASS", (outcome.reason, outcome.mismatches)
    assert any(
        item.subject.endswith(".artifact_retrieval") and item.value == ACCEPTED_ARTIFACT_RETRIEVAL
        for item in outcome.measurements
    )
    for label in ("direct_staging_read", "verified_original_via_transport", None):
        staged = _render_record(recipe)
        staged["phases"][0]["artifact_retrieval"] = label
        outcome = join_row(
            recipe, _base(), {}, {recipe.case_id: staged}, {recipe.case_id: browser_row}
        )
        assert outcome.status == "BLOCKED", (label, outcome.status, outcome.reason)
        assert outcome.reason is not None
        assert "not retrieved through the accepted transport" in outcome.reason
        assert (label or "unlabelled") in outcome.reason

    command = _command_recipe()
    command_record = _command_record(command)
    outcome = join_row(
        command, _base(), _command_backend(command), {command.case_id: command_record}, {}
    )
    assert outcome.status == "PASS", (outcome.reason, outcome.mismatches)
    retrievals = {
        item.subject: item.value
        for item in outcome.measurements
        if item.subject.endswith(".artifact_retrieval")
    }
    assert retrievals == {
        "before.artifact_retrieval": ACCEPTED_ARTIFACT_RETRIEVAL,
        "after.artifact_retrieval": ACCEPTED_ARTIFACT_RETRIEVAL,
    }
    command_record["phases"][1]["artifact_retrieval"] = "direct_staging_read"
    outcome = join_row(
        command, _base(), _command_backend(command), {command.case_id: command_record}, {}
    )
    assert outcome.status == "BLOCKED", (outcome.status, outcome.reason)
    assert outcome.reason is not None
    assert outcome.reason.startswith("phase after:")


def test_a_property_row_does_not_close_on_a_blocked_phase_or_a_wrong_composition() -> None:
    """The reported counterexample, verbatim: the command repair had not swept its own class.

    `_command_row_phases` validated phase order, each phase's own status and each phase's reported
    composition -- but only for accepted command rows. Every other rendering row still went straight
    to `final_observation`, which read the last phase's observation after checking the *wrapper's*
    status. So a record could say `OBSERVED` at the top, carry a single phase whose own status was
    `BLOCKED` and whose `public_fingerprint` named another composition entirely, and still close the
    row -- the R2-F3 defect surviving in every path the repair did not touch.

    Both halves are pinned here, because they fail independently: a phase that did not observe, and
    a phase that observed something else.
    """

    recipe = _landmarked_recipe()
    render_record, browser_row = _fully_observed(recipe)
    baseline = join_row(
        recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
    )
    assert baseline.status == "PASS", (baseline.reason, baseline.mismatches)

    blocked = copy.deepcopy(render_record)
    blocked["phases"][0]["status"] = "BLOCKED"
    blocked["phases"][0]["blocked_code"] = "decoder_unavailable"
    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: blocked}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "BLOCKED", outcome.status
    assert "decoder_unavailable" in str(outcome.reason)

    wrong = copy.deepcopy(render_record)
    wrong["phases"][0]["public_fingerprint"] = "sha256:" + "b" * 64
    outcome = join_row(recipe, _base(), {}, {recipe.case_id: wrong}, {recipe.case_id: browser_row})
    assert outcome.status == "MISMATCH", outcome.status
    assert any(item.subject == "final.public_fingerprint" for item in outcome.mismatches)

    missing = copy.deepcopy(render_record)
    del missing["phases"][0]["public_fingerprint"]
    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: missing}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "BLOCKED", outcome.status
    assert "did not report the composition" in str(outcome.reason)

    doubled = copy.deepcopy(render_record)
    doubled["phases"] = [doubled["phases"][0], copy.deepcopy(doubled["phases"][0])]
    outcome = join_row(
        recipe, _base(), {}, {recipe.case_id: doubled}, {recipe.case_id: browser_row}
    )
    assert outcome.status == "BLOCKED", outcome.status
    assert "not one rendered composition" in str(outcome.reason)


def test_an_import_integration_row_is_held_to_the_same_phase_rules() -> None:
    """The rule is per rendering path, not per family.

    `import_integration` joins the same three ways a property row does, so a repair that covered
    only the rows someone happened to test would leave the same hole in the next path. This walks
    the corpus for a row of that observation and applies the counterexample to it as well.
    """

    candidates = [recipe for recipe in BOOK.recipes if recipe.observation == "import_integration"]
    assert candidates, "the corpus is supposed to carry import_integration rows"

    for recipe in candidates:
        render_record, browser_row = _fully_observed(recipe)
        render_record["phases"][0]["status"] = "BLOCKED"
        render_record["phases"][0]["blocked_code"] = "decoder_unavailable"
        outcome = join_row(
            recipe, _base(), {}, {recipe.case_id: render_record}, {recipe.case_id: browser_row}
        )
        assert outcome.status == "BLOCKED", (recipe.case_id, outcome.status)


# ---------------------------------------------------------------------------------------------
# B-65: the two import rows are judged on the imported composition both halves drove
# ---------------------------------------------------------------------------------------------

IMPORTED_ASSET_ID = "generated.0123456789abcdef0123456789abcdef"


def _import_recipe(gesture: str = "pointer") -> CaseRecipe:
    return BOOK.by_id[f"import_integration.generated_source.explicit_import_then_insert.{gesture}"]


@pytest.mark.parametrize(
    ("content_end_exclusive", "expected_start", "expected_duration"),
    [(0, 0, 124), (3560, 3560, 40)],
)
def test_import_insert_scenario_matches_source_duration_and_remaining_capacity(
    content_end_exclusive: int,
    expected_start: int,
    expected_duration: int,
) -> None:
    """The real Add control uses the probed source interval, clamped once to remaining capacity."""

    asset = _imported_asset()
    state = {
        "timeline_revision": 7,
        "clips": [],
        "tracks": [
            {
                "track_id": "track-primary",
                "kind": "primary_video",
                "enabled": True,
                "locked": False,
            }
        ],
        "assets": [asset],
        "edit_capacity_frames": 3600,
        "content_end_exclusive": content_end_exclusive,
    }

    clip = inserted_clip_wire(state, IMPORTED_ASSET_ID)

    assert clip["start_frame"] == expected_start
    assert clip["duration_frames"] == expected_duration


def _imported_asset() -> dict[str, Any]:
    """The asset entry the product mints for the imported source, with its measured table."""

    return {
        "asset_id": IMPORTED_ASSET_ID,
        "kind": "video",
        "source_time_base": {"num": 1, "den": SOURCE_TIME_BASE_DEN},
        "source_frame_count": IMPORTED_SOURCE_FRAME_COUNT,
        "source_sample_count": _base()["assets"][0]["source_sample_count"],
        "embedded_audio": "present_bound",
        "timestamp_policy": "nonnegative_monotonic_v1",
        "landmarks": [
            {"frame_index": index, "pts": pts, "dts": pts, "duration_ticks": duration}
            for index, pts, duration in IMPORTED_SOURCE_PROFILE.pts_table()
        ],
    }


def _import_wire() -> dict[str, Any]:
    """The product's Authoring timeline after the explicit insertion, synthesized for the join.

    The shape is the real one (the 1920x1080 content-mode output and inserted clip both span the
    imported source's independently measured 124-frame interval; one primary track), carried on
    the corpus base's identity fields. These tests are about what the join does with the record, not
    about whether the registries are right -- the render stage's own run establishes that.
    """

    wire = _base()
    wire["output"] = {
        **wire["output"],
        "width": 1920,
        "height": 1080,
        "duration_frames": IMPORTED_SOURCE_FRAME_COUNT,
    }
    wire["assets"] = [_imported_asset()]
    wire["tracks"] = [track for track in wire["tracks"] if track["kind"] == "primary_video"]
    wire["timeline_revision"] = 7
    wire["clips"] = [
        {
            "clip_id": "clip-r7-1",
            "asset_id": IMPORTED_ASSET_ID,
            "track_id": wire["tracks"][0]["track_id"],
            "start_frame": 0,
            "duration_frames": IMPORTED_SOURCE_FRAME_COUNT,
            "source_start_frame": 0,
            "enabled": True,
            "transform": {
                "anchor_x_bp": 5000,
                "anchor_y_bp": 5000,
                "position_x_bp": 0,
                "position_y_bp": 0,
                "scale_x_bp": 10000,
                "scale_y_bp": 10000,
                "rotation_mdeg": 0,
            },
            "crop": {"left_bp": 0, "top_bp": 0, "right_bp": 0, "bottom_bp": 0},
            "opacity_bp": 10000,
            "blend": "normal",
            "text": None,
            "transition": {"kind": "none", "duration_frames": 0},
            "effect": {
                "kind": "none",
                "brightness_permille": 0,
                "contrast_permille": 1000,
                "saturation_permille": 1000,
            },
        }
    ]
    return wire


def _chain_facts(gesture: str) -> dict[str, Any]:
    """The facts both halves record about the chain, as the render stage spells them."""

    return {
        "gesture": gesture,
        "source_fingerprint": "sha256:" + "a" * 64,
        "imported_source_frame_count": IMPORTED_SOURCE_FRAME_COUNT,
        "imported_source_landmark_table_sha256": "b" * 64,
        "receipt_rows": 1,
        "import_changed_timeline_revision": False,
        "import_changed_timeline_fingerprint": False,
        "import_advanced_workspace_revision": True,
        "pre_import_timeline_revision": 7,
        "post_import_timeline_revision": 7,
        "post_insert_timeline_revision": 8,
        "post_undo_timeline_revision": 9,
        "post_redo_timeline_revision": 10,
        "inserted_clip_id": "clip-r7-1",
        "inserted_clip_track_id": "track-primary",
        "inserted_clip_start_frame": 0,
        "inserted_clip_duration_frames": IMPORTED_SOURCE_FRAME_COUNT,
        "inserted_clip_source_start_frame": 0,
        "output_width": 1920,
        "output_height": 1080,
        "output_duration_frames": IMPORTED_SOURCE_FRAME_COUNT,
        "post_import_clip_count": 0,
        "post_insert_clip_count": 1,
        "post_undo_clip_count": 0,
        "post_redo_clip_count": 1,
        "undo_restored_pre_insert_timeline": True,
        "library_retained_after_undo": True,
        "redo_restored_post_insert_timeline": True,
    }


def _import_record(recipe: CaseRecipe, wire: dict[str, Any]) -> dict[str, Any]:
    """A render record of the imported composition whose artifact agrees with its expectation."""

    gesture = recipe.case_id.rsplit(".", 1)[-1]
    with imported_source(IMPORTED_ASSET_ID):
        expectation = derive_expectation(recipe, wire)
    return {
        "case_id": recipe.case_id,
        "status": "OBSERVED",
        "blocked_code": None,
        "phases": [
            {
                "phase": "post_insert",
                "status": "OBSERVED",
                "artifact_retrieval": ACCEPTED_ARTIFACT_RETRIEVAL,
                "public_fingerprint": str(wire["public_fingerprint"]),
                "observation": _observation(expectation),
            }
        ],
        "import": {
            **_chain_facts(gesture),
            "imported_asset_id": IMPORTED_ASSET_ID,
            "composition": wire,
        },
    }


def _import_browser_row(recipe: CaseRecipe) -> dict[str, Any]:
    gesture = recipe.case_id.rsplit(".", 1)[-1]
    return {
        "case_id": recipe.case_id,
        "executed": True,
        "missing": [],
        # The product's fixed 1080p timeline is presented scaled down; the join judges the
        # artifact and records the presentation.
        "canvas_width": 960,
        "canvas_height": 540,
        "facts": {**_chain_facts(gesture), "importRequestCount": 1},
    }


def _import_outcome(recipe: CaseRecipe, record: dict[str, Any], row: dict[str, Any]) -> Any:
    return join_row(recipe, _base(), {}, {recipe.case_id: record}, {recipe.case_id: row})


def test_an_import_row_is_judged_on_the_imported_composition_never_the_corpus_base() -> None:
    """B-65: the corpus base declares no imported asset, so a record of it proves nothing here.

    Before the repair the two import rows were joined like any property row -- expectation from
    the base, artifact of the base -- and PASSED on a composition the journey never produced.
    """

    for gesture in ("pointer", "keyboard"):
        recipe = _import_recipe(gesture)
        render_record, browser_row = _fully_observed(recipe)
        outcome = _import_outcome(recipe, render_record, browser_row)
        assert outcome.status == "BLOCKED", (gesture, outcome.status, outcome.reason)
        assert "imported composition" in str(outcome.reason)


def test_an_import_row_closes_when_both_halves_agree_and_the_artifact_matches() -> None:
    for gesture in ("pointer", "keyboard"):
        recipe = _import_recipe(gesture)
        wire = _import_wire()
        outcome = _import_outcome(recipe, _import_record(recipe, wire), _import_browser_row(recipe))
        assert outcome.status == "PASS", (
            gesture,
            outcome.status,
            outcome.reason,
            outcome.mismatches,
        )
        subjects = {item.subject for item in outcome.measurements}
        # The chain's identities and the presentation are kept as evidence, not dropped.
        assert "import.inserted_clip_id" in subjects
        assert "browser.inserted_clip_id" in subjects
        assert "browser.preview_scale" in subjects
        assert "final.public_fingerprint" in subjects


def test_an_import_row_whose_halves_disagree_on_a_chain_identity_is_a_mismatch() -> None:
    """The browser inserted one clip and the render stage another: not the same chain."""

    recipe = _import_recipe()
    row = _import_browser_row(recipe)
    row["facts"]["inserted_clip_id"] = "clip-r7-2"
    outcome = _import_outcome(recipe, _import_record(recipe, _import_wire()), row)
    assert outcome.status == "MISMATCH"
    assert [(m.mismatch_class, m.subject, m.expected, m.observed) for m in outcome.mismatches] == [
        ("identity", "browser.inserted_clip_id", "clip-r7-1", "clip-r7-2")
    ]


def test_an_import_row_missing_a_chain_identity_on_either_side_blocks() -> None:
    recipe = _import_recipe()
    for side in ("browser", "import"):
        record = _import_record(recipe, _import_wire())
        row = _import_browser_row(recipe)
        target = row["facts"] if side == "browser" else record["import"]
        del target["post_undo_timeline_revision"]
        outcome = _import_outcome(recipe, record, row)
        assert outcome.status == "BLOCKED", (side, outcome.status)
        assert f"{side}.post_undo_timeline_revision" in str(outcome.reason)


def test_every_identity_and_effect_fact_is_required_of_both_halves() -> None:
    """Sweep the tables: dropping any one fact from either side blocks the row."""

    recipe = _import_recipe()
    for key in (*IMPORT_IDENTITY_FACTS, *IMPORT_EFFECT_FACTS):
        for side in ("browser", "import"):
            record = _import_record(recipe, _import_wire())
            row = _import_browser_row(recipe)
            target = row["facts"] if side == "browser" else record["import"]
            del target[key]
            outcome = _import_outcome(recipe, record, row)
            assert outcome.status == "BLOCKED", (key, side, outcome.status)
            assert f"{side}.{key}" in str(outcome.reason), (key, side, outcome.reason)


def test_an_import_row_whose_undo_did_not_restore_the_composition_is_a_mismatch() -> None:
    """AC10's undo/redo across pre-import history is a measured effect, not a gesture count."""

    recipe = _import_recipe()
    record = _import_record(recipe, _import_wire())
    row = _import_browser_row(recipe)
    record["import"]["undo_restored_pre_insert_timeline"] = False
    row["facts"]["undo_restored_pre_insert_timeline"] = False
    row["facts"]["library_retained_after_undo"] = False
    outcome = _import_outcome(recipe, record, row)
    assert outcome.status == "MISMATCH"
    found = {(m.mismatch_class, m.subject, m.expected, m.observed) for m in outcome.mismatches}
    assert found == {
        ("import_effect", "import.undo_restored_pre_insert_timeline", "True", "False"),
        ("import_effect", "browser.undo_restored_pre_insert_timeline", "True", "False"),
        ("import_effect", "browser.library_retained_after_undo", "True", "False"),
    }


def test_an_import_row_whose_asset_is_not_the_imported_source_blocks() -> None:
    """The product measured a different frame table: no profile is borrowed for it."""

    recipe = _import_recipe()
    wire = _import_wire()
    wire["assets"][0]["landmarks"][3]["pts"] += 1
    outcome = _import_outcome(recipe, _import_record(recipe, wire), _import_browser_row(recipe))
    assert outcome.status == "BLOCKED"
    assert "frame table" in str(outcome.reason)


def test_an_import_row_whose_artifact_shows_the_wrong_source_frame_is_a_mismatch() -> None:
    """The final comparison is live: the source-range mapping is compared, not just recorded."""

    recipe = _import_recipe()
    record = _import_record(recipe, _import_wire())
    observation = record["phases"][0]["observation"]
    assert observation["source_mapping"], "the imported composition states a source mapping"
    observation["source_mapping"][3]["source_frame"] += 1
    outcome = _import_outcome(recipe, record, _import_browser_row(recipe))
    assert outcome.status == "MISMATCH"
    assert {m.mismatch_class for m in outcome.mismatches} == {"source_mapping"}


def test_an_import_row_whose_phase_rendered_another_composition_is_a_mismatch() -> None:
    recipe = _import_recipe()
    record = _import_record(recipe, _import_wire())
    record["phases"][0]["public_fingerprint"] = "sha256:" + "f" * 64
    outcome = _import_outcome(recipe, record, _import_browser_row(recipe))
    assert outcome.status == "MISMATCH"
    assert [(m.mismatch_class, m.subject) for m in outcome.mismatches] == [
        ("identity", "final.public_fingerprint")
    ]


def test_an_import_row_still_needs_the_browser_to_have_presented_it() -> None:
    recipe = _import_recipe()
    record = _import_record(recipe, _import_wire())
    assert _import_outcome(recipe, record, _browser_row(recipe.case_id, executed=False)).status == (
        "NOT_RUN"
    )
    gap = _import_browser_row(recipe)
    gap["missing"] = ["monitor canvas: not presented"]
    outcome = _import_outcome(recipe, record, gap)
    assert outcome.status == "BLOCKED"
    assert "monitor canvas" in str(outcome.reason)


def test_an_import_row_reads_the_gatherer_s_stringified_facts_as_the_render_stage_s_values() -> (
    None
):
    """The browser stage document spells every fact as text (`"true"`, `"124"`, `"null"`).

    Held to the render stage's JSON booleans and integers, those must compare equal, and a
    `"null"` is an absent observation, not a value that happens to differ.
    """

    recipe = _import_recipe()
    row = _import_browser_row(recipe)
    row["facts"] = {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in row["facts"].items()
    }
    assert row["facts"]["undo_restored_pre_insert_timeline"] == "true"
    assert row["facts"]["output_duration_frames"] == str(IMPORTED_SOURCE_FRAME_COUNT)
    outcome = _import_outcome(recipe, _import_record(recipe, _import_wire()), row)
    assert outcome.status == "PASS", (outcome.status, outcome.reason, outcome.mismatches)
    row["facts"]["post_undo_clip_count"] = "null"
    outcome = _import_outcome(recipe, _import_record(recipe, _import_wire()), row)
    assert outcome.status == "BLOCKED"
    assert "browser.post_undo_clip_count" in str(outcome.reason)


def test_the_imported_profile_answers_only_inside_the_alias_and_only_for_that_asset() -> None:
    """`profile_for` never learns the minted id by itself, and forgets it on exit."""

    assert profile_for(IMPORTED_ASSET_ID) is None
    with imported_source(IMPORTED_ASSET_ID):
        assert profile_for(IMPORTED_ASSET_ID) is IMPORTED_SOURCE_PROFILE
        assert profile_for("generated.other") is None
        assert profile_for("vid-primary") is not None
    assert profile_for(IMPORTED_ASSET_ID) is None
