"""The `set_clip_audio` command: its place in the closed vocabulary, its effect and its history.

The command sets all four values of a clip's audio member at once. A value is checked as the
command's own payload (`invalid_command`) before the result is decoded: the integers and the boolean
in their bounds, the fades within the clip, and a non-identity value only on a clip whose own source
carries bound audio. The identity is written absent, so resetting a clip leaves no member. History
replays it like every other property command, and a rebase over a concurrent change of the clip's
extent or audio conflicts while a change of another property does not. The thirteen
independent-audio names stay refused as deferred.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.core.composition_contract import (
    IDENTITY_CLIP_AUDIO,
    NLE_OPERATION_IDS,
    ClipAudio,
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    TIMELINE_TRANSACTION_SCHEMA_V2,
    NleAuthoringState,
    create_nle_authoring_state,
)
from comfyui_h3_context.core.semantic_conformance import build_corpus
from comfyui_h3_context.core.semantic_conformance_cases import (
    BASE_FIXTURE_NAMES,
    CLIP_AUDIO_BASE,
    build_recipes,
)
from comfyui_h3_context.core.semantic_conformance_drive import command_phases, run_setup, transact
from comfyui_h3_context.core.semantic_conformance_expect import (
    derive_audio_levels,
    required_landmarks,
)
from comfyui_h3_context.core.timeline_authoring import (
    NleCommandKind,
    TimelineCommandError,
    apply_nle_command,
    decode_nle_command,
)
from comfyui_h3_context.core.timeline_history import (
    TIMELINE_TRANSACTION_SCHEMA,
    TimelineHistoryError,
    TimelineHistoryState,
    apply_timeline_transaction,
    decode_timeline_transaction,
)
from comfyui_h3_context.core.timeline_history_v2 import (
    TimelineHistoryStateV2,
    TimelineHistoryV2Error,
    apply_timeline_transaction_v2,
)

FIXTURE = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"
#: `clip-main` (48 frames) shows `vid-primary`, whose audio is bound; `clip-video-overlay` shows a
#: video whose audio the overlay policy excludes; `clip-image` and `clip-title` have no sound.
MAIN = "clip-main"
ADJUSTED: dict[str, object] = {
    "gain_mb": -600,
    "muted": False,
    "fade_in_frames": 12,
    "fade_out_frames": 12,
}


def _wire() -> dict[str, Any]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wire: dict[str, Any] = copy.deepcopy(document["snapshot"])
    return wire


def _snapshot(wire: dict[str, Any] | None = None) -> PublicCompositionSnapshot:
    wire = _wire() if wire is None else wire
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def _short_main(start_frame: int = 0) -> PublicCompositionSnapshot:
    """`clip-main` cut to 24 frames at `start_frame`, and the overlay video without its dissolve.

    The fixture's output is 48 frames and the overlay's dissolve needs `clip-main` beneath it, so
    the full-length clip can neither move nor change track; this one can do both.
    """
    wire = _wire()
    main = next(clip for clip in wire["clips"] if clip["clip_id"] == MAIN)
    main["start_frame"], main["duration_frames"] = start_frame, 24
    overlay = next(clip for clip in wire["clips"] if clip["clip_id"] == "clip-video-overlay")
    overlay["transition"] = {"kind": "none", "duration_frames": 0}
    return _snapshot(wire)


def _command(clip_id: str = MAIN, **values: object) -> dict[str, object]:
    return {"kind": "set_clip_audio", "payload": {"clip_id": clip_id, **ADJUSTED, **values}}


def _apply(
    snapshot: PublicCompositionSnapshot | NleAuthoringState, command: dict[str, object]
) -> PublicCompositionSnapshot | NleAuthoringState:
    return apply_nle_command(snapshot, decode_nle_command(command)).snapshot


def _audio(snapshot: PublicCompositionSnapshot | NleAuthoringState, clip_id: str = MAIN) -> Any:
    return next(clip.audio for clip in snapshot.clips if clip.clip_id == clip_id)


def _code(call: Callable[[], object]) -> str:
    try:
        call()
    except (TimelineCommandError, TimelineHistoryError, TimelineHistoryV2Error) as exc:
        return exc.code
    raise AssertionError("expected a refusal")


def _refusal(command: dict[str, object], snapshot: PublicCompositionSnapshot | None = None) -> str:
    """The command's own refusal, code and reason: the decoder would refuse some of these too."""
    with pytest.raises(TimelineCommandError) as raised:
        _apply(_snapshot() if snapshot is None else snapshot, command)
    return str(raised.value)


