"""Current connection qualification guards; no test observation is live provider evidence."""

import json
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import pytest

from comfyui_h3_context.adapters.prompt_model_transport import (
    PromptModelTransportError,
    RemoteExchangeMetrics,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelContractError,
    PromptModelOutcomeId,
    load_prompt_model_catalog,
)
from comfyui_h3_context.core.provider_connection_qualification import (
    RESULT_KEYS,
    build_remote_connection_qualification_evidence,
)
from comfyui_h3_context.core.remote_provider_policy import policy_for_profile
from scripts import prompt_model_qualification as tool

PROFILES = ("openai.remote", "gemini.remote", "anthropic.remote")
MODEL = "arbitrary-text-model-2026"
COMMIT = "a" * 40
TREE = "b" * 40


def arguments(
    path: Path, *, profile_id: str = "openai.remote", mode: str = "hermetic"
) -> list[str]:
    argv = ["--mode", mode, "--profile", profile_id, "--model", MODEL, "--output", str(path)]
    if mode == "live":
        policy = policy_for_profile(load_prompt_model_catalog().require(profile_id))
        argv += [
            "--authorize-policy-sha256",
            policy.fingerprint,
            "--authorize-max-transmissions",
            str(policy.max_transmissions),
            "--candidate-commit",
            COMMIT,
            "--candidate-tree",
            TREE,
        ]
    return argv


def no_key(_prompt: str) -> str:
    pytest.fail("credential access must not occur")


def no_candidate() -> tuple[str, str]:
    pytest.fail("candidate access must not occur")


