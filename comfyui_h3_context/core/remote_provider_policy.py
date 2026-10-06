"""Closed provider-connection protocols and finite wire limits.

Model choice comes from server-owned discovery. No price, billing or cost authority participates
in permission. Historical pinned rows remain readable but cannot qualify the new connections.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from hashlib import sha256
from typing import NoReturn

from .prompt_model_budget import OUTPUT_SAFETY_MARGIN_FLOOR_TOKENS, OUTPUT_SAFETY_MARGIN_PERCENT
from .prompt_model_provider import (
    PROMPT_MODEL_CATALOG_PATH,
    ConnectionQualificationEvidence,
    LegacyPromptModelProfile,
    PromptModelContractError,
    PromptModelDialect,
    PromptModelFamily,
    PromptModelMediaKind,
    PromptModelProfile,
    PromptModelQualificationState,
    decode_prompt_model_catalog,
)

REMOTE_PROVIDER_POLICY_SCHEMA = "h3.remote.prompt_model.policy.v2"

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


class RemoteCredentialScheme(str, Enum):
    """Credential header construction selected by the declared provider dialect."""

    BEARER = "bearer"
    X_API_KEY = "x-api-key"  # pragma: allowlist secret -- header scheme name, never a key
    X_GOOG_API_KEY = "x-goog-api-key"  # pragma: allowlist secret -- header scheme name


@dataclass(frozen=True, slots=True)
class _RemoteProviderProtocolBinding:
    family: PromptModelFamily
    dialect: PromptModelDialect
    provider_id: str
    origin: str
    discovery_route: str
    chat_route: str
    extra_headers: tuple[tuple[str, str], ...]
    reasoning_effort: str
    store: bool | None
    credential_scheme: RemoteCredentialScheme
    api_version: str | None
    discovery_credential_scheme: RemoteCredentialScheme


_PROTOCOL_BY_PROFILE = {
    "openai.remote": _RemoteProviderProtocolBinding(
        family=PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        dialect=PromptModelDialect.OPENAI_CHAT_COMPLETIONS,
        provider_id="openai",
        origin="https://api.openai.com",
        discovery_route="/v1/models",
        chat_route="/v1/chat/completions",
        extra_headers=(),
        reasoning_effort="none",
        store=False,
        credential_scheme=RemoteCredentialScheme.BEARER,
        api_version=None,
        discovery_credential_scheme=RemoteCredentialScheme.BEARER,
    ),
    "gemini.remote": _RemoteProviderProtocolBinding(
        family=PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        dialect=PromptModelDialect.OPENAI_CHAT_COMPLETIONS,
        provider_id="google_gemini",
        origin="https://generativelanguage.googleapis.com",
        discovery_route="/v1beta/models?pageSize=1000",
        chat_route="/v1beta/openai/chat/completions",
        extra_headers=(("x-goog-api-client", "rookiestar-minimax-h3-context-oai/1.0.0"),),
        reasoning_effort="low",
        store=None,
        credential_scheme=RemoteCredentialScheme.BEARER,
        api_version=None,
        discovery_credential_scheme=RemoteCredentialScheme.X_GOOG_API_KEY,
    ),
    "anthropic.remote": _RemoteProviderProtocolBinding(
        family=PromptModelFamily.REMOTE_ANTHROPIC,
        dialect=PromptModelDialect.ANTHROPIC_MESSAGES,
        provider_id="anthropic",
        origin="https://api.anthropic.com",
        discovery_route="/v1/models?limit=1000",
        chat_route="/v1/messages",
        extra_headers=(),
        reasoning_effort="none",
        store=None,
        credential_scheme=RemoteCredentialScheme.X_API_KEY,
        api_version="2023-06-01",
        discovery_credential_scheme=RemoteCredentialScheme.X_API_KEY,
    ),
}


def _binding_for_profile(profile_id: str) -> _RemoteProviderProtocolBinding | None:
    current = _PROTOCOL_BY_PROFILE.get(profile_id)
    if current is not None:
        return current
    archived = next((row for row in _historical_profiles() if row.profile_id == profile_id), None)
    if not isinstance(archived, LegacyPromptModelProfile):
        return None
    current = next(
        (
            row
            for row in _PROTOCOL_BY_PROFILE.values()
            if row.origin == archived.endpoint
            and row.family is archived.family
            and row.dialect is archived.wire_dialect
        ),
        None,
    )
    if current is None or len(archived.discovery_routes) != 1 or not archived.chat_route:
        return None
    # SECURITY: only the sealed historical profile's exact protocol remains decodable. A
    # caller-provided old route cannot borrow the current connection's credential table.
    return replace(
        current,
        discovery_route=archived.discovery_routes[0],
        chat_route=archived.chat_route,
        discovery_credential_scheme=current.credential_scheme,
    )


@dataclass(frozen=True, slots=True)
class RemoteProviderPolicy:
    """One indivisible provider policy; callers may select it but cannot compose it."""

    profile_id: str
    provider_id: str
    policy_version: str
    origin: str
    discovery_route: str
    chat_route: str
    extra_headers: tuple[tuple[str, str], ...]
    reasoning_effort: str
    store: bool | None
    credential_scheme: RemoteCredentialScheme
    api_version: str | None
    max_transmissions: int = 4
    route_credentials: tuple[tuple[str, str, RemoteCredentialScheme], ...] = ()

    def __post_init__(self) -> None:
        identifiers = (
            self.profile_id,
            self.provider_id,
            self.policy_version,
        )
        if any(type(value) is not str or not value or len(value) > 128 for value in identifiers):
            _fail("remote_policy_identity")
        binding = _binding_for_profile(self.profile_id)
        if binding is None or (
            self.provider_id,
            self.origin,
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
        expected_credentials = (
            ("GET", binding.discovery_route, binding.discovery_credential_scheme),
            ("POST", binding.chat_route, binding.credential_scheme),
        )
        if (
            type(self.route_credentials) is not tuple
            or any(
                type(item) is not tuple
                or len(item) != 3
                or not isinstance(item[2], RemoteCredentialScheme)
                for item in self.route_credentials
            )
            or (self.route_credentials and self.route_credentials != expected_credentials)
        ):
            _fail("remote_policy_route_credentials")
        object.__setattr__(self, "route_credentials", expected_credentials)
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
            if type(route) is not str or not route.startswith("/") or ".." in route:
                _fail("remote_policy_route")
        if self.origin not in {
            "https://api.openai.com",
            "https://generativelanguage.googleapis.com",
            "https://api.anthropic.com",
        }:
            _fail("remote_policy_origin")
        _positive(self.max_transmissions, 4, "remote_policy_transmissions")

    @property
    def family(self) -> PromptModelFamily:
        binding = _binding_for_profile(self.profile_id)
        if binding is None:
            _fail("remote_policy_profile")
        return binding.family

    @property
    def wire_dialect(self) -> PromptModelDialect:
        binding = _binding_for_profile(self.profile_id)
        if binding is None:
            _fail("remote_policy_profile")
        return binding.dialect

    def credential_for_route(self, method: str, path: str) -> RemoteCredentialScheme:
        for admitted_method, admitted_path, scheme in self.route_credentials:
            if (method, path) == (admitted_method, admitted_path):
                return scheme
        _fail("remote_policy_route_credentials")

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
            "discovery_route": self.discovery_route,
            "chat_route": self.chat_route,
            "extra_headers": [[name, value] for name, value in self.extra_headers],
            "reasoning_effort": self.reasoning_effort,
            "store": self.store,
            "credential_scheme": self.credential_scheme.value,
            "api_version": self.api_version,
            "max_transmissions": self.max_transmissions,
            "route_credentials": [
                [method, path, scheme.value] for method, path, scheme in self.route_credentials
            ],
        }


_POLICIES = tuple(
    RemoteProviderPolicy(
        profile_id=profile_id,
        provider_id=binding.provider_id,
        policy_version="2.1.0",
        origin=binding.origin,
        discovery_route=binding.discovery_route,
        chat_route=binding.chat_route,
        extra_headers=binding.extra_headers,
        reasoning_effort=binding.reasoning_effort,
        store=binding.store,
        credential_scheme=binding.credential_scheme,
        api_version=binding.api_version,
    )
    for profile_id, binding in _PROTOCOL_BY_PROFILE.items()
)
_POLICY_BY_PROFILE = {policy.profile_id: policy for policy in _POLICIES}


@lru_cache(maxsize=1)
def _historical_profiles() -> tuple[PromptModelProfile, ...]:
    """Decode sealed v5 only for historical direct-call compatibility, never the Settings loader."""
    source = PROMPT_MODEL_CATALOG_PATH.with_name("prompt_model_profiles_v5.json")
    return decode_prompt_model_catalog(source.read_bytes()).profiles


def remote_provider_policies() -> tuple[RemoteProviderPolicy, ...]:
    """Return the closed curated inventory in deterministic catalog order."""

    return _POLICIES


def policy_for_profile(profile: object) -> RemoteProviderPolicy:
    """Return a policy only when every catalog-owned execution field matches exactly."""

    if not isinstance(profile, PromptModelProfile):
        _fail("remote_policy_profile")
    policy = _POLICY_BY_PROFILE.get(profile.profile_id)
    historical = isinstance(profile, LegacyPromptModelProfile)
    if isinstance(profile, LegacyPromptModelProfile):
        archived = next(
            (row for row in _historical_profiles() if row.profile_id == profile.profile_id), None
        )
        if not isinstance(archived, LegacyPromptModelProfile) or (
            profile.model_id != archived.model_id
            or profile.model_digest != archived.model_digest
            or profile.model_revision != archived.model_revision
            or profile.endpoint != archived.endpoint
        ):
            _fail("remote_policy_profile")
        binding = _binding_for_profile(profile.profile_id)
        if binding is None:
            _fail("remote_policy_profile")
        policy = RemoteProviderPolicy(
            profile_id=profile.profile_id,
            provider_id=binding.provider_id,
            policy_version="2.0.0",
            origin=binding.origin,
            discovery_route=binding.discovery_route,
            chat_route=binding.chat_route,
            extra_headers=binding.extra_headers,
            reasoning_effort=binding.reasoning_effort,
            store=binding.store,
            credential_scheme=binding.credential_scheme,
            api_version=binding.api_version,
            max_transmissions=profile.max_calls_per_action,
        )
    if policy is None:
        _fail("remote_policy_profile")
    if historical:
        policy = replace(policy, max_transmissions=profile.max_calls_per_action)
    expected_family = policy.family
    expected_dialect = policy.wire_dialect
    expected = (
        profile.family is expected_family
        and profile.wire_dialect is expected_dialect
        and profile.endpoint == policy.origin
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
        if not isinstance(evidence, ConnectionQualificationEvidence) or (
            evidence.profile_id != profile.profile_id
            or evidence.family is not expected_family
            or evidence.adapter_version != profile.adapter_version
            or evidence.parser_version != profile.parser_version
            or evidence.max_transmissions != policy.max_transmissions
        ):
            # SECURITY: historical model evidence cannot qualify a new connection authority.
            _fail("remote_policy_qualification")
    return policy


def remote_output_tokens_from_plan(policy: object, reserved_output_tokens: object) -> int:
    """Recover the requested completion from the shared plan's technical safety reserve."""

    if not isinstance(policy, RemoteProviderPolicy):
        _fail("remote_output_policy")
    reserved = _positive(reserved_output_tokens, 1 << 24, "remote_output_tokens")
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


__all__ = [
    "REMOTE_PROVIDER_POLICY_SCHEMA",
    "REMOTE_OUTPUT_SAFETY_MARGIN_TOKENS",
    "RemoteCredentialScheme",
    "RemoteProviderPolicy",
    "policy_for_profile",
    "remote_output_tokens_from_plan",
    "remote_provider_policies",
]
