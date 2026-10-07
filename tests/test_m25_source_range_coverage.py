from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest

from comfyui_h3_context.core.composition_contract import (
    CompositionContractError,
    PublicCompositionSnapshot,
    decode_public_snapshot,
    public_snapshot_fingerprint,
    resolve_composition,
    resolve_source_interval_coverage,
)
from comfyui_h3_context.core.timeline_authoring import (
    NleCommand,
    TimelineCommandError,
    apply_nle_command,
    decode_nle_command,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"


def _fixture_wire() -> dict[str, Any]:
    document = cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
    wire = cast(dict[str, Any], copy.deepcopy(document["snapshot"]))
    wire["clips"] = [wire["clips"][0]]
    return wire


def _resign(wire: dict[str, Any]) -> PublicCompositionSnapshot:
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return decode_public_snapshot(wire)


def _set_primary_timing(
    wire: dict[str, Any],
    *,
    time_base_den: int,
    source_frame_count: int,
    landmarks: list[dict[str, int]],
    source_start_frame: int,
    duration_frames: int,
) -> None:
    asset = cast(dict[str, Any], wire["assets"][0])
    asset["source_time_base"] = {"num": 1, "den": time_base_den}
    asset["source_frame_count"] = source_frame_count
    asset["landmarks"] = landmarks
    clip = cast(dict[str, Any], wire["clips"][0])
    clip["source_start_frame"] = source_start_frame
    clip["duration_frames"] = duration_frames


def _noop_edit() -> NleCommand:
    return decode_nle_command(
        {
            "kind": "set_clip_enabled",
            "payload": {"clip_id": "clip-main", "enabled": True},
        }
    )


def _command_rejection(snapshot: PublicCompositionSnapshot) -> str:
    with pytest.raises(TimelineCommandError) as exc_info:
        apply_nle_command(snapshot, _noop_edit())
    return exc_info.value.code


def test_low_rate_interval_uses_output_time_instead_of_source_frame_ordinals() -> None:
    wire = _fixture_wire()
    _set_primary_timing(
        wire,
        time_base_den=1_200,
        source_frame_count=24,
        landmarks=[
            {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 100},
            {"frame_index": 12, "pts": 1_200, "dts": 1_200, "duration_ticks": 100},
            {"frame_index": 23, "pts": 2_300, "dts": 2_300, "duration_ticks": 100},
        ],
        source_start_frame=0,
        duration_frames=48,
    )
    snapshot = _resign(wire)

    assert resolve_source_interval_coverage(
        snapshot.assets[0], 0, 48, snapshot.output.frame_rate
    ) == (Fraction(0), Fraction(2_400))
    assert apply_nle_command(snapshot, _noop_edit()).snapshot.clips[0].duration_frames == 48
    assert resolve_composition(snapshot, 47).layers[0].source_frame == 23


def test_high_rate_interval_rejects_true_end_overrun_before_command_admission() -> None:
    wire = _fixture_wire()
    _set_primary_timing(
        wire,
        time_base_den=6_000,
        source_frame_count=120,
        landmarks=[
            {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 100},
            {"frame_index": 80, "pts": 8_000, "dts": 8_000, "duration_ticks": 100},
            {"frame_index": 119, "pts": 11_900, "dts": 11_900, "duration_ticks": 100},
        ],
        source_start_frame=80,
        duration_frames=24,
    )
    snapshot = _resign(wire)

    assert _command_rejection(snapshot) == "source_range_unavailable"
    with pytest.raises(CompositionContractError, match="source_range_unavailable"):
        resolve_composition(snapshot, 23)


def test_high_rate_interval_accepts_an_exact_source_end() -> None:
    wire = _fixture_wire()
    _set_primary_timing(
        wire,
        time_base_den=6_000,
        source_frame_count=120,
        landmarks=[
            {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 100},
            {"frame_index": 60, "pts": 6_000, "dts": 6_000, "duration_ticks": 100},
            {"frame_index": 119, "pts": 11_900, "dts": 11_900, "duration_ticks": 100},
        ],
        source_start_frame=60,
        duration_frames=24,
    )
    snapshot = _resign(wire)

    assert resolve_source_interval_coverage(
        snapshot.assets[0], 60, 24, snapshot.output.frame_rate
    ) == (Fraction(6_000), Fraction(12_000))
    assert apply_nle_command(snapshot, _noop_edit()).snapshot.clips[0].duration_frames == 24
    assert resolve_composition(snapshot, 23).layers[0].source_pts == 6_000


def test_fractional_tick_overrun_is_not_hidden_by_integer_flooring() -> None:
    wire = _fixture_wire()
    _set_primary_timing(
        wire,
        time_base_den=1_000,
        source_frame_count=2,
        landmarks=[
            {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 40},
            {"frame_index": 1, "pts": 40, "dts": 40, "duration_ticks": 1},
        ],
        source_start_frame=0,
        duration_frames=1,
    )
    snapshot = _resign(wire)

    # One 24-fps frame ends at 41 2/3 source ticks. Flooring that endpoint to the declared end
    # tick 41 would incorrectly admit a real 2/3-tick overrun.
    assert _command_rejection(snapshot) == "source_range_unavailable"
    with pytest.raises(CompositionContractError, match="source_range_unavailable"):
        resolve_source_interval_coverage(snapshot.assets[0], 0, 1, snapshot.output.frame_rate)


def test_vfr_interval_uses_rational_pts_coverage() -> None:
    wire = _fixture_wire()
    _set_primary_timing(
        wire,
        time_base_den=1_000,
        source_frame_count=6,
        landmarks=[
            {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 40},
            {"frame_index": 1, "pts": 40, "dts": 40, "duration_ticks": 80},
            {"frame_index": 2, "pts": 120, "dts": 120, "duration_ticks": 35},
            {"frame_index": 3, "pts": 155, "dts": 155, "duration_ticks": 60},
            {"frame_index": 4, "pts": 215, "dts": 215, "duration_ticks": 45},
            {"frame_index": 5, "pts": 260, "dts": 260, "duration_ticks": 40},
        ],
        source_start_frame=1,
        duration_frames=6,
    )
    snapshot = _resign(wire)

    assert resolve_source_interval_coverage(
        snapshot.assets[0], 1, 6, snapshot.output.frame_rate
    ) == (Fraction(40), Fraction(290))
    assert apply_nle_command(snapshot, _noop_edit()).snapshot.clips[0].duration_frames == 6
    assert resolve_composition(snapshot, 5).layers[0].source_pts == 215


def test_missing_vfr_start_landmark_fails_closed() -> None:
    wire = _fixture_wire()
    _set_primary_timing(
        wire,
        time_base_den=1_000,
        source_frame_count=6,
        landmarks=[
            {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 40},
            {"frame_index": 2, "pts": 80, "dts": 80, "duration_ticks": 55},
            {"frame_index": 5, "pts": 200, "dts": 200, "duration_ticks": 40},
        ],
        source_start_frame=1,
        duration_frames=1,
    )
    snapshot = _resign(wire)

    assert _command_rejection(snapshot) == "source_range_unavailable"
    scene = resolve_composition(snapshot, 0)
    assert scene.layers[0].source_frame is None
    assert ("timing_unavailable", "clip-main") in {
        (blocker.code, blocker.subject_id) for blocker in scene.blockers
    }


def test_empty_admitted_timing_coverage_fails_closed() -> None:
    wire = _fixture_wire()
    _set_primary_timing(
        wire,
        time_base_den=1_000,
        source_frame_count=6,
        landmarks=[],
        source_start_frame=0,
        duration_frames=1,
    )
    snapshot = _resign(wire)

    assert _command_rejection(snapshot) == "source_range_unavailable"
    with pytest.raises(CompositionContractError, match="source_range_unavailable"):
        resolve_source_interval_coverage(snapshot.assets[0], 0, 1, snapshot.output.frame_rate)
    scene = resolve_composition(snapshot, 0)
    assert [blocker.code for blocker in scene.blockers] == [
        "timing_unavailable",
        "embedded_audio_unavailable",
    ]