def hermetic_result(path: Path, profile_id: str = "openai.remote") -> dict[str, object]:
    assert (
        tool.main(
            arguments(path, profile_id=profile_id),
            credential_reader=no_key,
            candidate_identity_reader=no_candidate,
        )
        == 0
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("profile_id", PROFILES)
def test_current_hermetic_uses_explicit_model_and_has_no_socket_key_or_price(
    tmp_path: Path,
    profile_id: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(tool, "RemoteHttpsExchange", side_effect=AssertionError("socket opened")):
        report = hermetic_result(tmp_path / "report.json", profile_id)
    assert set(report) == RESULT_KEYS
    assert report["model_id"] == MODEL
    assert report["observed_transmissions"] == 2
    assert report["basis"] == "completion"
    assert report["catalog_promotion_performed"] is False
    assert report["repository_commit"] is None and report["repository_tree"] is None
    wire = json.dumps(report)
    assert "quiet street" not in wire
    assert not any(
        word in wire for word in ("price_basis", "max_cost", "credential", "api_key", "host")
    )
    profile = load_prompt_model_catalog().require(profile_id)
    with pytest.raises(PromptModelContractError):
        build_remote_connection_qualification_evidence(report, profile=profile)
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def live_shaped_fixture(path: Path) -> dict[str, object]:
    """Synthetic schema fixture only; this does not record or prove an external observation."""
    report = hermetic_result(path)
    return {**report, "mode": "live", "repository_commit": COMMIT, "repository_tree": TREE}


def test_strict_live_reducer_derives_hash_and_never_uses_a_caller_digest(tmp_path: Path) -> None:
    report = live_shaped_fixture(tmp_path / "fixture.json")
    profile = load_prompt_model_catalog().require("openai.remote")
    evidence = build_remote_connection_qualification_evidence(report, profile=profile)
    assert evidence.observation_model_id == MODEL
    changed = {**report, "observed_on": "2026-10-07"}
    other = build_remote_connection_qualification_evidence(changed, profile=profile)
    assert other.evidence_basis_sha256 != evidence.evidence_basis_sha256
    assert not {"model_id", "price_basis_id", "max_cost_micro_usd"} & evidence.to_wire().keys()


@pytest.mark.parametrize("http_status", [402, 429])
def test_answered_quota_proves_reachability_without_claiming_a_draft(
    tmp_path: Path,
    http_status: int,
) -> None:
    class QuotaExchange(tool._HermeticExchange):
        def request(
            self,
            method: str,
            path: str,
            payload: Mapping[str, object] | None = None,
            *,
            timeout_seconds: float | None = None,
        ) -> Mapping[str, object]:
            if method != "POST":
                return super().request(method, path, payload, timeout_seconds=timeout_seconds)
            self.calls.append((method, path))
            self.metrics = RemoteExchangeMetrics(
                http_status=http_status,
                request_bytes=256,
                response_bytes=64,
                prompt_tokens=0,
                completion_tokens=0,
                usage_present=False,
            )
            raise PromptModelTransportError(PromptModelOutcomeId.QUOTA, "private provider prose")

    path = tmp_path / "quota.json"
    assert (
        tool.main(
            arguments(path),
            credential_reader=no_key,
            candidate_identity_reader=no_candidate,
            exchange_factory=lambda profile, model, _key: QuotaExchange(profile, model),
        )
        == 0
    )
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["basis"] == "reachability"
    assert report["schema_valid"] is False and report["receipt_valid"] is True
    assert "private provider prose" not in path.read_text(encoding="utf-8")
    profile = load_prompt_model_catalog().require("openai.remote")
    with pytest.raises(PromptModelContractError):
        build_remote_connection_qualification_evidence(report, profile=profile)
    # Synthetic decoder fixture only; no provider observation is recorded or promoted.
    fixture = {**report, "mode": "live", "repository_commit": COMMIT, "repository_tree": TREE}
    assert (
        build_remote_connection_qualification_evidence(
            fixture, profile=profile
        ).observation_model_id
        == MODEL
    )
    for field, value in (
        ("request_bytes", 0),
        ("http_status", 401),
        ("outcome_id", PromptModelOutcomeId.AUTHENTICATION.value),
    ):
        invalid = {**fixture, "receipt": {**fixture["receipt"], field: value}}
        if field == "outcome_id":
            invalid[field] = value
        with pytest.raises(PromptModelContractError):
            build_remote_connection_qualification_evidence(invalid, profile=profile)
    with pytest.raises(PromptModelContractError):
        build_remote_connection_qualification_evidence(
            {**fixture, "schema_valid": True}, profile=profile
        )


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "h3.prompt_model.remote_qualification.v1"},
        {"status": "FAIL"},
        {"profile_id": "anthropic.remote"},
        {"provider_id": "google_gemini"},
        {"adapter_version": "0.0.0"},
        {"parser_version": "0.0.0"},
        {"policy_sha256": "sha256:" + "0" * 64},
        {"max_transmissions": True},
        {"observed_transmissions": 1},
        {"observed_transmissions": 5},
        {"observed_transmissions": True},
        {"repository_commit": None},
        {"repository_tree": "x" * 40},
        {"observed_on": "2026-99-99"},
        {"model_id": "../unsafe"},
        {"model_id": "a..b"},
        {"basis": "reachability"},
        {"schema_valid": 1},
        {"receipt_valid": False},
        {"catalog_promotion_performed": True},
        {"price_basis_id": "historical"},
        {"evidence_basis_sha256": "sha256:" + "0" * 64},
        {"prompt": "forbidden"},
    ],
)
def test_current_reducer_refuses_legacy_spliced_or_incomplete_reports(
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    report = live_shaped_fixture(tmp_path / "fixture.json")
    with pytest.raises(PromptModelContractError):
        build_remote_connection_qualification_evidence(
            {**report, **change}, profile=load_prompt_model_catalog().require("openai.remote")
        )


@pytest.mark.parametrize(
    "change",
    [
        {"model_id": "foreign"},
        {"http_status": True},
        {"request_bytes": 0},
        {"response_bytes": -1},
        {"duration_ms": False},
        {"completion_tokens": 2**31},
        {"usage_present": False},
        {"price_basis_id": "legacy"},
        {"host": "private"},
    ],
)
def test_current_reducer_refuses_foreign_or_non_content_free_receipts(
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    report = live_shaped_fixture(tmp_path / "fixture.json")
    receipt = report["receipt"]
    assert isinstance(receipt, Mapping)
    report["receipt"] = {**receipt, **change}
    with pytest.raises(PromptModelContractError):
        build_remote_connection_qualification_evidence(
            report, profile=load_prompt_model_catalog().require("openai.remote")
        )


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--authorize-policy-sha256", "sha256:" + "0" * 64),
        ("--authorize-max-transmissions", "3"),
        ("--candidate-commit", "c" * 39),
        ("--candidate-tree", "b" * 39),
        ("--model", "../unsafe"),
    ],
)
def test_live_invalid_input_refuses_before_key_or_exchange(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    argv = arguments(tmp_path / "report.json", mode="live")
    argv[argv.index(flag) + 1] = value
    with (
        patch.object(tool, "RemoteHttpsExchange", side_effect=AssertionError("socket opened")),
        pytest.raises(SystemExit),
    ):
        tool.main(argv, credential_reader=no_key, candidate_identity_reader=no_candidate)
    assert not (tmp_path / "report.json").exists()


def test_live_candidate_drift_refuses_before_key(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        tool.main(
            arguments(tmp_path / "report.json", mode="live"),
            credential_reader=no_key,
            candidate_identity_reader=lambda: ("c" * 40, TREE),
        )


def test_request_budget_refuses_before_key_or_candidate(tmp_path: Path) -> None:
    with (
        patch.object(tool, "_request", side_effect=PromptModelContractError("request_plan")),
        pytest.raises(SystemExit),
    ):
        tool.main(
            arguments(tmp_path / "report.json", mode="live"),
            credential_reader=no_key,
            candidate_identity_reader=no_candidate,
        )


def test_live_injected_transport_refuses_before_key_or_candidate(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        tool.main(
            arguments(tmp_path / "report.json", mode="live"),
            credential_reader=no_key,
            candidate_identity_reader=no_candidate,
            exchange_factory=lambda *_: pytest.fail("sent"),
        )


def test_hermetic_cannot_borrow_live_authority(tmp_path: Path) -> None:
    argv = arguments(tmp_path / "report.json", mode="live")
    argv[argv.index("--mode") + 1] = "hermetic"
    with pytest.raises(SystemExit):
        tool.main(argv, credential_reader=no_key, candidate_identity_reader=no_candidate)


def test_existing_output_and_late_creation_never_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("immutable", encoding="utf-8")
    with pytest.raises(SystemExit):
        tool.main(
            arguments(path, mode="live"),
            credential_reader=no_key,
            candidate_identity_reader=no_candidate,
        )
    with patch.object(Path, "exists", return_value=False), pytest.raises(SystemExit):
        tool._write(path, {"status": "PASS"})
    assert path.read_text(encoding="utf-8") == "immutable"


@pytest.mark.parametrize(
    "value",
    [
        '{"schema":"h3.prompt_model.draft_json.v1","prompt_text":"a","prompt_text":"b"}',
        '{"schema":"h3.prompt_model.draft_json.v1","prompt_text":"ok","secret":"x"}',
        '```json\n{"schema":"h3.prompt_model.draft_json.v1","prompt_text":"ok"}\n```',
        '{"schema":"h3.prompt_model.draft_json.v1","prompt_text":" "}',
    ],
)
def test_closed_draft_rejects_duplicate_extra_fenced_or_empty_content(value: str) -> None:
    assert not tool._closed_draft(value)
