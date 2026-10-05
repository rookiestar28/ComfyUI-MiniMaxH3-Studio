"""Sanitized, fingerprinted recordings for explicitly authorized official-oracle fixtures."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .canonical import canonical_fingerprint
from .context_reporting import ProviderOutcome
from .contracts import ProviderIdentity
from .errors import ContractValidationError, OfficialContextIRError

OFFICIAL_CONTEXT_IR_RECORDING_SCHEMA = "h3.context.ir.recording.v1"
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PUBLIC_FIELDS = frozenset(
    {
        "recording_id",
        "schema_version",
        "provider",
        "provider_version",
        "endpoint_revision",
        "authorization_label",
        "outcome",
        "task_id",
        "request_fingerprint",
        "output_fingerprint",
        "sanitized",
    }
)


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded public identifier")
    lowered = value.casefold()
    if any(marker in lowered for marker in ("secret", "token", "credential", "password", "path")):
        raise ContractValidationError(f"{field_name} must not contain sensitive markers")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a SHA-256 fingerprint")
    return value


@dataclass(frozen=True, slots=True)
class OfficialContextIRRecording:
    """Public recording metadata with no representable prompt, locator, or credential field."""

    recording_id: str
    schema_version: str
    provider: ProviderIdentity
    provider_version: str
    endpoint_revision: str
    authorization_label: str
    outcome: ProviderOutcome
    task_id: str
    request_fingerprint: str
    output_fingerprint: str
    sanitized: bool = True

    def __post_init__(self) -> None:
        _identifier(self.recording_id, "recording_id")
        if self.schema_version != OFFICIAL_CONTEXT_IR_RECORDING_SCHEMA:
            raise ContractValidationError("unsupported official recording schema")
        if self.provider is not ProviderIdentity.OFFICIAL_MINIMAX:
            raise ContractValidationError("official recording provider must be official_minimax")
        for value, field_name in (
            (self.provider_version, "provider_version"),
            (self.endpoint_revision, "endpoint_revision"),
            (self.authorization_label, "authorization_label"),
            (self.task_id, "task_id"),
        ):
            _identifier(value, field_name)
        if not isinstance(self.outcome, ProviderOutcome):
            raise ContractValidationError("recording outcome must be a ProviderOutcome")
        if not isinstance(self.sanitized, bool) or not self.sanitized:
            raise ContractValidationError("official recordings must be marked sanitized")
        _fingerprint(self.request_fingerprint, "request_fingerprint")
        _fingerprint(self.output_fingerprint, "output_fingerprint")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "recording_id": self.recording_id,
            "schema_version": self.schema_version,
            "provider": self.provider.value,
            "provider_version": self.provider_version,
            "endpoint_revision": self.endpoint_revision,
            "authorization_label": self.authorization_label,
            "outcome": self.outcome.value,
            "task_id": self.task_id,
            "request_fingerprint": self.request_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "sanitized": self.sanitized,
        }

    @classmethod
    def from_public_dict(cls, value: Mapping[str, object]) -> OfficialContextIRRecording:
        if not isinstance(value, Mapping):
            raise ContractValidationError("official recording must be a mapping")
        if set(value) != _PUBLIC_FIELDS:
            raise ContractValidationError("official recording contains unknown or missing fields")
        try:
            provider = ProviderIdentity(value["provider"])
            outcome = ProviderOutcome(value["outcome"])
        except (TypeError, ValueError):
            raise ContractValidationError("official recording enum value is invalid") from None
        return cls(
            recording_id=value["recording_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
            provider=provider,
            provider_version=value["provider_version"],  # type: ignore[arg-type]
            endpoint_revision=value["endpoint_revision"],  # type: ignore[arg-type]
            authorization_label=value["authorization_label"],  # type: ignore[arg-type]
            outcome=outcome,
            task_id=value["task_id"],  # type: ignore[arg-type]
            request_fingerprint=value["request_fingerprint"],  # type: ignore[arg-type]
            output_fingerprint=value["output_fingerprint"],  # type: ignore[arg-type]
            sanitized=value["sanitized"],  # type: ignore[arg-type]
        )


def build_official_context_ir_recording(
    result: object,
    *,
    authorization_label: str,
) -> OfficialContextIRRecording:
    """Create a sanitized recording from a successful injected-adapter result."""

    from .official_context_ir import OfficialContextIRResult

    if not isinstance(result, OfficialContextIRResult):
        raise ContractValidationError("recording result must be an OfficialContextIRResult")
    receipt = result.receipt
    if (
        receipt.provider is not ProviderIdentity.OFFICIAL_MINIMAX
        or receipt.outcome is not ProviderOutcome.SUCCEEDED
        or receipt.task_id is None
        or receipt.input_fingerprint is None
        or receipt.output_fingerprint is None
    ):
        raise OfficialContextIRError(
            "recording", "only successful official results can be recorded"
        )
    if not isinstance(receipt.provider_version, str) or not isinstance(
        receipt.endpoint_revision, str
    ):
        raise OfficialContextIRError("recording", "official receipt metadata is incomplete")
    provider_version = receipt.provider_version
    endpoint_revision = receipt.endpoint_revision
    _identifier(authorization_label, "authorization_label")
    request_fingerprint = _fingerprint(f"sha256:{receipt.input_fingerprint}", "request_fingerprint")
    output_fingerprint = _fingerprint(f"sha256:{receipt.output_fingerprint}", "output_fingerprint")
    projection = {
        "schema_version": OFFICIAL_CONTEXT_IR_RECORDING_SCHEMA,
        "provider": ProviderIdentity.OFFICIAL_MINIMAX.value,
        "provider_version": provider_version,
        "endpoint_revision": endpoint_revision,
        "authorization_label": authorization_label,
        "outcome": receipt.outcome.value,
        "task_id": receipt.task_id,
        "request_fingerprint": request_fingerprint,
        "output_fingerprint": output_fingerprint,
        "sanitized": True,
    }
    recording_id = f"recording_{canonical_fingerprint(projection).split(':', 1)[1][:24]}"
    return OfficialContextIRRecording(
        recording_id=recording_id,
        schema_version=OFFICIAL_CONTEXT_IR_RECORDING_SCHEMA,
        provider=ProviderIdentity.OFFICIAL_MINIMAX,
        provider_version=provider_version,
        endpoint_revision=endpoint_revision,
        authorization_label=authorization_label,
        outcome=ProviderOutcome.SUCCEEDED,
        task_id=receipt.task_id,
        request_fingerprint=request_fingerprint,
        output_fingerprint=output_fingerprint,
    )


__all__ = [
    "OFFICIAL_CONTEXT_IR_RECORDING_SCHEMA",
    "OfficialContextIRRecording",
    "build_official_context_ir_recording",
]
