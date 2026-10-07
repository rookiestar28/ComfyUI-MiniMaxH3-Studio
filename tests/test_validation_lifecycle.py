"""M10-05 compile/validate lifecycle and execution-admission tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from comfyui_h3_context.core import (
    ContextReport,
    LifecycleState,
    PromptDocument,
    PromptRenderStatus,
    ReportLifecycleError,
    TaskMode,
    ValidatedReportEnvelope,
    apply_audit_override,
    build_audit_override,
    canonical_fingerprint,
    fingerprint_context_report,
    require_execution_ready,
    validate_context_report,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
)


def _compiled_report() -> tuple[ContextReport, PromptDocument]:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "Preserve the exact declared intent.",
        duration_seconds=5.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, report, document = H3ContextCompilerNode().compile(plan)
    return report, document


class ValidationLifecycleTests(unittest.TestCase):
    def test_compiler_report_is_not_execution_ready_until_validator_runs(self) -> None:
        draft, _ = _compiled_report()
        self.assertEqual(draft.validation.status.value, "not_run")
        with self.assertRaises(ReportLifecycleError) as failure:
            require_execution_ready(draft)
        self.assertEqual(failure.exception.code, "validation_not_run")

        envelope = validate_context_report(draft)
        self.assertIsInstance(envelope, ValidatedReportEnvelope)
        self.assertEqual(envelope.status.value, "passed")
        self.assertTrue(envelope.is_execution_ready)
        self.assertEqual(require_execution_ready(envelope), envelope.report)
        self.assertEqual(envelope.report_revision, draft.revision)
        self.assertEqual(envelope.report_fingerprint, fingerprint_context_report(envelope.report))
        self.assertEqual(
            envelope.prompt_fingerprint,
            canonical_fingerprint(envelope.report.prompt_document.text),
        )
        wire = envelope.to_wire()
        self.assertEqual(json.loads(json.dumps(wire, sort_keys=True)), wire)
        self.assertEqual(
            set(wire),
            {
                "schema",
                "report_id",
                "report_revision",
                "report_fingerprint",
                "prompt_fingerprint",
                "status",
                "report",
            },
        )

    def test_failed_validation_and_stale_identity_fail_closed(self) -> None:
        draft, document = _compiled_report()
        tampered = replace(document, text="tampered prompt")
        failed = validate_context_report(replace(draft, prompt_document=tampered))
        self.assertEqual(failed.status.value, "failed")
        self.assertFalse(failed.is_execution_ready)
        with self.assertRaises(ReportLifecycleError) as validation_failure:
            require_execution_ready(failed)
        self.assertEqual(validation_failure.exception.code, "validation_failed")

        passed = validate_context_report(draft)
        with self.assertRaises(ReportLifecycleError) as revision_failure:
            require_execution_ready(passed, expected_revision=passed.report_revision + 1)
        self.assertEqual(revision_failure.exception.code, "stale_revision")
        with self.assertRaises(ReportLifecycleError) as fingerprint_failure:
            require_execution_ready(
                passed,
                expected_report_fingerprint="sha256:" + ("0" * 64),
            )
        self.assertEqual(fingerprint_failure.exception.code, "stale_report")

        stale_report = replace(passed.report, revision=passed.report.revision + 1)
        with self.assertRaises(ReportLifecycleError) as envelope_failure:
            ValidatedReportEnvelope(
                report=stale_report,
                report_revision=passed.report_revision,
                report_fingerprint=passed.report_fingerprint,
                prompt_fingerprint=passed.prompt_fingerprint,
                status=passed.status,
            )
        self.assertEqual(envelope_failure.exception.code, "revision_mismatch")

    def test_manual_override_creates_revision_and_requires_new_envelope(self) -> None:
        draft, _ = _compiled_report()
        passed = validate_context_report(draft).report
        override = build_audit_override(
            fingerprint_context_report(passed),
            revision=7,
            reason="explicit reviewer edit",
            prompt_text=passed.prompt_document.text,
        )
        edited = apply_audit_override(passed, override)
        self.assertEqual(edited.revision, 7)
        self.assertEqual(edited.validation.status.value, "passed")
        self.assertNotEqual(edited.report_id, passed.report_id)
        with self.assertRaises(ReportLifecycleError) as stale:
            require_execution_ready(passed, expected_revision=7)
        self.assertEqual(stale.exception.code, "stale_revision")
        edited_envelope = validate_context_report(edited)
        self.assertTrue(edited_envelope.is_execution_ready)

    def test_lifecycle_state_mapping_is_terminal_and_explicit(self) -> None:
        self.assertEqual(
            {state.value for state in LifecycleState},
            {"not_run", "passed", "failed", "stale"},
        )
        self.assertEqual(PromptRenderStatus.RENDERED.value, "rendered")


if __name__ == "__main__":
    unittest.main()
