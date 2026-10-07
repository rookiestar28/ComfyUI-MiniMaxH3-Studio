"""M16-01 source requalification contract and fail-closed consistency tests."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from scripts.m16_01_source_requalification import (
    M16RequalificationError,
    build_default_requalification,
    load_requalification,
    validate_requalification,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "m16_01_source_requalification.json"
SCHEMA = ROOT / "governance" / "contracts" / "source_requalification_v1.schema.json"


def _document() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


def _source_document(source: str) -> dict[str, Any]:
    if source == "builder":
        return cast(dict[str, Any], build_default_requalification())
    return _document()


def test_m16_01_fixture_is_complete_and_passes_offline_validation() -> None:
    result = load_requalification(FIXTURE, ROOT)
    assert result.status == "PASS"
    assert result.item == "M16-01"
    assert result.supported_profile_status == "QUALIFIED_UNCHANGED"
    assert result.latest_substitution_authorized is False
    assert result.network_performed is False
    assert result.model_downloaded is False
    assert result.media_read is False


def test_m16_01_public_contract_schema_is_closed_and_matches_fixture() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    document = _document()
    assert schema["$id"] == "comfyui-h3-context://contracts/source_requalification_v1.schema.json"
    assert schema["properties"]["schema"]["const"] == "h3-context-source-requalification/1"
    assert schema["additionalProperties"] is False
    jsonschema.Draft202012Validator(schema).validate(document)


def test_named_api_model_family_and_dynamic_surfaces_are_pinned() -> None:
    document = _document()
    surfaces = {item["surface_id"]: item for item in document["surfaces"]}
    assert {
        "minimax.guide",
        "comfyui.text_generate",
        "comfyui.clip_loader",
        "comfyui.model_family",
        "comfyui.docs.app_mode",
        "comfyui.docs.subgraph_blueprints",
        "comfyui.docs.subgraphs",
        "ollama.docs.chat",
        "ollama.docs.tags",
        "ollama.docs.show",
        "ollama.docs.ps",
    } <= surfaces.keys()
    assert surfaces["comfyui.text_generate"]["disposition"] == "EXACT_MATCH"
    assert surfaces["comfyui.model_family"]["disposition"] == "EXACT_MATCH"
    for surface_id in ("comfyui.docs.app_mode", "comfyui.docs.subgraphs"):
        assert surfaces[surface_id]["baseline_revision"] == "unavailable"
        assert surfaces[surface_id]["disposition"] == "BLOCKED_PENDING_REQUALIFICATION"


def test_latest_api_authority_and_subgraph_drift_remain_blocked() -> None:
    document = _document()
    diffs = {item["diff_id"]: item for item in document["schema_diffs"]}
    assert diffs["comfyui.api.versioning"]["disposition"] == "BLOCKED_PENDING_REQUALIFICATION"
    assert diffs["comfyui.dynamic_subgraph"]["baseline_fingerprint"] == "unavailable"
    profile = document["supported_profile"]
    assert profile["node_api"] == "V1_ONLY"
    assert profile["latest_substitution_authorized"] is False


def test_material_latest_native_drift_cannot_promote_supported_profile() -> None:
    document = _document()
    mutated = copy.deepcopy(document)
    mutated["supported_profile"]["status"] = "QUALIFIED_LATEST"
    with pytest.raises(M16RequalificationError, match="supported_profile.status"):
        validate_requalification(mutated, ROOT)


def test_schema_diff_must_bind_to_declared_surface_and_preserve_severity() -> None:
    document = _document()
    mutated = copy.deepcopy(document)
    mutated["schema_diffs"][0]["surface_id"] = "unknown.surface"
    with pytest.raises(M16RequalificationError, match=r"schema_diffs\[0\].surface_id"):
        validate_requalification(mutated, ROOT)


def test_declared_surface_identity_and_material_disposition_are_pinned() -> None:
    document = _document()
    mutated = copy.deepcopy(document)
    mutated["surfaces"][0]["observed_revision"] = "git:0000000000000000000000000000000000000000"
    with pytest.raises(M16RequalificationError, match="surfaces.*frozen"):
        validate_requalification(mutated, ROOT)


def test_claim_source_references_and_closed_record_inventory_are_required() -> None:
    document = _document()
    unknown_source = copy.deepcopy(document)
    unknown_source["claims"][0]["source_ids"] = ["private.source"]
    with pytest.raises(M16RequalificationError, match="source_ids"):
        validate_requalification(unknown_source, ROOT)

    missing_claim = copy.deepcopy(document)
    missing_claim["claims"] = [
        item for item in missing_claim["claims"] if item["record_id"] != "native.string_boundary"
    ]
    with pytest.raises(M16RequalificationError, match="claims"):
        validate_requalification(missing_claim, ROOT)


def test_schema_diff_expected_finding_cannot_be_relabelled_as_unchanged() -> None:
    document = _document()
    mutated = copy.deepcopy(document)
    mutated["schema_diffs"][0]["severity"] = "unchanged"
    mutated["schema_diffs"][0]["disposition"] = "UNCHANGED_CONTRACT"
    with pytest.raises(M16RequalificationError, match="schema_diffs.*frozen"):
        validate_requalification(mutated, ROOT)


def test_private_unknown_and_signed_source_fields_fail_closed() -> None:
    document = _document()
    mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
        lambda value: value["surfaces"][0].update({"token": "secret"}),
        lambda value: value["surfaces"][0].update(
            {"url": "https://example.invalid/source?signature=secret"}
        ),
        lambda value: value.update({"private_path": "C:/secret"}),
    )
    for mutation in mutations:
        mutated = copy.deepcopy(document)
        mutation(mutated)
        with pytest.raises(M16RequalificationError):
            validate_requalification(mutated, ROOT)


def test_loader_rejects_duplicate_members_and_nonfinite_numbers(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema":"h3-context-source-requalification/1","schema":"again"}', encoding="utf-8"
    )
    with pytest.raises(M16RequalificationError, match="duplicate JSON member"):
        load_requalification(duplicate, ROOT)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text(
        '{"schema":"h3-context-source-requalification/1","value":NaN}', encoding="utf-8"
    )
    with pytest.raises(M16RequalificationError, match="non-finite JSON"):
        load_requalification(nonfinite, ROOT)


def test_claims_cannot_promote_assisted_or_weight_backed_capability() -> None:
    document = _document()
    mutated = copy.deepcopy(document)
    claim = next(item for item in mutated["claims"] if item["record_id"] == "product.scope")
    claim["status"] = "accepted"
    claim["statement"] = "ASSISTED_PROFILE_QUALIFIED"
    with pytest.raises(M16RequalificationError, match="product.scope"):
        validate_requalification(mutated, ROOT)


@pytest.mark.parametrize("source", ("builder", "fixture"))
def test_current_package_license_separation_claim_is_apache_2(source: str) -> None:
    document = _source_document(source)
    claim = next(item for item in document["claims"] if item["record_id"] == "model.license")
    assert (
        claim["statement"]
        == "MiniMax H3 Community License remains separate from this Apache-2.0 package"
    )


@pytest.mark.parametrize("source", ("builder", "fixture"))
def test_current_project_attribution_is_apache_2_without_rewriting_dependencies(
    source: str,
) -> None:
    document = _source_document(source)
    attributions = {item["attribution_id"]: item for item in document["attributions"]}
    assert attributions["project"]["license"] == "Apache-2.0"
    assert attributions["ollama"]["license"] == "MIT (upstream project)"


def test_cli_check_is_offline_and_deterministic() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "m16_01_source_requalification.py"),
            "--ledger",
            str(FIXTURE),
            "--repo-root",
            str(ROOT),
            "--check",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"status": "PASS"' in completed.stdout
    assert '"network_performed": false' in completed.stdout
