from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.recompute_closure import (
    RecomputeDisposition,
    RecomputePlan,
    RecomputePlanningError,
    plan_recompute,
)
from comfyui_h3_context.core.segment_workspace import (
    MAX_WORKSPACE_SEGMENTS,
    AcceptedIntentAuthority,
    SegmentContextManifest,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    create_workspace,
    derive_segment_manifests,
)


def _fingerprint(character: str) -> str:
    return "sha256:" + character * 64


def _producer_fingerprint(segment: SegmentDeclaration) -> str:
    return canonical_fingerprint(segment.to_wire())


def _segment(
    segment_id: str,
    *,
    relation: SegmentRelationKind = SegmentRelationKind.INDEPENDENT,
    predecessor: str | None = None,
    settings_digest_char: str = "d",
) -> SegmentDeclaration:
    return SegmentDeclaration(
        segment_id=segment_id,
        task_mode=TaskMode.T2VA,
        source_id="source.catalog.1",
        reference_ids=(),
        duration=SegmentDuration.from_frame_count(124),
        relation=relation,
        predecessor_segment_id=predecessor,
        accepted_intent_fingerprint=_fingerprint("a"),
        semantic_receipt_fingerprint=_fingerprint("b"),
        profile_fingerprint=_fingerprint("1"),
        reference_registry_fingerprint=_fingerprint("2"),
        native_binding_fingerprint=_fingerprint("c"),
        producer_settings_fingerprint=_fingerprint(settings_digest_char),
    )


def _manifests(
    segments: tuple[SegmentDeclaration, ...],
) -> tuple[SegmentContextManifest, ...]:
    workspace = create_workspace(
        workspace_id="workspace.alpha",
        segments=segments,
        accepted_intent_authorities=tuple(
            AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
            for item in segments
        ),
    )
    return derive_segment_manifests(workspace)


def _replace_manifest(manifest: SegmentContextManifest, **changes: Any) -> SegmentContextManifest:
    return replace(manifest, manifest_fingerprint=None, **changes)


def _dispositions(plan: RecomputePlan) -> tuple[RecomputeDisposition, ...]:
    return tuple(item.disposition for item in plan.decisions)


def test_no_producer_change_is_all_clean_despite_workspace_projection_change() -> None:
    previous = _manifests((_segment("s1"), _segment("s2")))
    current = tuple(
        _replace_manifest(
            item,
            workspace_revision=2,
            workspace_fingerprint=_fingerprint("f"),
        )
        for item in previous
    )

    plan = plan_recompute(previous, current)

    assert _dispositions(plan) == (
        RecomputeDisposition.CLEAN,
        RecomputeDisposition.CLEAN,
    )
    assert plan.mandatory_segment_ids == ()
    assert plan.requires_full_recompute is False


def test_future_only_edit_cannot_dirty_earlier_producers() -> None:
    previous = _manifests(
        (
            _segment("s1"),
            _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
            _segment("s3", relation=SegmentRelationKind.PREDECESSOR, predecessor="s2"),
        )
    )
    current = _manifests(
        (
            _segment("s1"),
            _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
            _segment(
                "s3",
                relation=SegmentRelationKind.PREDECESSOR,
                predecessor="s2",
                settings_digest_char="e",
            ),
        )
    )

    plan = plan_recompute(previous, current)

    assert _dispositions(plan) == (
        RecomputeDisposition.CLEAN,
        RecomputeDisposition.CLEAN,
        RecomputeDisposition.DIRTY_SELF,
    )
    assert plan.decisions[2].reason_codes == ("producer_fingerprint_changed",)