def test_the_command_is_the_thirty_fourth_operation_after_set_effect() -> None:
    assert len(NLE_OPERATION_IDS) == 34
    assert NLE_OPERATION_IDS.index("set_clip_audio") == NLE_OPERATION_IDS.index("set_effect") + 1
    assert NleCommandKind("set_clip_audio") is NleCommandKind.SET_CLIP_AUDIO
    assert tuple(member.value for member in NleCommandKind) == NLE_OPERATION_IDS


def test_the_payload_is_closed_to_its_five_members() -> None:
    command = decode_nle_command(_command())
    assert command.kind is NleCommandKind.SET_CLIP_AUDIO
    assert command.to_wire() == _command()
    complete = {"clip_id": MAIN, **ADJUSTED}
    for payload in (
        {key: value for key, value in complete.items() if key != "muted"},
        {**complete, "pan": 0},
    ):
        with pytest.raises(TimelineCommandError) as raised:
            decode_nle_command({"kind": "set_clip_audio", "payload": payload})
        assert raised.value.code == "invalid_command"


# GUARD: name the complete deferred vocabulary; sampling it lets an omitted name escape refusal.
@pytest.mark.parametrize(
    "name",
    [
        "create_audio_track",
        "insert_audio",
        "import_audio",
        "replace_audio",
        "link_audio",
        "unlink_audio",
        "set_gain",
        "set_pan",
        "set_mute",
        "set_solo",
        "set_envelope",
        "set_waveform",
        "mix_audio",
        "h3.authoring.audio.command.v1.gain",
    ],
)
def test_the_independent_audio_names_stay_deferred(name: str) -> None:
    with pytest.raises(TimelineCommandError) as raised:
        decode_nle_command({"kind": name, "payload": {"clip_id": MAIN}})
    assert raised.value.code == "audio_editing_deferred"


def test_the_command_sets_all_four_values_and_the_identity_is_written_absent() -> None:
    adjusted = _apply(_snapshot(), _command())
    assert _audio(adjusted) == ClipAudio(-600, False, 12, 12)
    muted = _apply(adjusted, _command(muted=True, fade_in_frames=0))
    assert _audio(muted) == ClipAudio(-600, True, 0, 12)
    reset = _apply(muted, _command(gain_mb=0, muted=False, fade_in_frames=0, fade_out_frames=0))
    assert _audio(reset) == IDENTITY_CLIP_AUDIO
    assert "audio" not in next(clip.to_wire() for clip in reset.clips if clip.clip_id == MAIN)
    # Every other clip is untouched.
    assert [clip.to_wire() for clip in reset.clips] == [
        clip.to_wire() for clip in _snapshot().clips
    ]


def test_the_bounds_are_the_member_bounds_and_the_fades_must_fit_the_clip() -> None:
    accepted = (
        _command(gain_mb=-6_000),
        _command(gain_mb=1_200),
        _command(fade_in_frames=0, fade_out_frames=48),
        _command(fade_in_frames=24, fade_out_frames=24),
    )
    for command in accepted:
        assert _audio(_apply(_snapshot(), command)) != IDENTITY_CLIP_AUDIO, command
    # Each refusal names the member it refuses: the command checks its own payload, so a value
    # outside the member's bounds never reaches the decoder, whose refusal would name the clip.
    refused = (
        (_command(gain_mb=-6_001), "gain_mb is outside its integer bounds"),
        (_command(gain_mb=1_201), "gain_mb is outside its integer bounds"),
        (_command(gain_mb=True), "gain_mb is outside its integer bounds"),
        # A float never reaches the member check: the payload is not canonical JSON.
        (_command(gain_mb=-600.0), "command payload is not bounded canonical JSON"),
        (_command(muted=0), "muted must be a boolean"),
        (_command(fade_in_frames=-1), "fade_in_frames is outside its integer bounds"),
        (_command(fade_out_frames=241), "fade_out_frames is outside its integer bounds"),
        (_command(fade_in_frames=25, fade_out_frames=24), "the fades exceed the clip duration"),
        (_command("bad handle"), "clip_id must be a bounded identifier"),
    )
    for command, reason in refused:
        assert _refusal(command) == f"invalid_command: {reason}", command
    # The sum rule reads the clip's length, not its end: on a 24-frame clip placed at frame 24
    # the fades may fill the clip and one frame more is refused for the same reason.
    placed = _short_main(24)
    assert _audio(_apply(placed, _command())) == ClipAudio(-600, False, 12, 12)
    assert _refusal(_command(fade_in_frames=13), placed) == (
        "invalid_command: the fades exceed the clip duration"
    )


