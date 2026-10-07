"""M15-03 pure product-shell projection contract tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from comfyui_h3_context.core import (
    ContextReport,
    ExecutionCorrelation,
    ProductScopeDisposition,
    ProductShellError,
    TaskMode,
    build_native_h3_wiring,
    build_product_shell_projection,
    validate_product_shell_wire,
)
from comfyui_h3_context.core.assisted_authoring_scope import AssistedAuthoringState
from comfyui_h3_context.core.product_shell import ProductShellHostProfile
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3ReferenceRegistryNode,
)


def _report(
    mode: TaskMode = TaskMode.T2VA, *, with_references: bool = False, with_audio: bool = False
) -> ContextReport:
    request = H3ContextRequestNode().build_request(
        mode,
        "Preserve the exact declared intent.",
        duration_seconds=5.0,
    )[0]
    registry = None
    if with_references:
        registry = H3ReferenceRegistryNode().build_registry(
            images=[object()], audios=[object()] if with_audio else None
        )[0]
    plan = H3ContextPlanNode().build_plan(request, registry)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


class ProductShellTests(unittest.TestCase):
    def test_host_versions_are_typed_provenance_not_exact_capability_gates(self) -> None:
        profile = ProductShellHostProfile(
            core_version="0.33.0",
            core_revision="1" * 40,
            frontend_version="1.49.6",
            frontend_revision="2" * 40,
        )
        self.assertEqual(profile.core_version, "0.33.0")
        self.assertEqual(profile.frontend_version, "1.49.6")

        with self.assertRaisesRegex(ProductShellError, "unsupported_host"):
            ProductShellHostProfile(core_version=f"{'9' * 65}.0")

    def test_projection_reports_catalog_availability_without_session_authority(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        projection = build_product_shell_projection(
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
        )
        self.assertEqual(projection.product_scope, ProductScopeDisposition.MANUAL_ONLY_SCOPED)
        self.assertTrue(projection.prompt_export_ready)
        self.assertTrue(projection.native_queue_ready)
        self.assertFalse(projection.assisted_ready)
        self.assertEqual(projection.readiness_reason, "manual_only_scoped")
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
        self.assertEqual(projection.prompt_fingerprint, wiring.prompt_fingerprint)
        self.assertTrue(all(field_id.startswith("h3.") for field_id in projection.field_ids))
        encoded = json.dumps(projection.to_wire(), ensure_ascii=False, sort_keys=True)
        self.assertNotIn("Preserve the exact declared intent.", encoded)
        self.assertNotIn("request", projection.to_wire())
        self.assertLessEqual(len(encoded.encode("utf-8")), 32_768)

        with self.assertRaises(ProductShellError):
            replace(
                projection,
                assisted_authoring=AssistedAuthoringState(True, True, False, False, False),
            )

    def test_projection_preserves_zero_based_child_path_and_one_based_label(self) -> None:
        report = _report(TaskMode.REF2VA, with_references=True)
        wiring = build_native_h3_wiring(report)
        projection = build_product_shell_projection(
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
        )
        first = projection.bindings[0]
        self.assertEqual(first.presentation_label, "<Picture 1>")
        self.assertEqual(first.presentation_ordinal, 1)
        self.assertEqual(
            first.native_child_path,
            "MiniMaxH3ReferenceToVideo.ref_images.ref_image_0",
        )

    def test_unqualified_native_inputs_preserve_export_but_cannot_be_promoted(self) -> None:
        report = _report(TaskMode.REF2VA, with_references=True, with_audio=True)
        wiring = build_native_h3_wiring(report)
        projection = build_product_shell_projection(
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
        )
        self.assertFalse(wiring.queue_ready)
        self.assertFalse(projection.native_queue_ready)
        self.assertTrue(projection.prompt_export_ready)
        self.assertEqual(projection.readiness_reason, "native_input_unqualified")
        with self.assertRaises(ProductShellError):
            validate_product_shell_wire(
                {
                    **projection.to_wire(),
                    "native_queue_ready": True,
                    "readiness_reason": "manual_only_scoped",
                },
                report,
                wiring,
                ExecutionCorrelation("prompt-1", "17"),
            )

    def test_semantic_wire_drift_and_sensitive_content_fail_closed(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        projection = build_product_shell_projection(
            report,
            wiring,
            ExecutionCorrelation("prompt-1", "17"),
        )
        wire = projection.to_wire()
        trusted_correlation = ExecutionCorrelation("prompt-1", "17")
        validate_product_shell_wire(wire, report, wiring, trusted_correlation)
        for drift in (
            {**wire, "readiness_reason": "forged"},
            {**wire, "limitations": ["token=hidden"]},
            {**wire, "assisted_ready": True},
            {**wire, "unknown": True},
        ):
            with self.subTest(drift=drift):
                with self.assertRaises(ProductShellError):
                    validate_product_shell_wire(drift, report, wiring, trusted_correlation)

        forged_correlation = {
            **wire,
            "correlation": {
                "prompt_id": "forged-prompt",
                "execution_node_id": "999",
            },
        }
        with self.assertRaisesRegex(ProductShellError, "semantic_drift"):
            validate_product_shell_wire(
                forged_correlation,
                report,
                wiring,
                trusted_correlation,
            )

    def test_untrusted_or_stale_wiring_is_rejected(self) -> None:
        report = _report()
        wiring = build_native_h3_wiring(report)
        stale = replace(wiring, report_id="another-report")
        with self.assertRaises(ProductShellError):
            build_product_shell_projection(
                report,
                stale,
                ExecutionCorrelation("prompt-1", "17"),
            )


if __name__ == "__main__":
    unittest.main()
