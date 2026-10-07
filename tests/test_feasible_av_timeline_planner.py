from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from comfyui_h3_context.core import (
    AssetRole,
    FeasibleAVTimelineRequest,
    FrameGridPolicy,
    FrameRoundingPolicy,
    LipState,
    MediaKind,
    PlannerEvent,
    PlannerEventKind,
    PlannerRelation,
    PlannerRelationKind,
    ReferenceAnchor,
    ReferenceAsset,
    ReferenceRegistry,
    ShotDraft,
    TaskMode,
    TaskModeRetentionReport,
    TaskModeRetentionRequest,
    TimelinePlannerStatus,
    TimePoint,
    build_feasible_av_timeline,
    build_reference_registry,
    build_task_mode_retention_report,
)

ROOT = Path(__file__).resolve().parents[1]


def _registry(*assets: ReferenceAsset) -> ReferenceRegistry:
    return build_reference_registry(assets)


def _asset(asset_id: str, kind: MediaKind, role: AssetRole, order: int) -> ReferenceAsset:
    return ReferenceAsset(asset_id, kind, role, order)


def _report(mode: TaskMode, registry: ReferenceRegistry) -> TaskModeRetentionReport:
    return build_task_mode_retention_report(TaskModeRetentionRequest.from_user_mode(mode, registry))


def _event(
    event_id: str,
    kind: PlannerEventKind,
    start: str,
    end: str,
    *,
    text: str | None = None,
    exact: bool = False,
    lip_state: LipState | None = None,
    asset_ids: tuple[str, ...] = (),
    hard: bool = False,
) -> PlannerEvent:
    return PlannerEvent(
        event_id,
        kind,
        TimePoint.from_text(start),
        TimePoint.from_text(end),
        text=text,
        exact=exact,
        lip_state=lip_state,
        asset_ids=asset_ids,
        hard=hard,
    )


def _request(
    mode: TaskMode,
    registry: ReferenceRegistry,
    *,
    duration: str = "8",
    events: tuple[PlannerEvent, ...] = (),
    anchors: tuple[ReferenceAnchor, ...] = (),
    shots: tuple[ShotDraft, ...] = (),
    relations: tuple[PlannerRelation, ...] = (),
    reference_order: tuple[str, ...] = (),
    frame_grid: FrameGridPolicy | None = None,
) -> FeasibleAVTimelineRequest:
    return FeasibleAVTimelineRequest(
        _report(mode, registry),
        TimePoint.from_text(duration),
        frame_grid or FrameGridPolicy(24, 1),
        reference_registry=registry,
        events=events,
        anchors=anchors,
        shots=shots,
        relations=relations,
        reference_order=reference_order,
    )


def test_all_modes_produce_contiguous_frame_grid_plans_and_anchor_rules() -> None:
    cases = (
        (TaskMode.T2VA, ReferenceRegistry.empty(), ()),
        (
            TaskMode.I2VA,
            _registry(_asset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1)),
            (ReferenceAnchor("anchor.first", "first_frame", "first", TimePoint.from_text("0")),),
        ),
        (
            TaskMode.FL2VA,
            _registry(
                _asset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),
                _asset("last", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2),
            ),
            (
                ReferenceAnchor("anchor.first", "first_frame", "first", TimePoint.from_text("0")),
                ReferenceAnchor("anchor.last", "last_frame", "last", TimePoint.from_text("8")),
            ),
        ),
        (
            TaskMode.L2VA,
            _registry(_asset("last", MediaKind.IMAGE, AssetRole.LAST_FRAME, 1)),
            (ReferenceAnchor("anchor.last", "last_frame", "last", TimePoint.from_text("8")),),
        ),
        (
            TaskMode.REF2VA,
            _registry(_asset("picture", MediaKind.IMAGE, AssetRole.REFERENCE, 1)),
            (ReferenceAnchor("anchor.picture", "reference", "picture", TimePoint.from_text("2")),),
        ),
    )
    for mode, registry, anchors in cases:
        result = build_feasible_av_timeline(_request(mode, registry, anchors=anchors))
        assert result.status in {TimelinePlannerStatus.COMPLETE, TimelinePlannerStatus.PARTIAL}
        assert result.plan is not None
        assert result.plan.shots[0].start.seconds == Decimal("0")
        assert result.plan.shots[-1].end.seconds == Decimal("8")
        assert all(
            shot.start.seconds == result.plan.frame_grid.snap(shot.start.seconds)
            and shot.end.seconds == result.plan.frame_grid.snap(shot.end.seconds)
            for shot in result.plan.shots
        )


