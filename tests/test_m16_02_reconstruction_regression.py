"""M16-02 reconstruction-regression matrix contract tests."""

# Keep adversarial matrix mutations readable beside their expected diagnostics.
# ruff: noqa: E501

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from scripts.m16_02_reconstruction_regression import (
    EXPECTED_CRITERIA,
    EXPECTED_REGRESSIONS,
    ReconstructionRegressionError,
    canonical_fingerprint,
    load_matrix,
    validate_matrix,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m16_02_reconstruction_regression.json"


def _matrix() -> dict[str, object]:
    return load_matrix(FIXTURE)


def _criterion(matrix: dict[str, object], criterion_id: str) -> dict[str, object]:
    criteria = matrix["criteria"]
    assert isinstance(criteria, list)
    return next(
        item
        for raw_item in criteria
        if (item := cast(dict[str, object], raw_item))["id"] == criterion_id
    )


def _regression(matrix: dict[str, object], regression_id: str) -> dict[str, object]:
    regressions = matrix["regressions"]
    assert isinstance(regressions, list)
    return next(
        item
        for raw_item in regressions
        if (item := cast(dict[str, object], raw_item))["id"] == regression_id
    )


def test_generated_matrix_has_complete_criteria_and_regressions() -> None:
    matrix = _matrix()
    criteria = matrix["criteria"]
    regressions = matrix["regressions"]
    assert isinstance(criteria, list)
    assert isinstance(regressions, list)
    assert tuple(cast(dict[str, object], item)["id"] for item in criteria) == EXPECTED_CRITERIA
    assert (
        tuple(cast(dict[str, object], item)["id"] for item in regressions) == EXPECTED_REGRESSIONS
    )
    assert matrix["qualification_status"] == "MANUAL_ONLY_SCOPED"
    assert validate_matrix(matrix, repo_root=ROOT) == matrix["fingerprint"]
    assert canonical_fingerprint(matrix) == matrix["fingerprint"]


def test_schema_is_closed_and_matches_contract() -> None:
    schema = json.loads(
        (ROOT / "governance" / "contracts" / "reconstruction_regression_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["$id"].endswith("reconstruction_regression_v1.schema.json")
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Criterion"]["additionalProperties"] is False
    assert schema["$defs"]["Regression"]["additionalProperties"] is False


def test_unavailable_h3_oracle_and_ollama_lanes_are_not_passes() -> None:
    matrix = _matrix()
    for criterion_id in ("P9", "P10", "E3", "E4"):
        criterion = _criterion(matrix, criterion_id)
        assert criterion["status"] in {"UNAVAILABLE", "BLOCKED"}
        assert criterion["disposition"] in {"unavailable", "blocked"}
    profiles = matrix["profiles"]
    assert isinstance(profiles, list)
    for profile_id in ("fixed-h3-generation", "official-oracle", "ollama-live"):
        profile = next(
            item
            for raw_item in profiles
            if (item := cast(dict[str, object], raw_item))["profile_id"] == profile_id
        )
        assert profile["disposition"] in {"unavailable", "blocked", "unsupported"}


def test_v3_and_native_stripped_paths_cannot_be_promoted() -> None:
    matrix = _matrix()
    assert _criterion(matrix, "P13")["status"] == "BLOCKED"
    guards = cast(dict[str, object], matrix["substitution_guards"])
    assert guards["native_stripped_qualifies"] is False
    assert guards["fallback_satisfies_native"] is False
    assert (
        _regression(matrix, "fully_qualified_v3_child_bindings")["subject_disposition"] == "blocked"
    )
    guards = cast(dict[str, object], matrix["substitution_guards"])
    assert guards["generic_registration_satisfies_native"] is False


def test_lane_contract_has_provider_and_evidence_kind_fields() -> None:
    matrix = _matrix()
    criterion = _criterion(matrix, "P1")
    execution = cast(dict[str, object], criterion["execution"])
    assert execution["provider_enabled"] is False
    assert "provider_selected" not in execution
    assert criterion["evidence_kind"] in {
        "structural_fixture",
        "host_e2e",
        "browser_host_probe",
        "release_artifact",
        "security_audit",
        "oracle_disposition",
        "generation_disposition",
        "provider_disposition",
        "manual_boundary",
    }


def test_duplicate_json_members_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
    with pytest.raises(ReconstructionRegressionError, match="duplicate"):
        load_matrix(duplicate)


def test_non_finite_json_numbers_are_rejected(tmp_path: Path) -> None:
    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text('{"schema":"x","value":NaN}', encoding="utf-8")
    with pytest.raises(ReconstructionRegressionError, match="finite"):
        load_matrix(non_finite)


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda m: m.update({"private_token": "x"}), "unknown/private"),
        (lambda m: m["profiles"][0].update({"private_path": "x"}), "unknown/private"),
        (lambda m: m["criteria"].pop(), "criteria"),
        (lambda m: m["criteria"][0].update({"evidence_refs": []}), "evidence"),
        (
            lambda m: _criterion(m, "P9").update({"status": "PASS", "disposition": "qualified"}),
            "unavailable",
        ),
        (lambda m: m["profiles"][1].update({"disposition": "qualified"}), "profile"),
        (lambda m: m["skip_debt"].update({"unapproved_count": 1}), "skip"),
        (
            lambda m: m["criteria"][0]["execution"].update({"network_contacted": True}),
            "side effect",
        ),
        (
            lambda m: _criterion(m, "P13").update({"status": "PASS", "disposition": "qualified"}),
            "V3",
        ),
        (
            lambda m: m["criteria"][0]["execution"].pop("provider_enabled"),
            "missing required fields",
        ),
        (
            lambda m: m["criteria"][0].update({"evidence_kind": "unknown_kind"}),
            "unsupported",
        ),
        (
            lambda m: m["substitution_guards"].update(
                {"generic_registration_satisfies_native": True}
            ),
            "must be false",
        ),
        (
            lambda m: m["artifact_boundary"].update({"one_entry_bundle": False}),
            "artifact_boundary",
        ),
    ],
)
def test_adversarial_matrix_mutations_fail_closed(
    mutation: Callable[[dict[str, object]], None], expected: str
) -> None:
    matrix = copy.deepcopy(_matrix())
    mutation(matrix)
    with pytest.raises(ReconstructionRegressionError, match=expected):
        validate_matrix(matrix, repo_root=ROOT)


