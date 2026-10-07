"""Sealed catalogue fixtures for backward-decoding and legacy direct-call regressions.

These helpers never replace the product's current catalogue loader. New connection authority is
tested independently against v6; old qualification must not mint that authority.
"""

from dataclasses import dataclass, replace
from datetime import date
from types import SimpleNamespace

from comfyui_h3_context.core.prompt_model_provider import (
    PROMPT_MODEL_CATALOG_PATH,
    LegacyPromptModelProfile,
    PromptModelCatalog,
    PromptModelContractError,
    PromptModelFamily,
    PromptModelQualificationState,
    RemotePromptModelQualificationEvidence,
    decode_prompt_model_catalog,
)


@dataclass(frozen=True, slots=True)
class HistoricalCatalogFixture(PromptModelCatalog):
    profiles: tuple[LegacyPromptModelProfile, ...]

    def require(self, profile_id: object) -> LegacyPromptModelProfile:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        raise PromptModelContractError("unknown_profile")


def load_historical_catalog() -> HistoricalCatalogFixture:
    decoded = decode_prompt_model_catalog(
        PROMPT_MODEL_CATALOG_PATH.with_name("prompt_model_profiles_v5.json").read_bytes()
    )
    rows: list[LegacyPromptModelProfile] = []
    for profile in decoded.profiles:
        if not isinstance(profile, LegacyPromptModelProfile):
            raise PromptModelContractError("historical_fixture_schema")
        rows.append(profile)
    return HistoricalCatalogFixture(schema=decoded.schema, profiles=tuple(rows))


def load_historical_direct_call_catalog() -> HistoricalCatalogFixture:
    catalog = load_historical_catalog()
    # Historical remote qualification is decode-only. Compatibility calls use explicit consent
    # and live identity checks; they cannot inherit the old model qualification as new authority.
    return replace(
        catalog,
        profiles=tuple(
            replace(
                row,
                qualification_state=PromptModelQualificationState.CATALOG_ONLY,
                qualification_evidence=None,
            )
            if row.family
            in {PromptModelFamily.REMOTE_OPENAI_COMPATIBLE, PromptModelFamily.REMOTE_ANTHROPIC}
            else row
            for row in catalog.profiles
        ),
    )


def historical_remote_evidence(profile_id: str) -> RemotePromptModelQualificationEvidence:
    row = load_historical_catalog().require(profile_id)
    assert isinstance(row, LegacyPromptModelProfile)
    assert isinstance(row.qualification_evidence, RemotePromptModelQualificationEvidence)
    return row.qualification_evidence


def historical_policy_facts(profile: LegacyPromptModelProfile) -> SimpleNamespace:
    """Archived facts for strict legacy evidence decoding, never a transport policy."""
    evidence = historical_remote_evidence(profile.profile_id)
    return SimpleNamespace(
        provider_id=evidence.provider_id,
        model_id=evidence.model_id,
        policy_version=evidence.policy_version,
        fingerprint=evidence.policy_sha256,
        price_basis_id=evidence.price_basis_id,
        price_checked_on=date.fromisoformat(evidence.source_checked_on),
        price_valid_through=date.fromisoformat(evidence.price_valid_through),
        max_transmissions=evidence.max_transmissions,
        max_input_tokens=evidence.max_input_tokens,
        max_output_tokens=evidence.max_output_tokens,
        max_cost_micro_usd=evidence.max_cost_micro_usd,
    )