def test_fl2va_requires_first_and_last_landing_in_final_shot() -> None:
    registry = _registry(
        _asset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),
        _asset("last", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2),
    )
    result = build_feasible_av_timeline(
        _request(
            TaskMode.FL2VA,
            registry,
            anchors=(
                ReferenceAnchor("anchor.first", "first_frame", "first", TimePoint.from_text("0")),
                ReferenceAnchor("anchor.last", "last_frame", "last", TimePoint.from_text("8")),
            ),
            shots=(
                ShotDraft("shot.1", TimePoint.from_text("0"), TimePoint.from_text("4")),
                ShotDraft("shot.2", TimePoint.from_text("4"), TimePoint.from_text("8")),
            ),
        )
    )
    assert result.is_valid
    assert result.plan is not None
    assert result.plan.shots[-1].anchor_ids == ("anchor.last",)


def test_exact_text_and_lip_state_are_preserved_and_shot_metadata_is_ordered() -> None:
    registry = ReferenceRegistry.empty()
    events = (
        _event(
            "dialogue.1",
            PlannerEventKind.DIALOGUE,
            "1",
            "2",
            text="Keep this exact!",
            exact=True,
            lip_state=LipState.OPEN,
            hard=True,
        ),
        _event(
            "text.1",
            PlannerEventKind.VISIBLE_TEXT,
            "5",
            "6",
            text="營業中",
            exact=True,
            hard=True,
        ),
    )
    result = build_feasible_av_timeline(
        _request(
            TaskMode.T2VA,
            registry,
            events=events,
            shots=(
                ShotDraft("shot.1", TimePoint.from_text("0"), TimePoint.from_text("4")),
                ShotDraft("shot.2", TimePoint.from_text("4"), TimePoint.from_text("8")),
            ),
        )
    )
    assert result.is_valid
    assert result.plan is not None
    assert result.plan.shots[0].cut_time is None
    assert result.plan.shots[1].cut_time is not None
    assert result.plan.events[0].text == "Keep this exact!"
    assert result.plan.events[0].lip_state is LipState.OPEN


def test_cross_shot_dialogue_requires_explicit_continuation_relation() -> None:
    registry = ReferenceRegistry.empty()
    event = _event(
        "dialogue.1", PlannerEventKind.DIALOGUE, "3", "5", text="Across", exact=True, hard=True
    )
    shots = (
        ShotDraft("shot.1", TimePoint.from_text("0"), TimePoint.from_text("4")),
        ShotDraft("shot.2", TimePoint.from_text("4"), TimePoint.from_text("8")),
    )
    blocked = build_feasible_av_timeline(
        _request(TaskMode.T2VA, registry, events=(event,), shots=shots)
    )
    assert blocked.status is TimelinePlannerStatus.BLOCKED
    assert any(
        item.code == "cross_shot_dialogue_without_continuation" for item in blocked.diagnostics
    )
    relation = PlannerRelation(
        "relation.dialogue",
        PlannerRelationKind.CONTINUES_ACROSS_CUT,
        ("dialogue.1",),
        ("dialogue.1",),
    )
    accepted = build_feasible_av_timeline(
        _request(TaskMode.T2VA, registry, events=(event,), shots=shots, relations=(relation,))
    )
    assert accepted.is_valid


