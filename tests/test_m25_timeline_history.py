from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from comfyui_h3_context.core.composition_contract import (
    INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
    NLE_OPERATION_IDS,
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
    resolve_composition,
)
from comfyui_h3_context.core.timeline_authoring import (
    NleCommand,
    NleCommandKind,
    TimelineCommandError,
    decode_nle_command,
)
from comfyui_h3_context.core.timeline_authoring import apply_nle_command as _apply_nle_command
from comfyui_h3_context.core.timeline_history import (
    MAX_HISTORY_BYTES,
    MAX_UNDO_ENTRIES,
    TIMELINE_HISTORY_CURSOR_SCHEMA,
    TIMELINE_RECEIPT_SCHEMA,
    TIMELINE_TRANSACTION_SCHEMA,
    TimelineHistoryError,
    TimelineHistoryState,
    TimelineReceipt,
    apply_timeline_transaction,
    decode_timeline_transaction,
    release_timeline_history,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"


class _V1Transition(Protocol):
    snapshot: PublicCompositionSnapshot
    selection: tuple[str, ...]
    affected_ids: tuple[str, ...]
    semantic_mutation: bool


def apply_nle_command(
    snapshot: PublicCompositionSnapshot,
    command: NleCommand,
    *,
    selection: tuple[str, ...] = (),
) -> _V1Transition:
    result = _apply_nle_command(snapshot, command, selection=selection)
    # IMPORTANT: these fixtures exercise V1 only; refine at the test boundary so the exact
    # native-render-qualified implementation bytes do not need a typing-only rewrite.
    assert isinstance(result.snapshot, PublicCompositionSnapshot)
    return cast(_V1Transition, result)


def fixture_wire() -> dict[str, Any]:
    document = cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
    return cast(dict[str, Any], copy.deepcopy(document["snapshot"]))


def resign(wire: dict[str, Any]) -> PublicCompositionSnapshot:
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def fixture_snapshot() -> PublicCompositionSnapshot:
    return decode_public_snapshot(fixture_wire())


def fixture_snapshot_with_edit_coverage() -> PublicCompositionSnapshot:
    wire = fixture_wire()
    # Command fixtures declare the exact synthetic CFR rows they edit. Sparse resolver examples
    # do not authorize missing source-start PTS or an undeclared tail interval.
    for asset in wire["assets"]:
        if asset["kind"] != "video":
            continue
        count = asset["source_frame_count"]
        indices = list(range(min(count, 63)))
        if count > 63:
            indices.append(count - 1)
        asset["landmarks"] = [
            {"frame_index": frame, "pts": frame * 512, "dts": frame * 512, "duration_ticks": 512}
            for frame in indices
        ]
    return resign(wire)


@pytest.mark.parametrize("disposition", ["present_bound", "absent"])
def test_measured_source_role_moves_undo_redo_preserve_assets_and_old_receipts(
    disposition: str,
) -> None:
    wire = fixture_wire()
    wire["clips"] = [wire["clips"][0]]
    wire["assets"][0]["embedded_audio"] = disposition
    wire["assets"][0]["source_sample_count"] = 144_000 if disposition == "present_bound" else None
    snapshot = resign(wire)
    original_assets = snapshot.assets
    state = TimelineHistoryState.initialize(snapshot)

    def apply(kind: str, payload: dict[str, object], request: str) -> object:
        nonlocal state
        state, receipt = apply_timeline_transaction(
            state,
            decode_timeline_transaction(
                transaction_wire(
                    state.snapshot,
                    request_id=request,
                    commands=[{"kind": kind, "payload": payload}],
                )
            ),
        )
        assert state.snapshot.assets == original_assets
        return receipt

    moved = apply(
        "move_clip",
        {"clip_id": "clip-main", "delta_frames": 0, "target_track_id": "track-video"},
        "move-overlay",
    )
    assert state.snapshot.clips[0].track_id == "track-video"
    assert resolve_composition(state.snapshot, 12).audio_span is None
    assert isinstance(moved, TimelineReceipt)
    saved_receipt = copy.deepcopy(moved.to_wire())
    undone = apply("undo", {"history_cursor": moved.history_cursor}, "undo-overlay")
    assert state.snapshot.clips[0].track_id == "track-primary"
    assert (resolve_composition(state.snapshot, 12).audio_span is not None) == (
        disposition == "present_bound"
    )
    assert isinstance(undone, TimelineReceipt)
    apply("redo", {"history_cursor": undone.history_cursor}, "redo-overlay")
    assert state.snapshot.clips[0].track_id == "track-video"
    assert resolve_composition(state.snapshot, 12).audio_span is None
    assert moved.to_wire() == saved_receipt


def command(command_kind: str, **payload: object) -> NleCommand:
    return decode_nle_command({"kind": command_kind, "payload": payload})


def transaction_wire(
    snapshot: PublicCompositionSnapshot,
    *,
    request_id: str,
    commands: list[dict[str, object]],
    transaction_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema": TIMELINE_TRANSACTION_SCHEMA,
        "request_id": request_id,
        "transaction_id": transaction_id or f"tx-{request_id}",
        "workspace_handle": snapshot.workspace_handle,
        "expected_workspace_revision": snapshot.workspace_revision,
        "expected_timeline_revision": snapshot.timeline_revision,
        "expected_timeline_fingerprint": snapshot.timeline_fingerprint,
        "commands": commands,
    }


def history_rejection_code(callable_: Callable[[], object]) -> str:
    try:
        callable_()
    except TimelineHistoryError as exc:
        return exc.code
    except TimelineCommandError as exc:
        return exc.code
    raise AssertionError("expected a timeline history or command rejection")


def clip_wire(
    *,
    clip_id: str,
    asset_id: str | None,
    track_id: str,
    start_frame: int,
    duration_frames: int,
    source_start_frame: int = 0,
    text: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "clip_id": clip_id,
        "asset_id": asset_id,
        "track_id": track_id,
        "start_frame": start_frame,
        "duration_frames": duration_frames,
        "source_start_frame": source_start_frame,
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
        "text": text,
        "transition": {"kind": "none", "duration_frames": 0},
        "effect": {
            "kind": "none",
            "brightness_permille": 0,
            "contrast_permille": 1000,
            "saturation_permille": 1000,
        },
    }


def rejection_code(callable_: Callable[[], object]) -> str:
    with pytest.raises(TimelineCommandError) as exc_info:
        callable_()
    return exc_info.value.code


def test_command_codec_has_exact_m25_10_membership_and_closed_payloads() -> None:
    assert tuple(member.value for member in NleCommandKind) == NLE_OPERATION_IDS
    command = decode_nle_command({"kind": "select_clips", "payload": {"clip_ids": ["clip-main"]}})
    assert command.to_wire() == {
        "kind": "select_clips",
        "payload": {"clip_ids": ["clip-main"]},
    }
    assert (
        rejection_code(
            lambda: decode_nle_command(
                {
                    "kind": "select_clips",
                    "payload": {"clip_ids": [], "unknown": True},
                }
            )
        )
        == "invalid_command"
    )
    grouped = decode_nle_command(
        {
            "kind": "move_group",
            "payload": {
                "clip_ids": ["clip-title", "clip-image"],
                "delta_frames": 1,
                "target_track_ids": ["track-text", "track-image"],
            },
        }
    )
    assert grouped.payload["clip_ids"] == ["clip-image", "clip-title"]
    assert grouped.payload["target_track_ids"] == ["track-image", "track-text"]
    assert rejection_code(lambda: NleCommand(NleCommandKind.SELECT_CLIPS, {})) == "invalid_command"


@pytest.mark.parametrize(
    "kind",
    (
        "create_audio_track",
        "insert_audio",
        "replace_audio",
        "link_audio",
        "set_gain",
        "set_pan",
        "set_mute",
        "set_solo",
        "set_envelope",
        "set_waveform",
        "mix_audio",
        f"{INDEPENDENT_AUDIO_COMMAND_NAMESPACE}.forged",
    ),
)
def test_independent_audio_commands_reject_before_command_allocation(kind: str) -> None:
    assert rejection_code(lambda: decode_nle_command({"kind": kind, "payload": {}})) == (
        "audio_editing_deferred"
    )


def test_history_module_does_not_define_a_second_geometry_model() -> None:
    source = (
        Path(__file__).parents[1] / "comfyui_h3_context" / "core" / "timeline_history.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "class CompositionTrack",
        "class CompositionClip",
        "class Transform2D",
        "class Crop",
        "class TextStyle",
        "class Transition",
        "class Effect",
    )
    assert all(member not in source for member in forbidden)


def test_track_commands_are_canonical_locked_and_atomic() -> None:
    snapshot = fixture_snapshot()
    created = apply_nle_command(
        snapshot,
        command("create_track", track_id="track-image-2", kind="image_overlay", order=2),
    )
    assert [(track.track_id, track.order) for track in created.snapshot.tracks] == [
        ("track-primary", 0),
        ("track-video", 1),
        ("track-image-2", 2),
        ("track-image", 3),
        ("track-text", 4),
    ]
    reordered = apply_nle_command(
        created.snapshot,
        command("reorder_track", track_id="track-image-2", order=4),
    )
    assert [track.track_id for track in reordered.snapshot.tracks] == [
        "track-primary",
        "track-video",
        "track-image",
        "track-text",
        "track-image-2",
    ]
    locked = apply_nle_command(
        reordered.snapshot,
        command("set_track_locked", track_id="track-image-2", locked=True),
    )
    assert locked.snapshot.tracks[-1].locked is True
    before = locked.snapshot.public_fingerprint
    assert (
        rejection_code(
            lambda: apply_nle_command(
                locked.snapshot,
                command("remove_track", track_id="track-image-2"),
            )
        )
        == "invalid_command"
    )
    assert locked.snapshot.public_fingerprint == before


def test_insert_replace_remove_and_selection_use_accepted_snapshot() -> None:
    snapshot = fixture_snapshot()
    inserted = apply_nle_command(
        snapshot,
        command(
            "insert_asset_clip",
            clip=clip_wire(
                clip_id="clip-image-2",
                asset_id="img-overlay",
                track_id="track-image",
                start_frame=30,
                duration_frames=10,
            ),
        ),
    )
    assert "clip-image-2" in {clip.clip_id for clip in inserted.snapshot.clips}
    assert inserted.snapshot.timeline_fingerprint != snapshot.timeline_fingerprint
    replaced = apply_nle_command(
        inserted.snapshot,
        command(
            "replace_clip_asset",
            clip_id="clip-image-2",
            asset_id="img-overlay",
            source_start_frame=0,
        ),
    )
    assert (
        next(clip for clip in replaced.snapshot.clips if clip.clip_id == "clip-image-2").asset_id
        == "img-overlay"
    )
    selected = apply_nle_command(
        replaced.snapshot,
        command("select_clips", clip_ids=["clip-image-2", "clip-main", "clip-image-2"]),
    )
    assert selected.selection == ("clip-image-2", "clip-main")
    assert selected.snapshot.public_fingerprint == replaced.snapshot.public_fingerprint
    removed = apply_nle_command(
        replaced.snapshot,
        command("remove_clip", clip_id="clip-image-2"),
    )
    assert "clip-image-2" not in {clip.clip_id for clip in removed.snapshot.clips}


@pytest.mark.parametrize(
    ("kind", "extra_payload"),
    (
        ("insert_asset_clip", {}),
        ("insert_range", {"scope_track_ids": ["track-image"]}),
    ),
)
def test_nested_clip_geometry_rejects_before_internal_sort_or_extent_access(
    kind: str,
    extra_payload: dict[str, object],
) -> None:
    snapshot = fixture_snapshot()
    malformed_clip = {
        "clip_id": "clip-malformed",
        "track_id": "track-image",
    }

    assert (
        rejection_code(
            lambda: apply_nle_command(
                snapshot,
                command(kind, clip=malformed_clip, **extra_payload),
            )
        )
        == "invalid_command"
    )
    assert snapshot == fixture_snapshot()


@pytest.mark.parametrize(
    ("kind", "extra_payload"),
    (
        ("insert_asset_clip", {}),
        ("insert_range", {"scope_track_ids": ["track-video"]}),
    ),
)
@pytest.mark.parametrize(
    ("member", "value", "remove"),
    (
        ("asset_id", None, True),
        ("asset_id", {}, False),
        ("source_start_frame", "0", False),
    ),
)
def test_nested_video_clip_fields_reject_before_source_range_access(
    kind: str,
    extra_payload: dict[str, object],
    member: str,
    value: object,
    remove: bool,
) -> None:
    snapshot = fixture_snapshot()
    malformed_clip = clip_wire(
        clip_id="clip-malformed-video",
        track_id="track-video",
        asset_id="asset-video",
        start_frame=60,
        duration_frames=1,
    )
    if remove:
        del malformed_clip[member]
    else:
        malformed_clip[member] = value

    assert (
        rejection_code(
            lambda: apply_nle_command(
                snapshot,
                command(kind, clip=malformed_clip, **extra_payload),
            )
        )
        == "invalid_contract"
    )
    assert snapshot == fixture_snapshot()


def test_range_deletion_prunes_selection_of_removed_clips() -> None:
    initial = TimelineHistoryState.initialize(fixture_snapshot())
    selected, _ = apply_timeline_transaction(
        initial,
        transaction_wire(
            initial.snapshot,
            request_id="request-select-before-range-delete",
            commands=[
                {
                    "kind": "select_clips",
                    "payload": {"clip_ids": ["clip-video-overlay"]},
                }
            ],
        ),
    )
    deleted, receipt = apply_timeline_transaction(
        selected,
        transaction_wire(
            selected.snapshot,
            request_id="request-delete-selected-range",
            commands=[
                {
                    "kind": "ripple_delete",
                    "payload": {
                        "start_frame": 12,
                        "duration_frames": 12,
                        "scope_track_ids": ["track-video"],
                        "remainder_ids": {},
                    },
                }
            ],
        ),
    )

    assert "clip-video-overlay" not in {clip.clip_id for clip in deleted.snapshot.clips}
    assert deleted.selection == ()
    assert receipt.selection == ()


def test_insert_title_and_remove_empty_track_have_success_paths() -> None:
    snapshot = fixture_snapshot()
    text = cast(dict[str, object], copy.deepcopy(fixture_wire()["clips"][3]["text"]))
    inserted = apply_nle_command(
        snapshot,
        command(
            "insert_title_clip",
            clip=clip_wire(
                clip_id="clip-title-2",
                asset_id=None,
                track_id="track-text",
                start_frame=30,
                duration_frames=10,
                text=text,
            ),
        ),
    )
    assert (
        next(clip for clip in inserted.snapshot.clips if clip.clip_id == "clip-title-2").text
        is not None
    )
    created = apply_nle_command(
        snapshot,
        command("create_track", track_id="track-empty", kind="image_overlay", order=4),
    )
    removed = apply_nle_command(
        created.snapshot,
        command("remove_track", track_id="track-empty"),
    )
    assert "track-empty" not in {track.track_id for track in removed.snapshot.tracks}


def test_primary_enabled_state_recomputes_the_derived_audio_owner() -> None:
    wire = fixture_wire()
    wire["clips"] = [wire["clips"][0]]
    snapshot = resign(wire)
    snapshot = apply_nle_command(
        snapshot,
        command("set_track_enabled", track_id="track-primary", enabled=True),
    ).snapshot
    assert resolve_composition(snapshot, 12).audio_span is not None
    disabled = apply_nle_command(
        snapshot,
        command("set_track_enabled", track_id="track-primary", enabled=False),
    )
    assert resolve_composition(disabled.snapshot, 12).audio_span is None


def test_primary_video_geometry_source_and_range_edits_keep_audio_derived() -> None:
    wire = cast(dict[str, Any], fixture_snapshot_with_edit_coverage().to_wire())
    wire["clips"] = [wire["clips"][0]]
    snapshot = resign(wire)
    snapshot = apply_nle_command(
        snapshot,
        command("set_track_enabled", track_id="track-primary", enabled=True),
    ).snapshot
    original_frame_0 = resolve_composition(snapshot, 0).audio_span
    original_frame_12 = resolve_composition(snapshot, 12).audio_span
    assert original_frame_0 is not None and original_frame_12 is not None

    trimmed = apply_nle_command(
        snapshot,
        command("trim_clip", clip_id="clip-main", edge="end", delta_frames=-1),
    ).snapshot
    slipped = apply_nle_command(
        trimmed,
        command("slip_clip", clip_id="clip-main", delta_frames=12),
    ).snapshot
    slipped_audio = resolve_composition(slipped, 12).audio_span
    assert slipped_audio is not None
    assert slipped_audio.source_start_sample > original_frame_12.source_start_sample

    moved = apply_nle_command(
        trimmed,
        command(
            "move_clip",
            clip_id="clip-main",
            delta_frames=1,
            target_track_id="track-primary",
        ),
    ).snapshot
    moved_audio = resolve_composition(moved, 1).audio_span
    assert resolve_composition(moved, 0).audio_span is None
    assert moved_audio is not None
    assert moved_audio.source_start_sample == original_frame_0.source_start_sample

    split = apply_nle_command(
        snapshot,
        command(
            "split_clip",
            clip_id="clip-main",
            at_offset_frames=12,
            right_clip_id="clip-main-right",
        ),
    ).snapshot
    right_audio = resolve_composition(split, 30).audio_span
    assert right_audio is not None and right_audio.clip_id == "clip-main-right"
    merged = apply_nle_command(
        split,
        command("merge_clips", left_clip_id="clip-main", right_clip_id="clip-main-right"),
    ).snapshot
    assert merged.timeline_fingerprint == snapshot.timeline_fingerprint

    rippled = apply_nle_command(
        snapshot,
        command(
            "ripple_delete",
            start_frame=12,
            duration_frames=35,
            scope_track_ids=["track-primary"],
            remainder_ids={"clip-main": "clip-main-after-ripple"},
        ),
    ).snapshot
    ripple_audio = resolve_composition(rippled, 12).audio_span
    assert ripple_audio is not None and ripple_audio.clip_id == "clip-main-after-ripple"
    assert ripple_audio.source_start_sample > original_frame_12.source_start_sample

    disabled = apply_nle_command(
        snapshot,
        command("set_clip_enabled", clip_id="clip-main", enabled=False),
    ).snapshot
    removed = apply_nle_command(
        snapshot,
        command("remove_clip", clip_id="clip-main"),
    ).snapshot
    assert resolve_composition(disabled, 12).audio_span is None
    assert resolve_composition(removed, 12).audio_span is None


def test_move_group_trim_split_merge_and_slip_are_integer_frame_exact() -> None:
    snapshot = fixture_snapshot_with_edit_coverage()
    moved = apply_nle_command(
        snapshot,
        command(
            "move_clip",
            clip_id="clip-video-overlay",
            delta_frames=2,
            target_track_id="track-video",
        ),
    )
    assert (
        next(
            clip for clip in moved.snapshot.clips if clip.clip_id == "clip-video-overlay"
        ).start_frame
        == 14
    )
    grouped = apply_nle_command(
        moved.snapshot,
        command(
            "move_group",
            clip_ids=["clip-video-overlay", "clip-title"],
            delta_frames=1,
            target_track_ids=["track-video", "track-text"],
        ),
    )
    grouped_by_id = {clip.clip_id: clip for clip in grouped.snapshot.clips}
    assert grouped_by_id["clip-video-overlay"].start_frame == 15
    assert grouped_by_id["clip-title"].start_frame == 7
    trimmed = apply_nle_command(
        grouped.snapshot,
        command("trim_clip", clip_id="clip-video-overlay", edge="end", delta_frames=-2),
    )
    assert (
        next(
            clip for clip in trimmed.snapshot.clips if clip.clip_id == "clip-video-overlay"
        ).duration_frames
        == 10
    )
    slipped = apply_nle_command(
        trimmed.snapshot,
        command("slip_clip", clip_id="clip-video-overlay", delta_frames=1),
    )
    assert (
        next(
            clip for clip in slipped.snapshot.clips if clip.clip_id == "clip-video-overlay"
        ).source_start_frame
        == 1
    )

    split = apply_nle_command(
        snapshot,
        command(
            "split_clip",
            clip_id="clip-main",
            at_offset_frames=24,
            right_clip_id="clip-main-right",
        ),
    )
    split_by_id = {clip.clip_id: clip for clip in split.snapshot.clips}
    assert (
        split_by_id["clip-main"].duration_frames,
        split_by_id["clip-main-right"].start_frame,
    ) == (
        24,
        24,
    )
    merged = apply_nle_command(
        split.snapshot,
        command("merge_clips", left_clip_id="clip-main", right_clip_id="clip-main-right"),
    )
    assert (
        next(clip for clip in merged.snapshot.clips if clip.clip_id == "clip-main").duration_frames
        == 48
    )


def test_move_clip_allows_track_only_motion_but_rejects_an_exact_noop() -> None:
    snapshot = apply_nle_command(
        fixture_snapshot(),
        command("create_track", track_id="track-video-2", kind="video_overlay", order=2),
    ).snapshot

    moved = apply_nle_command(
        snapshot,
        command(
            "move_clip",
            clip_id="clip-video-overlay",
            delta_frames=0,
            target_track_id="track-video-2",
        ),
    )
    clip = next(item for item in moved.snapshot.clips if item.clip_id == "clip-video-overlay")
    assert (clip.track_id, clip.start_frame) == ("track-video-2", 12)

    grouped = apply_nle_command(
        moved.snapshot,
        command(
            "move_group",
            clip_ids=["clip-video-overlay"],
            delta_frames=0,
            target_track_ids=["track-video"],
        ),
    )
    group_clip = next(
        item for item in grouped.snapshot.clips if item.clip_id == "clip-video-overlay"
    )
    assert (group_clip.track_id, group_clip.start_frame) == ("track-video", 12)

    assert (
        rejection_code(
            lambda: apply_nle_command(
                grouped.snapshot,
                command(
                    "move_clip",
                    clip_id="clip-video-overlay",
                    delta_frames=0,
                    target_track_id="track-video",
                ),
            )
        )
        == "invalid_command"
    )


def test_visual_text_transition_and_effect_commands_use_m25_10_validation() -> None:
    snapshot = fixture_snapshot()
    updated = apply_nle_command(
        snapshot,
        command("set_clip_enabled", clip_id="clip-image", enabled=False),
    ).snapshot
    updated = apply_nle_command(
        updated,
        command(
            "set_visual_transform",
            clip_id="clip-image",
            transform={
                "anchor_x_bp": 4000,
                "anchor_y_bp": 6000,
                "position_x_bp": 100,
                "position_y_bp": -100,
                "scale_x_bp": 9000,
                "scale_y_bp": 11000,
                "rotation_mdeg": 2500,
            },
        ),
    ).snapshot
    updated = apply_nle_command(
        updated,
        command(
            "set_crop",
            clip_id="clip-image",
            crop={"left_bp": 100, "top_bp": 200, "right_bp": 300, "bottom_bp": 400},
        ),
    ).snapshot
    updated = apply_nle_command(
        updated,
        command("set_opacity_blend", clip_id="clip-image", opacity_bp=7000, blend="screen"),
    ).snapshot
    updated = apply_nle_command(
        updated,
        command("set_text_content", clip_id="clip-title", content="Updated title"),
    ).snapshot
    updated = apply_nle_command(
        updated,
        command(
            "set_text_style",
            clip_id="clip-title",
            style={
                "font_asset_id": "font-main",
                "size_px": 48,
                "weight": 400,
                "style": "italic",
                "align": "left",
                "line_height_bp": 13000,
                "fill_rgba": [1, 2, 3, 255],
                "background_rgba": None,
            },
        ),
    ).snapshot
    updated = apply_nle_command(
        updated,
        command(
            "set_transition",
            clip_id="clip-video-overlay",
            transition={"kind": "none", "duration_frames": 0},
        ),
    ).snapshot
    updated = apply_nle_command(
        updated,
        command(
            "set_effect",
            clip_id="clip-image",
            effect={
                "kind": "color_adjust_v1",
                "brightness_permille": 20,
                "contrast_permille": 900,
                "saturation_permille": 1100,
            },
        ),
    ).snapshot
    by_id = {clip.clip_id: clip for clip in updated.clips}
    assert by_id["clip-image"].enabled is False
    assert by_id["clip-image"].transform.position_x_bp == 100
    assert by_id["clip-image"].crop.bottom_bp == 400
    assert (by_id["clip-image"].opacity_bp, by_id["clip-image"].blend) == (7000, "screen")
    assert by_id["clip-image"].effect.kind == "color_adjust_v1"
    assert by_id["clip-title"].text is not None
    assert (by_id["clip-title"].text.content, by_id["clip-title"].text.style) == (
        "Updated title",
        "italic",
    )
    assert by_id["clip-video-overlay"].transition.kind == "none"


def test_insert_overwrite_and_ripple_ranges_are_atomic_over_declared_scope() -> None:
    wire = fixture_wire()
    primary = wire["clips"][0]
    title_text = cast(dict[str, object], copy.deepcopy(wire["clips"][3]["text"]))
    wire["clips"] = [
        primary,
        clip_wire(
            clip_id="title-a",
            asset_id=None,
            track_id="track-text",
            start_frame=5,
            duration_frames=5,
            text=title_text,
        ),
        clip_wire(
            clip_id="title-b",
            asset_id=None,
            track_id="track-text",
            start_frame=20,
            duration_frames=5,
            text=title_text,
        ),
    ]
    snapshot = resign(wire)
    inserted = apply_nle_command(
        snapshot,
        command(
            "insert_range",
            clip=clip_wire(
                clip_id="title-insert",
                asset_id=None,
                track_id="track-text",
                start_frame=10,
                duration_frames=4,
                text=title_text,
            ),
            scope_track_ids=["track-text"],
        ),
    )
    assert {clip.clip_id: clip.start_frame for clip in inserted.snapshot.clips}["title-b"] == 24
    rippled = apply_nle_command(
        inserted.snapshot,
        command(
            "ripple_delete",
            start_frame=10,
            duration_frames=4,
            scope_track_ids=["track-text"],
            remainder_ids={},
        ),
    )
    ripple_by_id = {clip.clip_id: clip for clip in rippled.snapshot.clips}
    assert "title-insert" not in ripple_by_id
    assert ripple_by_id["title-b"].start_frame == 20

    wire = fixture_wire()
    title_text = cast(dict[str, object], copy.deepcopy(wire["clips"][3]["text"]))
    wire["clips"] = [
        wire["clips"][0],
        clip_wire(
            clip_id="title-span",
            asset_id=None,
            track_id="track-text",
            start_frame=5,
            duration_frames=15,
            text=title_text,
        ),
    ]
    overwritten = apply_nle_command(
        resign(wire),
        command(
            "overwrite_range",
            clip=clip_wire(
                clip_id="title-overwrite",
                asset_id=None,
                track_id="track-text",
                start_frame=8,
                duration_frames=6,
                text=title_text,
            ),
            start_frame=8,
            duration_frames=6,
            scope_track_ids=["track-text"],
            remainder_ids={"title-span": "title-span-right"},
        ),
    )
    overwrite_by_id = {clip.clip_id: clip for clip in overwritten.snapshot.clips}
    assert (
        overwrite_by_id["title-span"].duration_frames,
        overwrite_by_id["title-overwrite"].start_frame,
        overwrite_by_id["title-span-right"].start_frame,
        overwrite_by_id["title-span-right"].duration_frames,
    ) == (3, 8, 14, 6)


def test_source_and_output_bounds_reject_without_mutating_the_snapshot() -> None:
    snapshot = fixture_snapshot()
    fingerprint = snapshot.public_fingerprint
    assert (
        rejection_code(
            lambda: apply_nle_command(
                snapshot,
                command("slip_clip", clip_id="clip-video-overlay", delta_frames=13),
            )
        )
        == "source_range_unavailable"
    )
    assert (
        rejection_code(
            lambda: apply_nle_command(
                snapshot,
                command(
                    "move_clip",
                    clip_id="clip-image",
                    delta_frames=1,
                    target_track_id="track-image",
                ),
            )
        )
        == "source_range_unavailable"
    )
    assert snapshot.public_fingerprint == fingerprint


def test_roll_slide_and_ripple_trim_preserve_outer_extent() -> None:
    wire = cast(dict[str, Any], fixture_snapshot_with_edit_coverage().to_wire())
    primary = cast(dict[str, object], wire["clips"][0])
    left = copy.deepcopy(primary)
    left.update({"clip_id": "primary-left", "duration_frames": 24})
    right = copy.deepcopy(primary)
    right.update(
        {
            "clip_id": "primary-right",
            "start_frame": 24,
            "duration_frames": 24,
            "source_start_frame": 24,
        }
    )
    wire["clips"] = [left, right]
    snapshot = resign(wire)
    rolled = apply_nle_command(
        snapshot,
        command(
            "roll_edit",
            left_clip_id="primary-left",
            right_clip_id="primary-right",
            delta_frames=2,
        ),
    )
    rolled_by_id = {clip.clip_id: clip for clip in rolled.snapshot.clips}
    assert (
        rolled_by_id["primary-left"].duration_frames,
        rolled_by_id["primary-right"].start_frame,
        rolled_by_id["primary-right"].duration_frames,
    ) == (26, 26, 22)
    roll_left_audio = resolve_composition(rolled.snapshot, 25).audio_span
    roll_right_scene = resolve_composition(rolled.snapshot, 26)
    assert roll_left_audio is not None and roll_right_scene.audio_span is not None
    assert roll_right_scene.audio_span.clip_id == "primary-right"
    assert roll_right_scene.audio_span.source_start_sample == 52_000
    assert roll_right_scene.blockers == ()

    wire = fixture_wire()
    primary = wire["clips"][0]
    wire["clips"] = [
        primary,
        clip_wire(
            clip_id="image-left",
            asset_id="img-overlay",
            track_id="track-image",
            start_frame=0,
            duration_frames=10,
        ),
        clip_wire(
            clip_id="image-middle",
            asset_id="img-overlay",
            track_id="track-image",
            start_frame=10,
            duration_frames=10,
        ),
        clip_wire(
            clip_id="image-right",
            asset_id="img-overlay",
            track_id="track-image",
            start_frame=20,
            duration_frames=10,
        ),
    ]
    snapshot = resign(wire)
    slid = apply_nle_command(
        snapshot,
        command(
            "slide_clip",
            clip_id="image-middle",
            left_clip_id="image-left",
            right_clip_id="image-right",
            delta_frames=2,
        ),
    )
    slid_by_id = {clip.clip_id: clip for clip in slid.snapshot.clips}
    assert (
        slid_by_id["image-left"].duration_frames,
        slid_by_id["image-middle"].start_frame,
        slid_by_id["image-right"].start_frame,
        slid_by_id["image-right"].duration_frames,
    ) == (12, 12, 22, 8)
    trimmed = apply_nle_command(
        snapshot,
        command(
            "ripple_trim",
            clip_id="image-left",
            edge="end",
            delta_frames=-2,
            scope_track_ids=["track-image"],
        ),
    )
    trimmed_by_id = {clip.clip_id: clip for clip in trimmed.snapshot.clips}
    assert (
        trimmed_by_id["image-left"].duration_frames,
        trimmed_by_id["image-middle"].start_frame,
    ) == (
        8,
        8,
    )
    start_trimmed = apply_nle_command(
        snapshot,
        command(
            "ripple_trim",
            clip_id="image-middle",
            edge="start",
            delta_frames=2,
            scope_track_ids=["track-image"],
        ),
    )
    start_trimmed_by_id = {clip.clip_id: clip for clip in start_trimmed.snapshot.clips}
    assert (
        start_trimmed_by_id["image-middle"].start_frame,
        start_trimmed_by_id["image-middle"].duration_frames,
        start_trimmed_by_id["image-right"].start_frame,
    ) == (10, 8, 18)


def test_transaction_decoder_is_closed_bounded_and_rejects_audio() -> None:
    snapshot = fixture_snapshot()
    wire = transaction_wire(
        snapshot,
        request_id="request-codec",
        commands=[{"kind": "select_clips", "payload": {"clip_ids": ["clip-main"]}}],
    )
    decoded = decode_timeline_transaction(wire)
    assert decoded.to_wire() == wire
    assert (
        history_rejection_code(lambda: decode_timeline_transaction({**wire, "unknown": True}))
        == "invalid_transaction"
    )
    assert (
        history_rejection_code(lambda: decode_timeline_transaction({**wire, "commands": []}))
        == "invalid_transaction"
    )
    assert (
        history_rejection_code(
            lambda: decode_timeline_transaction({**wire, "request_id": "not valid"})
        )
        == "invalid_transaction"
    )
    assert (
        history_rejection_code(
            lambda: decode_timeline_transaction(
                {
                    **wire,
                    "commands": [{"kind": "set_gain", "payload": {}}],
                }
            )
        )
        == "audio_editing_deferred"
    )
    malformed_history_cursor = ":".join(
        (TIMELINE_HISTORY_CURSOR_SCHEMA, "not-a-sequence", "not-a-digest")
    )
    malformed_cursor = transaction_wire(
        snapshot,
        request_id="request-malformed-history-cursor",
        commands=[
            {
                "kind": "undo",
                "payload": {"history_cursor": malformed_history_cursor},
            }
        ],
    )
    assert (
        history_rejection_code(lambda: decode_timeline_transaction(malformed_cursor))
        == "history_cursor_invalid"
    )
    overflow_history_cursor = f"{TIMELINE_HISTORY_CURSOR_SCHEMA}:1000001:{'a' * 64}"
    overflow_cursor = copy.deepcopy(malformed_cursor)
    overflow_cursor["commands"] = [
        {
            "kind": "undo",
            "payload": {"history_cursor": overflow_history_cursor},
        }
    ]
    assert (
        history_rejection_code(lambda: decode_timeline_transaction(overflow_cursor))
        == "history_cursor_invalid"
    )
    too_much_work: list[dict[str, object]] = [
        {
            "kind": "select_clips",
            "payload": {"clip_ids": [f"clip-{index:03d}" for index in range(128)]},
        }
        for _ in range(32)
    ]
    assert (
        history_rejection_code(
            lambda: decode_timeline_transaction(
                transaction_wire(
                    snapshot,
                    request_id="request-work-limit",
                    commands=too_much_work,
                )
            )
        )
        == "resource_limit"
    )
    large_ids = [f"clip-{index:03d}-" + ("x" * 60) for index in range(128)]
    too_many_bytes: list[dict[str, object]] = [
        {"kind": "select_clips", "payload": {"clip_ids": large_ids}} for _ in range(32)
    ]
    assert (
        history_rejection_code(
            lambda: decode_timeline_transaction(
                transaction_wire(
                    snapshot,
                    request_id="request-byte-limit",
                    commands=too_many_bytes,
                )
            )
        )
        == "resource_limit"
    )
    non_rebasable = transaction_wire(
        snapshot,
        request_id="request-non-rebasable",
        commands=[
            {
                "kind": "rebase_transaction",
                "payload": {
                    "base_timeline_fingerprint": snapshot.timeline_fingerprint,
                    "commands": [
                        {
                            "kind": "move_clip",
                            "payload": {
                                "clip_id": "clip-image",
                                "delta_frames": 1,
                                "target_track_id": "track-image",
                            },
                        }
                    ],
                },
            }
        ],
    )
    assert (
        history_rejection_code(lambda: decode_timeline_transaction(non_rebasable))
        == "rebase_conflict"
    )


def test_transaction_is_atomic_advances_revisions_once_and_has_exact_receipt() -> None:
    initial = TimelineHistoryState.initialize(fixture_snapshot())
    before = initial.snapshot
    tx = transaction_wire(
        before,
        request_id="request-atomic",
        commands=[
            {
                "kind": "move_clip",
                "payload": {
                    "clip_id": "clip-video-overlay",
                    "delta_frames": 1,
                    "target_track_id": "track-video",
                },
            },
            {
                "kind": "set_opacity_blend",
                "payload": {"clip_id": "clip-image", "opacity_bp": 8000, "blend": "screen"},
            },
        ],
    )
    state, receipt = apply_timeline_transaction(initial, tx)
    assert (state.snapshot.workspace_revision, state.snapshot.timeline_revision) == (
        before.workspace_revision + 1,
        before.timeline_revision + 1,
    )
    assert len(state.undo_entries) == 1
    assert receipt.schema == TIMELINE_RECEIPT_SCHEMA
    assert set(receipt.to_wire()) == {
        "schema",
        "request_id",
        "transaction_id",
        "workspace_handle",
        "before_workspace_revision",
        "after_workspace_revision",
        "before_workspace_fingerprint",
        "after_workspace_fingerprint",
        "before_timeline_revision",
        "after_timeline_revision",
        "before_timeline_fingerprint",
        "after_timeline_fingerprint",
        "commands",
        "affected_ids",
        "inverse",
        "history_cursor",
        "selection",
        "snapshot",
    }
    rejected = transaction_wire(
        state.snapshot,
        request_id="request-atomic-reject",
        commands=[
            {"kind": "set_clip_enabled", "payload": {"clip_id": "clip-image", "enabled": False}},
            {"kind": "remove_clip", "payload": {"clip_id": "missing-clip"}},
        ],
    )
    before_rejection = state
    assert history_rejection_code(lambda: apply_timeline_transaction(state, rejected)) == (
        "invalid_command"
    )
    assert state == before_rejection


def test_request_idempotency_and_stale_cas_are_fail_closed() -> None:
    initial = TimelineHistoryState.initialize(fixture_snapshot())
    tx = transaction_wire(
        initial.snapshot,
        request_id="request-idempotent",
        commands=[{"kind": "select_clips", "payload": {"clip_ids": ["clip-main"]}}],
    )
    state, receipt = apply_timeline_transaction(initial, tx)
    replayed_state, replayed_receipt = apply_timeline_transaction(state, tx)
    assert replayed_state == state
    assert replayed_receipt.to_wire() == receipt.to_wire()
    conflict = copy.deepcopy(tx)
    conflict["commands"] = [{"kind": "select_clips", "payload": {"clip_ids": []}}]
    assert history_rejection_code(lambda: apply_timeline_transaction(state, conflict)) == (
        "idempotency_conflict"
    )
    stale = transaction_wire(
        initial.snapshot,
        request_id="request-stale",
        commands=[{"kind": "select_clips", "payload": {"clip_ids": []}}],
    )
    assert history_rejection_code(lambda: apply_timeline_transaction(state, stale)) == (
        "stale_workspace_revision"
    )
    stale_timeline = transaction_wire(
        state.snapshot,
        request_id="request-stale-timeline",
        commands=[{"kind": "select_clips", "payload": {"clip_ids": []}}],
    )
    stale_timeline["expected_timeline_revision"] = state.snapshot.timeline_revision - 1
    assert (
        history_rejection_code(lambda: apply_timeline_transaction(state, stale_timeline))
        == "stale_timeline_revision"
    )
    stale_fingerprint = transaction_wire(
        state.snapshot,
        request_id="request-stale-fingerprint",
        commands=[{"kind": "select_clips", "payload": {"clip_ids": []}}],
    )
    stale_fingerprint["expected_timeline_fingerprint"] = "sha256:" + ("0" * 64)
    assert (
        history_rejection_code(lambda: apply_timeline_transaction(state, stale_fingerprint))
        == "stale_timeline_fingerprint"
    )


def test_undo_redo_restore_content_selection_and_embedded_audio() -> None:
    wire = fixture_wire()
    wire["clips"] = [wire["clips"][0]]
    initial = TimelineHistoryState.initialize(resign(wire))
    changed, accepted = apply_timeline_transaction(
        initial,
        transaction_wire(
            initial.snapshot,
            request_id="request-disable-primary",
            commands=[
                {
                    "kind": "set_track_enabled",
                    "payload": {"track_id": "track-primary", "enabled": False},
                },
                {"kind": "select_clips", "payload": {"clip_ids": ["clip-main"]}},
            ],
        ),
    )
    assert resolve_composition(changed.snapshot, 12).audio_span is None
    undone, undo_receipt = apply_timeline_transaction(
        changed,
        transaction_wire(
            changed.snapshot,
            request_id="request-undo",
            commands=[{"kind": "undo", "payload": {"history_cursor": accepted.history_cursor}}],
        ),
    )
    assert undone.selection == ()
    assert resolve_composition(undone.snapshot, 12).audio_span is not None
    redone, redo_receipt = apply_timeline_transaction(
        undone,
        transaction_wire(
            undone.snapshot,
            request_id="request-redo",
            commands=[{"kind": "redo", "payload": {"history_cursor": undo_receipt.history_cursor}}],
        ),
    )
    assert redone.selection == ("clip-main",)
    assert resolve_composition(redone.snapshot, 12).audio_span is None
    assert redo_receipt.after_timeline_revision == accepted.after_timeline_revision + 2


def test_new_mutation_after_undo_invalidates_redo_branch() -> None:
    initial = TimelineHistoryState.initialize(fixture_snapshot())
    changed, first = apply_timeline_transaction(
        initial,
        transaction_wire(
            initial.snapshot,
            request_id="request-first",
            commands=[{"kind": "select_clips", "payload": {"clip_ids": ["clip-main"]}}],
        ),
    )
    undone, undo_receipt = apply_timeline_transaction(
        changed,
        transaction_wire(
            changed.snapshot,
            request_id="request-first-undo",
            commands=[{"kind": "undo", "payload": {"history_cursor": first.history_cursor}}],
        ),
    )
    branched, _ = apply_timeline_transaction(
        undone,
        transaction_wire(
            undone.snapshot,
            request_id="request-branch",
            commands=[{"kind": "select_clips", "payload": {"clip_ids": ["clip-image"]}}],
        ),
    )
    redo = transaction_wire(
        branched.snapshot,
        request_id="request-invalid-redo",
        commands=[{"kind": "redo", "payload": {"history_cursor": undo_receipt.history_cursor}}],
    )
    assert history_rejection_code(lambda: apply_timeline_transaction(branched, redo)) == (
        "history_branch_invalid"
    )


def test_undo_requires_the_exact_current_branch_head() -> None:
    state = TimelineHistoryState.initialize(fixture_snapshot())
    state, first = apply_timeline_transaction(
        state,
        transaction_wire(
            state.snapshot,
            request_id="request-head-first",
            commands=[{"kind": "select_clips", "payload": {"clip_ids": ["clip-main"]}}],
        ),
    )
    state, _ = apply_timeline_transaction(
        state,
        transaction_wire(
            state.snapshot,
            request_id="request-head-second",
            commands=[{"kind": "select_clips", "payload": {"clip_ids": ["clip-image"]}}],
        ),
    )
    non_head = transaction_wire(
        state.snapshot,
        request_id="request-non-head-undo",
        commands=[{"kind": "undo", "payload": {"history_cursor": first.history_cursor}}],
    )
    assert history_rejection_code(lambda: apply_timeline_transaction(state, non_head)) == (
        "history_branch_invalid"
    )


@pytest.mark.parametrize(
    "property_command",
    (
        {"kind": "set_clip_enabled", "payload": {"clip_id": "clip-image", "enabled": False}},
        {
            "kind": "set_visual_transform",
            "payload": {
                "clip_id": "clip-image",
                "transform": {
                    "anchor_x_bp": 5000,
                    "anchor_y_bp": 5000,
                    "position_x_bp": 100,
                    "position_y_bp": 200,
                    "scale_x_bp": 10000,
                    "scale_y_bp": 10000,
                    "rotation_mdeg": 0,
                },
            },
        },
        {
            "kind": "set_crop",
            "payload": {
                "clip_id": "clip-image",
                "crop": {"left_bp": 10, "top_bp": 20, "right_bp": 30, "bottom_bp": 40},
            },
        },
        {
            "kind": "set_opacity_blend",
            "payload": {"clip_id": "clip-image", "opacity_bp": 7000, "blend": "screen"},
        },
        {
            "kind": "set_text_content",
            "payload": {"clip_id": "clip-title", "content": "Rebased title"},
        },
        {
            "kind": "set_text_style",
            "payload": {
                "clip_id": "clip-title",
                "style": {
                    "font_asset_id": "font-main",
                    "size_px": 48,
                    "weight": 400,
                    "style": "italic",
                    "align": "left",
                    "line_height_bp": 13000,
                    "fill_rgba": [1, 2, 3, 255],
                    "background_rgba": None,
                },
            },
        },
        {
            "kind": "set_transition",
            "payload": {
                "clip_id": "clip-video-overlay",
                "transition": {"kind": "none", "duration_frames": 0},
            },
        },
        {
            "kind": "set_effect",
            "payload": {
                "clip_id": "clip-image",
                "effect": {
                    "kind": "color_adjust_v1",
                    "brightness_permille": 10,
                    "contrast_permille": 900,
                    "saturation_permille": 1100,
                },
            },
        },
    ),
)
def test_rebase_accepts_each_unchanged_property_path(
    property_command: dict[str, object],
) -> None:
    initial = TimelineHistoryState.initialize(fixture_snapshot())
    base_fingerprint = initial.snapshot.timeline_fingerprint
    current, _ = apply_timeline_transaction(
        initial,
        transaction_wire(
            initial.snapshot,
            request_id="request-property-base-change",
            commands=[
                {
                    "kind": "trim_clip",
                    "payload": {"clip_id": "clip-main", "edge": "end", "delta_frames": -1},
                }
            ],
        ),
    )
    rebased, receipt = apply_timeline_transaction(
        current,
        transaction_wire(
            current.snapshot,
            request_id="request-property-rebase",
            commands=[
                {
                    "kind": "rebase_transaction",
                    "payload": {
                        "base_timeline_fingerprint": base_fingerprint,
                        "commands": [property_command],
                    },
                }
            ],
        ),
    )
    assert rebased.snapshot.timeline_revision == current.snapshot.timeline_revision + 1
    assert receipt.affected_ids


def test_rebase_allows_unchanged_property_path_and_rejects_changed_path() -> None:
    initial = TimelineHistoryState.initialize(fixture_snapshot())
    base_fingerprint = initial.snapshot.timeline_fingerprint
    moved, _ = apply_timeline_transaction(
        initial,
        transaction_wire(
            initial.snapshot,
            request_id="request-trim-before-rebase",
            commands=[
                {
                    "kind": "trim_clip",
                    "payload": {
                        "clip_id": "clip-image",
                        "edge": "end",
                        "delta_frames": -1,
                    },
                }
            ],
        ),
    )
    rebased, _ = apply_timeline_transaction(
        moved,
        transaction_wire(
            moved.snapshot,
            request_id="request-rebase-ok",
            commands=[
                {
                    "kind": "rebase_transaction",
                    "payload": {
                        "base_timeline_fingerprint": base_fingerprint,
                        "commands": [
                            {
                                "kind": "set_opacity_blend",
                                "payload": {
                                    "clip_id": "clip-image",
                                    "opacity_bp": 7000,
                                    "blend": "screen",
                                },
                            }
                        ],
                    },
                }
            ],
        ),
    )
    assert (
        next(clip for clip in rebased.snapshot.clips if clip.clip_id == "clip-image").opacity_bp
        == 7000
    )
    conflict = transaction_wire(
        rebased.snapshot,
        request_id="request-rebase-conflict",
        commands=[
            {
                "kind": "rebase_transaction",
                "payload": {
                    "base_timeline_fingerprint": base_fingerprint,
                    "commands": [
                        {
                            "kind": "set_opacity_blend",
                            "payload": {
                                "clip_id": "clip-image",
                                "opacity_bp": 6000,
                                "blend": "multiply",
                            },
                        }
                    ],
                },
            }
        ],
    )
    assert history_rejection_code(lambda: apply_timeline_transaction(rebased, conflict)) == (
        "rebase_conflict"
    )


def test_rebase_selection_rejects_a_target_that_did_not_exist_at_the_base() -> None:
    initial = TimelineHistoryState.initialize(fixture_snapshot_with_edit_coverage())
    base_fingerprint = initial.snapshot.timeline_fingerprint
    current, _ = apply_timeline_transaction(
        initial,
        transaction_wire(
            initial.snapshot,
            request_id="request-split-before-selection-rebase",
            commands=[
                {
                    "kind": "split_clip",
                    "payload": {
                        "clip_id": "clip-main",
                        "at_offset_frames": 24,
                        "right_clip_id": "clip-main-right",
                    },
                }
            ],
        ),
    )
    stale_selection = transaction_wire(
        current.snapshot,
        request_id="request-selection-rebase-new-target",
        commands=[
            {
                "kind": "rebase_transaction",
                "payload": {
                    "base_timeline_fingerprint": base_fingerprint,
                    "commands": [
                        {
                            "kind": "select_clips",
                            "payload": {"clip_ids": ["clip-main-right"]},
                        }
                    ],
                },
            }
        ],
    )

    assert (
        history_rejection_code(lambda: apply_timeline_transaction(current, stale_selection))
        == "rebase_conflict"
    )


def test_history_quota_evicts_oldest_cursor_and_release_is_terminal() -> None:
    state = TimelineHistoryState.initialize(fixture_snapshot())
    first_cursor = ""
    for index in range(MAX_UNDO_ENTRIES + 1):
        state, receipt = apply_timeline_transaction(
            state,
            transaction_wire(
                state.snapshot,
                request_id=f"request-quota-{index}",
                commands=[
                    {
                        "kind": "select_clips",
                        "payload": {"clip_ids": [] if index % 2 else ["clip-main"]},
                    }
                ],
            ),
        )
        if index == 0:
            first_cursor = receipt.history_cursor
    assert len(state.undo_entries) == MAX_UNDO_ENTRIES
    stale_cursor = transaction_wire(
        state.snapshot,
        request_id="request-evicted-cursor",
        commands=[{"kind": "undo", "payload": {"history_cursor": first_cursor}}],
    )
    assert history_rejection_code(lambda: apply_timeline_transaction(state, stale_cursor)) == (
        "history_cursor_invalid"
    )
    released = release_timeline_history(state)
    assert released.released is True
    assert released.undo_entries == released.redo_entries == ()
    after_release = transaction_wire(
        released.snapshot,
        request_id="request-after-release",
        commands=[{"kind": "select_clips", "payload": {"clip_ids": []}}],
    )
    assert history_rejection_code(lambda: apply_timeline_transaction(released, after_release)) == (
        "workspace_released"
    )


def test_history_byte_quota_evicts_oldest_material_deterministically() -> None:
    wire = fixture_wire()
    primary = wire["clips"][0]
    text = cast(dict[str, object], copy.deepcopy(wire["clips"][3]["text"]))
    text["content"] = "x" * 2048
    wire["clips"] = [primary] + [
        clip_wire(
            clip_id=f"large-title-{index:03d}",
            asset_id=None,
            track_id="track-text",
            start_frame=0,
            duration_frames=1,
            text=text,
        )
        for index in range(127)
    ]
    state = TimelineHistoryState.initialize(resign(wire))
    first_cursor = ""
    for index in range(16):
        state, receipt = apply_timeline_transaction(
            state,
            transaction_wire(
                state.snapshot,
                request_id=f"request-byte-quota-{index}",
                commands=[
                    {
                        "kind": "select_clips",
                        "payload": {"clip_ids": [] if index % 2 else ["clip-main"]},
                    }
                ],
            ),
        )
        if index == 0:
            first_cursor = receipt.history_cursor
    history_bytes = sum(
        entry.byte_size() for entry in (*state.undo_entries, *state.redo_entries)
    ) + sum(record.byte_size() for record in state.idempotency_records)
    assert history_bytes <= MAX_HISTORY_BYTES
    assert len(state.undo_entries) < 16
    evicted = transaction_wire(
        state.snapshot,
        request_id="request-byte-evicted-cursor",
        commands=[{"kind": "undo", "payload": {"history_cursor": first_cursor}}],
    )
    assert history_rejection_code(lambda: apply_timeline_transaction(state, evicted)) == (
        "history_cursor_invalid"
    )


@pytest.mark.parametrize("frame_count", [256, 257, 362, 512])
def test_dense_source_landmarks_survive_insert_replay_undo_and_redo(frame_count: int) -> None:
    wire = fixture_wire()
    source = wire["assets"][0]
    source["source_frame_count"] = frame_count
    source["source_sample_count"] = frame_count * 2000
    source["landmarks"] = [
        {"frame_index": frame, "pts": frame * 512, "dts": frame * 512, "duration_ticks": 512}
        for frame in range(frame_count)
    ]
    wire["assets"] = [source]
    wire["clips"] = []
    wire["output"]["duration_frames"] = frame_count
    initial = TimelineHistoryState.initialize(resign(wire))
    original_assets = initial.snapshot.assets
    tx = transaction_wire(
        initial.snapshot,
        request_id="dense-source-insert",
        commands=[
            {
                "kind": "insert_asset_clip",
                "payload": {
                    "clip": clip_wire(
                        clip_id="dense-source-clip",
                        asset_id="vid-primary",
                        track_id="track-primary",
                        start_frame=0,
                        duration_frames=frame_count,
                    )
                },
            }
        ],
    )
    inserted, receipt = apply_timeline_transaction(initial, tx)
    assert inserted.snapshot.assets == original_assets
    assert inserted.snapshot.clips[0].duration_frames == frame_count
    assert len(inserted.undo_entries) == len(inserted.idempotency_records) == 1
    frozen_receipt = copy.deepcopy(receipt.to_wire())
    replayed, replay_receipt = apply_timeline_transaction(inserted, tx)
    assert replayed == inserted
    assert replay_receipt.to_wire() == frozen_receipt
    undone, undo_receipt = apply_timeline_transaction(
        inserted,
        transaction_wire(
            inserted.snapshot,
            request_id="dense-source-undo",
            commands=[{"kind": "undo", "payload": {"history_cursor": receipt.history_cursor}}],
        ),
    )
    assert undone.snapshot.assets == original_assets
    assert undone.snapshot.clips == ()
    redone, _ = apply_timeline_transaction(
        undone,
        transaction_wire(
            undone.snapshot,
            request_id="dense-source-redo",
            commands=[{"kind": "redo", "payload": {"history_cursor": undo_receipt.history_cursor}}],
        ),
    )
    assert redone.snapshot.assets == original_assets
    assert redone.snapshot.clips == inserted.snapshot.clips
    assert receipt.to_wire() == frozen_receipt
    assert len(redone.idempotency_records) == 3
    assert len(redone.snapshot.assets[0].landmarks) == frame_count


def test_history_byte_accounting_counts_utf8_and_preserves_accepted_text() -> None:
    states = []
    for content in ("A", "\u00e9"):
        wire = fixture_wire()
        title = next(clip for clip in wire["clips"] if clip["text"] is not None)
        title["text"]["content"] = content
        initial = TimelineHistoryState.initialize(resign(wire))
        state, _ = apply_timeline_transaction(
            initial,
            transaction_wire(
                initial.snapshot,
                request_id="unicode-storage",
                commands=[{"kind": "select_clips", "payload": {"clip_ids": [title["clip_id"]]}}],
            ),
        )
        stored_title = next(clip for clip in state.snapshot.clips if clip.text is not None)
        assert stored_title.text is not None and stored_title.text.content == content
        states.append(state)
    ascii_state, multibyte = states
    assert ascii_state.snapshot.public_fingerprint != multibyte.snapshot.public_fingerprint
    # Both accepted strings have one character; the second uses one additional UTF-8 byte.
    assert multibyte.undo_entries[0].byte_size() == ascii_state.undo_entries[0].byte_size() + 2
    assert (
        multibyte.idempotency_records[0].byte_size()
        == ascii_state.idempotency_records[0].byte_size() + 1
    )


def test_history_single_material_byte_limit_remains_bounded() -> None:
    wire = fixture_wire()
    title = next(clip for clip in wire["clips"] if clip["text"] is not None)
    title["text"]["content"] = "\U0001f989" * 2048
    wire["clips"] = []
    for index in range(64):
        clip = copy.deepcopy(title)
        clip["clip_id"] = f"bounded-title-{index}"
        wire["clips"].append(clip)
    initial = TimelineHistoryState.initialize(resign(wire))
    tx = transaction_wire(
        initial.snapshot,
        request_id="single-material-limit",
        commands=[{"kind": "select_clips", "payload": {"clip_ids": ["bounded-title-0"]}}],
    )
    assert (
        history_rejection_code(lambda: apply_timeline_transaction(initial, tx)) == "resource_limit"
    )
    assert initial.undo_entries == ()
    assert initial.idempotency_records == ()
