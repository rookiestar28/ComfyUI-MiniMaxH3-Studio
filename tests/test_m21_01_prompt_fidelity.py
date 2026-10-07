"""M21-01 official prompt fidelity audit rule tests.

Every row here is about prose the linter cannot see. The structural linter already checks label
syntax, residue, security and duration; these rules check whether the rendered sentences obey the
official guide the repository renders to, and whether they kept the user's own restrictions.

Two properties are asserted throughout rather than in one place. No rule mutates anything, and every
diagnostic is reconstructible for display from its stable identity plus typed parameters alone --
without reading `message`, which is the English fallback and not the contract.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from jsonschema import Draft202012Validator
from security_corpus import PROSE_AUDIT_ADVERSARIAL

from comfyui_h3_context.core import (
    PROMPT_FIDELITY_DIAGNOSTIC_IDS,
    PROMPT_FIDELITY_SCHEMA,
    AssetRole,
    ContextPlan,
    ContextReport,
    DescriptionLengthBand,
    EvidenceLevel,
    MediaKind,
    PromptDocument,
    PromptFidelityDiagnostic,
    PromptFidelityDiagnosticId,
    PromptRenderStatus,
    ReferenceAsset,
    TaskMode,
    ValidationSeverity,
    ValidationStatus,
    audit_prompt_fidelity,
    build_reference_registry,
)
from comfyui_h3_context.core.errors import ContextReportError, PromptLintError
from comfyui_h3_context.core.prompt_fidelity import _MAX_TEXT as MAX_AUDITED_TEXT
from comfyui_h3_context.core.prompt_fidelity import (
    ABOVE_TARGET_CHARACTERS,
    BELOW_TARGET_CHARACTERS,
    SEVERELY_SHORT_CHARACTERS,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "governance" / "contracts" / "prompt_fidelity_v2.schema.json"

PLAIN_INTENT = "A red kite drifts above a quiet field."
MOTION_INTENT = "A red kite drifts above a quiet field while the camera pans right."


def _plan(intent: str = PLAIN_INTENT, *, duration_seconds: float = 8.0) -> ContextPlan:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA, intent, duration_seconds=duration_seconds
    )[0]
    return H3ContextPlanNode().build_plan(request)[0]


def _document(plan: ContextPlan, text: str | None = None) -> PromptDocument:
    _, _, document = H3ContextCompilerNode().compile(plan)
    assert document.status is PromptRenderStatus.RENDERED
    return document if text is None else replace(document, text=text)


def _ids(plan: ContextPlan, document: PromptDocument) -> list[str]:
    return [item.diagnostic_id.value for item in audit_prompt_fidelity(plan, document).diagnostics]


def _only(
    plan: ContextPlan, document: PromptDocument, identity: PromptFidelityDiagnosticId
) -> PromptFidelityDiagnostic:
    matches = [
        item
        for item in audit_prompt_fidelity(plan, document).diagnostics
        if item.diagnostic_id is identity
    ]
    assert len(matches) == 1, f"expected exactly one {identity.value}, got {len(matches)}"
    return matches[0]


def _body(text: str) -> str:
    """A document body in the official three-field shape, so the rules see a real subject."""

    return (
        f"integrated_multimodal_description: {text}\n\n"
        "overall_soundscape: N/A\n\n"
        "non_diegetic_music: N/A"
    )


class ShotTimestampRuleTests(unittest.TestCase):
    """Guide 4.2: no timestamp on the first shot, then strictly increasing in-duration cut times."""

    def test_the_renderers_own_output_raises_no_timestamp_finding(self) -> None:
        plan = _plan()
        # The renderer already emits MM:SS.mmm. The rule guards against regression and against a
        # document that did not come from it, so on a correct render it must stay silent.
        self.assertNotIn(
            PromptFidelityDiagnosticId.SHOT_TIMESTAMP_MALFORMED.value, _ids(plan, _document(plan))
        )

    def test_a_cut_time_outside_the_official_format_is_an_error(self) -> None:
        plan = _plan()
        document = _document(plan, _body("[Shot 1] Opening. [Shot 2] At 3.5s, the field widens."))
        finding = _only(plan, document, PromptFidelityDiagnosticId.SHOT_TIMESTAMP_MALFORMED)
        self.assertIs(finding.severity, ValidationSeverity.ERROR)
        self.assertIs(finding.evidence_level, EvidenceLevel.OFFICIAL)
        # Reconstructible without reading `message`.
        self.assertEqual(
            finding.parameter_map,
            {"shot_index": 2, "cut_time": "3.5s", "expected_format": "MM:SS.mmm"},
        )

    def test_a_cut_time_that_does_not_increase_is_an_error(self) -> None:
        plan = _plan()
        document = _document(
            plan,
            _body(
                "[Shot 1] Opening. [Shot 2] At 00:03.500, the field widens. "
                "[Shot 3] At 00:02.000, the kite dips."
            ),
        )
        finding = _only(plan, document, PromptFidelityDiagnosticId.SHOT_TIMESTAMP_NOT_INCREASING)
        self.assertEqual(finding.parameter_map["shot_index"], 3)
        self.assertEqual(finding.parameter_map["cut_time"], "00:02.000")

    def test_a_cut_time_past_the_declared_duration_is_an_error(self) -> None:
        plan = _plan(duration_seconds=8.0)
        document = _document(plan, _body("[Shot 1] Opening. [Shot 2] At 00:12.000, the kite dips."))
        finding = _only(plan, document, PromptFidelityDiagnosticId.SHOT_TIMESTAMP_OUT_OF_DURATION)
        self.assertEqual(finding.parameter_map["duration_seconds"], "8.0")

    def test_a_timestamp_on_the_first_shot_is_an_error(self) -> None:
        plan = _plan()
        document = _document(plan, _body("[Shot 1] At 00:00.000, the field opens."))
        finding = _only(plan, document, PromptFidelityDiagnosticId.SHOT_TIMESTAMP_ON_FIRST_SHOT)
        self.assertEqual(finding.parameter_map, {"shot_index": 1, "cut_time": "00:00.000"})

    def test_a_correct_multi_shot_sequence_produces_no_timestamp_finding(self) -> None:
        plan = _plan()
        document = _document(
            plan,
            _body(
                "[Shot 1] Opening. [Shot 2] At 00:02.000, the field widens. "
                "[Shot 3] At 00:05.250, the kite dips."
            ),
        )
        self.assertEqual(
            [item for item in _ids(plan, document) if item.startswith("fidelity.shot_timestamp")],
            [],
        )


class CameraAndCutRuleTests(unittest.TestCase):
    """Guide 4.2 and 4.3: neither a move nor a cut may be invented on the user's behalf."""

    def test_an_uninvited_camera_move_is_reported(self) -> None:
        plan = _plan(PLAIN_INTENT)
        document = _document(plan, _body("[Shot 1] The camera pans right across the field."))
        finding = _only(plan, document, PromptFidelityDiagnosticId.CAMERA_UNREQUESTED_MOTION)
        # Lexical detection over prose stays a warning: a false error would reject a legitimate
        # prompt, which is worse than a missing check.
        self.assertIs(finding.severity, ValidationSeverity.WARNING)
        self.assertEqual(finding.parameter_map, {"term": "pan right"})

    def test_a_camera_move_the_user_asked_for_is_not_reported(self) -> None:
        plan = _plan(MOTION_INTENT)
        document = _document(plan, _body("[Shot 1] The camera pans right across the field."))
        self.assertNotIn(
            PromptFidelityDiagnosticId.CAMERA_UNREQUESTED_MOTION.value, _ids(plan, document)
        )

    def test_an_uninvited_cut_is_reported(self) -> None:
        plan = _plan(PLAIN_INTENT)
        document = _document(plan, _body("[Shot 1] Opening, then the camera cuts to the treeline."))
        finding = _only(plan, document, PromptFidelityDiagnosticId.CAMERA_UNREQUESTED_CUT)
        self.assertEqual(finding.parameter_map, {"term": "the camera cuts to"})

    def test_a_cut_the_user_asked_for_is_not_reported(self) -> None:
        plan = _plan("A red kite drifts above a field, then the shot cuts to the treeline.")
        document = _document(plan, _body("[Shot 1] Opening, then the camera cuts to the treeline."))
        self.assertNotIn(
            PromptFidelityDiagnosticId.CAMERA_UNREQUESTED_CUT.value, _ids(plan, document)
        )


