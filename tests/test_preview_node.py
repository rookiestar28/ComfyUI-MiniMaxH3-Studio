"""M3-05 bounded redacted context preview node tests."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    MAX_PREVIEW_BYTES,
    ContextReport,
    PreviewStatus,
    TaskMode,
    build_context_preview,
    fingerprint_context_report,
)
from comfyui_h3_context.nodes import (
    PREVIEW_NODE_ID,
    PREVIEW_SOCKET_TYPE,
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextPreviewNode,
    H3ContextRequestNode,
    H3ReferenceRegistryNode,
    PreviewNodeError,
)

ROOT = Path(__file__).resolve().parents[1]
NODE_MODULE = ROOT / "comfyui_h3_context" / "nodes.py"
PREVIEW_MODULE = ROOT / "comfyui_h3_context" / "core" / "preview.py"


def make_report(
    intent: str = "Preserve this declared intent.",
    mode: TaskMode = TaskMode.T2VA,
    with_reference: bool = False,
) -> ContextReport:
    raw = H3ContextRequestNode().build_request(mode, intent)[0]
    registry = None
    if with_reference:
        registry = H3ReferenceRegistryNode().build_registry(images=[object()])[0]
    plan = H3ContextPlanNode().build_plan(raw, registry)[0]
    return H3ContextCompilerNode().compile(plan)[1]


class PreviewNodeTests(unittest.TestCase):
    def test_metadata_preserves_prompt_output_and_adds_typed_preview(self) -> None:
        inputs = H3ContextPreviewNode.INPUT_TYPES()
        self.assertEqual(H3ContextPreviewNode.NODE_ID, PREVIEW_NODE_ID)
        self.assertEqual(tuple(inputs), ("required",))
        self.assertEqual(tuple(inputs["required"]), ("report",))
        self.assertEqual(
            H3ContextPreviewNode.RETURN_TYPES, ("H3_PROMPT_STRING", PREVIEW_SOCKET_TYPE)
        )
        self.assertEqual(H3ContextPreviewNode.RETURN_NAMES, ("prompt", "preview"))

        report = make_report(with_reference=True, mode=TaskMode.REF2VA)
        prompt, preview = H3ContextPreviewNode().preview(report)
        self.assertEqual(prompt, report.prompt_document.text)
        self.assertEqual(preview.report_id, report.report_id)
        self.assertEqual(preview.provider_identity, "manual")
        labels = cast(list[dict[str, object]], preview.reference_mapping["labels"])
        self.assertEqual(labels[0]["label"], "<Picture 1>")

    def test_preview_exposes_required_sections_and_is_json_safe(self) -> None:
        report = make_report(with_reference=True, mode=TaskMode.REF2VA)
        before = report.to_wire()
        preview = build_context_preview(report)
        wire = preview.to_wire()

        self.assertEqual(
            {
                "request",
                "reference_mapping",
                "evidence",
                "plan",
                "prompt",
                "validation",
                "provider_identity",
                "provider_receipt",
                "fingerprints",
                "limitations",
                "diagnostics",
            },
            set(wire)
            - {
                "preview_schema",
                "preview_id",
                "schema_version",
                "report_id",
                "status",
                "omitted_fields",
                "redacted_fields",
            },
        )
        fingerprints = cast(dict[str, str], wire["fingerprints"])
        self.assertEqual(fingerprints["report"], fingerprint_context_report(report))
        self.assertEqual(json.loads(json.dumps(wire, ensure_ascii=False)), wire)
        self.assertEqual(report.to_wire(), before)

    def test_preview_redacts_untrusted_text_without_mutating_source(self) -> None:
        sensitive_url = "https://private.example/task?sig=do-not-echo"
        report = make_report(
            "Keep "
            + sensitive_url
            + " token=do-not-echo Authorization: Bearer bearer-secret /mnt/private/shot.mp4",
        )
        source_prompt = report.prompt_document.text
        preview = H3ContextPreviewNode().preview(report)[1]
        encoded = json.dumps(preview.to_wire(), ensure_ascii=False)

        self.assertNotIn(sensitive_url, encoded)
        self.assertNotIn("do-not-echo", encoded)
        self.assertNotIn("bearer-secret", encoded)
        self.assertNotIn("/mnt/private/shot.mp4", encoded)
        self.assertTrue(preview.redacted_fields)
        self.assertEqual(report.prompt_document.text, source_prompt)

    def test_large_preview_is_explicitly_truncated_and_bounded(self) -> None:
        report = make_report("x" * 65_000)
        preview = build_context_preview(report)
        encoded = json.dumps(preview.to_wire(), ensure_ascii=False).encode("utf-8")

        self.assertEqual(preview.status, PreviewStatus.TRUNCATED)
        self.assertTrue(preview.omitted_fields)
        self.assertLessEqual(len(encoded), MAX_PREVIEW_BYTES)
        self.assertIn("[TRUNCATED]", cast(str, preview.prompt["text"]))

    def test_preview_is_deterministic_and_invalid_input_fails_closed(self) -> None:
        report = make_report()
        first = H3ContextPreviewNode().preview(report)[1]
        second = H3ContextPreviewNode().preview(report)[1]
        self.assertEqual(first.to_wire(), second.to_wire())

        with self.assertRaises(PreviewNodeError) as context:
            H3ContextPreviewNode().preview(object())
        self.assertIn("invalid_report", str(context.exception))

    def test_preview_modules_have_no_optional_runtime_imports(self) -> None:
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
        for path in (NODE_MODULE, PREVIEW_MODULE):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imports.add(node.module.split(".", 1)[0])
            self.assertTrue(forbidden.isdisjoint(imports), imports)


if __name__ == "__main__":
    unittest.main()
