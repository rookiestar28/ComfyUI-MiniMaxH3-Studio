"""Tooling schema matches actual output values without becoming runtime authority."""

from __future__ import annotations

import copy
import importlib
import importlib.util
from typing import Any

from jsonschema import Draft202012Validator
from test_m25_authoring_output_service import completed as completed_output
from test_m25_authoring_output_service import image_bound as image_bound
from test_m25_authoring_output_service import subject as subject

from comfyui_h3_context.adapters.comfyui_authoring_output_runtime import AuthoringOutputRuntime
from comfyui_h3_context.core.authoring_output_protocol import OutputProtocolError


def validator() -> Draft202012Validator:
    name = "scripts.authoring_output_schemas"
    assert importlib.util.find_spec(name) is not None, "closed output schema is missing"
    schema = importlib.import_module(name).output_wire_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_schema_accepts_actual_create_status_capability_and_error(subject: Any) -> None:
    registry, request, *_ = subject
    check = validator()
    created = registry.create(request)
    check.validate(request.to_wire())
    check.validate(created)
    completed = completed_output(registry, request)
    check.validate(completed)
    assert completed["phase"] == "succeeded"
    owner = AuthoringOutputRuntime(None)
    try:
        check.validate(owner.capability())
        check.validate({**owner.capability(), "supported": True})
    finally:
        owner.close()
    check.validate(OutputProtocolError("expired").to_wire())
    check.validate(
        {"schema": "h3.authoring.output_cancel.v1", "workspace_handle": request.workspace_handle}
    )


def test_schema_refuses_private_fields_handles_and_impossible_status(subject: Any) -> None:
    registry, request, *_ = subject
    check = validator()
    completed = completed_output(registry, request)
    for changed in (
        {"private_path": "not-a-locator"},
        {"phase": "queued"},
        {"failure": "cancelled"},
        {"progress_bp": 9999},
        {"output_handle": None},
        {"job_handle": "pw_foreign"},
        {"workspace_handle": request.workspace_handle + "\n"},
        {"state_version": True},
    ):
        assert not check.is_valid({**completed, **changed}), changed
    for field, value in (
        ("verified", False),
        ("audio_streams", 2),
        ("width", 63),
        ("byte_length", 536870913),
    ):
        altered_output = copy.deepcopy(completed)
        altered_output["output"][field] = value
        assert not check.is_valid(altered_output), field
    for availability in ("gone", "expired"):
        check.validate({**completed, "availability": availability})
    check.validate({**completed, "availability": "gone", "output_handle": None, "output": None})
