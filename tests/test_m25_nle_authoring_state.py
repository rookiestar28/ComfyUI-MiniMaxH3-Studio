from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import cast

import pytest

from comfyui_h3_context.core.composition_contract import (
    CompositionClip,
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    NleAuthoringState,
    adapt_legacy_authoring_projection_to_nle_state,
    create_nle_authoring_state,
    decode_nle_authoring_state,
    materialize_render_snapshot,
)
from comfyui_h3_context.core.timeline_authoring import (
    NleCommand,
    NleCommandKind,
    apply_nle_command,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"


def fixture_snapshot() -> PublicCompositionSnapshot:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    wire = copy.deepcopy(document["snapshot"])
    primary = wire["assets"][0]
    primary["source_frame_count"] = 48
    primary["landmarks"] = [
        {"frame_index": frame, "pts": frame * 512, "dts": frame * 512, "duration_ticks": 512}
        for frame in range(48)
    ]
    overlay = wire["assets"][1]
    overlay["embedded_audio"] = "absent"
    overlay["source_sample_count"] = None
    overlay["source_frame_count"] = 12
    overlay["landmarks"] = [
        {"frame_index": frame, "pts": frame * 512, "dts": frame * 512, "duration_ticks": 512}
        for frame in range(12)
    ]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def state_for(clips: tuple[CompositionClip, ...]) -> NleAuthoringState:
    snapshot = fixture_snapshot()
    return create_nle_authoring_state(
        project_id=snapshot.project_id,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        edit_capacity_frames=3_600,
        assets=snapshot.assets,
        tracks=snapshot.tracks,
        clips=clips,
        audio_extension=snapshot.audio_extension,
        blockers=snapshot.blockers,
    )


def test_empty_and_bin_only_state_has_zero_extent_and_no_render_snapshot() -> None:
    state = state_for(())
    wire = state.to_wire()

    assert state.schema == NLE_AUTHORING_SCHEMA
    assert state.profile_id == NLE_AUTHORING_PROFILE_ID
    assert state.operation_profile_id == NLE_OPERATION_PROFILE_ID
    assert state.edit_capacity_frames == 3_600
    assert state.content_end_exclusive == 0
    assert wire["assets"]
    assert wire["clips"] == []
    assert "output" not in wire
    assert materialize_render_snapshot(state) is None
    assert decode_nle_authoring_state(wire) == state


def test_legacy_projection_adapter_separates_fixed_capacity_from_render_duration() -> None:
    snapshot = fixture_snapshot()
    projection = {
        "schema": "h3.context.authoring_workbench.projection.v1",
        "workspace_handle": snapshot.workspace_handle,
        "context_source_id": "context-fixture",
        "task_mode": "T2VA",
        "registry_fingerprint": "sha256:" + "1" * 64,
        "reference": {"revision": snapshot.workspace_revision, "sources": []},
        "availability": {"producer": "fixture", "revision": 1},
        "timeline": {
            "revision": snapshot.timeline_revision,
            "content_fingerprint": "sha256:" + "2" * 64,
            "profile": {"max_extent_frames": 3_600},
            "clips": [],
            "links": [],
            "selection": [],
            "blockers": [],
        },
        "rejection": None,
    }
    timings = {asset.asset_id: asset.to_wire() for asset in snapshot.assets}

    empty = adapt_legacy_authoring_projection_to_nle_state(
        projection,
        project_id=snapshot.project_id,
        workspace_revision=snapshot.workspace_revision,
        asset_timings=timings,
    )
    assert empty.content_end_exclusive == 0
    assert materialize_render_snapshot(empty) is None

    placed = copy.deepcopy(projection)
    placed_timeline = cast(dict[str, object], placed["timeline"])
    placed_timeline["clips"] = [
        {
            "clip_id": "clip-extent",
            "asset_id": "vid-primary",
            "kind": "video",
            "lane": 0,
            "start_frame": 24,
            "frames": 12,
            "source_start_frame": 0,
            "envelope": [],
        }
    ]
    state = adapt_legacy_authoring_projection_to_nle_state(
        placed,
        project_id=snapshot.project_id,
        workspace_revision=snapshot.workspace_revision,
        asset_timings=timings,
    )
    render_snapshot = materialize_render_snapshot(state)
    assert state.edit_capacity_frames == 3_600
    assert state.content_end_exclusive == 36
    assert render_snapshot is not None and render_snapshot.output.duration_frames == 36


def test_nonempty_render_snapshot_duration_is_maximum_placed_clip_end() -> None:
    snapshot = fixture_snapshot()
    state = state_for(snapshot.clips)
    expected_end = max(clip.start_frame + clip.duration_frames for clip in snapshot.clips)

    assert state.content_end_exclusive == expected_end
    render_snapshot = materialize_render_snapshot(state)
    assert render_snapshot is not None
    assert render_snapshot.output.duration_frames == expected_end
    assert render_snapshot.clips == snapshot.clips


def test_authoring_wire_is_closed_and_rejects_forged_extent_or_fingerprint() -> None:
    state = state_for(())
    unknown = state.to_wire()
    unknown["output"] = {}
    with pytest.raises(ValueError, match="must be closed"):
        decode_nle_authoring_state(unknown)

    forged = state.to_wire()
    forged["content_end_exclusive"] = 1
    with pytest.raises(ValueError, match="content extent"):
        decode_nle_authoring_state(forged)

    stale = state.to_wire()
    workspace_revision = stale["workspace_revision"]
    assert isinstance(workspace_revision, int)
    stale["workspace_revision"] = workspace_revision + 1
    with pytest.raises(ValueError, match="fingerprint"):
        decode_nle_authoring_state(stale)


def test_capacity_is_a_fixed_server_profile_not_a_client_selected_output() -> None:
    snapshot = fixture_snapshot()
    with pytest.raises(ValueError, match="capacity"):
        create_nle_authoring_state(
            project_id=snapshot.project_id,
            workspace_handle=snapshot.workspace_handle,
            workspace_revision=snapshot.workspace_revision,
            timeline_revision=snapshot.timeline_revision,
            edit_capacity_frames=3_601,
            assets=snapshot.assets,
            tracks=snapshot.tracks,
            clips=(),
            audio_extension=snapshot.audio_extension,
            blockers=snapshot.blockers,
        )


def test_empty_authoring_accepts_non_render_command_without_creating_an_output() -> None:
    initial = state_for(())
    command = NleCommand(
        NleCommandKind.CREATE_TRACK,
        {"track_id": "track-new", "kind": "image_overlay", "order": 1},
    )

    transition = apply_nle_command(initial, command)

    assert isinstance(transition.snapshot, NleAuthoringState)
    assert transition.snapshot.content_end_exclusive == 0
    assert "track-new" in {track.track_id for track in transition.snapshot.tracks}
    assert materialize_render_snapshot(transition.snapshot) is None


def test_authoring_insert_uses_content_end_and_rejects_capacity_overflow_atomically() -> None:
    snapshot = fixture_snapshot()
    initial = state_for(())
    clip = snapshot.clips[2].to_wire()
    clip.update({"clip_id": "clip-near-capacity", "start_frame": 3_500, "duration_frames": 96})
    command = NleCommand(NleCommandKind.INSERT_ASSET_CLIP, {"clip": clip})

    transition = apply_nle_command(initial, command)
    assert isinstance(transition.snapshot, NleAuthoringState)
    render_snapshot = materialize_render_snapshot(transition.snapshot)

    assert transition.snapshot.content_end_exclusive == 3_596
    assert transition.snapshot.edit_capacity_frames == 3_600
    assert render_snapshot is not None and render_snapshot.output.duration_frames == 3_596

    clip["duration_frames"] = 101
    overflow = NleCommand(NleCommandKind.INSERT_ASSET_CLIP, {"clip": clip})
    with pytest.raises(ValueError, match="capacity"):
        apply_nle_command(initial, overflow)
    assert initial.clips == ()
    assert initial.content_end_exclusive == 0
