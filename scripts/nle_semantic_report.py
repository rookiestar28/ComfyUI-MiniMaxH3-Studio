"""M25-20 corrective: join the executed stages into the one closed conformance report.

Three stages observe the corpus and none of them may judge itself:

- ``nle_semantic_qualify`` drives every row a pure in-process call can decide -- the snapshot
  contract, the timeline transaction decoder and the render planner's currentness check.
- ``nle_semantic_render`` renders each rendering row through the accepted path and observes the
  resulting artifact with the independent extractor.
- the Playwright journeys under ``frontend/tests/e2e/journeys`` present each row on the accepted
  runtime and record what the browser actually showed.

This script joins them. It contributes no observation of its own: every value it writes into a row
comes from one of the three stage artifacts or from ``semantic_conformance_expect``, which reads the
contract. Where a stage produced nothing for a row, the row is ``BLOCKED`` or ``NOT_RUN`` and names
what is missing -- the one thing that must never happen here is a row closed by evidence of a
cheaper kind than its ``OBSERVATION_KIND`` declares, which is the substitution the post-closeout
review rejected.

Which stage answers which row:

- ``render_and_browser`` and ``import_integration``: the three-way comparison. The expectation comes
  from the contract, the browser observation from the journey, the final observation from the render
  stage's independent extraction, and ``compare_case`` decides.
- ``contract_refusal``, ``command_refusal`` and ``command_effect``: the backend stage. A refusal is
  raised by exactly one authority -- the snapshot decoder, the timeline command decoder, the history
  or the currentness check -- and that authority's answer *is* the observation. There is nothing for
  a browser or an extractor to add, and routing it through a synthesized browser observation would
  be inventing evidence.
- ``shell_invariant``: the browser stage. The interaction is the whole observation.
- ``declared_negative``: ``compare_case`` with ``supported=False``. A declared negative still has to
  execute; an unexecuted one is ``NOT_RUN``, never a free ``DECLARED_UNSUPPORTED``.

The assembled rows go through ``build_report``, which admits them against the frozen corpus before
it will call anything conformant.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Collection, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, Final, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.errors import ContractValidationError  # noqa: E402
from comfyui_h3_context.core.semantic_conformance import (  # noqa: E402
    ACCEPTED_ARTIFACT_RETRIEVAL,
    AUDIO_SURFACE_NEGATIVE_FACTS,
    IMPORT_EFFECT_FACTS,
    IMPORT_IDENTITY_FACTS,
    TOLERANCE_PROFILE,
    build_corpus,
)
from comfyui_h3_context.core.semantic_conformance_cases import (  # noqa: E402
    BASE_FIXTURE_NAMES,
    BASE_PRESENTATION,
    CORPUS_BASE,
    CaseRecipe,
    build_recipes,
)
from comfyui_h3_context.core.semantic_conformance_compare import (  # noqa: E402
    AlphaSample,
    AudioLevel,
    AudioOnset,
    BrowserObservation,
    CaseOutcome,
    Expectation,
    FinalObservation,
    FrameGrid,
    FrameTiming,
    GeometryLandmark,
    Measurement,
    Mismatch,
    OutputMetadata,
    PatchSample,
    PresentedFrame,
    SilenceInterval,
    SourceLandmark,
    TextObservation,
    compare_case,
)
from comfyui_h3_context.core.semantic_conformance_drive import (  # noqa: E402
    DriveError,
    command_phases,
    run_setup,
)
from comfyui_h3_context.core.semantic_conformance_expect import (  # noqa: E402
    ExpectationError,
    browser_observable,
    derive_browser_expectation,
    derive_expectation,
    missing_landmarks,
    preview_scale,
    required_landmarks,
)
from comfyui_h3_context.core.semantic_conformance_media import (  # noqa: E402
    imported_source,
    imported_source_landmarks_match,
)
from scripts.nle_semantic_conformance import build_report  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures"
FIXTURE_PATH = FIXTURES_DIR / BASE_FIXTURE_NAMES[CORPUS_BASE]

#: The phases an accepted command row must render, in this order. A command's claim is what it
#: changed, so no single artifact can express it: the pair is the evidence, and a record carrying
#: one of them is not weaker proof of the same thing, it is proof of something else.
COMMAND_PHASES: Final = ("before", "after")

#: Rows the backend stage owns outright, because exactly one authority decides them.
BACKEND_OBSERVATIONS = ("contract_refusal", "command_refusal", "command_effect")
#: Rows whose whole observation is the browser interaction.
BROWSER_OBSERVATIONS = ("shell_invariant",)
#: Rows that need all three observations to agree.
JOINED_OBSERVATIONS = ("render_and_browser", "import_integration")


class JoinError(ValueError):
    """A stage artifact cannot be read as the stage it claims to be."""


# ---------------------------------------------------------------------------------------------
# Stage readers. Each one returns `case_id -> row`, and none of them invents a row.
# ---------------------------------------------------------------------------------------------


def read_backend(path: Path) -> dict[str, Mapping[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise JoinError("the backend stage artifact carries no rows")
    return {str(row["case_id"]): row for row in rows}


#: The render stage's header record, naming the composition base it rendered against.
#:
#: CRITICAL: the render stage binds real media into the fixture's shape, so the compositions it
#: renders are not the fixture's compositions and their public fingerprints are different values.
#: An expectation derived from the fixture would therefore describe one composition while the
#: artifact is a picture of another, and every disagreement would be blamed on the renderer. The
#: join reads this base and derives from it. Never quietly fall back to the fixture when a render
#: artifact is present: a base that is merely plausible is the same substitution this whole stage
#: exists to detect.
RENDER_BASE_RECORD = "base_wire"


def _render_lines(path: Path) -> list[Mapping[str, Any]]:
    lines: list[Mapping[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, Mapping):
            raise JoinError("the render stage wrote a record that is not an object")
        lines.append(record)
    return lines


def read_render(path: Path) -> dict[str, Mapping[str, Any]]:
    """Read the render stage's JSON Lines, one record per rendering row."""

    records: dict[str, Mapping[str, Any]] = {}
    for record in _render_lines(path):
        if record.get("record") == RENDER_BASE_RECORD:
            continue
        records[str(record["case_id"])] = record
    return records


def read_render_bases(path: Path) -> dict[str, Mapping[str, Any]]:
    """Every composition base the render stage declared it rendered against, by name.

    The stage writes one record before each base's rows. A record without a `base` name is read as
    the accepted corpus base: that is what a render artifact produced before M25-45 is, and it is
    unambiguous because there was only one base to be.
    """

    bases: dict[str, Mapping[str, Any]] = {}
    for record in _render_lines(path):
        if record.get("record") != RENDER_BASE_RECORD:
            continue
        base = record.get(RENDER_BASE_RECORD)
        if not isinstance(base, Mapping):
            raise JoinError("the render stage declared an unreadable composition base")
        name = record.get("base", CORPUS_BASE)
        if not isinstance(name, str) or name not in BASE_FIXTURE_NAMES:
            raise JoinError("the render stage named a composition base the corpus does not declare")
        if name in bases:
            raise JoinError("the render stage declared one composition base twice")
        bases[name] = base
    if not bases:
        raise JoinError("the render stage did not declare the composition base it rendered against")
    return bases


#: The schema the browser collector writes and this join reads. Both sides name it so a stage
#: artifact from some other tool cannot be joined by accident.
BROWSER_STAGE_SCHEMA = "h3.context.nle_semantic_browser_stage.v1"

