from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
    resolve_composition,
)
from comfyui_h3_context.core.timeline_authoring import TimelineCommandError
from comfyui_h3_context.core.timeline_history import (
    TIMELINE_TRANSACTION_SCHEMA,
    TimelineHistoryError,
    TimelineHistoryState,
    apply_timeline_transaction,
)


def _snapshot(rate: int | str) -> PublicCompositionSnapshot:
    fixture = Path(__file__).parent / "fixtures/m25_10_composition_contract_v1.json"
    wire = cast(dict[str, Any], json.loads(fixture.read_text(encoding="utf-8"))["snapshot"])
    wire["assets"] = [wire["assets"][0]]
    wire["tracks"] = [wire["tracks"][0]]
    wire["clips"] = [wire["clips"][0]]
    durations = [2000, 4000] * 8 if rate == "vfr" else [48000 // int(rate)] * int(rate)
    pts = 0
    landmarks = []
    for index, duration in enumerate(durations):
        landmarks.append({"frame_index": index, "pts": pts, "dts": pts, "duration_ticks": duration})
        pts += duration
    wire["assets"][0].update(
        source_time_base={"num": 1, "den": 48000},
        source_frame_count=len(landmarks),
        source_sample_count=48000,
        landmarks=landmarks,
    )
    wire["clips"][0].update(start_frame=0, duration_frames=24, source_start_frame=0)
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def _command(kind: str, **payload: object) -> dict[str, object]:
    return {"kind": kind, "payload": payload}


def _transaction(
    state: TimelineHistoryState, request: str, *commands: dict[str, object]
) -> dict[str, object]:
    snapshot = state.snapshot
    return {
        "schema": TIMELINE_TRANSACTION_SCHEMA,
        "request_id": request,
        "transaction_id": f"tx-{request}",
        "workspace_handle": snapshot.workspace_handle,
        "expected_workspace_revision": snapshot.workspace_revision,
        "expected_timeline_revision": snapshot.timeline_revision,
        "expected_timeline_fingerprint": snapshot.timeline_fingerprint,
        "commands": list(commands),
    }


def _observations(snapshot: PublicCompositionSnapshot) -> tuple[tuple[int, ...], ...]:
    observations = []
    for frame in range(24):
        scene = resolve_composition(snapshot, frame)
        assert not scene.blockers
        assert len(scene.layers) == 1
        layer = scene.layers[0]
        audio = scene.audio_span
        assert layer.source_frame is not None and layer.source_pts is not None
        assert audio is not None
        observations.append(
            (
                layer.source_frame,
                layer.source_pts,
                audio.output_start_sample,
                audio.output_end_sample,
                audio.source_start_sample,
                audio.source_end_sample,
            )
        )
    return tuple(observations)


@pytest.mark.parametrize("rate", [12, 24, 48, "vfr"])
def test_split_merge_undo_redo_and_replay_preserve_source_observations(rate: int | str) -> None:
    initial = TimelineHistoryState.initialize(_snapshot(rate))
    expected = _observations(initial.snapshot)
    # The independent synthetic source at 250 ms is known before any edit or resolver comparison.
    source_index_at_quarter_second = {12: 3, 24: 6, 48: 12, "vfr": 4}[rate]
    assert expected[6] == (source_index_at_quarter_second, 12000, 12000, 14000, 12000, 14000)
    assert tuple(row[4:] for row in expected) == tuple(
        (frame * 2000, (frame + 1) * 2000) for frame in range(24)
    )
    split_tx = _transaction(
        initial,
        "split",
        _command("split_clip", clip_id="clip-main", at_offset_frames=6, right_clip_id="clip-right"),
    )
    split, split_receipt = apply_timeline_transaction(initial, split_tx)
    assert _observations(split.snapshot) == expected
    replayed, replay_receipt = apply_timeline_transaction(split, split_tx)
    assert replayed == split and replay_receipt == split_receipt
    undone, undo_receipt = apply_timeline_transaction(
        split,
        _transaction(split, "undo", _command("undo", history_cursor=split_receipt.history_cursor)),
    )
    assert _observations(undone.snapshot) == expected and len(undone.snapshot.clips) == 1
    redone, _ = apply_timeline_transaction(
        undone,
        _transaction(undone, "redo", _command("redo", history_cursor=undo_receipt.history_cursor)),
    )
    assert _observations(redone.snapshot) == expected and len(redone.snapshot.clips) == 2
    merged, _ = apply_timeline_transaction(
        redone,
        _transaction(
            redone,
            "merge",
            _command("merge_clips", left_clip_id="clip-main", right_clip_id="clip-right"),
        ),
    )
    assert _observations(merged.snapshot) == expected and len(merged.snapshot.clips) == 1


@pytest.mark.parametrize("rate", [12, 24, 48, "vfr"])
def test_source_edit_undo_branch_preserves_media_and_invalidates_old_redo(rate: int | str) -> None:
    initial = TimelineHistoryState.initialize(_snapshot(rate))
    expected = _observations(initial.snapshot)
    split, receipt = apply_timeline_transaction(
        initial,
        _transaction(
            initial,
            "split",
            _command(
                "split_clip", clip_id="clip-main", at_offset_frames=6, right_clip_id="clip-right"
            ),
        ),
    )
    undone, undo_receipt = apply_timeline_transaction(
        split, _transaction(split, "undo", _command("undo", history_cursor=receipt.history_cursor))
    )
    branched, _ = apply_timeline_transaction(
        undone,
        _transaction(
            undone,
            "branch",
            _command("set_opacity_blend", clip_id="clip-main", opacity_bp=9000, blend="normal"),
        ),
    )
    assert _observations(branched.snapshot) == expected
    with pytest.raises(TimelineHistoryError, match="history_branch_invalid"):
        apply_timeline_transaction(
            branched,
            _transaction(
                branched, "old-redo", _command("redo", history_cursor=undo_receipt.history_cursor)
            ),
        )


@pytest.mark.parametrize("rate", [12, 24, 48, "vfr"])
def test_unavailable_source_shift_refuses_entire_transaction_without_history_advance(
    rate: int | str,
) -> None:
    initial = TimelineHistoryState.initialize(_snapshot(rate))
    before = initial.snapshot.to_wire()
    with pytest.raises(
        (TimelineCommandError, TimelineHistoryError), match="source_range_unavailable"
    ):
        apply_timeline_transaction(
            initial,
            _transaction(
                initial,
                "reject",
                _command("set_opacity_blend", clip_id="clip-main", opacity_bp=9000, blend="normal"),
                _command("slip_clip", clip_id="clip-main", delta_frames=2),
            ),
        )
    assert initial.snapshot.to_wire() == before
    assert initial.undo_entries == initial.redo_entries == ()


def test_unrepresentable_low_rate_split_refuses_atomically() -> None:
    initial = TimelineHistoryState.initialize(_snapshot(12))
    before = initial.snapshot.to_wire()
    with pytest.raises(
        (TimelineCommandError, TimelineHistoryError),
        match="source_range_unavailable|invalid_timing",
    ):
        apply_timeline_transaction(
            initial,
            _transaction(
                initial,
                "fractional-split",
                _command(
                    "split_clip",
                    clip_id="clip-main",
                    at_offset_frames=1,
                    right_clip_id="clip-right",
                ),
            ),
        )
    assert initial.snapshot.to_wire() == before
    assert initial.undo_entries == initial.redo_entries == ()
