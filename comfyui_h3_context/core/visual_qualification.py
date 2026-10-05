"""Fail-closed M11 visual-runtime qualification bindings.

The binding joins a frozen M11-01 benchmark candidate, an M11-08 acceptance result, and one exact
model manifest.  It never discovers a model, runs media, or turns injected contract evidence into a
supported runtime claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from .canonical import canonical_fingerprint
from .errors import VisualQualificationError
from .model_manifest import ModelCapability, ModelCapabilityState, ModelManifest
from .visual_acceptance import AcceptanceStatus, VisualAcceptanceReport
from .visual_benchmark import (
    VisualBenchmarkPlan,
    VisualCandidateFamily,
    VisualCandidateProfile,
    VisualDisposition,
)

VISUAL_QUALIFICATION_SCHEMA = "h3.visual.qualification.v1"
MAX_VISUAL_QUALIFICATION_TTL_SECONDS = 86_400
# SECURITY: admission remains empty until an independently accepted receipt is pinned in code.
_TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS: frozenset[str] = frozenset()
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise VisualQualificationError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _bounded_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise VisualQualificationError(f"{field_name} must be bounded non-empty text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise VisualQualificationError(f"{field_name} contains a control character")
    return value


def _utc_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise VisualQualificationError(f"{field_name} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise VisualQualificationError(f"{field_name} must be an RFC3339 UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise VisualQualificationError(f"{field_name} must be UTC")
    return parsed


def _candidate_fingerprint(candidate: VisualCandidateProfile) -> str:
    return canonical_fingerprint(candidate.to_wire())


def _receipt_payload(receipt: VisualQualificationReceipt) -> dict[str, object]:
    return {
        "schema": receipt.schema,
        "plan_fingerprint": receipt.plan_fingerprint,
        "acceptance_fingerprint": receipt.acceptance_fingerprint,
        "candidate_id": receipt.candidate_id,
        "candidate_fingerprint": receipt.candidate_fingerprint,
        "manifest_fingerprint": receipt.manifest_fingerprint,
        "adapter_id": receipt.adapter_id,
        "adapter_version": receipt.adapter_version,
        "model_id": receipt.model_id,
        "device_profile_id": receipt.device_profile_id,
        "device_kind": receipt.device_kind,
        "dtype": receipt.dtype,
        "host_profile": receipt.host_profile,
        "disposition": receipt.disposition.value,
        "decided_at_utc": receipt.decided_at_utc,
        "expires_at_utc": receipt.expires_at_utc,
    }


@dataclass(frozen=True, slots=True)
class VisualQualificationReceipt:
    """Versioned integrity envelope for one accepted executable visual profile."""

    plan_fingerprint: str
    acceptance_fingerprint: str
    candidate_id: str
    candidate_fingerprint: str
    manifest_fingerprint: str
    adapter_id: str
    adapter_version: str
    model_id: str
    device_profile_id: str
    device_kind: str
    dtype: str
    host_profile: str
    disposition: VisualDisposition
    decided_at_utc: str
    expires_at_utc: str
    receipt_fingerprint: str
    schema: str = VISUAL_QUALIFICATION_SCHEMA

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.plan_fingerprint, "plan_fingerprint"),
            (self.acceptance_fingerprint, "acceptance_fingerprint"),
            (self.candidate_fingerprint, "candidate_fingerprint"),
            (self.manifest_fingerprint, "manifest_fingerprint"),
            (self.receipt_fingerprint, "receipt_fingerprint"),
        ):
            _fingerprint(value, field_name)
        for value, field_name in (
            (self.candidate_id, "candidate_id"),
            (self.adapter_id, "adapter_id"),
            (self.adapter_version, "adapter_version"),
            (self.model_id, "model_id"),
            (self.device_profile_id, "device_profile_id"),
            (self.device_kind, "device_kind"),
            (self.dtype, "dtype"),
            (self.host_profile, "host_profile"),
        ):
            _bounded_text(value, field_name)
        if self.disposition is not VisualDisposition.QUALIFIED:
            raise VisualQualificationError("qualification receipt disposition is not qualified")
        decided = _utc_time(self.decided_at_utc, "decided_at_utc")
        expires = _utc_time(self.expires_at_utc, "expires_at_utc")
        if expires <= decided:
            raise VisualQualificationError("qualification receipt expiry must follow its decision")
        if (expires - decided).total_seconds() > MAX_VISUAL_QUALIFICATION_TTL_SECONDS:
            raise VisualQualificationError("qualification receipt exceeds the maximum TTL")
        if self.schema != VISUAL_QUALIFICATION_SCHEMA:
            raise VisualQualificationError("unsupported visual qualification schema")
        if self.receipt_fingerprint != canonical_fingerprint(_receipt_payload(self)):
            raise VisualQualificationError("qualification receipt fingerprint is tampered")

    def _payload(self) -> dict[str, object]:
        return _receipt_payload(self)

    def to_public_dict(self) -> dict[str, object]:
        return {**self._payload(), "receipt_fingerprint": self.receipt_fingerprint}

    def assert_current(self, current_time_utc: str) -> None:
        _validate_receipt_current(self, current_time_utc)


@dataclass(frozen=True, slots=True)
class VisualQualificationBinding:
    """Validated admission token retained by one native VLM adapter instance."""

    plan: VisualBenchmarkPlan
    acceptance: VisualAcceptanceReport
    manifest: ModelManifest
    receipt: VisualQualificationReceipt
    validated_at_utc: str

    def __post_init__(self) -> None:
        _validate_binding_evidence(
            self.plan,
            self.acceptance,
            self.manifest,
            self.receipt,
            current_time_utc=self.validated_at_utc,
        )


def _require_exact_typed_value(value: object, expected: Any, field_name: str) -> None:
    origin = get_origin(expected)
    if origin in (Union, UnionType):
        for option in get_args(expected):
            try:
                _require_exact_typed_value(value, option, field_name)
                return
            except VisualQualificationError:
                continue
        raise VisualQualificationError(
            f"qualification requires exact concrete evidence at {field_name}"
        )
    if origin is tuple:
        if type(value) is not tuple:
            raise VisualQualificationError(
                f"qualification requires exact concrete evidence at {field_name}"
            )
        arguments = get_args(expected)
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            for index, item in enumerate(value):
                _require_exact_typed_value(item, arguments[0], f"{field_name}[{index}]")
            return
        if len(value) != len(arguments):
            raise VisualQualificationError(
                f"qualification requires exact concrete evidence at {field_name}"
            )
        for index, (item, item_type) in enumerate(zip(value, arguments, strict=True)):
            _require_exact_typed_value(item, item_type, f"{field_name}[{index}]")
        return
    if origin is frozenset:
        if type(value) is not frozenset:
            raise VisualQualificationError(
                f"qualification requires exact concrete evidence at {field_name}"
            )
        (item_type,) = get_args(expected)
        for index, item in enumerate(value):
            _require_exact_typed_value(item, item_type, f"{field_name}[{index}]")
        return
    if expected is Any:
        raise VisualQualificationError(f"qualification evidence type is not closed at {field_name}")
    if expected is float:
        if type(value) not in (int, float):
            raise VisualQualificationError(
                f"qualification requires exact concrete evidence at {field_name}"
            )
        return
    if not isinstance(expected, type):
        raise VisualQualificationError(
            f"qualification evidence type is unsupported at {field_name}"
        )
    if is_dataclass(expected):
        if type(value) is not expected:
            raise VisualQualificationError(
                f"qualification requires exact concrete evidence at {field_name}"
            )
        annotations = get_type_hints(expected)
        for data_field in fields(expected):
            try:
                field_value = object.__getattribute__(value, data_field.name)
            except AttributeError as exc:
                raise VisualQualificationError(
                    f"qualification contains malformed evidence at {field_name}.{data_field.name}"
                ) from exc
            _require_exact_typed_value(
                field_value,
                annotations[data_field.name],
                f"{field_name}.{data_field.name}",
            )
        validator = expected.__dict__.get("__post_init__")
        if validator is not None:
            validator(value)
        return
    if issubclass(expected, Enum) or expected in (str, int, bool, type(None)):
        if type(value) is not expected:
            raise VisualQualificationError(
                f"qualification requires exact concrete evidence at {field_name}"
            )
        return
    raise VisualQualificationError(f"qualification evidence type is unsupported at {field_name}")


def _validate_exact_evidence_values(
    values: tuple[tuple[object, type[object], str], ...],
) -> None:
    try:
        for value, expected, field_name in values:
            _require_exact_typed_value(value, expected, field_name)
    except VisualQualificationError:
        raise
    except Exception as exc:
        raise VisualQualificationError("qualification contains malformed evidence") from exc


def _validate_exact_issuance_evidence(
    plan: object,
    acceptance: object,
    manifest: object,
) -> None:
    _validate_exact_evidence_values(
        (
            (plan, VisualBenchmarkPlan, "plan"),
            (acceptance, VisualAcceptanceReport, "acceptance"),
            (manifest, ModelManifest, "manifest"),
        )
    )


def _validate_exact_evidence_graph(
    plan: object,
    acceptance: object,
    manifest: object,
    receipt: object,
) -> None:
    _validate_exact_evidence_values(
        (
            (plan, VisualBenchmarkPlan, "plan"),
            (acceptance, VisualAcceptanceReport, "acceptance"),
            (manifest, ModelManifest, "manifest"),
            (receipt, VisualQualificationReceipt, "receipt"),
        )
    )


def _validate_receipt_current(receipt: VisualQualificationReceipt, current_time_utc: str) -> None:
    if type(receipt) is not VisualQualificationReceipt:
        raise VisualQualificationError("qualification requires exact concrete evidence at receipt")
    if receipt.receipt_fingerprint != canonical_fingerprint(_receipt_payload(receipt)):
        raise VisualQualificationError("qualification receipt fingerprint is tampered")
    decided = _utc_time(receipt.decided_at_utc, "decided_at_utc")
    expires = _utc_time(receipt.expires_at_utc, "expires_at_utc")
    if (expires - decided).total_seconds() > MAX_VISUAL_QUALIFICATION_TTL_SECONDS:
        raise VisualQualificationError("qualification receipt exceeds the maximum TTL")
    now = _utc_time(current_time_utc, "current_time_utc")
    if now < decided:
        raise VisualQualificationError("qualification receipt decision is in the future")
    if now >= expires:
        raise VisualQualificationError("qualification receipt is expired")


def validate_visual_qualification_admission(
    binding: object,
    manifest: ModelManifest,
    execution_device_kind: str | None = None,
    execution_device_index: int | None = None,
) -> None:
    """Revalidate one exact concrete binding at a production admission boundary."""

    # SECURITY: isinstance can be spoofed and virtual dispatch would delegate admission to callers.
    if type(binding) is not VisualQualificationBinding:
        raise VisualQualificationError("visual qualification requires an exact concrete binding")
    try:
        plan = object.__getattribute__(binding, "plan")
        acceptance = object.__getattribute__(binding, "acceptance")
        bound_manifest = object.__getattribute__(binding, "manifest")
        receipt = object.__getattribute__(binding, "receipt")
    except AttributeError as exc:
        raise VisualQualificationError("visual qualification contains malformed evidence") from exc
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _validate_binding_evidence(
        plan,
        acceptance,
        bound_manifest,
        receipt,
        current_time_utc=now,
    )
    _validate_exact_evidence_values(((manifest, ModelManifest, "runtime_manifest"),))
    if manifest.fingerprint != bound_manifest.fingerprint:
        raise VisualQualificationError("runtime manifest does not match qualification binding")
    if execution_device_kind is None:
        manifest_kind = manifest.runtime.device.kind
        execution_device_kind = (
            manifest_kind.value if hasattr(manifest_kind, "value") else str(manifest_kind)
        )
        execution_device_index = manifest.runtime.device.index
    if execution_device_kind != receipt.device_kind:
        raise VisualQualificationError("execution device does not match qualification receipt")
    if execution_device_index is not None:
        raise VisualQualificationError("execution device index is not bound by qualification")


def _find_candidate(plan: VisualBenchmarkPlan, candidate_id: str) -> VisualCandidateProfile:
    candidates = tuple(item for item in plan.candidates if item.candidate_id == candidate_id)
    if len(candidates) != 1:
        raise VisualQualificationError("qualification candidate is absent from the benchmark plan")
    return candidates[0]


def _validate_join(
    plan: VisualBenchmarkPlan,
    acceptance: VisualAcceptanceReport,
    candidate: VisualCandidateProfile,
    manifest: ModelManifest,
    device_profile_id: str,
) -> None:
    if acceptance.plan_id != plan.plan_id or acceptance.plan_fingerprint != plan.fingerprint:
        raise VisualQualificationError("visual acceptance does not match the benchmark plan")
    if acceptance.status is not AcceptanceStatus.QUALIFIED:
        raise VisualQualificationError("visual acceptance is not qualified")
    if candidate.disposition is not VisualDisposition.QUALIFIED:
        raise VisualQualificationError("visual benchmark candidate is not qualified")
    if candidate.candidate_id not in acceptance.executable_candidate_ids:
        raise VisualQualificationError("visual candidate is not executable in acceptance")
    accepted = tuple(
        item
        for item in acceptance.candidate_receipts
        if item.candidate_id == candidate.candidate_id
    )
    if len(accepted) != 1 or not accepted[0].executable:
        raise VisualQualificationError("visual candidate acceptance receipt is not executable")
    if not isinstance(manifest, ModelManifest) or not manifest.approved:
        raise VisualQualificationError("model manifest is not approved")
    if ModelCapability.VISION not in manifest.capabilities:
        raise VisualQualificationError("model manifest lacks vision capability")
    if manifest.cancellation is not ModelCapabilityState.QUALIFIED:
        raise VisualQualificationError("model manifest cancellation cleanup is not qualified")
    if candidate.family is not VisualCandidateFamily.COMFYUI_NATIVE:
        raise VisualQualificationError("native VLM requires a ComfyUI-native candidate")
    if manifest.backend_family.value != candidate.family.value:
        raise VisualQualificationError("model backend family does not match the candidate")
    if (
        manifest.adapter_id != candidate.adapter_id
        or manifest.adapter_version != candidate.adapter_version
        or manifest.model_id != candidate.model_id
    ):
        raise VisualQualificationError("model/adapter identity does not match the candidate")
    if candidate.requires_network:
        raise VisualQualificationError("native qualified candidate cannot require network access")
    devices = tuple(
        item for item in candidate.device_profiles if item.profile_id == device_profile_id
    )
    if len(devices) != 1:
        raise VisualQualificationError("qualification device profile is absent")
    device = devices[0]
    manifest_device = manifest.runtime.device.kind
    manifest_device_value = (
        manifest_device.value if hasattr(manifest_device, "value") else str(manifest_device)
    )
    if manifest_device_value != device.device.value or manifest.runtime.dtype != device.dtype:
        raise VisualQualificationError("model runtime does not match the qualified device profile")
    if not device.supports_determinism or not device.supports_cancellation_cleanup:
        raise VisualQualificationError("qualified device profile lacks deterministic cleanup")
    runtime_limits = manifest.runtime.limits
    max_memory_bytes = min(device.peak_ram_mb_limit, plan.limits.max_peak_ram_mb) * 1024 * 1024
    if (
        manifest.runtime.max_media_items
        > min(device.max_media_items, plan.limits.max_media_items_per_case)
        or runtime_limits.max_references
        > min(device.max_media_items, plan.limits.max_media_items_per_case)
        or runtime_limits.max_wall_time_seconds
        > min(device.timeout_seconds, plan.limits.max_wall_time_seconds)
        or runtime_limits.max_output_bytes
        > min(device.max_output_bytes, plan.limits.max_output_bytes)
        or runtime_limits.max_concurrency > plan.limits.max_concurrency
        or runtime_limits.max_memory_bytes > max_memory_bytes
    ):
        raise VisualQualificationError("model runtime exceeds qualified resource limits")


def create_visual_qualification_receipt(
    plan: VisualBenchmarkPlan,
    acceptance: VisualAcceptanceReport,
    manifest: ModelManifest,
    *,
    candidate_id: str,
    device_profile_id: str,
    decided_at_utc: str,
    expires_at_utc: str,
) -> VisualQualificationReceipt:
    """Create a self-fingerprinted receipt only from matching qualified evidence."""

    _validate_exact_issuance_evidence(plan, acceptance, manifest)
    for value, field_name in (
        (candidate_id, "candidate_id"),
        (device_profile_id, "device_profile_id"),
        (decided_at_utc, "decided_at_utc"),
        (expires_at_utc, "expires_at_utc"),
    ):
        if type(value) is not str:
            raise VisualQualificationError(
                f"qualification requires exact concrete evidence at issuance.{field_name}"
            )
    candidate = _find_candidate(plan, candidate_id)
    _validate_join(plan, acceptance, candidate, manifest, device_profile_id)
    decided = _utc_time(decided_at_utc, "decided_at_utc")
    expires = _utc_time(expires_at_utc, "expires_at_utc")
    if expires <= decided:
        raise VisualQualificationError("qualification receipt expiry must follow its decision")
    device = next(
        item for item in candidate.device_profiles if item.profile_id == device_profile_id
    )
    payload: dict[str, object] = {
        "schema": VISUAL_QUALIFICATION_SCHEMA,
        "plan_fingerprint": plan.fingerprint,
        "acceptance_fingerprint": acceptance.fingerprint,
        "candidate_id": candidate.candidate_id,
        "candidate_fingerprint": _candidate_fingerprint(candidate),
        "manifest_fingerprint": manifest.fingerprint,
        "adapter_id": candidate.adapter_id,
        "adapter_version": candidate.adapter_version,
        "model_id": candidate.model_id,
        "device_profile_id": device.profile_id,
        "device_kind": device.device.value,
        "dtype": device.dtype,
        "host_profile": manifest.host_profile,
        "disposition": VisualDisposition.QUALIFIED.value,
        "decided_at_utc": decided_at_utc,
        "expires_at_utc": expires_at_utc,
    }
    return VisualQualificationReceipt(
        plan_fingerprint=plan.fingerprint,
        acceptance_fingerprint=acceptance.fingerprint,
        candidate_id=candidate.candidate_id,
        candidate_fingerprint=_candidate_fingerprint(candidate),
        manifest_fingerprint=manifest.fingerprint,
        adapter_id=candidate.adapter_id,
        adapter_version=candidate.adapter_version,
        model_id=candidate.model_id,
        device_profile_id=device.profile_id,
        device_kind=device.device.value,
        dtype=device.dtype,
        host_profile=manifest.host_profile,
        disposition=VisualDisposition.QUALIFIED,
        decided_at_utc=decided_at_utc,
        expires_at_utc=expires_at_utc,
        receipt_fingerprint=canonical_fingerprint(payload),
    )


def _validate_binding_evidence(
    plan: VisualBenchmarkPlan,
    acceptance: VisualAcceptanceReport,
    manifest: ModelManifest,
    receipt: VisualQualificationReceipt,
    *,
    current_time_utc: str,
) -> None:
    _validate_exact_evidence_graph(plan, acceptance, manifest, receipt)
    # SECURITY: a caller-computed self-hash proves integrity, not repository approval.
    if receipt.receipt_fingerprint not in _TRUSTED_VISUAL_QUALIFICATION_RECEIPT_FINGERPRINTS:
        raise VisualQualificationError("visual qualification receipt is not trusted")
    _validate_receipt_current(receipt, current_time_utc)
    candidate = _find_candidate(plan, receipt.candidate_id)
    _validate_join(plan, acceptance, candidate, manifest, receipt.device_profile_id)
    device = next(
        item for item in candidate.device_profiles if item.profile_id == receipt.device_profile_id
    )
    expected = {
        "plan_fingerprint": plan.fingerprint,
        "acceptance_fingerprint": acceptance.fingerprint,
        "candidate_fingerprint": _candidate_fingerprint(candidate),
        "manifest_fingerprint": manifest.fingerprint,
        "adapter_id": candidate.adapter_id,
        "adapter_version": candidate.adapter_version,
        "model_id": candidate.model_id,
        "device_kind": device.device.value,
        "dtype": device.dtype,
        "host_profile": manifest.host_profile,
    }
    actual = {field_name: getattr(receipt, field_name) for field_name in expected}
    if actual != expected:
        raise VisualQualificationError("visual qualification receipt identity mismatch")


def bind_visual_qualification(
    plan: VisualBenchmarkPlan,
    acceptance: VisualAcceptanceReport,
    manifest: ModelManifest,
    receipt: VisualQualificationReceipt,
    *,
    current_time_utc: str,
) -> VisualQualificationBinding:
    """Validate receipt integrity, freshness, and all frozen identities for runtime admission."""

    return VisualQualificationBinding(
        plan=plan,
        acceptance=acceptance,
        manifest=manifest,
        receipt=receipt,
        validated_at_utc=current_time_utc,
    )


__all__ = [
    "MAX_VISUAL_QUALIFICATION_TTL_SECONDS",
    "VISUAL_QUALIFICATION_SCHEMA",
    "VisualQualificationBinding",
    "VisualQualificationReceipt",
    "bind_visual_qualification",
    "create_visual_qualification_receipt",
    "validate_visual_qualification_admission",
]
