"""Language metadata cannot invent a language or alter exact spoken text."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_full_rendering import full_plan
from test_rendering import make_plan

from comfyui_h3_context.core import (
    ExactTextConstraint,
    ExactTextKind,
    HardConstraintSet,
    TaskMode,
    audit_prompt_fidelity,
    render_base_prompt,
    render_full_reference_prompt,
)
from comfyui_h3_context.core.dialogue_language import (
    OFFICIAL_STABLE_DIALOGUE_LANGUAGES,
    RESERVED_NON_LANGUAGE_TAGS,
    derive_dialogue_language,
    normalize_dialogue_language,
)
from comfyui_h3_context.core.errors import ContractValidationError, PromptRenderingError
from comfyui_h3_context.core.prompt_fidelity import PLAN_READINESS_IDS
from comfyui_h3_context.core.rendering import _assert_constraints_preserved
from comfyui_h3_context.nodes import H3HardConstraintProducerNode


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("english", "English"),
        ("EN", "English"),
        ("zh", "Chinese"),
        ("fr", "French"),
        ("ar", "Arabic"),
        ("de", "German"),
        ("it", "Italian"),
        ("ja", "Japanese"),
        ("ko", "Korean"),
        ("pt", "Portuguese"),
        ("ru", "Russian"),
        ("es", "Spanish"),
        ("Dutch", "Dutch"),
        ("Traditional Chinese", "Traditional Chinese"),
        ("zh-Hant", "zh-Hant"),
    ],
)
def test_language_normalization(value: str, expected: str) -> None:
    assert normalize_dialogue_language(value) == expected


@pytest.mark.parametrize(
    "value", ["", " ", "--", "[English]", "<English>", "English\n", "en_US", "a" * 65]
)
def test_invalid_language_metadata_is_refused(value: str) -> None:
    with pytest.raises(ContractValidationError):
        normalize_dialogue_language(value)


@pytest.mark.parametrize("value", sorted(RESERVED_NON_LANGUAGE_TAGS))
def test_reserved_language_names_are_refused(value: str) -> None:
    with pytest.raises(ContractValidationError, match="language"):
        ExactTextConstraint("line", ExactTextKind.DIALOGUE, "Keep this.", value.upper())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("안녕하세요 漢字", "Korean"),
        ("こんにちは", "Japanese"),
        ("漢字とかな", "Japanese"),
        ("你好世界", "Chinese"),
        ("Hello.", None),
        ("Привет", None),
        ("مرحبا", None),
        ("한국어かな", None),
    ],
)
def test_only_unambiguous_scripts_supply_a_language(text: str, expected: str | None) -> None:
    assert derive_dialogue_language(text) == expected


@pytest.mark.parametrize("kind", [ExactTextKind.DIALOGUE, ExactTextKind.LYRICS])
@pytest.mark.parametrize(
    ("language", "spoken", "block"),
    [
        ("en", "  Keep this!\n", "<d>[English]   Keep this!\n</d>"),
        (None, "你好", "<d>[Chinese] 你好</d>"),
        (None, "Hello.", "<d>Hello.</d>"),
    ],
)
def test_base_and_full_preserve_the_declared_derived_or_absent_tag(
    kind: ExactTextKind, language: str | None, spoken: str, block: str
) -> None:
    constraints = HardConstraintSet((ExactTextConstraint("line", kind, spoken, language),))
    base = make_plan(TaskMode.T2VA, constraints=constraints)
    full = full_plan()
    full = replace(
        full,
        hard_constraints=constraints,
        request=replace(full.request, hard_constraints=constraints),
    )
    assert block in render_base_prompt(base).text
    assert block in render_full_reference_prompt(full).text


@given(
    kind=st.sampled_from([ExactTextKind.DIALOGUE, ExactTextKind.LYRICS]),
    text=st.text(
        alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="<>[]"),
        min_size=1,
        max_size=80,
    ),
    language=st.one_of(st.none(), st.sampled_from(OFFICIAL_STABLE_DIALOGUE_LANGUAGES)),
)
def test_every_generated_language_tag_is_usable(
    kind: ExactTextKind, text: str, language: str | None
) -> None:
    plan = make_plan(
        TaskMode.T2VA,
        constraints=HardConstraintSet((ExactTextConstraint("line", kind, text, language),)),
    )
    rendered = render_base_prompt(plan).text
    full = full_plan()
    full = replace(
        full,
        hard_constraints=plan.hard_constraints,
        request=replace(full.request, hard_constraints=plan.hard_constraints),
    )
    for prompt in (rendered, render_full_reference_prompt(full).text):
        for reserved in RESERVED_NON_LANGUAGE_TAGS:
            assert f"<d>[{reserved}]" not in prompt.casefold()
        assert text in prompt


def test_preservation_guard_rejects_a_placeholder_replacing_an_untagged_line() -> None:
    plan = make_plan(
        TaskMode.T2VA,
        constraints=HardConstraintSet(
            (ExactTextConstraint("line", ExactTextKind.DIALOGUE, "Hello."),)
        ),
    )
    with pytest.raises(PromptRenderingError):
        _assert_constraints_preserved(plan, "<d>[unspecified] Hello.</d>")


def test_visible_text_language_remains_author_metadata() -> None:
    value = ExactTextConstraint("title", ExactTextKind.VISIBLE_TEXT, "Exact title", "unknown")
    assert value.language == "unknown"


def test_optional_canvas_language_keeps_old_widget_positions_and_reaches_the_constraint() -> None:
    shape = H3HardConstraintProducerNode.INPUT_TYPES()
    assert list(shape["required"]) == [
        "dialogue",
        "visible_text",
        "required_content",
        "forbidden_content",
        "timing_start",
        "timing_end",
        "keep_target",
        "keep_value",
        "change_replacement",
    ]
    assert list(shape["optional"])[0] == "dialogue_language"
    assert shape["optional"]["dialogue_language"][1]["default"] == "auto"
    # This serialized workflow predates the optional language widget.
    workflow = json.loads(
        '{"nodes":[{"widgets_values":["Hello.","Sign","","","","","subject","",""]}]}'
    )
    old_widgets = workflow["nodes"][0]["widgets_values"]
    old_values = dict(zip(shape["required"], old_widgets, strict=True))
    legacy, _ = H3HardConstraintProducerNode().produce(**old_values)
    declared, _ = H3HardConstraintProducerNode().produce(
        dialogue="Hello.", dialogue_language="English"
    )
    assert legacy.exact_texts[0].text == "Hello."
    assert legacy.exact_texts[1].text == "Sign"
    assert legacy.exact_texts[0].language is None
    assert declared.exact_texts[0].language == "English"


@pytest.mark.parametrize(
    ("body", "code", "readiness"),
    [
        ("(S1) says: <d>Hello.</d>", "language_missing", True),
        ("(S1) says: <d>[unspecified] Hello.</d>", "language_tag_invalid", False),
        ("(S1) says: <d>[Dutch] Hallo.</d>", "language_unstable", False),
    ],
)
def test_prose_language_findings_warn_and_have_the_required_classification(
    body: str, code: str, readiness: bool
) -> None:
    plan = make_plan(TaskMode.T2VA)
    document = replace(render_base_prompt(plan), text=body)
    audit = audit_prompt_fidelity(plan, document)
    finding = next(
        item
        for item in audit.diagnostics
        if item.diagnostic_id.value == f"fidelity.dialogue.{code}"
    )
    assert finding.severity.value == "warning"
    assert (finding.diagnostic_id in PLAN_READINESS_IDS) is readiness
    assert (finding in audit.prose_blocking) is not readiness


def test_script_derivation_is_advisory_and_unknown_language_withholds_readiness() -> None:
    for spoken, expected in [("你好", "language_derived"), ("Hello.", "language_missing")]:
        plan = make_plan(
            TaskMode.T2VA,
            constraints=HardConstraintSet(
                (ExactTextConstraint("line", ExactTextKind.DIALOGUE, spoken),)
            ),
        )
        audit = audit_prompt_fidelity(plan, render_base_prompt(plan))
        findings = [
            item
            for item in audit.diagnostics
            if item.diagnostic_id.value == f"fidelity.dialogue.{expected}"
        ]
        assert len(findings) == 1
        if expected == "language_derived":
            assert findings[0].severity.value == "info"
            assert findings[0].parameter_map["language"] == "Chinese"
            assert findings[0] not in audit.blocking
        else:
            assert findings[0] in audit.readiness_findings