#: Every field a browser row must carry. `executed` and `missing` are what make an absent
#: observation say so instead of arriving as a silent pass; `canvas_width`/`canvas_height` are the
#: actual backing dimensions, without which a pixel tolerance means nothing because CSS coordinates
#: scale with the viewport.
BROWSER_ROW_FIELDS = ("case_id", "executed", "missing", "canvas_width", "canvas_height")


def read_browser(path: Path) -> dict[str, Mapping[str, Any]]:
    """Read the browser stage, refusing an artifact that is not shaped like one.

    GUARD: a malformed row must raise here rather than fall through the join. A row missing
    `executed` would otherwise be read as unexecuted and reported `NOT_RUN`, which looks like an
    honest gap and is actually a broken collector -- the two need to stay distinguishable, because
    only one of them is a finding about the product.
    """

    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != BROWSER_STAGE_SCHEMA:
        raise JoinError("the browser stage artifact does not declare the expected schema")
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise JoinError("the browser stage artifact carries no rows")
    collected: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or any(field not in row for field in BROWSER_ROW_FIELDS):
            raise JoinError("a browser row is missing a required field")
        case_id = str(row["case_id"])
        if case_id in collected:
            raise JoinError("a browser row is reported twice")
        collected[case_id] = row
    return collected


# ---------------------------------------------------------------------------------------------
# Rebuilding the observations the comparator takes
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# Landmark readers. Each one reads what an observer measured; none of them invents a value, and a
# malformed entry raises rather than being skipped -- a silently dropped landmark reads downstream
# as "the observer measured nothing", which is the one thing that must stay distinguishable from a
# collector that is broken.
# ---------------------------------------------------------------------------------------------


