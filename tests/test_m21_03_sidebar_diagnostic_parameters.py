"""M21-03 sidebar projection carries M21-01's typed diagnostic parameters.

The browser composes a localised diagnostic sentence from a stable identity plus typed
parameters. `ValidationDiagnostic` carries neither, so `build_sidebar_workspace_projection`
recomputes the audit -- a pure function of the plan and document the report already holds --
and attaches the parameters to the diagnostics that have them. These rows pin that the
attachment is present, positional, additive, and unable to fail a projection.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from comfyui_h3_context.core import (
    ContextReport,
    ExecutionCorrelation,
    NativeH3Wiring,
    PromptLintError,
    SidebarWorkspaceError,
    SidebarWorkspaceProjection,
    TaskMode,
    ValidationStatus,
    build_native_h3_wiring,
    build_sidebar_workspace_projection,
    canonical_fingerprint,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "governance"
        / "contracts"
        / "sidebar_workspace_v2.schema.json"
    ).read_text(encoding="utf-8")
)

_CORRELATION = ExecutionCorrelation("prompt.m2103", "node.m2103")
_WORKSPACE_ID = "ws_m2103_sidebar_diagnostic_parameters_projection"


def _report(inserted: str = "") -> ContextReport:
    """The real rendered report, optionally with one extra sentence in the description.

    The section body and the document text are edited together. Editing only the text would
    leave the document's own section absent from it and the structural linter would fail the
    report for that instead, which is not what these rows measure.
    """

    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "Preserve the exact declared intent.",
        duration_seconds=5.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request, None)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    if inserted:
        section = document.sections[0]
        assert section.heading == "integrated_multimodal_description"
        body = f"{section.body} {inserted}"
        document = replace(
            document,
            text=document.text.replace(section.body, body, 1),
            sections=(replace(section, body=body),) + document.sections[1:],
        )
    return H3ContextValidatorNode().validate(plan, document)[1]


def _project(report: ContextReport) -> SidebarWorkspaceProjection:
    """Project the report the way its own validation status allows.

    A passed report must carry runtime wiring and a failed one must not, so the projection of a
    report whose prose tripped an `error` rule is only reachable without wiring. Both shapes are
    exercised here on purpose: the parameter attachment is a property of the projection, not of
    the queue-ready path.
    """

    if report.validation.status is ValidationStatus.PASSED:
        wiring: NativeH3Wiring = build_native_h3_wiring(report)
        return build_sidebar_workspace_projection(
            report,
            wiring,
            _CORRELATION,
            workspace_id=_WORKSPACE_ID,
            base_prompt_fingerprint=wiring.prompt_fingerprint,
        )
    return build_sidebar_workspace_projection(
        report,
        None,
        _CORRELATION,
        workspace_id=_WORKSPACE_ID,
        base_prompt_fingerprint=canonical_fingerprint(report.prompt_document.text),
    )


def _diagnostics(report: ContextReport) -> tuple[dict[str, object], ...]:
    return _project(report).diagnostics


class SidebarDiagnosticParameterTests(unittest.TestCase):
    def test_a_default_report_preserves_parameterless_and_per_shot_shapes(self) -> None:
        diagnostics = _diagnostics(_report())

        # The attachment is additive: a diagnostic without parameters keeps the exact three
        # keys every accepted consumer already reads.
        parameterless = [
            item for item in diagnostics if item["code"] == "fidelity.soundscape.unspecified"
        ]
        self.assertEqual(len(parameterless), 1)
        self.assertEqual(set(parameterless[0]), {"code", "severity", "message"})

        per_shot = [
            item for item in diagnostics if item["code"] == "fidelity.description.semantic_empty"
        ]
        self.assertEqual(len(per_shot), 1)
        self.assertEqual(
            set(per_shot[0]),
            {"code", "severity", "message", "parameters"},
        )
        self.assertEqual(per_shot[0]["parameters"], {"segment_id": "segment_1"})

    def test_a_fidelity_diagnostic_carries_its_typed_parameters(self) -> None:
        found = [
            item
            for item in _diagnostics(_report("Then the camera pans right across the room."))
            if item["code"] == "fidelity.camera.unrequested_motion"
        ]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["parameters"], {"term": "pan right"})
        # `message` is untouched, because it remains the English fallback for a build whose
        # catalog has never heard of this identity.
        self.assertIsInstance(found[0]["message"], str)
        self.assertTrue(str(found[0]["message"]).strip())

    def test_repeated_identities_take_their_own_parameters_in_order(self) -> None:
        # The internal-vocabulary rule reports every term it finds, so one identity really can
        # appear more than once. A single map keyed by identity would give both findings the
        # last one's values, which is the defect this row exists to catch.
        report = _report("The panel index and the grid position stay fixed.")
        found = [
            item
            for item in _diagnostics(report)
            if item["code"] == "fidelity.internal_vocabulary.present"
        ]
        self.assertEqual(len(found), 2)
        self.assertEqual(
            [item["parameters"] for item in found],
            [{"term": "panel index"}, {"term": "grid position"}],
        )

    def test_the_informational_length_band_never_reaches_the_projection(self) -> None:
        # It is excluded from the validation stream by M21-01, so no projection diagnostic can
        # carry it and the attachment must not invent one.
        codes = {item["code"] for item in _diagnostics(_report())}
        self.assertNotIn("fidelity.description.length_band", codes)

    def test_the_wire_validates_with_and_without_parameters(self) -> None:
        validator = Draft202012Validator(_SCHEMA)
        for inserted in ("", "Then the camera pans right across the room."):
            validator.validate(_project(_report(inserted)).to_wire())

    def test_an_unauditable_report_fails_closed_before_app_mode_projection(self) -> None:
        # M24-05 made the same audit authoritative for explicit guide readiness. If it refuses,
        # App Mode must not silently drop that field and display a structurally ready workspace.
        report = _report("Then the camera pans right across the room.")
        with (
            mock.patch(
                "comfyui_h3_context.core.sidebar_workspace.audit_prompt_fidelity",
                side_effect=PromptLintError("refused"),
            ),
            self.assertRaises(SidebarWorkspaceError) as raised,
        ):
            _project(report)
        self.assertEqual(raised.exception.code, "projection_failed")


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
