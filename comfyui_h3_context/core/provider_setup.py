"""Operational provider setup, consent, and preflight authority.

This module extends the policy-only provider disclosure without performing provider, media, model,
or network I/O. Concrete adapters supply bounded preflight observations; execution is admitted only
when the setup, consent, and preflight fingerprints still join.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from .canonical import canonical_fingerprint
from .contracts import ProviderIdentity
from .errors import (
    ContractValidationError,
    LocalAdapterCancelledError,
    LocalAdapterTimeoutError,
    ModelTransportError,
    SecurityPolicyError,
)
from .local_adapters import LocalCancellationProbe
from .model_manifest import ModelProbeReport, ModelProbeStatus
from .provider_policy import (
    ProviderExecutionPolicy,
    ProviderFallbackPolicy,
    ProviderPrivacyMode,
    build_provider_transparency,
)

PROVIDER_SETUP_SCHEMA = "h3-context-provider-setup/1"
PROVIDER_CONSENT_AUTHORITY_SCHEMA = "h3-context-provider-consent-authority/1"
PROVIDER_PREFLIGHT_SCHEMA = "h3-context-provider-preflight/1"
MAX_PROVIDER_SETUP_WIRE_BYTES = 32_768
MAX_PROVIDER_SETUP_JSON_DEPTH = 16
MAX_PROVIDER_SETUP_JSON_ITEMS = 256
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProviderLocalBackend(str, Enum):
    NONE = "none"
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"


class ProviderDestinationClass(str, Enum):
    NONE = "none"
    IN_PROCESS = "in_process"
    LOOPBACK_HTTP = "loopback_http"
    INTERNET = "internet"


class ProviderTransferBoundary(str, Enum):
    NONE = "none"
    IN_PROCESS = "in_process"
    OLLAMA_PROCESS = "ollama_process"
    LOCAL_SERVER_PROCESS = "local_server_process"
    REMOTE_UPLOAD = "remote_upload"


class ProviderConsentStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    GRANTED = "granted"
    DENIED = "denied"


class ProviderPreflightStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    NOT_RUN = "not_run"
    READY = "ready"
    BACKEND_ABSENT = "backend_absent"
    UNSAFE_CONFIGURATION = "unsafe_configuration"
    MODEL_MISSING = "model_missing"
    DIGEST_MISMATCH = "digest_mismatch"
    VERSION_MISMATCH = "version_mismatch"
    CAPABILITY_MISMATCH = "capability_mismatch"
    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    UNSUPPORTED_MEDIA = "unsupported_media"
    MODERATED = "moderated"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    STALE = "stale"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a bounded identifier")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _exact_members(value: object, expected: frozenset[str], field: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ContractValidationError(f"{field} must be an exact JSON object")
    mapped = cast(dict[str, object], value)
    if frozenset(mapped) != expected:
        raise ContractValidationError(f"{field} members are not the closed contract")
    return mapped


@dataclass(frozen=True, slots=True)
class ProviderSetup:
    """Serializable setup containing references but no resolved credential or private locator."""

    policy: ProviderExecutionPolicy
    revision: int
    local_backend: ProviderLocalBackend
    destination: ProviderDestinationClass
    transfer_boundary: ProviderTransferBoundary
    preflight_required: bool
    schema: str = PROVIDER_SETUP_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ProviderExecutionPolicy):
            raise ContractValidationError("provider setup policy is invalid")
        if type(self.revision) is not int or not 1 <= self.revision <= 2_147_483_647:
            raise ContractValidationError("provider setup revision is outside the bounded range")
        if not isinstance(self.local_backend, ProviderLocalBackend):
            raise ContractValidationError("provider setup local backend is invalid")
        if not isinstance(self.destination, ProviderDestinationClass):
            raise ContractValidationError("provider setup destination is invalid")
        if not isinstance(self.transfer_boundary, ProviderTransferBoundary):
            raise ContractValidationError("provider setup transfer boundary is invalid")
        if type(self.preflight_required) is not bool:
            raise ContractValidationError("provider setup preflight flag must be a boolean")
        if self.schema != PROVIDER_SETUP_SCHEMA:
            raise ContractValidationError("unsupported provider setup schema")
        _validate_matrix(self)

    @property
    def provider(self) -> ProviderIdentity:
        return self.policy.provider

    @property
    def fallback_policy(self) -> str:
        return (
            ProviderFallbackPolicy.CALLER_SELECTED_ONLY.value
            if self.policy.fallback_provider is not None
            else ProviderFallbackPolicy.NEVER_AUTOMATIC.value
        )

    @property
    def fallback_provider(self) -> ProviderIdentity | None:
        return self.policy.fallback_provider

    @property
    def diagnostics(self) -> tuple[str, ...]:
        values = [item.code for item in build_provider_transparency(self.policy).diagnostics]
        if self.local_backend is ProviderLocalBackend.OLLAMA:
            if self.policy.offline:
                values.append("setup.ollama_offline_conflict")
            if not self.policy.network_allowed:
                values.append("setup.ollama_network_required")
        return tuple(dict.fromkeys(values))

    @property
    def policy_valid(self) -> bool:
        return not self.diagnostics

    def _unsigned_wire(self) -> dict[str, object]:
        transparency = build_provider_transparency(self.policy)
        return {
            "schema": self.schema,
            "revision": self.revision,
            "provider": self.provider.value,
            "local_backend": self.local_backend.value,
            "destination": self.destination.value,
            "transfer_boundary": self.transfer_boundary.value,
            "preflight_required": self.preflight_required,
            "policy_valid": self.policy_valid,
            "diagnostics": list(self.diagnostics),
            "policy": self.policy.to_wire(),
            "disclosure": {
                "network": transparency.network_requirement.value
                if self.local_backend is not ProviderLocalBackend.OLLAMA
                else "loopback",
                "media_transfer": self.transfer_boundary.value,
                "retention": transparency.retention_policy.value,
                "cost": transparency.cost_policy.value,
                "credential": transparency.credential_requirement.value,
                "credential_runtime_only": transparency.credential_runtime_only,
                "fallback": self.fallback_policy,
            },
        }

    @property
    def setup_fingerprint(self) -> str:
        return canonical_fingerprint(self._unsigned_wire())

    @property
    def disclosure(self) -> str:
        preflight = "required" if self.preflight_required else "not_required"
        return (
            f"Provider setup revision={self.revision}; provider={self.provider.value}; "
            f"backend={self.local_backend.value}; destination={self.destination.value}; "
            f"media_transfer={self.transfer_boundary.value}; preflight={preflight}; "
            f"fallback={self.fallback_policy}; "
            f"policy_valid={'yes' if self.policy_valid else 'no'}."
        )

    def to_wire(self) -> dict[str, object]:
        return {**self._unsigned_wire(), "setup_fingerprint": self.setup_fingerprint}

    def to_public_dict(self) -> dict[str, object]:
        wire = self.to_wire()
        policy = cast(dict[str, object], dict(cast(dict[str, object], wire["policy"])))
        policy.pop("credential_reference", None)
        wire["policy"] = policy
        return wire


def _validate_matrix(setup: ProviderSetup) -> None:
    provider = setup.policy.provider
    if provider is ProviderIdentity.LOCAL:
        if setup.local_backend is ProviderLocalBackend.NONE:
            raise ContractValidationError("local provider requires an explicit local backend")
    elif setup.local_backend is not ProviderLocalBackend.NONE:
        raise ContractValidationError("non-local provider cannot select a local backend")

    expected = {
        (ProviderIdentity.MANUAL, ProviderLocalBackend.NONE): (
            ProviderDestinationClass.NONE,
            ProviderTransferBoundary.NONE,
            False,
        ),
        (ProviderIdentity.LOCAL, ProviderLocalBackend.COMFYUI_NATIVE): (
            ProviderDestinationClass.IN_PROCESS,
            ProviderTransferBoundary.IN_PROCESS,
            True,
        ),
        (ProviderIdentity.LOCAL, ProviderLocalBackend.OLLAMA): (
            ProviderDestinationClass.LOOPBACK_HTTP,
            ProviderTransferBoundary.OLLAMA_PROCESS,
            True,
        ),
        (ProviderIdentity.REMOTE_CUSTOM, ProviderLocalBackend.NONE): (
            ProviderDestinationClass.INTERNET,
            ProviderTransferBoundary.REMOTE_UPLOAD,
            True,
        ),
        (ProviderIdentity.OFFICIAL_MINIMAX, ProviderLocalBackend.NONE): (
            ProviderDestinationClass.INTERNET,
            ProviderTransferBoundary.REMOTE_UPLOAD,
            True,
        ),
    }
    required = expected.get((provider, setup.local_backend))
    if required != (setup.destination, setup.transfer_boundary, setup.preflight_required):
        raise ContractValidationError("provider setup route/backend matrix is inconsistent")


def build_provider_setup(
    policy: ProviderExecutionPolicy,
    *,
    local_backend: ProviderLocalBackend | None = None,
    revision: int = 1,
) -> ProviderSetup:
    if not isinstance(policy, ProviderExecutionPolicy):
        raise ContractValidationError("provider setup requires ProviderExecutionPolicy")
    build_provider_transparency(policy)
    selected = local_backend
    if selected is None:
        selected = (
            ProviderLocalBackend.COMFYUI_NATIVE
            if policy.provider is ProviderIdentity.LOCAL
            else ProviderLocalBackend.NONE
        )
    if not isinstance(selected, ProviderLocalBackend):
        raise ContractValidationError("provider setup local backend is invalid")
    route = {
        (ProviderIdentity.MANUAL, ProviderLocalBackend.NONE): (
            ProviderDestinationClass.NONE,
            ProviderTransferBoundary.NONE,
            False,
        ),
        (ProviderIdentity.LOCAL, ProviderLocalBackend.COMFYUI_NATIVE): (
            ProviderDestinationClass.IN_PROCESS,
            ProviderTransferBoundary.IN_PROCESS,
            True,
        ),
        (ProviderIdentity.LOCAL, ProviderLocalBackend.OLLAMA): (
            ProviderDestinationClass.LOOPBACK_HTTP,
            ProviderTransferBoundary.OLLAMA_PROCESS,
            True,
        ),
        (ProviderIdentity.REMOTE_CUSTOM, ProviderLocalBackend.NONE): (
            ProviderDestinationClass.INTERNET,
            ProviderTransferBoundary.REMOTE_UPLOAD,
            True,
        ),
        (ProviderIdentity.OFFICIAL_MINIMAX, ProviderLocalBackend.NONE): (
            ProviderDestinationClass.INTERNET,
            ProviderTransferBoundary.REMOTE_UPLOAD,
            True,
        ),
    }.get((policy.provider, selected))
    if route is None:
        raise ContractValidationError("provider and local backend are incompatible")
    return ProviderSetup(policy, revision, selected, *route)


@dataclass(frozen=True, slots=True)
class ProviderConsentAuthority:
    setup_fingerprint: str
    setup_revision: int
    status: ProviderConsentStatus
    network_granted: bool
    media_transfer_granted: bool
    schema: str = PROVIDER_CONSENT_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        _fingerprint(self.setup_fingerprint, "consent setup fingerprint")
        if type(self.setup_revision) is not int or self.setup_revision < 1:
            raise ContractValidationError("consent setup revision is invalid")
        if not isinstance(self.status, ProviderConsentStatus):
            raise ContractValidationError("consent status is invalid")
        if type(self.network_granted) is not bool or type(self.media_transfer_granted) is not bool:
            raise ContractValidationError("consent grants must be booleans")
        if self.schema != PROVIDER_CONSENT_AUTHORITY_SCHEMA:
            raise ContractValidationError("unsupported provider consent authority schema")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "setup_fingerprint": self.setup_fingerprint,
            "setup_revision": self.setup_revision,
            "status": self.status.value,
            "network_granted": self.network_granted,
            "media_transfer_granted": self.media_transfer_granted,
        }


def build_provider_consent_authority(setup: ProviderSetup) -> ProviderConsentAuthority:
    if not isinstance(setup, ProviderSetup):
        raise ContractValidationError("consent authority requires ProviderSetup")
    required = setup.destination in {
        ProviderDestinationClass.LOOPBACK_HTTP,
        ProviderDestinationClass.INTERNET,
    }
    network_granted = not required or setup.policy.network_allowed
    transfer_granted = (
        setup.transfer_boundary is not ProviderTransferBoundary.REMOTE_UPLOAD
        or setup.policy.upload_consent
    )
    if not required:
        status = ProviderConsentStatus.NOT_REQUIRED
    elif network_granted and transfer_granted:
        status = ProviderConsentStatus.GRANTED
    else:
        status = ProviderConsentStatus.DENIED
    return ProviderConsentAuthority(
        setup.setup_fingerprint,
        setup.revision,
        status,
        network_granted,
        transfer_granted,
    )


@dataclass(frozen=True, slots=True)
class ProviderPreflightReceipt:
    setup_fingerprint: str
    setup_revision: int
    provider: ProviderIdentity
    local_backend: ProviderLocalBackend
    status: ProviderPreflightStatus
    diagnostics: tuple[str, ...] = ()
    backend_revision: str | None = None
    endpoint_fingerprint: str | None = None
    manifest_id: str | None = None
    model_digest: str | None = None
    network_attempted: bool = False
    media_transferred: bool = False
    model_executed: bool = False
    download_attempted: bool = False
    admin_attempted: bool = False
    fallback_provider: ProviderIdentity | None = None
    schema: str = PROVIDER_PREFLIGHT_SCHEMA

    def __post_init__(self) -> None:
        _fingerprint(self.setup_fingerprint, "preflight setup fingerprint")
        if type(self.setup_revision) is not int or self.setup_revision < 1:
            raise ContractValidationError("preflight setup revision is invalid")
        if not isinstance(self.provider, ProviderIdentity):
            raise ContractValidationError("preflight provider is invalid")
        if not isinstance(self.local_backend, ProviderLocalBackend):
            raise ContractValidationError("preflight local backend is invalid")
        if not isinstance(self.status, ProviderPreflightStatus):
            raise ContractValidationError("preflight status is invalid")
        if type(self.diagnostics) is not tuple or len(self.diagnostics) > 16:
            raise ContractValidationError("preflight diagnostics are unbounded")
        for diagnostic in self.diagnostics:
            _identifier(diagnostic, "preflight diagnostic")
        for metadata_value, field in (
            (self.backend_revision, "preflight backend revision"),
            (self.manifest_id, "preflight manifest id"),
        ):
            if metadata_value is not None:
                _identifier(metadata_value, field)
        for fingerprint_value, field in (
            (self.endpoint_fingerprint, "preflight endpoint fingerprint"),
            (self.model_digest, "preflight model digest"),
        ):
            if fingerprint_value is not None:
                _fingerprint(fingerprint_value, field)
        for activity_value in (
            self.network_attempted,
            self.media_transferred,
            self.model_executed,
            self.download_attempted,
            self.admin_attempted,
        ):
            if type(activity_value) is not bool:
                raise ContractValidationError("preflight activity flags must be booleans")
        if (
            self.media_transferred
            or self.model_executed
            or self.download_attempted
            or self.admin_attempted
        ):
            raise SecurityPolicyError(
                "provider preflight cannot transfer media, execute, download, or administer"
            )
        if self.fallback_provider is not None:
            raise SecurityPolicyError("provider preflight cannot select a fallback")
        if self.schema != PROVIDER_PREFLIGHT_SCHEMA:
            raise ContractValidationError("unsupported provider preflight schema")

    @property
    def is_ready(self) -> bool:
        return self.status in {ProviderPreflightStatus.READY, ProviderPreflightStatus.NOT_REQUIRED}

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "setup_fingerprint": self.setup_fingerprint,
            "setup_revision": self.setup_revision,
            "provider": self.provider.value,
            "local_backend": self.local_backend.value,
            "status": self.status.value,
            "diagnostics": list(self.diagnostics),
            "backend_revision": self.backend_revision,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "manifest_id": self.manifest_id,
            "model_digest": self.model_digest,
            "network_attempted": self.network_attempted,
            "media_transferred": self.media_transferred,
            "model_executed": self.model_executed,
            "download_attempted": self.download_attempted,
            "admin_attempted": self.admin_attempted,
            "fallback_provider": None,
        }


def build_static_provider_preflight(
    setup: ProviderSetup,
    *,
    backend_available: bool = True,
    backend_revision: str | None = None,
) -> ProviderPreflightReceipt:
    if not isinstance(setup, ProviderSetup) or type(backend_available) is not bool:
        raise ContractValidationError("static provider preflight inputs are invalid")
    if not setup.policy_valid:
        status = ProviderPreflightStatus.UNSAFE_CONFIGURATION
    elif setup.provider is ProviderIdentity.MANUAL:
        status = ProviderPreflightStatus.NOT_REQUIRED
    elif setup.local_backend is ProviderLocalBackend.COMFYUI_NATIVE:
        status = (
            ProviderPreflightStatus.READY
            if backend_available
            else ProviderPreflightStatus.BACKEND_ABSENT
        )
    else:
        status = ProviderPreflightStatus.NOT_RUN
        backend_revision = None
    return ProviderPreflightReceipt(
        setup.setup_fingerprint,
        setup.revision,
        setup.provider,
        setup.local_backend,
        status,
        diagnostics=()
        if status in {ProviderPreflightStatus.READY, ProviderPreflightStatus.NOT_REQUIRED}
        else (status.value,),
        backend_revision=backend_revision,
    )


@dataclass(frozen=True, slots=True)
class ProviderConnectivityRequest:
    """Content-free request to an injected remote connectivity boundary."""

    setup_fingerprint: str
    setup_revision: int
    provider: ProviderIdentity
    credential_reference: str

    def __post_init__(self) -> None:
        _fingerprint(self.setup_fingerprint, "connectivity setup fingerprint")
        if type(self.setup_revision) is not int or self.setup_revision < 1:
            raise ContractValidationError("connectivity setup revision is invalid")
        if self.provider not in {
            ProviderIdentity.REMOTE_CUSTOM,
            ProviderIdentity.OFFICIAL_MINIMAX,
        }:
            raise ContractValidationError("connectivity request requires a remote provider")
        _identifier(self.credential_reference, "connectivity credential reference")


@dataclass(frozen=True, slots=True)
class ProviderConnectivityObservation:
    """Redacted result from a probe that sends no prompt or media."""

    status: ProviderPreflightStatus
    backend_revision: str
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {
            ProviderPreflightStatus.READY,
            ProviderPreflightStatus.AUTHENTICATION,
            ProviderPreflightStatus.QUOTA,
            ProviderPreflightStatus.TRANSPORT,
            ProviderPreflightStatus.TIMEOUT,
            ProviderPreflightStatus.CANCELLED,
            ProviderPreflightStatus.UNSAFE_CONFIGURATION,
        }:
            raise ContractValidationError("connectivity observation status is not admitted")
        _identifier(self.backend_revision, "connectivity backend revision")
        if type(self.diagnostics) is not tuple or len(self.diagnostics) > 16:
            raise ContractValidationError("connectivity diagnostics are unbounded")
        for diagnostic in self.diagnostics:
            _identifier(diagnostic, "connectivity diagnostic")


@runtime_checkable
class ProviderConnectivityProbe(Protocol):
    def probe(
        self,
        request: ProviderConnectivityRequest,
        *,
        cancellation: LocalCancellationProbe | None = None,
    ) -> ProviderConnectivityObservation: ...


def run_remote_provider_preflight(
    setup: ProviderSetup,
    probe: ProviderConnectivityProbe,
    *,
    cancellation: LocalCancellationProbe | None = None,
) -> ProviderPreflightReceipt:
    """Run a content-free remote connectivity probe through an injected outer boundary."""

    if (
        not isinstance(setup, ProviderSetup)
        or setup.destination is not ProviderDestinationClass.INTERNET
    ):
        raise ContractValidationError("remote preflight requires an internet provider setup")
    if not setup.policy_valid:
        raise SecurityPolicyError("remote preflight setup policy is not valid")
    if not isinstance(probe, ProviderConnectivityProbe):
        raise ContractValidationError("remote preflight requires ProviderConnectivityProbe")
    reference = setup.policy.credential_reference
    if reference is None:
        raise SecurityPolicyError("remote preflight requires an opaque credential reference")
    if cancellation is not None and cancellation.is_cancelled():
        return ProviderPreflightReceipt(
            setup.setup_fingerprint,
            setup.revision,
            setup.provider,
            setup.local_backend,
            ProviderPreflightStatus.CANCELLED,
            diagnostics=("cancelled_before_connect",),
        )
    request = ProviderConnectivityRequest(
        setup.setup_fingerprint,
        setup.revision,
        setup.provider,
        reference,
    )
    try:
        observation = probe.probe(request, cancellation=cancellation)
        if not isinstance(observation, ProviderConnectivityObservation):
            raise ContractValidationError("connectivity probe returned an invalid observation")
        status = observation.status
        diagnostics = observation.diagnostics
        backend_revision = observation.backend_revision
    except LocalAdapterCancelledError:
        status = ProviderPreflightStatus.CANCELLED
        diagnostics = ("cancelled",)
        backend_revision = None
    except LocalAdapterTimeoutError:
        status = ProviderPreflightStatus.TIMEOUT
        diagnostics = ("timeout",)
        backend_revision = None
    except ModelTransportError:
        status = ProviderPreflightStatus.TRANSPORT
        diagnostics = ("transport",)
        backend_revision = None
    if cancellation is not None and cancellation.is_cancelled():
        status = ProviderPreflightStatus.CANCELLED
        diagnostics = ("cancelled_after_response",)
        backend_revision = None
    return ProviderPreflightReceipt(
        setup.setup_fingerprint,
        setup.revision,
        setup.provider,
        setup.local_backend,
        status,
        diagnostics=diagnostics,
        backend_revision=backend_revision,
        network_attempted=True,
    )


def _ollama_failure_status(report: ModelProbeReport) -> ProviderPreflightStatus:
    diagnostics = set(report.diagnostics)
    if "cloud_route" in diagnostics:
        return ProviderPreflightStatus.UNSAFE_CONFIGURATION
    if "model_digest_mismatch" in diagnostics or "process_identity_mismatch" in diagnostics:
        return ProviderPreflightStatus.DIGEST_MISMATCH
    if "server_version_mismatch" in diagnostics:
        return ProviderPreflightStatus.VERSION_MISMATCH
    if "capability_mismatch" in diagnostics:
        return ProviderPreflightStatus.CAPABILITY_MISMATCH
    if report.status is ModelProbeStatus.UNAVAILABLE or "model_missing" in diagnostics:
        return ProviderPreflightStatus.MODEL_MISSING
    return ProviderPreflightStatus.UNSAFE_CONFIGURATION


def build_ollama_provider_preflight(
    setup: ProviderSetup,
    report: ModelProbeReport,
    *,
    endpoint_fingerprint: str,
    expected_manifest_id: str,
    expected_model_digest: str,
    expected_server_version: str,
) -> ProviderPreflightReceipt:
    if (
        not isinstance(setup, ProviderSetup)
        or setup.local_backend is not ProviderLocalBackend.OLLAMA
    ):
        raise ContractValidationError("Ollama preflight requires an Ollama provider setup")
    if not isinstance(report, ModelProbeReport):
        raise ContractValidationError("Ollama preflight report is invalid")
    _fingerprint(endpoint_fingerprint, "Ollama endpoint fingerprint")
    _identifier(expected_manifest_id, "Ollama manifest id")
    _fingerprint(expected_model_digest, "Ollama expected model digest")
    _identifier(expected_server_version, "Ollama expected server version")
    status = _ollama_failure_status(report)
    model_digest = None if report.observed_model is None else report.observed_model.digest
    if report.manifest_id != expected_manifest_id:
        status = ProviderPreflightStatus.STALE
    elif report.server is None or report.server.version != expected_server_version:
        status = ProviderPreflightStatus.VERSION_MISMATCH
    elif report.server.cloud_enabled:
        status = ProviderPreflightStatus.UNSAFE_CONFIGURATION
    elif report.observed_model is None:
        status = ProviderPreflightStatus.MODEL_MISSING
    elif report.observed_model.digest != expected_model_digest:
        status = ProviderPreflightStatus.DIGEST_MISMATCH
    elif report.status is ModelProbeStatus.SUPPORTED:
        status = ProviderPreflightStatus.READY
    return ProviderPreflightReceipt(
        setup.setup_fingerprint,
        setup.revision,
        setup.provider,
        setup.local_backend,
        status,
        diagnostics=tuple(report.diagnostics),
        backend_revision=None if report.server is None else report.server.version,
        endpoint_fingerprint=endpoint_fingerprint,
        manifest_id=report.manifest_id,
        model_digest=model_digest,
        network_attempted=True,
    )


def admit_provider_execution(
    setup: ProviderSetup,
    consent: ProviderConsentAuthority,
    preflight: ProviderPreflightReceipt,
) -> None:
    """Fail closed unless all three current authorities identify the same ready setup."""

    if not isinstance(setup, ProviderSetup):
        raise ContractValidationError("provider execution setup is invalid")
    if not isinstance(consent, ProviderConsentAuthority) or not isinstance(
        preflight, ProviderPreflightReceipt
    ):
        raise ContractValidationError("provider execution authority is invalid")
    if not setup.policy_valid:
        raise SecurityPolicyError("provider setup policy is not valid")
    expected_consent = build_provider_consent_authority(setup)
    if consent != expected_consent:
        raise SecurityPolicyError("provider consent authority is forged or contradictory")
    # CRITICAL: never admit copied/stale consent or preflight evidence for a new setup revision.
    if (
        consent.setup_fingerprint != setup.setup_fingerprint
        or preflight.setup_fingerprint != setup.setup_fingerprint
        or consent.setup_revision != setup.revision
        or preflight.setup_revision != setup.revision
        or preflight.provider is not setup.provider
        or preflight.local_backend is not setup.local_backend
    ):
        raise SecurityPolicyError("provider setup authority is stale or incompatible")
    if consent.status is ProviderConsentStatus.DENIED:
        raise SecurityPolicyError("provider consent is not granted")
    if not preflight.is_ready:
        raise SecurityPolicyError("provider preflight is not ready")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError("provider setup JSON contains duplicate members")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ContractValidationError("provider setup JSON contains a non-finite constant")


def _validate_json_resources(value: object, *, depth: int = 0, count: list[int]) -> None:
    if depth > MAX_PROVIDER_SETUP_JSON_DEPTH:
        raise ContractValidationError("provider setup JSON exceeds the resource depth limit")
    count[0] += 1
    if count[0] > MAX_PROVIDER_SETUP_JSON_ITEMS:
        raise ContractValidationError("provider setup JSON exceeds the resource item limit")
    if type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            if type(key) is not str or len(key) > 256:
                raise ContractValidationError("provider setup JSON member name is unbounded")
            _validate_json_resources(child, depth=depth + 1, count=count)
    elif type(value) is list:
        for child in cast(list[object], value):
            _validate_json_resources(child, depth=depth + 1, count=count)
    elif value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > 4_096:
            raise ContractValidationError("provider setup JSON text is unbounded")
    else:
        raise ContractValidationError("provider setup JSON contains an unsupported value type")


def decode_provider_setup_json(payload: str | bytes | bytearray) -> ProviderSetup:
    """Decode the exact strict-UTF-8 setup wire contract with duplicate/resource closure."""

    if type(payload) is str:
        try:
            encoded = str.encode(payload, "utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise ContractValidationError("provider setup JSON is not strict UTF-8") from exc
    elif type(payload) is bytes:
        encoded = payload
    elif type(payload) is bytearray:
        encoded = bytes(payload)
    else:
        raise ContractValidationError("provider setup JSON requires exact text or byte input")
    if not encoded or len(encoded) > MAX_PROVIDER_SETUP_WIRE_BYTES:
        raise ContractValidationError("provider setup JSON exceeds the bounded byte limit")
    try:
        text = encoded.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractValidationError("provider setup JSON is not strict JSON") from exc
    _validate_json_resources(value, count=[0])
    root = _exact_members(
        value,
        frozenset(
            {
                "schema",
                "revision",
                "provider",
                "local_backend",
                "destination",
                "transfer_boundary",
                "preflight_required",
                "policy_valid",
                "diagnostics",
                "policy",
                "disclosure",
                "setup_fingerprint",
            }
        ),
        "provider setup",
    )
    policy_wire = _exact_members(
        root["policy"],
        frozenset(
            {
                "provider",
                "privacy_mode",
                "offline",
                "network_allowed",
                "upload_consent",
                "credential_reference",
                "fallback_provider",
            }
        ),
        "provider setup policy",
    )
    _exact_members(
        root["disclosure"],
        frozenset(
            {
                "network",
                "media_transfer",
                "retention",
                "cost",
                "credential",
                "credential_runtime_only",
                "fallback",
            }
        ),
        "provider setup disclosure",
    )
    if type(root["policy_valid"]) is not bool or type(root["diagnostics"]) is not list:
        raise ContractValidationError("provider setup policy status is invalid")
    try:
        fallback_raw = policy_wire["fallback_provider"]
        provider_raw = policy_wire["provider"]
        privacy_raw = policy_wire["privacy_mode"]
        offline_raw = policy_wire["offline"]
        network_raw = policy_wire["network_allowed"]
        upload_raw = policy_wire["upload_consent"]
        credential_raw = policy_wire["credential_reference"]
        backend_raw = root["local_backend"]
        revision_raw = root["revision"]
        if type(provider_raw) is not str or type(privacy_raw) is not str:
            raise ContractValidationError("provider setup policy enums are invalid")
        if (
            type(offline_raw) is not bool
            or type(network_raw) is not bool
            or type(upload_raw) is not bool
        ):
            raise ContractValidationError("provider setup policy flags are invalid")
        if credential_raw is not None and type(credential_raw) is not str:
            raise ContractValidationError("provider credential reference is invalid")
        if fallback_raw is not None and type(fallback_raw) is not str:
            raise ContractValidationError("provider fallback identity is invalid")
        if type(backend_raw) is not str or type(revision_raw) is not int:
            raise ContractValidationError("provider setup backend or revision is invalid")
        setup = build_provider_setup(
            ProviderExecutionPolicy(
                provider=ProviderIdentity(provider_raw),
                privacy_mode=ProviderPrivacyMode(privacy_raw),
                offline=offline_raw,
                network_allowed=network_raw,
                upload_consent=upload_raw,
                credential_reference=credential_raw,
                fallback_provider=None if fallback_raw is None else ProviderIdentity(fallback_raw),
            ),
            local_backend=ProviderLocalBackend(backend_raw),
            revision=revision_raw,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ContractValidationError("provider setup JSON values are invalid") from exc
    if setup.to_wire() != root:
        raise ContractValidationError("provider setup JSON is non-canonical or stale")
    return setup


__all__ = [
    "MAX_PROVIDER_SETUP_WIRE_BYTES",
    "MAX_PROVIDER_SETUP_JSON_DEPTH",
    "MAX_PROVIDER_SETUP_JSON_ITEMS",
    "PROVIDER_CONSENT_AUTHORITY_SCHEMA",
    "PROVIDER_PREFLIGHT_SCHEMA",
    "PROVIDER_SETUP_SCHEMA",
    "ProviderConsentAuthority",
    "ProviderConsentStatus",
    "ProviderDestinationClass",
    "ProviderConnectivityObservation",
    "ProviderConnectivityProbe",
    "ProviderConnectivityRequest",
    "ProviderLocalBackend",
    "ProviderPreflightReceipt",
    "ProviderPreflightStatus",
    "ProviderSetup",
    "ProviderTransferBoundary",
    "admit_provider_execution",
    "build_ollama_provider_preflight",
    "build_provider_consent_authority",
    "build_provider_setup",
    "build_static_provider_preflight",
    "decode_provider_setup_json",
    "run_remote_provider_preflight",
]
