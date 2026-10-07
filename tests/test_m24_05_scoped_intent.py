"""Explicit denotation and temporal declarations never arise from legacy defaults."""

from dataclasses import replace
from decimal import Decimal

import pytest

from comfyui_h3_context.core.constraints import TimePoint
from comfyui_h3_context.core.contracts import AssetRole, MediaKind
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.intent_graph import (
    IntentGraph,
    RetentionDomain,
    RetentionRelation,
    RetentionScope,
    SegmentDevelopment,
    TimelineSegment,
    VisualRetentionMarker,
)
from comfyui_h3_context.core.registry import ReferenceAsset, build_reference_registry


def test_legacy_defaults_do_not_mint_current_scope_or_development() -> None:
    relation = RetentionRelation(
        "retain",
        RetentionDomain.VISUAL,
        ("image",),
        "subject",
        VisualRetentionMarker.FULLY_PRESERVED,
    )
    segment = TimelineSegment("shot", TimePoint.from_text("0"), TimePoint.from_text("5"))
    assert relation.scope is RetentionScope.UNSPECIFIED
    assert segment.development is SegmentDevelopment.UNSPECIFIED
    assert relation.to_wire()["scope"] == "unspecified"
    assert segment.to_wire()["development"] == "unspecified"
    graph = IntentGraph(TimePoint.from_text("5"), build_reference_registry(()))
    assert graph.to_wire()["schema"] == "h3.context.intent_graph.v2"


def test_picture_denotation_requires_declared_picture_role() -> None:
    asset = ReferenceAsset("image", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1)
    relation = RetentionRelation(
        "retain",
        RetentionDomain.VISUAL,
        ("image",),
        "image",
        VisualRetentionMarker.FULLY_PRESERVED,
        scope=RetentionScope.PICTURE,
    )
    graph = IntentGraph(
        TimePoint.from_text("5"), build_reference_registry((asset,)), retention=(relation,)
    )
    assert not any(d.location == "retention" for d in graph.validate())
    unqualified = replace(
        graph, registry=build_reference_registry((replace(asset, role=AssetRole.REFERENCE),))
    )
    assert any(d.code == "retention_target_scope_mismatch" for d in unqualified.validate())


def test_domain_scope_mismatch_is_rejected() -> None:
    with pytest.raises(ContractValidationError):
        RetentionRelation(
            "retain",
            RetentionDomain.VISUAL,
            ("image",),
            "subject",
            VisualRetentionMarker.FULLY_PRESERVED,
            scope=RetentionScope.AUDIO_LAYER,
        )


def test_old_retention_confidence_cannot_authorize_unspecified_scope() -> None:
    from comfyui_h3_context.core.contracts import TaskMode
    from comfyui_h3_context.core.task_mode_retention_classifier import (
        ClassifierDisposition,
        ClassifierEvidenceSource,
        RetentionEvidence,
        TaskModeRetentionRequest,
        build_task_mode_retention_report,
    )

    registry = build_reference_registry(
        (ReferenceAsset("image", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
    )
    evidence = RetentionEvidence(
        RetentionDomain.VISUAL,
        VisualRetentionMarker.FULLY_PRESERVED,
        ("image",),
        "subject",
        ClassifierEvidenceSource.USER_DIRECTIVE,
        Decimal(1),
    )
    report = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            TaskMode.REF2VA,
            registry,
            retention_evidence=(evidence,),
        )
    )
    assert report.retention[0].disposition is ClassifierDisposition.ABSTAINED
    explicit = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            TaskMode.REF2VA,
            registry,
            retention_evidence=(replace(evidence, scope=RetentionScope.SUBJECT),),
        )
    )
    assert explicit.retention[0].disposition is ClassifierDisposition.ACCEPTED
    assert explicit.input_fingerprint != report.input_fingerprint


def test_directive_scope_is_part_of_authority_identity() -> None:
    from comfyui_h3_context.core.reference_directives import (
        DirectiveAction,
        DirectiveTargetKind,
        ReferenceDirective,
        RetentionAspect,
    )

    layer = ReferenceDirective(
        "retain",
        DirectiveAction.RETAIN,
        DirectiveTargetKind.AUDIO,
        "track",
        source_asset_ids=("audio",),
        retention_aspects=(RetentionAspect.AUDIO,),
        retention_scope=RetentionScope.AUDIO_LAYER,
    )
    final = replace(layer, retention_scope=RetentionScope.COMPLETE_FINAL_AUDIO_TRACK)
    assert layer.semantic_key() != final.semantic_key()
    assert layer.to_wire()["schema"] == "h3.reference.directives.v2"
    assert final.to_wire()["retention_scope"] == "complete_final_audio_track"


@pytest.mark.parametrize(
    "scope,target",
    [
        (RetentionScope.SUBJECT, "asset"),
        (RetentionScope.PICTURE, "subject"),
        (RetentionScope.VIDEO_STRUCTURE, "subject"),
        (RetentionScope.AUDIO_LAYER, "asset"),
        (RetentionScope.COMPLETE_FINAL_AUDIO_TRACK, "subject"),
    ],
)
def test_scoped_directive_refuses_a_different_denotation(
    scope: RetentionScope, target: str
) -> None:
    from comfyui_h3_context.core.errors import DirectiveSemanticsError
    from comfyui_h3_context.core.reference_directives import (
        DirectiveAction,
        DirectiveTargetKind,
        ReferenceDirective,
        RetentionAspect,
    )

    with pytest.raises(DirectiveSemanticsError, match="scope"):
        ReferenceDirective(
            "retain",
            DirectiveAction.RETAIN,
            DirectiveTargetKind(target),
            "target",
            source_asset_ids=("source",),
            retention_aspects=(RetentionAspect.IDENTITY,),
            retention_scope=scope,
        )


@pytest.mark.parametrize(
    "scope,kind,role",
    [
        (RetentionScope.PICTURE, MediaKind.IMAGE, AssetRole.FIRST_FRAME),
        (RetentionScope.VIDEO_STRUCTURE, MediaKind.VIDEO, AssetRole.EDITING_SOURCE),
    ],
)
def test_classifier_cannot_authorize_an_unqualified_asset_denotation(
    scope: RetentionScope,
    kind: MediaKind,
    role: AssetRole,
) -> None:
    from comfyui_h3_context.core.contracts import TaskMode
    from comfyui_h3_context.core.task_mode_retention_classifier import (
        ClassifierDisposition,
        ClassifierEvidenceSource,
        RetentionEvidence,
        TaskModeRetentionRequest,
        build_task_mode_retention_report,
    )

    evidence = RetentionEvidence(
        RetentionDomain.VISUAL,
        VisualRetentionMarker.FULLY_PRESERVED,
        ("source",),
        "source",
        ClassifierEvidenceSource.USER_DIRECTIVE,
        Decimal(1),
        scope=scope,
    )
    for actual_role, expected in (
        (AssetRole.REFERENCE, ClassifierDisposition.REJECTED),
        (role, ClassifierDisposition.ACCEPTED),
    ):
        registry = build_reference_registry((ReferenceAsset("source", kind, actual_role, 1),))
        report = build_task_mode_retention_report(
            TaskModeRetentionRequest(TaskMode.REF2VA, registry, retention_evidence=(evidence,)),
        )
        assert report.retention[0].disposition is expected
