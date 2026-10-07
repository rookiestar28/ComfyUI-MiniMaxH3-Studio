"""What every timeline command does to a clip's audio member.

A command that moves a clip's edge keeps each fade at the edge it belongs to and clamps the fades
to the new duration, fade-out first; a command that cuts through a clip drops the fade whose ramp
the cut removed; merge is the inverse of split; a replaced asset without bound audio takes the
identity; every other command leaves the member as it was. Each result is decoded and admitted
again, so the decoder's sum rule stands behind every clamp, and no command that changes a duration
is refused because of a fade.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.core.composition_contract import (
    IDENTITY_CLIP_AUDIO,
    ClipAudio,
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.nle_authoring_contract import (
    NleAuthoringState,
    create_nle_authoring_state,
)
from comfyui_h3_context.core.timeline_authoring import (
    TimelineCommandError,
    apply_nle_command,
    decode_nle_command,
)
from comfyui_h3_context.core.timeline_history import (
    TIMELINE_TRANSACTION_SCHEMA,
    TimelineHistoryState,
    apply_timeline_transaction,
    decode_timeline_transaction,
)

FIXTURE = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"
PRIMARY_TRACK = "track-primary"
#: Every adjusted clip carries this gain, so a row shows what an edit did to the fades alone.
GAIN = -600
FADED = ClipAudio(GAIN, False, 12, 12)

Clip = tuple[str, int, int, int, ClipAudio]


def _audio_wire(audio: ClipAudio) -> dict[str, object]:
    return {
        "gain_mb": audio.gain_mb,
        "muted": audio.muted,
        "fade_in_frames": audio.fade_in_frames,
        "fade_out_frames": audio.fade_out_frames,
    }


def _composition(clips: list[Clip]) -> PublicCompositionSnapshot:
    """The accepted composition's assets and tracks with only the given primary-track clips.

    Each clip is (identifier, start, duration, source start, audio). Every source frame is a
    landmark, so whatever source shift an edit makes is representable.
    """

    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wire: dict[str, Any] = copy.deepcopy(document["snapshot"])
    for asset in wire["assets"]:
        if asset["kind"] == "video":
            asset["landmarks"] = [
                {
                    "frame_index": frame,
                    "pts": frame * 512,
                    "dts": frame * 512,
                    "duration_ticks": 512,
                }
                for frame in range(asset["source_frame_count"])
            ]
    template = next(clip for clip in wire["clips"] if clip["clip_id"] == "clip-main")
    wire["clips"] = []
    for clip_id, start, duration, source_start, audio in clips:
        clip = copy.deepcopy(template)
        clip.update(
            clip_id=clip_id,
            start_frame=start,
            duration_frames=duration,
            source_start_frame=source_start,
        )
        if audio != IDENTITY_CLIP_AUDIO:
            clip["audio"] = _audio_wire(audio)
        wire["clips"].append(clip)
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def _apply(
    snapshot: PublicCompositionSnapshot | NleAuthoringState, kind: str, payload: dict[str, object]
) -> PublicCompositionSnapshot | NleAuthoringState:
    return apply_nle_command(
        snapshot, decode_nle_command({"kind": kind, "payload": payload})
    ).snapshot


def _members(snapshot: PublicCompositionSnapshot | NleAuthoringState) -> dict[str, ClipAudio]:
    return {clip.clip_id: clip.audio for clip in snapshot.clips}


def _new_clip(
    clip_id: str, start: int, duration: int, *, asset_id: str = "vid-primary"
) -> dict[str, Any]:
    clip = next(item.to_wire() for item in _composition([("x", 0, 48, 0, FADED)]).clips)
    clip.pop("audio")
    clip.update(
        clip_id=clip_id,
        asset_id=asset_id,
        start_frame=start,
        duration_frames=duration,
        source_start_frame=0,
    )
    return clip


#: (row, clips, command, payload, every clip's member after it).
ROWS: list[tuple[str, list[Clip], str, dict[str, object], dict[str, ClipAudio]]] = [
    (
        "trim_end_below_both_fades_clamps_the_fade_in",
        [("a", 0, 48, 0, FADED)],
        "trim_clip",
        {"clip_id": "a", "edge": "end", "delta_frames": -30},
        {"a": ClipAudio(GAIN, False, 6, 12)},
    ),
    (
        "trim_end_below_the_fade_out_leaves_no_fade_in",
        [("a", 0, 48, 0, FADED)],
        "trim_clip",
        {"clip_id": "a", "edge": "end", "delta_frames": -40},
        {"a": ClipAudio(GAIN, False, 0, 8)},
    ),
    (
        "trim_that_still_fits_both_fades_changes_nothing",
        [("a", 0, 48, 0, FADED)],
        "trim_clip",
        {"clip_id": "a", "edge": "end", "delta_frames": -24},
        {"a": FADED},
    ),
    (
        "trim_start_clamps_the_fade_out_first",
        [("a", 0, 48, 0, FADED)],
        "trim_clip",
        {"clip_id": "a", "edge": "start", "delta_frames": 30},
        {"a": ClipAudio(GAIN, False, 6, 12)},
    ),
    (
        "ripple_trim_end",
        [("a", 0, 48, 0, FADED)],
        "ripple_trim",
        {"clip_id": "a", "edge": "end", "delta_frames": -30, "scope_track_ids": [PRIMARY_TRACK]},
        {"a": ClipAudio(GAIN, False, 6, 12)},
    ),
    (
        "ripple_trim_start",
        [("a", 0, 48, 0, FADED)],
        "ripple_trim",
        {"clip_id": "a", "edge": "start", "delta_frames": 30, "scope_track_ids": [PRIMARY_TRACK]},
        {"a": ClipAudio(GAIN, False, 6, 12)},
    ),
    (
        "roll_back_clamps_the_left_clip",
        [("a", 0, 24, 0, FADED), ("b", 24, 24, 24, ClipAudio(300, False, 12, 12))],
        "roll_edit",
        {"left_clip_id": "a", "right_clip_id": "b", "delta_frames": -6},
        {"a": ClipAudio(GAIN, False, 6, 12), "b": ClipAudio(300, False, 12, 12)},
    ),
    (
        "roll_forward_clamps_the_right_clip",
        [("a", 0, 24, 0, FADED), ("b", 24, 24, 24, ClipAudio(300, False, 12, 12))],
        "roll_edit",
        {"left_clip_id": "a", "right_clip_id": "b", "delta_frames": 6},
        {"a": FADED, "b": ClipAudio(300, False, 6, 12)},
    ),
    (
        "slide_clamps_the_neighbour_it_shortens",
        [
            ("l", 0, 16, 0, ClipAudio(GAIN, False, 8, 8)),
            ("m", 16, 16, 16, ClipAudio(-1200, True, 4, 4)),
            ("r", 32, 16, 32, ClipAudio(0, False, 8, 8)),
        ],
        "slide_clip",
        {"clip_id": "m", "left_clip_id": "l", "right_clip_id": "r", "delta_frames": 4},
        {
            "l": ClipAudio(GAIN, False, 8, 8),
            "m": ClipAudio(-1200, True, 4, 4),
            "r": ClipAudio(0, False, 4, 8),
        },
    ),
    (
        "slide_back_clamps_the_left_neighbour",
        [
            ("l", 0, 16, 0, ClipAudio(GAIN, False, 8, 8)),
            ("m", 16, 16, 16, ClipAudio(-1200, True, 4, 4)),
            ("r", 32, 16, 32, ClipAudio(0, False, 8, 8)),
        ],
        "slide_clip",
        {"clip_id": "m", "left_clip_id": "l", "right_clip_id": "r", "delta_frames": -4},
        {
            "l": ClipAudio(GAIN, False, 4, 8),
            "m": ClipAudio(-1200, True, 4, 4),
            "r": ClipAudio(0, False, 8, 8),
        },
    ),
    (
        "split_outside_the_fades",
        [("a", 0, 48, 0, FADED)],
        "split_clip",
        {"clip_id": "a", "at_offset_frames": 20, "right_clip_id": "b"},
        {"a": ClipAudio(GAIN, False, 12, 0), "b": ClipAudio(GAIN, False, 0, 12)},
    ),
    (
        "split_inside_the_fade_in",
        [("a", 0, 48, 0, FADED)],
        "split_clip",
        {"clip_id": "a", "at_offset_frames": 6, "right_clip_id": "b"},
        {"a": ClipAudio(GAIN, False, 6, 0), "b": ClipAudio(GAIN, False, 0, 12)},
    ),
    (
        "split_keeps_the_mute_on_both_halves",
        [("a", 0, 48, 0, ClipAudio(GAIN, True, 12, 12))],
        "split_clip",
        {"clip_id": "a", "at_offset_frames": 20, "right_clip_id": "b"},
        {"a": ClipAudio(GAIN, True, 12, 0), "b": ClipAudio(GAIN, True, 0, 12)},
    ),
    (
        "split_writes_an_identity_half_absent",
        [("a", 0, 48, 0, ClipAudio(0, False, 12, 0))],
        "split_clip",
        {"clip_id": "a", "at_offset_frames": 20, "right_clip_id": "b"},
        {"a": ClipAudio(0, False, 12, 0), "b": IDENTITY_CLIP_AUDIO},
    ),
    (
        "merge_restores_what_split_divided",
        [
            ("a", 0, 20, 0, ClipAudio(GAIN, False, 12, 0)),
            ("b", 20, 28, 20, ClipAudio(GAIN, False, 0, 12)),
        ],
        "merge_clips",
        {"left_clip_id": "a", "right_clip_id": "b"},
        {"a": FADED},
    ),
    (
        "merge_of_two_identity_clips",
        [("a", 0, 20, 0, IDENTITY_CLIP_AUDIO), ("b", 20, 28, 20, IDENTITY_CLIP_AUDIO)],
        "merge_clips",
        {"left_clip_id": "a", "right_clip_id": "b"},
        {"a": IDENTITY_CLIP_AUDIO},
    ),
    (
        "ripple_delete_through_a_clip_cuts_both_pieces",
        [("a", 0, 48, 0, FADED)],
        "ripple_delete",
        {
            "start_frame": 18,
            "duration_frames": 12,
            "scope_track_ids": [PRIMARY_TRACK],
            "remainder_ids": {"a": "b"},
        },
        {"a": ClipAudio(GAIN, False, 12, 0), "b": ClipAudio(GAIN, False, 0, 12)},
    ),
    (
        "ripple_delete_of_a_tail",
        [("a", 0, 48, 0, FADED)],
        "ripple_delete",
        {
            "start_frame": 40,
            "duration_frames": 8,
            "scope_track_ids": [PRIMARY_TRACK],
            "remainder_ids": {},
        },
        {"a": ClipAudio(GAIN, False, 12, 0)},
    ),
    (
        "ripple_delete_of_a_head",
        [("a", 0, 48, 0, FADED)],
        "ripple_delete",
        {
            "start_frame": 0,
            "duration_frames": 8,
            "scope_track_ids": [PRIMARY_TRACK],
            "remainder_ids": {},
        },
        {"a": ClipAudio(GAIN, False, 0, 12)},
    ),
    # The kept piece is shorter than the fade it keeps: the fade is clamped to the piece's new
    # length, not to the length the clip had before the cut.
    (
        "ripple_delete_of_a_tail_through_the_fade_in_clamps_it",
        [("a", 0, 48, 0, FADED)],
        "ripple_delete",
        {
            "start_frame": 6,
            "duration_frames": 42,
            "scope_track_ids": [PRIMARY_TRACK],
            "remainder_ids": {},
        },
        {"a": ClipAudio(GAIN, False, 6, 0)},
    ),
    (
        "ripple_delete_of_a_head_through_the_fade_out_clamps_it",
        [("a", 0, 48, 0, FADED)],
        "ripple_delete",
        {
            "start_frame": 0,
            "duration_frames": 42,
            "scope_track_ids": [PRIMARY_TRACK],
            "remainder_ids": {},
        },
        {"a": ClipAudio(GAIN, False, 0, 6)},
    ),
    (
        "overwrite_through_a_clip_cuts_both_pieces_and_inserts_identity",
        [("a", 0, 48, 0, FADED)],
        "overwrite_range",
        {
            "start_frame": 18,
            "duration_frames": 12,
            "scope_track_ids": [PRIMARY_TRACK],
            "remainder_ids": {"a": "b"},
            "clip": _new_clip("c", 18, 12),
        },
        {
            "a": ClipAudio(GAIN, False, 12, 0),
            "b": ClipAudio(GAIN, False, 0, 12),
            "c": IDENTITY_CLIP_AUDIO,
        },
    ),
    (
        "replace_with_a_source_without_bound_audio_takes_the_identity",
        [("a", 0, 24, 0, ClipAudio(GAIN, False, 6, 6))],
        "replace_clip_asset",
        {"clip_id": "a", "asset_id": "vid-overlay", "source_start_frame": 0},
        {"a": IDENTITY_CLIP_AUDIO},
    ),
    (
        "replace_with_a_source_with_bound_audio_keeps_it",
        [("a", 0, 24, 0, ClipAudio(GAIN, False, 6, 6))],
        "replace_clip_asset",
        {"clip_id": "a", "asset_id": "vid-primary", "source_start_frame": 12},
        {"a": ClipAudio(GAIN, False, 6, 6)},
    ),
    (
        "move_to_an_overlay_track_keeps_it",
        [("a", 0, 24, 0, FADED)],
        "move_clip",
        {"clip_id": "a", "delta_frames": 4, "target_track_id": "track-video"},
        {"a": FADED},
    ),
    (
        "slip_keeps_it",
        [("a", 0, 24, 0, FADED)],
        "slip_clip",
        {"clip_id": "a", "delta_frames": 12},
        {"a": FADED},
    ),
    (
        "disable_keeps_it",
        [("a", 0, 24, 0, FADED), ("b", 24, 24, 24, IDENTITY_CLIP_AUDIO)],
        "set_clip_enabled",
        {"clip_id": "a", "enabled": False},
        {"a": FADED, "b": IDENTITY_CLIP_AUDIO},
    ),
    (
        "a_visual_edit_keeps_it",
        [("a", 0, 24, 0, FADED)],
        "set_opacity_blend",
        {"clip_id": "a", "opacity_bp": 6_000, "blend": "normal"},
        {"a": FADED},
    ),
    (
        "move_group_changes_time_and_track_but_keeps_each_member",
        [
            ("a", 0, 12, 0, ClipAudio(GAIN, False, 3, 4)),
            ("b", 16, 12, 16, ClipAudio(300, True, 2, 6)),
        ],
        "move_group",
        {
            "clip_ids": ["a", "b"],
            "delta_frames": 4,
            "target_track_ids": ["track-video", "track-video"],
        },
        {"a": ClipAudio(GAIN, False, 3, 4), "b": ClipAudio(300, True, 2, 6)},
    ),
    (
        "insert_range_with_absent_audio_keeps_the_shifted_members",
        [
            ("a", 0, 12, 0, ClipAudio(GAIN, False, 3, 4)),
            ("b", 16, 12, 16, ClipAudio(300, True, 2, 6)),
        ],
        "insert_range",
        {"clip": _new_clip("c", 0, 12), "scope_track_ids": [PRIMARY_TRACK]},
        {
            "a": ClipAudio(GAIN, False, 3, 4),
            "b": ClipAudio(300, True, 2, 6),
            "c": IDENTITY_CLIP_AUDIO,
        },
    ),
    (
        "insert_range_with_adjusted_audio_keeps_carried_and_shifted_members",
        [
            ("a", 0, 12, 0, ClipAudio(GAIN, False, 3, 4)),
            ("b", 16, 12, 16, ClipAudio(300, True, 2, 6)),
        ],
        "insert_range",
        {
            "clip": _new_clip("c", 0, 12) | {"audio": _audio_wire(ClipAudio(-900, True, 5, 7))},
            "scope_track_ids": [PRIMARY_TRACK],
        },
        {
            "a": ClipAudio(GAIN, False, 3, 4),
            "b": ClipAudio(300, True, 2, 6),
            "c": ClipAudio(-900, True, 5, 7),
        },
    ),
    (
        "remove_clip_keeps_the_remaining_members",
        [
            ("a", 0, 12, 0, ClipAudio(GAIN, False, 3, 4)),
            ("b", 16, 12, 16, ClipAudio(300, True, 2, 6)),
            ("c", 32, 12, 32, IDENTITY_CLIP_AUDIO),
        ],
        "remove_clip",
        {"clip_id": "a"},
        {"b": ClipAudio(300, True, 2, 6), "c": IDENTITY_CLIP_AUDIO},
    ),
    (
        "select_clips_keeps_every_member",
        [
            ("a", 0, 12, 0, ClipAudio(GAIN, False, 3, 4)),
            ("b", 16, 12, 16, ClipAudio(300, True, 2, 6)),
            ("c", 32, 12, 32, IDENTITY_CLIP_AUDIO),
        ],
        "select_clips",
        {"clip_ids": ["a", "b"]},
        {
            "a": ClipAudio(GAIN, False, 3, 4),
            "b": ClipAudio(300, True, 2, 6),
            "c": IDENTITY_CLIP_AUDIO,
        },
    ),
    (
        "disable_track_keeps_the_members_on_that_track",
        [
            ("a", 0, 12, 0, ClipAudio(GAIN, False, 3, 4)),
            ("b", 16, 12, 16, ClipAudio(300, True, 2, 6)),
        ],
        "set_track_enabled",
        {"track_id": PRIMARY_TRACK, "enabled": False},
        {"a": ClipAudio(GAIN, False, 3, 4), "b": ClipAudio(300, True, 2, 6)},
    ),
]


@pytest.mark.parametrize(
    ("clips", "kind", "payload", "expected"),
    [row[1:] for row in ROWS],
    ids=[row[0] for row in ROWS],
)
def test_each_command_leaves_the_member_the_rules_state(
    clips: list[Clip], kind: str, payload: dict[str, object], expected: dict[str, ClipAudio]
) -> None:
    before = _composition(clips)
    transition = apply_nle_command(before, decode_nle_command({"kind": kind, "payload": payload}))
    after = transition.snapshot
    assert _members(after) == expected
    # GUARD: prove a real transition; unchanged audio alone also passes when a command does nothing.
    if kind == "move_group":
        clip_ids = payload["clip_ids"]
        target_track_ids = payload["target_track_ids"]
        delta_frames = payload["delta_frames"]
        assert isinstance(clip_ids, list) and isinstance(target_track_ids, list)
        assert isinstance(delta_frames, int)
        for clip in after.clips:
            original = next(item for item in before.clips if item.clip_id == clip.clip_id)
            index = clip_ids.index(clip.clip_id)
            assert clip.start_frame == original.start_frame + delta_frames
            assert clip.track_id == target_track_ids[index]
    elif kind == "insert_range":
        carried = payload["clip"]
        assert isinstance(carried, dict)
        for original in before.clips:
            shifted = next(item for item in after.clips if item.clip_id == original.clip_id)
            assert shifted.start_frame == original.start_frame + carried["duration_frames"]
        inserted = next(item for item in after.clips if item.clip_id == carried["clip_id"])
        assert inserted.to_wire() == carried
    elif kind == "remove_clip":
        assert payload["clip_id"] not in _members(after)
    elif kind == "select_clips":
        clip_ids = payload["clip_ids"]
        assert isinstance(clip_ids, list)
        assert transition.selection == tuple(clip_ids)
        assert after.to_wire() == before.to_wire()
        assert transition.semantic_mutation is False
    elif kind == "set_track_enabled":
        track = next(item for item in after.tracks if item.track_id == payload["track_id"])
        assert track.enabled is payload["enabled"]
    # One value, one wire: an identity member is written absent.
    wires = after.to_wire()["clips"]
    assert isinstance(wires, list)
    for clip in wires:
        assert ("audio" in clip) is (expected[clip["clip_id"]] != IDENTITY_CLIP_AUDIO)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (ClipAudio(GAIN, False, 12, 0), ClipAudio(0, False, 0, 12)),
        (ClipAudio(GAIN, True, 12, 0), ClipAudio(GAIN, False, 0, 12)),
        (ClipAudio(GAIN, False, 12, 4), ClipAudio(GAIN, False, 0, 12)),
        (ClipAudio(GAIN, False, 12, 0), ClipAudio(GAIN, False, 4, 12)),
    ],
    ids=["gains_differ", "mutes_differ", "left_fades_out", "right_fades_in"],
)
def test_merge_refuses_halves_that_split_could_not_have_made(
    left: ClipAudio, right: ClipAudio
) -> None:
    snapshot = _composition([("a", 0, 20, 0, left), ("b", 20, 28, 20, right)])
    with pytest.raises(TimelineCommandError) as raised:
        _apply(snapshot, "merge_clips", {"left_clip_id": "a", "right_clip_id": "b"})
    assert raised.value.code == "invalid_command"


@pytest.mark.parametrize("kind", ["insert_asset_clip", "insert_range"])
@pytest.mark.parametrize(
    ("clip", "code"),
    [
        (_new_clip("c", 24, 24) | {"audio": _audio_wire(FADED)}, None),
        (
            _new_clip("c", 24, 24, asset_id="vid-overlay") | {"audio": _audio_wire(FADED)},
            "invalid_contract",
        ),
        (
            _new_clip("c", 24, 20) | {"audio": _audio_wire(ClipAudio(GAIN, False, 12, 12))},
            "invalid_contract",
        ),
    ],
    ids=["bound_audio", "audio_the_overlay_policy_excludes", "fades_longer_than_the_clip"],
)
def test_an_inserted_clip_carries_a_member_the_decoder_admits(
    kind: str, clip: dict[str, Any], code: str | None
) -> None:
    snapshot = _composition([("a", 0, 24, 0, IDENTITY_CLIP_AUDIO)])
    payload: dict[str, object] = {"clip": clip}
    if kind == "insert_range":
        payload["scope_track_ids"] = [PRIMARY_TRACK]
    if code is not None:
        with pytest.raises(TimelineCommandError) as raised:
            _apply(snapshot, kind, payload)
        assert raised.value.code == code
        return
    assert _members(_apply(snapshot, kind, payload))["c"] == FADED


@pytest.mark.parametrize(
    "audio",
    [
        pytest.param(_audio_wire(IDENTITY_CLIP_AUDIO), id="explicit_identity"),
        pytest.param(None, id="null"),
        pytest.param([], id="array"),
        pytest.param(
            {key: value for key, value in _audio_wire(FADED).items() if key != "muted"},
            id="missing_member",
        ),
        pytest.param(_audio_wire(FADED) | {"pan": 0}, id="extra_member"),
        pytest.param(_audio_wire(FADED) | {"gain_mb": -6_001}, id="gain_below_lower_bound"),
        pytest.param(_audio_wire(FADED) | {"gain_mb": 1_201}, id="gain_above_upper_bound"),
        pytest.param(_audio_wire(FADED) | {"gain_mb": True}, id="gain_boolean"),
        pytest.param(_audio_wire(FADED) | {"gain_mb": "-600"}, id="gain_text"),
        pytest.param(_audio_wire(FADED) | {"muted": 1}, id="muted_integer"),
        pytest.param(_audio_wire(FADED) | {"muted": None}, id="muted_null"),
        pytest.param(_audio_wire(FADED) | {"fade_in_frames": -1}, id="fade_in_negative"),
        pytest.param(_audio_wire(FADED) | {"fade_out_frames": True}, id="fade_out_boolean"),
    ],
)
def test_insert_range_refuses_a_noncanonical_carried_member(audio: object) -> None:
    snapshot = _composition([("a", 0, 24, 0, FADED)])
    clip = _new_clip("c", 24, 24)
    payload: dict[str, object] = {"clip": clip, "scope_track_ids": [PRIMARY_TRACK]}
    # The same clip without audio is admitted: the refusal must come from the carried member.
    assert _members(_apply(snapshot, "insert_range", payload)) == {
        "a": FADED,
        "c": IDENTITY_CLIP_AUDIO,
    }
    clip["audio"] = audio
    with pytest.raises(TimelineCommandError) as raised:
        _apply(snapshot, "insert_range", payload)
    assert raised.value.code == "invalid_contract"


def test_a_replacement_the_composition_does_not_declare_is_refused_by_the_contract() -> None:
    """No member is read off an asset that is not there: the result is refused like any clip
    that names an undeclared asset, with the contract's code."""

    snapshot = _composition([("a", 0, 24, 0, FADED)])
    with pytest.raises(TimelineCommandError) as raised:
        _apply(
            snapshot,
            "replace_clip_asset",
            {"clip_id": "a", "asset_id": "vid-missing", "source_start_frame": 0},
        )
    assert raised.value.code == "invalid_contract"