def _entries(section: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    value = section.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise JoinError(f"{key} is not a list of observations")
    for entry in value:
        if not isinstance(entry, Mapping):
            raise JoinError(f"{key} carries an entry that is not an observation")
    return tuple(value)


def _source_landmarks(section: Mapping[str, Any]) -> tuple[SourceLandmark, ...]:
    return tuple(
        SourceLandmark(
            output_frame=int(entry["output_frame"]),
            source_frame=int(entry["source_frame"]),
            source_pts=None if entry.get("source_pts") is None else int(entry["source_pts"]),
            source_time_base_num=int(entry["source_time_base_num"]),
            source_time_base_den=int(entry["source_time_base_den"]),
        )
        for entry in _entries(section, "source_mapping")
    )


def _presented_frames(section: Mapping[str, Any]) -> tuple[PresentedFrame, ...]:
    return tuple(
        PresentedFrame(
            output_frame=int(entry["output_frame"]),
            source_frame=int(entry["source_frame"]),
            source_time=Fraction(str(entry["source_time"])),
        )
        for entry in _entries(section, "source_mapping")
    )


def _geometry(section: Mapping[str, Any]) -> tuple[GeometryLandmark, ...]:
    return tuple(
        GeometryLandmark(
            label=str(entry["label"]),
            left=int(entry["left"]),
            top=int(entry["top"]),
            right=int(entry["right"]),
            bottom=int(entry["bottom"]),
        )
        for entry in _entries(section, "geometry")
    )


def _patches(section: Mapping[str, Any], key: str) -> tuple[PatchSample, ...]:
    return tuple(
        PatchSample(
            label=str(entry["label"]),
            size_px=int(entry["size_px"]),
            edge_distance_px=int(entry["edge_distance_px"]),
            red=int(entry["red"]),
            green=int(entry["green"]),
            blue=int(entry["blue"]),
        )
        for entry in _entries(section, key)
    )


def _alphas(section: Mapping[str, Any]) -> tuple[AlphaSample, ...]:
    return tuple(
        AlphaSample(
            label=str(entry["label"]),
            output_frame=int(entry["output_frame"]),
            alpha_milli=int(entry["alpha_milli"]),
        )
        for entry in _entries(section, "alphas")
    )


def _onsets(section: Mapping[str, Any]) -> tuple[AudioOnset, ...]:
    return tuple(
        AudioOnset(label=str(entry["label"]), sample_index=int(entry["sample_index"]))
        for entry in _entries(section, "audio_onsets")
    )


def _levels(section: Mapping[str, Any]) -> tuple[AudioLevel, ...]:
    """Each measured window, with the samples it was measured over -- never defaulted."""

    return tuple(
        AudioLevel(
            label=str(entry["label"]),
            start_sample=int(entry["start_sample"]),
            sample_count=int(entry["sample_count"]),
            level_ppm=int(entry["level_ppm"]),
        )
        for entry in _entries(section, "audio_levels")
    )


def _silences(section: Mapping[str, Any], key: str) -> tuple[SilenceInterval, ...]:
    """The measured peak inside each declared gap.

    `abs_peak` is required rather than defaulted: an absent peak and a measured zero are the same
    value with opposite meanings, and the whole point of a silence check is that somebody looked.
    """

    return tuple(
        SilenceInterval(
            label=str(entry["label"]),
            start_sample=int(entry["start_sample"]),
            end_sample=int(entry["end_sample"]),
            abs_peak=int(entry["abs_peak"]),
        )
        for entry in _entries(section, key)
    )


def _text(section: Mapping[str, Any]) -> TextObservation | None:
    value = section.get("text")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise JoinError("text is not an observation")
    return TextObservation(
        content=str(value["content"]),
        font_identity=str(value["font_identity"]),
        line_count=int(value["line_count"]),
        weight=int(value["weight"]),
        style=str(value["style"]),
        align=str(value["align"]),
        line_bounds=_geometry(value),
        visible_glyph_ratio_milli=int(value.get("visible_glyph_ratio_milli", 0)),
    )


def _frame_grid(section: Mapping[str, Any]) -> FrameGrid:
    return FrameGrid(
        width=int(section["width"]),
        height=int(section["height"]),
        frame_count=int(section["frame_count"]),
        frame_rate_num=int(section["frame_rate_num"]),
        frame_rate_den=int(section["frame_rate_den"]),
    )


def _frame_timing(section: Mapping[str, Any]) -> FrameTiming | None:
    """Rebuild the measured timing from the raw table the extractor emitted.

    GUARD: this must never be reconstructed from the summary pairs -- a count and a uniform step
    rebuild a *clean* sequence, strictly monotonic with no duplicates and no negative stamps, and a
    truncated, re-timed or duplicated artifact would arrive here looking perfect. That laundering is
    exactly the class of defect F4 was raised for, so a record with no raw table measures nothing
    usable and the row blocks instead.
    """

    base_num = section.get("time_base_num")
    base_den = section.get("time_base_den")
    pts = section.get("pts_ticks")
    if not isinstance(base_num, int) or not isinstance(base_den, int):
        return None
    if not isinstance(pts, list) or not pts:
        return None
    dts = section.get("dts_ticks")
    durations = section.get("duration_ticks")
    if not isinstance(dts, list) or not isinstance(durations, list):
        return None
    try:
        return FrameTiming(
            time_base_num=base_num,
            time_base_den=base_den,
            pts_ticks=tuple(int(tick) for tick in pts),
            dts_ticks=tuple(None if tick is None else int(tick) for tick in dts),
            duration_ticks=tuple(None if tick is None else int(tick) for tick in durations),
        )
    except (TypeError, ValueError):
        return None


def _output_metadata(section: Mapping[str, Any]) -> OutputMetadata:
    return OutputMetadata(
        container=str(section["container"]),
        video_codec=str(section["video_codec"]),
        pixel_format=str(section["pixel_format"]),
        width=int(section["width"]),
        height=int(section["height"]),
        pixel_aspect_num=int(section["pixel_aspect_num"]),
        pixel_aspect_den=int(section["pixel_aspect_den"]),
        color_policy=str(section["color_policy"]),
        frame_rate_num=int(section["frame_rate_num"]),
        frame_rate_den=int(section["frame_rate_den"]),
        audio_stream_count=int(section["audio_stream_count"]),
        audio_codec=None if section["audio_codec"] is None else str(section["audio_codec"]),
        sample_rate=None if section["sample_rate"] is None else int(section["sample_rate"]),
        channels=None if section["channels"] is None else int(section["channels"]),
    )


def final_observation(case_id: str, record: Mapping[str, Any] | None) -> FinalObservation:
    """Rebuild what the independent extractor decoded, or say what is missing."""

    if record is None:
        return FinalObservation(
            case_id=case_id,
            extractor_fingerprint="",
            executed=False,
            missing=("the render stage produced no record for this row",),
        )
    if record.get("status") != "OBSERVED":
        code = record.get("blocked_code") or "the render stage blocked"
        return FinalObservation(
            case_id=case_id, extractor_fingerprint="", executed=True, missing=(str(code),)
        )
    phases = record.get("phases")
    if not isinstance(phases, list) or not phases:
        return FinalObservation(
            case_id=case_id,
            extractor_fingerprint="",
            executed=True,
            missing=("the render stage recorded no phase",),
        )
    # The row's subject is the last phase: a command row renders before and after, and the "after"
    # is the one the expectation describes.
    #
    # GUARD: the wrapper's status is not the phase's. A record may say `OBSERVED` at the top and
    # carry a phase that blocked, and reading that phase's observation anyway is how a row closes on
    # evidence the stage itself declined to vouch for. Never drop this check because the caller
    # "already checked the record": the caller checked the wrapper.
    subject = phases[-1]
    if isinstance(subject, Mapping) and subject.get("status") not in (None, "OBSERVED"):
        code = subject.get("blocked_code") or "the phase observed no artifact"
        return FinalObservation(
            case_id=case_id, extractor_fingerprint="", executed=True, missing=(str(code),)
        )
    observation = phases[-1].get("observation")
    if not isinstance(observation, Mapping):
        return FinalObservation(
            case_id=case_id,
            extractor_fingerprint="",
            executed=True,
            missing=("the render stage recorded no observation",),
        )
    timing = _frame_timing(observation.get("frame_timing_table") or {})
    missing: list[str] = []
    if timing is None:
        missing.append("no measured packet timing table was recorded")
    extent = observation.get("audio_extent_samples")
    return FinalObservation(
        case_id=case_id,
        extractor_fingerprint=str(observation.get("extractor_fingerprint") or ""),
        frame_grid=_frame_grid(observation["frame_grid"]),
        frame_timing=timing,
        output_metadata=_output_metadata(observation["output_metadata"]),
        audio_extent_samples=None if extent is None else int(extent),
        source_mapping=_source_landmarks(observation),
        geometry=_geometry(observation),
        patches=_patches(observation, "patches"),
        color_patches=_patches(observation, "color_patches"),
        alphas=_alphas(observation),
        text=_text(observation),
        audio_onsets=_onsets(observation),
        silences=_silences(observation, "silences"),
        audio_levels=_levels(observation),
        executed=True,
        missing=tuple(missing),
    )


def browser_observation(case_id: str, row: Mapping[str, Any] | None) -> BrowserObservation:
    """Rebuild what the accepted runtime actually presented, or say what is missing."""

    if row is None:
        return BrowserObservation(
            case_id=case_id,
            executed=False,
            missing=("the browser stage produced no record for this row",),
        )
    missing = tuple(str(item) for item in row.get("missing") or ())
    return BrowserObservation(
        case_id=case_id,
        canvas_width=int(row.get("canvas_width") or 0),
        canvas_height=int(row.get("canvas_height") or 0),
        source_mapping=_presented_frames(row),
        geometry=_geometry(row),
        patches=_patches(row, "patches"),
        color_patches=_patches(row, "color_patches"),
        alphas=_alphas(row),
        text=_text(row),
        audio_onsets=_onsets(row),
        silence_probes=_silences(row, "silence_probes"),
        refusal_code=row.get("refusal_code"),
        executed=bool(row.get("executed")),
        missing=missing,
    )


def _measurements(row: Mapping[str, Any]) -> tuple[Measurement, ...]:
    return tuple(
        Measurement(str(item["subject"]), str(item["value"]), item.get("bound"))
        for item in row.get("measurements") or ()
    )


def _unaccepted_retrieval(phase: Mapping[str, Any]) -> str | None:
    """Why an observed phase is not judged: its bytes did not come through the accepted transport.

    GUARD: `ACCEPTED_ARTIFACT_RETRIEVAL` is the only label the join admits. The render stage once
    labelled every phase `direct_staging_read` -- it decoded the file the renderer had just
    written -- and the report carried that label as a fact so nothing could describe it as
    transport proof; now that the stage retrieves through `AuthoringOutputRegistry`, a phase with
    any other label is blocked, not measured. Never widen this to a list of "acceptable" routes.
    """

    retrieval = phase.get("artifact_retrieval")
    if retrieval == ACCEPTED_ARTIFACT_RETRIEVAL:
        return None
    return (
        "the artifact was not retrieved through the accepted transport "
        f"({retrieval if isinstance(retrieval, str) and retrieval else 'unlabelled'})"
    )


def _retrieval_facts(record: Mapping[str, Any] | None) -> tuple[Measurement, ...]:
    """How each phase's artifact was obtained, recorded per phase.

    GUARD: this is evidence, not decoration. Reading the file the renderer just wrote proves the
    encoder produced the right bytes; it does not prove the accepted retrieval path hands those
    bytes back unchanged, and the two are different claims. The report therefore states which one a
    row actually has. Never drop this to tidy the measurements, and never let a row that says
    `direct_staging_read` be described anywhere as transport proof.
    """

    if record is None:
        return ()
    phases = record.get("phases")
    if not isinstance(phases, list):
        return ()
    facts: list[Measurement] = []
    for phase in phases:
        if not isinstance(phase, Mapping):
            continue
        retrieval = phase.get("artifact_retrieval")
        if retrieval is None:
            continue
        facts.append(Measurement(f"{phase.get('phase')}.artifact_retrieval", str(retrieval)))
    return tuple(facts)


def _browser_facts(row: Mapping[str, Any] | None) -> tuple[Measurement, ...]:
    """Retain every fact the browser measured, whether or not an expectation compared it.

    A landmark with no analytic expectation is still evidence that the row was presented and what
    it looked like. Dropping it would lose the only record that the interaction happened.
    """

    if row is None:
        return ()
    facts = row.get("facts")
    if not isinstance(facts, Mapping):
        return ()
    return tuple(Measurement(f"browser.{key}", str(value)) for key, value in sorted(facts.items()))


# ---------------------------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------------------------


def _composition(recipe: CaseRecipe, base: Mapping[str, Any]) -> dict[str, Any]:
    """The composition this row actually renders: its edits *and* its setup steps.

    CRITICAL: a recipe states its case in two ways and both are load-bearing. Some rows change the
    fixture directly (`edits`); others reach their subject by running real timeline commands first
    (`setup`) -- eleven rendering rows in the corpus carry a setup and no edits at all. Deriving the
    expectation from the edited base alone would describe the composition the row *started* from,
    so an artifact of the composition it actually rendered would be compared against a description
    of a different one, and every disagreement would be blamed on the renderer.
    """

    setup = run_setup(recipe, base)
    if setup.state is None:
        raise ExpectationError(setup.error or "the row reached no usable composition")
    return dict(setup.state.snapshot.to_wire())


def _phase_names(phases: object) -> tuple[str, ...] | None:
    if not isinstance(phases, list):
        return None
    names: list[str] = []
    for phase in phases:
        if not isinstance(phase, Mapping):
            return None
        names.append(str(phase.get("phase")))
    return tuple(names)


def _no_browser(case_id: str) -> BrowserObservation:
    """The absent browser side of a backend-owned row, declared rather than fabricated.

    An accepted timeline command has no canvas in its claim: the corpus gives it a `command_effect`
    observation, and the browser stage never visits it. This object is therefore never compared --
    `compare_case` is called with `sides=("final",)` -- and exists only because the comparator takes
    three observations positionally. Never populate it to make a comparison succeed.
    """

    return BrowserObservation(case_id=case_id, executed=True)


def _rendering_row_phase(
    recipe: CaseRecipe, base: Mapping[str, Any], record: Mapping[str, Any] | None
) -> tuple[str | None, tuple[Measurement, ...], tuple[Mismatch, ...]]:
    """Judge the one artifact an ordinary rendering row had to produce.

    Returns `(blocking reason or None, measurements, mismatches)`.

    GUARD: this is the same rule `_command_row_phases` applies, for the rows that are not accepted
    commands. The original repair swept only the command path, so a property row still closed on a
    record whose single phase was `BLOCKED` and whose `public_fingerprint` named another
    composition, as long as the wrapper said `OBSERVED` and an observation object was present -- the
    R2-F3 defect surviving in every other rendering path. A row renders exactly one composition, so
    it records exactly one phase, that phase carries its own `OBSERVED` status, and it reports the
    composition this row's own setup reaches. Never let the count, the status or the identity go
    unchecked here because the landmark comparison "would catch it": the landmarks are read out of
    that same phase.
    """

    if record is None:
        return ("the render stage produced no record for this row", (), ())
    if record.get("status") != "OBSERVED":
        return (str(record.get("blocked_code") or "the render stage blocked"), (), ())
    phases = record.get("phases")
    if not isinstance(phases, list) or not phases:
        return ("the render stage recorded no readable phases", (), ())
    if len(phases) != 1:
        names = _phase_names(phases)
        return (f"phases are {list(names or ())}, not one rendered composition", (), ())
    phase = phases[0]
    if not isinstance(phase, Mapping):
        return ("the render stage recorded no readable phase", (), ())
    if phase.get("status") != "OBSERVED":
        return (str(phase.get("blocked_code") or "the phase observed no artifact"), (), ())
    transport = _unaccepted_retrieval(phase)
    if transport is not None:
        return (transport, (), ())
    reported = phase.get("public_fingerprint")
    if not isinstance(reported, str) or not reported:
        return ("the phase did not report the composition it rendered", (), ())
    measurements = (Measurement("final.public_fingerprint", reported),)
    try:
        declared = str(_composition(recipe, base)["public_fingerprint"])
    except ExpectationError as exc:
        return (f"the row reached no composition to identify: {exc}", measurements, ())
    if reported != declared:
        # The stage rendered *a* composition, but not this row's. Reading its landmarks as if they
        # answered this row's expectation is the vacuity in a new place.
        return (
            None,
            measurements,
            (Mismatch("identity", "final.public_fingerprint", declared, reported),),
        )
    return (None, measurements, ())


def _command_row_phases(
    recipe: CaseRecipe, base: Mapping[str, Any], record: Mapping[str, Any] | None
) -> tuple[str | None, tuple[Measurement, ...], tuple[Mismatch, ...]]:
    """Judge an accepted command row on the two artifacts it had to render.

    Returns `(blocking reason or None, measurements, mismatches)`.

    GUARD: this validates the phases; it does not count them. The check it replaces accepted any
    `OBSERVED` wrapper with a non-empty phase list and a mapping called `output_metadata` in its
    last entry -- so a record with a single phase named `before`, whose own status was `BLOCKED`
    and whose metadata said `width: -1`, closed an accepted command that had rendered no "after" at
    all. Never reduce this to a length check, never let the wrapper's status stand in for a phase's
    own, and never accept a phase's word for which composition it rendered: the identity is decided
    by driving the row's own command through the real decoder here.
    """

    if record is None:
        return ("the render stage produced no record for this row", (), ())
    if record.get("status") != "OBSERVED":
        return (str(record.get("blocked_code") or "the render stage blocked"), (), ())
    names = _phase_names(record.get("phases"))
    if names is None:
        return ("the render stage recorded no readable phases", (), ())
    if names != COMMAND_PHASES:
        return (f"phases are {list(names)}, not {list(COMMAND_PHASES)}", (), ())
    phases = cast(list[Mapping[str, Any]], record["phases"])

    try:
        wires = command_phases(recipe, base).wires()
    except (ContractValidationError, DriveError) as exc:
        return (f"the row's own phases could not be derived: {exc}", (), ())
    try:
        expectations = tuple(derive_expectation(recipe, wire) for wire in wires)
    except ExpectationError as exc:
        return (f"no expectation for a phase: {exc}", (), ())
    for name, wire, expectation in zip(COMMAND_PHASES, wires, expectations, strict=True):
        absent = _absent_expectation_landmarks(recipe, expectation, wire)
        if absent:
            return (f"phase {name} declares nothing to compare: {', '.join(absent)}", (), ())

    measurements: list[Measurement] = []
    mismatches: list[Mismatch] = []
    observed: list[FinalObservation] = []
    rows = zip(COMMAND_PHASES, phases, wires, expectations, strict=True)
    for name, phase, wire, expectation in rows:
        if phase.get("status") != "OBSERVED":
            code = phase.get("blocked_code") or "it observed no artifact"
            return (f"phase {name}: {code}", tuple(measurements), ())
        transport = _unaccepted_retrieval(phase)
        if transport is not None:
            return (f"phase {name}: {transport}", tuple(measurements), ())
        reported = phase.get("public_fingerprint")
        if not isinstance(reported, str) or not reported:
            return (f"phase {name} did not report the composition it rendered", (), ())
        measurements.append(Measurement(f"{name}.public_fingerprint", reported))
        declared = str(wire["public_fingerprint"])
        if reported != declared:
            # The record rendered *a* composition, but not this phase's. Reporting the artifact
            # facts as if they answered the expectation would be the vacuity in a new place.
            mismatches.append(
                Mismatch("identity", f"{name}.public_fingerprint", declared, reported)
            )
            continue
        final = final_observation(recipe.case_id, {"status": "OBSERVED", "phases": [phase]})
        if not final.executed or final.missing:
            reason = ", ".join(final.missing) or "the phase observed nothing"
            return (f"phase {name}: {reason}", tuple(measurements), ())
        gaps = missing_landmarks(recipe, final, wire, side=f"final.{name}")
        if gaps:
            return (
                f"required observations are absent: {', '.join(gaps)}",
                tuple(measurements),
                (),
            )
        observed.append(final)
        outcome = compare_case(
            expectation,
            _no_browser(recipe.case_id),
            final,
            TOLERANCE_PROFILE,
            sides=("final",),
        )
        measurements.extend(
            Measurement(f"{name}.{item.subject}", item.value, item.bound)
            for item in outcome.measurements
        )
        mismatches.extend(
            Mismatch(item.mismatch_class, f"{name}.{item.subject}", item.expected, item.observed)
            for item in outcome.mismatches
        )
        if outcome.status not in ("PASS", "MISMATCH"):
            return (f"phase {name}: {outcome.reason or outcome.status}", tuple(measurements), ())

    if mismatches or len(observed) != len(COMMAND_PHASES):
        return (None, tuple(measurements), tuple(mismatches))

    # Expected no-change versus expected measurable change. When the command moves the rendered
    # composition, the two derived expectations differ, and the "after" artifact must therefore
    # *fail* the "before" expectation: a stage that rendered the same composition twice would
    # otherwise satisfy both phases and prove nothing about the command. When the command changes
    # only state an artifact cannot show -- selection, locking -- the expectations are equal and
    # there is nothing here to require.
    expects_change = expectations[0] != expectations[1]
    measurements.append(Measurement("phases.expects_visible_change", str(expects_change)))
    if expects_change:
        # GUARD: the proof is a *measurement* one phase's expectation rejects in the other
        # phase's artifact, never a landmark an observation simply does not carry. The two
        # phases prescribe different sample points -- a trimmed primary states no identity at the
        # frames it vacated, a rippled still states its own absence at the frames it no longer
        # covers -- so each artifact is observed at the union of both phases' points
        # (`nle_semantic_render._companion_prescriptions`) and is held to the *other* phase's
        # expectation. Either direction proves the change: a command whose effect is confined to
        # frames only the after composition states (`ripple_delete`) is proved by the before
        # artifact failing the after expectation there. A record that observed a phase only at
        # its own points is blocked here as unmeasured, not passed on absence.
        #
        # One absence is a finding rather than a gap: a fiducial the other phase places on the
        # canvas and this phase's composition moves off it (`set_visual_transform` rotates the
        # primary's top-left mark above the frame). Geometry is observed per frame, not per
        # landmark -- the extractor classifies the whole target frame and reports every fiducial
        # it finds -- so when this artifact carries the layer's own rectangle, the frame was
        # examined and the mark was not there. That absence still proves nothing on its own;
        # it is merely not a reason to block a change some other landmark measured.
        crossed = {
            name: compare_case(
                expectations[1 - index],
                _no_browser(recipe.case_id),
                observed[index],
                TOLERANCE_PROFILE,
                sides=("final",),
            )
            for index, name in enumerate(COMMAND_PHASES)
        }
        examined = {
            name: {landmark.label for landmark in observed[index].geometry}
            for index, name in enumerate(COMMAND_PHASES)
        }
        unmeasured = sorted(
            {
                f"{name}:{item.subject}"
                for name, outcome in crossed.items()
                for item in outcome.mismatches
                if item.observed == "absent"
                and not _fiducial_absent_from_an_examined_layer(item.subject, examined[name])
            }
        )
        if unmeasured:
            return (
                "a phase's artifact was not observed where the other phase's expectation "
                "claims: " + ", ".join(unmeasured),
                tuple(measurements),
                (),
            )
        for name, outcome in crossed.items():
            measurements.append(Measurement(f"{name}.against_other_phase", outcome.status))
            if outcome.status not in ("PASS", "MISMATCH"):
                return (
                    f"the {name} artifact could not be held to the other phase's expectation: "
                    f"{outcome.reason or outcome.status}",
                    tuple(measurements),
                    (),
                )
        measured = any(
            item.observed != "absent" for outcome in crossed.values() for item in outcome.mismatches
        )
        if not measured:
            mismatches.append(
                Mismatch(
                    "identity",
                    "after.observable_effect",
                    "an artifact the before expectation rejects, or a before artifact the "
                    "after expectation rejects",
                    "each artifact still satisfies the other phase's expectation",
                )
            )
    return (None, tuple(measurements), tuple(mismatches))


def _fiducial_absent_from_an_examined_layer(subject: str, examined: Collection[str]) -> bool:
    """Whether `subject` names a patch fiducial of a layer whose rectangle was observed."""

    prefix = "final.geometry."
    if not subject.startswith(prefix):
        return False
    label = subject[len(prefix) :]
    clip_id, dot, patch = label.partition(".")
    return bool(dot and patch) and clip_id in examined


def _absent_expectation_landmarks(
    recipe: CaseRecipe, expectation: Expectation, wire: Mapping[str, Any]
) -> tuple[str, ...]:
    """Name the landmarks the row requires that its own expectation does not state.

    An expectation that declares nothing compares nothing, so this is the first place a row can
    become vacuous -- earlier than any missing observation, and quieter, because both observations
    can be complete and the comparison still asks nothing of them.
    """

    absent: list[str] = []
    for landmark in required_landmarks(recipe, wire):
        value = getattr(expectation, landmark, None)
        if value is None or (isinstance(value, tuple) and not value):
            absent.append(f"expectation.{landmark}")
    return tuple(absent)


SURFACE_NEGATIVE_PREFIX: Final = "deferred_negative.surface_"


def _surface_negative_row(recipe: CaseRecipe, row: Mapping[str, Any] | None) -> CaseOutcome:
    """Close one audio-surface negative on what the browser counted, and on nothing less.

    GUARD (B-66): the row closes as `DECLARED_UNSUPPORTED` only when the journey executed, named
    no gap, and every count in `AUDIO_SURFACE_NEGATIVE_FACTS` is present, readable and zero. A
    nonzero count is the negative failing -- the surface exposes what the policy forbids -- and
    is reported as a MISMATCH on that count. Never fold a missing count into "zero": an absent
    observation is BLOCKED, the same rule every rendering row is held to.
    """

    if row is None:
        return CaseOutcome(recipe.case_id, "NOT_RUN", reason="the browser stage produced no record")
    if not row.get("executed"):
        return CaseOutcome(
            recipe.case_id, "NOT_RUN", reason="the browser interaction did not execute"
        )
    gaps = tuple(str(item) for item in row.get("missing") or ())
    measurements = _browser_facts(row)
    if gaps:
        return CaseOutcome(
            recipe.case_id,
            "BLOCKED",
            measurements=measurements,
            reason=f"observation is missing: {', '.join(gaps)}",
        )
    facts = row.get("facts")
    if not isinstance(facts, Mapping):
        facts = {}
    absent: list[str] = []
    unreadable: list[str] = []
    mismatches: list[Mismatch] = []
    for key in AUDIO_SURFACE_NEGATIVE_FACTS:
        if key not in facts:
            absent.append(key)
            continue
        text = str(facts[key]).strip()
        if not text.lstrip("-").isdigit():
            unreadable.append(key)
            continue
        count = int(text)
        if count != 0:
            mismatches.append(Mismatch("audio_surface", f"browser.{key}", "0", str(count)))
    if absent or unreadable:
        parts = []
        if absent:
            parts.append(f"required negative observations are absent: {', '.join(absent)}")
        if unreadable:
            parts.append(f"negative observations are not counts: {', '.join(unreadable)}")
        return CaseOutcome(
            recipe.case_id, "BLOCKED", measurements=measurements, reason="; ".join(parts)
        )
    if mismatches:
        return CaseOutcome(
            recipe.case_id,
            "MISMATCH",
            mismatches=tuple(mismatches),
            measurements=measurements,
        )
    return CaseOutcome(
        recipe.case_id,
        "DECLARED_UNSUPPORTED",
        measurements=measurements,
        reason=recipe.unsupported_reason,
    )


def _fact_text(value: object) -> str:
    """One spelling for a recorded fact on either side.

    GUARD (B-65): the browser stage document carries every fact as a string -- the gatherer's
    `stringifyFacts` writes `"true"`, `"3600"` -- while the render stage records JSON booleans
    and integers. The comparison is on the spelling this function returns, so a JS boolean, a
    Python boolean and the gatherer's lower-case text must all land on the same word; without
    that, every boolean effect fact reads as a MISMATCH ("True" vs "true").
    """

    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return "True" if value.strip().lower() == "true" else "False"
    return str(value)


def _fact_absent(facts: Mapping[str, Any], key: str) -> bool:
    """A fact the side did not observe: missing, `None`, or the gatherer's `"null"` text."""

    if key not in facts:
        return True
    value = facts[key]
    return value is None or (
        isinstance(value, str) and value.strip().lower() in ("null", "none", "")
    )


def _import_integration_row(
    recipe: CaseRecipe,
    record: Mapping[str, Any] | None,
    row: Mapping[str, Any] | None,
) -> CaseOutcome:
    """Judge one `import_integration` row on the chain both stages drove (B-65).

    The composition under judgement is the product's own timeline after the explicit insertion,
    as the render stage read it back from the real history (`import.composition`), never the
    corpus base: the base declares no imported asset, so an expectation derived from it would
    describe a picture nobody made. The browser journey drove the same chain through the shell's
    controls, and the two halves are held to each other on `IMPORT_IDENTITY_FACTS` (what the
    product's insert control drafted, what its probe measured, how the history moved) and to the
    plan on `IMPORT_EFFECT_FACTS`. The imported asset is read as `IMPORTED_SOURCE_PROFILE` only
    after `imported_source_landmarks_match` confirmed the product measured that profile's frame
    table, so the source-range expectation is anchored in the product's own probe.

    GUARD: never derive this row's composition from the corpus base, and never compare the two
    halves on the minted asset id or on a fingerprint over it -- those differ between two runs of
    the same chain, and "equal" would only ever be reachable by copying one side into the other.
    """

    if row is None:
        return CaseOutcome(recipe.case_id, "NOT_RUN", reason="the browser stage produced no record")
    if not row.get("executed"):
        return CaseOutcome(
            recipe.case_id, "NOT_RUN", reason="the browser interaction did not execute"
        )
    browser_measurements = _browser_facts(row)
    gaps = tuple(str(item) for item in row.get("missing") or ())
    if gaps:
        return CaseOutcome(
            recipe.case_id,
            "BLOCKED",
            measurements=browser_measurements,
            reason=f"observation is missing: {', '.join(gaps)}",
        )
    if record is None:
        return CaseOutcome(
            recipe.case_id,
            "NOT_RUN",
            measurements=browser_measurements,
            reason="the render stage produced no record",
        )
    section = record.get("import")
    if not isinstance(section, Mapping):
        section = {}
    import_measurements = tuple(
        Measurement(f"import.{key}", _fact_text(value))
        for key, value in sorted(section.items())
        if key != "composition" and not isinstance(value, (Mapping, list))
    )
    measurements = browser_measurements + import_measurements + _retrieval_facts(record)
    if record.get("status") != "OBSERVED":
        return CaseOutcome(
            recipe.case_id,
            "BLOCKED",
            measurements=measurements,
            reason=str(record.get("blocked_code") or "the render stage blocked"),
        )
    wire = section.get("composition")
    if not isinstance(wire, Mapping):
        return CaseOutcome(
            recipe.case_id,
            "BLOCKED",
            measurements=measurements,
            reason="the render stage recorded no imported composition",
        )
    asset_id = section.get("imported_asset_id")
    asset = next(
        (
            a
            for a in wire.get("assets") or ()
            if isinstance(a, Mapping) and a.get("asset_id") == asset_id
        ),
        None,
    )
    if not isinstance(asset_id, str) or asset is None:
        return CaseOutcome(
            recipe.case_id,
            "BLOCKED",
            measurements=measurements,
            reason="the imported composition carries no asset under the receipt's asset id",
        )
    if not imported_source_landmarks_match(asset):
        return CaseOutcome(
            recipe.case_id,
            "BLOCKED",
            measurements=measurements,
            reason=(
                "the product's probe of the imported asset did not measure the imported "
                "source's frame table"
            ),
        )

    # The two halves, held to each other and to the plan.
    browser_facts = row.get("facts")
    if not isinstance(browser_facts, Mapping):
        browser_facts = {}
    absent: list[str] = []
    mismatches: list[Mismatch] = []
    for key in IMPORT_IDENTITY_FACTS:
        if _fact_absent(section, key):
            absent.append(f"import.{key}")
        if _fact_absent(browser_facts, key):
            absent.append(f"browser.{key}")
        if not _fact_absent(section, key) and not _fact_absent(browser_facts, key):
            expected = _fact_text(section[key])
            observed = _fact_text(browser_facts[key])
            if expected != observed:
                mismatches.append(Mismatch("identity", f"browser.{key}", expected, observed))
    for key, required in IMPORT_EFFECT_FACTS.items():
        for side, facts in (("import", section), ("browser", browser_facts)):
            if _fact_absent(facts, key):
                if f"{side}.{key}" not in absent:
                    absent.append(f"{side}.{key}")
                continue
            observed = _fact_text(facts[key])
            if observed != required:
                mismatches.append(Mismatch("import_effect", f"{side}.{key}", required, observed))
    if absent:
        return CaseOutcome(
            recipe.case_id,
            "BLOCKED",
            measurements=measurements,
            reason=f"required observations are absent: {', '.join(absent)}",
        )
    if mismatches:
        return CaseOutcome(
            recipe.case_id,
            "MISMATCH",
            mismatches=tuple(mismatches),
            measurements=measurements,
        )

    # The final artifact, against the expectation the imported composition states.
    with imported_source(asset_id):
        try:
            expectation = derive_expectation(recipe, wire)
        except ExpectationError as exc:
            return CaseOutcome(
                recipe.case_id,
                "BLOCKED",
                measurements=measurements,
                reason=f"no expectation: {exc}",
            )
        phase_blocked, phase_measurements, phase_mismatches = _import_row_phase(record, wire)
        return _three_way_outcome(
            recipe,
            wire,
            expectation,
            record,
            row,
            phase_blocked,
            phase_measurements,
            phase_mismatches,
            extra_measurements=import_measurements,
        )


def _import_row_phase(
    record: Mapping[str, Any], wire: Mapping[str, Any]
) -> tuple[str | None, tuple[Measurement, ...], tuple[Mismatch, ...]]:
    """The one-phase rule of `_rendering_row_phase`, with the identity the record itself names.

    An import row's composition is not derivable from the corpus, so the phase is held to the
    composition the stage recorded as the render's input: a phase that rendered another one is
    the same vacuity `_rendering_row_phase` refuses.
    """

    phases = record.get("phases")
    if not isinstance(phases, list) or not phases:
        return ("the render stage recorded no readable phases", (), ())
    if len(phases) != 1:
        names = _phase_names(phases)
        return (f"phases are {list(names or ())}, not one rendered composition", (), ())
    phase = phases[0]
    if not isinstance(phase, Mapping):
        return ("the render stage recorded no readable phase", (), ())
    if phase.get("status") != "OBSERVED":
        return (str(phase.get("blocked_code") or "the phase observed no artifact"), (), ())
    transport = _unaccepted_retrieval(phase)
    if transport is not None:
        return (transport, (), ())
    reported = phase.get("public_fingerprint")
    if not isinstance(reported, str) or not reported:
        return ("the phase did not report the composition it rendered", (), ())
    measurements = (Measurement("final.public_fingerprint", reported),)
    declared = str(wire.get("public_fingerprint") or "")
    if reported != declared:
        return (
            None,
            measurements,
            (Mismatch("identity", "final.public_fingerprint", declared, reported),),
        )
    return (None, measurements, ())


def join_row(
    recipe: CaseRecipe,
    base: Mapping[str, Any],
    backend: Mapping[str, Mapping[str, Any]],
    render: Mapping[str, Mapping[str, Any]],
    browser: Mapping[str, Mapping[str, Any]],
) -> CaseOutcome:
    if recipe.observation in BACKEND_OBSERVATIONS:
        row = backend.get(recipe.case_id)
        if row is None:
            return CaseOutcome(
                recipe.case_id, "BLOCKED", reason="the backend stage produced no record"
            )
        status = str(row["status"])
        measurements = _measurements(row)
        if not recipe.renders and recipe.case_id in render:
            # A refusal that still produced an artifact is the failure a code-only assertion
            # cannot see. The recipe forbids rendering, so a record here is a real finding about
            # the render stage or about the refusal, never something to drop quietly.
            return CaseOutcome(
                recipe.case_id,
                "MISMATCH",
                mismatches=(
                    Mismatch(
                        "refusal_code",
                        "final.artifact",
                        "no rendered artifact",
                        "an artifact was produced",
                    ),
                ),
                measurements=measurements,
            )
        if recipe.renders and status == "PASS":
            # An accepted command row is not closed by the timeline moving. Both compositions --
            # the one it started from and the one it produced -- have to have rendered, and each
            # artifact has to agree with the expectation derived from its own composition, or the
            # row would prove only that the decoder accepted something nobody ever made a picture
            # of. Refusing rows render nothing by construction, so this applies exactly where the
            # recipe says it renders.
            blocked, phase_measurements, phase_mismatches = _command_row_phases(
                recipe, base, render.get(recipe.case_id)
            )
            measurements += _retrieval_facts(render.get(recipe.case_id)) + phase_measurements
            if blocked is not None:
                return CaseOutcome(
                    recipe.case_id, "BLOCKED", measurements=measurements, reason=blocked
                )
            if phase_mismatches:
                return CaseOutcome(
                    recipe.case_id,
                    "MISMATCH",
                    mismatches=phase_mismatches,
                    measurements=measurements,
                )
        return CaseOutcome(
            recipe.case_id,
            status,
            measurements=measurements,
            reason=row.get("reason"),
        )

    if recipe.observation == "declared_negative":
        # A declared negative is closed by whichever stage can actually execute it. The ten seam
        # rows are a claim about the contract and the command decoder, both of which live in the
        # backend; the three surface rows are a claim about what the shipped UI exposes, which
        # only the browser can look at. Neither gets a free `DECLARED_UNSUPPORTED` for having
        # been declared, and neither is closed by the other's stage.
        if recipe.case_id.startswith(SURFACE_NEGATIVE_PREFIX):
            return _surface_negative_row(recipe, browser.get(recipe.case_id))
        row = backend.get(recipe.case_id)
        if row is not None and str(row["status"]) != "NOT_RUN":
            return CaseOutcome(
                recipe.case_id,
                str(row["status"]),
                measurements=_measurements(row),
                reason=row.get("reason"),
            )
        return CaseOutcome(
            recipe.case_id,
            "NOT_RUN",
            reason="the backend stage produced no negative record for this seam",
        )

    if recipe.observation in BROWSER_OBSERVATIONS:
        row = browser.get(recipe.case_id)
        if row is None:
            return CaseOutcome(
                recipe.case_id, "NOT_RUN", reason="the browser stage produced no record"
            )
        if not row.get("executed"):
            return CaseOutcome(
                recipe.case_id, "NOT_RUN", reason="the browser interaction did not execute"
            )
        gaps = tuple(str(item) for item in row.get("missing") or ())
        if gaps:
            return CaseOutcome(
                recipe.case_id,
                "BLOCKED",
                measurements=_browser_facts(row),
                reason=f"observation is missing: {', '.join(gaps)}",
            )
        return CaseOutcome(recipe.case_id, "PASS", measurements=_browser_facts(row))

    if recipe.observation == "import_integration":
        return _import_integration_row(
            recipe, render.get(recipe.case_id), browser.get(recipe.case_id)
        )

    try:
        wire = _composition(recipe, base)
        expectation = derive_expectation(recipe, wire)
    except ExpectationError as exc:
        return CaseOutcome(recipe.case_id, "BLOCKED", reason=f"no expectation: {exc}")
    # A row with no render record at all is NOT_RUN, not BLOCKED: nothing ran it, which the
    # existing path below already says correctly. The phase rules apply to a record that exists.
    rendered = render.get(recipe.case_id)
    phase_blocked, phase_measurements, phase_mismatches = (
        _rendering_row_phase(recipe, base, rendered) if rendered is not None else (None, (), ())
    )
    return _three_way_outcome(
        recipe,
        wire,
        expectation,
        rendered,
        browser.get(recipe.case_id),
        phase_blocked,
        phase_measurements,
        phase_mismatches,
    )


def _three_way_outcome(
    recipe: CaseRecipe,
    wire: Mapping[str, Any],
    expectation: Expectation,
    rendered: Mapping[str, Any] | None,
    browser_row: Mapping[str, Any] | None,
    phase_blocked: str | None,
    phase_measurements: tuple[Measurement, ...],
    phase_mismatches: tuple[Mismatch, ...],
    extra_measurements: tuple[Measurement, ...] = (),
) -> CaseOutcome:
    """Judge one composition on its expectation, its browser presentation and its final artifact."""

    if phase_blocked is not None:
        return CaseOutcome(
            recipe.case_id,
            "BLOCKED",
            measurements=phase_measurements + _retrieval_facts(rendered),
            reason=phase_blocked,
        )
    if phase_mismatches:
        return CaseOutcome(
            recipe.case_id,
            "MISMATCH",
            mismatches=phase_mismatches,
            measurements=phase_measurements + _retrieval_facts(rendered),
        )
    observed_browser = browser_observation(recipe.case_id, browser_row)
    observed_final = final_observation(recipe.case_id, rendered)
    # CRITICAL: fail closed on an absent landmark. `compare_case` compares only what the expectation
    # declares, so a row whose expectation carried nothing but container facts used to PASS on an
    # artifact that could have been the wrong frames, or black, with a browser row that carried only
    # its canvas dimensions. Requiring the row's own landmarks -- and reporting BLOCKED, which is a
    # true statement, when they are absent -- is what stops "executed" from meaning "counted".
    # Never make this conditional on whether the observers happen to produce a field yet.
    absent = (
        _absent_expectation_landmarks(recipe, expectation, wire)
        + missing_landmarks(recipe, observed_browser, wire, side="browser")
        + missing_landmarks(recipe, observed_final, wire, side="final")
    )
    already_blocked = (
        not observed_browser.executed
        or not observed_final.executed
        or bool(observed_browser.missing)
        or bool(observed_final.missing)
    )
    # An observation that already names why it is incomplete keeps that reason: a stage that
    # blocked on a render deadline is more useful to a reader than the list of landmarks the
    # blocked stage consequently did not produce.
    if absent and not already_blocked:
        return CaseOutcome(
            recipe.case_id,
            "BLOCKED",
            measurements=(_browser_facts(browser_row) + _retrieval_facts(rendered)),
            reason=f"required observations are absent: {', '.join(absent)}",
        )
    # See BROWSER_OBSERVES_ONLY_AN_UNSCALED_PREVIEW: a preview the product scales down carries
    # no landmark at output precision, so the browser side of such a row is its presentation
    # alone (executed, fingerprint confirmed above through `missing`) and the comparison is the
    # final artifact's. The scale is recorded so the report says which rows this applied to.
    scale = preview_scale(wire, **BASE_PRESENTATION[recipe.base])
    if not browser_observable(wire, recipe.base):
        # The comparison below will not look at the browser side, so the presentation itself
        # has to be established here: a row the preview scales down is still a row the browser
        # had to present, and a browser row that never executed, or that names a gap (a
        # diverged fingerprint), is not excused by the scale.
        if not observed_browser.executed:
            return CaseOutcome(
                recipe.case_id,
                "NOT_RUN",
                measurements=_retrieval_facts(rendered),
                reason="the browser interaction did not execute",
            )
        if observed_browser.missing:
            return CaseOutcome(
                recipe.case_id,
                "BLOCKED",
                measurements=(_browser_facts(browser_row) + _retrieval_facts(rendered)),
                reason=f"observation is missing: {', '.join(observed_browser.missing)}",
            )
    # The browser side is held to the same composition as the preview presents it: the colours
    # its decoder produces (BROWSER_VIDEO_DECODES_THROUGH_LIBYUV_BT709) and only the landmarks
    # the canvas can carry (BROWSER_UNOBSERVABLE); the final side is held to all of it.
    browser_expectation = derive_browser_expectation(recipe, wire)
    outcome = compare_case(
        expectation,
        observed_browser,
        observed_final,
        TOLERANCE_PROFILE,
        supported=recipe.supported,
        declared_unsupported_reason=recipe.unsupported_reason,
        sides=("browser", "final") if browser_observable(wire, recipe.base) else ("final",),
        browser_expectation=browser_expectation,
    )
    extra = (
        extra_measurements
        + phase_measurements
        + _browser_facts(browser_row)
        + _retrieval_facts(rendered)
        + (Measurement("browser.preview_scale", f"{scale:.6f}"),)
    )
    if not extra:
        return outcome
    return CaseOutcome(
        outcome.case_id,
        outcome.status,
        mismatches=outcome.mismatches,
        measurements=outcome.measurements + extra,
        reason=outcome.reason,
    )


def _describe_divergence(declared: object, fixture: object, path: str = "") -> str | None:
    """The first place a declared base disagrees with the corpus fixture, or None."""

    if isinstance(declared, Mapping) and isinstance(fixture, Mapping):
        for key in sorted(set(declared) | set(fixture)):
            if key not in declared:
                return f"{path}/{key} is absent"
            if key not in fixture:
                return f"{path}/{key} is not part of the corpus base"
            found = _describe_divergence(declared[key], fixture[key], f"{path}/{key}")
            if found is not None:
                return found
        return None
    if isinstance(declared, list) and isinstance(fixture, list):
        if len(declared) != len(fixture):
            return f"{path} has {len(declared)} entries, the corpus declares {len(fixture)}"
        for index, (left, right) in enumerate(zip(declared, fixture, strict=True)):
            found = _describe_divergence(left, right, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    if declared != fixture:
        return f"{path} is {declared!r}, the corpus declares {fixture!r}"
    return None


def _corpus_bases() -> dict[str, dict[str, Any]]:
    """Every declared base composition, read from the corpus's own fixtures, by name."""

    return {
        name: cast(
            dict[str, Any],
            json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))["snapshot"],
        )
        for name, filename in BASE_FIXTURE_NAMES.items()
    }


