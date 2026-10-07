from __future__ import annotations

from dataclasses import replace

import pytest

from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.segment_workspace import (
    MAX_WORKSPACE_SEGMENTS,
    MULTI_SEGMENT_WORKSPACE_SCHEMA,
    SEGMENT_CONTEXT_MANIFEST_SCHEMA,
    AcceptedIntentAuthority,
    MultiSegmentWorkspace,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    SegmentWorkspaceError,
    create_workspace,
    derive_segment_manifests,
    revise_workspace,
)


def _fingerprint(token: str) -> str:
    return f"sha256:{token * 64}"


def _segment(
    segment_id: str,
    *,
    relation: SegmentRelationKind = SegmentRelationKind.INDEPENDENT,
    predecessor: str | None = None,
    intent_digest_char: str = "a",
    settings_digest_char: str = "b",
) -> SegmentDeclaration:
    return SegmentDeclaration(
        segment_id=segment_id,
        task_mode=TaskMode.T2VA,
        source_id=f"source.{segment_id}",
        reference_ids=(),
        duration=SegmentDuration.from_frame_count(90),
        relation=relation,
        predecessor_segment_id=predecessor,
        accepted_intent_fingerprint=_fingerprint(intent_digest_char),
        semantic_receipt_fingerprint=None,
        profile_fingerprint=_fingerprint("1"),
        reference_registry_fingerprint=_fingerprint("2"),
        native_binding_fingerprint=_fingerprint("c"),
        producer_settings_fingerprint=_fingerprint(settings_digest_char),
    )


def _authorities(
    segments: tuple[SegmentDeclaration, ...],
) -> tuple[AcceptedIntentAuthority, ...]:
    return tuple(
        AcceptedIntentAuthority(segment.segment_id, segment.accepted_intent_fingerprint)
        for segment in segments
    )


def _workspace(
    workspace_id: str,
    segments: tuple[SegmentDeclaration, ...],
    *,
    selected_segment_ids: tuple[str, ...] = (),
) -> MultiSegmentWorkspace:
    return create_workspace(
        workspace_id,
        segments,
        accepted_intent_authorities=_authorities(segments),
        selected_segment_ids=selected_segment_ids,
    )


def test_equal_workspace_input_has_byte_stable_workspace_and_manifest_identity() -> None:
    segments = (
        _segment("s1"),
        _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
    )
    first = _workspace("workspace.alpha", segments, selected_segment_ids=("s2",))
    second = _workspace("workspace.alpha", segments, selected_segment_ids=("s2",))

    assert first.schema == MULTI_SEGMENT_WORKSPACE_SCHEMA
    assert first.to_wire_bytes() == second.to_wire_bytes()
    assert first.fingerprint == second.fingerprint

    first_manifests = derive_segment_manifests(first)
    second_manifests = derive_segment_manifests(second)
    assert first_manifests == second_manifests
    assert all(item.schema == SEGMENT_CONTEXT_MANIFEST_SCHEMA for item in first_manifests)
    assert first_manifests[1].dependency_segment_ids == ("s1",)
    assert first_manifests[1].ancestry_root_segment_id == "s1"
    assert first_manifests[1].ancestry_depth == 1


def test_revision_is_atomic_and_rejects_stale_or_invalid_edits() -> None:
    original = _workspace("workspace.alpha", (_segment("s1"),))
    revised_segments = (
        _segment("s1"),
        _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
    )
    revised = revise_workspace(
        original,
        expected_workspace_fingerprint=original.fingerprint,
        accepted_intent_authorities=_authorities(revised_segments),
        segments=revised_segments,
        selected_segment_ids=("s2",),
    )

    assert revised.revision == 2
    assert revised.parent_workspace_fingerprint == original.fingerprint
    assert revised.fingerprint != original.fingerprint
    assert original.revision == 1
    assert tuple(item.segment_id for item in original.segments) == ("s1",)

    with pytest.raises(SegmentWorkspaceError, match="stale_workspace"):
        revise_workspace(
            original,
            expected_workspace_fingerprint=_fingerprint("f"),
            accepted_intent_authorities=_authorities(original.segments),
            selected_segment_ids=("s1",),
        )
    with pytest.raises(SegmentWorkspaceError, match="unknown_selected_segment"):
        revise_workspace(
            original,
            expected_workspace_fingerprint=original.fingerprint,
            accepted_intent_authorities=_authorities(original.segments),
            selected_segment_ids=("missing",),
        )
    assert original.revision == 1
    assert original.parent_workspace_fingerprint is None


def test_manifest_binds_every_producer_fact_and_reset_starts_new_ancestry() -> None:
    workspace = _workspace(
        "workspace.alpha",
        (
            _segment("s1"),
            _segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),
            _segment("s3", relation=SegmentRelationKind.RESET, intent_digest_char="d"),
            _segment(
                "s4",
                relation=SegmentRelationKind.ADJACENT_PAIR,
                predecessor="s3",
                settings_digest_char="e",
            ),
        ),
    )
    manifests = derive_segment_manifests(workspace)

    assert manifests[0].reset_boundary is False
    assert manifests[2].reset_boundary is True
    assert manifests[2].dependency_segment_ids == ()
    assert manifests[2].ancestry_root_segment_id == "s3"
    assert manifests[2].ancestry_depth == 0
    assert manifests[3].dependency_segment_ids == ("s3",)
    assert manifests[3].ancestry_root_segment_id == "s3"
    assert manifests[3].ancestry_depth == 1

    changed = replace(workspace.segments[3], producer_settings_fingerprint=_fingerprint("f"))
    changed_workspace = _workspace("workspace.alpha", (*workspace.segments[:3], changed))
    changed_manifests = derive_segment_manifests(changed_workspace)
    assert tuple(item.producer_fingerprint for item in changed_manifests[:3]) == tuple(
        item.producer_fingerprint for item in manifests[:3]
    )
    assert changed_manifests[3].producer_fingerprint != manifests[3].producer_fingerprint


