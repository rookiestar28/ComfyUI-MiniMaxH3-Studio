"""Exact M22-14 policy and cost authority for curated remote prompt-model profiles.

The catalog says which profile exists.  This module says what that exact profile is allowed to do:
one official origin, two routes, a closed header vocabulary and one request dialect. Historical
pricing is compatibility metadata. The value is deliberately redundant with the catalog. A row
that drifts cannot silently inherit a policy merely because it belongs to the remote family.

Legacy cost helpers preserve content-free compatibility shapes without controlling provider access.
Current billing and cost management belong to the user and their provider account.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import Enum
from hashlib import sha256
from typing import NoReturn

from .prompt_model_budget import OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS, OUTPUT_SAFETY_MARGIN_PERCENT
from .prompt_model_provider import (
    PromptModelContractError,
    PromptModelDialect,
    PromptModelFamily,
    PromptModelMediaKind,
    PromptModelProfile,
    PromptModelQualificationState,
    RemotePromptModelQualificationEvidence,
)

REMOTE_PROVIDER_POLICY_SCHEMA = "h3.remote.prompt_model.policy.v1"
REMOTE_COST_AUTHORITY_SCHEMA = "h3.remote.prompt_model.cost_authority.v1"
REMOTE_COST_DECISION_SCHEMA = "h3.remote.prompt_model.cost_decision.v1"

MAX_REMOTE_INPUT_TOKENS = 512
MAX_REMOTE_OUTPUT_TOKENS = 128
MAX_REMOTE_COST_MICRO_USD = 10_000
MAX_REMOTE_PRICE_MICRO_USD_PER_MILLION = 100_000_000
# The shared budget planner reserves this safety floor beyond the caller's requested completion.
# Remote provider billing and wire limits bind the requested completion, not the local reserve.
REMOTE_OUTPUT_SAFETY_MARGIN_TOKENS = OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS


def _fail(code: str) -> NoReturn:
    raise PromptModelContractError(code)


def _positive(value: object, maximum: int, code: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _date(value: object, code: str) -> date:
    if type(value) is not date:
        _fail(code)
    return value


class RemoteCredentialScheme(str, Enum):
    """Credential header construction selected by the declared provider dialect."""

    BEARER = "bearer"
    X_API_KEY = "x-api-key"  # pragma: allowlist secret -- header scheme name, never a key


@dataclass(frozen=True, slots=True)
class _RemoteProviderProtocolBinding:
    family: PromptModelFamily
    dialect: PromptModelDialect
    provider_id: str
    origin: str
    model_id: str
    discovery_route: str
    chat_route: str
    extra_headers: tuple[tuple[str, str], ...]
    reasoning_effort: str
    store: bool | None
    credential_scheme: RemoteCredentialScheme
    api_version: str | None


_PROTOCOL_BY_PROFILE = {
    "openai.gpt_5_6_terra.remote": _RemoteProviderProtocolBinding(
        family=PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        dialect=PromptModelDialect.OPENAI_CHAT_COMPLETIONS,
        provider_id="openai",
        origin="https://api.openai.com",
        model_id="gpt-5.6-terra",
        discovery_route="/v1/models",
        chat_route="/v1/chat/completions",
        extra_headers=(),
        reasoning_effort="none",
        store=False,
        credential_scheme=RemoteCredentialScheme.BEARER,
        api_version=None,
    ),
    "gemini.gemini_3_7_flash.remote": _RemoteProviderProtocolBinding(
        family=PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        dialect=PromptModelDialect.OPENAI_CHAT_COMPLETIONS,
        provider_id="google_gemini",
        origin="https://generativelanguage.googleapis.com",
        model_id="gemini-3.7-flash",
        discovery_route="/v1beta/openai/models",
        chat_route="/v1beta/openai/chat/completions",
        extra_headers=(("x-goog-api-client", "rookiestar-minimax-h3-context-oai/1.0.0"),),
        reasoning_effort="low",
        store=None,
        credential_scheme=RemoteCredentialScheme.BEARER,
        api_version=None,
    ),
    "anthropic.claude_sonnet_4_6.remote": _RemoteProviderProtocolBinding(
        family=PromptModelFamily.REMOTE_ANTHROPIC,
        dialect=PromptModelDialect.ANTHROPIC_MESSAGES,
        provider_id="anthropic",
        origin="https://api.anthropic.com",
        model_id="claude-sonnet-4-6",
        discovery_route="/v1/models",
        chat_route="/v1/messages",
        extra_headers=(),
        reasoning_effort="none",
        store=None,
        credential_scheme=RemoteCredentialScheme.X_API_KEY,
        api_version="2023-06-01",
    ),
}


@dataclass(frozen=True, slots=True)
class RemoteProviderPolicy:
    """One indivisible provider policy; callers may select it but cannot compose it."""

    profile_id: str
    provider_id: str
    policy_version: str
    origin: str
    model_id: str
    discovery_route: str
    chat_route: str
    extra_headers: tuple[tuple[str, str], ...]
    reasoning_effort: str
    store: bool | None
    credential_scheme: RemoteCredentialScheme
    api_version: str | None
    input_price_micro_usd_per_million: int
    output_price_micro_usd_per_million: int
    price_basis_id: str
    price_checked_on: date
    price_valid_through: date
    max_transmissions: int = 2
    max_input_tokens: int = MAX_REMOTE_INPUT_TOKENS
    max_output_tokens: int = MAX_REMOTE_OUTPUT_TOKENS
    max_cost_micro_usd: int = MAX_REMOTE_COST_MICRO_USD

    def __post_init__(self) -> None:
        identifiers = (
            self.profile_id,
            self.provider_id,
            self.policy_version,
            self.model_id,
            self.price_basis_id,
        )
        if any(type(value) is not str or not value or len(value) > 128 for value in identifiers):
            _fail("remote_policy_identity")
        binding = _PROTOCOL_BY_PROFILE.get(self.profile_id)
        if binding is None or (
            self.provider_id,
            self.origin,
            self.model_id,
            self.discovery_route,
            self.chat_route,
            self.extra_headers,
            self.reasoning_effort,
            self.store,
            self.credential_scheme,
            self.api_version,
        ) != (
            binding.provider_id,
            binding.origin,
            binding.model_id,
            binding.discovery_route,
            binding.chat_route,
            binding.extra_headers,
            binding.reasoning_effort,
            binding.store,
            binding.credential_scheme,
            binding.api_version,
        ):
            # SECURITY: policy values are a closed protocol unit. A caller cannot splice one
            # provider's host, route, header or credential scheme into another profile.
            _fail("remote_policy_protocol")
        if self.reasoning_effort not in {"none", "low"}:
            _fail("remote_policy_reasoning")
        if self.store not in {None, False}:
            _fail("remote_policy_storage")
        if not isinstance(self.credential_scheme, RemoteCredentialScheme):
            _fail("remote_policy_credential_scheme")
        if self.credential_scheme is RemoteCredentialScheme.X_API_KEY:
            if self.api_version != "2023-06-01":
                _fail("remote_policy_api_version")
        elif self.api_version is not None:
            _fail("remote_policy_api_version")
        if type(self.extra_headers) is not tuple or len(self.extra_headers) > 1:
            _fail("remote_policy_headers")
        names: set[str] = set()
        for item in self.extra_headers:
            if type(item) is not tuple or len(item) != 2:
                _fail("remote_policy_headers")
            name, value = item
            if (
                name != "x-goog-api-client"
                or name in names
                or type(value) is not str
                or not value
                or len(value) > 128
                or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
            ):
                _fail("remote_policy_headers")
            names.add(name)
        for route in (self.discovery_route, self.chat_route):
            if type(route) is not str or not route.startswith("/") or ".." in route or "?" in route:
                _fail("remote_policy_route")
        if self.origin not in {
            "https://api.openai.com",
            "https://generativelanguage.googleapis.com",
            "https://api.anthropic.com",
        }:
            _fail("remote_policy_origin")
        _positive(
            self.input_price_micro_usd_per_million,
            MAX_REMOTE_PRICE_MICRO_USD_PER_MILLION,
            "remote_policy_price",
        )
        _positive(
            self.output_price_micro_usd_per_million,
            MAX_REMOTE_PRICE_MICRO_USD_PER_MILLION,
            "remote_policy_price",
        )
        _positive(self.max_input_tokens, MAX_REMOTE_INPUT_TOKENS, "remote_policy_tokens")
        _positive(self.max_output_tokens, MAX_REMOTE_OUTPUT_TOKENS, "remote_policy_tokens")
        _positive(self.max_cost_micro_usd, MAX_REMOTE_COST_MICRO_USD, "remote_policy_cost")
        if self.max_transmissions != 2:
            _fail("remote_policy_transmissions")
        checked = _date(self.price_checked_on, "remote_policy_date")
        valid = _date(self.price_valid_through, "remote_policy_date")
        if valid < checked:
            _fail("remote_policy_date")

    @property
    def family(self) -> PromptModelFamily:
        return _PROTOCOL_BY_PROFILE[self.profile_id].family

    @property
    def wire_dialect(self) -> PromptModelDialect:
        return _PROTOCOL_BY_PROFILE[self.profile_id].dialect

    @property
    def fingerprint(self) -> str:
        material = json.dumps(
            self.to_wire(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + sha256(material).hexdigest()

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REMOTE_PROVIDER_POLICY_SCHEMA,
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "family": self.family.value,
            "wire_dialect": self.wire_dialect.value,
            "policy_version": self.policy_version,
            "origin": self.origin,
            "model_id": self.model_id,
            "discovery_route": self.discovery_route,
            "chat_route": self.chat_route,
            "extra_headers": [[name, value] for name, value in self.extra_headers],
            "reasoning_effort": self.reasoning_effort,
            "store": self.store,
            "credential_scheme": self.credential_scheme.value,
            "api_version": self.api_version,
            "input_price_micro_usd_per_million": self.input_price_micro_usd_per_million,
            "output_price_micro_usd_per_million": self.output_price_micro_usd_per_million,
            "price_basis_id": self.price_basis_id,
            "price_checked_on": self.price_checked_on.isoformat(),
            "price_valid_through": self.price_valid_through.isoformat(),
            "max_transmissions": self.max_transmissions,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_micro_usd": self.max_cost_micro_usd,
        }


_POLICIES = (
    RemoteProviderPolicy(
        profile_id="openai.gpt_5_6_terra.remote",
        provider_id="openai",
        policy_version="1.0.0",
        origin="https://api.openai.com",
        model_id="gpt-5.6-terra",
        discovery_route="/v1/models",
        chat_route="/v1/chat/completions",
        extra_headers=(),
        reasoning_effort="none",
        store=False,
        credential_scheme=RemoteCredentialScheme.BEARER,
        api_version=None,
        input_price_micro_usd_per_million=2_000_000,
        output_price_micro_usd_per_million=12_000_000,
        price_basis_id="openai.gpt-5.6-terra.standard.2026-08-23",
        price_checked_on=date(2026, 8, 23),
        price_valid_through=date(2026, 9, 22),
    ),
    RemoteProviderPolicy(
        profile_id="gemini.gemini_3_7_flash.remote",
        provider_id="google_gemini",
        policy_version="1.0.0",
        origin="https://generativelanguage.googleapis.com",
        model_id="gemini-3.7-flash",
        discovery_route="/v1beta/openai/models",
        chat_route="/v1beta/openai/chat/completions",
        extra_headers=(("x-goog-api-client", "rookiestar-minimax-h3-context-oai/1.0.0"),),
        reasoning_effort="low",
        store=None,
        credential_scheme=RemoteCredentialScheme.BEARER,
        api_version=None,
        input_price_micro_usd_per_million=750_000,
        output_price_micro_usd_per_million=3_750_000,
        price_basis_id="google.gemini-3.7-flash.standard.2026-08-23",
        price_checked_on=date(2026, 8, 23),
        price_valid_through=date(2026, 9, 22),
    ),
    RemoteProviderPolicy(
        profile_id="anthropic.claude_sonnet_4_6.remote",
        provider_id="anthropic",
        policy_version="1.0.0",
        origin="https://api.anthropic.com",
        model_id="claude-sonnet-4-6",
        discovery_route="/v1/models",
        chat_route="/v1/messages",
        extra_headers=(),
        reasoning_effort="none",
        store=None,
        credential_scheme=RemoteCredentialScheme.X_API_KEY,
        api_version="2023-06-01",
        input_price_micro_usd_per_million=3_000_000,
        output_price_micro_usd_per_million=15_000_000,
        price_basis_id="anthropic.claude-sonnet-4-6.standard.2026-08-23",
        price_checked_on=date(2026, 8, 23),
        price_valid_through=date(2026, 9, 22),
    ),
)

_POLICY_BY_PROFILE = {policy.profile_id: policy for policy in _POLICIES}


def remote_provider_policies() -> tuple[RemoteProviderPolicy, ...]:
    """Return the closed curated inventory in deterministic catalog order."""

    return _POLICIES


def policy_for_profile(profile: object) -> RemoteProviderPolicy:
    """Return a policy only when every catalog-owned execution field matches exactly."""

    if not isinstance(profile, PromptModelProfile):
        _fail("remote_policy_profile")
    policy = _POLICY_BY_PROFILE.get(profile.profile_id)
    if policy is None:
        _fail("remote_policy_profile")
    expected_family = policy.family
    expected_dialect = policy.wire_dialect
    expected = (
        profile.family is expected_family
        and profile.wire_dialect is expected_dialect
        and profile.endpoint == policy.origin
        and profile.model_id == policy.model_id
        and profile.model_revision == policy.model_id
        and profile.discovery_routes == (policy.discovery_route,)
        and profile.chat_route == policy.chat_route
        and profile.capabilities.family is expected_family
        and profile.capabilities.accepted_media == frozenset({PromptModelMediaKind.TEXT})
        and not profile.capabilities.streaming
        and not profile.capabilities.local_only
        and profile.capabilities.requires_credential
        and profile.cost_class == "paid_remote"
        and profile.usage_receipt_required
        and profile.max_retries == 0
        and profile.max_concurrency == 1
        and profile.max_calls_per_action == policy.max_transmissions
    )
    if not expected:
        # SECURITY: a profile id is not authority to borrow another row's origin, route or model.
        _fail("remote_policy_mismatch")
    if profile.qualification_state is PromptModelQualificationState.QUALIFIED:
        evidence = profile.qualification_evidence
        if not isinstance(evidence, RemotePromptModelQualificationEvidence) or (
            evidence.provider_id != policy.provider_id
            or evidence.family is not expected_family
            or evidence.profile_id != policy.profile_id
            or evidence.model_id != policy.model_id
            or evidence.policy_version != policy.policy_version
            or evidence.policy_sha256 != policy.fingerprint
            or evidence.price_basis_id != policy.price_basis_id
            or evidence.source_checked_on != policy.price_checked_on.isoformat()
            or evidence.price_valid_through != policy.price_valid_through.isoformat()
            or evidence.max_transmissions != policy.max_transmissions
            or evidence.max_input_tokens != policy.max_input_tokens
            or evidence.max_output_tokens != policy.max_output_tokens
            or evidence.max_cost_micro_usd != policy.max_cost_micro_usd
            or evidence.adapter_version != profile.adapter_version
            or evidence.parser_version != profile.parser_version
        ):
            # SECURITY: a syntactically valid live claim cannot authorize a later policy revision.
            _fail("remote_policy_qualification")
    return policy


@dataclass(frozen=True, slots=True)
class RemoteCostAuthority:
    """One session-scoped acknowledgement of the exact displayed policy ceilings."""

    profile_id: str
    policy_sha256: str
    price_basis_id: str
    max_input_tokens: int
    max_output_tokens: int
    max_cost_micro_usd: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value or len(value) > 128
            for value in (self.profile_id, self.policy_sha256, self.price_basis_id)
        ):
            _fail("remote_cost_authority")
        if not self.policy_sha256.startswith("sha256:") or len(self.policy_sha256) != 71:
            _fail("remote_cost_authority")
        _positive(self.max_input_tokens, MAX_REMOTE_INPUT_TOKENS, "remote_cost_authority")
        _positive(self.max_output_tokens, MAX_REMOTE_OUTPUT_TOKENS, "remote_cost_authority")
        _positive(self.max_cost_micro_usd, MAX_REMOTE_COST_MICRO_USD, "remote_cost_authority")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REMOTE_COST_AUTHORITY_SCHEMA,
            "profile_id": self.profile_id,
            "policy_sha256": self.policy_sha256,
            "price_basis_id": self.price_basis_id,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_micro_usd": self.max_cost_micro_usd,
        }


def build_remote_cost_authority(policy: object, *, on_date: object) -> RemoteCostAuthority:
    """Build historical cost metadata for compatibility, without authorizing billing."""

    if not isinstance(policy, RemoteProviderPolicy):
        _fail("remote_cost_policy")
    _date(on_date, "remote_cost_date")
    return RemoteCostAuthority(
        profile_id=policy.profile_id,
        policy_sha256=policy.fingerprint,
        price_basis_id=policy.price_basis_id,
        max_input_tokens=policy.max_input_tokens,
        max_output_tokens=policy.max_output_tokens,
        max_cost_micro_usd=policy.max_cost_micro_usd,
    )


class RemoteCostRejection(str, Enum):
    AUTHORITY_MISSING = "authority_missing"
    AUTHORITY_MISMATCH = "authority_mismatch"
    PRICE_STALE = "price_stale"
    INPUT_TOKENS_EXCEEDED = "input_tokens_exceeded"
    OUTPUT_TOKENS_EXCEEDED = "output_tokens_exceeded"
    COST_EXCEEDED = "cost_exceeded"


@dataclass(frozen=True, slots=True)
class RemoteCostDecision:
    profile_id: str
    price_basis_id: str
    maximum_cost_micro_usd: int
    rejection: RemoteCostRejection | None

    @property
    def admitted(self) -> bool:
        return self.rejection is None

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REMOTE_COST_DECISION_SCHEMA,
            "profile_id": self.profile_id,
            "price_basis_id": self.price_basis_id,
            "maximum_cost_micro_usd": self.maximum_cost_micro_usd,
            "rejection": None if self.rejection is None else self.rejection.value,
        }


def _cost_micro_usd(policy: RemoteProviderPolicy, input_tokens: int, output_tokens: int) -> int:
    numerator = (
        input_tokens * policy.input_price_micro_usd_per_million
        + output_tokens * policy.output_price_micro_usd_per_million
    )
    return -(-numerator // 1_000_000)


def remote_cost_micro_usd(policy: object, *, input_tokens: object, output_tokens: object) -> int:
    """Price observed integer usage conservatively without accepting content or floating point."""

    if not isinstance(policy, RemoteProviderPolicy):
        _fail("remote_cost_policy")
    admitted_input = _positive(input_tokens, 1 << 24, "remote_cost_tokens", allow_zero=True)
    admitted_output = _positive(output_tokens, 1 << 24, "remote_cost_tokens", allow_zero=True)
    return _cost_micro_usd(policy, admitted_input, admitted_output)


def remote_output_tokens_from_plan(policy: object, reserved_output_tokens: object) -> int:
    """Recover the requested completion from the shared plan's technical safety reserve."""

    if not isinstance(policy, RemoteProviderPolicy):
        _fail("remote_cost_policy")
    reserved = _positive(reserved_output_tokens, 1 << 24, "remote_cost_tokens")
    # IMPORTANT: the reserve uses max(floor, percentage). Subtracting only the floor sends
    # excess output tokens once requests exceed that window; invert both branches exactly.
    scale = 100 + OUTPUT_SAFETY_MARGIN_PERCENT
    requested = min(
        reserved - REMOTE_OUTPUT_SAFETY_MARGIN_TOKENS,
        (reserved * 100 + scale - 1) // scale,
    )
    margin = max(
        REMOTE_OUTPUT_SAFETY_MARGIN_TOKENS,
        requested * OUTPUT_SAFETY_MARGIN_PERCENT // 100,
    )
    if requested < 1 or requested + margin != reserved:
        _fail("remote_output_tokens")
    return requested


