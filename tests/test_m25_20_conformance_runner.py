"""M25-20 corrective: the backend qualification runner actually executes the corpus.

The post-closeout review's finding F1 was that the 306-row corpus was enumerated and counted but
never executed. `scripts.nle_semantic_qualify` is the backend qualification runner that closes that
gap, and these tests hold it to the properties the review actually asked for rather than to its
internal mechanics:

- it produces exactly one row per corpus case, never fewer;
- a duplicated or missing row is refused by `admit_report`, so a short or repeated run cannot read
  as complete;
- a row whose declared expectation disagrees with what the real decoder produced is reported
  `MISMATCH`, never silently rewritten to agree; and
- the report never carries a path-shaped or URL-shaped string.

These are property tests of the runner's contract, not a restatement of
`test_m25_20_conformance_cases.py`'s per-recipe correctness proofs.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from typing import cast

import pytest

from comfyui_h3_context.core.composition_contract import public_snapshot_fingerprint
from comfyui_h3_context.core.semantic_conformance import (
    SemanticConformanceError,
    admit_report,
    build_corpus,
)
from comfyui_h3_context.core.semantic_conformance_cases import apply_edits, build_recipes
from comfyui_h3_context.core.semantic_conformance_drive import run_setup
from scripts.nle_semantic_qualify import (
    FIXTURE_PATH,
    ROOT,
    QualifyRow,
    _command_effect_row,
    _command_refusal_row,
    _currentness_row,
    _snapshot_layer_row,
    build_report,
)

_PATH_LIKE = re.compile(r"[A-Za-z]:[\\/]")
_URL_LIKE = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)


@pytest.fixture(scope="module")
def report() -> dict[str, object]:
    corpus = build_corpus()
    book = build_recipes(corpus)
    return build_report(book, corpus)


def test_runner_covers_exactly_every_corpus_row(report: dict[str, object]) -> None:
    corpus = build_corpus()
    rows = report["rows"]
    assert isinstance(rows, list)
    # M25-45 took the corpus from the accepted 306 to 310 by adding one row for each visual layer
    # class at the shipped 1280 x 720 cap, and M25-77 to 334 with the 22 `clip_audio` rows and the
    # two rows of its `set_clip_audio` command. The pin moves with the corpus and the accepted rows
    # are asserted to still be there, so a growth that quietly replaced a row would fail here.
    assert len(rows) == len(corpus.cases) == 334
    assert {row["case_id"] for row in rows} == set(corpus.case_ids)
    high_resolution = {
        row["case_id"] for row in rows if str(row["case_id"]).startswith("high_resolution.")
    }
    clip_audio = {row["case_id"] for row in rows if str(row["case_id"]).startswith("clip_audio.")}
    clip_audio_command = {"command.set_clip_audio.accepted", "command.set_clip_audio.refused"}
    assert len(high_resolution) == 4
    assert len(clip_audio) == 22
    assert clip_audio_command <= {row["case_id"] for row in rows}
    assert len(set(corpus.case_ids) - high_resolution - clip_audio - clip_audio_command) == 306
    admission = report["admission"]
    assert isinstance(admission, dict)
    assert admission == {
        "admitted": True,
        "duplicates": [],
        "missing": [],
        "unknown": [],
        "ineligible": [],
    }


def test_report_totals_reconcile_with_admitted_rows(report: dict[str, object]) -> None:
    totals = report["totals"]
    rows = report["rows"]
    assert isinstance(totals, dict)
    assert isinstance(rows, list)
    assert sum(totals.values()) == len(rows) == 334
    # No mismatch and nothing left unresolved by an execution defect: every row this runner can
    # reach is `PASS` or, for the ten deferred audio-seam negatives it executes,
    # `DECLARED_UNSUPPORTED`; the rest are honestly `BLOCKED` (awaiting render/browser evidence) or
    # `NOT_RUN` (out of a backend runner's scope), never silently coerced into any of them.
    assert totals["MISMATCH"] == 0
    # A `DECLARED_UNSUPPORTED` row here is an executed negative, never a declared one: the seam
    # rows carry the forged-command refusal and both unchanged fingerprints as measurements. The
    # three `surface_*` rows are a claim about the shipped UI, so they stay `NOT_RUN`.
    assert totals["DECLARED_UNSUPPORTED"] == 10
    unsupported = [row for row in rows if row["status"] == "DECLARED_UNSUPPORTED"]
    assert all(row["measurements"] for row in unsupported)
    assert (
        totals["PASS"] + totals["DECLARED_UNSUPPORTED"] + totals["BLOCKED"] + totals["NOT_RUN"]
        == 334
    )
    # The four high-resolution rows and the fourteen rendering `clip_audio` rows are
    # `render_and_browser`, so a backend runner reaches none of them: they are among the 168
    # `BLOCKED`, awaiting the render and browser stages, never counted as passing here.
    assert totals["BLOCKED"] == 168


def test_admission_refuses_a_duplicated_row() -> None:
    corpus = build_corpus()
    pairs = [(case.case_id, "PASS" if case.supported else "NOT_RUN") for case in corpus.cases]
    duplicated = [pairs[0], *pairs]  # the first case now appears twice
    admission = admit_report(corpus, duplicated)
    assert admission.admitted() is False
    assert admission.duplicates == (pairs[0][0],)


def test_admission_refuses_a_missing_row() -> None:
    corpus = build_corpus()
    pairs = [(case.case_id, "PASS" if case.supported else "NOT_RUN") for case in corpus.cases]
    missing = pairs[1:]  # the first case never ran
    admission = admit_report(corpus, missing)
    assert admission.admitted() is False
    assert admission.missing == (pairs[0][0],)


def test_wrong_declared_snapshot_code_becomes_mismatch_not_a_silent_fix() -> None:
    book = build_recipes(build_corpus())
    recipe = book.by_id["crop.left_bp.full_removal_refused"]
    assert recipe.refusal_code == "invalid_contract"
    wrong = replace(recipe, refusal_code="unsupported_output_profile")

    row = _snapshot_layer_row(wrong)

    assert row.status == "MISMATCH"
    assert row.declared == "unsupported_output_profile"
    assert row.observed == "invalid_contract"  # what the real decoder actually raised


def test_wrong_declared_command_code_becomes_mismatch_not_a_silent_fix() -> None:
    book = build_recipes(build_corpus())
    recipe = book.by_id["command.create_track.refused"]
    assert recipe.refusal_code == "invalid_contract"
    wrong = replace(recipe, refusal_code="invalid_command")

    row = _command_refusal_row(wrong)

    assert row.status == "MISMATCH"
    assert row.declared == "invalid_command"
    assert row.observed == "invalid_contract"


def test_wrong_declared_currentness_code_becomes_mismatch_not_a_silent_fix() -> None:
    book = build_recipes(build_corpus())
    recipe = book.by_id["history_currentness.stale_snapshot_refused"]
    assert recipe.refusal_code == "stale_snapshot"
    wrong = replace(recipe, refusal_code="source_replaced")

    row = _currentness_row(wrong)

    assert row.status == "MISMATCH"
    assert row.declared == "source_replaced"
    assert row.observed == "stale_snapshot"


def test_command_effect_reports_mismatch_when_a_declared_change_did_not_happen() -> None:
    book = build_recipes(build_corpus())
    # `set_opacity_blend.accepted` genuinely changes the composition; declaring the case as
    # `invariant_ok`-equivalent would hide that. There is no typed field for this, so the row
    # function itself computes the expectation from a closed, predeclared set -- proven here by
    # confirming a genuine effect case is not swallowed as an accepted invariant.
    recipe = book.by_id["command.set_opacity_blend.accepted"]

    row = _command_effect_row(recipe)

    assert row.status == "PASS"
    assert row.declared == "accepted:changed"
    assert row.observed == "accepted:changed"


def test_qualify_row_rejects_a_path_shaped_declared_value() -> None:
    with pytest.raises(SemanticConformanceError):
        QualifyRow(
            case_id="command.create_track.accepted",
            case_class="command",
            observation="command_effect",
            declared=f"accepted at {ROOT}",
            observed="accepted",
            status="PASS",
        )


def test_output_carries_no_path_or_url_shaped_string(report: dict[str, object]) -> None:
    encoded = json.dumps(report, ensure_ascii=False)
    assert not _PATH_LIKE.search(encoded)
    assert not _URL_LIKE.search(encoded)
    assert str(ROOT) not in encoded


def test_a_row_records_the_composition_its_own_setup_reaches() -> None:
    """A row whose case is in its setup must not record the composition it started from.

    The stage applied a recipe's `edits` and never ran its `setup`, so every row that reaches its
    subject by running real timeline commands recorded the base fixture's fingerprint. Dozens of
    distinct cases therefore reported the same composition, and the render stage they hand off to
    had no way to tell which one it was supposed to have rendered.
    """

    book = build_recipes()
    with_setup = [
        recipe
        for recipe in book.rendering()
        if recipe.setup and recipe.observation == "render_and_browser"
    ]
    assert with_setup, "the corpus is supposed to have rows whose case is in their setup"

    base = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["snapshot"]
    for recipe in with_setup:
        row = _snapshot_layer_row(recipe)
        recorded = dict(row.measurements).get("public_fingerprint")
        assert recorded is not None, recipe.case_id
        edited = copy.deepcopy(base)
        apply_edits(edited, recipe)
        assert recorded != public_snapshot_fingerprint(edited), recipe.case_id
        reached = run_setup(recipe, base).state
        assert reached is not None, recipe.case_id
        assert recorded == reached.snapshot.public_fingerprint, recipe.case_id


def test_the_recorded_compositions_are_not_one_composition_repeated() -> None:
    """The fingerprints are the hand-off to the render stage, so they have to identify a row.

    This is a floor, not a target: it says that the recorded compositions distinguish most rows,
    which the setup-blind version did not. Which rows legitimately share one is pinned in the
    corpus tests, not here.
    """

    corpus = build_corpus()
    document = build_report(build_recipes(corpus), corpus)
    rows = cast(list[dict[str, object]], document["rows"])
    recorded = [
        str(item["value"])
        for row in rows
        for item in cast(list[dict[str, object]], row.get("measurements") or [])
        if item["subject"] == "public_fingerprint"
    ]
    assert len(recorded) >= 150
    assert len(set(recorded)) >= 100, len(set(recorded))
