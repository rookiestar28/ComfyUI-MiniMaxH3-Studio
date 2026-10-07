"""Native discovery completeness and structured rate-limit regressions."""

from dataclasses import replace

import pytest
from test_remote_prompt_model import StubConnection

from comfyui_h3_context.adapters.prompt_model_transport import (
    _STATUS_OUTCOMES,
    PromptModelTransportError,
    RemoteHttpsExchange,
)
from comfyui_h3_context.adapters.provider_readiness import _census
from comfyui_h3_context.core.prompt_model_provider import (
    ModelMetadata,
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelRemediation,
    admit_egress_destination,
    load_prompt_model_catalog,
    read_ollama_choice_metadata,
    remediation_for,
)
from comfyui_h3_context.core.remote_prompt_model import (
    RuntimeCredential,
    map_remote_outcome,
    scrub_upstream_error,
)
from comfyui_h3_context.core.remote_provider_policy import (
    RemoteCredentialScheme,
    policy_for_profile,
)


def test_anthropic_incomplete_page_cannot_become_choice_authority() -> None:
    listing = {"data": [{"id": f"native-model-{i}"} for i in range(25)], "has_more": True}
    with pytest.raises(PromptModelContractError):
        _census(
            PromptModelFamily.REMOTE_ANTHROPIC,
            listing,
            None,
            discovery_route="/v1/models?limit=1000",
        )


def test_loopback_unclassified_429_means_rate_not_quota() -> None:
    assert _STATUS_OUTCOMES[429] is PromptModelOutcomeId.RATE_LIMITED


def test_anthropic_full_page_publishes_explicit_capabilities_only() -> None:
    listing = {
        "has_more": False,
        "data": [
            {
                "id": "text-alpha",
                "display_name": "Text Alpha",
                "created_at": "2026-10-05T10:00:00Z",
                "max_input_tokens": 200000,
                "max_tokens": 64000,
                "capabilities": {
                    "structured_outputs": {"supported": False},
                    "thinking": {"supported": True},
                },
            }
        ],
    }
    rows = _census(
        PromptModelFamily.REMOTE_ANTHROPIC, listing, None, discovery_route="/v1/models?limit=1000"
    )
    assert len(rows) == 1 and rows[0].admitted
    facts = rows[0].metadata
    assert facts is not None and facts.display_name == "Text Alpha"
    assert facts.max_input_tokens == 200000 and facts.max_output_tokens == 64000
    assert facts.structured_output is False and facts.reasoning_mandatory is None
    assert facts.created is not None and facts.shutdown_date is None


def test_native_gemini_filters_text_and_preserves_limits_and_request_spelling() -> None:
    listing = {
        "models": [
            {
                "name": "models/arbitrary-text-latest",
                "supportedGenerationMethods": ["generateContent"],
                "inputTokenLimit": 65536,
                "outputTokenLimit": 2048,
                "thinking": False,
            },
            {"name": "models/arbitrary-embedding", "supportedGenerationMethods": ["embedContent"]},
        ]
    }
    rows = _census(
        PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        listing,
        None,
        discovery_route="/v1beta/models?pageSize=1000",
    )
    assert len(rows) == 1 and rows[0].identifier == "arbitrary-text-latest"
    assert rows[0].metadata == ModelMetadata(
        max_input_tokens=65536,
        max_output_tokens=2048,
        reasoning_mandatory=False,
        moving_alias=True,
        locality="remote",
    )


@pytest.mark.parametrize("count", [513, 1000])
def test_raw_bound_precedes_gemini_text_filtering(count: int) -> None:
    listing = {
        "models": [
            {"name": f"models/embedding-{i}", "supportedGenerationMethods": ["embedContent"]}
            for i in range(count)
        ]
    }
    with pytest.raises(PromptModelContractError):
        _census(
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            listing,
            None,
            discovery_route="/v1beta/models?pageSize=1000",
        )


def test_gemini_continuation_never_publishes_a_partial_census() -> None:
    with pytest.raises(PromptModelContractError):
        _census(
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            {"models": [], "nextPageToken": "another-page"},
            None,
            discovery_route="/v1beta/models?pageSize=1000",
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"max_tokens": True},
        {"max_input_tokens": -1},
        {"display_name": "x" * 129},
        {"display_name": "control\x00"},
        {"capabilities": {"structured_outputs": {"supported": 1}}},
        {"created_at": "2026-10-06T12:00:00"},
    ],
)
def test_malformed_native_row_refuses_whole_census(patch: dict[str, object]) -> None:
    listing = {"data": [{"id": "valid-model"}, {"id": "bad-model", **patch}], "has_more": False}
    with pytest.raises(PromptModelContractError):
        _census(
            PromptModelFamily.REMOTE_ANTHROPIC,
            listing,
            None,
            discovery_route="/v1/models?limit=1000",
        )