class DialogueSpeakerRuleTests(unittest.TestCase):
    """Guide 4.4: a stable speaker ID establishes who is speaking, before the `<d>` block."""

    def test_a_dialogue_block_without_a_speaker_identity_is_reported(self) -> None:
        plan = _plan()
        document = _document(plan, _body("[Shot 1] She says: <d>[English] I get off here.</d>"))
        finding = _only(
            plan, document, PromptFidelityDiagnosticId.DIALOGUE_SPEAKER_IDENTITY_MISSING
        )
        # The official rule is mechanical, but this repository's own renderer does not yet emit
        # speaker IDs. An error here would fail every dialogue prompt the product itself produces,
        # so the rule reports until a renderer that can satisfy it ships.
        self.assertIs(finding.severity, ValidationSeverity.WARNING)
        self.assertEqual(finding.parameter_map, {"block_index": 1})

    def test_a_stable_speaker_identity_satisfies_the_rule(self) -> None:
        plan = _plan()
        document = _document(
            plan, _body("[Shot 1] The young woman (S1) says: <d>[English] I get off here.</d>")
        )
        self.assertNotIn(
            PromptFidelityDiagnosticId.DIALOGUE_SPEAKER_IDENTITY_MISSING.value,
            _ids(plan, document),
        )

    def test_a_compound_identity_satisfies_the_rule(self) -> None:
        plan = _plan()
        document = _document(
            plan, _body("[Shot 1] The two children (S1,S2) shout, <d>[English] Wait for us!</d>")
        )
        self.assertNotIn(
            PromptFidelityDiagnosticId.DIALOGUE_SPEAKER_IDENTITY_MISSING.value,
            _ids(plan, document),
        )

    def test_each_unidentified_block_is_reported_separately(self) -> None:
        plan = _plan()
        document = _document(
            plan,
            _body(
                "[Shot 1] She says: <d>[English] One.</d>\n"
                "[Shot 2] At 00:04.000, he replies: <d>[English] Two.</d>"
            ),
        )
        blocks = [
            item.parameter_map["block_index"]
            for item in audit_prompt_fidelity(plan, document).diagnostics
            if item.diagnostic_id is PromptFidelityDiagnosticId.DIALOGUE_SPEAKER_IDENTITY_MISSING
        ]
        self.assertEqual(blocks, [1, 2])


