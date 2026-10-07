from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
    resolve_composition,
)
from comfyui_h3_context.core.timeline_authoring import (
    NleCommand,
    TimelineCommandError,
    decode_nle_command,
)
from comfyui_h3_context.core.timeline_authoring import apply_nle_command as _apply_nle_command

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


def _base_wire() -> dict[str, Any]:
    document = cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
    wire = cast(dict[str, Any], copy.deepcopy(document["snapshot"]))
    wire["assets"] = [wire["assets"][0]]
    wire["tracks"] = [wire["tracks"][0]]
    wire["clips"] = [wire["clips"][0]]
    wire["output"]["duration_frames"] = 48
    return wire


def _clip(
    template: dict[str, Any],
    clip_id: str,
    start_frame: int,
    duration_frames: int,
    source_start_frame: int,
) -> dict[str, Any]:
    result = copy.deepcopy(template)
    result.update(
        clip_id=clip_id,
        start_frame=start_frame,
        duration_frames=duration_frames,
        source_start_frame=source_start_frame,
    )
    return result


def _snapshot(
    rate: int,
    clips: tuple[tuple[str, int, int, int], ...] = (("clip-main", 0, 18, 0),),
    *,
    enabled: bool = True,
) -> PublicCompositionSnapshot:
    wire = _base_wire()
    asset = cast(dict[str, Any], wire["assets"][0])
    ticks_per_frame = 6_000 // rate
    asset.update(
        source_time_base={"num": 1, "den": 6_000},
        source_frame_count=64,
        source_sample_count=64 * 48_000 // rate,
        landmarks=[
            {
                "frame_index": frame,
                "pts": frame * ticks_per_frame,
                "dts": frame * ticks_per_frame,
                "duration_ticks": ticks_per_frame,
            }
            for frame in range(64)
        ],
    )
    track = cast(dict[str, Any], wire["tracks"][0])
    track["enabled"] = enabled
    template = cast(dict[str, Any], wire["clips"][0])
    wire["clips"] = [_clip(template, *row) for row in clips]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def _vfr_snapshot(
    *, exact_boundary: bool, source_offset: bool = False
) -> PublicCompositionSnapshot:
    wire = _base_wire()
    asset = cast(dict[str, Any], wire["assets"][0])
    landmarks = [
        {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 40},
        {"frame_index": 1, "pts": 40, "dts": 40, "duration_ticks": 40},
        {"frame_index": 2, "pts": 80, "dts": 80, "duration_ticks": 45},
        {"frame_index": 3, "pts": 125, "dts": 125, "duration_ticks": 35},
        {"frame_index": 4, "pts": 165, "dts": 165, "duration_ticks": 35},
        {"frame_index": 5, "pts": 205, "dts": 205, "duration_ticks": 45},
    ]
    if not exact_boundary:
        landmarks = [row for row in landmarks if row["pts"] != (165 if source_offset else 125)]
    asset.update(
        source_time_base={"num": 1, "den": 1_000},
        source_frame_count=6,
        source_sample_count=12_000,
        landmarks=landmarks,
    )
    clip = cast(dict[str, Any], wire["clips"][0])
    clip.update(
        duration_frames=4,
        source_start_frame=1 if source_offset else 0,
    )
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def _command(kind: str, **payload: object) -> NleCommand:
    return decode_nle_command({"kind": kind, "payload": payload})


def _observation(snapshot: PublicCompositionSnapshot, frame: int) -> tuple[int, int, int, int]:
    scene = resolve_composition(snapshot, frame)
    assert scene.blockers == () and len(scene.layers) == 1 and scene.audio_span is not None
    layer = scene.layers[0]
    audio = scene.audio_span
    assert layer.source_frame is not None and layer.source_pts is not None
    return (
        layer.source_frame,
        layer.source_pts,
        audio.source_start_sample,
        audio.source_end_sample,
    )


def _range_command(snapshot: PublicCompositionSnapshot, kind: str) -> NleCommand:
    payload: dict[str, object] = {
        "start_frame": 1,
        "duration_frames": 2,
        "scope_track_ids": ["track-primary"],
        "remainder_ids": {"clip-main": "clip-right"},
    }
    if kind == "overwrite_range":
        clips = cast(list[dict[str, Any]], snapshot.to_wire()["clips"])
        template = clips[0]
        payload["clip"] = _clip(template, "clip-overwrite", 1, 2, 0)
    return _command(kind, **payload)


