from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from comfyui_h3_context.core import (
    AssetRole,
    AudioRetentionMarker,
    ClassifierDisposition,
    ClassifierEvidenceSource,
    FullReferenceTaskType,
    MediaKind,
    ModeEvidence,
    ReferenceAsset,
    ReferenceRegistry,
    RetentionDomain,
    RetentionEvidence,
    RetentionScope,
    TaskMode,
    TaskModeRetentionRequest,
    TaskModeRetentionStatus,
    TaskTypeEvidence,
    VisualRetentionMarker,
    build_reference_registry,
    build_task_mode_retention_report,
)

ROOT = Path(__file__).resolve().parents[1]


def _registry(*assets: ReferenceAsset) -> ReferenceRegistry:
    return build_reference_registry(assets)


def _asset(
    asset_id: str,
    kind: MediaKind,
    role: AssetRole,
    order: int,
) -> ReferenceAsset:
    return ReferenceAsset(asset_id, kind, role, order)


def _mode(mode: TaskMode, confidence: str = "1") -> ModeEvidence:
    return ModeEvidence(
        mode,
        ClassifierEvidenceSource.USER_DIRECTIVE,
        Decimal(confidence),
        (f"mode.{mode.value}",),
    )


def _task(
    task_type: FullReferenceTaskType,
    confidence: str = "1",
    source_asset_ids: tuple[str, ...] = (),
) -> TaskTypeEvidence:
    return TaskTypeEvidence(
        task_type,
        ClassifierEvidenceSource.USER_DIRECTIVE,
        Decimal(confidence),
        source_asset_ids,
        (f"task.{task_type.value}",),
    )


def _retention(
    domain: RetentionDomain,
    marker: VisualRetentionMarker | AudioRetentionMarker,
    source_asset_ids: tuple[str, ...],
    target_id: str,
    confidence: str = "1",
) -> RetentionEvidence:
    return RetentionEvidence(
        domain,
        marker,
        source_asset_ids,
        target_id,
        ClassifierEvidenceSource.USER_DIRECTIVE,
        Decimal(confidence),
        (f"retention.{target_id}",),
        scope=(
            RetentionScope.SUBJECT
            if domain is RetentionDomain.VISUAL
            else RetentionScope.COMPLETE_FINAL_AUDIO_TRACK
            if marker is AudioRetentionMarker.FULLY_COPY
            else RetentionScope.AUDIO_LAYER
        ),
    )


def test_all_modes_validate_only_their_explicit_anchor_combinations() -> None:
    cases = (
        (TaskMode.T2VA, ReferenceRegistry.empty()),
        (
            TaskMode.I2VA,
            _registry(_asset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1)),
        ),
        (
            TaskMode.FL2VA,
            _registry(
                _asset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),
                _asset("last", MediaKind.IMAGE, AssetRole.LAST_FRAME, 2),
            ),
        ),
        (
            TaskMode.L2VA,
            _registry(_asset("last", MediaKind.IMAGE, AssetRole.LAST_FRAME, 1)),
        ),
    )

    for mode, registry in cases:
        report = build_task_mode_retention_report(
            TaskModeRetentionRequest(mode, registry, mode_evidence=(_mode(mode),))
        )
        assert report.status is TaskModeRetentionStatus.COMPLETE
        assert report.mode.disposition is ClassifierDisposition.ACCEPTED


def test_ref2va_requires_explicit_task_and_retention_evidence() -> None:
    registry = _registry(
        _asset("picture", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
        _asset("audio", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 2),
    )
    report = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            TaskMode.REF2VA,
            registry,
            mode_evidence=(_mode(TaskMode.REF2VA),),
            task_type_evidence=(
                _task(FullReferenceTaskType.REFERENCE_GENERATION, source_asset_ids=("picture",)),
            ),
            retention_evidence=(
                _retention(
                    RetentionDomain.VISUAL,
                    VisualRetentionMarker.FULLY_PRESERVED,
                    ("picture",),
                    "subject-1",
                ),
                _retention(
                    RetentionDomain.AUDIO,
                    AudioRetentionMarker.REFERENCE,
                    ("audio",),
                    "audio-1",
                ),
            ),
        )
    )
    assert report.status is TaskModeRetentionStatus.COMPLETE
    assert {item.task_type for item in report.task_types} == {
        FullReferenceTaskType.REFERENCE_GENERATION
    }
    assert {item.marker for item in report.retention} == {
        VisualRetentionMarker.FULLY_PRESERVED,
        AudioRetentionMarker.REFERENCE,
    }


def test_impossible_roles_and_missing_audio_evidence_fail_before_planning() -> None:
    i2va = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            TaskMode.I2VA,
            _registry(
                _asset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1),
                _asset("style", MediaKind.IMAGE, AssetRole.STYLE_REFERENCE, 2),
            ),
            mode_evidence=(_mode(TaskMode.I2VA),),
        )
    )
    assert i2va.status is TaskModeRetentionStatus.BLOCKED
    assert any(item.code == "impossible_role_mixture" for item in i2va.diagnostics)

    audio_only = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            TaskMode.REF2VA,
            _registry(_asset("audio", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 1)),
            mode_evidence=(_mode(TaskMode.REF2VA),),
        )
    )
    assert audio_only.status is TaskModeRetentionStatus.PARTIAL
    assert any(item.code == "missing_task_type_evidence" for item in audio_only.diagnostics)
    assert any(item.code == "missing_retention_evidence" for item in audio_only.diagnostics)