def test_names_never_infer_reasoning_limits_or_structured_support() -> None:
    rows = _census(
        PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        {"data": [{"id": "reasoning-super-json-9000", "created": 10}]},
        None,
        discovery_route="/v1/models",
    )
    assert rows[0].metadata == ModelMetadata(created=10, locality="remote")


@pytest.mark.parametrize(
    "values, mandatory, control",
    [([False], False, True), ([True, False], False, True), (["low", "medium"], True, False)],
)
def test_native_thinking_controls_do_not_guess_boolean_support(
    values: list[object], mandatory: bool, control: bool
) -> None:
    facts = read_ollama_choice_metadata(
        {
            "capabilities": ["completion", "thinking"],
            "thinking": {"values": values},
            "details": {"family": "example", "parameter_size": "7B", "quantization_level": "Q4"},
            "model_info": {"example.context_length": 32768},
            "license": "Synthetic licence.",
        },
        "sha256:" + "b" * 64,
    )
    assert facts.reasoning_mandatory is mandatory and facts.reasoning_control_supported is control
    assert facts.context_length == 32768 and facts.family == "example"
    assert facts.license_sha256 is not None and "Synthetic licence" not in str(facts.to_wire())


def test_cloud_show_refuses_before_metadata_reduction() -> None:
    with pytest.raises(PromptModelContractError):
        read_ollama_choice_metadata(
            {"capabilities": ["completion"], "remote_host": "remote.invalid"}, "sha256:" + "b" * 64
        )


@pytest.mark.parametrize(
    "status, code, expected",
    [
        (429, "rate_limit_error", PromptModelOutcomeId.RATE_LIMITED),
        (429, "insufficient_quota", PromptModelOutcomeId.QUOTA),
        (429, "billing_error", PromptModelOutcomeId.QUOTA),
        (429, "unknown", PromptModelOutcomeId.RATE_LIMITED),
        (402, "invalid_request_error", PromptModelOutcomeId.QUOTA),
        (529, "overloaded_error", PromptModelOutcomeId.RATE_LIMITED),
        (404, "not_found_error", PromptModelOutcomeId.MODEL_MISSING),
    ],
)
def test_closed_error_classification_and_remediation(
    status: int, code: str, expected: PromptModelOutcomeId
) -> None:
    error = scrub_upstream_error(
        status, {"error": {"type": code, "message": "quota prose is not authority"}}
    )
    assert map_remote_outcome(error) is expected
    assert remediation_for(expected) is (
        PromptModelRemediation.SELECT_MODEL
        if expected is PromptModelOutcomeId.MODEL_MISSING
        else PromptModelRemediation.RETRY_LATER
    )


def test_google_native_listing_and_compatible_chat_use_separate_credentials() -> None:
    profile = load_prompt_model_catalog().require("gemini.remote")
    policy = policy_for_profile(profile)
    credential = RuntimeCredential("test-credential-" + "K" * 40)
    exchange = RemoteHttpsExchange(
        admit_egress_destination(profile.family, profile.endpoint),
        credential,
        policy=policy,
        connection_factory=lambda *_a, **_k: StubConnection(body={}),
    )
    exchange.request("GET", policy.discovery_route)
    assert (
        "x-goog-api-key" in StubConnection.last_headers
        and "Authorization" not in StubConnection.last_headers
    )
    exchange.request("POST", policy.chat_route, {"model": "synthetic-model"})
    assert (
        "Authorization" in StubConnection.last_headers
        and "x-goog-api-key" not in StubConnection.last_headers
    )
    before = exchange.transmission_count
    with pytest.raises(PromptModelTransportError):
        exchange.request("GET", "/v1beta/models?pageSize=1000&key=forbidden")
    assert exchange.transmission_count == before
    with pytest.raises(PromptModelContractError):
        replace(
            policy,
            route_credentials=(
                ("GET", policy.discovery_route, RemoteCredentialScheme.BEARER),
                ("POST", policy.chat_route, RemoteCredentialScheme.X_GOOG_API_KEY),
            ),
        )
