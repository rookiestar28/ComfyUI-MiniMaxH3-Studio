"""M13-09 source-profiled rendering, parsing, semantic validation, and budgets."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from jsonschema import Draft202012Validator
from test_full_rendering import full_plan
from test_rendering import frame_registry, make_plan

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    OFFICIAL_H3_BASE_GUIDE_DIGEST,
    OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
    OFFICIAL_H3_GUIDE_REVISION,
    ContractValidationError,
    EvidenceLevel,
    ExactTextConstraint,
    ExactTextKind,
    HardConstraintSet,
    ProfileIdentity,
    PromptBudget,
    PromptBudgetUsage,
    PromptDocument,
    PromptProfile,
    SourceProfileBinding,
    SourceProfileDecision,
    SourceProfiledPromptResult,
    TaskMode,
    TimePoint,
    build_official_source_profile_binding,
    build_source_profile_binding,
    render_parsed_prompt,
    render_profiled_prompt,
    validate_legacy_source_profiled_prompt_wire,
    validate_profiled_prompt,
    validate_source_profiled_prompt_wire,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_PROFILE = ProfileIdentity(PromptProfile.BASE, CURRENT_SCHEMA_VERSION)
FULL_PROFILE = ProfileIdentity(PromptProfile.FULL_REFERENCE, CURRENT_SCHEMA_VERSION)


class SourceProfiledPromptTests(unittest.TestCase):
    def _official_binding(self, profile: ProfileIdentity) -> SourceProfileBinding:
        digest = (
            OFFICIAL_H3_BASE_GUIDE_DIGEST
            if profile.name is PromptProfile.BASE
            else OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST
        )
        return build_official_source_profile_binding(
            profile,
            observed_revision=OFFICIAL_H3_GUIDE_REVISION,
            observed_digest=digest,
        )

    @staticmethod
    def _replace_document_text(document: PromptDocument, old: str, new: str) -> PromptDocument:
        return replace(
            document,
            text=document.text.replace(old, new),
            sections=tuple(
                replace(section, body=section.body.replace(old, new))
                for section in document.sections
            ),
        )

    def test_base_round_trip_is_source_bound_and_backend_mapping_is_visible(self) -> None:
        binding = build_official_source_profile_binding(
            BASE_PROFILE,
            observed_revision=OFFICIAL_H3_GUIDE_REVISION,
            observed_digest=OFFICIAL_H3_BASE_GUIDE_DIGEST,
        )

        result = render_profiled_prompt(make_plan(TaskMode.T2VA), binding)

        self.assertEqual(binding.decision, SourceProfileDecision.ACCEPTED)
        self.assertTrue(result.is_valid)
        self.assertTrue(result.parse_result.is_valid)
        self.assertEqual(render_parsed_prompt(result.parse_result), result.document.text)
        self.assertEqual(result.backend_labels, ())
        self.assertEqual(result.to_wire()["schema"], "h3-source-profiled-prompt/2")

    def test_full_reference_round_trip_keeps_copy_reference_and_timestamp_semantics(self) -> None:
        binding = self._official_binding(FULL_PROFILE)

        result = render_profiled_prompt(full_plan(), binding)

        self.assertTrue(result.is_valid)
        self.assertIn("[Shot 2] At 00:02.000", result.document.text)
        self.assertIn("<Video 1> is copied in full into this shot.", result.document.text)
        self.assertIn("<Subject 1>: fully_preserved -", result.document.text)
        for internal in ("event_1", "retention_1", "full_copy"):
            self.assertNotIn(internal, result.document.text)
        self.assertEqual(
            tuple(label.label for label in result.backend_labels),
            ("<Picture 1>", "<Picture 2>", "<Audio 1>", "<Video 1>", "<Audio 2>"),
        )

    def test_source_drift_is_explicit_and_fail_closed(self) -> None:
        binding = build_official_source_profile_binding(
            BASE_PROFILE,
            observed_revision="changed-revision",
            observed_digest="0" * 64,
        )

        result = render_profiled_prompt(make_plan(TaskMode.T2VA), binding)

        self.assertEqual(binding.decision, SourceProfileDecision.DRIFTED)
        self.assertFalse(result.is_valid)
        self.assertIn("source.drifted", {item.code for item in result.diagnostics})
        self.assertFalse(binding.is_official)

    def test_modified_evidence_cannot_masquerade_as_official(self) -> None:
        binding = build_source_profile_binding(
            BASE_PROFILE,
            source_revision=OFFICIAL_H3_GUIDE_REVISION,
            source_digest=OFFICIAL_H3_BASE_GUIDE_DIGEST,
            evidence_level=EvidenceLevel.MODIFIED,
        )

        result = render_profiled_prompt(make_plan(TaskMode.T2VA), binding)

        self.assertEqual(binding.decision, SourceProfileDecision.MODIFIED)
        self.assertFalse(binding.is_official)
        self.assertFalse(result.is_valid)
        self.assertIn("source.modified_evidence", {item.code for item in result.diagnostics})

    def test_budget_is_configurable_and_overflow_is_reported(self) -> None:
        plan = make_plan(TaskMode.T2VA)
        binding = build_official_source_profile_binding(
            BASE_PROFILE,
            observed_revision=OFFICIAL_H3_GUIDE_REVISION,
            observed_digest=OFFICIAL_H3_BASE_GUIDE_DIGEST,
        )
        document = render_profiled_prompt(plan, binding).document
        budget = PromptBudget(max_characters=len(document.text) - 1)

        result = validate_profiled_prompt(plan, document, binding, budget)

        self.assertFalse(result.is_valid)
        self.assertIn("budget.characters", {item.code for item in result.diagnostics})
        self.assertGreater(result.usage.estimated_tokens, 0)

    def test_every_supported_base_mode_has_a_canonical_source_bound_round_trip(self) -> None:
        binding = self._official_binding(BASE_PROFILE)

        for mode in (TaskMode.T2VA, TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA):
            with self.subTest(mode=mode.value):
                plan = make_plan(mode, frame_registry(mode))
                result = render_profiled_prompt(plan, binding)
                self.assertTrue(result.is_valid, result.diagnostics)
                self.assertEqual(result.document.task_mode, mode)
                self.assertEqual(render_parsed_prompt(result.parse_result), result.document.text)
                self.assertEqual(result.backend_labels, plan.request.reference_registry.labels)

    def test_token_and_per_section_budgets_fail_closed_with_distinct_codes(self) -> None:
        plan = make_plan(TaskMode.T2VA)
        binding = self._official_binding(BASE_PROFILE)
        document = render_profiled_prompt(plan, binding).document

        token_result = validate_profiled_prompt(
            plan,
            document,
            binding,
            PromptBudget(max_estimated_tokens=1),
        )
        section_result = validate_profiled_prompt(
            plan,
            document,
            binding,
            PromptBudget(section_character_limits=(("overall_soundscape", 1),)),
        )

        self.assertIn("budget.estimated_tokens", {x.code for x in token_result.diagnostics})
        self.assertIn("budget.section_characters", {x.code for x in section_result.diagnostics})
        self.assertEqual(
            section_result.usage.section_characters[1],
            ("overall_soundscape", len(document.sections[1].body)),
        )

    def test_profile_binding_mismatch_and_experimental_evidence_are_rejected(self) -> None:
        mismatched = render_profiled_prompt(
            make_plan(TaskMode.T2VA), self._official_binding(FULL_PROFILE)
        )
        experimental = build_source_profile_binding(
            BASE_PROFILE,
            source_revision=OFFICIAL_H3_GUIDE_REVISION,
            source_digest=OFFICIAL_H3_BASE_GUIDE_DIGEST,
            evidence_level=EvidenceLevel.EXPERIMENTAL,
        )

        self.assertFalse(mismatched.is_valid)
        self.assertIn("source.profile_mismatch", {x.code for x in mismatched.diagnostics})
        self.assertEqual(experimental.decision, SourceProfileDecision.UNSUPPORTED)
        self.assertFalse(render_profiled_prompt(make_plan(TaskMode.T2VA), experimental).is_valid)

    def test_binding_decision_cannot_contradict_evidence_or_use_a_polymorphic_proxy(self) -> None:
        binding = self._official_binding(BASE_PROFILE)

        with self.assertRaises(ContractValidationError):
            replace(binding, evidence_level=EvidenceLevel.MODIFIED)
        with self.assertRaises(ContractValidationError):
            build_source_profile_binding(
                BASE_PROFILE,
                source_revision=OFFICIAL_H3_GUIDE_REVISION,
                source_digest=OFFICIAL_H3_BASE_GUIDE_DIGEST,
                evidence_level=EvidenceLevel.OFFICIAL,
            )

        class BindingProxy(SourceProfileBinding):
            pass

        with self.assertRaises(ContractValidationError):
            BindingProxy(*tuple(getattr(binding, field) for field in binding.__slots__))

    def test_builders_and_public_binding_surfaces_reject_hostile_current_values(self) -> None:
        class HostileRevision:
            def __eq__(self, other: object) -> bool:
                raise RuntimeError("caller equality must not run")

        for revision, digest in (
            (HostileRevision(), OFFICIAL_H3_BASE_GUIDE_DIGEST),
            (Mock(spec=str), OFFICIAL_H3_BASE_GUIDE_DIGEST),
            (OFFICIAL_H3_GUIDE_REVISION, Mock(spec=str)),
        ):
            with self.subTest(revision=revision, digest=digest):
                with self.assertRaises(ContractValidationError):
                    build_official_source_profile_binding(
                        BASE_PROFILE,
                        observed_revision=revision,  # type: ignore[arg-type]
                        observed_digest=digest,
                    )
        with self.assertRaises(ContractValidationError):
            build_source_profile_binding(
                BASE_PROFILE,
                source_revision=OFFICIAL_H3_GUIDE_REVISION,
                source_digest=OFFICIAL_H3_BASE_GUIDE_DIGEST,
                evidence_level=Mock(spec=EvidenceLevel),
            )

        drifted = build_official_source_profile_binding(
            BASE_PROFILE,
            observed_revision="changed-revision",
            observed_digest="0" * 64,
        )
        object.__setattr__(drifted, "decision", SourceProfileDecision.ACCEPTED)
        with self.assertRaises(ContractValidationError):
            _ = drifted.is_official
        with self.assertRaises(ContractValidationError):
            drifted.to_wire()

    def test_mutated_budget_document_usage_and_direct_result_fail_with_contract_errors(
        self,
    ) -> None:
        plan = make_plan(TaskMode.T2VA)
        binding = self._official_binding(BASE_PROFILE)
        valid = render_profiled_prompt(plan, binding)

        for malformed in (1_000_001, -1, True, "100", Mock(spec=int)):
            budget = PromptBudget()
            object.__setattr__(budget, "max_characters", malformed)
            with self.subTest(budget=malformed):
                with self.assertRaises(ContractValidationError):
                    validate_profiled_prompt(plan, valid.document, binding, budget)

        document = valid.document
        object.__setattr__(document, "text", Mock(spec=str))
        with self.assertRaises(ContractValidationError):
            validate_profiled_prompt(plan, document, binding)
        with self.assertRaises(ContractValidationError):
            _ = valid.is_valid

        with self.assertRaises(ContractValidationError):
            PromptBudgetUsage(-1, 1, ())
        with self.assertRaises(ContractValidationError):
            SourceProfiledPromptResult(
                binding,
                Mock(),
                Mock(),
                (),
                (),
                PromptBudget(),
                Mock(),
            )

    def test_positive_speaker_copy_mode_and_label_ownership_substitutions_are_rejected(
        self,
    ) -> None:
        speaker_plan = make_plan(
            TaskMode.T2VA,
            constraints=HardConstraintSet(
                (
                    ExactTextConstraint(
                        "dialogue_1", ExactTextKind.DIALOGUE, "(S1) Hello", "English"
                    ),
                )
            ),
        )
        base_binding = self._official_binding(BASE_PROFILE)
        speaker = render_profiled_prompt(speaker_plan, base_binding).document
        wrong_speaker = self._replace_document_text(speaker, "(S1)", "(S2)")
        speaker_result = validate_profiled_prompt(speaker_plan, wrong_speaker, base_binding)
        self.assertIn("semantic.speaker_id", {x.code for x in speaker_result.diagnostics})

        plan = full_plan()
        binding = self._official_binding(FULL_PROFILE)
        document = render_profiled_prompt(plan, binding).document
        wrong_copy = self._replace_document_text(
            document,
            "is copied in full into this shot",
            "is used as a reference for this shot",
        )
        swapped = self._replace_document_text(document, "<Picture 1>", "<Picture swap>")
        swapped = self._replace_document_text(swapped, "<Picture 2>", "<Picture 1>")
        swapped = self._replace_document_text(swapped, "<Picture swap>", "<Picture 2>")

        copy_result = validate_profiled_prompt(plan, wrong_copy, binding)
        label_result = validate_profiled_prompt(plan, swapped, binding)
        self.assertIn("semantic.copy_mode", {x.code for x in copy_result.diagnostics})
        self.assertIn("backend.label_ownership", {x.code for x in label_result.diagnostics})

    def test_binding_wire_exposes_expected_and_observed_fingerprints(self) -> None:
        binding = self._official_binding(BASE_PROFILE)
        wire = binding.to_wire()

        self.assertEqual(wire["source_revision"], OFFICIAL_H3_GUIDE_REVISION)
        self.assertEqual(wire["source_digest"], OFFICIAL_H3_BASE_GUIDE_DIGEST)
        self.assertEqual(wire["expected_revision"], OFFICIAL_H3_GUIDE_REVISION)
        self.assertEqual(wire["expected_digest"], OFFICIAL_H3_BASE_GUIDE_DIGEST)
        fingerprint = wire["binding_fingerprint"]
        self.assertIsInstance(fingerprint, str)
        assert isinstance(fingerprint, str)
        self.assertEqual(len(fingerprint), 64)

    def test_wire_envelope_validates_against_the_committed_schema(self) -> None:
        result = render_profiled_prompt(
            make_plan(TaskMode.T2VA), self._official_binding(BASE_PROFILE)
        ).to_wire()
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "source_profiled_prompt_v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)
        Draft202012Validator.check_schema(schema)

        self.assertNotIn("is_valid", result)
        parse_wire = result["parse_result"]
        assert isinstance(parse_wire, dict)
        self.assertNotIn("source_text", parse_wire)
        self.assertEqual(list(validator.iter_errors(result)), [])
        wrong_schema = dict(result)
        wrong_schema["schema"] = "wrong"
        extra_member = dict(result)
        extra_member["unexpected"] = True
        forged_binding = dict(result)
        binding_wire = result["binding"]
        assert isinstance(binding_wire, dict)
        forged_binding["binding"] = {**binding_wire, "decision": "forged"}
        accepted_modified = json.loads(json.dumps(result))
        accepted_modified["binding"]["evidence_level"] = "modified"
        wrong_digest = json.loads(json.dumps(result))
        wrong_digest["binding"]["source_digest"] = "0" * 64
        wrong_uri = json.loads(json.dumps(result))
        wrong_uri["binding"]["source_uri"] = "https://evil.invalid/guide"
        open_span = json.loads(json.dumps(result))
        open_span["parse_result"]["unparsed_spans"] = [{"arbitrary": True}]
        for mutation in (
            wrong_schema,
            extra_member,
            forged_binding,
            accepted_modified,
            wrong_digest,
            wrong_uri,
            open_span,
        ):
            with self.subTest(mutation=mutation):
                self.assertNotEqual(list(validator.iter_errors(mutation)), [])

    def test_wire_semantic_validator_rejects_non_schema_cross_field_contradictions(self) -> None:
        wire = render_profiled_prompt(
            make_plan(TaskMode.T2VA), self._official_binding(BASE_PROFILE)
        ).to_wire()
        validate_source_profiled_prompt_wire(wire)
        overflow_wire = render_profiled_prompt(
            make_plan(TaskMode.T2VA),
            self._official_binding(BASE_PROFILE),
            PromptBudget(max_characters=1),
        ).to_wire()
        validate_source_profiled_prompt_wire(overflow_wire)

        contradictory_budget = json.loads(json.dumps(wire))
        contradictory_budget["budget"]["max_characters"] = 1
        stale_usage = json.loads(json.dumps(wire))
        stale_usage["usage"]["characters"] += 1
        stale_fingerprint = json.loads(json.dumps(wire))
        stale_fingerprint["binding"]["binding_fingerprint"] = "0" * 64

        for mutation in (contradictory_budget, stale_usage, stale_fingerprint):
            with self.subTest(mutation=mutation):
                # JSON Schema owns structure; this production validator owns joins that
                # Draft 2020-12 cannot express (hashes, lengths, and inequalities).
                with self.assertRaises(ContractValidationError):
                    validate_source_profiled_prompt_wire(mutation)

    def test_legacy_ready_report_is_inspectable_but_cannot_authorize_current_scope(self) -> None:
        wire = render_profiled_prompt(
            make_plan(TaskMode.T2VA), self._official_binding(BASE_PROFILE)
        ).to_wire()
        old = json.loads(json.dumps(wire))
        old["schema"] = "h3-source-profiled-prompt/1"
        old["conformance"] = {
            "schema": "h3.context.guide_conformance.v1",
            "readiness": "ready",
            "reasons": [],
        }
        original_text = old["document"]["text"]
        validate_legacy_source_profiled_prompt_wire(old)
        self.assertEqual(old["document"]["text"], original_text)
        with self.assertRaises(ContractValidationError):
            validate_source_profiled_prompt_wire(old)
        old["schema"] = "h3-source-profiled-prompt/2"
        with self.assertRaises(ContractValidationError):
            validate_source_profiled_prompt_wire(old)

    def test_speaker_id_and_na_semantics_are_checked_without_rewriting_text(self) -> None:
        plan = make_plan(
            TaskMode.T2VA,
            constraints=HardConstraintSet(
                (ExactTextConstraint("dialogue_1", ExactTextKind.DIALOGUE, "Hello", "English"),)
            ),
        )
        binding = build_official_source_profile_binding(
            BASE_PROFILE,
            observed_revision=OFFICIAL_H3_GUIDE_REVISION,
            observed_digest=OFFICIAL_H3_BASE_GUIDE_DIGEST,
        )
        source = render_profiled_prompt(plan, binding).document
        malformed = replace(
            source,
            text=source.text.replace("(S1) says: <d>[English]", "(S0) says: <d>[English]").replace(
                "overall_soundscape: soft morning street ambience", "overall_soundscape: N/A"
            ),
        )
        result = validate_profiled_prompt(plan, malformed, binding)

        self.assertFalse(result.is_valid)
        codes = {item.code for item in result.diagnostics}
        self.assertIn("semantic.speaker_id", codes)
        self.assertIn("semantic.na_allowed", codes)

    def test_unknown_backend_label_is_rejected(self) -> None:
        plan = full_plan()
        binding = self._official_binding(FULL_PROFILE)
        source = render_profiled_prompt(plan, binding).document
        malformed = replace(source, text=source.text.replace("<Video 1>", "<Video 9>"))

        result = validate_profiled_prompt(plan, malformed, binding)

        self.assertFalse(result.is_valid)
        self.assertIn("backend.missing_label", {item.code for item in result.diagnostics})

    def test_malformed_full_reference_timestamp_is_rejected_without_repair(self) -> None:
        plan = full_plan()
        binding = self._official_binding(FULL_PROFILE)
        source = render_profiled_prompt(plan, binding).document
        malformed = replace(source, text=source.text.replace("At 00:02.000", "At 0:2"))

        result = validate_profiled_prompt(plan, malformed, binding)

        self.assertFalse(result.is_valid)
        self.assertIn("semantic.timestamp", {item.code for item in result.diagnostics})
        self.assertEqual(result.document.text, malformed.text)

    def test_multi_digit_full_reference_timestamp_drift_is_rejected(self) -> None:
        plan = full_plan()
        first, second = plan.intent_graph.segments
        segments = tuple(
            replace(
                second if ordinal == 2 else first,
                segment_id=f"segment_{ordinal}",
                start=TimePoint.from_text(str((ordinal - 1) / 2)),
                end=(
                    plan.intent_graph.effective_duration
                    if ordinal == 10
                    else TimePoint.from_text(str(ordinal / 2))
                ),
                event_ids=("event_1",) if ordinal == 2 else (),
            )
            for ordinal in range(1, 11)
        )
        plan = replace(plan, intent_graph=replace(plan.intent_graph, segments=segments))
        binding = self._official_binding(FULL_PROFILE)
        source = render_profiled_prompt(plan, binding).document
        self.assertIn("[Shot 10] At 00:04.500", source.text)
        malformed = self._replace_document_text(
            source, "[Shot 10] At 00:04.500", "[Shot 10] At 00:04.600"
        )

        result = validate_profiled_prompt(plan, malformed, binding)

        self.assertFalse(result.is_valid)
        self.assertIn("semantic.timestamp", {item.code for item in result.diagnostics})

    def test_source_profiled_module_has_no_optional_runtime_imports(self) -> None:
        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "source_profiled_prompt.py").read_text(
                encoding="utf-8"
            )
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
