"""M7-03 explicit audit preview/manual override workflow tests."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from comfyui_h3_context.core import (
    AUDIT_OVERRIDE_SCHEMA,
    AuditOverrideError,
    ContextPreview,
    ContextReport,
    ExactTextConstraint,
    ExactTextKind,
    HardConstraintSet,
    TaskMode,
    ValidationStatus,
    apply_audit_override,
    build_audit_override,
    build_context_preview,
    canonical_fingerprint,
    fingerprint_context_report,
)
from comfyui_h3_context.nodes import (
    AUDIT_OVERRIDE_NODE_ID,
    AUDIT_OVERRIDE_SOCKET_TYPE,
    AuditOverrideNodeError,
    H3ContextAuditOverrideNode,
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "comfyui_h3_context" / "core" / "audit_override.py"
NODE_MODULE = ROOT / "comfyui_h3_context" / "nodes.py"
WORKFLOW = ROOT / "workflows" / "m7_03_h3_context_audit_override.json"


def make_report(*, hard: HardConstraintSet | None = None) -> ContextReport:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "A red kite crosses the sky while the camera follows its arc.",
        duration_seconds=5.167,
        hard_constraints=hard,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    return H3ContextCompilerNode().compile(plan)[1]


class AuditOverrideTests(unittest.TestCase):
    def test_safe_edit_is_versioned_revalidated_and_source_is_unchanged(self) -> None:
        source = make_report()
        source_wire = source.to_wire()
        source_fingerprint = fingerprint_context_report(source)
        # M24-05: the manual skeleton now renders the user's own sentence instead of the
        # former generic fallback, so the simulated edit anchors on that sentence.
        text = source.prompt_document.text.replace(
            "A red kite crosses the sky while the camera follows its arc.",
            "A red kite crosses the sky while the camera follows its arc, held in a gentle arc.",
        )
        assert text != source.prompt_document.text
        override = build_audit_override(source_fingerprint, 1, "Clarify camera motion", text)
        updated = apply_audit_override(source, override)

        self.assertEqual(override.to_wire()["schema"], AUDIT_OVERRIDE_SCHEMA)
        self.assertEqual(updated.validation.status, ValidationStatus.PASSED)
        self.assertNotEqual(updated.report_id, source.report_id)
        self.assertNotEqual(fingerprint_context_report(updated), source_fingerprint)
        self.assertEqual(source.to_wire(), source_wire)
        preview = build_context_preview(updated)
        self.assertIsInstance(preview, ContextPreview)
        self.assertEqual(preview.fingerprints["report"], fingerprint_context_report(updated))
        self.assertEqual(preview.provider_identity, "manual")

    def test_hard_constraint_loss_is_visible_and_never_repaired(self) -> None:
        hard = HardConstraintSet(
            constraints=(
                ExactTextConstraint("dialogue_1", ExactTextKind.DIALOGUE, "Keep the light on."),
            )
        )
        source = make_report(hard=hard)
        self.assertIn("<d>Keep the light on.</d>", source.prompt_document.text)
        text = source.prompt_document.text.replace("Keep the light on.", "Change the light.")
        updated = apply_audit_override(
            source,
            build_audit_override(
                fingerprint_context_report(source),
                2,
                "Try alternate wording",
                text,
            ),
        )
        self.assertEqual(updated.validation.status, ValidationStatus.FAILED)
        self.assertIn(
            "constraint.exact_text_lost",
            {item.code for item in updated.validation.diagnostics},
        )
        self.assertNotIn("Keep the light on.", updated.prompt_document.text)

    def test_stale_and_unsafe_edits_fail_closed(self) -> None:
        source = make_report()
        safe = source.prompt_document.text
        cases = (
            build_audit_override("sha256:" + "0" * 64, 1, "stale", safe),
            ("unsafe", "https://private.example/signed?sig=secret"),
        )
        with self.assertRaises(AuditOverrideError) as stale:
            apply_audit_override(source, cases[0])
        self.assertEqual(stale.exception.code, "stale_report")
        with self.assertRaises(AuditOverrideError) as unsafe:
            build_audit_override(fingerprint_context_report(source), 1, "unsafe", cases[1][1])
        self.assertEqual(unsafe.exception.code, "unsafe_override")
        with self.assertRaises(AuditOverrideError):
            build_audit_override(
                fingerprint_context_report(source),
                1,
                "unsafe",
                r"Keep the image at \\private-server\share\shot.png",
            )

    def test_contract_bounds_and_node_surface_fail_closed(self) -> None:
        source = make_report()
        fingerprint = fingerprint_context_report(source)
        for args in (
            (fingerprint, 0, "reason", source.prompt_document.text),
            (fingerprint, 1, "", source.prompt_document.text),
            (fingerprint, 1, "reason", "\x00"),
        ):
            with self.subTest(args=args), self.assertRaises(AuditOverrideError):
                build_audit_override(*args)
        inputs = H3ContextAuditOverrideNode.INPUT_TYPES()["required"]
        self.assertEqual(
            tuple(inputs),
            ("report", "base_report_fingerprint", "revision", "reason", "prompt_text"),
        )
        self.assertEqual(H3ContextAuditOverrideNode.NODE_ID, AUDIT_OVERRIDE_NODE_ID)
        self.assertEqual(H3ContextAuditOverrideNode.RETURN_TYPES[2], AUDIT_OVERRIDE_SOCKET_TYPE)
        self.assertEqual(H3ContextAuditOverrideNode.RETURN_NAMES[-1], "prompt_document")
        with self.assertRaises(AuditOverrideNodeError):
            H3ContextAuditOverrideNode().apply(
                source,
                "sha256:" + "0" * 64,
                1,
                "stale",
                source.prompt_document.text,
            )

    def test_module_has_no_optional_runtime_imports_and_wire_is_json_safe(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
        forbidden = {
            "aiohttp",
            "comfy",
            "comfy_api",
            "cv2",
            "diffusers",
            "httpx",
            "moviepy",
            "numpy",
            "requests",
            "torch",
            "transformers",
        }
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports)
        self.assertEqual(
            json.loads(
                json.dumps(
                    build_audit_override(
                        fingerprint_context_report(make_report()),
                        1,
                        "safe reason",
                        make_report().prompt_document.text,
                    ).to_wire()
                )
            ),
            build_audit_override(
                fingerprint_context_report(make_report()),
                1,
                "safe reason",
                make_report().prompt_document.text,
            ).to_wire(),
        )
        node_tree = ast.parse(NODE_MODULE.read_text(encoding="utf-8"), filename=str(NODE_MODULE))
        self.assertIsNotNone(node_tree)

    def test_model_free_workflow_fixture_pins_override_identity_and_projection(self) -> None:
        fixture = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        prompt = fixture["prompt"]
        expected = fixture["expected"]
        self.assertEqual(fixture["fixture_id"], "m7-03-audit-override")
        self.assertEqual(
            canonical_fingerprint(prompt),
            "".join(expected["graph_fingerprint_parts"]),
        )
        self.assertEqual(expected["output_projection"]["native_node"], "8")
        request = H3ContextRequestNode().build_request(
            prompt["1"]["inputs"]["task_mode"],
            prompt["1"]["inputs"]["user_intent"],
            duration_seconds=prompt["1"]["inputs"]["duration_seconds"],
        )[0]
        plan = H3ContextPlanNode().build_plan(request)[0]
        _, report, _ = H3ContextCompilerNode().compile(plan)
        override_inputs = prompt["4"]["inputs"]
        updated = apply_audit_override(
            report,
            build_audit_override(
                override_inputs["base_report_fingerprint"],
                override_inputs["revision"],
                override_inputs["reason"],
                override_inputs["prompt_text"],
            ),
        )
        self.assertEqual(
            fingerprint_context_report(updated),
            expected["override_projection"]["report_fingerprint"],
        )
        validation, validated = H3ContextValidatorNode().validate(plan, updated.prompt_document)
        self.assertEqual(validation.status.value, expected["validated_projection"]["status"])
        self.assertEqual(
            validation.validator_version,
            expected["validated_projection"]["validator_version"],
        )
        self.assertEqual(
            fingerprint_context_report(validated),
            expected["validated_projection"]["report_fingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
