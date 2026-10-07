"""M2-04 structural and semantic prompt-linter tests."""

from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from pathlib import Path

from test_rendering import make_plan

from comfyui_h3_context.core import (
    ContentScope,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    PromptLintError,
    PromptLintResult,
    RequiredContent,
    TaskMode,
    TimePoint,
    lint_prompt,
    render_base_prompt,
    render_full_reference_prompt,
)

ROOT = Path(__file__).resolve().parents[1]


def codes(result: PromptLintResult) -> tuple[str, ...]:
    return tuple(diagnostic.code for diagnostic in result.diagnostics)


class LintingTests(unittest.TestCase):
    def test_valid_base_document_passes_without_mutation(self) -> None:
        plan = make_plan(TaskMode.T2VA)
        document = render_base_prompt(plan)
        before_plan = plan.to_wire()
        before_document = document.to_wire()

        result = lint_prompt(plan, document)

        self.assertTrue(result.is_valid)
        self.assertFalse(result.diagnostics)
        self.assertEqual(result.to_wire(), {"is_valid": True, "diagnostics": []})
        self.assertEqual(plan.to_wire(), before_plan)
        self.assertEqual(document.to_wire(), before_document)

    def test_valid_full_reference_document_passes(self) -> None:
        from test_full_rendering import full_plan

        plan = full_plan()
        result = lint_prompt(plan, render_full_reference_prompt(plan))

        self.assertTrue(result.is_valid)
        self.assertFalse(result.diagnostics)

    def test_missing_and_duplicate_sections_are_actionable(self) -> None:
        plan = make_plan(TaskMode.T2VA)
        document = render_base_prompt(plan)
        malformed = replace(
            document,
            sections=(
                document.sections[0],
                replace(document.sections[1], heading=document.sections[0].heading),
            ),
        )

        result = lint_prompt(plan, malformed)

        self.assertIn("prompt.missing_section", codes(result))
        self.assertIn("prompt.duplicate_section", codes(result))
        self.assertIn("prompt.section_order", codes(result))
        self.assertTrue(all(diagnostic.remediation for diagnostic in result.diagnostics))

    def test_invalid_and_orphan_reference_labels_fail_closed(self) -> None:
        from test_rendering import frame_registry

        plan = make_plan(TaskMode.I2VA, frame_registry(TaskMode.I2VA))
        document = render_base_prompt(plan)
        malformed = replace(
            document,
            text=f"{document.text.replace('<Picture 1>', '')} <Picture 0> <Picture 99>",
        )

        result = lint_prompt(plan, malformed)

        self.assertIn("reference.invalid_label", codes(result))
        self.assertIn("reference.orphan_asset", codes(result))
        self.assertTrue(result.has_errors)

    def test_timeline_conflict_and_unsupported_mode_are_reported(self) -> None:
        plan = make_plan(TaskMode.T2VA, segment_count=2)
        document = render_base_prompt(plan)
        graph = replace(
            plan.intent_graph,
            segments=(
                plan.intent_graph.segments[0],
                replace(plan.intent_graph.segments[1], start=TimePoint.from_text("1")),
            ),
        )
        conflict_plan = replace(plan, intent_graph=graph)
        result = lint_prompt(conflict_plan, document)
        self.assertIn("timeline.overlap", codes(result))

        unsupported_request = replace(plan.request, task_mode=TaskMode.REF2VA)
        unsupported_plan = replace(plan, request=unsupported_request)
        unsupported_result = lint_prompt(unsupported_plan, document)
        self.assertIn("profile.unsupported_mode", codes(unsupported_result))
        self.assertIn("document.mode_mismatch", codes(unsupported_result))

    def test_provider_residue_is_fatal_without_echoing_secret(self) -> None:
        plan = make_plan(TaskMode.T2VA)
        document = render_base_prompt(plan)
        residue = "https://example.invalid/private?sig=do-not-echo"
        contaminated = replace(document, text=f"{document.text}\n{residue}")

        result = lint_prompt(plan, contaminated)

        self.assertIn("security.provider_residue", codes(result))
        finding = next(
            diagnostic
            for diagnostic in result.diagnostics
            if diagnostic.code == "security.provider_residue"
        )
        self.assertEqual(finding.severity.value, "fatal")
        self.assertNotIn(residue, finding.message)
        self.assertNotIn("do-not-echo", finding.remediation)

    def test_hard_constraint_loss_and_forbidden_content_are_distinct(self) -> None:
        constraints = HardConstraintSet(
            (
                ExactTextConstraint("exact_1", ExactTextKind.DIALOGUE, "Keep this exact line."),
                RequiredContent("required_1", ContentScope.GENERAL, "red umbrella"),
                ForbiddenContent("forbidden_1", ContentScope.GENERAL, "secret sign"),
            )
        )
        plan = make_plan(TaskMode.T2VA, constraints=constraints)
        document = render_base_prompt(plan)
        lost = replace(
            document,
            text=document.text.replace("Keep this exact line.", "").replace("red umbrella", ""),
        )
        lost_result = lint_prompt(plan, lost)
        self.assertIn("constraint.exact_text_lost", codes(lost_result))
        self.assertIn("constraint.required_content_lost", codes(lost_result))

        forbidden = replace(document, text=f"{document.text} secret sign")
        forbidden_result = lint_prompt(plan, forbidden)
        self.assertIn("constraint.forbidden_content_present", codes(forbidden_result))

    def test_input_types_and_clean_import_boundary(self) -> None:
        with self.assertRaises(PromptLintError):
            lint_prompt("not a plan", "not a document")  # type: ignore[arg-type]

        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "linting.py").read_text(encoding="utf-8")
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
