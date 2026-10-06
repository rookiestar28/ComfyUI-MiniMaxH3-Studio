"""Closed options and final-channel response facts shared by wire dialects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NoReturn

from ..prompt_model_provider import PromptModelContractError, PromptModelOutcomeId

MAX_FINAL_TEXT = 200_000
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class RequestOptions:
    structured_output: bool = True
    reasoning_control: bool = True
    ollama_think_off: bool = True

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (self.structured_output, self.reasoning_control, self.ollama_think_off)
        ):
            raise PromptModelContractError("request_options")

    @classmethod
    def safe(cls) -> RequestOptions:
        return cls(False, False, False)


@dataclass(frozen=True, slots=True)
class DialectAnswer:
    text: str
    observed_model_id: str
    finish_reason: str


class DialectResponseError(PromptModelContractError):
    """Only a closed outcome/code can escape a refused provider channel."""

    def __init__(self, outcome_id: PromptModelOutcomeId, code: str) -> None:
        self.outcome_id = outcome_id
        super().__init__(code)


def fail(code: str) -> NoReturn:
    raise PromptModelContractError(code)


def draft_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "schema": {"type": "string", "enum": ["h3.prompt_model.draft_json.v1"]},
            "prompt_text": {"type": "string"},
        },
        "required": ["schema", "prompt_text"],
        "additionalProperties": False,
    }


def response_model(observed: object, requested: object, *, snapshot: bool = False) -> str:
    if not isinstance(observed, str) or _MODEL_ID.fullmatch(observed) is None:
        fail("response_model")
    if requested is not None and observed != requested:
        # IMPORTANT: only a suffix of the requested alias is admitted. Prefix/substring matching
        # would attribute a different model's answer to the selected connection.
        if (
            not snapshot
            or not isinstance(requested, str)
            or not observed.startswith(requested + "-")
        ):
            fail("response_model")
    return observed


def final_text(value: object, reason: object, *, truncated_reason: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_FINAL_TEXT:
        fail("response_text")
    # SECURITY: invalid Unicode must be refused before budgeting/serialization. JSON may decode
    # lone surrogates, and a later UTF-8 encode would otherwise escape the typed failure boundary.
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        fail("response_text")
    if reason == truncated_reason:
        raise DialectResponseError(
            PromptModelOutcomeId.DRAFT_TRUNCATED
            if value.strip()
            else PromptModelOutcomeId.TRUNCATED_REASONING,
            "response_truncated",
        )
    if not value.strip():
        raise DialectResponseError(PromptModelOutcomeId.TRUNCATED_REASONING, "response_empty")
    return value