def test_a_title_clip_takes_no_member() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    title = copy.deepcopy(
        next(clip for clip in document["snapshot"]["clips"] if clip["clip_id"] == "clip-title")
    )
    snapshot = _composition([("a", 0, 48, 0, IDENTITY_CLIP_AUDIO)])
    title.update(clip_id="t", start_frame=0)
    # Without the member the title is inserted, so the refusal below is the member's own.
    assert "t" in _members(_apply(snapshot, "insert_title_clip", {"clip": copy.deepcopy(title)}))
    title["audio"] = _audio_wire(FADED)
    with pytest.raises(TimelineCommandError) as raised:
        _apply(snapshot, "insert_title_clip", {"clip": title})
    assert raised.value.code == "invalid_contract"


def test_undo_and_redo_restore_a_clamped_member() -> None:
    state = TimelineHistoryState.initialize(_composition([("a", 0, 48, 0, FADED)]))

    def transact(request_id: str, kind: str, payload: dict[str, object]) -> Any:
        nonlocal state
        snapshot = state.snapshot
        state, receipt = apply_timeline_transaction(
            state,
            decode_timeline_transaction(
                {
                    "schema": TIMELINE_TRANSACTION_SCHEMA,
                    "request_id": request_id,
                    "transaction_id": f"tx-{request_id}",
                    "workspace_handle": snapshot.workspace_handle,
                    "expected_workspace_revision": snapshot.workspace_revision,
                    "expected_timeline_revision": snapshot.timeline_revision,
                    "expected_timeline_fingerprint": snapshot.timeline_fingerprint,
                    "commands": [{"kind": kind, "payload": payload}],
                }
            ),
        )
        return receipt

    trimmed = transact("trim", "trim_clip", {"clip_id": "a", "edge": "end", "delta_frames": -30})
    assert _members(state.snapshot) == {"a": ClipAudio(GAIN, False, 6, 12)}
    undone = transact("undo", "undo", {"history_cursor": trimmed.history_cursor})
    assert _members(state.snapshot) == {"a": FADED}
    transact("redo", "redo", {"history_cursor": undone.history_cursor})
    assert _members(state.snapshot) == {"a": ClipAudio(GAIN, False, 6, 12)}


def test_the_authoring_state_keeps_the_rules_through_split_and_merge() -> None:
    snapshot = _composition([("a", 0, 48, 0, FADED)])
    state = create_nle_authoring_state(
        project_id=snapshot.project_id,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        edit_capacity_frames=3_600,
        assets=snapshot.assets,
        tracks=snapshot.tracks,
        clips=snapshot.clips,
        audio_extension=snapshot.audio_extension,
        blockers=snapshot.blockers,
    )
    split = _apply(
        state, "split_clip", {"clip_id": "a", "at_offset_frames": 20, "right_clip_id": "b"}
    )
    assert isinstance(split, NleAuthoringState)
    assert _members(split) == {
        "a": ClipAudio(GAIN, False, 12, 0),
        "b": ClipAudio(GAIN, False, 0, 12),
    }
    merged = _apply(split, "merge_clips", {"left_clip_id": "a", "right_clip_id": "b"})
    assert isinstance(merged, NleAuthoringState)
    assert _members(merged) == {"a": FADED}
