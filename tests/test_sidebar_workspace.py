"""M15-04 backend-owned sidebar workspace and revision lifecycle tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator

from comfyui_h3_context.core import (
    ContextReport,
    ExactTextConstraint,
    ExactTextKind,
    ExecutionCorrelation,
    GuideReadiness,
    HardConstraintSet,
    IntentSubject,
    NativeH3AdapterError,
    SidebarSubjectCandidate,
    SidebarWorkspaceError,
    TaskMode,
    TimelineSegment,
    TimePoint,
    ValidationStatus,
    build_intent_graph,
    build_native_h3_wiring,
    build_sidebar_workspace_projection,
    canonical_fingerprint,
    evaluate_guide_conformance,
    fingerprint_context_report,
    stage_audit_override,
    validate_sidebar_revision,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3IntentGraphProducerNode,
    H3ReferenceRegistryNode,
)


def _validated_report(
    mode: TaskMode = TaskMode.T2VA,
    *,
    references: bool = False,
    subjects: bool = False,
    subject_description: str = "warm timber bakery interior",
    hard_constraints: HardConstraintSet | None = None,
) -> ContextReport:
    request = H3ContextRequestNode().build_request(
        mode,
        "Preserve the exact declared intent.",
        duration_seconds=5.0,
        hard_constraints=hard_constraints,
    )[0]
    registry = None
    if references:
        registry = H3ReferenceRegistryNode().build_registry(
            images=[object()],
            videos=[object()],
            paired_audios=[object()],
            audios=[object()],
        )[0]
    plan_node = H3ContextPlanNode()
    plan = plan_node.build_plan(request, registry)[0]
    if subjects:
        if registry is None:
            raise AssertionError("subject fixtures require a reference registry")
        duration = TimePoint.from_text(str(plan.request.effective_duration_seconds))
        graph_result = build_intent_graph(
            effective_duration=duration,
            registry=registry,
            subjects=(
                IntentSubject("subject_baker", "the baker", ("image_1",)),
                IntentSubject(
                    "subject_room",
                    "the bakery interior",
                    ("video_1",),
                    subject_description,
                ),
            ),
            segments=(
                TimelineSegment(
                    "segment_1",
                    TimePoint.from_text("0"),
                    duration,
                    subject_ids=("subject_baker", "subject_room"),
                ),
            ),
        )
        if graph_result.graph is None:
            raise AssertionError(graph_result.diagnostics)
        plan = plan_node.build_plan(request, registry, graph_result.graph)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


def _validated_i2va_first_frame_report() -> ContextReport:
    request = H3ContextRequestNode().build_request(
        TaskMode.I2VA,
        "Preserve the exact declared intent.",
        duration_seconds=5.0,
    )[0]
    registry = H3ReferenceRegistryNode().build_registry(first_frame=[object()])[0]
    plan = H3ContextPlanNode().build_plan(request, registry)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


def _validated_semantic_t2va_report(*, complete_silence: bool) -> ContextReport:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "A baker opens the quiet bakery before sunrise.",
        duration_seconds=5.0,
    )[0]
    registry = H3ReferenceRegistryNode().build_registry()[0]
    intent = H3IntentGraphProducerNode().produce(
        request,
        registry,
        subject_label="the baker",
        action_description="opens the bakery",
        complete_silence=complete_silence,
    )[0]
    plan = H3ContextPlanNode().build_plan(request, registry, intent)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


class SidebarWorkspaceTests(unittest.TestCase):
    def test_workspace_projects_the_same_explicit_guide_readiness_as_the_report_plan(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (root / "governance" / "contracts" / "sidebar_workspace_v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)

        for complete_silence, expected_readiness, expected_reasons in (
            (
                False,
                GuideReadiness.INCOMPLETE,
                ["fidelity.soundscape.unspecified"],
            ),
            (True, GuideReadiness.READY, []),
        ):
            with self.subTest(complete_silence=complete_silence):
                report = _validated_semantic_t2va_report(complete_silence=complete_silence)
                wiring = build_native_h3_wiring(report)
                projection = build_sidebar_workspace_projection(
                    report,
                    wiring,
                    ExecutionCorrelation("prompt-guide", "6"),
                    workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
                    base_prompt_fingerprint=wiring.prompt_fingerprint,
                )
                expected = evaluate_guide_conformance(report.plan, report.prompt_document)

                self.assertEqual(projection.lifecycle, "ready")
                self.assertEqual(projection.guide_conformance, expected)
                self.assertIs(expected.readiness, expected_readiness)
                wire = projection.to_wire()
                self.assertEqual(
                    wire["guide_conformance"],
                    {
                        "schema": "h3.context.guide_conformance.v2",
                        "readiness": expected_readiness.value,
                        "reasons": expected_reasons,
                    },
                )
                self.assertEqual(list(validator.iter_errors(wire)), [])
                self.assertNotIn("baker", json.dumps(wire["guide_conformance"]))

    def test_image_only_media_receipt_matches_shared_wire_vector(self) -> None:
        root = Path(__file__).resolve().parents[1]
        vector_document = json.loads(
            (
                root / "tests" / "fixtures" / "sidebar_workspace_media_receipt_vectors.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            vector_document["schema"],
            "h3-context-sidebar-media-receipt-vectors/1",
        )
        vector = vector_document["vectors"][0]
        self.assertEqual(vector["producer_case"]["task_mode"], "i2va")
        self.assertEqual(vector["producer_case"]["reference_roles"], ["first_frame"])

        report = _validated_i2va_first_frame_report()
        wiring = build_native_h3_wiring(report)
        projection = build_sidebar_workspace_projection(
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
            workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
            base_prompt_fingerprint=wiring.prompt_fingerprint,
        )

        self.assertEqual(projection.media_receipt, vector["media_receipt"])
        self.assertEqual(
            projection.capabilities["timed_reference_limits"]["status"],
            "not_applicable",
        )
        workspace_schema = json.loads(
            (root / "governance" / "contracts" / "sidebar_workspace_v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            list(Draft202012Validator(workspace_schema).iter_errors(projection.to_wire())),
            [],
        )

    def test_subject_candidates_are_full_reference_only_and_graph_ordered(self) -> None:
        report = _validated_report(TaskMode.REF2VA, references=True, subjects=True)
        wiring = build_native_h3_wiring(report)
        projection = build_sidebar_workspace_projection(
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
            workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
            base_prompt_fingerprint=wiring.prompt_fingerprint,
        )

        self.assertEqual(
            tuple(candidate.to_wire() for candidate in projection.subject_candidates),
            (
                {
                    "subject_id": "subject_baker",
                    "ordinal": 1,
                    "label": "<Subject 1>",
                    "display": "the baker",
                },
                {
                    "subject_id": "subject_room",
                    "ordinal": 2,
                    "label": "<Subject 2>",
                    "display": "warm timber bakery interior",
                },
            ),
        )
        expected_labels = tuple(
            f"<Subject {ordinal}>"
            for ordinal, _subject in enumerate(report.plan.intent_graph.subjects, 1)
        )
        self.assertEqual(
            tuple(candidate.label for candidate in projection.subject_candidates),
            expected_labels,
        )

        base_report = _validated_report(TaskMode.T2VA)
        base_wiring = build_native_h3_wiring(base_report)
        base_projection = build_sidebar_workspace_projection(
            base_report,
            base_wiring,
            ExecutionCorrelation("prompt-2", "18"),
            workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
            base_prompt_fingerprint=base_wiring.prompt_fingerprint,
        )
        self.assertEqual(base_projection.subject_candidates, ())
        self.assertEqual(base_projection.to_wire()["subject_candidates"], [])

    def test_subject_candidate_contract_rejects_drift_and_unbounded_display(self) -> None:
        for values in (
            ("subject_1", 1, "<Subject 2>", "the baker"),
            ("subject_1", 0, "<Subject 0>", "the baker"),
            ("subject_1", 1, "<Subject 1>", "x" * 513),
        ):
            with self.subTest(values=values):
                with self.assertRaises(SidebarWorkspaceError):
                    SidebarSubjectCandidate(*values)

    def test_subject_candidate_display_reuses_the_bounded_redacted_preview(self) -> None:
        report = _validated_report(
            TaskMode.REF2VA,
            references=True,
            subjects=True,
            subject_description="password=hidden",
        )
        projection = build_sidebar_workspace_projection(
            report,
            None,
            ExecutionCorrelation("prompt-1", "17"),
            workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
            base_prompt_fingerprint=canonical_fingerprint(report.prompt_document.text),
        )

        self.assertEqual(projection.subject_candidates[1].display, "[REDACTED]")
        self.assertNotIn("hidden", json.dumps(projection.to_wire()))

    def test_portable_schemas_are_closed_and_version_pinned(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contracts = root / "governance" / "contracts"
        for filename, schema in (
            ("sidebar_workspace_v2.schema.json", "h3.context.sidebar.workspace.v2"),
            ("sidebar_action_v1.schema.json", "h3.context.sidebar.action.v1"),
            ("sidebar_action_v2.schema.json", "h3.context.sidebar.action.v2"),
            ("sidebar_transfer_v1.schema.json", "h3.context.sidebar.transfer.v1"),
        ):
            value = json.loads((contracts / filename).read_text(encoding="utf-8"))
            self.assertEqual(value["properties"]["schema"]["const"], schema)
            self.assertFalse(value["additionalProperties"])
            Draft202012Validator.check_schema(value)

        report = _validated_report()
        wiring = build_native_h3_wiring(report)
        projection = build_sidebar_workspace_projection(
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
            workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
            base_prompt_fingerprint=wiring.prompt_fingerprint,
        )
        workspace_schema = json.loads(
            (contracts / "sidebar_workspace_v2.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            list(Draft202012Validator(workspace_schema).iter_errors(projection.to_wire())),
            [],
        )

    def test_ready_projection_has_fixed_stages_and_backend_ordered_candidates(self) -> None:
        report = _validated_report(TaskMode.T2VA)
        wiring = build_native_h3_wiring(report)
        projection = build_sidebar_workspace_projection(
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
            workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
            base_prompt_fingerprint=wiring.prompt_fingerprint,
        )

        self.assertEqual(projection.schema, "h3.context.sidebar.workspace.v2")
        self.assertEqual(
            projection.assisted_authoring.to_wire(),
            {
                "available": True,
                "selected": False,
                "ready": False,
                "authorized_for_this_action": False,
                "defaulted": False,
            },
        )
        self.assertEqual(
            tuple(stage.stage_id for stage in projection.stages),
            ("intent", "media", "understand", "audit", "execute"),
        )
        self.assertEqual(projection.lifecycle, "ready")
        self.assertTrue(projection.actions.export)
        self.assertEqual(projection.planning["policy"], "deterministic_manual")
        self.assertEqual(projection.planning["alternatives"], [])
        self.assertEqual(projection.planning["timeline"]["start_seconds"], 0)
        self.assertEqual(projection.planning["timeline"]["effective_frame_count"], 124)
        self.assertEqual(
            projection.capabilities["supported_modes"],
            ["t2va", "i2va", "fl2va", "l2va", "ref2va"],
        )
        self.assertEqual(projection.capabilities["native_prompt_boundary"], "STRING")
        self.assertEqual(
            projection.capabilities["reference_limits"],
            {"total": 12, "image": 9, "video": 3, "audio": 3},
        )
        self.assertEqual(projection.media_receipt["status"], "not_required")
        self.assertTrue(projection.media_receipt["queue_ready"])
        self.assertEqual(projection.proposal["diff"]["status"], "unchanged")
        self.assertEqual(projection.reference_candidates, ())
        self.assertLessEqual(
            len(json.dumps(projection.to_wire(), ensure_ascii=False).encode("utf-8")),
            65_536,
        )
        encoded = json.dumps(projection.to_wire(), ensure_ascii=False)
        self.assertNotIn("C:\\", encoded)
        self.assertNotIn("https://", encoded)

    def test_unverified_timed_reference_is_explicit_and_blocks_workspace_actions(self) -> None:
        report = _validated_report(TaskMode.REF2VA, references=True)
        wiring = build_native_h3_wiring(report)
        self.assertFalse(wiring.queue_ready)
        projection = build_sidebar_workspace_projection(
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
            workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
            base_prompt_fingerprint=wiring.prompt_fingerprint,
        )

        self.assertEqual(projection.lifecycle, "blocked")
        self.assertEqual(projection.media_receipt["status"], "unverified")
        self.assertFalse(projection.media_receipt["queue_ready"])
        self.assertEqual(
            projection.capabilities["timed_reference_limits"]["limitation"],
            "h3_base_reference_duration_metadata_unverified",
        )
        self.assertFalse(projection.actions.export)
        self.assertFalse(projection.actions.copy_prompt)
        self.assertEqual(
            tuple(candidate.label for candidate in projection.reference_candidates),
            ("<Picture 1>", "<Audio 1>", "<Video 1>", "<Audio 2>"),
        )
        paired = projection.reference_candidates[1]
        self.assertEqual(paired.paired_with, "<Video 1>")

    def test_stage_then_validate_is_revisioned_and_never_browser_validated(self) -> None:
        source = _validated_report()
        source_fingerprint = fingerprint_context_report(source)
        # M24-05: the manual skeleton now renders the user's own sentence instead of the
        # former generic fallback, so the simulated edit anchors on that sentence.
        text = source.prompt_document.text.replace(
            "Preserve the exact declared intent.",
            "Preserve the exact declared intent, held in a gentle camera arc.",
        )
        assert text != source.prompt_document.text
        staged = stage_audit_override(
            source,
            expected_revision=source.revision,
            expected_report_fingerprint=source_fingerprint,
            reason="Clarify camera motion",
            prompt_text=text,
        )

        self.assertEqual(staged.revision, source.revision + 1)
        self.assertEqual(staged.validation.status, ValidationStatus.NOT_RUN)
        self.assertEqual(source.validation.status, ValidationStatus.PASSED)
        with self.assertRaises(NativeH3AdapterError):
            build_native_h3_wiring(staged)

        validated, wiring = validate_sidebar_revision(
            staged,
            expected_revision=staged.revision,
            expected_report_fingerprint=fingerprint_context_report(staged),
        )
        self.assertEqual(validated.validation.status, ValidationStatus.PASSED)
        if wiring is None:
            self.fail("passed validation did not rebuild native wiring")
        self.assertEqual(wiring.report_id, validated.report_id)
        self.assertEqual(wiring.prompt, text)

        projection = build_sidebar_workspace_projection(
            validated,
            wiring,
            ExecutionCorrelation("prompt-2", "18"),
            workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
            base_prompt_fingerprint=build_native_h3_wiring(source).prompt_fingerprint,
            proposal_reason="Clarify camera motion",
            base_report=source,
        )
        self.assertEqual(projection.proposal["diff"]["status"], "changed")
        self.assertTrue(projection.proposal["diff"]["lines"])
        self.assertEqual(projection.planning["creative_additions_status"], "user_authored")

    def test_stale_identity_and_failed_hard_constraint_never_export(self) -> None:
        hard = HardConstraintSet(
            constraints=(
                ExactTextConstraint("dialogue_1", ExactTextKind.DIALOGUE, "Keep the light on."),
            )
        )
        source = _validated_report(hard_constraints=hard)
        fingerprint = fingerprint_context_report(source)
        with self.assertRaises(SidebarWorkspaceError) as stale:
            stage_audit_override(
                source,
                expected_revision=source.revision + 1,
                expected_report_fingerprint=fingerprint,
                reason="stale",
                prompt_text=source.prompt_document.text,
            )
        self.assertEqual(stale.exception.code, "stale_revision")

        staged = stage_audit_override(
            source,
            expected_revision=source.revision,
            expected_report_fingerprint=fingerprint,
            reason="Test a rejected change",
            prompt_text=source.prompt_document.text.replace(
                "Keep the light on.", "Change the light."
            ),
        )
        failed, wiring = validate_sidebar_revision(
            staged,
            expected_revision=staged.revision,
            expected_report_fingerprint=fingerprint_context_report(staged),
        )
        self.assertEqual(failed.validation.status, ValidationStatus.FAILED)
        self.assertIsNone(wiring)
        projection = build_sidebar_workspace_projection(
            failed,
            None,
            ExecutionCorrelation("prompt-2", "18"),
            workspace_id="ws_0123456789abcdefghijklmnopqrstuv",
            base_prompt_fingerprint=source.prompt_document.text
            and build_native_h3_wiring(source).prompt_fingerprint,
        )
        self.assertEqual(projection.lifecycle, "blocked")
        self.assertFalse(projection.actions.export)
        self.assertFalse(projection.actions.copy_prompt)

    def test_exact_base_types_and_unsafe_prompt_fail_closed(self) -> None:
        source = _validated_report()
        fingerprint = fingerprint_context_report(source)

        class Spoof(str):
            pass

        for text in (Spoof(source.prompt_document.text), "https://private.invalid/x"):
            with (
                self.subTest(text_type=type(text).__name__),
                self.assertRaises(SidebarWorkspaceError),
            ):
                stage_audit_override(
                    source,
                    expected_revision=source.revision,
                    expected_report_fingerprint=fingerprint,
                    reason="safe reason",
                    prompt_text=text,
                )

        forged = replace(source, revision=1)
        with self.assertRaises(SidebarWorkspaceError):
            validate_sidebar_revision(
                forged,
                expected_revision=0,
                expected_report_fingerprint=fingerprint_context_report(forged),
            )


if __name__ == "__main__":
    unittest.main()