def admit_remote_cost(
    policy: object,
    authority: object,
    *,
    estimated_input_tokens: object,
    reserved_output_tokens: object,
    on_date: object,
) -> RemoteCostDecision:
    """Accept legacy cost arguments without imposing a repository billing policy."""

    if not isinstance(policy, RemoteProviderPolicy):
        _fail("remote_cost_policy")
    _positive(estimated_input_tokens, 1 << 24, "remote_cost_tokens", allow_zero=True)
    _positive(reserved_output_tokens, 1 << 24, "remote_cost_tokens")
    return admit_remote_cost_authority(policy, authority, on_date=on_date)


def admit_remote_cost_authority(
    policy: object,
    authority: object,
    *,
    on_date: object,
) -> RemoteCostDecision:
    """Retain the legacy decision shape without enforcing price or cost authority."""

    if not isinstance(policy, RemoteProviderPolicy):
        _fail("remote_cost_policy")
    _date(on_date, "remote_cost_date")
    # IMPORTANT: historical prices and authority fields are compatibility data. Reintroducing
    # admission here would make provider access depend on maintainer-managed billing metadata.
    return RemoteCostDecision(
        profile_id=policy.profile_id,
        price_basis_id="",
        maximum_cost_micro_usd=0,
        rejection=None,
    )


__all__ = [
    "MAX_REMOTE_COST_MICRO_USD",
    "MAX_REMOTE_INPUT_TOKENS",
    "MAX_REMOTE_OUTPUT_TOKENS",
    "REMOTE_COST_AUTHORITY_SCHEMA",
    "REMOTE_COST_DECISION_SCHEMA",
    "REMOTE_PROVIDER_POLICY_SCHEMA",
    "REMOTE_OUTPUT_SAFETY_MARGIN_TOKENS",
    "RemoteCostAuthority",
    "RemoteCostDecision",
    "RemoteCostRejection",
    "RemoteProviderPolicy",
    "admit_remote_cost",
    "admit_remote_cost_authority",
    "build_remote_cost_authority",
    "policy_for_profile",
    "remote_cost_micro_usd",
    "remote_output_tokens_from_plan",
    "remote_provider_policies",
]