class DescriptionLengthRuleTests(unittest.TestCase):
    """A graded quality signal. It is never a pass/fail and never blocks a report."""

    def _band(self, characters: int) -> DescriptionLengthBand:
        plan = _plan()
        document = _document(plan, _body("[Shot 1] " + "a" * max(characters - 9, 0)))
        return audit_prompt_fidelity(plan, document).length_band

    def test_each_band_boundary_is_exact(self) -> None:
        self.assertIs(
            self._band(SEVERELY_SHORT_CHARACTERS - 1), DescriptionLengthBand.SEVERELY_SHORT
        )
        self.assertIs(self._band(SEVERELY_SHORT_CHARACTERS), DescriptionLengthBand.BELOW_TARGET)
        self.assertIs(self._band(BELOW_TARGET_CHARACTERS), DescriptionLengthBand.WITHIN_TARGET)
        self.assertIs(self._band(ABOVE_TARGET_CHARACTERS), DescriptionLengthBand.WITHIN_TARGET)
        self.assertIs(self._band(ABOVE_TARGET_CHARACTERS + 1), DescriptionLengthBand.ABOVE_TARGET)

    def test_the_band_is_informational_and_carries_community_evidence(self) -> None:
        plan = _plan()
        document = _document(plan, _body("[Shot 1] Short."))
        finding = _only(plan, document, PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND)
        self.assertIs(finding.severity, ValidationSeverity.INFO)
        # This synthetic single-token fixture uses the legacy character advisory; the official
        # 350-500 English-word guidance applies only to ordinary English generation prose.
        self.assertIs(finding.evidence_level, EvidenceLevel.COMMUNITY_RECOMMENDED)
        self.assertEqual(finding.parameter_map["band"], "severely_short")

    def test_english_generation_uses_official_word_advisory_without_blocking(self) -> None:
        plan = _plan()
        for count, expected in (
            (349, "below_target"),
            (350, None),
            (500, None),
            (501, "above_target"),
        ):
            with self.subTest(words=count):
                document = _document(plan, _body(" ".join(["breeze"] * count)))
                audit = audit_prompt_fidelity(plan, document)
                findings = [
                    item
                    for item in audit.diagnostics
                    if item.diagnostic_id is PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND
                ]
                if expected is None:
                    self.assertEqual(findings, [])
                else:
                    self.assertEqual(len(findings), 1)
                    self.assertEqual(findings[0].parameter_map["band"], expected)
                    self.assertIs(findings[0].evidence_level, EvidenceLevel.OFFICIAL)
                    self.assertIs(findings[0].severity, ValidationSeverity.INFO)
                    self.assertNotIn(findings[0], audit.blocking)

    def test_an_in_band_description_emits_no_length_finding(self) -> None:
        plan = _plan()
        document = _document(plan, _body("[Shot 1] " + "a" * 500))
        self.assertNotIn(
            PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND.value, _ids(plan, document)
        )

    def test_the_band_never_reaches_the_validation_stream(self) -> None:
        plan = _plan()
        document = _document(plan, _body("[Shot 1] Short."))
        result = audit_prompt_fidelity(plan, document)
        self.assertNotIn(
            PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND,
            {item.diagnostic_id for item in result.blocking},
        )


