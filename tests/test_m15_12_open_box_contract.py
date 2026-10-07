from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.m15_12_open_box_contract import (
    ContractError,
    _contains_sensitive,
    _pnpm_executable,
    classify_red_exit,
    contract_digest,
    load_contract,
    main,
    redact_text,
    source_fingerprints,
    validate_contract,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tests" / "fixtures" / "m15_12_open_box_contract.json"


def _valid_report(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "h3-context-open-box-red/1",
        "status": "RED_REPRODUCED",
        "item": "M15-12",
        "candidate": "a" * 40,
        "public_worktree": True,
        "contract_sha256": contract_digest(contract),
        "source_fingerprints": source_fingerprints(ROOT),
        "command": [
            _pnpm_executable(),
            "--dir",
            "frontend",
            "exec",
            "vitest",
            "run",
            "--config",
            "vitest.m15-12-red.config.ts",
        ],
        "failure_count": 9,
        "exit_code": 1,
        "criteria": [
            {"test_id": entry["test_id"], "status": "RED"} for entry in contract["criteria"]
        ],
        "first_failure": "RED-M15-12-01",
        "output": "sanitized assertion output",
    }


def test_contract_fixture_is_closed_and_maps_every_criterion() -> None:
    contract = load_contract(CONTRACT_PATH)
    validate_contract(contract)
    assert contract["schema"] == "h3-context-open-box-contract/1"
    criteria = contract["criteria"]
    assert len(criteria) == 9
    assert {entry["test_id"] for entry in criteria} == {
        f"RED-M15-12-{index:02d}" for index in range(1, 10)
    }
    assert contract["acceptance_semantics"] == {
        "evidence_layers": ["reproduce", "pin", "sweep"],
        "final_owner": "M15-18",
        "forbidden_substitutes": [
            "prose_only",
            "screenshot_only",
            "dom_existence_only",
            "full_gate_only",
            "viewport_equals_panel_only",
            "prior_blocker_review_only",
        ],
        "legacy_gap_sources": ["M15-03", "M15-04", "M15-09", "M15-10", "M15-11"],
        "required_join_fields": [
            "criterion_id",
            "test_id",
            "transition",
            "dom_roles_actions",
            "graph_mutations",
            "queue_calls",
            "projection",
            "geometry",
            "privacy",
            "cleanup",
            "retained_artifact",
        ],
        "review_scope": "every_criterion",
    }
    assert criteria[6]["required_roles"] == ["tab", "tabpanel", "button"]


def test_contract_rejects_semantic_drift_in_a_known_criterion() -> None:
    contract = load_contract(CONTRACT_PATH)
    contract["criteria"][1]["required_action"] = "terminal_node_only"
    with pytest.raises(ContractError, match="semantic"):
        validate_contract(contract)


def test_contract_requires_transition_and_non_substituting_evidence() -> None:
    contract = load_contract(CONTRACT_PATH)
    contract["criteria"][1]["transition"] = ["native_preference", "node-only"]
    with pytest.raises(ContractError, match="transition"):
        validate_contract(contract)
    contract = load_contract(CONTRACT_PATH)
    contract["acceptance_semantics"]["forbidden_substitutes"].remove("prose_only")
    with pytest.raises(ContractError, match="acceptance_semantics"):
        validate_contract(contract)


def test_contract_pins_the_independent_viewport_panel_matrix() -> None:
    contract = load_contract(CONTRACT_PATH)
    contract["geometry"]["pairs"][0]["viewport_width"] = 480
    with pytest.raises(ContractError, match="viewport/panel matrix"):
        validate_contract(contract)

    contract = load_contract(CONTRACT_PATH)
    contract["geometry"]["minimum_panel_px"] = 560
    with pytest.raises(ContractError, match="480px floor"):
        validate_contract(contract)


def test_contract_rejects_unknown_members() -> None:
    contract = load_contract(CONTRACT_PATH)
    contract["unexpected"] = True
    with pytest.raises(ContractError, match="unknown"):
        validate_contract(contract)


def test_contract_rejects_duplicate_criterion_ids() -> None:
    contract = load_contract(CONTRACT_PATH)
    contract["criteria"][1]["id"] = contract["criteria"][0]["id"]
    with pytest.raises(ContractError, match="duplicate"):
        validate_contract(contract)


def test_red_exit_requires_expected_nonzero_failure() -> None:
    assert classify_red_exit(1, "failed") == "RED_REPRODUCED"
    assert classify_red_exit(2, "failed") == "SETUP_ERROR"
    assert classify_red_exit(0, "passed") == "UNEXPECTED_PASS"
    assert classify_red_exit(1, "cannot find module") == "SETUP_ERROR"
    assert classify_red_exit(1, "Invalid Chai property: toBeInTheDocument") == "SETUP_ERROR"


def test_redaction_removes_paths_and_sensitive_markers() -> None:
    text = (
        "B:\\private\\workspace\\frontend\\tests\\x.tsx "
        "https://example.invalid/signed?token=secret raw prompt content"
    )
    redacted = redact_text(text, ROOT)
    assert "B:\\private" not in redacted
    assert "https://" not in redacted
    assert "token=secret" not in redacted
    assert "raw prompt" not in redacted.casefold()


def test_redaction_rejects_unc_and_generic_posix_absolute_paths() -> None:
    text = r"\\server\share\secret.txt //server/share/secret.txt /srv/private/secret.txt"
    assert _contains_sensitive(text)
    redacted = redact_text(text, ROOT)
    assert r"\\server\share\secret.txt" not in redacted
    assert "//server/share/secret.txt" not in redacted
    assert "/srv/private/secret.txt" not in redacted
    assert "[REDACTED_PATH]" in redacted


def test_report_requires_exact_criteria_and_candidate_fingerprint() -> None:
    contract = load_contract(CONTRACT_PATH)
    validate_contract(contract)
    report = _valid_report(contract)
    validate_report(report, contract, expected_candidate="a" * 40)
    report_criteria = report["criteria"]
    assert isinstance(report_criteria, list)
    report_criteria.pop()
    with pytest.raises(ContractError, match="criteria"):
        validate_report(report, contract, expected_candidate="a" * 40)

    report = _valid_report(contract)
    report_criteria = report["criteria"]
    assert isinstance(report_criteria, list)
    report_criteria.reverse()
    with pytest.raises(ContractError, match="criteria"):
        validate_report(report, contract, expected_candidate="a" * 40)


def test_report_rejects_stale_or_partial_source_fingerprints() -> None:
    contract = load_contract(CONTRACT_PATH)
    report = _valid_report(contract)
    fingerprints = report["source_fingerprints"]
    assert isinstance(fingerprints, dict)
    first_path = next(iter(fingerprints))
    fingerprints[first_path] = "0" * 64
    with pytest.raises(ContractError, match="source fingerprint"):
        validate_report(report, contract, expected_candidate="a" * 40)

    report = _valid_report(contract)
    fingerprints = report["source_fingerprints"]
    assert isinstance(fingerprints, dict)
    fingerprints.pop(next(iter(fingerprints)))
    with pytest.raises(ContractError, match="source fingerprint"):
        validate_report(report, contract, expected_candidate="a" * 40)


def test_report_requires_all_nine_failures_and_a_strict_boolean() -> None:
    contract = load_contract(CONTRACT_PATH)
    report = _valid_report(contract)
    report["failure_count"] = 8
    with pytest.raises(ContractError, match="failure_count"):
        validate_report(report, contract, expected_candidate="a" * 40)

    report = _valid_report(contract)
    report["public_worktree"] = 1
    with pytest.raises(ContractError, match="public_worktree must be boolean"):
        validate_report(report, contract, expected_candidate="a" * 40)

    report = _valid_report(contract)
    report["public_worktree"] = False
    with pytest.raises(ContractError, match="public_worktree must be true"):
        validate_report(report, contract, expected_candidate="a" * 40)
    validate_report(
        report,
        contract,
        expected_candidate="a" * 40,
        require_clean=False,
    )


def test_report_requires_exact_red_exit_command_and_first_failure() -> None:
    contract = load_contract(CONTRACT_PATH)
    report = _valid_report(contract)
    report["exit_code"] = 2
    with pytest.raises(ContractError, match="exactly 1"):
        validate_report(report, contract, expected_candidate="a" * 40)

    report = _valid_report(contract)
    report["command"] = ["vitest", "run"]
    with pytest.raises(ContractError, match="dedicated M15-12 RED lane"):
        validate_report(report, contract, expected_candidate="a" * 40)

    report = _valid_report(contract)
    report["first_failure"] = "generic_test_failure"
    with pytest.raises(ContractError, match="contract test id"):
        validate_report(report, contract, expected_candidate="a" * 40)


def test_report_rejects_sensitive_or_locator_bearing_command_values() -> None:
    contract = load_contract(CONTRACT_PATH)
    report = _valid_report(contract)
    report["command"] = ["vitest", "https://example.invalid/?token=secret"]
    with pytest.raises(ContractError, match="command"):
        validate_report(report, contract, expected_candidate="a" * 40)

    report = _valid_report(contract)
    report["output"] = r"C:\private\workspace\failure.txt"
    with pytest.raises(ContractError, match="output"):
        validate_report(report, contract, expected_candidate="a" * 40)


@pytest.mark.parametrize("outside", ["contract", "report"])
def test_cli_rejects_absolute_contract_and_report_paths_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: str
) -> None:
    # IMPORTANT: pytest temp can live inside this checkout. Use an explicit fixture workspace
    # so both absolute-path escape checks stay real without writing outside the project.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("scripts.m15_12_open_box_contract.ROOT", workspace)
    contract = load_contract(CONTRACT_PATH)
    contract_path = (tmp_path if outside == "contract" else workspace) / "contract.json"
    report_path = (tmp_path if outside == "report" else workspace) / "report.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    report_path.write_text(json.dumps(_valid_report(contract)), encoding="utf-8")
    with pytest.raises(ContractError, match="escapes repository"):
        main(
            [
                "--check",
                "--contract",
                str(contract_path),
                "--report",
                str(report_path),
            ]
        )


