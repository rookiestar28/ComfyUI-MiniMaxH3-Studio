"""M2-05 prompt parser and manual-validation tests."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from test_full_rendering import full_plan
from test_rendering import frame_registry, make_plan

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    ProfileIdentity,
    PromptParseError,
    PromptParseResult,
    PromptProfile,
    TaskMode,
    parse_prompt,
    render_base_prompt,
    render_full_reference_prompt,
    render_parsed_prompt,
)

ROOT = Path(__file__).resolve().parents[1]


class ParsingTests(unittest.TestCase):
    def test_canonical_base_t2v_parses_and_round_trips_byte_identically(self) -> None:
        plan = make_plan(TaskMode.T2VA)
        document = render_base_prompt(plan)
        original = document.text

        result = parse_prompt(original, document.profile, document.task_mode)

        self.assertIsInstance(result, PromptParseResult)
        self.assertTrue(result.canonical)
        self.assertTrue(result.is_valid)
        self.assertEqual(
            tuple(field.name for field in result.fields),
            tuple(document.sections[index].heading for index in range(3)),
        )
        self.assertEqual(
            tuple(field.value for field in result.fields),
            tuple(section.body for section in document.sections),
        )
        self.assertEqual(render_parsed_prompt(result), original)
        self.assertEqual(original, document.text)
        for field in result.fields:
            self.assertEqual(original[field.value_start : field.value_end], field.value)

    def test_keyframe_preambles_are_recognized_without_mode_inference(self) -> None:
        for mode in (TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA):
            segment_count = 2 if mode is TaskMode.FL2VA else 1
            plan = make_plan(mode, frame_registry(mode), segment_count=segment_count)
            document = render_base_prompt(plan)

            result = parse_prompt(document.text, document.profile, mode)

            self.assertTrue(result.canonical, mode.value)
            self.assertIsNotNone(result.preamble)
            self.assertEqual(render_parsed_prompt(result), document.text)

    def test_canonical_full_reference_parses_and_round_trips(self) -> None:
        plan = full_plan()
        document = render_full_reference_prompt(plan)

        result = parse_prompt(document.text, document.profile, document.task_mode)

        self.assertTrue(result.is_valid)
        self.assertEqual(
            tuple(field.name for field in result.recognized_fields),
            tuple(section.heading for section in document.sections),
        )
        self.assertEqual(render_parsed_prompt(result), document.text)

    def test_manual_text_retains_fields_and_unparsed_spans_without_recovery_claim(self) -> None:
        text = (
            "integrated_multimodal_description: manually supplied description\n\n"
            "operator_note: retain this note for review\n\n"
            "overall_soundscape: manually supplied ambience\n\n"
            "non_diegetic_music: N/A"
        )
        profile = ProfileIdentity(PromptProfile.BASE, CURRENT_SCHEMA_VERSION)

        result = parse_prompt(text, profile, TaskMode.T2VA)

        self.assertFalse(result.canonical)
        self.assertFalse(result.is_valid)
        self.assertEqual(
            tuple(field.name for field in result.fields),
            (
                "integrated_multimodal_description",
                "overall_soundscape",
                "non_diegetic_music",
            ),
        )
        self.assertTrue(any("operator_note" in span.text for span in result.unparsed_spans))
        self.assertIn("parse.unparsed_span", {item.code for item in result.diagnostics})
        with self.assertRaises(PromptParseError):
            render_parsed_prompt(result)
        self.assertEqual(
            result.to_wire()["unparsed_spans"],
            [span.to_wire() for span in result.unparsed_spans],
        )

    def test_missing_duplicate_and_out_of_order_fields_are_reported(self) -> None:
        profile = ProfileIdentity(PromptProfile.BASE, CURRENT_SCHEMA_VERSION)
        text = (
            "overall_soundscape: ambience\n\n"
            "integrated_multimodal_description: description\n\n"
            "integrated_multimodal_description: duplicate"
        )

        result = parse_prompt(text, profile, TaskMode.T2VA)
        codes = {item.code for item in result.diagnostics}

        self.assertFalse(result.canonical)
        self.assertTrue(
            {"parse.missing_field", "parse.duplicate_field", "parse.field_order"} <= codes
        )

    def test_explicit_profile_and_mode_are_required_and_unsupported_modes_fail(self) -> None:
        plan = make_plan(TaskMode.T2VA)
        document = render_base_prompt(plan)

        with self.assertRaises(PromptParseError):
            parse_prompt(document.text, document.profile, TaskMode.REF2VA)
        with self.assertRaises(PromptParseError):
            parse_prompt(document.text, "h3_base", TaskMode.T2VA)  # type: ignore[arg-type]
        with self.assertRaises(PromptParseError):
            parse_prompt("\x00", document.profile, TaskMode.T2VA)

    def test_clean_import_and_bounded_result_contract(self) -> None:
        plan = make_plan(TaskMode.T2VA)
        document = render_base_prompt(plan)
        result = parse_prompt(document.text, document.profile, document.task_mode)
        self.assertEqual(result.to_wire()["canonical"], True)

        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "parsing.py").read_text(encoding="utf-8")
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
