from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
    TIMELINE_TRANSACTION_SCHEMA_V2,
    create_nle_authoring_state,
)
from comfyui_h3_context.core.timeline_authoring import NleCommand, NleCommandKind
from comfyui_h3_context.core.timeline_history_v2 import (
    TimelineHistoryStateV2,
    TimelineHistoryV2Error,
    apply_timeline_transaction_v2,
    timeline_history_projection_v2,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"


def fixture_snapshot() -> PublicCompositionSnapshot:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    wire = copy.deepcopy(document["snapshot"])
    for asset, frame_count in ((wire["assets"][0], 48), (wire["assets"][1], 12)):
        asset["source_frame_count"] = frame_count
        asset["landmarks"] = [
            {"frame_index": frame, "pts": frame * 512, "dts": frame * 512, "duration_ticks": 512}
            for frame in range(frame_count)
        ]
    wire["assets"][1]["embedded_audio"] = "absent"
    wire["assets"][1]["source_sample_count"] = None
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def empty_history() -> TimelineHistoryStateV2:
    snapshot = fixture_snapshot()
    authoring = create_nle_authoring_state(
        project_id=snapshot.project_id,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        edit_capacity_frames=3_600,
        assets=snapshot.assets,
        tracks=snapshot.tracks,
        clips=(),
        audio_extension=snapshot.audio_extension,
        blockers=snapshot.blockers,
    )
    return TimelineHistoryStateV2.initialize(authoring)


def transaction(
    state: TimelineHistoryStateV2,
    request_id: str,
    *commands: NleCommand,
) -> dict[str, Any]:
    authoring = state.authoring
    return {
        "schema": TIMELINE_TRANSACTION_SCHEMA_V2,
        "authoring_schema": NLE_AUTHORING_SCHEMA,
        "profile_id": NLE_AUTHORING_PROFILE_ID,
        "operation_profile_id": NLE_OPERATION_PROFILE_ID,
        "request_id": request_id,
        "transaction_id": f"tx-{request_id}",
        "workspace_handle": authoring.workspace_handle,
        "expected_workspace_revision": authoring.workspace_revision,
        "expected_timeline_revision": authoring.timeline_revision,
        "expected_timeline_fingerprint": authoring.timeline_fingerprint,
        "expected_authoring_fingerprint": authoring.authoring_fingerprint,
        "commands": [command.to_wire() for command in commands],
    }


def test_v2_projection_has_empty_authoring_without_a_render_snapshot() -> None:
    state = empty_history()
    projection = timeline_history_projection_v2(state.authoring.workspace_handle, state)

    assert projection["schema"] == TIMELINE_HISTORY_PROJECTION_SCHEMA_V2
    projected_authoring = projection["authoring"]
    assert isinstance(projected_authoring, dict)
    assert projected_authoring["content_end_exclusive"] == 0
    assert projection["render_snapshot"] is None


def test_title_track_and_clip_share_one_atomic_undo_redo_entry() -> None:
    state = empty_history()
    title = copy.deepcopy(next(clip.to_wire() for clip in fixture_snapshot().clips if clip.text))
    title.update(
        clip_id="title.first",
        track_id="track.title.first",
        start_frame=0,
        duration_frames=24,
        source_start_frame=0,
    )
    create = NleCommand(
        NleCommandKind.CREATE_TRACK,
        {
            "track_id": "track.title.first",
            "kind": "text_overlay",
            "order": len(state.authoring.tracks),
        },
    )
    insert = NleCommand(NleCommandKind.INSERT_TITLE_CLIP, {"clip": title})
    updated, receipt = apply_timeline_transaction_v2(
        state, transaction(state, "title.add", create, insert)
    )
    assert len(updated.authoring.tracks) == len(state.authoring.tracks) + 1
    assert len(updated.authoring.clips) == 1
    assert updated.authoring.content_end_exclusive == 24
    assert len(updated.undo_entries) == 1
    assert receipt.history_cursor is not None
    undone, undo_receipt = apply_timeline_transaction_v2(
        updated,
        transaction(
            updated,
            "title.undo",
            NleCommand(
                NleCommandKind.UNDO,
                {"history_cursor": receipt.history_cursor},
            ),
        ),
    )
    assert undone.authoring.tracks == state.authoring.tracks
    assert undone.authoring.clips == state.authoring.clips
    assert undone.authoring.content_end_exclusive == 0
    redone, _ = apply_timeline_transaction_v2(
        undone,
        transaction(
            undone,
            "title.redo",
            NleCommand(
                NleCommandKind.REDO,
                {"history_cursor": undo_receipt.history_cursor},
            ),
        ),
    )
    assert redone.authoring.tracks == updated.authoring.tracks
    assert redone.authoring.clips == updated.authoring.clips
    assert redone.authoring.content_end_exclusive == 24


def test_invalid_second_title_command_publishes_no_track_clip_or_history() -> None:
    state = empty_history()
    before = copy.deepcopy(state)
    title = copy.deepcopy(next(clip.to_wire() for clip in fixture_snapshot().clips if clip.text))
    title.update(
        clip_id="title.invalid",
        track_id="track.title.invalid",
        start_frame=0,
        duration_frames=24,
        source_start_frame=0,
    )
    text = title["text"]
    assert isinstance(text, dict)
    text["font_asset_id"] = "font.missing"
    create = NleCommand(
        NleCommandKind.CREATE_TRACK,
        {
            "track_id": "track.title.invalid",
            "kind": "text_overlay",
            "order": len(state.authoring.tracks),
        },
    )
    with pytest.raises(TimelineHistoryV2Error) as refused:
        apply_timeline_transaction_v2(
            state,
            transaction(
                state,
                "title.refused",
                create,
                NleCommand(NleCommandKind.INSERT_TITLE_CLIP, {"clip": title}),
            ),
        )
    assert refused.value.code == "invalid_contract"
    assert state == before
    assert not state.undo_entries and not state.redo_entries
    assert all(track.track_id != "track.title.invalid" for track in state.authoring.tracks)
    assert state.authoring.clips == ()


def test_v2_insert_undo_and_redo_keep_render_extent_separate_from_capacity() -> None:
    state = empty_history()
    image_clip = fixture_snapshot().clips[2].to_wire()
    image_clip.update(
        {"clip_id": "clip-v2-near-capacity", "start_frame": 3_500, "duration_frames": 96}
    )
    insert = NleCommand(NleCommandKind.INSERT_ASSET_CLIP, {"clip": image_clip})
    inserted, receipt = apply_timeline_transaction_v2(state, transaction(state, "insert", insert))

    assert inserted.authoring.edit_capacity_frames == 3_600
    assert inserted.authoring.content_end_exclusive == 3_596
    assert receipt.history_cursor is not None
    receipt_wire = receipt.to_wire()
    render_snapshot = receipt_wire["render_snapshot"]
    assert isinstance(render_snapshot, dict)
    output = render_snapshot["output"]
    assert isinstance(output, dict)
    assert output["duration_frames"] == 3_596

    undo = NleCommand(NleCommandKind.UNDO, {"history_cursor": receipt.history_cursor})
    undone, undo_receipt = apply_timeline_transaction_v2(
        inserted, transaction(inserted, "undo", undo)
    )
    empty_projection = timeline_history_projection_v2(undone.authoring.workspace_handle, undone)
    assert undone.authoring.content_end_exclusive == 0
    assert empty_projection["render_snapshot"] is None
    assert undo_receipt.history_cursor is not None

    redo = NleCommand(NleCommandKind.REDO, {"history_cursor": undo_receipt.history_cursor})
    redone, _ = apply_timeline_transaction_v2(undone, transaction(undone, "redo", redo))
    assert redone.authoring.content_end_exclusive == 3_596


def test_v2_selection_changes_history_metadata_without_changing_authoring_identity() -> None:
    state = empty_history()
    command = NleCommand(NleCommandKind.SELECT_CLIPS, {"clip_ids": []})

    updated, receipt = apply_timeline_transaction_v2(
        state, transaction(state, "select-empty", command)
    )

    assert updated.authoring == state.authoring
    assert updated.selection == ()
    assert not updated.undo_entries
    assert receipt.before_authoring_fingerprint == receipt.after_authoring_fingerprint
    assert receipt.history_cursor is None


def test_v2_capacity_refusal_is_atomic_and_v1_schema_is_rejected() -> None:
    state = empty_history()
    image_clip = fixture_snapshot().clips[2].to_wire()
    image_clip.update(
        {"clip_id": "clip-over-capacity", "start_frame": 3_500, "duration_frames": 101}
    )
    command = NleCommand(NleCommandKind.INSERT_ASSET_CLIP, {"clip": image_clip})
    before = state.authoring

    with pytest.raises(TimelineHistoryV2Error, match="content extent exceeds edit capacity"):
        apply_timeline_transaction_v2(state, transaction(state, "overflow", command))
    assert state.authoring is before
    assert not state.undo_entries and not state.idempotency_records

    v1 = transaction(state, "wrong-version", command)
    v1["schema"] = "h3.context.timeline_transaction.v1"
    with pytest.raises(TimelineHistoryV2Error, match="unsupported_profile"):
        apply_timeline_transaction_v2(state, v1)


@pytest.mark.parametrize("frame_count", [257, 512])
def test_v2_dense_source_landmarks_survive_insert_replay_undo_and_redo(
    frame_count: int,
) -> None:
    source_snapshot = fixture_snapshot()
    asset_wire = source_snapshot.assets[0].to_wire()
    asset_wire["source_frame_count"] = frame_count
    asset_wire["source_sample_count"] = frame_count * 2_000
    asset_wire["landmarks"] = [
        {
            "frame_index": frame,
            "pts": frame * 512,
            "dts": frame * 512,
            "duration_ticks": 512,
        }
        for frame in range(frame_count)
    ]
    snapshot_wire = source_snapshot.to_wire()
    snapshot_wire["assets"] = [asset_wire]
    snapshot_wire["clips"] = []
    snapshot_wire["public_fingerprint"] = public_snapshot_fingerprint(snapshot_wire)
    snapshot = decode_public_snapshot(snapshot_wire)
    authoring = create_nle_authoring_state(
        project_id=snapshot.project_id,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        edit_capacity_frames=3_600,
        assets=snapshot.assets,
        tracks=snapshot.tracks,
        clips=(),
        audio_extension=snapshot.audio_extension,
        blockers=snapshot.blockers,
    )
    initial = TimelineHistoryStateV2.initialize(authoring)
    clip = source_snapshot.clips[0].to_wire()
    clip.update(
        {
            "clip_id": "dense-v2-clip",
            "asset_id": snapshot.assets[0].asset_id,
            "start_frame": 0,
            "duration_frames": frame_count,
            "source_start_frame": 0,
        }
    )
    insert = NleCommand(NleCommandKind.INSERT_ASSET_CLIP, {"clip": clip})
    insert_wire = transaction(initial, "dense-v2-insert", insert)

    inserted, receipt = apply_timeline_transaction_v2(initial, insert_wire)
    assert len(inserted.authoring.assets[0].landmarks) == frame_count
    assert inserted.authoring.clips[0].duration_frames == frame_count
    replayed, replay_receipt = apply_timeline_transaction_v2(inserted, insert_wire)
    assert replayed == inserted
    assert replay_receipt.to_wire() == receipt.to_wire()

    undo = NleCommand(NleCommandKind.UNDO, {"history_cursor": receipt.history_cursor})
    undone, undo_receipt = apply_timeline_transaction_v2(
        inserted, transaction(inserted, "dense-v2-undo", undo)
    )
    assert undone.authoring.clips == ()
    redo = NleCommand(NleCommandKind.REDO, {"history_cursor": undo_receipt.history_cursor})
    redone, _ = apply_timeline_transaction_v2(undone, transaction(undone, "dense-v2-redo", redo))
    assert redone.authoring.clips == inserted.authoring.clips
    assert len(redone.authoring.assets[0].landmarks) == frame_count