@pytest.mark.parametrize(
    ("segments", "message"),
    [
        ((_segment("s1"), _segment("s1")), "duplicate_segment_id"),
        (
            (_segment("s2", relation=SegmentRelationKind.PREDECESSOR, predecessor="s1"),),
            "unknown_or_forward_predecessor",
        ),
    ],
)
def test_invalid_dependency_graphs_fail_closed(
    segments: tuple[SegmentDeclaration, ...], message: str
) -> None:
    with pytest.raises(SegmentWorkspaceError, match=message):
        _workspace("workspace.alpha", segments)

    with pytest.raises(SegmentWorkspaceError, match="unexpected_predecessor"):
        _segment("s1", relation=SegmentRelationKind.INDEPENDENT, predecessor="other")


def test_workspace_is_bounded_and_public_projection_is_content_free() -> None:
    workspace = _workspace(
        "workspace.alpha",
        (
            SegmentDeclaration(
                segment_id="s1",
                task_mode=TaskMode.REF2VA,
                source_id="source.catalog.1",
                reference_ids=("reference.image.1", "reference.audio.1"),
                duration=SegmentDuration(duration_milliseconds=6000),
                relation=SegmentRelationKind.CUT,
                predecessor_segment_id=None,
                accepted_intent_fingerprint=_fingerprint("a"),
                semantic_receipt_fingerprint=_fingerprint("b"),
                profile_fingerprint=_fingerprint("1"),
                reference_registry_fingerprint=_fingerprint("2"),
                native_binding_fingerprint=_fingerprint("c"),
                producer_settings_fingerprint=_fingerprint("d"),
            ),
        ),
        selected_segment_ids=("s1",),
    )
    public = workspace.to_public_dict()
    encoded = workspace.to_wire_bytes().decode("utf-8")

    assert derive_segment_manifests(workspace)[0].reset_boundary is True
    assert public == workspace.to_wire()
    assert public["segments"][0]["source_id"] == "source.catalog.1"  # type: ignore[index]
    for forbidden in ("prompt", "media_path", "credential", "provider", "http://", "https://"):
        assert forbidden not in encoded.lower()

    with pytest.raises(SegmentWorkspaceError, match="bounded_identifier"):
        replace(workspace.segments[0], source_id="C:/private/media.png")
    with pytest.raises(SegmentWorkspaceError, match="segment_limit"):
        segments = tuple(_segment(f"s{index}") for index in range(MAX_WORKSPACE_SEGMENTS + 1))
        create_workspace(
            "workspace.too-large",
            segments,
            accepted_intent_authorities=_authorities(segments),
        )


def test_duration_is_authored_once_and_never_holds_a_non_producible_length() -> None:
    """M17-25 / AC-M17-25-04. Duration is the only authored member and the length
    it derives is always producible, so the previous contract's defect -- any
    positive frame count accepted, including the fixtures' own 100, 300 and 180 --
    is unrepresentable rather than merely unused."""

    with pytest.raises(TypeError):
        SegmentDuration()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        SegmentDuration(frame_count=81)  # type: ignore[call-arg]
    with pytest.raises(SegmentWorkspaceError, match="positive_integer"):
        SegmentDuration(duration_milliseconds=True)
    with pytest.raises(SegmentWorkspaceError, match="duration_not_producible"):
        SegmentDuration(duration_milliseconds=1)
    with pytest.raises(SegmentWorkspaceError, match="duration_not_producible"):
        SegmentDuration(duration_milliseconds=200_000)
    with pytest.raises(SegmentWorkspaceError, match="frame_count_out_of_bounds"):
        SegmentDuration.from_frame_count(4)
    with pytest.raises(SegmentWorkspaceError, match="frame_count_out_of_bounds"):
        SegmentDuration.from_frame_count(3601)

    exact = SegmentDuration.from_frame_count(124)
    assert (exact.duration_milliseconds, exact.frame_count, exact.snapped) == (5167, 124, False)
    migrated = SegmentDuration.from_frame_count(100)
    assert (migrated.duration_milliseconds, migrated.frame_count, migrated.snapped) == (
        4167,
        107,
        True,
    )
    assert migrated.to_wire() == {
        "duration_milliseconds": 4167,
        "frame_count": 107,
        "delivered_milliseconds": 4458,
        "snapped": True,
    }
    with pytest.raises(SegmentWorkspaceError, match="sha256_fingerprint"):
        replace(_segment("s1"), accepted_intent_fingerprint="sha256:not-a-digest")


def test_create_and_revise_reject_stale_accepted_intent_authority() -> None:
    segment = _segment("s1")
    stale = (AcceptedIntentAuthority("s1", _fingerprint("f")),)
    with pytest.raises(SegmentWorkspaceError, match="stale_accepted_intent"):
        create_workspace(
            "workspace.alpha",
            (segment,),
            accepted_intent_authorities=stale,
        )

    workspace = _workspace("workspace.alpha", (segment,))
    changed = replace(segment, accepted_intent_fingerprint=_fingerprint("d"))
    with pytest.raises(SegmentWorkspaceError, match="stale_accepted_intent"):
        revise_workspace(
            workspace,
            expected_workspace_fingerprint=workspace.fingerprint,
            segments=(changed,),
            accepted_intent_authorities=_authorities((segment,)),
        )