def test_cli_rejects_report_output_in_a_public_workspace_path() -> None:
    with pytest.raises(ContractError, match="ignored .planning"):
        main(["--run-red", "--report", "tests/forbidden-red-report.json"])
    with pytest.raises(ContractError, match="one JSON file"):
        main(["--run-red", "--report", ".planning/not-a-json-report.txt"])


def test_cli_prints_only_a_repository_relative_report_path(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path = ROOT / ".planning" / "M15-12-OPEN_BOX_RED_REPORT.json"
    contract = load_contract(CONTRACT_PATH)
    report = _valid_report(contract)
    monkeypatch.setattr(
        "scripts.m15_12_open_box_contract.load_contract",
        lambda path: report if path == report_path else contract,
    )
    monkeypatch.setattr(
        "scripts.m15_12_open_box_contract.validate_report", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("scripts.m15_12_open_box_contract._git_output", lambda *args: "")
    assert main(["--check", "--report", str(report_path.relative_to(ROOT))]) == 0
    output = capsys.readouterr().out
    assert str(ROOT) not in output
    assert json.loads(output)["report"] == ".planning/M15-12-OPEN_BOX_RED_REPORT.json"


def test_loader_rejects_duplicate_json_members(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
    with pytest.raises(ContractError, match="duplicate"):
        load_contract(path)


def test_loader_rejects_non_finite_json_values(tmp_path: Path) -> None:
    path = tmp_path / "nonfinite.json"
    path.write_text('{"schema":"x","value":NaN}', encoding="utf-8")
    with pytest.raises(ContractError, match="non-finite"):
        load_contract(path)


def test_report_is_json_serializable() -> None:
    contract = load_contract(CONTRACT_PATH)
    validate_contract(contract)
    assert json.dumps(contract, ensure_ascii=False)