@pytest.mark.parametrize("rate", [12, 24, 48])
def test_cfr_split_and_merge_preserve_picture_and_audio(rate: int) -> None:
    duration = 24
    snapshot = _snapshot(rate, (("clip-main", 0, duration, 0),))
    before = tuple(_observation(snapshot, frame) for frame in range(duration))
    split = apply_nle_command(
        snapshot,
        _command(
            "split_clip",
            clip_id="clip-main",
            at_offset_frames=12,
            right_clip_id="clip-right",
        ),
    ).snapshot

    assert tuple(_observation(split, frame) for frame in range(duration)) == before
    merged = apply_nle_command(
        split,
        _command("merge_clips", left_clip_id="clip-main", right_clip_id="clip-right"),
    ).snapshot
    assert tuple(_observation(merged, frame) for frame in range(duration)) == before
    assert merged.clips == snapshot.clips


def test_merge_uses_physical_source_time_continuity_at_48fps() -> None:
    snapshot = _snapshot(
        48,
        (
            ("clip-left", 0, 6, 0),
            ("clip-right", 6, 12, 12),
        ),
    )
    before = tuple(_observation(snapshot, frame) for frame in range(18))

    merged = apply_nle_command(
        snapshot,
        _command("merge_clips", left_clip_id="clip-left", right_clip_id="clip-right"),
    ).snapshot

    assert tuple(_observation(merged, frame) for frame in range(18)) == before


@pytest.mark.parametrize(
    "case",
    [
        "trim_start",
        "slip",
        "ripple_trim_start",
        "roll",
        "slide",
        "overwrite_remainder",
        "ripple_remainder",
    ],
)
def test_each_source_shift_caller_maps_output_delta_to_exact_source_time(case: str) -> None:
    if case == "trim_start":
        snapshot = _snapshot(48)
        expected = _observation(snapshot, 2)
        result = apply_nle_command(
            snapshot,
            _command("trim_clip", clip_id="clip-main", edge="start", delta_frames=2),
        ).snapshot
        actual = _observation(result, 2)
    elif case == "slip":
        snapshot = _snapshot(48)
        expected = _observation(snapshot, 2)
        result = apply_nle_command(
            snapshot, _command("slip_clip", clip_id="clip-main", delta_frames=2)
        ).snapshot
        actual = _observation(result, 0)
    elif case == "ripple_trim_start":
        snapshot = _snapshot(48)
        expected = _observation(snapshot, 2)
        result = apply_nle_command(
            snapshot,
            _command(
                "ripple_trim",
                clip_id="clip-main",
                edge="start",
                delta_frames=2,
                scope_track_ids=["track-primary"],
            ),
        ).snapshot
        actual = _observation(result, 0)
    elif case == "roll":
        snapshot = _snapshot(
            48,
            (("clip-left", 0, 6, 0), ("clip-right", 6, 12, 12)),
        )
        expected = _observation(snapshot, 8)
        result = apply_nle_command(
            snapshot,
            _command(
                "roll_edit",
                left_clip_id="clip-left",
                right_clip_id="clip-right",
                delta_frames=2,
            ),
        ).snapshot
        actual = _observation(result, 8)
    elif case == "slide":
        snapshot = _snapshot(
            48,
            (
                ("clip-left", 0, 6, 0),
                ("clip-middle", 6, 6, 12),
                ("clip-right", 12, 12, 24),
            ),
        )
        expected = _observation(snapshot, 14)
        result = apply_nle_command(
            snapshot,
            _command(
                "slide_clip",
                clip_id="clip-middle",
                left_clip_id="clip-left",
                right_clip_id="clip-right",
                delta_frames=2,
            ),
        ).snapshot
        actual = _observation(result, 14)
    elif case == "overwrite_remainder":
        snapshot = _snapshot(48)
        expected = _observation(snapshot, 3)
        result = apply_nle_command(snapshot, _range_command(snapshot, "overwrite_range")).snapshot
        actual = _observation(result, 3)
    else:
        snapshot = _snapshot(48)
        expected = _observation(snapshot, 3)
        result = apply_nle_command(snapshot, _range_command(snapshot, "ripple_delete")).snapshot
        actual = _observation(result, 1)
    assert actual == expected