class InternalVocabularyRuleTests(unittest.TestCase):
    """M21-02 introduces a producer of this vocabulary; the check exists before it does."""

    def test_internal_representation_words_never_ship(self) -> None:
        plan = _plan()
        document = _document(
            plan, _body("[Shot 1] The subject from the contact sheet walks into the field.")
        )
        finding = _only(plan, document, PromptFidelityDiagnosticId.INTERNAL_VOCABULARY_PRESENT)
        self.assertIs(finding.severity, ValidationSeverity.ERROR)
        # This is the repository's own vocabulary, not an official rule.
        self.assertIs(finding.evidence_level, EvidenceLevel.FRAMEWORK_REFERENCE)
        self.assertEqual(finding.parameter_map, {"term": "contact sheet"})

    def test_a_prompt_free_of_that_vocabulary_is_silent(self) -> None:
        plan = _plan()
        document = _document(plan, _body("[Shot 1] The kite crosses the field."))
        self.assertNotIn(
            PromptFidelityDiagnosticId.INTERNAL_VOCABULARY_PRESENT.value, _ids(plan, document)
        )


class ExplicitConstraintRuleTests(unittest.TestCase):
    """The user's own restrictions, re-checked against what was actually rendered."""

    def _motion_plan(self, intent: str = PLAIN_INTENT) -> ContextPlan:
        registry = build_reference_registry(
            (ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.MOTION_REFERENCE, 1),)
        )
        request = H3ContextRequestNode().build_request(
            TaskMode.REF2VA, intent, duration_seconds=8.0
        )[0]
        request = replace(
            request, assets=registry.to_asset_descriptors(), reference_registry=registry
        )
        return H3ContextPlanNode().build_plan(request, registry)[0]

    def test_a_motion_only_reference_credited_with_an_excluded_trait_is_reported(self) -> None:
        plan = self._motion_plan()
        document = _document(
            plan,
            _body(
                "[Shot 1] The runner adopts the wardrobe and gait of <Video 1> across the field."
            ),
        )
        finding = _only(plan, document, PromptFidelityDiagnosticId.CONSTRAINT_EXCLUDED_SOURCE_TRAIT)
        # This is the exact failure mode the reference-role ownership model exists to prevent.
        self.assertIs(finding.severity, ValidationSeverity.WARNING)
        self.assertEqual(
            finding.parameter_map,
            {"label": "<Video 1>", "trait": "wardrobe", "role": "motion_reference"},
        )

    def test_a_motion_only_reference_credited_with_motion_alone_is_silent(self) -> None:
        plan = self._motion_plan()
        document = _document(
            plan, _body("[Shot 1] The runner adopts the gait and cadence of <Video 1>.")
        )
        self.assertNotIn(
            PromptFidelityDiagnosticId.CONSTRAINT_EXCLUDED_SOURCE_TRAIT.value,
            _ids(plan, document),
        )

    def test_a_technique_the_user_ruled_out_is_reported_when_it_appears(self) -> None:
        plan = _plan("A red kite drifts above a field, static camera, no cuts.")
        document = _document(plan, _body("[Shot 1] Opening, then the camera cuts to the treeline."))
        findings = [
            item.parameter_map
            for item in audit_prompt_fidelity(plan, document).diagnostics
            if item.diagnostic_id is PromptFidelityDiagnosticId.CONSTRAINT_NEGATED_TECHNIQUE_USED
        ]
        self.assertEqual(findings, [{"technique": "cut", "term": "the camera cuts to"}])

    def test_a_restriction_the_prompt_honoured_is_silent(self) -> None:
        plan = _plan("A red kite drifts above a field, static camera, no cuts.")
        document = _document(plan, _body("[Shot 1] The kite drifts steadily above the field."))
        self.assertNotIn(
            PromptFidelityDiagnosticId.CONSTRAINT_NEGATED_TECHNIQUE_USED.value,
            _ids(plan, document),
        )


