"""Content-free report admission rejects private tokens at every guarded field."""

from __future__ import annotations

import pytest

from comfyui_h3_context.core.semantic_conformance import (
    MISMATCH_CLASSES,
    SemanticConformanceError,
)
from comfyui_h3_context.core.semantic_conformance_compare import (
    CaseOutcome,
    Measurement,
    Mismatch,
)
from scripts.nle_semantic_qualify import QualifyRow

FIELDS = (
    "declared",
    "observed",
    "reason",
    "measurement_subject",
    "measurement_value",
    "mismatch_subject",
    "mismatch_expected",
    "mismatch_observed",
    "mismatch_bound",
    "measured_subject",
    "measured_value",
    "measured_bound",
    "outcome_reason",
)


def _admit(field: str, value: str) -> object:
    if field.startswith("mismatch_"):
        values = {"subject": "sample", "expected": "1/24", "observed": "2/24", "bound": "1/24"}
        values[field.removeprefix("mismatch_")] = value
        return Mismatch(MISMATCH_CLASSES[0], **values)
    if field.startswith("measured_"):
        values = {"subject": "sample", "value": "1/24", "bound": "1/24"}
        values[field.removeprefix("measured_")] = value
        return Measurement(**values)
    if field == "outcome_reason":
        return CaseOutcome("synthetic.case", "BLOCKED", reason=value)
    row = QualifyRow("synthetic.case", "command", "command_effect", "accepted", "accepted", "PASS")
    if field.startswith("measurement_"):
        pair = (value, "1/24") if field == "measurement_subject" else ("sample", value)
        return QualifyRow(
            row.case_id,
            row.case_class,
            row.observation,
            row.declared,
            row.observed,
            row.status,
            measurements=(pair,),
        )
    if field == "declared":
        return QualifyRow(
            row.case_id, row.case_class, row.observation, value, row.observed, row.status
        )
    if field == "observed":
        return QualifyRow(
            row.case_id, row.case_class, row.observation, row.declared, value, row.status
        )
    return QualifyRow(
        row.case_id,
        row.case_class,
        row.observation,
        row.declared,
        row.observed,
        row.status,
        reason=value,
    )


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize(
    "token",
    (r"C:\synthetic\clip", "/synthetic/clip", r"\\synthetic\clip", "https://example.invalid/clip"),
)
@pytest.mark.parametrize("prefix", ("", "accepted at ", "accepted=(", "accepted='"))
def test_private_token_is_rejected_at_start_and_embedded(
    field: str, token: str, prefix: str
) -> None:
    with pytest.raises(SemanticConformanceError, match="private path or URL"):
        _admit(field, prefix + token)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", ("accepted:changed", "invariant_ok", "1/24", "fixture/synthetic"))
def test_content_free_vocabulary_remains_admitted(field: str, value: str) -> None:
    assert _admit(field, value) is not None
