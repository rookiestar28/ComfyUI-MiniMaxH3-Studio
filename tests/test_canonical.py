"""M1-08 canonical serialization and material fingerprint tests."""

from __future__ import annotations

import ast
import json
import math
import unittest
from dataclasses import replace
from pathlib import Path

from test_context_reporting import sample_report

from comfyui_h3_context.core import (
    CANONICAL_SCHEMA,
    CanonicalArtifact,
    CanonicalizationError,
    FloatPrecision,
    ProviderIdentity,
    ProviderOutcome,
    ProviderReceipt,
    TimePoint,
    binary32_token,
    binary64_token,
    canonical_bytes,
    canonical_context_report_bytes,
    canonical_fingerprint,
    canonical_json,
    context_report_projection,
    fingerprint_context_report,
    float_token,
)

ROOT = Path(__file__).resolve().parents[1]


class CanonicalTests(unittest.TestCase):
    def test_mapping_order_nfc_and_compact_utf8_are_stable(self) -> None:
        first = {"b": "營業中", "a": [True, None, "e\u0301"]}
        second = {"a": [True, None, "é"], "b": "營業中"}
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        encoded = canonical_json(first)
        self.assertEqual(encoded, '{"a":[true,null,"é"],"b":"營業中"}')
        self.assertNotIn(" ", encoded)
        self.assertNotIn("\n", encoded)
        self.assertEqual(canonical_fingerprint(first), canonical_fingerprint(second))

    def test_float_tokens_have_fixed_width_and_reject_unsafe_values(self) -> None:
        self.assertEqual(binary32_token(1.0), "3f800000")
        self.assertEqual(binary64_token(1.0), "3ff0000000000000")
        self.assertEqual(binary32_token(-0.0), "00000000")
        self.assertEqual(binary64_token(-0.0), "0000000000000000")
        self.assertEqual(float_token(1, FloatPrecision.BINARY32), "3f800000")
        with self.assertRaises(CanonicalizationError):
            binary64_token(math.nan)
        with self.assertRaises(CanonicalizationError):
            binary64_token(math.inf)
        with self.assertRaises(CanonicalizationError):
            binary32_token(1e100)
        with self.assertRaises(CanonicalizationError):
            canonical_bytes({"duration": 1.0})

    def test_bounds_keys_and_unsupported_values_fail_closed(self) -> None:
        with self.assertRaises(CanonicalizationError):
            canonical_bytes({"bad key": "value"})
        with self.assertRaises(CanonicalizationError):
            canonical_bytes({"count": 2**53})
        with self.assertRaises(CanonicalizationError):
            canonical_bytes({"items": list(range(257))})
        nested: object = "value"
        for _ in range(34):
            nested = {"nested": nested}
        with self.assertRaises(CanonicalizationError):
            canonical_bytes({"items": nested})
        with self.assertRaises(CanonicalizationError):
            canonical_bytes({"items": {1, 2}})

    def test_report_projection_excludes_operational_ids_but_changes_material_text(self) -> None:
        report = sample_report()
        baseline = fingerprint_context_report(report)
        self.assertTrue(canonical_context_report_bytes(report))
        changed_report_id = replace(report, report_id="report_display_2")
        self.assertEqual(baseline, fingerprint_context_report(changed_report_id))

        changed_document_id = replace(
            report.prompt_document,
            document_id="prompt_display_2",
        )
        self.assertEqual(
            baseline,
            fingerprint_context_report(replace(report, prompt_document=changed_document_id)),
        )
        changed_text = replace(
            report.prompt_document,
            text="<Subject 1> a different subject.",
        )
        self.assertNotEqual(
            baseline,
            fingerprint_context_report(replace(report, prompt_document=changed_text)),
        )

    def test_provider_operational_metadata_is_excluded_but_outcome_is_material(self) -> None:
        report = sample_report()
        receipt = ProviderReceipt(
            "receipt_1",
            ProviderIdentity.REMOTE_CUSTOM,
            ProviderOutcome.SUCCEEDED,
            provider_version="provider-1",
            endpoint_revision="revision-1",
            task_id="task_1",
            input_fingerprint="a" * 64,
            output_fingerprint="b" * 64,
            redacted_message="redacted display detail",
        )
        remote_report = replace(report, provider_receipt=receipt)
        changed_task = replace(receipt, task_id="task_2", redacted_message="another detail")
        self.assertEqual(
            fingerprint_context_report(remote_report),
            fingerprint_context_report(replace(remote_report, provider_receipt=changed_task)),
        )
        changed_outcome = replace(receipt, outcome=ProviderOutcome.MODERATED)
        self.assertNotEqual(
            fingerprint_context_report(remote_report),
            fingerprint_context_report(replace(remote_report, provider_receipt=changed_outcome)),
        )

    def test_time_source_spelling_is_normalized_to_typed_seconds(self) -> None:
        report = sample_report()
        graph = report.plan.intent_graph
        segment = graph.segments[0]
        normalized_segment = replace(
            segment,
            start=TimePoint.from_text("00.000"),
            end=TimePoint.from_text("00:05.000"),
        )
        normalized_graph = replace(
            graph,
            effective_duration=TimePoint.from_text("00:05.000"),
            segments=(normalized_segment,),
        )
        normalized_plan = replace(report.plan, intent_graph=normalized_graph)
        normalized_report = replace(report, plan=normalized_plan)
        self.assertEqual(
            fingerprint_context_report(report), fingerprint_context_report(normalized_report)
        )

    def test_canonical_artifact_round_trip_and_projection_schema(self) -> None:
        report = sample_report()
        projection = context_report_projection(report)
        self.assertEqual(projection["projection_schema"], CANONICAL_SCHEMA)
        artifact = CanonicalArtifact.from_report(report)
        self.assertEqual(artifact.schema, CANONICAL_SCHEMA)
        self.assertEqual(artifact.payload, canonical_context_report_bytes(report))
        self.assertEqual(artifact.fingerprint, fingerprint_context_report(report))
        self.assertEqual(
            json.loads(artifact.payload.decode("utf-8"))["projection_schema"], CANONICAL_SCHEMA
        )

    def test_canonical_module_has_no_optional_runtime_imports(self) -> None:
        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "canonical.py").read_text(encoding="utf-8")
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
