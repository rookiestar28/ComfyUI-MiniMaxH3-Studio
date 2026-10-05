"""Explicit provider selection, privacy, consent, and credential-resolution policy.

The policy boundary is deliberately offline and provider-implementation agnostic. It selects one
descriptor exactly, validates the declared policy, and resolves credentials only through an
injected runtime resolver. It never performs network, media, or provider I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from .contracts import ProviderIdentity, ValidationDiagnostic, ValidationSeverity
from .errors import ContractValidationError, SecurityPolicyError
from .providers import (
    CredentialRequirement,
    NetworkRequirement,
    PrivacyLocation,
    ProviderDescriptor,
)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SENSITIVE_REFERENCE_MARKERS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "path",
    "secret",
    "signed",
    "token",
    "url",
)
_REMOTE_IDENTITIES = frozenset({ProviderIdentity.REMOTE_CUSTOM, ProviderIdentity.OFFICIAL_MINIMAX})


class ProviderPrivacyMode(str, Enum):
    """Caller-visible privacy decision for one provider execution path."""

    LOCAL_ONLY = "local_only"
    EXPLICIT_REMOTE = "explicit_remote"


class ProviderRetentionPolicy(str, Enum):
    """Conservative disclosure of where provider input may remain after execution."""

    NONE = "none"
    LOCAL_RUNTIME = "local_runtime"
    PROVIDER_DEFINED = "provider_defined"
    NOT_DECLARED = "not_declared"


class ProviderCostPolicy(str, Enum):
    """Non-price-bearing cost implication shown before a route can execute."""

    NONE = "none"
    LOCAL_RUNTIME = "local_runtime"
    PROVIDER_PRICING = "provider_pricing"
    NOT_DECLARED = "not_declared"


class ProviderFallbackPolicy(str, Enum):
    """Fallback behavior is always explicit; no automatic route substitution is allowed."""

    NEVER_AUTOMATIC = "never_automatic"
    CALLER_SELECTED_ONLY = "caller_selected_only"


def _require_enum(value: object, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise ContractValidationError(f"{field_name} must be a {expected.__name__}")


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be a boolean")
    return value


def _require_reference(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise SecurityPolicyError(f"{field_name} must be a bounded non-secret reference")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_REFERENCE_MARKERS):
        raise SecurityPolicyError(f"{field_name} must not contain secret-like material")
    return value


@dataclass(frozen=True, slots=True)
class ProviderExecutionPolicy:
    """Serializable, non-secret policy for one explicitly selected provider."""

    provider: ProviderIdentity
    privacy_mode: ProviderPrivacyMode
    offline: bool = True
    network_allowed: bool = False
    upload_consent: bool = False
    credential_reference: str | None = None
    fallback_provider: ProviderIdentity | None = None

    def __post_init__(self) -> None:
        _require_enum(self.provider, ProviderIdentity, "provider")
        _require_enum(self.privacy_mode, ProviderPrivacyMode, "privacy_mode")
        _require_bool(self.offline, "offline")
        _require_bool(self.network_allowed, "network_allowed")
        _require_bool(self.upload_consent, "upload_consent")
        if self.credential_reference is not None:
            _require_reference(self.credential_reference, "credential_reference")
        if self.fallback_provider is not None:
            _require_enum(self.fallback_provider, ProviderIdentity, "fallback_provider")
            if self.fallback_provider is self.provider:
                raise SecurityPolicyError(
                    "fallback_provider must differ from the selected provider"
                )

    def to_wire(self) -> dict[str, object]:
        """Return only bounded workflow configuration; no resolved credential is representable."""

        return {
            "provider": self.provider.value,
            "privacy_mode": self.privacy_mode.value,
            "offline": self.offline,
            "network_allowed": self.network_allowed,
            "upload_consent": self.upload_consent,
            "credential_reference": self.credential_reference,
            "fallback_provider": (
                None if self.fallback_provider is None else self.fallback_provider.value
            ),
        }


@dataclass(frozen=True, slots=True)
class _ProviderDisclosureProfile:
    """Static, conservative metadata used when no executable descriptor is registered."""

    privacy_location: PrivacyLocation
    network_requirement: NetworkRequirement
    credential_requirement: CredentialRequirement
    retention_policy: ProviderRetentionPolicy
    cost_policy: ProviderCostPolicy
    provider_version: str


_DISCLOSURE_PROFILES: dict[ProviderIdentity, _ProviderDisclosureProfile] = {
    ProviderIdentity.MANUAL: _ProviderDisclosureProfile(
        PrivacyLocation.LOCAL,
        NetworkRequirement.NONE,
        CredentialRequirement.NONE,
        ProviderRetentionPolicy.NONE,
        ProviderCostPolicy.NONE,
        "manual-v1",
    ),
    ProviderIdentity.LOCAL: _ProviderDisclosureProfile(
        PrivacyLocation.LOCAL,
        NetworkRequirement.NONE,
        CredentialRequirement.NONE,
        ProviderRetentionPolicy.LOCAL_RUNTIME,
        ProviderCostPolicy.LOCAL_RUNTIME,
        "local-v1",
    ),
    ProviderIdentity.REMOTE_CUSTOM: _ProviderDisclosureProfile(
        PrivacyLocation.REMOTE,
        NetworkRequirement.INTERNET,
        CredentialRequirement.REQUIRED,
        ProviderRetentionPolicy.PROVIDER_DEFINED,
        ProviderCostPolicy.PROVIDER_PRICING,
        "not_declared",
    ),
    ProviderIdentity.OFFICIAL_MINIMAX: _ProviderDisclosureProfile(
        PrivacyLocation.REMOTE,
        NetworkRequirement.INTERNET,
        CredentialRequirement.REQUIRED,
        ProviderRetentionPolicy.PROVIDER_DEFINED,
        ProviderCostPolicy.PROVIDER_PRICING,
        "not_declared",
    ),
}


def _profile_for(provider: ProviderIdentity) -> _ProviderDisclosureProfile:
    try:
        return _DISCLOSURE_PROFILES[provider]
    except KeyError as exc:  # pragma: no cover - ProviderIdentity is a closed enum
        raise ContractValidationError("provider disclosure profile is unavailable") from exc


@dataclass(frozen=True, slots=True)
class ProviderConsentNotice:
    """Non-secret disclosure suitable for a confirmation UI or audit socket."""

    provider: ProviderIdentity
    privacy_location: PrivacyLocation
    network_required: bool
    media_upload_required: bool
    consent_granted: bool
    credential_runtime_only: bool
    fallback_provider: ProviderIdentity | None
    requires_explicit_consent: bool
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider, ProviderIdentity):
            raise ContractValidationError("consent notice provider must be a ProviderIdentity")
        if not isinstance(self.privacy_location, PrivacyLocation):
            raise ContractValidationError(
                "consent notice privacy_location must be a PrivacyLocation"
            )
        for value, field_name in (
            (self.network_required, "network_required"),
            (self.media_upload_required, "media_upload_required"),
            (self.consent_granted, "consent_granted"),
            (self.credential_runtime_only, "credential_runtime_only"),
            (self.requires_explicit_consent, "requires_explicit_consent"),
        ):
            if not isinstance(value, bool):
                raise ContractValidationError(f"{field_name} must be a boolean")
        if self.fallback_provider is not None and not isinstance(
            self.fallback_provider, ProviderIdentity
        ):
            raise ContractValidationError("consent notice fallback must be a ProviderIdentity")
        if not isinstance(self.message, str) or not self.message or len(self.message) > 1024:
            raise ContractValidationError("consent notice message must be bounded")
        lowered = self.message.casefold()
        if any(marker in lowered for marker in ("https://", "http://", "authorization", "token=")):
            raise SecurityPolicyError("consent notice message contains sensitive material")

    def to_public_dict(self) -> dict[str, object]:
        """Return the disclosure fields approved for a user-visible projection."""

        return {
            "provider": self.provider.value,
            "privacy_location": self.privacy_location.value,
            "network_required": self.network_required,
            "media_upload_required": self.media_upload_required,
            "consent_granted": self.consent_granted,
            "credential_runtime_only": self.credential_runtime_only,
            "fallback_provider": (
                None if self.fallback_provider is None else self.fallback_provider.value
            ),
            "requires_explicit_consent": self.requires_explicit_consent,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ProviderTransparency:
    """Bounded pre-execution disclosure for one explicit provider policy."""

    schema: str
    provider: ProviderIdentity
    provider_version: str
    privacy_mode: ProviderPrivacyMode
    network_requirement: NetworkRequirement
    network_allowed: bool
    offline: bool
    media_upload_required: bool
    upload_consent: bool
    credential_requirement: CredentialRequirement
    credential_runtime_only: bool
    retention_policy: ProviderRetentionPolicy
    cost_policy: ProviderCostPolicy
    fallback_policy: ProviderFallbackPolicy
    fallback_provider: ProviderIdentity | None
    policy_valid: bool
    execution_allowed: bool
    execution_boundary: str
    diagnostics: tuple[ValidationDiagnostic, ...]
    disclosure: str
    consent_notice: ProviderConsentNotice

    def __post_init__(self) -> None:
        if self.schema != "h3-context-provider-transparency/1":
            raise ContractValidationError("unsupported provider transparency schema")
        _require_enum(self.provider, ProviderIdentity, "transparency provider")
        _require_enum(self.privacy_mode, ProviderPrivacyMode, "transparency privacy mode")
        _require_enum(self.network_requirement, NetworkRequirement, "transparency network")
        _require_enum(
            self.credential_requirement,
            CredentialRequirement,
            "transparency credential requirement",
        )
        _require_enum(self.retention_policy, ProviderRetentionPolicy, "transparency retention")
        _require_enum(self.cost_policy, ProviderCostPolicy, "transparency cost")
        _require_enum(self.fallback_policy, ProviderFallbackPolicy, "transparency fallback")
        for value, field_name in (
            (self.network_allowed, "network_allowed"),
            (self.offline, "offline"),
            (self.media_upload_required, "media_upload_required"),
            (self.upload_consent, "upload_consent"),
            (self.credential_runtime_only, "credential_runtime_only"),
            (self.policy_valid, "policy_valid"),
            (self.execution_allowed, "execution_allowed"),
        ):
            _require_bool(value, field_name)
        if self.fallback_provider is not None:
            _require_enum(
                self.fallback_provider, ProviderIdentity, "transparency fallback provider"
            )
            if self.fallback_policy is ProviderFallbackPolicy.NEVER_AUTOMATIC:
                raise SecurityPolicyError("fallback provider requires caller-selected-only policy")
        if (
            self.fallback_provider is None
            and self.fallback_policy is not ProviderFallbackPolicy.NEVER_AUTOMATIC
        ):
            raise SecurityPolicyError("caller-selected-only fallback requires a fallback provider")
        if (
            not isinstance(self.provider_version, str)
            or _IDENTIFIER_PATTERN.fullmatch(self.provider_version) is None
        ):
            raise ContractValidationError("transparency provider_version must be bounded")
        if self.execution_boundary != "policy_only":
            raise ContractValidationError("transparency execution boundary must be policy_only")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > 32:
            raise ContractValidationError("transparency diagnostics are unbounded")
        if not all(isinstance(item, ValidationDiagnostic) for item in self.diagnostics):
            raise ContractValidationError("transparency diagnostics contain an invalid value")
        if not isinstance(self.consent_notice, ProviderConsentNotice):
            raise ContractValidationError(
                "transparency consent_notice must be a ProviderConsentNotice"
            )
        if not isinstance(self.disclosure, str) or not 1 <= len(self.disclosure) <= 4096:
            raise ContractValidationError("transparency disclosure must be bounded")
        lowered = self.disclosure.casefold()
        if (
            any(
                marker in lowered
                for marker in (
                    "http://",
                    "https://",
                    "authorization",
                    "bearer ",
                    "api_key",
                    "password",
                    "secret",
                    "token=",
                    "sig=",
                    "x-amz-",
                    "\\",
                )
            )
            or "/" in self.disclosure
        ):
            raise SecurityPolicyError("transparency disclosure contains sensitive metadata")
        if self.policy_valid != (not self.diagnostics):
            raise ContractValidationError("policy_valid contradicts transparency diagnostics")
        if self.execution_allowed != self.policy_valid:
            raise ContractValidationError("execution_allowed must match policy_valid")

    def to_wire(self) -> dict[str, object]:
        """Return an output projection with no credential reference or runtime secret."""

        return {
            "schema": self.schema,
            "provider": self.provider.value,
            "provider_version": self.provider_version,
            "privacy_mode": self.privacy_mode.value,
            "network": {
                "requirement": self.network_requirement.value,
                "allowed": self.network_allowed,
                "offline": self.offline,
            },
            "media_upload": {
                "required": self.media_upload_required,
                "consent_granted": self.upload_consent,
            },
            "credential": {
                "requirement": self.credential_requirement.value,
                "runtime_only": self.credential_runtime_only,
            },
            "retention": self.retention_policy.value,
            "cost": self.cost_policy.value,
            "fallback": {
                "policy": self.fallback_policy.value,
                "provider": None
                if self.fallback_provider is None
                else self.fallback_provider.value,
            },
            "policy_valid": self.policy_valid,
            "execution_allowed": self.execution_allowed,
            "execution_boundary": self.execution_boundary,
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "disclosure": self.disclosure,
            "consent_notice": self.consent_notice.to_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """Runtime-only credential wrapper; its value is intentionally excluded from public output."""

    reference: str
    value: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_reference(self.reference, "credential reference")
        if not isinstance(self.value, str) or not self.value:
            raise SecurityPolicyError("resolved credential value must be a non-empty string")
        if any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in self.value):
            raise SecurityPolicyError("resolved credential value contains an unsafe code point")

    def to_public_dict(self) -> dict[str, str]:
        return {"reference": self.reference}


@runtime_checkable
class CredentialResolver(Protocol):
    """Injected runtime credential lookup; implementations remain outside pure core."""

    def resolve(self, reference: str) -> ResolvedCredential:
        """Resolve one non-secret reference without exposing the value to workflow serialization."""


def _fatal(code: str, message: str, location: str | None = None) -> ValidationDiagnostic:
    return ValidationDiagnostic(ValidationSeverity.FATAL, code, message, location)


def _validate_policy_profile(
    policy: ProviderExecutionPolicy,
    profile: _ProviderDisclosureProfile,
) -> tuple[ValidationDiagnostic, ...]:
    """Apply the same fail-closed policy rules to a disclosure-only identity profile."""

    diagnostics: list[ValidationDiagnostic] = []
    is_remote = policy.provider in _REMOTE_IDENTITIES
    expected_privacy = (
        ProviderPrivacyMode.EXPLICIT_REMOTE if is_remote else ProviderPrivacyMode.LOCAL_ONLY
    )
    if policy.privacy_mode is not expected_privacy:
        diagnostics.append(
            _fatal(
                "policy.privacy_mode_mismatch",
                f"{policy.provider.value} requires privacy mode {expected_privacy.value}",
                "privacy_mode",
            )
        )
    if policy.privacy_mode is ProviderPrivacyMode.LOCAL_ONLY and (
        profile.privacy_location is not PrivacyLocation.LOCAL
    ):
        diagnostics.append(
            _fatal(
                "policy.privacy_mode_mismatch",
                "local-only policy cannot select a provider that processes data remotely",
                "privacy_mode",
            )
        )
    if policy.privacy_mode is ProviderPrivacyMode.EXPLICIT_REMOTE and (
        profile.privacy_location is PrivacyLocation.LOCAL
    ):
        diagnostics.append(
            _fatal(
                "policy.privacy_mode_mismatch",
                "explicit-remote policy cannot select a local-only provider",
                "privacy_mode",
            )
        )
    if is_remote and not policy.upload_consent:
        diagnostics.append(
            _fatal(
                "policy.upload_consent_required",
                "remote providers require explicit media-upload consent",
                "upload_consent",
            )
        )
    if not is_remote and policy.upload_consent:
        diagnostics.append(
            _fatal(
                "policy.upload_consent_forbidden",
                "local providers cannot declare remote media-upload consent",
                "upload_consent",
            )
        )
    if profile.network_requirement is not NetworkRequirement.NONE and not policy.network_allowed:
        diagnostics.append(
            _fatal(
                "policy.network_required",
                "selected provider requires network permission",
                "network_allowed",
            )
        )
    if policy.offline and profile.network_requirement is not NetworkRequirement.NONE:
        diagnostics.append(
            _fatal(
                "policy.offline_network_conflict",
                "offline policy cannot select a provider with a network requirement",
                "offline",
            )
        )
    requirement = profile.credential_requirement
    if requirement is CredentialRequirement.REQUIRED and policy.credential_reference is None:
        diagnostics.append(
            _fatal(
                "policy.credential_reference_required",
                "selected provider requires a runtime credential reference",
                "credential_reference",
            )
        )
    if requirement is CredentialRequirement.NONE and policy.credential_reference is not None:
        diagnostics.append(
            _fatal(
                "policy.credential_reference_unexpected",
                "selected provider does not accept a credential reference",
                "credential_reference",
            )
        )
    return tuple(diagnostics)


def validate_provider_policy(
    descriptor: ProviderDescriptor,
    policy: ProviderExecutionPolicy,
) -> tuple[ValidationDiagnostic, ...]:
    """Return fatal policy diagnostics before an adapter may execute."""

    if not isinstance(descriptor, ProviderDescriptor):
        raise ContractValidationError("descriptor must be a ProviderDescriptor")
    if not isinstance(policy, ProviderExecutionPolicy):
        raise ContractValidationError("policy must be a ProviderExecutionPolicy")

    diagnostics: list[ValidationDiagnostic] = []
    capabilities = descriptor.capabilities
    is_remote = policy.provider in _REMOTE_IDENTITIES

    if descriptor.identity is not policy.provider:
        diagnostics.append(
            _fatal(
                "policy.provider_mismatch",
                f"selected policy provider {policy.provider.value} does not match descriptor "
                f"{descriptor.identity.value}",
                "provider",
            )
        )

    expected_privacy = (
        ProviderPrivacyMode.EXPLICIT_REMOTE if is_remote else ProviderPrivacyMode.LOCAL_ONLY
    )
    if policy.privacy_mode is not expected_privacy:
        diagnostics.append(
            _fatal(
                "policy.privacy_mode_mismatch",
                f"{policy.provider.value} requires privacy mode {expected_privacy.value}",
                "privacy_mode",
            )
        )
    if policy.privacy_mode is ProviderPrivacyMode.LOCAL_ONLY and (
        capabilities.privacy_location is not PrivacyLocation.LOCAL
    ):
        diagnostics.append(
            _fatal(
                "policy.privacy_mode_mismatch",
                "local-only policy cannot select a provider that processes data remotely",
                "privacy_mode",
            )
        )
    if policy.privacy_mode is ProviderPrivacyMode.EXPLICIT_REMOTE and (
        capabilities.privacy_location is PrivacyLocation.LOCAL
    ):
        diagnostics.append(
            _fatal(
                "policy.privacy_mode_mismatch",
                "explicit-remote policy cannot select a local-only provider",
                "privacy_mode",
            )
        )

    if is_remote and not policy.upload_consent:
        diagnostics.append(
            _fatal(
                "policy.upload_consent_required",
                "remote providers require explicit media-upload consent",
                "upload_consent",
            )
        )
    if not is_remote and policy.upload_consent:
        diagnostics.append(
            _fatal(
                "policy.upload_consent_forbidden",
                "local providers cannot declare remote media-upload consent",
                "upload_consent",
            )
        )

    if (
        capabilities.network_requirement is not NetworkRequirement.NONE
        and not policy.network_allowed
    ):
        diagnostics.append(
            _fatal(
                "policy.network_required",
                "selected provider requires network permission",
                "network_allowed",
            )
        )
    if policy.offline and capabilities.network_requirement is not NetworkRequirement.NONE:
        diagnostics.append(
            _fatal(
                "policy.offline_network_conflict",
                "offline policy cannot select a provider with a network requirement",
                "offline",
            )
        )

    credential_requirement = capabilities.credential_requirement.value
    if credential_requirement == "required" and policy.credential_reference is None:
        diagnostics.append(
            _fatal(
                "policy.credential_reference_required",
                "selected provider requires a runtime credential reference",
                "credential_reference",
            )
        )
    if credential_requirement == "none" and policy.credential_reference is not None:
        diagnostics.append(
            _fatal(
                "policy.credential_reference_unexpected",
                "selected provider does not accept a credential reference",
                "credential_reference",
            )
        )
    return tuple(diagnostics)


def build_provider_consent_notice(
    descriptor: ProviderDescriptor,
    policy: ProviderExecutionPolicy,
) -> ProviderConsentNotice:
    """Build a non-secret disclosure before the final execution-policy gate is applied."""

    if not isinstance(descriptor, ProviderDescriptor):
        raise ContractValidationError("descriptor must be a ProviderDescriptor")
    if not isinstance(policy, ProviderExecutionPolicy):
        raise ContractValidationError("policy must be a ProviderExecutionPolicy")
    if descriptor.identity is not policy.provider:
        raise SecurityPolicyError("consent notice provider does not match the selected descriptor")

    capabilities = descriptor.capabilities
    remote = capabilities.privacy_location is not PrivacyLocation.LOCAL
    network_required = capabilities.network_requirement is not NetworkRequirement.NONE
    credential_runtime_only = capabilities.credential_requirement.value != "none"
    if remote:
        message = (
            f"Provider {policy.provider.value} will process media remotely; network access and "
            "media upload require explicit user consent. Credentials are resolved at runtime and "
            "never serialized. Provider fallback is never automatic."
        )
    else:
        message = (
            f"Provider {policy.provider.value} processes data locally; no remote transfer or "
            "network access is requested. Credentials, if used, remain runtime-only. Provider "
            "fallback is never automatic."
        )
    return ProviderConsentNotice(
        provider=policy.provider,
        privacy_location=capabilities.privacy_location,
        network_required=network_required,
        media_upload_required=remote,
        consent_granted=policy.upload_consent,
        credential_runtime_only=credential_runtime_only,
        fallback_provider=policy.fallback_provider,
        requires_explicit_consent=remote or network_required,
        message=message,
    )


def build_provider_transparency(
    policy: ProviderExecutionPolicy,
    descriptor: ProviderDescriptor | None = None,
) -> ProviderTransparency:
    """Build a conservative, non-executing disclosure for an explicit provider policy.

    A descriptor may refine network and credential capabilities for a registered adapter. Without
    one, the closed identity profile is used and remote retention/pricing remain explicitly
    provider-defined. Policy failures are returned in the projection instead of being hidden so a
    confirmation UI can explain why execution is blocked.
    """

    if not isinstance(policy, ProviderExecutionPolicy):
        raise ContractValidationError("policy must be a ProviderExecutionPolicy")
    profile = _profile_for(policy.provider)
    if descriptor is not None:
        if not isinstance(descriptor, ProviderDescriptor):
            raise ContractValidationError("descriptor must be a ProviderDescriptor")
        if descriptor.identity is not policy.provider:
            raise SecurityPolicyError("transparency descriptor does not match selected provider")
        capabilities = descriptor.capabilities
        profile = _ProviderDisclosureProfile(
            capabilities.privacy_location,
            capabilities.network_requirement,
            capabilities.credential_requirement,
            profile.retention_policy,
            profile.cost_policy,
            descriptor.provider_version,
        )
        diagnostics = validate_provider_policy(descriptor, policy)
    else:
        diagnostics = _validate_policy_profile(policy, profile)

    remote = profile.privacy_location is not PrivacyLocation.LOCAL
    fallback_policy = (
        ProviderFallbackPolicy.CALLER_SELECTED_ONLY
        if policy.fallback_provider is not None
        else ProviderFallbackPolicy.NEVER_AUTOMATIC
    )
    fallback_sentence = (
        "Fallback is caller-selected only and never automatic."
        if fallback_policy is ProviderFallbackPolicy.CALLER_SELECTED_ONLY
        else "Fallback is never automatic."
    )
    execution_state = "yes" if not diagnostics else "no"
    network_value = profile.network_requirement.value
    credential_value = profile.credential_requirement.value
    retention_value = profile.retention_policy.value
    cost_value = profile.cost_policy.value
    if remote:
        message = (
            f"Provider {policy.provider.value} is remote; network={network_value}; "
            f"upload={'required' if remote else 'not_required'}; "
            f"credential={credential_value} (runtime_only); "
            f"retention={retention_value}; cost={cost_value}; "
            f"fallback={fallback_policy.value}; execution_allowed={execution_state}. "
            f"Remote transfer requires explicit consent and network permission; {fallback_sentence}"
        )
    else:
        message = (
            f"Provider {policy.provider.value} is local-only; network={network_value}; "
            f"upload=not_required; credential={credential_value} (runtime_only); "
            f"retention={retention_value}; cost={cost_value}; "
            f"fallback={fallback_policy.value}; execution_allowed={execution_state}. "
            f"No remote transfer is requested and {fallback_sentence}"
        )
    consent_notice = ProviderConsentNotice(
        provider=policy.provider,
        privacy_location=profile.privacy_location,
        network_required=profile.network_requirement is not NetworkRequirement.NONE,
        media_upload_required=remote,
        consent_granted=policy.upload_consent,
        credential_runtime_only=profile.credential_requirement is not CredentialRequirement.NONE,
        fallback_provider=policy.fallback_provider,
        requires_explicit_consent=remote
        or profile.network_requirement is not NetworkRequirement.NONE,
        message=message,
    )
    return ProviderTransparency(
        schema="h3-context-provider-transparency/1",
        provider=policy.provider,
        provider_version=profile.provider_version,
        privacy_mode=policy.privacy_mode,
        network_requirement=profile.network_requirement,
        network_allowed=policy.network_allowed,
        offline=policy.offline,
        media_upload_required=remote,
        upload_consent=policy.upload_consent,
        credential_requirement=profile.credential_requirement,
        credential_runtime_only=profile.credential_requirement is not CredentialRequirement.NONE,
        retention_policy=profile.retention_policy,
        cost_policy=profile.cost_policy,
        fallback_policy=fallback_policy,
        fallback_provider=policy.fallback_provider,
        policy_valid=not diagnostics,
        execution_allowed=not diagnostics,
        execution_boundary="policy_only",
        diagnostics=diagnostics,
        disclosure=message,
        consent_notice=consent_notice,
    )


def select_provider_descriptor(
    policy: ProviderExecutionPolicy,
    descriptors: Iterable[ProviderDescriptor],
) -> ProviderDescriptor:
    """Select exactly the requested provider; never invoke or synthesize a fallback."""

    if not isinstance(policy, ProviderExecutionPolicy):
        raise ContractValidationError("policy must be a ProviderExecutionPolicy")
    matches: list[ProviderDescriptor] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, ProviderDescriptor):
            raise ContractValidationError("descriptors must contain ProviderDescriptor values")
        if descriptor.identity is policy.provider:
            matches.append(descriptor)
    if not matches:
        raise SecurityPolicyError(f"selected provider is not registered: {policy.provider.value}")
    if len(matches) > 1:
        raise SecurityPolicyError(
            f"selected provider is registered more than once: {policy.provider.value}"
        )
    return matches[0]


def resolve_provider_credential(
    policy: ProviderExecutionPolicy,
    descriptor: ProviderDescriptor,
    resolver: CredentialResolver | None,
) -> ResolvedCredential | None:
    """Resolve a required/optional credential at runtime without leaking resolver failures."""

    diagnostics = validate_provider_policy(descriptor, policy)
    if diagnostics:
        raise SecurityPolicyError(f"provider policy rejected: {diagnostics[0].code}")

    requirement = descriptor.capabilities.credential_requirement.value
    reference = policy.credential_reference
    if requirement == "none":
        return None
    if requirement == "optional" and reference is None:
        return None
    if reference is None:
        raise SecurityPolicyError("provider credential reference is required")
    if resolver is None:
        raise SecurityPolicyError("provider credential resolver is required")
    if not isinstance(resolver, CredentialResolver):
        raise ContractValidationError("resolver must implement CredentialResolver")
    try:
        resolved = resolver.resolve(reference)
    except Exception:
        raise SecurityPolicyError("provider credential resolution failed") from None
    if not isinstance(resolved, ResolvedCredential) or resolved.reference != reference:
        raise SecurityPolicyError("provider credential resolver returned an invalid credential")
    return resolved


DEFAULT_PROVIDER_POLICY = ProviderExecutionPolicy(
    provider=ProviderIdentity.MANUAL,
    privacy_mode=ProviderPrivacyMode.LOCAL_ONLY,
)


__all__ = [
    "ProviderCostPolicy",
    "ProviderConsentNotice",
    "CredentialResolver",
    "DEFAULT_PROVIDER_POLICY",
    "build_provider_consent_notice",
    "build_provider_transparency",
    "ProviderExecutionPolicy",
    "ProviderFallbackPolicy",
    "ProviderPrivacyMode",
    "ProviderRetentionPolicy",
    "ProviderTransparency",
    "ResolvedCredential",
    "resolve_provider_credential",
    "select_provider_descriptor",
    "validate_provider_policy",
]