class BoundaryAndContractTests(unittest.TestCase):
    """The properties every rule shares, asserted once against all of them."""

    def test_no_rule_resolves_an_unstructured_natural_language_reference(self) -> None:
        plan = _plan()
        # "the second video" is not the canonical mini-language. A deterministic audit that guessed
        # at unstructured intent would stop being deterministic, and a false error on a legitimate
        # prompt is worse than a missing check.
        document = _document(
            plan, _body("[Shot 1] The runner matches the motion of the second video shown earlier.")
        )
        self.assertEqual(
            [item for item in _ids(plan, document) if item.startswith("fidelity.constraint")],
            [],
        )

    def test_every_emitted_identity_is_a_member_of_the_exported_registry(self) -> None:
        plan = _plan()
        document = _document(
            plan,
            _body(
                "[Shot 1] At 00:00.000, from the contact sheet the camera pans right and cuts to "
                "a face that says <d>[English] Hello.</d> [Shot 2] At 3.5s, again. "
                "[Shot 3] At 00:01.000, once more."
            ),
        )
        emitted = {
            item.diagnostic_id.value for item in audit_prompt_fidelity(plan, document).diagnostics
        }
        self.assertTrue(emitted)
        self.assertTrue(emitted <= PROMPT_FIDELITY_DIAGNOSTIC_IDS)
        self.assertEqual(len(PROMPT_FIDELITY_DIAGNOSTIC_IDS), len(PromptFidelityDiagnosticId))

    def test_every_diagnostic_composes_a_sentence_without_reading_message(self) -> None:
        plan = _plan()
        document = _document(
            plan,
            _body(
                "[Shot 1] At 00:00.000, from the contact sheet the camera pans right and cuts to "
                "a face that says <d>[English] Hello.</d>"
            ),
        )
        # A consumer that never touches `message` must still be able to render every finding. This
        # is the property that lets M21-03 localise the family instead of shipping English prose.
        catalog = {
            identity: identity.value.replace(".", " ").replace("_", " ") + " {parameters}"
            for identity in PromptFidelityDiagnosticId
        }
        emitted = audit_prompt_fidelity(plan, document).diagnostics
        self.assertTrue(emitted)
        for item in emitted:
            sentence = catalog[item.diagnostic_id].format(parameters=item.parameter_map)
            self.assertIn(item.diagnostic_id.value.split(".")[-1].replace("_", " "), sentence)
            for value in item.parameter_map.values():
                self.assertIn(str(value), sentence)

    def test_no_diagnostic_or_parameter_carries_private_material(self) -> None:
        plan = _plan()
        document = _document(
            plan,
            _body(
                "[Shot 1] At 00:00.000, from the contact sheet the camera pans right and cuts to "
                "a face that says <d>[English] Hello.</d>"
            ),
        )
        forbidden = (
            "http://",
            "https://",
            "file://",
            "authorization",
            "bearer ",
            "api_key",
            "api-key",
            "password",
            "secret",
            "token=",
            "sig=",
            "x-amz-",
            "c:\\",
            "b:\\",
            "/home/",
            "/users/",
        )
        for item in audit_prompt_fidelity(plan, document).diagnostics:
            surfaces = [item.message, item.remediation, item.location]
            surfaces.extend(str(value) for value in item.parameter_map.values())
            for surface in surfaces:
                lowered = surface.casefold()
                for needle in forbidden:
                    self.assertNotIn(needle, lowered)

    def test_the_audit_mutates_nothing(self) -> None:
        plan = _plan()
        document = _document(plan)
        before = (plan.plan_id, plan.diagnostics, document.text, document.document_id)
        audit_prompt_fidelity(plan, document)
        self.assertEqual(
            (plan.plan_id, plan.diagnostics, document.text, document.document_id), before
        )

    def test_adversarial_prose_is_bounded_and_never_raises(self) -> None:
        # The rules scan whatever a document contains, so their patterns meet hostile shapes:
        # a cut marker repeated five hundred times, a near-miss timestamp with a five-hundred-digit
        # minute field, a thousand unclosed dialogue markers. Each must return a bounded result
        # rather than hang or throw.
        plan = _plan()
        for case in PROSE_AUDIT_ADVERSARIAL:
            with self.subTest(case=case.case_id):
                document = _document(plan, _body(case.payload))
                started = perf_counter()
                result = audit_prompt_fidelity(plan, document)
                self.assertLess(perf_counter() - started, 2.0)
                self.assertLessEqual(len(result.diagnostics), 256)

    def test_an_oversized_prompt_never_reaches_the_rules(self) -> None:
        plan = _plan()
        # The document contract is the first bound and refuses the oversized text outright, so the
        # rules never see it. The audit keeps its own bound behind that as defence in depth.
        with self.assertRaises(ContextReportError):
            _document(plan, "x" * 65_537)
        self.assertEqual(MAX_AUDITED_TEXT, 65_536)

    def test_the_audit_refuses_anything_that_is_not_a_typed_pair(self) -> None:
        plan = _plan()
        document = _document(plan)
        with self.assertRaises(PromptLintError):
            audit_prompt_fidelity(object(), document)  # type: ignore[arg-type]
        with self.assertRaises(PromptLintError):
            audit_prompt_fidelity(plan, object())  # type: ignore[arg-type]

    def test_the_wire_payload_matches_the_versioned_contract(self) -> None:
        plan = _plan()
        document = _document(
            plan,
            _body("[Shot 1] At 00:00.000, from the contact sheet, she says <d>[English] Hi.</d>"),
        )
        payload = audit_prompt_fidelity(plan, document).to_wire()
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
        self.assertEqual(payload["schema"], PROMPT_FIDELITY_SCHEMA)