def test_fixed_24_reject_tolerates_only_binary_float_spelling_drift() -> None:
    epsilon = Decimal("0.000000000001")
    grid = FrameGridPolicy(24, 1, FrameRoundingPolicy.REJECT)
    for frame_delta in (epsilon, -epsilon):
        seconds = (Decimal(124) + frame_delta) / Decimal(24)
        assert grid.is_aligned(seconds)
        assert grid.snap(seconds) == seconds

    outside = (Decimal(124) + epsilon + Decimal("0.000000000001")) / Decimal(24)
    fractional = Decimal("124.5") / Decimal(24)
    assert not grid.is_aligned(outside)
    assert not grid.is_aligned(fractional)

    for policy in (
        FrameRoundingPolicy.FLOOR,
        FrameRoundingPolicy.CEIL,
        FrameRoundingPolicy.NEAREST,
    ):
        unchanged = FrameGridPolicy(24, 1, policy)
        almost = (Decimal(124) - epsilon) / Decimal(24)
        assert not unchanged.is_aligned(almost)
        assert unchanged.snap(almost) != almost
    non_h3_grid = FrameGridPolicy(30, 1, FrameRoundingPolicy.REJECT)
    almost_30 = (Decimal(124) + epsilon) / Decimal(30)
    assert not non_h3_grid.is_aligned(almost_30)


def test_audio_layers_remain_separate_and_reference_order_is_stable() -> None:
    registry = _registry(
        _asset("picture", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
        _asset("audio", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 2),
    )
    events = (
        _event("ambience.1", PlannerEventKind.AMBIENCE, "0", "8", asset_ids=("audio",)),
        _event("music.1", PlannerEventKind.MUSIC, "0", "8", asset_ids=("audio",)),
        _event("sfx.1", PlannerEventKind.SFX, "2", "3", asset_ids=("audio",)),
    )
    result = build_feasible_av_timeline(
        _request(
            TaskMode.REF2VA,
            registry,
            events=events,
            reference_order=("picture", "audio"),
        )
    )
    assert result.is_valid
    assert result.plan is not None
    assert result.plan.reference_order == ("picture", "audio")
    assert {item.kind for item in result.plan.events} == {
        PlannerEventKind.AMBIENCE,
        PlannerEventKind.MUSIC,
        PlannerEventKind.SFX,
    }


def test_gap_overlap_and_anchor_mismatch_fail_closed() -> None:
    registry = _registry(_asset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1))
    result = build_feasible_av_timeline(
        _request(
            TaskMode.I2VA,
            registry,
            anchors=(
                ReferenceAnchor("anchor.first", "first_frame", "first", TimePoint.from_text("1")),
            ),
            shots=(
                ShotDraft("shot.1", TimePoint.from_text("0"), TimePoint.from_text("5")),
                ShotDraft("shot.2", TimePoint.from_text("4"), TimePoint.from_text("8")),
            ),
        )
    )
    assert result.status is TimelinePlannerStatus.BLOCKED
    assert {item.code for item in result.diagnostics} >= {"anchor_not_at_start", "shot_overlap"}


def test_deterministic_fingerprint_and_schema_fixture() -> None:
    request = _request(TaskMode.T2VA, ReferenceRegistry.empty())
    first = build_feasible_av_timeline(request)
    second = build_feasible_av_timeline(request)
    assert first.plan is not None and second.plan is not None
    assert first.plan.fingerprint == second.plan.fingerprint
    schema = json.loads(
        (ROOT / "governance/contracts/feasible_av_timeline_planner_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["$id"] == (
        "comfyui-h3-context://contracts/feasible_av_timeline_planner_v1.schema.json"
    )
    fixture = json.loads(
        (ROOT / "tests/fixtures/m13_06_feasible_av_timeline.json").read_text(encoding="utf-8")
    )
    assert fixture["fixture_status"] == "static_feasible_timeline_contract"
    assert fixture["plan"]["schema"] == "h3.feasible_av_timeline_planner.v1"
