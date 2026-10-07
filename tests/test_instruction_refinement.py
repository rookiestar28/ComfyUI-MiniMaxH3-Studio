"""Instruction data, Unicode bounds and immutable prose-review guarantees."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from test_audit_override import make_report

from comfyui_h3_context.adapters.assisted_draft_execution import (
    build_assisted_guarantee,
    build_assisted_instruction,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.core import ExactTextConstraint, ExactTextKind, HardConstraintSet
from comfyui_h3_context.core.assisted_refinement import validate_revision_instruction
from comfyui_h3_context.core.errors import SidebarWorkspaceError


@pytest.mark.parametrize(
    "text", ["", " \n\t", "\x85", "\x1c", "x" * 2049, "\ud800", "\udfff", "a\x00b", 1]
)
def test_invalid_instruction_is_closed_without_content(text: object) -> None:
    with pytest.raises(ValueError, match="^revision instruction is invalid$"):
        validate_revision_instruction(text)


@pytest.mark.parametrize("text", [" 😀 中文 \n", "😀" * 2048, "x" * 2048, "\ufeff"])
def test_valid_instruction_is_never_normalized_or_truncated(text: str) -> None:
    assert validate_revision_instruction(text) == text


def test_instruction_changes_only_v2_data_and_leaves_legacy_source_and_guarantee_intact() -> None:
    report = make_report(
        hard=HardConstraintSet(
            constraints=(
                ExactTextConstraint("dialogue_1", ExactTextKind.DIALOGUE, "Keep the light on."),
                ExactTextConstraint("visible_1", ExactTextKind.VISIBLE_TEXT, "OPEN"),
            )
        )
    )
    guarantee = build_assisted_guarantee(report)
    original = json.loads(build_assisted_instruction(report, guarantee).split("\n", 1)[1])
    for direction in (
        "Describe the lighting.",
        "Change the dialogue, speaker, timing and references.",
    ):
        actual = json.loads(
            build_assisted_instruction(report, guarantee, revision_instruction=direction).split(
                "\n", 1
            )[1]
        )
        assert actual.pop("revision_instruction") == direction
        assert actual.pop("schema") == "h3.context.assisted_draft.request.v2"
        assert {**actual, "schema": original["schema"]} == original
    assert original["schema"] == "h3.context.assisted_draft.request.v1"
    changed = report.prompt_document.text.replace("Keep the light on.", "Change the light.")
    with pytest.raises(SidebarWorkspaceError, match="exact user text changed"):
        SidebarWorkspaceRegistry._validate_assisted_candidate(report, changed, guarantee)

    # Keep the exact-span refusal decisive even when the prose audit is green.
    audit, staged = SidebarWorkspaceRegistry._validate_assisted_candidate(
        report, report.prompt_document.text, guarantee
    )
    with (
        patch(
            "comfyui_h3_context.adapters.comfyui_sidebar_workspace.audit_prompt_fidelity",
            return_value=audit,
        ),
        patch(
            "comfyui_h3_context.adapters.comfyui_sidebar_workspace.stage_audit_override",
            return_value=staged,
        ),
    ):
        with pytest.raises(SidebarWorkspaceError, match="exact user text changed"):
            SidebarWorkspaceRegistry._validate_assisted_candidate(report, changed, guarantee)
        # Removing the immutable spans (the guard's input mutation) makes that same refusal fail.
        SidebarWorkspaceRegistry._validate_assisted_candidate(
            report,
            changed,
            replace(guarantee, user_dialogue=(), visible_text=(), hard_constraints=()),
        )


@pytest.mark.parametrize("repair", [False, True])
def test_maximum_unicode_instruction_and_repair_obey_exact_wire_byte_cap(repair: bool) -> None:
    from test_chosen_model_budget import Exchange, request, state

    from comfyui_h3_context.adapters.prompt_model_transport import run_prompt_model_session
    from comfyui_h3_context.core.prompt_model_provider import ModelMetadata, PromptModelOutcomeId
    from comfyui_h3_context.core.prompt_model_session import PromptModelMessage, PromptModelRole

    report = make_report()
    direction = "😀" * 2048
    data = build_assisted_instruction(
        report, build_assisted_guarantee(report), revision_instruction=direction
    )
    if repair:
        data += "\nprevious_candidate:" + json.dumps("Synthetic previous candidate 中文 😀" * 60)
        data += "\nrepair_requirements:\nPreserve exact source facts."
    base = request(ModelMetadata())
    measured = replace(
        base,
        messages=(base.messages[0], PromptModelMessage(PromptModelRole.USER, data)),
    )
    exchange = Exchange()
    assert run_prompt_model_session(measured, exchange, action_state=state()).answer is not None
    actual = exchange.payloads[0]
    messages = actual["messages"]
    assert isinstance(messages, list)
    assert isinstance(messages[1], dict)
    assert messages[1]["content"] == data
    assert data.count(direction) == 1
    wire_bytes = len(json.dumps(actual, ensure_ascii=True, separators=(",", ":")).encode())
    assert wire_bytes > len(data.encode("utf-8"))
    for ceiling, sent in ((wire_bytes, True), (wire_bytes - 1, False)):
        bounded = replace(
            measured,
            profile=replace(
                measured.profile,
                capabilities=replace(measured.profile.capabilities, max_request_bytes=ceiling),
            ),
        )
        exact = Exchange()
        result = run_prompt_model_session(bounded, exact, action_state=state())
        assert bool(exact.payloads) is sent
        assert (result.answer is not None) is sent
        if not sent:
            assert result.outcome.outcome_id is PromptModelOutcomeId.REQUEST_TOO_LARGE