def test_internal_evidence_paths_are_rejected() -> None:
    matrix = copy.deepcopy(_matrix())
    _criterion(matrix, "P1")["evidence_refs"] = [".planning/secret.json"]
    with pytest.raises(ReconstructionRegressionError, match="evidence"):
        validate_matrix(matrix, repo_root=ROOT)


def test_locator_and_private_content_values_are_rejected() -> None:
    matrix = copy.deepcopy(_matrix())
    _criterion(matrix, "P1")["claim_ceiling"] = "https://private.example/payload"
    with pytest.raises(ReconstructionRegressionError, match="forbidden"):
        validate_matrix(matrix, repo_root=ROOT)


@pytest.mark.parametrize(
    "marker",
    [
        "raw_prompt",
        "file://private",
        "authorization",
        "credential",
        "provider_payload",
        "raw provider content",
        "provider payload",
        "provider content",
        "raw prompt content",
        "raw media content",
        "api key",
        "private media",
        "local path",
        "signed URL",
        "file:private",
    ],
)
def test_raw_prompt_and_provider_private_markers_are_rejected(marker: str) -> None:
    matrix = copy.deepcopy(_matrix())
    _criterion(matrix, "P1")["claim_ceiling"] = marker
    with pytest.raises(ReconstructionRegressionError, match="forbidden"):
        validate_matrix(matrix, repo_root=ROOT)


def test_recomputed_fingerprint_cannot_drift_from_generated_frozen_output() -> None:
    matrix = copy.deepcopy(_matrix())
    _criterion(matrix, "P1")["title"] = "drifted title"
    matrix["fingerprint"] = canonical_fingerprint(matrix)
    with pytest.raises(ReconstructionRegressionError, match="frozen output"):
        validate_matrix(matrix, repo_root=ROOT)


def test_status_skip_is_not_a_valid_disposition() -> None:
    matrix = copy.deepcopy(_matrix())
    _criterion(matrix, "P1")["status"] = "SKIP"
    with pytest.raises(ReconstructionRegressionError, match="status"):
        validate_matrix(matrix, repo_root=ROOT)
