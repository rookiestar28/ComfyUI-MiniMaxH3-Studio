"""Model-independent provider protocol and technical wire-bound regressions."""

from dataclasses import fields, replace

import pytest

from comfyui_h3_context.core.prompt_model_budget import PromptModelWorkload
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelContractError,
    PromptModelFamily,
    load_prompt_model_catalog,
)
from comfyui_h3_context.core.remote_prompt_model import RemoteConsentRecord
from comfyui_h3_context.core.remote_provider_policy import (
    policy_for_profile,
    remote_output_tokens_from_plan,
    remote_provider_policies,
)


def test_every_remote_connection_has_one_fixed_protocol_and_no_model_or_billing_authority() -> None:
    profiles = tuple(
        row
        for row in load_prompt_model_catalog().profiles
        if row.family is not PromptModelFamily.OLLAMA
    )
    policies = remote_provider_policies()
    assert tuple(row.profile_id for row in profiles) == tuple(row.profile_id for row in policies)
    assert {(row.provider_id, row.profile_id) for row in policies} == {
        ("openai", "openai.remote"),
        ("google_gemini", "gemini.remote"),
        ("anthropic", "anthropic.remote"),
    }
    forbidden = {
        "model_id",
        "price_basis_id",
        "cost_authority",
        "max_cost_micro_usd",
        "price_valid_through",
    }
    assert forbidden.isdisjoint(field.name for field in fields(RemoteConsentRecord))
    for profile, policy in zip(profiles, policies, strict=True):
        assert policy_for_profile(profile) == policy
        assert forbidden.isdisjoint(policy.to_wire())
        assert policy.max_transmissions == 4


def test_protocol_binds_origin_routes_headers_and_credential_scheme_as_one_unit() -> None:
    openai, gemini, anthropic = remote_provider_policies()
    assert (openai.origin, openai.discovery_route, openai.chat_route) == (
        "https://api.openai.com",
        "/v1/models",
        "/v1/chat/completions",
    )
    assert openai.extra_headers == () and openai.store is False
    assert (gemini.origin, gemini.discovery_route, gemini.chat_route) == (
        "https://generativelanguage.googleapis.com",
        "/v1beta/models?pageSize=1000",
        "/v1beta/openai/chat/completions",
    )
    assert gemini.extra_headers == (
        ("x-goog-api-client", "rookiestar-minimax-h3-context-oai/1.0.0"),
    )
    assert gemini.store is None and anthropic.api_version == "2023-06-01"
    for drift in (
        {"origin": gemini.origin},
        {"chat_route": anthropic.chat_route},
        {"credential_scheme": anthropic.credential_scheme},
        {"extra_headers": gemini.extra_headers},
    ):
        with pytest.raises(PromptModelContractError):
            replace(openai, **drift)


def test_connection_cannot_splice_another_providers_protocol() -> None:
    profiles = {row.profile_id: row for row in load_prompt_model_catalog().profiles}
    openai, gemini = profiles["openai.remote"], profiles["gemini.remote"]
    for drift in (
        {"endpoint": gemini.endpoint},
        {"discovery_routes": gemini.discovery_routes},
        {"chat_route": gemini.chat_route},
        {"max_calls_per_action": 2},
    ):
        with pytest.raises(PromptModelContractError):
            policy_for_profile(replace(openai, **drift))


@pytest.mark.parametrize("tokens", [1, 128, 320, 321, 511, 512, 2_000])
def test_requested_output_is_recovered_across_both_safety_margin_branches(tokens: int) -> None:
    workload = PromptModelWorkload(
        characters=1,
        wide_characters=0,
        messages=1,
        visual_inputs=0,
        request_bytes=1,
        requested_output_tokens=tokens,
    )
    assert (
        remote_output_tokens_from_plan(
            remote_provider_policies()[0], workload.reserved_output_tokens
        )
        == tokens
    )


@pytest.mark.parametrize("reserved", [1, 623, True])
def test_invalid_output_reserves_fail_closed(reserved: object) -> None:
    with pytest.raises(PromptModelContractError):
        remote_output_tokens_from_plan(remote_provider_policies()[0], reserved)


def test_fingerprint_changes_with_wire_bound_and_cannot_contain_content() -> None:
    policy = remote_provider_policies()[0]
    assert replace(policy, max_transmissions=3).fingerprint != policy.fingerprint
    assert {"Authorization", "Bearer", "prompt_text", "response_text", "credential"}.isdisjoint(
        policy.to_wire()
    )