@pytest.mark.parametrize(
    "case",
    [
        "trim_start",
        "split",
        "slip",
        "ripple_trim_start",
        "roll",
        "slide",
        "overwrite_remainder",
        "ripple_remainder",
    ],
)
def test_unrepresentable_source_shift_is_an_atomic_typed_refusal(case: str) -> None:
    if case == "roll":
        snapshot = _snapshot(
            12,
            (("clip-left", 0, 6, 0), ("clip-right", 6, 12, 3)),
        )
        command = _command(
            "roll_edit",
            left_clip_id="clip-left",
            right_clip_id="clip-right",
            delta_frames=1,
        )
    elif case == "slide":
        snapshot = _snapshot(
            12,
            (
                ("clip-left", 0, 6, 0),
                ("clip-middle", 6, 6, 3),
                ("clip-right", 12, 12, 6),
            ),
        )
        command = _command(
            "slide_clip",
            clip_id="clip-middle",
            left_clip_id="clip-left",
            right_clip_id="clip-right",
            delta_frames=1,
        )
    else:
        snapshot = _snapshot(12)
        if case == "trim_start":
            command = _command("trim_clip", clip_id="clip-main", edge="start", delta_frames=1)
        elif case == "split":
            command = _command(
                "split_clip",
                clip_id="clip-main",
                at_offset_frames=1,
                right_clip_id="clip-right",
            )
        elif case == "slip":
            command = _command("slip_clip", clip_id="clip-main", delta_frames=1)
        elif case == "ripple_trim_start":
            command = _command(
                "ripple_trim",
                clip_id="clip-main",
                edge="start",
                delta_frames=1,
                scope_track_ids=["track-primary"],
            )
        else:
            command = _range_command(
                snapshot,
                "overwrite_range" if case == "overwrite_remainder" else "ripple_delete",
            )
    fingerprint = snapshot.public_fingerprint
    with pytest.raises(TimelineCommandError, match="source_range_unavailable"):
        apply_nle_command(snapshot, command)
    assert snapshot.public_fingerprint == fingerprint


def test_vfr_shift_requires_exact_landmark_but_sparse_playback_and_properties_remain_valid() -> (
    None
):
    exact = _vfr_snapshot(exact_boundary=True)
    before = _observation(exact, 3)
    split = apply_nle_command(
        exact,
        _command(
            "split_clip",
            clip_id="clip-main",
            at_offset_frames=3,
            right_clip_id="clip-right",
        ),
    ).snapshot
    assert _observation(split, 3) == before
    assert (
        next(clip for clip in split.clips if clip.clip_id == "clip-right").source_start_frame == 3
    )

    sparse = _vfr_snapshot(exact_boundary=False)
    assert _observation(sparse, 3)[0] == 2
    changed = apply_nle_command(
        sparse,
        _command(
            "set_opacity_blend",
            clip_id="clip-main",
            opacity_bp=9000,
            blend="normal",
        ),
    ).snapshot
    assert changed.clips[0].opacity_bp == 9000
    with pytest.raises(TimelineCommandError, match="source_range_unavailable"):
        apply_nle_command(
            sparse,
            _command(
                "split_clip",
                clip_id="clip-main",
                at_offset_frames=3,
                right_clip_id="clip-right",
            ),
        )


def test_vfr_source_offset_and_disabled_track_use_the_same_exact_boundary_authority() -> None:
    offset = _vfr_snapshot(exact_boundary=True, source_offset=True)
    split = apply_nle_command(
        offset,
        _command(
            "split_clip",
            clip_id="clip-main",
            at_offset_frames=3,
            right_clip_id="clip-right",
        ),
    ).snapshot
    assert (
        next(clip for clip in split.clips if clip.clip_id == "clip-right").source_start_frame == 4
    )

    disabled = _snapshot(48, enabled=False)
    disabled_split = apply_nle_command(
        disabled,
        _command(
            "split_clip",
            clip_id="clip-main",
            at_offset_frames=2,
            right_clip_id="clip-right",
        ),
    ).snapshot
    assert (
        next(
            clip for clip in disabled_split.clips if clip.clip_id == "clip-right"
        ).source_start_frame
        == 4
    )
