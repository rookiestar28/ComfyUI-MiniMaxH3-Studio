"""M2-06 bounded adversarial corpus and regression tests."""

from __future__ import annotations

import ast
import dataclasses
import tempfile
import unittest
from pathlib import Path

from security_corpus import (
    ALL_CASES,
    INSTRUCTION_LIKE_TEXT,
    LABEL_COLLISIONS,
    MALFORMED_UNICODE,
    OVERSIZED_FIELDS,
    TRAVERSAL_LIKE_PATHS,
)
from test_rendering import make_plan

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    AssetRole,
    BackendLabel,
    BackendLabelKind,
    BackendTarget,
    ContractValidationError,
    MediaKind,
    ProfileIdentity,
    PromptLintResult,
    PromptParseError,
    PromptProfile,
    ReferenceAsset,
    ReferenceLabelError,
    ReferenceRegistry,
    SecurityPolicyError,
    TaskMode,
    build_reference_registry,
    lint_prompt,
    parse_prompt,
    render_base_prompt,
)
from comfyui_h3_context.core.security import (
    RedactedReceipt,
    validate_local_path,
    validate_remote_url,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_PROFILE = ProfileIdentity(PromptProfile.BASE, CURRENT_SCHEMA_VERSION)


class SecurityCorpusTests(unittest.TestCase):
    def test_corpus_ids_are_unique_bounded_and_secret_free(self) -> None:
        ids = [case.case_id for case in ALL_CASES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(1 <= len(case.payload) <= 65_537 for case in ALL_CASES))
        self.assertTrue(all("credential" not in case.payload.casefold() for case in ALL_CASES))

    def test_malformed_unicode_and_oversized_parser_input_fail_closed(self) -> None:
        for case in MALFORMED_UNICODE:
            with self.subTest(case=case.case_id), self.assertRaises(PromptParseError):
                parse_prompt(case.payload, BASE_PROFILE, TaskMode.T2VA)
        for case in OVERSIZED_FIELDS:
            with self.subTest(case=case.case_id), self.assertRaises(PromptParseError):
                parse_prompt(case.payload, BASE_PROFILE, TaskMode.T2VA)

    def test_instruction_like_text_remains_data_and_is_not_executed(self) -> None:
        for case in INSTRUCTION_LIKE_TEXT:
            text = (
                "integrated_multimodal_description: declared description\n\n"
                f"operator_note: {case.payload}\n\n"
                "overall_soundscape: declared ambience\n\n"
                "non_diegetic_music: N/A"
            )
            with self.subTest(case=case.case_id):
                result = parse_prompt(text, BASE_PROFILE, TaskMode.T2VA)
                self.assertFalse(result.canonical)
                self.assertTrue(any(case.payload in span.text for span in result.unparsed_spans))
                self.assertEqual(result.source_text, text)

    def test_label_collisions_and_path_traversal_are_rejected(self) -> None:
        asset = ReferenceAsset("image_a", MediaKind.IMAGE, AssetRole.REFERENCE, 1)
        registry = build_reference_registry((asset,))
        for case in LABEL_COLLISIONS:
            forged_kind = (
                BackendLabelKind.PICTURE
                if "wrong_ordinal" in case.case_id
                else BackendLabelKind.VIDEO
            )
            if "wrong_ordinal" in case.case_id:
                with self.subTest(case=case.case_id), self.assertRaises(ReferenceLabelError):
                    BackendLabel(
                        BackendTarget.COMFYUI_H3,
                        forged_kind,
                        1,
                        asset.asset_id,
                        case.payload,
                    )
            else:
                forged = BackendLabel(
                    BackendTarget.COMFYUI_H3,
                    forged_kind,
                    1,
                    asset.asset_id,
                    case.payload,
                )
                with self.subTest(case=case.case_id), self.assertRaises(ReferenceLabelError):
                    ReferenceRegistry((asset,), (forged,))
        with self.assertRaises(ContractValidationError):
            ReferenceAsset("../outside", MediaKind.IMAGE, AssetRole.REFERENCE, 1)
        self.assertEqual(registry.label_for("image_a").label, "<Picture 1>")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "safe.txt").write_text("safe", encoding="utf-8")
            for case in TRAVERSAL_LIKE_PATHS:
                with self.subTest(case=case.case_id), self.assertRaises(SecurityPolicyError):
                    validate_local_path(case.payload, root)

    def test_provider_output_mutation_and_residue_are_fail_closed(self) -> None:
        receipt = RedactedReceipt(
            provider="remote_custom",
            status="succeeded",
            task_id="task_123",
            request_fingerprint="a" * 64,
        )
        changed = dataclasses.replace(receipt, status="failed")
        self.assertEqual(receipt.status, "succeeded")
        self.assertEqual(changed.status, "failed")
        self.assertNotIn("credential", receipt.to_public_dict())

        document = render_base_prompt(make_plan(TaskMode.T2VA))
        contaminated = dataclasses.replace(
            document,
            text=document.text + "\nhttps://provider.invalid/task",
        )
        lint_result = lint_prompt(make_plan(TaskMode.T2VA), contaminated)
        self.assertIsInstance(lint_result, PromptLintResult)
        self.assertIn("security.provider_residue", {item.code for item in lint_result.diagnostics})
        finding = next(
            item for item in lint_result.diagnostics if item.code == "security.provider_residue"
        )
        self.assertNotIn("provider.invalid", finding.message)

    def test_seeded_bounded_mutations_terminate_and_preserve_source(self) -> None:
        state = 2_608_05

        def next_int(limit: int) -> int:
            nonlocal state
            state = (state * 1_103_515_245 + 12_345) & 0x7FFF_FFFF
            return state % limit

        profile = BASE_PROFILE
        alphabet = ("a", "Z", "\n", " ", "<", ">", "營", "\t", ":")
        for index in range(64):
            bases = (
                "integrated_multimodal_description: scene",
                "overall_soundscape: ambience",
                "unknown_field: note",
                "non_diegetic_music: N/A",
            )
            base = bases[next_int(len(bases))]
            mutation = "".join(alphabet[next_int(len(alphabet))] for _ in range(next_int(24)))
            text = f"{base}{mutation}"
            with self.subTest(index=index):
                try:
                    result = parse_prompt(text, profile, TaskMode.T2VA)
                except PromptParseError:
                    continue
                self.assertEqual(result.source_text, text)
                self.assertLessEqual(len(result.unparsed_spans), 256)

                plan = make_plan(TaskMode.T2VA)
                document = render_base_prompt(plan)
                mutated_document = dataclasses.replace(document, text=document.text + mutation)
                lint_result = lint_prompt(plan, mutated_document)
                self.assertLessEqual(len(lint_result.diagnostics), 512)

    def test_remote_url_validator_remains_network_free_and_bounded(self) -> None:
        for case in ALL_CASES:
            with self.subTest(case=case.case_id):
                try:
                    validate_remote_url(case.payload, allowed_hosts={"api.example.test"})
                except (SecurityPolicyError, ValueError):
                    pass

    def test_security_modules_have_no_optional_runtime_imports(self) -> None:
        forbidden = {
            "aiohttp",
            "comfy",
            "comfy_api",
            "cv2",
            "httpx",
            "moviepy",
            "numpy",
            "requests",
            "torch",
            "transformers",
        }
        for filename in ("security.py", "parsing.py", "linting.py"):
            module = ast.parse(
                (ROOT / "comfyui_h3_context" / "core" / filename).read_text(encoding="utf-8")
            )
            imports: set[str] = set()
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imports.add(node.module.split(".")[0])
            self.assertTrue(forbidden.isdisjoint(imports), filename)


if __name__ == "__main__":
    unittest.main()
