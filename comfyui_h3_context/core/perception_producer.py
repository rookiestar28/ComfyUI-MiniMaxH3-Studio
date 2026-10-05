"""Typed public envelopes for the M15 media and perception producer nodes.

The portable projection excludes host-owned media values. Browser-authored identity and metadata
remain explicitly caller-declared; module-owned admission authority binds those declarations to
the exact runtime payload without upgrading them to trusted-loader provenance.
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import ContractValidationError

PERCEPTION_PRODUCER_SCHEMA = "h3.perception_producer.v1"
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REFERENCE_ROLES = frozenset({"input", "reference", "first_frame", "last_frame", "paired_audio"})


class ProducerKind(str, Enum):
    MEDIA = "media"
    VISUAL = "visual"
    AUDIO = "audio"


class ProducerRoute(str, Enum):
    MANUAL_HOST = "manual_host"
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class ProducerDisposition(str, Enum):
    COMPLETE = "complete"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


class FingerprintClaim(str, Enum):
    """Evidence ceiling for a browser-authored digest."""

    CALLER_DECLARED_UNVERIFIED = "caller_declared_unverified"


class ProducerReasonCode(str, Enum):
    HOST_MEDIA_ADMITTED = "host_media_admitted"
    HOST_MEDIA_INPUT_MISMATCH = "host_media_input_mismatch"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    PROFILE_UNSUPPORTED = "profile_unsupported"
    CANCELLED_BEFORE_EXECUTION = "cancelled_before_execution"
    EXECUTION_FAILED = "execution_failed"


_REASON_MESSAGES = {
    ProducerReasonCode.HOST_MEDIA_ADMITTED: "host media admitted",
    ProducerReasonCode.HOST_MEDIA_INPUT_MISMATCH: (
        "exactly one matching host media input is required"
    ),
    ProducerReasonCode.PROFILE_UNAVAILABLE: (
        "selected profile has no qualified production adapter"
    ),
    ProducerReasonCode.PROFILE_UNSUPPORTED: "selected profile is not supported",
    ProducerReasonCode.CANCELLED_BEFORE_EXECUTION: "cancelled before profile execution",
    ProducerReasonCode.EXECUTION_FAILED: "selected profile execution failed",
}


def _exact_enum(value: object, expected: type[Enum], field_name: str) -> None:
    if type(value) is not expected:
        raise ContractValidationError(f"{field_name} must be an exact {expected.__name__}")


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a bounded public identifier")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ContractValidationError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _bounded_int(value: object, field_name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractValidationError(f"{field_name} is outside its integer bound")
    return value


def _bounded_float(value: object, field_name: str, minimum: float, maximum: float) -> float:
    if type(value) is not float or not minimum <= value <= maximum:
        raise ContractValidationError(f"{field_name} is outside its finite float bound")
    return value


def execution_fingerprint(*parts: str) -> str:
    """Fingerprint bounded public execution identity without inspecting private media."""

    if not parts or any(type(item) is not str or len(item) > 256 for item in parts):
        raise ContractValidationError("execution fingerprint parts must be bounded strings")
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MediaAdmissionEvidence:
    """Bounded caller declarations bound to one exact host payload at admission time."""

    declared_source_fingerprint: str
    width_pixels: int | None
    height_pixels: int | None
    duration_seconds: float
    sample_rate_hz: int | None
    channel_count: int | None
    reference_role: str
    reference_order: int
    fingerprint_claim: FingerprintClaim = FingerprintClaim.CALLER_DECLARED_UNVERIFIED

    def __post_init__(self) -> None:
        if type(self) is not MediaAdmissionEvidence:
            raise ContractValidationError("admission evidence must be an exact concrete value")
        _fingerprint(self.declared_source_fingerprint, "declared_source_fingerprint")
        _exact_enum(self.fingerprint_claim, FingerprintClaim, "fingerprint_claim")
        if self.width_pixels is not None:
            _bounded_int(self.width_pixels, "width_pixels", 1, 32768)
        if self.height_pixels is not None:
            _bounded_int(self.height_pixels, "height_pixels", 1, 32768)
        _bounded_float(self.duration_seconds, "duration_seconds", 0.0, 86400.0)
        if self.sample_rate_hz is not None:
            _bounded_int(self.sample_rate_hz, "sample_rate_hz", 1, 768000)
        if self.channel_count is not None:
            _bounded_int(self.channel_count, "channel_count", 1, 64)
        if type(self.reference_role) is not str or self.reference_role not in _REFERENCE_ROLES:
            raise ContractValidationError("reference_role is not supported")
        _bounded_int(self.reference_order, "reference_order", 0, 63)

    def assert_for_media_kind(self, media_kind: str) -> None:
        self.__post_init__()
        if media_kind == "image":
            if (
                self.width_pixels is None
                or self.height_pixels is None
                or self.duration_seconds != 0.0
                or self.sample_rate_hz is not None
                or self.channel_count is not None
            ):
                raise ContractValidationError("image admission metadata is inconsistent")
        elif media_kind == "video":
            if (
                self.width_pixels is None
                or self.height_pixels is None
                or self.duration_seconds <= 0.0
                or self.sample_rate_hz is not None
                or self.channel_count is not None
            ):
                raise ContractValidationError("video admission metadata is inconsistent")
        elif media_kind == "audio":
            if (
                self.width_pixels is not None
                or self.height_pixels is not None
                or self.duration_seconds <= 0.0
                or self.sample_rate_hz is None
                or self.channel_count is None
            ):
                raise ContractValidationError("audio admission metadata is inconsistent")
        else:
            raise ContractValidationError("media_kind must be image, video, or audio")

    def to_wire(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "declared_source_fingerprint": self.declared_source_fingerprint,
            "fingerprint_claim": self.fingerprint_claim.value,
            "width_pixels": self.width_pixels,
            "height_pixels": self.height_pixels,
            "duration_seconds": self.duration_seconds,
            "sample_rate_hz": self.sample_rate_hz,
            "channel_count": self.channel_count,
            "reference_role": self.reference_role,
            "reference_order": self.reference_order,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProducerComponentReceipt:
    component_type: str
    component_fingerprint: str

    def __post_init__(self) -> None:
        if type(self) is not ProducerComponentReceipt:
            raise ContractValidationError("component receipt must be an exact concrete value")
        _identifier(self.component_type, "component_type")
        _fingerprint(self.component_fingerprint, "component_fingerprint")

    def to_wire(self) -> dict[str, str]:
        self.__post_init__()
        return {
            "component_type": self.component_type,
            "component_fingerprint": self.component_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ProducerReceipt:
    route: ProducerRoute
    profile_id: str
    backend_id: str
    model_id: str
    device: str
    executed: bool
    cleanup_succeeded: bool
    progress_fraction: float
    execution_fingerprint: str

    def __post_init__(self) -> None:
        if type(self) is not ProducerReceipt:
            raise ContractValidationError("producer receipt must be an exact concrete value")
        _exact_enum(self.route, ProducerRoute, "route")
        for value, name in (
            (self.profile_id, "profile_id"),
            (self.backend_id, "backend_id"),
            (self.model_id, "model_id"),
            (self.device, "device"),
        ):
            _identifier(value, name)
        if type(self.executed) is not bool or type(self.cleanup_succeeded) is not bool:
            raise ContractValidationError("receipt execution and cleanup flags must be booleans")
        _fingerprint(self.execution_fingerprint, "execution_fingerprint")
        _bounded_float(self.progress_fraction, "progress_fraction", 0.0, 1.0)
        if self.executed and not self.cleanup_succeeded:
            raise ContractValidationError("completed execution requires successful cleanup")
        if self.executed != (self.progress_fraction == 1.0):
            raise ContractValidationError("execution state and progress are inconsistent")

    def to_wire(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "route": self.route.value,
            "profile_id": self.profile_id,
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "device": self.device,
            "executed": self.executed,
            "cleanup_succeeded": self.cleanup_succeeded,
            "progress_fraction": self.progress_fraction,
            "execution_fingerprint": self.execution_fingerprint,
        }


@dataclass(frozen=True, eq=False)
class PerceptionProducerResult:
    kind: ProducerKind
    disposition: ProducerDisposition
    route: ProducerRoute
    profile_id: str
    asset_id: str
    admission_evidence: MediaAdmissionEvidence
    media_kind: str
    components: tuple[ProducerComponentReceipt, ...]
    receipt: ProducerReceipt
    reason_code: ProducerReasonCode
    runtime_payload: object | None = field(default=None, repr=False, compare=False)
    schema: str = PERCEPTION_PRODUCER_SCHEMA

    def __post_init__(self) -> None:
        if type(self) is not PerceptionProducerResult:
            raise ContractValidationError("producer result must be an exact concrete value")
        if type(self.schema) is not str or self.schema != PERCEPTION_PRODUCER_SCHEMA:
            raise ContractValidationError("producer schema is unsupported")
        _exact_enum(self.kind, ProducerKind, "kind")
        _exact_enum(self.disposition, ProducerDisposition, "disposition")
        _exact_enum(self.route, ProducerRoute, "route")
        _exact_enum(self.reason_code, ProducerReasonCode, "reason_code")
        _identifier(self.profile_id, "profile_id")
        _identifier(self.asset_id, "asset_id")
        if type(self.admission_evidence) is not MediaAdmissionEvidence:
            raise ContractValidationError("admission_evidence must be exact")
        self.admission_evidence.assert_for_media_kind(self.media_kind)
        if type(self.components) is not tuple or any(
            type(item) is not ProducerComponentReceipt for item in self.components
        ):
            raise ContractValidationError("components must contain exact component receipts")
        if len(self.components) > 64:
            raise ContractValidationError("components exceed the bounded public inventory")
        if type(self.receipt) is not ProducerReceipt:
            raise ContractValidationError("receipt must be an exact ProducerReceipt")
        self.receipt.__post_init__()
        if self.receipt.route is not self.route or self.receipt.profile_id != self.profile_id:
            raise ContractValidationError("producer result and receipt route/profile do not match")
        if self.kind is ProducerKind.MEDIA:
            if (
                self.route is not ProducerRoute.MANUAL_HOST
                or self.profile_id != "host_media_admission_v1"
            ):
                raise ContractValidationError("media admission requires the manual host profile")
            if (
                self.receipt.backend_id != "comfyui_host"
                or self.receipt.model_id != "not_applicable"
                or self.receipt.device != "host_owned"
            ):
                raise ContractValidationError("media admission receipt identity is inconsistent")
        elif self.route is ProducerRoute.MANUAL_HOST:
            raise ContractValidationError("perception results require an execution route")
        elif self.receipt.backend_id != self.route.value or self.receipt.model_id != "not_resolved":
            raise ContractValidationError("perception receipt identity is inconsistent")
        if not self.receipt.cleanup_succeeded:
            raise ContractValidationError("producer result requires successful cleanup")
        if self.kind is ProducerKind.AUDIO and self.media_kind != "audio":
            raise ContractValidationError("audio perception requires admitted audio")
        if self.kind is ProducerKind.VISUAL and self.media_kind not in {"image", "video"}:
            raise ContractValidationError("visual perception requires image or video")
        expected_reason = {
            ProducerDisposition.COMPLETE: ProducerReasonCode.HOST_MEDIA_ADMITTED,
            ProducerDisposition.REJECTED: ProducerReasonCode.HOST_MEDIA_INPUT_MISMATCH,
            ProducerDisposition.UNAVAILABLE: ProducerReasonCode.PROFILE_UNAVAILABLE,
            ProducerDisposition.UNSUPPORTED: ProducerReasonCode.PROFILE_UNSUPPORTED,
            ProducerDisposition.CANCELLED: ProducerReasonCode.CANCELLED_BEFORE_EXECUTION,
            ProducerDisposition.FAILED: ProducerReasonCode.EXECUTION_FAILED,
        }.get(self.disposition)
        if expected_reason is None or self.reason_code is not expected_reason:
            raise ContractValidationError("disposition and reason_code do not match")
        if self.disposition is ProducerDisposition.COMPLETE:
            if self.kind is not ProducerKind.MEDIA:
                raise ContractValidationError("no visual or audio production profile is qualified")
            if not self.components or self.runtime_payload is None or not self.receipt.executed:
                raise ContractValidationError("complete producer results require executed evidence")
            expected_component = f"admitted_{self.media_kind}"
            if len(self.components) != 1 or self.components[0].component_type != expected_component:
                raise ContractValidationError("media admission component inventory is inconsistent")
        elif self.components or self.runtime_payload is not None or self.receipt.executed:
            raise ContractValidationError("non-complete producer results must not contain evidence")
        expected_execution = _expected_execution_fingerprint(self)
        if self.receipt.execution_fingerprint != expected_execution:
            raise ContractValidationError("producer execution fingerprint is inconsistent")
        if self.components:
            expected_component_fp = execution_fingerprint(
                self.admission_evidence.fingerprint, self.media_kind, "admitted"
            )
            if self.components[0].component_fingerprint != expected_component_fp:
                raise ContractValidationError("producer component fingerprint is inconsistent")

    @property
    def component_types(self) -> tuple[str, ...]:
        _assert_result_current(self)
        return tuple(item.component_type for item in self.components)

    @property
    def reason(self) -> str:
        _assert_result_current(self)
        return _REASON_MESSAGES[self.reason_code]

    def to_wire(self) -> dict[str, object]:
        _assert_result_current(self)
        return {
            "schema": self.schema,
            "kind": self.kind.value,
            "disposition": self.disposition.value,
            "route": self.route.value,
            "profile_id": self.profile_id,
            "asset_id": self.asset_id,
            "admission_evidence": self.admission_evidence.to_wire(),
            "media_kind": self.media_kind,
            "components": [item.to_wire() for item in self.components],
            "receipt": self.receipt.to_wire(),
            "reason_code": self.reason_code.value,
            "reason": _REASON_MESSAGES[self.reason_code],
        }

    def assert_current(self) -> None:
        _assert_result_current(self)


_TRUSTED_COMPLETE: weakref.WeakKeyDictionary[PerceptionProducerResult, tuple[int, str]] = (
    weakref.WeakKeyDictionary()
)


def _assert_result_current(result: object) -> None:
    """Validate authority without dispatching caller-overridable instance methods."""

    if type(result) is not PerceptionProducerResult:
        raise ContractValidationError("producer result must be an exact concrete value")
    PerceptionProducerResult.__post_init__(result)
    if result.disposition is ProducerDisposition.COMPLETE:
        trusted = _TRUSTED_COMPLETE.get(result)
        current = (id(result.runtime_payload), _result_authority_fingerprint(result))
        if trusted is None or trusted != current:
            raise ContractValidationError("complete producer result is not current admission")


def _expected_execution_fingerprint(result: PerceptionProducerResult) -> str:
    parts = [
        result.kind.value,
        result.route.value,
        result.profile_id,
        result.asset_id,
        result.admission_evidence.fingerprint,
        result.media_kind,
    ]
    if result.kind is not ProducerKind.MEDIA:
        parts.append(result.receipt.device)
    return execution_fingerprint(*parts)


def _result_authority_fingerprint(result: PerceptionProducerResult) -> str:
    payload = {
        "schema": result.schema,
        "kind": result.kind.value,
        "disposition": result.disposition.value,
        "route": result.route.value,
        "profile_id": result.profile_id,
        "asset_id": result.asset_id,
        "admission_evidence": result.admission_evidence.to_wire(),
        "media_kind": result.media_kind,
        "components": [item.to_wire() for item in result.components],
        "receipt": result.receipt.to_wire(),
        "reason_code": result.reason_code.value,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _trust_complete(result: PerceptionProducerResult) -> PerceptionProducerResult:
    if result.disposition is not ProducerDisposition.COMPLETE or result.runtime_payload is None:
        raise ContractValidationError(
            "only complete runtime results may receive admission authority"
        )
    _TRUSTED_COMPLETE[result] = (id(result.runtime_payload), _result_authority_fingerprint(result))
    _assert_result_current(result)
    return result


def admit_host_media(
    *,
    media_kind: str,
    asset_id: str,
    admission_evidence: MediaAdmissionEvidence,
    image: object | None = None,
    video: object | None = None,
    audio: object | None = None,
) -> PerceptionProducerResult:
    if type(admission_evidence) is not MediaAdmissionEvidence:
        raise ContractValidationError("admission_evidence must be exact")
    admission_evidence.assert_for_media_kind(media_kind)
    _identifier(asset_id, "asset_id")
    supplied = {"image": image, "video": video, "audio": audio}
    selected = supplied[media_kind]
    matches = selected is not None and sum(value is not None for value in supplied.values()) == 1
    route = ProducerRoute.MANUAL_HOST
    profile_id = "host_media_admission_v1"
    run_fingerprint = execution_fingerprint(
        ProducerKind.MEDIA.value,
        route.value,
        profile_id,
        asset_id,
        admission_evidence.fingerprint,
        media_kind,
    )
    receipt = ProducerReceipt(
        route=route,
        profile_id=profile_id,
        backend_id="comfyui_host",
        model_id="not_applicable",
        device="host_owned",
        executed=matches,
        cleanup_succeeded=True,
        progress_fraction=1.0 if matches else 0.0,
        execution_fingerprint=run_fingerprint,
    )
    components = (
        (
            ProducerComponentReceipt(
                component_type=f"admitted_{media_kind}",
                component_fingerprint=execution_fingerprint(
                    admission_evidence.fingerprint, media_kind, "admitted"
                ),
            ),
        )
        if matches
        else ()
    )
    result = PerceptionProducerResult(
        kind=ProducerKind.MEDIA,
        disposition=ProducerDisposition.COMPLETE if matches else ProducerDisposition.REJECTED,
        route=route,
        profile_id=profile_id,
        asset_id=asset_id,
        admission_evidence=admission_evidence,
        media_kind=media_kind,
        components=components,
        receipt=receipt,
        reason_code=(
            ProducerReasonCode.HOST_MEDIA_ADMITTED
            if matches
            else ProducerReasonCode.HOST_MEDIA_INPUT_MISMATCH
        ),
        runtime_payload=selected if matches else None,
    )
    return _trust_complete(result) if matches else result


def declare_unqualified_perception(
    media: PerceptionProducerResult,
    *,
    kind: ProducerKind,
    route: ProducerRoute | str,
    profile_id: str,
    device: str,
    cancel_requested: bool,
) -> PerceptionProducerResult:
    if type(media) is not PerceptionProducerResult:
        raise ContractValidationError("media must be an exact producer result")
    _assert_result_current(media)
    if (
        media.kind is not ProducerKind.MEDIA
        or media.disposition is not ProducerDisposition.COMPLETE
    ):
        raise ContractValidationError("perception requires a complete admitted media result")
    _exact_enum(kind, ProducerKind, "kind")
    if kind is ProducerKind.MEDIA:
        raise ContractValidationError("perception kind must be visual or audio")
    try:
        selected_route = route if type(route) is ProducerRoute else ProducerRoute(route)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("route is not supported") from exc
    if selected_route not in {
        ProducerRoute.COMFYUI_NATIVE,
        ProducerRoute.OLLAMA,
        ProducerRoute.SPECIALIST,
    }:
        raise ContractValidationError("route is not a perception execution route")
    _identifier(profile_id, "profile_id")
    if type(device) is not str or device not in {"auto", "cpu", "cuda", "mps"}:
        raise ContractValidationError("device is not supported")
    if type(cancel_requested) is not bool:
        raise ContractValidationError("cancel_requested must be a boolean")
    run_fingerprint = execution_fingerprint(
        kind.value,
        selected_route.value,
        profile_id,
        media.asset_id,
        media.admission_evidence.fingerprint,
        media.media_kind,
        device,
    )
    disposition = (
        ProducerDisposition.CANCELLED if cancel_requested else ProducerDisposition.UNAVAILABLE
    )
    return PerceptionProducerResult(
        kind=kind,
        disposition=disposition,
        route=selected_route,
        profile_id=profile_id,
        asset_id=media.asset_id,
        admission_evidence=media.admission_evidence,
        media_kind=media.media_kind,
        components=(),
        receipt=ProducerReceipt(
            route=selected_route,
            profile_id=profile_id,
            backend_id=selected_route.value,
            model_id="not_resolved",
            device=device,
            executed=False,
            cleanup_succeeded=True,
            progress_fraction=0.0,
            execution_fingerprint=run_fingerprint,
        ),
        reason_code=(
            ProducerReasonCode.CANCELLED_BEFORE_EXECUTION
            if cancel_requested
            else ProducerReasonCode.PROFILE_UNAVAILABLE
        ),
    )


def _exact_object(value: object, keys: set[str], field_name: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ContractValidationError(f"{field_name} must be an exact closed object")
    return value


def validate_perception_producer_wire(value: object) -> None:
    """Validate the semantic stage that JSON Schema cannot express."""

    try:
        root = _exact_object(
            value,
            {
                "schema",
                "kind",
                "disposition",
                "route",
                "profile_id",
                "asset_id",
                "admission_evidence",
                "media_kind",
                "components",
                "receipt",
                "reason_code",
                "reason",
            },
            "producer wire",
        )
        evidence_wire = _exact_object(
            root["admission_evidence"],
            {
                "declared_source_fingerprint",
                "fingerprint_claim",
                "width_pixels",
                "height_pixels",
                "duration_seconds",
                "sample_rate_hz",
                "channel_count",
                "reference_role",
                "reference_order",
            },
            "admission_evidence",
        )
        evidence = MediaAdmissionEvidence(
            declared_source_fingerprint=evidence_wire["declared_source_fingerprint"],
            fingerprint_claim=FingerprintClaim(evidence_wire["fingerprint_claim"]),
            width_pixels=evidence_wire["width_pixels"],
            height_pixels=evidence_wire["height_pixels"],
            duration_seconds=evidence_wire["duration_seconds"],
            sample_rate_hz=evidence_wire["sample_rate_hz"],
            channel_count=evidence_wire["channel_count"],
            reference_role=evidence_wire["reference_role"],
            reference_order=evidence_wire["reference_order"],
        )
        component_values = root["components"]
        if type(component_values) is not list:
            raise ContractValidationError("components must be an exact array")
        components = tuple(
            ProducerComponentReceipt(
                **_exact_object(item, {"component_type", "component_fingerprint"}, "component")
            )
            for item in component_values
        )
        receipt_wire = _exact_object(
            root["receipt"],
            {
                "route",
                "profile_id",
                "backend_id",
                "model_id",
                "device",
                "executed",
                "cleanup_succeeded",
                "progress_fraction",
                "execution_fingerprint",
            },
            "receipt",
        )
        receipt = ProducerReceipt(
            route=ProducerRoute(receipt_wire["route"]),
            profile_id=receipt_wire["profile_id"],
            backend_id=receipt_wire["backend_id"],
            model_id=receipt_wire["model_id"],
            device=receipt_wire["device"],
            executed=receipt_wire["executed"],
            cleanup_succeeded=receipt_wire["cleanup_succeeded"],
            progress_fraction=receipt_wire["progress_fraction"],
            execution_fingerprint=receipt_wire["execution_fingerprint"],
        )
        reason_code = ProducerReasonCode(root["reason_code"])
        if root["reason"] != _REASON_MESSAGES[reason_code]:
            raise ContractValidationError("reason does not match reason_code")
        disposition = ProducerDisposition(root["disposition"])
        candidate = PerceptionProducerResult(
            schema=root["schema"],
            kind=ProducerKind(root["kind"]),
            disposition=disposition,
            route=ProducerRoute(root["route"]),
            profile_id=root["profile_id"],
            asset_id=root["asset_id"],
            admission_evidence=evidence,
            media_kind=root["media_kind"],
            components=components,
            receipt=receipt,
            reason_code=reason_code,
            runtime_payload=object() if disposition is ProducerDisposition.COMPLETE else None,
        )
        candidate.__post_init__()
    except ContractValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractValidationError("producer wire is malformed") from exc


__all__ = [
    "PERCEPTION_PRODUCER_SCHEMA",
    "FingerprintClaim",
    "MediaAdmissionEvidence",
    "PerceptionProducerResult",
    "ProducerComponentReceipt",
    "ProducerDisposition",
    "ProducerKind",
    "ProducerReasonCode",
    "ProducerReceipt",
    "ProducerRoute",
    "admit_host_media",
    "declare_unqualified_perception",
    "execution_fingerprint",
    "validate_perception_producer_wire",
]
