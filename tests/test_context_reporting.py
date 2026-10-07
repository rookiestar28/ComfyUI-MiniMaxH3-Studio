"""M1-07 plan, prompt document, and context report contract tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    AssetRole,
    AudioIntent,
    AudioLayer,
    CameraIntent,
    ContextPlan,
    ContextReport,
    ContextReportError,
    EvidenceLevel,
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSet,
    EvidenceSource,
    EvidenceSourceKind,
    IntentAction,
    IntentGraph,
    IntentScene,
    IntentSubject,
    Limitation,
    MediaKind,
    MediaMetadata,
    NormalizedContextRequest,
    PlanStage,
    PlanStep,
    PlanStepStatus,
    PromptDocument,
    PromptRenderStatus,
    PromptSection,
    Provenance,
    ProviderIdentity,
    ProviderOutcome,
    ProviderReceipt,
    RawContextRequest,
    ReferenceAsset,
    ReferenceRegistry,
    RetentionDomain,
    RetentionRelation,
    StyleIntent,
    SupportStatus,
    TaskMode,
    TimelineSegment,
    TimePoint,
    ValidationDiagnostic,
    ValidationResult,
    ValidationSeverity,
    ValidationStatus,
    VisualRetentionMarker,
    build_intent_graph,
    build_reference_registry,
    normalize_request,
)

ROOT = Path(__file__).resolve().parents[1]


def sample_evidence() -> EvidenceSet:
    return EvidenceSet(
        (
            EvidenceRecord(
                "evidence_1",
                "The user declared the red coat subject.",
                EvidenceOrigin.USER_DECLARED,
                SupportStatus.SUPPORTED,
                Provenance(
                    EvidenceSource(EvidenceSourceKind.USER_INPUT, "request.user_intent"),
                    ProviderIdentity.MANUAL,
                    EvidenceLevel.COMMUNITY_RECOMMENDED,
                ),
            ),
        )
    )


def sample_registry() -> ReferenceRegistry:
    return build_reference_registry(
        (
            ReferenceAsset(
                "asset_1",
                MediaKind.IMAGE,
                AssetRole.REFERENCE,
                1,
                MediaMetadata(width=640, height=480),
            ),
        )
    )


def sample_request() -> NormalizedContextRequest:
    result = normalize_request(
        RawContextRequest(
            TaskMode.REF2VA,
            "Use the red coat subject.",
            reference_registry=sample_registry(),
            evidence=sample_evidence(),
        )
    )
    assert result.request is not None
    return result.request


def sample_graph() -> IntentGraph:
    registry = sample_registry()
    result = build_intent_graph(
        effective_duration=TimePoint.from_text("5"),
        registry=registry,
        subjects=(IntentSubject("subject_1", "the woman", ("asset_1",)),),
        scenes=(IntentScene("scene_1", "a station", ("subject_1",), "style_1"),),
        actions=(IntentAction("action_1", "walks", ("subject_1",), "scene_1"),),
        cameras=(CameraIntent("camera_1", "slow push", "scene_1", ("subject_1",)),),
        styles=(StyleIntent("style_1", "cinematic"),),
        audios=(AudioIntent("audio_1", AudioLayer.AMBIENCE, "station ambience"),),
        events=(),
        retention=(
            RetentionRelation(
                "retention_1",
                RetentionDomain.VISUAL,
                ("asset_1",),
                "subject_1",
                VisualRetentionMarker.FULLY_PRESERVED,
            ),
        ),
        segments=(
            TimelineSegment(
                "segment_1",
                TimePoint.from_text("0"),
                TimePoint.from_text("5"),
                "scene_1",
                ("subject_1",),
                ("action_1",),
                "camera_1",
                "style_1",
                ("audio_1",),
                (),
            ),
        ),
    )
    assert result.graph is not None
    return result.graph


def sample_plan() -> ContextPlan:
    request = sample_request()
    return ContextPlan(
        plan_id="plan_1",
        schema_version=CURRENT_SCHEMA_VERSION,
        request=request,
        intent_graph=sample_graph(),
        hard_constraints=request.hard_constraints,
        evidence=request.evidence,
        steps=(
            PlanStep("step_1", PlanStage.NORMALIZE, PlanStepStatus.COMPLETED, "normalize request"),
            PlanStep(
                "step_2",
                PlanStage.BIND_REFERENCES,
                PlanStepStatus.COMPLETED,
                "bind assets",
                ("evidence_1",),
            ),
            PlanStep("step_3", PlanStage.ASSEMBLE_INTENT, PlanStepStatus.PLANNED, "assemble graph"),
        ),
        limitations=(
            Limitation("limit_1", "manual_observation", "No media perception was executed."),
        ),
    )


def sample_prompt() -> PromptDocument:
    request = sample_request()
    return PromptDocument(
        document_id="prompt_1",
        schema_version=CURRENT_SCHEMA_VERSION,
        profile=request.profile,
        task_mode=request.task_mode,
        plan_id="plan_1",
        text="<Subject 1> the woman.\n<Scene> a station.",
        sections=(
            PromptSection("section_1", 1, "Subject", "<Subject 1> the woman.", ("evidence_1",)),
            PromptSection("section_2", 2, "Scene", "<Scene> a station.", ("evidence_1",)),
        ),
        status=PromptRenderStatus.RENDERED,
        source_evidence_ids=("evidence_1",),
    )


def sample_report() -> ContextReport:
    request = sample_request()
    return ContextReport(
        report_id="report_1",
        schema_version=CURRENT_SCHEMA_VERSION,
        request=request,
        plan=sample_plan(),
        prompt_document=sample_prompt(),
        validation=ValidationResult(
            "validation_1",
            CURRENT_SCHEMA_VERSION,
            "prompt_1",
            ValidationStatus.PASSED,
            validator_version="validator-1",
        ),
        evidence=request.evidence,
        limitations=(
            Limitation("limit_1", "manual_observation", "No media perception was executed."),
        ),
        provider_receipt=ProviderReceipt(
            "receipt_1", ProviderIdentity.MANUAL, ProviderOutcome.NOT_REQUESTED
        ),
    )


class ContextReportingTests(unittest.TestCase):
    def test_valid_plan_prompt_report_round_trip_preserves_exact_text(self) -> None:
        report = sample_report()
        self.assertTrue(report.is_successful)
        self.assertEqual(report.prompt_document.text, "<Subject 1> the woman.\n<Scene> a station.")
        encoded = json.dumps(report.to_wire(), ensure_ascii=False, sort_keys=True)
        self.assertIn("No media perception was executed.", encoded)
        self.assertIn("<Subject 1>", encoded)
        self.assertNotIn("api_key", encoded.lower())

    def test_values_are_immutable_and_cross_object_identity_is_required(self) -> None:
        plan = sample_plan()
        with self.assertRaises(FrozenInstanceError):
            plan.plan_id = "changed"  # type: ignore[misc]
        with self.assertRaises(ContextReportError):
            ContextPlan(
                plan_id=plan.plan_id,
                schema_version=plan.schema_version,
                request=plan.request,
                intent_graph=plan.intent_graph,
                hard_constraints=plan.hard_constraints,
                evidence=EvidenceSet.empty(),
                steps=plan.steps,
            )

    def test_plan_order_and_evidence_references_fail_closed(self) -> None:
        request = sample_request()
        with self.assertRaises(ContextReportError):
            ContextPlan(
                "bad_order",
                CURRENT_SCHEMA_VERSION,
                request,
                sample_graph(),
                request.hard_constraints,
                request.evidence,
                (
                    PlanStep("step_1", PlanStage.RENDER, PlanStepStatus.PLANNED, "render"),
                    PlanStep("step_2", PlanStage.NORMALIZE, PlanStepStatus.PLANNED, "normalize"),
                ),
            )
        with self.assertRaises(ContextReportError):
            ContextPlan(
                "bad_evidence",
                CURRENT_SCHEMA_VERSION,
                request,
                sample_graph(),
                request.hard_constraints,
                request.evidence,
                (
                    PlanStep(
                        "step_bad",
                        PlanStage.BIND_REFERENCES,
                        PlanStepStatus.PLANNED,
                        "bind",
                        ("missing_evidence",),
                    ),
                ),
            )

    def test_prompt_sections_validation_states_and_exact_constraint_surface(self) -> None:
        prompt = sample_prompt()
        self.assertEqual([section.order for section in prompt.sections], [1, 2])
        with self.assertRaises(ContextReportError):
            PromptDocument(
                prompt.document_id,
                prompt.schema_version,
                prompt.profile,
                prompt.task_mode,
                prompt.plan_id,
                prompt.text,
                (
                    PromptSection("section_1", 1, "Subject", "body"),
                    PromptSection("section_3", 3, "Scene", "body"),
                ),
            )
        diagnostic = ValidationDiagnostic(
            ValidationSeverity.ERROR, "prompt.invalid", "prompt structure is invalid"
        )
        failed = ValidationResult(
            "validation_failed",
            CURRENT_SCHEMA_VERSION,
            "prompt_1",
            ValidationStatus.FAILED,
            (diagnostic,),
        )
        self.assertFalse(failed.is_valid)
        with self.assertRaises(ContextReportError):
            ValidationResult(
                "validation_bad",
                CURRENT_SCHEMA_VERSION,
                "prompt_1",
                ValidationStatus.PASSED,
                (diagnostic,),
            )

    def test_provider_outcomes_are_distinct_and_receipts_are_redacted(self) -> None:
        receipt = ProviderReceipt(
            "receipt_remote",
            ProviderIdentity.REMOTE_CUSTOM,
            ProviderOutcome.TIMEOUT,
            provider_version="remote-2",
            endpoint_revision="rev-17",
            task_id="task_17",
            input_fingerprint="a" * 64,
            redacted_message="read timeout after bounded deadline",
        )
        self.assertFalse(receipt.is_successful)
        self.assertEqual(receipt.outcome, ProviderOutcome.TIMEOUT)
        with self.assertRaises(ContextReportError):
            ProviderReceipt(
                "bad_receipt",
                ProviderIdentity.REMOTE_CUSTOM,
                ProviderOutcome.SUCCEEDED,
                provider_version="remote-2",
                endpoint_revision="rev-17",
                redacted_message="https://private.example/signed?sig=secret",
            )
        encoded = json.dumps(receipt.to_wire())
        self.assertIn("task_17", encoded)
        self.assertNotIn("https://", encoded)

    def test_failed_provider_or_validation_cannot_be_successful_report(self) -> None:
        report = sample_report()
        failed = ContextReport(
            report.report_id,
            report.schema_version,
            report.request,
            report.plan,
            report.prompt_document,
            ValidationResult(
                "validation_failed",
                CURRENT_SCHEMA_VERSION,
                "prompt_1",
                ValidationStatus.FAILED,
                (ValidationDiagnostic(ValidationSeverity.ERROR, "invalid", "invalid prompt"),),
            ),
            report.evidence,
            report.limitations,
            provider_receipt=ProviderReceipt(
                "receipt_remote",
                ProviderIdentity.REMOTE_CUSTOM,
                ProviderOutcome.MODERATED,
                provider_version="remote-2",
                endpoint_revision="rev-17",
            ),
        )
        self.assertFalse(failed.is_successful)
        self.assertTrue(failed.has_errors)

    def test_report_schema_and_clean_import_boundary(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "h3_context_report_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/h3_context_report_v1.schema.json"
        )
        self.assertIn("prompt_document", schema["required"])
        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "context_reporting.py").read_text(
                encoding="utf-8"
            )
        )
        optional_roots = {"comfy", "torch", "torchaudio", "requests", "httpx", "PIL", "numpy"}
        imported: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(optional_roots.isdisjoint(imported))


if __name__ == "__main__":
    unittest.main()
