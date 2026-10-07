"""M1-03 immutable hard-constraint contract tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    ContractValidationError,
    EvidenceLevel,
    H3ContextContract,
    ModelVariant,
    ProfileIdentity,
    PromptProfile,
    ProviderIdentity,
    RawContextRequest,
    TaskMode,
    normalize_request,
)
from comfyui_h3_context.core.constraints import (
    ConstraintConflictError,
    ConstraintTransformation,
    ContentScope,
    DirectiveTarget,
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    KeepChangeAction,
    KeepChangeDirective,
    RequiredContent,
    TimePoint,
    TimingConstraint,
    TransformationField,
    merge_constraints,
    normalize_constraints,
)

ROOT = Path(__file__).resolve().parents[1]


class ImmutableConstraintTests(unittest.TestCase):
    def test_exact_text_is_preserved_without_unicode_or_whitespace_rewrite(self) -> None:
        declared = "  Café e\u0301...\n\t营业中?!  "
        value = ExactTextConstraint(
            constraint_id="dialogue_1",
            kind=ExactTextKind.DIALOGUE,
            text=declared,
            language="zh-Hant",
            location="shot-1",
        )
        normalized = normalize_constraints([value])

        self.assertIs(normalized.constraints[0], value)
        self.assertEqual(cast(ExactTextConstraint, normalized.constraints[0]).text, declared)
        self.assertEqual(value.to_wire()["text"], declared)
        with self.assertRaises(FrozenInstanceError):
            value.text = "rewritten"  # type: ignore[misc]

    def test_timing_keeps_source_spelling_and_has_typed_decimal_value(self) -> None:
        start = TimePoint.from_text("00:03.500")
        end = TimePoint.from_text("00:05.000")
        timing = TimingConstraint("timing_1", start=start, end=end, label="dialogue entrance")

        self.assertEqual(start.raw, "00:03.500")
        self.assertEqual(start.seconds, Decimal("3.500"))
        self.assertEqual(timing.to_wire()["start"], {"raw": "00:03.500", "seconds": "3.500"})
        self.assertEqual(timing.to_wire()["end"], {"raw": "00:05.000", "seconds": "5.000"})
        with self.assertRaises(ContractValidationError):
            TimePoint.from_text(" 3.5")
        with self.assertRaises(ContractValidationError):
            TimePoint.from_text("-1")
        with self.assertRaises(ContractValidationError):
            TimingConstraint("timing_2", start=end, end=start)

    def test_required_forbidden_and_keep_change_have_explicit_ownership(self) -> None:
        required = RequiredContent(
            "required_1", ContentScope.VISIBLE_TEXT, 'the sign reads "营业中"', location="shot-1"
        )
        forbidden = ForbiddenContent(
            "forbidden_1", ContentScope.AUDIO, "no audience applause", location="all"
        )
        keep = KeepChangeDirective(
            "directive_1", KeepChangeAction.KEEP, DirectiveTarget.SUBJECT, "the red coat"
        )
        change = KeepChangeDirective(
            "directive_2",
            KeepChangeAction.CHANGE,
            DirectiveTarget.CAMERA,
            "camera movement",
            replacement="slow push-in",
        )
        constraints = normalize_constraints([required, forbidden, keep, change])

        self.assertEqual(len(constraints.constraints), 4)
        self.assertEqual(cast(dict[str, object], change.to_wire())["replacement"], "slow push-in")
        with self.assertRaises(ContractValidationError):
            KeepChangeDirective(
                "bad_keep", KeepChangeAction.KEEP, DirectiveTarget.STYLE, "style", "new style"
            )
        with self.assertRaises(ContractValidationError):
            KeepChangeDirective(
                "bad_change", KeepChangeAction.CHANGE, DirectiveTarget.STYLE, "style"
            )

    def test_normalization_and_merge_copy_order_and_reject_conflicts(self) -> None:
        first = ExactTextConstraint("text_1", ExactTextKind.LYRICS, "la\u0301  \n")
        second = RequiredContent("required_1", ContentScope.ACTION, "the door opens")
        normalized = normalize_constraints([first, second])
        source = [first, second]
        source.clear()

        self.assertEqual(normalized.constraints, (first, second))
        merged = merge_constraints(normalized, HardConstraintSet((first,)))
        self.assertEqual(merged.constraints, (first, second))

        conflict = ExactTextConstraint("text_1", ExactTextKind.LYRICS, "different")
        with self.assertRaises(ConstraintConflictError):
            merge_constraints(normalized, HardConstraintSet((conflict,)))

    def test_authorized_transformation_is_explicit_and_stale_replacements_fail(self) -> None:
        original = ExactTextConstraint("text_1", ExactTextKind.VISIBLE_TEXT, "原文?!")
        constraints = HardConstraintSet((original,))
        record = ConstraintTransformation(
            transformation_id="change_1",
            constraint_id="text_1",
            field=TransformationField.TEXT,
            original_value="原文?!",
            transformed_value="原文。",
            authorized_by="user_explicit",
            reason="user supplied revised punctuation",
        )
        transformed = constraints.apply_authorized_transformation(record)
        updated = cast(ExactTextConstraint, transformed.constraints[0])

        self.assertEqual(original.text, "原文?!")
        self.assertEqual(updated.text, "原文。")
        self.assertEqual(transformed.transformations, (record,))
        self.assertEqual(transformed.to_wire()["transformations"], [record.to_wire()])
        with self.assertRaises(ContractValidationError):
            constraints.apply_authorized_transformation(
                ConstraintTransformation(
                    "change_2",
                    "text_1",
                    TransformationField.TEXT,
                    "wrong source",
                    "new",
                    "user_explicit",
                    "stale request",
                )
            )
        with self.assertRaises(ContractValidationError):
            HardConstraintSet((original,), (record,))
        with self.assertRaises(ContractValidationError):
            ConstraintTransformation(
                "change_3", "text_1", TransformationField.TEXT, "same", "same", "", ""
            )

    def test_timing_transformation_preserves_original_record(self) -> None:
        timing = TimingConstraint("timing_1", TimePoint.from_text("1.00"))
        record = ConstraintTransformation(
            "change_time",
            "timing_1",
            TransformationField.START,
            "1.00",
            "1.250",
            "user_explicit",
            "retime the entrance",
        )
        transformed = HardConstraintSet((timing,)).apply_authorized_transformation(record)
        updated = cast(TimingConstraint, transformed.constraints[0])

        self.assertEqual(timing.start.raw, "1.00")
        self.assertEqual(updated.start.raw, "1.250")
        self.assertEqual(transformed.transformations[0].original_value, "1.00")

        second_record = ConstraintTransformation(
            "change_time_again",
            "timing_1",
            TransformationField.START,
            "1.250",
            "2.000",
            "user_explicit",
            "retime again",
        )
        twice = transformed.apply_authorized_transformation(second_record)
        self.assertEqual(cast(TimingConstraint, twice.constraints[0]).start.raw, "2.000")

    def test_contract_and_normalized_request_carry_constraints(self) -> None:
        hard = HardConstraintSet(
            (ExactTextConstraint("dialogue_1", ExactTextKind.DIALOGUE, "Keep this exact."),)
        )
        contract = H3ContextContract(
            schema_version=CURRENT_SCHEMA_VERSION,
            profile=ProfileIdentity(PromptProfile.BASE, CURRENT_SCHEMA_VERSION),
            task_mode=TaskMode.T2VA,
            model_variant=ModelVariant.BASE_FL2VA,
            evidence_level=EvidenceLevel.OFFICIAL,
            provider=ProviderIdentity.MANUAL,
            hard_constraints=hard,
        )
        wire = contract.to_wire()
        self.assertEqual(cast(dict[str, object], wire["hard_constraints"]), hard.to_wire())

        normalized = normalize_request(
            RawContextRequest(
                mode=TaskMode.T2VA,
                user_intent="A scene",
                hard_constraints=hard,
            )
        )
        self.assertTrue(normalized.is_valid)
        assert normalized.request is not None
        self.assertEqual(normalized.request.hard_constraints, hard)

        invalid = normalize_request(
            RawContextRequest(
                mode=TaskMode.T2VA,
                user_intent="A scene",
                hard_constraints=cast(HardConstraintSet, object()),
            )
        )
        self.assertFalse(invalid.is_valid)
        self.assertIsNone(invalid.request)
        self.assertIn("invalid_hard_constraints", {item.code for item in invalid.diagnostics})

    def test_invalid_constraint_types_and_duplicate_ids_fail_closed(self) -> None:
        with self.assertRaises(ContractValidationError):
            normalize_constraints([cast(ExactTextConstraint, object())])
        with self.assertRaises(ContractValidationError):
            HardConstraintSet(
                (
                    ExactTextConstraint("same", ExactTextKind.DIALOGUE, "a"),
                    RequiredContent("same", ContentScope.GENERAL, "b"),
                )
            )
        orphan = ConstraintTransformation(
            "bad", "missing", TransformationField.TEXT, "a", "b", "user_explicit", "reason"
        )
        with self.assertRaises(ContractValidationError):
            HardConstraintSet().apply_authorized_transformation(orphan)

    def test_core_constraint_module_has_no_optional_runtime_imports(self) -> None:
        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "constraints.py").read_text(encoding="utf-8")
        )
        optional_roots = {"comfy", "torch", "requests", "httpx", "PIL", "numpy"}
        imported: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(optional_roots.isdisjoint(imported))

    def test_wire_is_json_serializable_and_schema_declares_hard_constraints(self) -> None:
        hard = HardConstraintSet((ForbiddenContent("forbidden_1", ContentScope.VISUAL, "no logo"),))
        encoded = json.dumps(hard.to_wire(), ensure_ascii=False, sort_keys=True)
        self.assertIn("no logo", encoded)
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "h3_context_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("hard_constraints", schema["required"])
        self.assertEqual(
            schema["properties"]["hard_constraints"]["$ref"], "#/$defs/HardConstraintSet"
        )
        hard_schema = schema["$defs"]["HardConstraintSet"]
        self.assertIn("constraints", hard_schema["required"])


if __name__ == "__main__":
    unittest.main()
