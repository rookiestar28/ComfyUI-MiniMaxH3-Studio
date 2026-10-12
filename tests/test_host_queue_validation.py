"""Registry-wide queue projection, inherited hooks and owned refusal contracts."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, cast

import pytest
from oracles.host_queue_validation import SCENARIOS, validation_call

from comfyui_h3_context import NODE_CLASS_MAPPINGS
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextNativeH3AdapterNode,
    H3ContextPlanNode,
    H3ContextPreviewNode,
    H3ContextValidatorNode,
)

ROOT = Path(__file__).resolve().parents[1]
TABLE = json.loads(
    (ROOT / "tests/fixtures/host_queue_validation_expectations_v1.json").read_bytes()
)
VALIDATORS: dict[str, Any] = {
    cls.__name__: cls for cls in NODE_CLASS_MAPPINGS.values() if hasattr(cls, "VALIDATE_INPUTS")
}
TIMELINE = "H3FullReferenceTimelineProducerNode"


def result_for(name: str, scenario: str) -> bool | str:
    cls = VALIDATORS[name]
    signature = inspect.signature(cls.VALIDATE_INPUTS)
    kwargs = validation_call(
        cls.INPUT_TYPES(),
        set(signature.parameters),
        scenario,
        input_is_list=getattr(cls, "INPUT_IS_LIST", False),
        accepts_kwargs=any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ),
    )
    return cast(bool | str, cls.VALIDATE_INPUTS(**kwargs))


def assert_expected(name: str, scenario: str) -> None:
    result = result_for(name, scenario)
    if TABLE["nodes"][name][scenario] == "admit":
        assert result is True, f"{name}/{scenario}: expected admission, got {result!r}"
    else:
        assert isinstance(result, str) and result, f"{name}/{scenario}: expected explicit refusal"


def test_complete_registered_validator_population_and_scenarios() -> None:
    assert len(NODE_CLASS_MAPPINGS) == TABLE["registered_class_count"]
    assert set(VALIDATORS) == set(TABLE["nodes"])
    assert tuple(TABLE["scenarios"]) == SCENARIOS
    for row in TABLE["nodes"].values():
        assert set(row) == {*SCENARIOS, "reason"}
        assert row["reason"]


@pytest.mark.parametrize(
    "name,scenario",
    [
        (name, scenario)
        for name in sorted(TABLE["nodes"])
        for scenario in SCENARIOS
        if (name, scenario) != (TIMELINE, "all_links_connected")
    ],
)
def test_queue_projection_matches_handwritten_contract(name: str, scenario: str) -> None:
    assert_expected(name, scenario)


def test_qualified_visual_link_is_admitted() -> None:
    assert_expected(TIMELINE, "all_links_connected")


def test_old_five_typed_placeholders_still_defer() -> None:
    assert H3ContextPlanNode.VALIDATE_INPUTS(None) is True
    assert H3ContextCompilerNode.VALIDATE_INPUTS(None) is True
    assert H3ContextValidatorNode.VALIDATE_INPUTS(None, None) is True
    assert H3ContextPreviewNode.VALIDATE_INPUTS(None) is True
    assert H3ContextNativeH3AdapterNode.VALIDATE_INPUTS(None) is True


def test_old_five_direct_invalid_objects_remain_rejected() -> None:
    for result in (
        H3ContextPlanNode.VALIDATE_INPUTS(object()),
        H3ContextCompilerNode.VALIDATE_INPUTS(object()),
        H3ContextValidatorNode.VALIDATE_INPUTS(object(), object()),
        H3ContextPreviewNode.VALIDATE_INPUTS(object()),
        H3ContextNativeH3AdapterNode.VALIDATE_INPUTS(object()),
    ):
        assert isinstance(result, str) and result


def test_oracle_preserves_absence_list_sentinel_and_type_delegation() -> None:
    schema: dict[str, Any] = {
        "required": {"source": ("IMAGE", {})},
        "optional": {"audio": ("AUDIO", {})},
    }
    assert validation_call(schema, {"source", "audio"}, "required_links_only") == {"source": None}
    assert validation_call(schema, {"source"}, "required_links_only", input_is_list=True) == {
        "source": (None,)
    }
    assert validation_call(schema, {"source", "audio", "input_types"}, "all_links_connected") == {
        "source": None,
        "audio": None,
        "input_types": {"source": "IMAGE", "audio": "AUDIO"},
    }
