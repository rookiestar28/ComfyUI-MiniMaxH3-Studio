"""Fixture bootstrap diagnostics expose closed facts, never supplied exception content."""

import pytest

from comfyui_h3_context.adapters.authoring_fonts import _ERROR_CODES as FONT_ERROR_CODES
from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringWorkbenchError
from scripts.m25_16_import_fixture import unexpected_reply


def test_typed_bootstrap_code_is_preserved() -> None:
    reply = unexpected_reply(
        "bootstrap", AuthoringWorkbenchError(409, "initialization_currentness_conflict")
    )
    assert reply == {
        "status": 500,
        "body": None,
        "error": "AuthoringWorkbenchError",
        "operation": "bootstrap",
        "code": "initialization_currentness_conflict",
    }


def test_arbitrary_operation_message_and_error_code_are_not_echoed() -> None:
    content = "private_content"
    for error in (RuntimeError(content), AuthoringWorkbenchError(500, content)):
        reply = unexpected_reply(content, error)
        assert content not in str(reply)
        assert reply["operation"] == "decode"
        assert reply["code"] == "unexpected_error"


@pytest.mark.parametrize("code", sorted(FONT_ERROR_CODES))
def test_every_font_authority_refusal_keeps_its_closed_bootstrap_code(code: str) -> None:
    reply = unexpected_reply("bootstrap", AuthoringWorkbenchError(422, code))
    assert reply["code"] == code
    assert reply["operation"] == "bootstrap"
    assert reply["body"] is None
