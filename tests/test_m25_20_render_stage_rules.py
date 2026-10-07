"""M25-20 post-closeout corrective: the render stage observes each phase where both phases look.

A command row is proved by its "after" artifact failing its "before" expectation on a measurement,
and the two phases prescribe different points -- a trimmed primary states no identity at the frames
it vacated, a moved overlay states its ends a frame later. An "after" artifact observed only at its
own points would then differ from the before expectation by absence alone, which is exactly what a
stage that rendered the same picture twice would also show, and the join refuses it as unmeasured.
So the stage observes every phase of a command row at the union of the phases' prescriptions.

Nothing here renders: the prescriptions are derived from the compositions the row's own driver
produces, which is all the stage itself consults to decide where to look.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

from comfyui_h3_context.adapters.authoring_output_service import AuthoringOutputRegistry
from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringWorkbenchError
from comfyui_h3_context.core.authoring_output_protocol import require_output_workspace
from comfyui_h3_context.core.semantic_conformance import ACCEPTED_ARTIFACT_RETRIEVAL
from comfyui_h3_context.core.semantic_conformance_cases import build_recipes
from comfyui_h3_context.core.semantic_conformance_drive import command_phases, run_setup
from comfyui_h3_context.core.semantic_conformance_expect import (
    derive_expectation,
    identity_reads,
    patch_sample_points,
)
from comfyui_h3_context.core.semantic_conformance_media import SOURCE_TIME_BASE_DEN

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import nle_semantic_conformance as extractor  # noqa: E402
from scripts import nle_semantic_render as stage  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "m25_20_semantic_corpus_base_v1.json"
BOOK = build_recipes()


def _base() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["snapshot"])


def _phases(case_id: str) -> tuple[tuple[str, Any], ...]:
    recipe = BOOK.by_id[case_id]
    phases = command_phases(recipe, _base())
    return (("before", phases.before), ("after", phases.after))


def _single(case_id: str) -> Any:
    setup = run_setup(BOOK.by_id[case_id], _base())
    assert setup.state is not None, setup.error
    return setup.state.snapshot


def _ffmpeg_expression(expression: str, n: int) -> float:
    """Evaluate the subset of ffmpeg's expression language the timing graph uses."""

    names = {
        "N": n,
        "TB": 1 / SOURCE_TIME_BASE_DEN,
        "iff": lambda c, a, b: a if c else b,
        "lt": lambda a, b: 1 if a < b else 0,
        "eq": lambda a, b: 1 if a == b else 0,
        "mod": lambda a, b: a % b,
        "floor": lambda a: float(a // 1),
    }
    body = expression.replace("\\,", ",").replace("if(", "iff(")
    return float(eval(body, {"__builtins__": {}}, names))  # noqa: S307 -- a literal expression


def test_the_timing_source_is_encoded_at_exactly_the_declared_tick_of_every_frame() -> None:
    """The `setpts` the render stage writes lands frame N on `pts_table()[N]` for all 84 frames.

    The product measures the file's table back and the base declares that measurement, so the
    expression is the whole of what makes `source_rate_different` and `vfr_unequal_intervals` the
    cases they name (B-58). `settb` precedes it in the graph, so `TB` is the source tick.
    """

    from comfyui_h3_context.core.semantic_conformance_media import (
        OUTPUT_FRAME_TICKS,
        TIMING_ASSET,
        profile_for,
    )

    profile = profile_for(TIMING_ASSET)
    assert profile is not None and profile.constant_rate() is None
    table = profile.pts_table()
    assert len(table) == 84
    assert [row[2] for row in table[:36]] == [2 * OUTPUT_FRAME_TICKS] * 36
    assert [row[2] for row in table[36:]] == [640, 384] * 24
    filter_text = stage._vfr_setpts(profile)
    assert filter_text.startswith("setpts=")
    for index, pts, _ in table:
        assert abs(_ffmpeg_expression(filter_text[len("setpts=") :], index) - pts) < 1e-6, index


def test_the_audio_prescription_places_an_owner_s_source_origin_by_source_time() -> None:
    """An owner reading the timing source from frame 36 has its origin three seconds back.

    The prescription's `shift_samples` is where the owner's source sample 0 lands on the output;
    the extractor labels onsets and measures a source-start burst's gap from it (B-58, B-61).
    Counting the start frame as output frames would put the origin at 1.5 s for a 12 fps source.
    """

    from comfyui_h3_context.core.semantic_conformance_media import (
        TIMING_ASSET,
        TIMING_VFR_START_FRAME,
    )

    wire = _base()
    primary = next(clip for clip in wire["clips"] if clip["clip_id"] == "clip-main")
    primary["asset_id"] = TIMING_ASSET
    primary["source_start_frame"] = TIMING_VFR_START_FRAME
    (window,) = stage._audio_owner_windows(wire)
    assert window["shift_samples"] == -3 * wire["output"]["sample_rate"]
    primary["source_start_frame"] = 0
    (window,) = stage._audio_owner_windows(wire)
    assert window["shift_samples"] == 0


def test_each_phase_of_a_command_row_is_observed_where_both_phases_look() -> None:
    """The trimmed primary's vacated identity frames are still read on the after artifact."""

    recipe = BOOK.by_id["command.trim_clip.accepted"]
    phases = _phases(recipe.case_id)
    before_wire, after_wire = (snapshot.to_wire() for _name, snapshot in phases)
    before, after = (derive_expectation(recipe, wire) for wire in (before_wire, after_wire))
    vacated = sorted(
        {item.output_frame for item in before.source_mapping}
        - {item.output_frame for item in after.source_mapping}
    )
    assert vacated, "this row is chosen because the trim vacates identity frames"
    assert not {read.output_frame for read in identity_reads(after_wire)} & set(vacated)

    joined = stage._companion_prescriptions(phases)
    assert set(joined) == {"before", "after"}
    after_frames = {read.output_frame for read in joined["after"].reads}
    assert set(vacated) <= after_frames
    # And the after phase's own points come first and are not displaced by the companion's.
    own = stage.Prescription.for_wire(after_wire)
    assert joined["after"].points[: len(own.points)] == own.points
    assert joined["after"].reads[: len(own.reads)] == own.reads
    before_labels = {point.label for point in patch_sample_points(before_wire)}
    assert before_labels <= {point.label for point in joined["after"].points}


def test_a_point_both_phases_name_is_measured_once_under_the_phase_s_own_prescription() -> None:
    """Where both phases prescribe a label, the phase's own point stands; the other is dropped."""

    phases = _phases("command.move_clip.accepted")
    joined = stage._companion_prescriptions(phases)
    for name, snapshot in phases:
        own = stage.Prescription.for_wire(snapshot.to_wire())
        labels = [point.label for point in joined[name].points]
        assert len(labels) == len(set(labels)), name
        frames = [read.output_frame for read in joined[name].reads]
        assert len(frames) == len(set(frames)), name
        for point in own.points:
            assert point in joined[name].points, (name, point.label)


def test_a_single_phase_row_is_observed_at_its_own_points_only() -> None:
    """Nothing joins a property row's prescription: there is no other phase to hold it to."""

    recipe = next(item for item in BOOK.rendering() if item.command is None)
    assert stage._companion_prescriptions(()) == {}
    assert recipe.command is None


# ---------------------------------------------------------------------------------------------
# The artifact reaches the extractor through the accepted M25-19 transport, and says so.
# ---------------------------------------------------------------------------------------------


def test_the_workspace_port_serves_exactly_the_row_s_composition() -> None:
    """The registry's currency reads and plan binding see the corpus row, nothing derived.

    `_read_currency` asks the workspace for its reference revision and its timeline snapshot and
    holds the request to them; the port answers with the row's own snapshot under its own handle,
    refuses every other handle, and the handle the corpus base carries is one the transport's own
    validator admits -- minted the product's way, frozen once, never rewritten by the stage.
    """

    wire = _base()
    handle = str(wire["workspace_handle"])
    assert re.fullmatch(r"authoring-[0-9a-f]{32}", handle), handle
    require_output_workspace(handle)
    snapshot = _single("transform.anchor_x_bp.identity")
    assert snapshot.workspace_handle == handle
    port = stage._CorpusOutputWorkspace(cast(Any, object()), snapshot, deadline=1.0)
    projection = port.dispatch(
        {"action": "read_projection", "payload": {"workspace_handle": handle}}
    )
    assert projection.status == 200 and projection.body == {
        "reference": {"revision": snapshot.workspace_revision}
    }
    history = port.dispatch(
        {"action": "read_timeline_history", "payload": {"workspace_handle": handle}}
    )
    assert history.status == 200 and history.body == {"snapshot": snapshot.to_wire()}
    with pytest.raises(AuthoringWorkbenchError):
        port.dispatch(
            {"action": "read_projection", "payload": {"workspace_handle": "authoring-" + "0" * 32}}
        )
    with pytest.raises(AuthoringWorkbenchError):
        port.prepare_render_plan("authoring-" + "0" * 32)


def test_an_observed_phase_is_labelled_with_the_accepted_transport_and_nothing_else() -> None:
    """The label the join admits is the one the stage writes, and only for a verified download."""

    assert stage.VERIFIED_DOWNLOAD == ACCEPTED_ARTIFACT_RETRIEVAL
    snapshot = _single("transform.anchor_x_bp.identity")
    cache: dict[str, stage.PhaseResult] = {}
    with mock.patch.object(stage, "_render_and_observe", return_value=({"ok": True}, 48, 1)):
        result = stage._render_phase(
            cast(Any, object()), "single", snapshot, timeout_seconds=1.0, cache=cache
        )
    assert result.status == "OBSERVED"
    assert result.artifact_retrieval == ACCEPTED_ARTIFACT_RETRIEVAL
    # A blocked phase retrieved nothing and says so.
    with mock.patch.object(
        stage, "_render_and_observe", side_effect=stage.RenderStageError("output_failed")
    ):
        blocked = stage._render_phase(
            cast(Any, object()), "single", snapshot, timeout_seconds=1.0, cache={}
        )
    assert blocked.status == "BLOCKED" and blocked.artifact_retrieval is None


def test_a_real_row_is_rendered_and_downloaded_through_the_output_registry() -> None:
    """With the pinned pair supplied, one row goes create -> status -> download and is judged.

    The registry's download is spied on, not replaced: the bytes the extractor decodes are the
    ones `open_download` served, and the phase carries the accepted label.
    """

    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffprobe_path = extractor.resolve_binary(Path(ffprobe_value).resolve(strict=True), "probe")
    ffmpeg_path = extractor.resolve_binary(Path(ffmpeg_value).resolve(strict=True), "renderer")
    qualification = json.loads(extractor.QUALIFICATION_PATH.read_text(encoding="utf-8"))
    # Short on purpose (B-68): the store's staging path must stay under MAX_PATH in a worktree.
    scratch = ROOT / ".tmp" / f"nsrt-{uuid.uuid4().hex[:12]}"
    scratch.mkdir(parents=True, exist_ok=False)
    runtime = stage._build_runtime(
        ffmpeg_path, ffprobe_path, qualification["renderer"], qualification["probe"], scratch
    )
    downloads: list[str] = []
    original = AuthoringOutputRegistry.open_download

    def spy(self: Any, handle: str, workspace_handle: str, **kwargs: Any) -> Any:
        downloads.append(handle)
        return original(self, handle, workspace_handle, **kwargs)

    try:
        recipe = BOOK.by_id["transform.anchor_x_bp.identity"]
        ((_, snapshot),) = stage._build_case_snapshots(runtime.base_wire, recipe).phases
        with mock.patch.object(AuthoringOutputRegistry, "open_download", spy):
            result = stage._render_phase(
                runtime, "single", snapshot, timeout_seconds=180.0, cache={}
            )
    finally:
        runtime.close()
    assert result.status == "OBSERVED", result.blocked_code
    assert result.artifact_retrieval == ACCEPTED_ARTIFACT_RETRIEVAL
    assert len(downloads) == 1 and downloads[0].startswith("aro_")
    assert result.measured_frame_count == 48
    observation = result.observation
    assert observation is not None and observation["extractor_fingerprint"] == (
        extractor.extractor_fingerprint()
    )