def test_audio_only_ref2va_with_explicit_evidence_is_representable() -> None:
    report = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            TaskMode.REF2VA,
            _registry(_asset("audio", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 1)),
            mode_evidence=(_mode(TaskMode.REF2VA),),
            task_type_evidence=(
                _task(FullReferenceTaskType.AUDIO_REFERENCE, source_asset_ids=("audio",)),
            ),
            retention_evidence=(
                _retention(
                    RetentionDomain.AUDIO,
                    AudioRetentionMarker.REFERENCE,
                    ("audio",),
                    "audio-1",
                ),
            ),
        )
    )
    assert report.status is TaskModeRetentionStatus.COMPLETE
    assert report.mode.disposition is ClassifierDisposition.ACCEPTED
    assert report.task_types[0].task_type is FullReferenceTaskType.AUDIO_REFERENCE
    assert report.retention[0].source_asset_ids == ("audio",)


def test_confidence_thresholds_are_frozen_and_mid_band_abstains() -> None:
    registry = _registry(_asset("picture", MediaKind.IMAGE, AssetRole.REFERENCE, 1))
    report = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            TaskMode.REF2VA,
            registry,
            mode_evidence=(_mode(TaskMode.REF2VA),),
            task_type_evidence=(_task(FullReferenceTaskType.REFERENCE_GENERATION, "0.89"),),
            retention_evidence=(
                _retention(
                    RetentionDomain.VISUAL,
                    VisualRetentionMarker.WEAK_REFERENCE,
                    ("picture",),
                    "subject-1",
                    "0.60",
                ),
            ),
        )
    )
    assert report.status is TaskModeRetentionStatus.PARTIAL
    assert report.task_types[0].disposition is ClassifierDisposition.ABSTAINED
    assert report.retention[0].disposition is ClassifierDisposition.ABSTAINED


def test_equal_confidence_retention_markers_remain_conflicting() -> None:
    registry = _registry(_asset("picture", MediaKind.IMAGE, AssetRole.REFERENCE, 1))
    report = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            TaskMode.REF2VA,
            registry,
            mode_evidence=(_mode(TaskMode.REF2VA),),
            task_type_evidence=(
                _task(FullReferenceTaskType.REFERENCE_GENERATION, source_asset_ids=("picture",)),
            ),
            retention_evidence=(
                _retention(
                    RetentionDomain.VISUAL,
                    VisualRetentionMarker.FULLY_PRESERVED,
                    ("picture",),
                    "subject-1",
                ),
                _retention(
                    RetentionDomain.VISUAL,
                    VisualRetentionMarker.ATTRIBUTE_TRANSFER,
                    ("picture",),
                    "subject-1",
                ),
            ),
        )
    )
    assert report.status is TaskModeRetentionStatus.CONFLICTING
    assert all(item.disposition is ClassifierDisposition.CONFLICTING for item in report.retention)


def test_report_is_deterministic_and_rejects_unknown_retention_assets() -> None:
    registry = _registry(_asset("picture", MediaKind.IMAGE, AssetRole.REFERENCE, 1))
    request = TaskModeRetentionRequest.from_user_mode(TaskMode.REF2VA, registry)
    first = build_task_mode_retention_report(request)
    second = build_task_mode_retention_report(request)
    assert first.fingerprint == second.fingerprint
    assert first.to_wire() == second.to_wire()
    invalid = TaskModeRetentionRequest(
        TaskMode.REF2VA,
        registry,
        mode_evidence=(_mode(TaskMode.REF2VA),),
        retention_evidence=(
            RetentionEvidence(
                RetentionDomain.VISUAL,
                VisualRetentionMarker.FULLY_PRESERVED,
                ("missing",),
                "subject-1",
                ClassifierEvidenceSource.USER_DIRECTIVE,
                Decimal("1"),
                ("retention.subject-1",),
            ),
        ),
    )
    assert build_task_mode_retention_report(invalid).status is TaskModeRetentionStatus.BLOCKED


def test_schema_fixture_and_serialization_bound_are_frozen() -> None:
    schema = json.loads(
        (
            ROOT / "governance" / "contracts" / "task_mode_retention_classifier_v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["$id"] == (
        "comfyui-h3-context://contracts/task_mode_retention_classifier_v1.schema.json"
    )
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "m13_05_task_mode_retention_classifier.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["fixture_status"] == "static_typed_classifier_contract"
    assert fixture["report"]["schema"] == "h3.task_mode_retention_classifier.v1"
    assert fixture["report"]["calibration"] == {
        "abstention_threshold": "0.60",
        "accept_threshold": "0.90",
        "schema": "h3.task_mode_retention_classifier.v1",
    }
    registry = _registry(_asset("picture", MediaKind.IMAGE, AssetRole.REFERENCE, 1))
    report = build_task_mode_retention_report(
        TaskModeRetentionRequest.from_user_mode(TaskMode.REF2VA, registry)
    )
    assert len(report.to_wire_bytes()) <= 131_072