class ValidationStreamTests(unittest.TestCase):
    """What the fidelity rules do, and do not do, to an existing report's verdict."""

    def _report(self, inserted: str = "") -> ContextReport:
        """Validate the real rendered document, optionally with one extra sentence in it.

        Replacing the whole body would trip the structural linter -- the document's own sections
        would no longer appear in its text -- and the row would then be measuring a lint failure
        rather than a fidelity rule.
        """

        plan = _plan()
        document = _document(plan)
        if inserted:
            section = document.sections[0]
            assert section.heading == "integrated_multimodal_description"
            body = f"{section.body} {inserted}"
            # The section and the text are edited together. Editing only the text would leave the
            # document's own section body absent from it, and the structural linter would fail the
            # report for that instead -- which is not what this row is measuring.
            document = replace(
                document,
                text=document.text.replace(section.body, body, 1),
                sections=(replace(section, body=body),) + document.sections[1:],
            )
        return H3ContextValidatorNode().validate(plan, document)[1]

    def test_a_correctly_rendered_report_keeps_its_passed_status(self) -> None:
        report = self._report()
        self.assertIs(report.validation.status, ValidationStatus.PASSED)
        codes = [
            item.code for item in report.validation.diagnostics if item.code.startswith("fidelity")
        ]
        # A correct render introduces no prose defect. M24-05 added guide-readiness rules over the
        # typed plan, and this fixture is the manual skeleton the plan node builds when no intent
        # graph is connected, so exactly those two readiness codes are expected and the report
        # still passes: readiness is reported, never enforced as a structural failure.
        self.assertEqual(
            codes,
            ["fidelity.soundscape.unspecified", "fidelity.description.semantic_empty"],
        )

    def test_a_mechanical_fidelity_error_fails_the_report(self) -> None:
        report = self._report("[Shot 2] At 3.5s, the field widens.")
        self.assertIs(report.validation.status, ValidationStatus.FAILED)
        self.assertIn(
            PromptFidelityDiagnosticId.SHOT_TIMESTAMP_MALFORMED.value,
            [item.code for item in report.validation.diagnostics],
        )

    def test_a_fidelity_warning_surfaces_without_failing_the_report(self) -> None:
        report = self._report("The camera pans right across the field.")
        # A warning is actionable, not blocking: lexical detection over prose must not stop a
        # prompt that a person would call correct.
        self.assertIs(report.validation.status, ValidationStatus.PASSED)
        self.assertIn(
            PromptFidelityDiagnosticId.CAMERA_UNREQUESTED_MOTION.value,
            [item.code for item in report.validation.diagnostics],
        )

    def test_an_informational_band_never_enters_the_validation_stream(self) -> None:
        report = self._report()
        self.assertNotIn(
            PromptFidelityDiagnosticId.DESCRIPTION_LENGTH_BAND.value,
            [item.code for item in report.validation.diagnostics],
        )


if __name__ == "__main__":  # pragma: no cover - direct execution convenience
    unittest.main()