@pytest.mark.parametrize("clip_id", ["clip-video-overlay", "clip-image", "clip-title"])
def test_only_a_clip_whose_source_carries_bound_audio_takes_a_value(clip_id: str) -> None:
    # The command refuses it itself, before the result reaches the decoder's admission rule. The
    # overlay video is the case that needs the bound-audio term: its source is a video.
    assert _refusal(_command(clip_id)) == (
        "invalid_command: only a clip whose source has bound audio is adjusted"
    )
    identity = _command(clip_id, gain_mb=0, fade_in_frames=0, fade_out_frames=0)
    assert _audio(_apply(_snapshot(), identity), clip_id) == IDENTITY_CLIP_AUDIO


def test_an_unknown_clip_and_a_locked_track_are_refused() -> None:
    assert _code(lambda: _apply(_snapshot(), _command("clip-absent"))) == "invalid_command"
    wire = _wire()
    next(track for track in wire["tracks"] if track["track_id"] == "track-primary")["locked"] = True
    assert _code(lambda: _apply(_snapshot(wire), _command())) == "invalid_command"


def _transaction_v1(
    snapshot: PublicCompositionSnapshot, request_id: str, *commands: dict[str, object]
) -> Any:
    return decode_timeline_transaction(
        {
            "schema": TIMELINE_TRANSACTION_SCHEMA,
            "request_id": request_id,
            "transaction_id": f"tx-{request_id}",
            "workspace_handle": snapshot.workspace_handle,
            "expected_workspace_revision": snapshot.workspace_revision,
            "expected_timeline_revision": snapshot.timeline_revision,
            "expected_timeline_fingerprint": snapshot.timeline_fingerprint,
            "commands": list(commands),
        }
    )


def _rebase(base_fingerprint: str, *commands: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "rebase_transaction",
        "payload": {"base_timeline_fingerprint": base_fingerprint, "commands": list(commands)},
    }


def test_history_undoes_and_redoes_the_command() -> None:
    state = TimelineHistoryState.initialize(_snapshot())
    state, receipt = apply_timeline_transaction(
        state, _transaction_v1(state.snapshot, "a", _command())
    )
    assert receipt.affected_ids == (MAIN,)
    assert _audio(state.snapshot) == ClipAudio(-600, False, 12, 12)
    undo: dict[str, object] = {
        "kind": "undo",
        "payload": {"history_cursor": receipt.history_cursor},
    }
    state, undone = apply_timeline_transaction(state, _transaction_v1(state.snapshot, "u", undo))
    assert _audio(state.snapshot) == IDENTITY_CLIP_AUDIO
    redo: dict[str, object] = {
        "kind": "redo",
        "payload": {"history_cursor": undone.history_cursor},
    }
    state, _ = apply_timeline_transaction(state, _transaction_v1(state.snapshot, "r", redo))
    assert _audio(state.snapshot) == ClipAudio(-600, False, 12, 12)


#: Concurrent edits made after the base the rebased command was written against. Another property
#: of the same clip leaves the projection the command reads; its place, its length or its audio
#: does not. Applied to `_short_main()`; a change of track alone first moves the overlay video
#: out of the way.
CONCURRENT: tuple[tuple[str, tuple[dict[str, object], ...], str | None], ...] = (
    (
        "another_property",
        (
            {
                "kind": "set_opacity_blend",
                "payload": {"clip_id": MAIN, "opacity_bp": 7_000, "blend": "screen"},
            },
        ),
        None,
    ),
    (
        "another_clip",
        (
            {
                "kind": "trim_clip",
                "payload": {"clip_id": "clip-image", "edge": "end", "delta_frames": -1},
            },
        ),
        None,
    ),
    (
        "duration",
        ({"kind": "trim_clip", "payload": {"clip_id": MAIN, "edge": "end", "delta_frames": -1}},),
        "rebase_conflict",
    ),
    (
        "start",
        (
            {
                "kind": "move_clip",
                "payload": {
                    "clip_id": MAIN,
                    "delta_frames": 24,
                    "target_track_id": "track-primary",
                },
            },
        ),
        "rebase_conflict",
    ),
    (
        "track",
        (
            {
                "kind": "move_clip",
                "payload": {
                    "clip_id": "clip-video-overlay",
                    "delta_frames": 24,
                    "target_track_id": "track-video",
                },
            },
            {
                "kind": "move_clip",
                "payload": {"clip_id": MAIN, "delta_frames": 0, "target_track_id": "track-video"},
            },
        ),
        "rebase_conflict",
    ),
    ("audio", (_command(gain_mb=-1_200),), "rebase_conflict"),
)