def join(
    backend: Mapping[str, Mapping[str, Any]],
    render: Mapping[str, Mapping[str, Any]],
    browser: Mapping[str, Mapping[str, Any]],
    *,
    declared_bases: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[CaseOutcome]:
    """One outcome per corpus row, in corpus order, with nothing added and nothing dropped.

    Every expectation is derived from the corpus fixture. The render stage declares the base it
    rendered against so this can *check* it, which is a different thing from adopting it.

    GUARD: never derive from the declared base. It was adopted once, and the consequence is the
    purest form of the vacuity this corrective exists to remove: the render stage had rewritten the
    fixture's declared source facts to match the media it happened to build -- 72 frame landmarks
    where the corpus declares 3, a source time base of 1/24 where the corpus declares 1/12288, and
    `embedded_audio: present_bound` on the overlay where the corpus declares
    `excluded_overlay_policy`, which changes which clips own audio at all. Every expectation was
    then derived from the renderer's own account of its media, and the `public_fingerprint` did not
    move, because none of those fields is hashed into it. A stage that cannot render what the
    corpus declares must fail and say so; it must not restate the corpus.
    """

    bases = _corpus_bases()
    for name, declared in (declared_bases or {}).items():
        divergence = _describe_divergence(declared, bases[name])
        if divergence is not None:
            raise JoinError(
                "the render stage declared a composition base that is not the corpus base: "
                f"{name}: {divergence}"
            )
    book = build_recipes()
    corpus = build_corpus()
    outcomes: list[CaseOutcome] = []
    for case in corpus.cases:
        recipe = book.by_id.get(case.case_id)
        if recipe is None:
            raise JoinError(f"{case.case_id} has no recipe")
        # A row is judged against the composition its own recipe names, never against whichever
        # base happened to be first: the two bases differ in output size, asset identity and every
        # fingerprint derived from them.
        outcomes.append(join_row(recipe, bases[recipe.base], backend, render, browser))
    return outcomes


def _optional(path: Path | None, reader: Any) -> dict[str, Mapping[str, Any]]:
    if path is None:
        return {}
    if not path.is_file():
        raise JoinError("a named stage artifact does not exist")
    result: dict[str, Mapping[str, Any]] = reader(path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Join the M25-20 stages into one closed report.")
    parser.add_argument("--backend", type=Path, help="nle_semantic_qualify output")
    parser.add_argument("--render", type=Path, help="nle_semantic_render JSON Lines output")
    parser.add_argument("--browser", type=Path, help="the browser stage's collected rows")
    parser.add_argument("--output", type=Path, required=True, help="where to write the report")
    parser.add_argument("--candidate-tree", required=True)
    parser.add_argument("--renderer-fingerprint", required=True)
    parser.add_argument("--probe-fingerprint", required=True)
    parser.add_argument("--extractor-fingerprint", required=True)
    parser.add_argument("--browser-profile-fingerprint", required=True)
    parser.add_argument("--chromium-version", required=True)
    args = parser.parse_args(argv)

    outcomes = join(
        _optional(args.backend, read_backend),
        _optional(args.render, read_render),
        _optional(args.browser, read_browser),
        declared_bases=None if args.render is None else read_render_bases(args.render),
    )
    report = build_report(
        outcomes,
        candidate_tree=args.candidate_tree,
        renderer_fingerprint=args.renderer_fingerprint,
        probe_fingerprint=args.probe_fingerprint,
        extractor_fingerprint=args.extractor_fingerprint,
        browser_profile_fingerprint=args.browser_profile_fingerprint,
        chromium_version=args.chromium_version,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "required_rows": report["required_rows"],
                "executed_rows": report["executed_rows"],
                "totals": report["totals"],
                "admitted": report["admission"]["admitted"],
                "conformant": report["conformant"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