def test_only_declared_downstream_closure_is_dirtied_and_cut_stops_propagation() -> None:
    previous = _manifests(
        (
            _segment("s1"),
            _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
            _segment("s3", relation=SegmentRelationKind.ADJACENT_PAIR, predecessor="s2"),
            _segment("s4", relation=SegmentRelationKind.CUT),
            _segment("s5", relation=SegmentRelationKind.PREDECESSOR, predecessor="s4"),
            _segment("s6", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
        )
    )
    current = _manifests(
        (
            _segment("s1", settings_digest_char="e"),
            _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
            _segment("s3", relation=SegmentRelationKind.ADJACENT_PAIR, predecessor="s2"),
            _segment("s4", relation=SegmentRelationKind.CUT),
            _segment("s5", relation=SegmentRelationKind.PREDECESSOR, predecessor="s4"),
            _segment("s6", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
        )
    )

    plan = plan_recompute(previous, current)

    assert _dispositions(plan) == (
        RecomputeDisposition.DIRTY_SELF,
        RecomputeDisposition.DIRTY_UPSTREAM,
        RecomputeDisposition.DIRTY_UPSTREAM,
        RecomputeDisposition.CLEAN,
        RecomputeDisposition.CLEAN,
        RecomputeDisposition.DIRTY_UPSTREAM,
    )
    assert plan.decisions[1].triggering_segment_ids == ("s1",)
    assert plan.decisions[2].triggering_segment_ids == ("s1",)
    assert plan.decisions[5].triggering_segment_ids == ("s1",)


def test_reset_is_a_new_root_and_adjacent_pair_binds_only_declared_pair() -> None:
    previous = _manifests(
        (
            _segment("s1"),
            _segment("s2", relation=SegmentRelationKind.RESET),
            _segment("s3", relation=SegmentRelationKind.ADJACENT_PAIR, predecessor="s2"),
        )
    )
    first_current = _manifests(
        (
            _segment("s1", settings_digest_char="e"),
            _segment("s2", relation=SegmentRelationKind.RESET),
            _segment("s3", relation=SegmentRelationKind.ADJACENT_PAIR, predecessor="s2"),
        )
    )
    first_plan = plan_recompute(previous, first_current)
    assert _dispositions(first_plan) == (
        RecomputeDisposition.DIRTY_SELF,
        RecomputeDisposition.CLEAN,
        RecomputeDisposition.CLEAN,
    )

    pair_current = _manifests(
        (
            _segment("s1"),
            _segment("s2", relation=SegmentRelationKind.RESET, settings_digest_char="f"),
            _segment("s3", relation=SegmentRelationKind.ADJACENT_PAIR, predecessor="s2"),
        )
    )
    pair_plan = plan_recompute(previous, pair_current)
    assert _dispositions(pair_plan) == (
        RecomputeDisposition.CLEAN,
        RecomputeDisposition.DIRTY_SELF,
        RecomputeDisposition.DIRTY_UPSTREAM,
    )


def test_missing_predecessor_blocks_exact_segment_and_its_downstream() -> None:
    previous = _manifests((_segment("s1"), _segment("s2")))
    missing = _replace_manifest(
        previous[0],
        relation=SegmentRelationKind.PREDECESSOR,
        dependency_segment_ids=("missing",),
        producer_fingerprint=_producer_fingerprint(
            _segment(
                "s1",
                relation=SegmentRelationKind.PREDECESSOR,
                predecessor="missing",
            )
        ),
    )
    downstream = _replace_manifest(
        previous[1],
        relation=SegmentRelationKind.PREDECESSOR,
        dependency_segment_ids=("s1",),
        ancestry_root_segment_id="s1",
        ancestry_depth=1,
        producer_fingerprint=_producer_fingerprint(
            _segment(
                "s2",
                relation=SegmentRelationKind.PREDECESSOR,
                predecessor="s1",
            )
        ),
    )

    plan = plan_recompute(previous, (missing, downstream))

    assert _dispositions(plan) == (
        RecomputeDisposition.BLOCKED_MISSING_PREDECESSOR,
        RecomputeDisposition.BLOCKED_MISSING_PREDECESSOR,
    )
    assert plan.decisions[0].reason_codes == ("missing_predecessor",)
    assert plan.decisions[1].triggering_segment_ids == ("s1",)


def test_structural_or_identity_incompatibility_requires_full_recompute() -> None:
    previous = _manifests((_segment("s1"), _segment("s2")))
    different_workspace = tuple(
        _replace_manifest(item, workspace_id="workspace.beta") for item in previous
    )
    mismatch = plan_recompute(previous, different_workspace)
    assert mismatch.requires_full_recompute is True
    assert _dispositions(mismatch) == (
        RecomputeDisposition.REQUIRES_FULL_RECOMPUTE,
        RecomputeDisposition.REQUIRES_FULL_RECOMPUTE,
    )
    assert mismatch.decisions[0].reason_codes == ("workspace_identity_changed",)

    forward_dependency = _replace_manifest(
        previous[0],
        relation=SegmentRelationKind.PREDECESSOR,
        dependency_segment_ids=("s2",),
        producer_fingerprint=_producer_fingerprint(
            _segment(
                "s1",
                relation=SegmentRelationKind.PREDECESSOR,
                predecessor="s2",
            )
        ),
    )
    cyclic = plan_recompute(previous, (forward_dependency, previous[1]))
    assert cyclic.requires_full_recompute is True
    assert cyclic.decisions[0].reason_codes == ("invalid_dependency_graph",)

    tampered = _manifests((_segment("s1"),))[0]
    object.__setattr__(tampered, "producer_fingerprint", _fingerprint("9"))
    tamper_plan = plan_recompute(previous[:1], (tampered,))
    assert tamper_plan.requires_full_recompute is True
    assert tamper_plan.decisions[0].reason_codes == ("tampered_manifest",)

    accepted_newer = tuple(
        _replace_manifest(
            item,
            workspace_revision=2,
            workspace_fingerprint=_fingerprint("8"),
        )
        for item in previous
    )
    stale = plan_recompute(accepted_newer, previous)
    assert stale.requires_full_recompute is True
    assert stale.decisions[0].reason_codes == ("stale_current_manifest",)


def test_unsafe_selection_returns_exact_required_enlargement() -> None:
    previous = _manifests(
        (
            _segment("s1"),
            _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
            _segment("s3", relation=SegmentRelationKind.PREDECESSOR, predecessor="s2"),
        )
    )
    current = _manifests(
        (
            _segment("s1", settings_digest_char="e"),
            _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
            _segment("s3", relation=SegmentRelationKind.PREDECESSOR, predecessor="s2"),
        )
    )

    blocked = plan_recompute(
        previous,
        current,
        requested_segment_ids=("s1",),
    )
    assert blocked.selection_safe is False
    assert blocked.missing_required_segment_ids == ("s2", "s3")
    assert blocked.required_enlargement_segment_ids == ("s2", "s3")

    safe = plan_recompute(
        previous,
        current,
        requested_segment_ids=("s1", "s2", "s3"),
    )
    assert safe.selection_safe is True
    assert safe.missing_required_segment_ids == ()


def test_unbounded_or_unknown_selection_fails_closed_without_content() -> None:
    manifests = _manifests((_segment("s1"),))
    with pytest.raises(RecomputePlanningError, match="manifest_limit"):
        plan_recompute(manifests * (MAX_WORKSPACE_SEGMENTS + 1), manifests)
    with pytest.raises(RecomputePlanningError, match="unknown_requested_segment"):
        plan_recompute(manifests, manifests, requested_segment_ids=("private/path",))