@pytest.mark.parametrize(
    ("name", "concurrent", "code"), CONCURRENT, ids=[row[0] for row in CONCURRENT]
)
def test_a_rebase_over_a_concurrent_edit_conflicts_only_where_the_clip_s_extent_or_audio_moved(
    name: str, concurrent: tuple[dict[str, object], ...], code: str | None
) -> None:
    initial = TimelineHistoryState.initialize(_short_main())
    base = initial.snapshot.timeline_fingerprint
    current, _ = apply_timeline_transaction(
        initial, _transaction_v1(initial.snapshot, f"{name}-base", *concurrent)
    )
    rebase = _transaction_v1(current.snapshot, f"{name}-rebase", _rebase(base, _command()))
    if code is None:
        rebased, _ = apply_timeline_transaction(current, rebase)
        assert _audio(rebased.snapshot) == ClipAudio(-600, False, 12, 12)
    else:
        assert _code(lambda: apply_timeline_transaction(current, rebase)) == code


def _authoring(snapshot: PublicCompositionSnapshot) -> NleAuthoringState:
    return create_nle_authoring_state(
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


def _transaction_v2(
    state: TimelineHistoryStateV2, request_id: str, *commands: dict[str, object]
) -> dict[str, object]:
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
        "commands": list(commands),
    }


def test_the_authoring_history_applies_undoes_and_rebases_the_command() -> None:
    state = TimelineHistoryStateV2.initialize(_authoring(_snapshot()))
    applied, receipt = apply_timeline_transaction_v2(state, _transaction_v2(state, "a", _command()))
    assert _audio(applied.authoring) == ClipAudio(-600, False, 12, 12)
    undo: dict[str, object] = {
        "kind": "undo",
        "payload": {"history_cursor": receipt.history_cursor},
    }
    undone, _ = apply_timeline_transaction_v2(applied, _transaction_v2(applied, "u", undo))
    assert _audio(undone.authoring) == IDENTITY_CLIP_AUDIO

    state = TimelineHistoryStateV2.initialize(_authoring(_short_main()))
    base = state.authoring.timeline_fingerprint
    for name, concurrent, code in CONCURRENT:
        current, _ = apply_timeline_transaction_v2(
            state, _transaction_v2(state, f"v2-{name}-base", *concurrent)
        )
        rebase = _transaction_v2(current, f"v2-{name}-rebase", _rebase(base, _command()))
        if code is None:
            rebased, _ = apply_timeline_transaction_v2(current, rebase)
            assert _audio(rebased.authoring) == ClipAudio(-600, False, 12, 12), name
        else:
            refused = _code(partial(apply_timeline_transaction_v2, current, rebase))
            assert refused == code, name


def _base_wire(name: str) -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / BASE_FIXTURE_NAMES[name]
    snapshot: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["snapshot"]
    return snapshot


def test_the_corpus_states_both_command_rows_on_the_tone_base() -> None:
    corpus = build_corpus()
    assert len(corpus.cases) == 334
    assert corpus.commands().count("set_clip_audio") == 1
    book = build_recipes(corpus)
    accepted = book.by_id["command.set_clip_audio.accepted"]
    refused = book.by_id["command.set_clip_audio.refused"]
    assert accepted.base == refused.base == CLIP_AUDIO_BASE
    # The rows' payloads exactly. The refused fades fit the member's bound one by one and exceed
    # the base's 248-frame clip together, so the command's own sum rule is what refuses the row.
    assert accepted.command is not None and refused.command is not None
    adjusted = {"clip_id": "clip-main", "gain_mb": -600, "muted": False}
    assert dict(accepted.command.payload) == {
        **adjusted,
        "fade_in_frames": 12,
        "fade_out_frames": 12,
    }
    assert dict(refused.command.payload) == {
        **adjusted,
        "fade_in_frames": 200,
        "fade_out_frames": 100,
    }
    assert (refused.refusal_code, refused.refusal_layer, refused.renders) == (
        "invalid_command",
        "command",
        False,
    )
    before, after = command_phases(accepted, _base_wire(CLIP_AUDIO_BASE)).wires()
    assert "audio_levels" in required_landmarks(accepted, after)
    levels_before = {level.label: level.level_ppm for level in derive_audio_levels(before)}
    levels_after = {level.label: level.level_ppm for level in derive_audio_levels(after)}
    # Before: the start, middle and end windows of the one owner run, at the tone's own level.
    # After: the fades add their quarter windows, and the steady middle is 6 dB down.
    assert levels_before == {
        "clip-main@2560": 1_000_000,
        "clip-main@247520": 1_000_000,
        "clip-main@492480": 1_000_000,
    }
    assert set(levels_before) < set(levels_after)
    assert len(levels_after) == 9
    assert levels_after["clip-main@247520"] == round(10 ** (-600 / 2_000) * 1_000_000)
    setup = run_setup(refused, _base_wire(CLIP_AUDIO_BASE))
    assert setup.state is not None
    assert refused.command is not None
    with pytest.raises(TimelineCommandError) as raised:
        transact(setup.state, refused.command.kind, dict(refused.command.payload), "subject")
    assert str(raised.value) == "invalid_command: the fades exceed the clip duration"
