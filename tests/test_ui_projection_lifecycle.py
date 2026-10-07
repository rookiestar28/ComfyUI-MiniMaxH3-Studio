"""M10-05 terminal Preview/UI projection and execution correlation tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from comfyui_h3_context.core import (
    BoundedUIProjection,
    ContextReport,
    ExecutionCorrelation,
    ReportLifecycleError,
    TaskMode,
    UIEventState,
    ValidationSeverity,
    build_ui_projection,
    validate_context_report,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextPreviewNode,
    H3ContextRequestNode,
    PreviewNodeError,
)


def _report() -> tuple[ContextReport, ContextReport]:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "A red kite crosses the sky while the camera follows its arc.",
        duration_seconds=5.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, draft, document = H3ContextCompilerNode().compile(plan)
    return draft, validate_context_report(replace(draft, prompt_document=document)).report


class UIProjectionLifecycleTests(unittest.TestCase):
    def test_terminal_preview_emits_comfyui_ui_and_result_boundary(self) -> None:
        _, report = _report()
        node = H3ContextPreviewNode()
        self.assertTrue(node.OUTPUT_NODE)
        self.assertEqual(node.FUNCTION, "emit")
        emitted = node.emit(
            report,
            prompt_id="prompt-123",
            execution_node_id="7",
            event_state=UIEventState.VALIDATED.value,
        )
        self.assertEqual(set(emitted), {"ui", "result"})
        ui = emitted["ui"]
        self.assertIsInstance(ui, dict)
        assert isinstance(ui, dict)
        self.assertTrue(all(isinstance(value, tuple) for value in ui.values()))
        self.assertEqual(ui["schema"], ("h3.context.ui.projection.v1",))
        self.assertEqual(
            ui["correlation"], ({"prompt_id": "prompt-123", "execution_node_id": "7"},)
        )
        self.assertNotIn("request", ui)
        self.assertNotIn("evidence", ui)
        self.assertNotIn("plan", ui)
        self.assertNotIn("provider_receipt", ui)
        json.dumps(emitted, ensure_ascii=False, default=str)
        result = emitted["result"]
        self.assertIsInstance(result, tuple)
        assert isinstance(result, tuple)
        self.assertEqual(result[0], report.prompt_document.text)

    def test_event_states_are_distinct_and_success_states_require_validation(self) -> None:
        _, report = _report()
        correlation = ExecutionCorrelation("prompt-123", "7")
        for state in (
            UIEventState.VALIDATED,
            UIEventState.CACHED,
            UIEventState.SUCCESS,
        ):
            projection = build_ui_projection(report, correlation, state)
            self.assertIsInstance(projection, BoundedUIProjection)
            self.assertEqual(projection.to_wire()["state"], state.value)
            self.assertEqual(projection.to_ui()["state"], (state.value,))

        draft, _ = _report()
        with self.assertRaises(ReportLifecycleError) as failure:
            build_ui_projection(draft, correlation, UIEventState.SUCCESS)
        self.assertEqual(failure.exception.code, "validation_not_run")

    def test_error_and_cancelled_states_preserve_failure_without_fake_success(self) -> None:
        draft, _ = _report()
        failed_document = replace(draft.prompt_document, text="tampered prompt")
        failed = validate_context_report(replace(draft, prompt_document=failed_document))
        correlation = ExecutionCorrelation("prompt-123", "7")
        for state in (UIEventState.ERROR, UIEventState.CANCELLED):
            projection = build_ui_projection(failed, correlation, state)
            self.assertEqual(projection.validation_status.value, "failed")
            self.assertEqual(projection.to_wire()["state"], state.value)

    def test_a_diagnostic_severity_outside_the_known_set_is_refused(self) -> None:
        """M18-05: the one invariant the retired UI-projection schema held and the module did not.

        The retired schema stated `severity` as an enum of four values; the typed authority only
        bounded its length, so any 32-character string reached the UI boundary as a severity. The
        check now lives here, against the live `ValidationSeverity` rather than a copied list.
        """

        _, report = _report()
        projection = build_ui_projection(
            report, ExecutionCorrelation("prompt-123", "preview"), UIEventState.SUCCESS
        )
        fields = {
            name: getattr(projection, name) for name in projection.__dataclass_fields__ if name
        }
        for severity in ("NOT_A_REAL_SEVERITY", "", "Info", "warn"):
            with self.subTest(severity=severity), self.assertRaises(ReportLifecycleError):
                BoundedUIProjection(
                    **{
                        **fields,
                        "diagnostics": (
                            {"code": "example", "severity": severity, "message": "bounded"},
                        ),
                    }
                )
        for severity in (member.value for member in ValidationSeverity):
            with self.subTest(accepted=severity):
                BoundedUIProjection(
                    **{
                        **fields,
                        "diagnostics": (
                            {"code": "example", "severity": severity, "message": "bounded"},
                        ),
                    }
                )

    def test_ui_projection_redacts_private_prompt_text_and_bounds_output(self) -> None:
        request = H3ContextRequestNode().build_request(
            TaskMode.T2VA,
            "Keep https://private.example/task?sig=hidden token=hidden /mnt/private/shot.mp4",
            duration_seconds=5.0,
        )[0]
        plan = H3ContextPlanNode().build_plan(request)[0]
        _, draft, document = H3ContextCompilerNode().compile(plan)
        failed = validate_context_report(replace(draft, prompt_document=document))
        projection = build_ui_projection(
            failed,
            ExecutionCorrelation("prompt-123", "preview"),
            UIEventState.ERROR,
        )
        encoded = json.dumps(projection.to_wire(), ensure_ascii=False)
        self.assertNotIn("https://private.example", encoded)
        self.assertNotIn("sig=hidden", encoded)
        self.assertNotIn("/mnt/private/shot.mp4", encoded)
        self.assertLessEqual(len(encoded.encode("utf-8")), 32_768)
        self.assertIn("prompt_text", projection.redacted_fields)

    def test_terminal_preview_requires_correlation_when_no_host_context_exists(self) -> None:
        _, report = _report()
        with self.assertRaises(PreviewNodeError) as failure:
            H3ContextPreviewNode().emit(report)
        self.assertIn("missing_correlation", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
