"""M15-17 public workflow, surface, and migration lifecycle authority."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "governance" / "contracts" / "workflow_surface_matrix_v1.json"
SCHEMA_PATH = ROOT / "governance" / "contracts" / "workflow_surface_matrix_v1.schema.json"


def _load(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, member in items:
            if key in value:
                raise AssertionError(f"duplicate JSON member: {key}")
            value[key] = member
        return value

    return cast(
        dict[str, Any],
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs),
    )


def test_matrix_classifies_every_public_workflow_subgraph_and_example() -> None:
    matrix = _load(MATRIX_PATH)
    assert matrix["schema"] == "h3-context-workflow-surface-matrix/1"
    expected = {
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "workflows", ROOT / "subgraphs")
        for path in root.glob("*.json")
    }
    expected.add("examples/minimal_core_pipeline.py")
    rows = matrix["artifacts"]
    assert {row["path"] for row in rows} == expected
    assert len(rows) == 21
    for row in rows:
        assert row["purpose"] in {
            "adapter",
            "compatibility",
            "convenience",
            "executable_example",
            "scenario",
        }
        assert row["qualification"] in {
            "qualified",
            "retained_compatibility",
            "retained_structural",
        }
        assert row["lifecycle"] == "RETAIN"
        assert row["rollback"] == "preserve_original_bytes"


def test_matrix_schema_is_closed_and_rejects_drift() -> None:
    matrix = _load(MATRIX_PATH)
    schema = _load(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(matrix)) == []

    unknown = copy.deepcopy(matrix)
    unknown["private_path"] = "not-allowed"
    assert list(validator.iter_errors(unknown))

    expanded = copy.deepcopy(matrix)
    expanded["modes"][-1]["sidebar_limits"]["image"] = 9
    assert list(validator.iter_errors(expanded))


def test_matrix_closes_mode_surfaces_and_ref2va_cardinality() -> None:
    matrix = _load(MATRIX_PATH)
    modes = {row["mode"]: row for row in matrix["modes"]}
    assert set(modes) == {"t2va", "i2va", "fl2va", "l2va", "ref2va"}
    for mode, row in modes.items():
        assert row["direct_node"] == "qualified"
        assert row["sidebar"] == "qualified"
        assert row["api_headless"] == "qualified"
        if mode == "t2va":
            assert row["fixed_subgraph"] == "qualified_base"
        elif mode == "ref2va":
            assert row["fixed_subgraph"] == "qualified_reference_bounded"
            assert row["sidebar_limits"] == {
                "image": 1,
                "video": 1,
                "standalone_audio": 1,
                "total_min": 1,
                "total_max": 3,
            }
        else:
            assert row["fixed_subgraph"] == "unavailable_no_published_fixture"


def test_legacy_lifecycle_is_separate_from_runtime_audit_enum() -> None:
    matrix = _load(MATRIX_PATH)
    assert matrix["runtime_audit_dispositions"] == ["ACCEPTED", "REJECTED"]
    rows = matrix["legacy_dispositions"]
    assert {row["lifecycle"] for row in rows} == {"MIGRATE", "RETAIN", "REJECT", "ROLLBACK"}
    for row in rows:
        assert row["source"]
        assert row["reason"]
        assert row["safe_action"]
        assert row["mutates_rejected_input"] is False
