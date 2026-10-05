"""Frozen visual-perception benchmark and adapter-selection contracts for M11-01.

The values in this module are planning and evidence-boundary metadata.  They do not discover
models, open media, call ComfyUI/Ollama, or turn a transport label into a capability claim.  A
candidate is executable evidence only after its explicit profile has a ``qualified`` disposition;
the default M11-01 plan intentionally contains no qualified live profile.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_fingerprint
from .contracts import EvidenceLevel
from .errors import VisualBenchmarkError

VISUAL_BENCHMARK_SCHEMA = "h3.visual.benchmark.v1"
MAX_VISUAL_FIXTURES = 64
MAX_VISUAL_CANDIDATES = 32
MAX_VISUAL_METRICS = 512
MAX_VISUAL_DEVICE_PROFILES = 16
MAX_VISUAL_CAPABILITIES_PER_ITEM = 32
MAX_VISUAL_TAGS = 32
MAX_VISUAL_SOURCE_REFS = 32

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
_SENSITIVE_MARKERS = (
    "http://",
    "https://",
    "file://",
    "\\\\",
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
    "signed",
)


class VisualCapability(str, Enum):
    """Closed capability vocabulary frozen for the visual benchmark."""

    GLOBAL_DESCRIPTION = "global_description"
    OCR = "ocr"
    SUBJECTS_OBJECTS = "subjects_objects"
    IDENTITY = "identity"
    SHOTS = "shots"
    ACTIONS = "actions"
    CAMERA = "camera"
    MOTION = "motion"
    STYLE = "style"
    EDITS = "edits"
    TIMESTAMPS = "timestamps"
    CONFIDENCE = "confidence"
    CORRUPTION = "corruption"
    ADVERSARIAL_TEXT = "adversarial_text"


class VisualModality(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


class VisualPrivacyClass(str, Enum):
    SYNTHETIC = "synthetic"
    PUBLIC = "public"


class VisualTimestampMode(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    CFR = "cfr"
    VFR = "vfr"


class VisualCandidateFamily(str, Enum):
    COMFYUI_NATIVE = "comfyui_native"
    OLLAMA = "ollama"
    SPECIALIST = "specialist"


class VisualDisposition(str, Enum):
    """Terminal profile outcome; only QUALIFIED permits executable evidence."""

    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class VisualDeviceKind(str, Enum):
    CPU = "cpu"
    CUDA = "cuda"
    MPS = "mps"
    AUTO = "auto"


class VisualMetricKind(str, Enum):
    ACCURACY = "accuracy"
    CALIBRATION = "calibration"
    LATENCY = "latency"
    VRAM = "vram"
    RAM = "ram"
    DETERMINISM = "determinism"
    PLATFORM_SUPPORT = "platform_support"


class VisualMetricUnit(str, Enum):
    BASIS_POINTS = "basis_points"
    MILLISECONDS = "milliseconds"
    MEBIBYTES = "mebibytes"
    BOOLEAN = "boolean"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise VisualBenchmarkError(f"{field_name} must be a bounded identifier")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise VisualBenchmarkError(f"{field_name} contains sensitive or locator material")
    return value


def _code(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise VisualBenchmarkError(f"{field_name} must be a lower-case bounded code")
    return value.casefold()


def _version(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise VisualBenchmarkError(f"{field_name} must be a numeric version")
    return value


def _text(value: object, field_name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise VisualBenchmarkError(f"{field_name} must be bounded non-empty text")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise VisualBenchmarkError(f"{field_name} contains sensitive or locator material")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise VisualBenchmarkError(f"{field_name} contains a control character")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise VisualBenchmarkError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise VisualBenchmarkError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VisualBenchmarkError(f"{field_name} must be a non-negative integer")
    return value


def _enum(value: object, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise VisualBenchmarkError(f"{field_name} must be a {expected.__name__}")


def _unique_enum_values(
    values: object,
    expected: type[Enum],
    field_name: str,
    maximum: int = MAX_VISUAL_CAPABILITIES_PER_ITEM,
) -> tuple[Enum, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise VisualBenchmarkError(f"{field_name} must be a bounded tuple")
    for value in values:
        _enum(value, expected, f"{field_name} item")
    if len(values) != len(set(values)):
        raise VisualBenchmarkError(f"{field_name} must not contain duplicates")
    return values


def _unique_strings(
    values: object, field_name: str, maximum: int = MAX_VISUAL_SOURCE_REFS
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise VisualBenchmarkError(f"{field_name} must be a bounded tuple")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise VisualBenchmarkError(f"{field_name} must not contain duplicates")
    return result


def _require_instances(values: Iterable[object], expected: type[object], field_name: str) -> None:
    if not all(isinstance(value, expected) for value in values):
        raise VisualBenchmarkError(f"{field_name} contains an invalid value")


@dataclass(frozen=True, slots=True)
class VisualFixture:
    """Redacted fixture identity and capability coverage; raw media never crosses this seam."""

    fixture_id: str
    modality: VisualModality
    capabilities: tuple[VisualCapability, ...]
    source_fingerprint: str
    annotation_fingerprint: str
    privacy_class: VisualPrivacyClass
    timestamp_mode: VisualTimestampMode
    tags: tuple[str, ...] = ()
    adversarial_text: bool = False
    corrupt_media: bool = False
    rotation_degrees: int = 0
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.fixture_id, "fixture_id")
        _enum(self.modality, VisualModality, "fixture modality")
        _unique_enum_values(self.capabilities, VisualCapability, "fixture capabilities")
        if not self.capabilities:
            raise VisualBenchmarkError("fixture must cover at least one capability")
        _fingerprint(self.source_fingerprint, "fixture source_fingerprint")
        _fingerprint(self.annotation_fingerprint, "fixture annotation_fingerprint")
        _enum(self.privacy_class, VisualPrivacyClass, "fixture privacy_class")
        _enum(self.timestamp_mode, VisualTimestampMode, "fixture timestamp_mode")
        if not isinstance(self.tags, tuple) or len(self.tags) > MAX_VISUAL_TAGS:
            raise VisualBenchmarkError("fixture tags must be a bounded tuple")
        for tag in self.tags:
            _code(tag, "fixture tag")
        if not isinstance(self.adversarial_text, bool) or not isinstance(self.corrupt_media, bool):
            raise VisualBenchmarkError("fixture adversarial/corrupt flags must be booleans")
        if (
            isinstance(self.rotation_degrees, bool)
            or not isinstance(self.rotation_degrees, int)
            or self.rotation_degrees not in (0, 90, 180, 270)
        ):
            raise VisualBenchmarkError("fixture rotation_degrees must be 0, 90, 180, or 270")
        if (
            self.modality is VisualModality.IMAGE
            and self.timestamp_mode is not VisualTimestampMode.NOT_APPLICABLE
        ):
            raise VisualBenchmarkError("image fixture timestamp_mode must be not_applicable")
        if (
            self.modality is VisualModality.VIDEO
            and self.timestamp_mode is VisualTimestampMode.NOT_APPLICABLE
        ):
            raise VisualBenchmarkError("video fixture must declare CFR or VFR timestamps")
        if self.adversarial_text and VisualCapability.ADVERSARIAL_TEXT not in self.capabilities:
            raise VisualBenchmarkError("adversarial_text fixture must cover adversarial_text")
        if self.corrupt_media and VisualCapability.CORRUPTION not in self.capabilities:
            raise VisualBenchmarkError("corrupt fixture must cover corruption")
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "fixture_id": self.fixture_id,
            "modality": self.modality.value,
            "capabilities": [item.value for item in self.capabilities],
            "source_fingerprint": self.source_fingerprint,
            "annotation_fingerprint": self.annotation_fingerprint,
            "privacy_class": self.privacy_class.value,
            "timestamp_mode": self.timestamp_mode.value,
            "tags": list(self.tags),
            "adversarial_text": self.adversarial_text,
            "corrupt_media": self.corrupt_media,
            "rotation_degrees": self.rotation_degrees,
        }


@dataclass(frozen=True, slots=True)
class VisualMetricThreshold:
    """One frozen per-capability or global qualification threshold."""

    metric_id: str
    metric: VisualMetricKind
    unit: VisualMetricUnit
    capability: VisualCapability | None
    required: bool
    minimum: int | None = None
    maximum: int | None = None
    evaluation_set: str = "visual.core.v1"
    description: str = "frozen benchmark threshold"
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.metric_id, "metric_id")
        _enum(self.metric, VisualMetricKind, "metric")
        _enum(self.unit, VisualMetricUnit, "metric unit")
        if self.capability is not None:
            _enum(self.capability, VisualCapability, "metric capability")
        if not isinstance(self.required, bool):
            raise VisualBenchmarkError("metric required must be boolean")
        if self.minimum is None and self.maximum is None:
            raise VisualBenchmarkError("metric threshold must define minimum or maximum")
        if self.minimum is not None:
            _non_negative_int(self.minimum, "metric minimum")
        if self.maximum is not None:
            _non_negative_int(self.maximum, "metric maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise VisualBenchmarkError("metric minimum cannot exceed maximum")
        _code(self.evaluation_set, "metric evaluation_set")
        _text(self.description, "metric description")
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "metric_id": self.metric_id,
            "metric": self.metric.value,
            "unit": self.unit.value,
            "capability": self.capability.value if self.capability is not None else None,
            "required": self.required,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "evaluation_set": self.evaluation_set,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class VisualDeviceProfile:
    """Finite device envelope used by a future executable adapter profile."""

    profile_id: str
    platform: str
    device: VisualDeviceKind
    dtype: str
    timeout_seconds: int
    max_media_items: int
    max_output_bytes: int
    peak_vram_mb_limit: int
    peak_ram_mb_limit: int
    supports_determinism: bool
    supports_cancellation_cleanup: bool
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "device profile_id")
        _code(self.platform, "device platform")
        _enum(self.device, VisualDeviceKind, "device kind")
        _code(self.dtype, "device dtype")
        _positive_int(self.timeout_seconds, "device timeout_seconds")
        _positive_int(self.max_media_items, "device max_media_items")
        _positive_int(self.max_output_bytes, "device max_output_bytes")
        _positive_int(self.peak_vram_mb_limit, "device peak_vram_mb_limit")
        _positive_int(self.peak_ram_mb_limit, "device peak_ram_mb_limit")
        if not isinstance(self.supports_determinism, bool) or not isinstance(
            self.supports_cancellation_cleanup, bool
        ):
            raise VisualBenchmarkError("device support flags must be booleans")
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "platform": self.platform,
            "device": self.device.value,
            "dtype": self.dtype,
            "timeout_seconds": self.timeout_seconds,
            "max_media_items": self.max_media_items,
            "max_output_bytes": self.max_output_bytes,
            "peak_vram_mb_limit": self.peak_vram_mb_limit,
            "peak_ram_mb_limit": self.peak_ram_mb_limit,
            "supports_determinism": self.supports_determinism,
            "supports_cancellation_cleanup": self.supports_cancellation_cleanup,
        }


@dataclass(frozen=True, slots=True)
class VisualCapabilityMeasurement:
    """Optional measured result for one candidate/capability pair."""

    capability: VisualCapability
    accuracy_basis_points: int | None = None
    calibration_ece_basis_points: int | None = None
    latency_ms: int | None = None
    peak_vram_mb: int | None = None
    peak_ram_mb: int | None = None
    deterministic: bool | None = None
    platform_supported: bool | None = None
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.capability, VisualCapability, "measurement capability")
        for value, field_name in (
            (self.accuracy_basis_points, "measurement accuracy_basis_points"),
            (self.calibration_ece_basis_points, "measurement calibration_ece_basis_points"),
            (self.latency_ms, "measurement latency_ms"),
            (self.peak_vram_mb, "measurement peak_vram_mb"),
            (self.peak_ram_mb, "measurement peak_ram_mb"),
        ):
            if value is not None:
                _non_negative_int(value, field_name)
        if self.accuracy_basis_points is not None and self.accuracy_basis_points > 10_000:
            raise VisualBenchmarkError("measurement accuracy exceeds 100 percent")
        if (
            self.calibration_ece_basis_points is not None
            and self.calibration_ece_basis_points > 10_000
        ):
            raise VisualBenchmarkError("measurement calibration exceeds 100 percent")
        if not isinstance(self.deterministic, (bool, type(None))) or not isinstance(
            self.platform_supported, (bool, type(None))
        ):
            raise VisualBenchmarkError("measurement boolean fields must be bool or None")
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "capability": self.capability.value,
            "accuracy_basis_points": self.accuracy_basis_points,
            "calibration_ece_basis_points": self.calibration_ece_basis_points,
            "latency_ms": self.latency_ms,
            "peak_vram_mb": self.peak_vram_mb,
            "peak_ram_mb": self.peak_ram_mb,
            "deterministic": self.deterministic,
            "platform_supported": self.platform_supported,
        }


@dataclass(frozen=True, slots=True)
class VisualCandidateProfile:
    """Candidate adapter/profile declaration with an explicit terminal disposition."""

    candidate_id: str
    family: VisualCandidateFamily
    adapter_id: str
    adapter_version: str
    model_id: str
    license: str
    capabilities: tuple[VisualCapability, ...]
    device_profiles: tuple[VisualDeviceProfile, ...]
    privacy_mode: str
    requires_network: bool
    supports_determinism: bool
    supports_cancellation_cleanup: bool
    disposition: VisualDisposition
    disposition_reason: str
    evidence_level: EvidenceLevel
    source_refs: tuple[str, ...] = ()
    measurements: tuple[VisualCapabilityMeasurement, ...] = ()
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        _enum(self.family, VisualCandidateFamily, "candidate family")
        _code(self.adapter_id, "candidate adapter_id")
        _version(self.adapter_version, "candidate adapter_version")
        _identifier(self.model_id, "candidate model_id")
        _code(self.license, "candidate license")
        _unique_enum_values(self.capabilities, VisualCapability, "candidate capabilities")
        if not self.capabilities:
            raise VisualBenchmarkError("candidate must declare at least one capability")
        if not isinstance(self.device_profiles, tuple) or not self.device_profiles:
            raise VisualBenchmarkError("candidate must declare at least one device profile")
        if len(self.device_profiles) > MAX_VISUAL_DEVICE_PROFILES:
            raise VisualBenchmarkError("candidate device profiles exceed the finite bound")
        _require_instances(self.device_profiles, VisualDeviceProfile, "candidate device_profiles")
        device_ids = tuple(item.profile_id for item in self.device_profiles)
        if len(device_ids) != len(set(device_ids)):
            raise VisualBenchmarkError("candidate device profile IDs must be unique")
        _code(self.privacy_mode, "candidate privacy_mode")
        for value, field_name in (
            (self.requires_network, "candidate requires_network"),
            (self.supports_determinism, "candidate supports_determinism"),
            (self.supports_cancellation_cleanup, "candidate supports_cancellation_cleanup"),
        ):
            if not isinstance(value, bool):
                raise VisualBenchmarkError(f"{field_name} must be boolean")
        _enum(self.disposition, VisualDisposition, "candidate disposition")
        _text(self.disposition_reason, "candidate disposition_reason")
        _enum(self.evidence_level, EvidenceLevel, "candidate evidence_level")
        _unique_strings(self.source_refs, "candidate source_refs")
        if (
            not isinstance(self.measurements, tuple)
            or len(self.measurements) > MAX_VISUAL_CAPABILITIES_PER_ITEM
        ):
            raise VisualBenchmarkError("candidate measurements must be a bounded tuple")
        _require_instances(self.measurements, VisualCapabilityMeasurement, "candidate measurements")
        measurement_ids = tuple(item.capability for item in self.measurements)
        if len(measurement_ids) != len(set(measurement_ids)):
            raise VisualBenchmarkError("candidate measurement capabilities must be unique")
        if not set(measurement_ids).issubset(set(self.capabilities)):
            raise VisualBenchmarkError(
                "candidate measurements must belong to candidate capabilities"
            )
        if self.disposition is VisualDisposition.QUALIFIED:
            if set(measurement_ids) != set(self.capabilities):
                raise VisualBenchmarkError(
                    "qualified candidate needs a measurement for every capability"
                )
            if not self.supports_determinism or not self.supports_cancellation_cleanup:
                raise VisualBenchmarkError(
                    "qualified candidate must declare determinism and cleanup"
                )
        if self.family is VisualCandidateFamily.OLLAMA and not self.requires_network:
            raise VisualBenchmarkError(
                "Ollama candidate must explicitly declare its loopback transport"
            )
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "family": self.family.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "model_id": self.model_id,
            "license": self.license,
            "capabilities": [item.value for item in self.capabilities],
            "device_profiles": [item.to_wire() for item in self.device_profiles],
            "privacy_mode": self.privacy_mode,
            "requires_network": self.requires_network,
            "supports_determinism": self.supports_determinism,
            "supports_cancellation_cleanup": self.supports_cancellation_cleanup,
            "disposition": self.disposition.value,
            "disposition_reason": self.disposition_reason,
            "evidence_level": self.evidence_level.value,
            "source_refs": list(self.source_refs),
            "measurements": [item.to_wire() for item in self.measurements],
        }


@dataclass(frozen=True, slots=True)
class VisualBenchmarkLimits:
    """Frozen candidate, compute, media, and time envelope."""

    max_candidates: int
    max_fixtures: int
    max_wall_time_seconds: int
    max_total_compute_seconds: int
    max_peak_vram_mb: int
    max_peak_ram_mb: int
    max_media_items_per_case: int
    max_output_bytes: int
    max_concurrency: int
    network_allowed: bool
    media_upload_allowed: bool
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.max_candidates, "limits max_candidates"),
            (self.max_fixtures, "limits max_fixtures"),
            (self.max_wall_time_seconds, "limits max_wall_time_seconds"),
            (self.max_total_compute_seconds, "limits max_total_compute_seconds"),
            (self.max_peak_vram_mb, "limits max_peak_vram_mb"),
            (self.max_peak_ram_mb, "limits max_peak_ram_mb"),
            (self.max_media_items_per_case, "limits max_media_items_per_case"),
            (self.max_output_bytes, "limits max_output_bytes"),
            (self.max_concurrency, "limits max_concurrency"),
        ):
            _positive_int(value, field_name)
        if not isinstance(self.network_allowed, bool) or not isinstance(
            self.media_upload_allowed, bool
        ):
            raise VisualBenchmarkError("limits network/media flags must be booleans")
        if self.network_allowed or self.media_upload_allowed:
            raise VisualBenchmarkError(
                "M11-01 offline benchmark cannot allow network or media upload"
            )
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_candidates": self.max_candidates,
            "max_fixtures": self.max_fixtures,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "max_total_compute_seconds": self.max_total_compute_seconds,
            "max_peak_vram_mb": self.max_peak_vram_mb,
            "max_peak_ram_mb": self.max_peak_ram_mb,
            "max_media_items_per_case": self.max_media_items_per_case,
            "max_output_bytes": self.max_output_bytes,
            "max_concurrency": self.max_concurrency,
            "network_allowed": self.network_allowed,
            "media_upload_allowed": self.media_upload_allowed,
        }


@dataclass(frozen=True, slots=True)
class VisualStopRules:
    """Finite abort/rejection rules frozen before benchmark results."""

    max_schema_failures: int
    max_timeout_failures: int
    max_resource_failures: int
    max_adversarial_unsafe_outputs: int
    abort_on_privacy_violation: bool
    abort_on_hidden_fallback: bool
    reject_on_missing_required_metric: bool
    reject_on_unsupported_media: bool
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.max_schema_failures, "stop max_schema_failures"),
            (self.max_timeout_failures, "stop max_timeout_failures"),
            (self.max_resource_failures, "stop max_resource_failures"),
            (self.max_adversarial_unsafe_outputs, "stop max_adversarial_unsafe_outputs"),
        ):
            _non_negative_int(value, field_name)
        for value, field_name in (
            (self.abort_on_privacy_violation, "stop abort_on_privacy_violation"),
            (self.abort_on_hidden_fallback, "stop abort_on_hidden_fallback"),
            (self.reject_on_missing_required_metric, "stop reject_on_missing_required_metric"),
            (self.reject_on_unsupported_media, "stop reject_on_unsupported_media"),
        ):
            if not isinstance(value, bool):
                raise VisualBenchmarkError(f"{field_name} must be boolean")
        if not self.abort_on_hidden_fallback or not self.reject_on_missing_required_metric:
            raise VisualBenchmarkError(
                "M11-01 must stop hidden fallback and missing required metrics"
            )
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_schema_failures": self.max_schema_failures,
            "max_timeout_failures": self.max_timeout_failures,
            "max_resource_failures": self.max_resource_failures,
            "max_adversarial_unsafe_outputs": self.max_adversarial_unsafe_outputs,
            "abort_on_privacy_violation": self.abort_on_privacy_violation,
            "abort_on_hidden_fallback": self.abort_on_hidden_fallback,
            "reject_on_missing_required_metric": self.reject_on_missing_required_metric,
            "reject_on_unsupported_media": self.reject_on_unsupported_media,
        }


@dataclass(frozen=True, slots=True)
class VisualCapabilityPolicy:
    """Mandatory/optional release scope and consequences for missing qualification."""

    mandatory: tuple[VisualCapability, ...]
    optional: tuple[VisualCapability, ...]
    consequence: str
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _unique_enum_values(self.mandatory, VisualCapability, "policy mandatory")
        _unique_enum_values(self.optional, VisualCapability, "policy optional")
        if set(self.mandatory) & set(self.optional):
            raise VisualBenchmarkError("mandatory and optional capabilities must be disjoint")
        if set(self.mandatory) | set(self.optional) != set(VisualCapability):
            raise VisualBenchmarkError(
                "mandatory and optional capabilities must cover the vocabulary"
            )
        _text(self.consequence, "policy consequence", 1024)
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "mandatory": [item.value for item in self.mandatory],
            "optional": [item.value for item in self.optional],
            "consequence": self.consequence,
        }


@dataclass(frozen=True, slots=True)
class VisualRoutingPolicy:
    """Disclosed order without a fidelity winner or implicit substitution."""

    preference_order: tuple[VisualCandidateFamily, ...]
    automatic_fallback: bool
    explicit_selection_required: bool
    fidelity_winner_preselected: bool
    disclosure: str
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _unique_enum_values(
            self.preference_order, VisualCandidateFamily, "routing preference_order"
        )
        if self.preference_order != (
            VisualCandidateFamily.COMFYUI_NATIVE,
            VisualCandidateFamily.OLLAMA,
            VisualCandidateFamily.SPECIALIST,
        ):
            raise VisualBenchmarkError("routing order must disclose native, Ollama, specialist")
        if self.automatic_fallback:
            raise VisualBenchmarkError("automatic fallback is forbidden")
        if not self.explicit_selection_required:
            raise VisualBenchmarkError("provider/profile selection must be explicit")
        if self.fidelity_winner_preselected:
            raise VisualBenchmarkError("benchmark cannot preselect a fidelity winner")
        _text(self.disclosure, "routing disclosure", 1024)
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "preference_order": [item.value for item in self.preference_order],
            "automatic_fallback": self.automatic_fallback,
            "explicit_selection_required": self.explicit_selection_required,
            "fidelity_winner_preselected": self.fidelity_winner_preselected,
            "disclosure": self.disclosure,
        }


@dataclass(frozen=True, slots=True)
class VisualBenchmarkPlan:
    """Complete frozen M11-01 plan; construction performs all gate checks."""

    plan_id: str
    plan_version: str
    fixtures: tuple[VisualFixture, ...]
    metrics: tuple[VisualMetricThreshold, ...]
    candidates: tuple[VisualCandidateProfile, ...]
    limits: VisualBenchmarkLimits
    stop_rules: VisualStopRules
    capability_policy: VisualCapabilityPolicy
    routing: VisualRoutingPolicy
    source_refs: tuple[str, ...]
    schema: str = VISUAL_BENCHMARK_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.plan_id, "plan_id")
        _version(self.plan_version, "plan_version")
        if (
            not isinstance(self.fixtures, tuple)
            or not self.fixtures
            or len(self.fixtures) > MAX_VISUAL_FIXTURES
        ):
            raise VisualBenchmarkError("plan fixtures must be a bounded non-empty tuple")
        if (
            not isinstance(self.metrics, tuple)
            or not self.metrics
            or len(self.metrics) > MAX_VISUAL_METRICS
        ):
            raise VisualBenchmarkError("plan metrics must be a bounded non-empty tuple")
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or len(self.candidates) > MAX_VISUAL_CANDIDATES
        ):
            raise VisualBenchmarkError("plan candidates must be a bounded non-empty tuple")
        _require_instances(self.fixtures, VisualFixture, "plan fixtures")
        _require_instances(self.metrics, VisualMetricThreshold, "plan metrics")
        _require_instances(self.candidates, VisualCandidateProfile, "plan candidates")
        for value, field_name in (
            (self.limits, "plan limits"),
            (self.stop_rules, "plan stop_rules"),
            (self.capability_policy, "plan capability_policy"),
            (self.routing, "plan routing"),
        ):
            if not isinstance(
                value,
                (
                    VisualBenchmarkLimits,
                    VisualStopRules,
                    VisualCapabilityPolicy,
                    VisualRoutingPolicy,
                ),
            ):
                raise VisualBenchmarkError(f"{field_name} contains an invalid value")
        _unique_strings(self.source_refs, "plan source_refs")
        fixture_ids = tuple(item.fixture_id for item in self.fixtures)
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        metric_ids = tuple(item.metric_id for item in self.metrics)
        for values, field_name in (
            (fixture_ids, "fixture IDs"),
            (candidate_ids, "candidate IDs"),
            (metric_ids, "metric IDs"),
        ):
            if len(values) != len(set(values)):
                raise VisualBenchmarkError(f"plan {field_name} must be unique")
        covered = {capability for fixture in self.fixtures for capability in fixture.capabilities}
        if covered != set(VisualCapability):
            missing = ",".join(sorted(item.value for item in set(VisualCapability) - covered))
            raise VisualBenchmarkError(f"fixture capability coverage is incomplete: {missing}")
        if len(self.fixtures) > self.limits.max_fixtures:
            raise VisualBenchmarkError("plan fixtures exceed frozen limits")
        if len(self.candidates) > self.limits.max_candidates:
            raise VisualBenchmarkError("plan candidates exceed frozen limits")
        families = {candidate.family for candidate in self.candidates}
        if families != set(VisualCandidateFamily):
            raise VisualBenchmarkError(
                "plan must represent native, Ollama, and specialist families"
            )
        capabilities = set(VisualCapability)
        threshold_pairs = {
            (metric.capability, metric.metric)
            for metric in self.metrics
            if metric.capability is not None
        }
        for capability in capabilities:
            if (capability, VisualMetricKind.ACCURACY) not in threshold_pairs:
                raise VisualBenchmarkError(f"missing accuracy threshold for {capability.value}")
            if (capability, VisualMetricKind.CALIBRATION) not in threshold_pairs:
                raise VisualBenchmarkError(f"missing calibration threshold for {capability.value}")
        global_metrics = {(metric.capability, metric.metric) for metric in self.metrics}
        for metric_kind in (
            VisualMetricKind.LATENCY,
            VisualMetricKind.VRAM,
            VisualMetricKind.RAM,
            VisualMetricKind.DETERMINISM,
            VisualMetricKind.PLATFORM_SUPPORT,
        ):
            if (None, metric_kind) not in global_metrics:
                raise VisualBenchmarkError(f"missing global {metric_kind.value} threshold")
        if self.schema != VISUAL_BENCHMARK_SCHEMA:
            raise VisualBenchmarkError("unsupported visual benchmark schema")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @property
    def executable_candidate_ids(self) -> tuple[str, ...]:
        """Only explicitly qualified profiles can provide executable evidence."""

        return tuple(
            candidate.candidate_id
            for candidate in self.candidates
            if candidate.disposition is VisualDisposition.QUALIFIED
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "fixtures": [item.to_wire() for item in self.fixtures],
            "metrics": [item.to_wire() for item in self.metrics],
            "candidates": [item.to_wire() for item in self.candidates],
            "limits": self.limits.to_wire(),
            "stop_rules": self.stop_rules.to_wire(),
            "capability_policy": self.capability_policy.to_wire(),
            "routing": self.routing.to_wire(),
            "source_refs": list(self.source_refs),
        }

    def to_public_summary(self) -> dict[str, object]:
        """Return a redacted acceptance projection with no prompts, media, or locators."""

        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.fingerprint,
            "fixture_count": len(self.fixtures),
            "metric_count": len(self.metrics),
            "candidate_count": len(self.candidates),
            "capability_count": len(VisualCapability),
            "routing": [item.value for item in self.routing.preference_order],
            "automatic_fallback": self.routing.automatic_fallback,
            "executable_candidate_ids": list(self.executable_candidate_ids),
            "dispositions": {
                item.value: sum(candidate.disposition is item for candidate in self.candidates)
                for item in VisualDisposition
            },
            "claim_ceiling": "structural_only",
        }


def _fixture_fingerprint(seed: str) -> str:
    return canonical_fingerprint({"fixture_seed": seed})


def _default_fixture(
    fixture_id: str,
    modality: VisualModality,
    capabilities: tuple[VisualCapability, ...],
    *,
    timestamp_mode: VisualTimestampMode,
    tags: tuple[str, ...],
    adversarial_text: bool = False,
    corrupt_media: bool = False,
    rotation_degrees: int = 0,
) -> VisualFixture:
    return VisualFixture(
        fixture_id=fixture_id,
        modality=modality,
        capabilities=capabilities,
        source_fingerprint=_fixture_fingerprint(fixture_id + ".source"),
        annotation_fingerprint=_fixture_fingerprint(fixture_id + ".annotation"),
        privacy_class=VisualPrivacyClass.SYNTHETIC,
        timestamp_mode=timestamp_mode,
        tags=tags,
        adversarial_text=adversarial_text,
        corrupt_media=corrupt_media,
        rotation_degrees=rotation_degrees,
    )


def _default_device(
    profile_id: str, device: VisualDeviceKind, platform: str
) -> VisualDeviceProfile:
    return VisualDeviceProfile(
        profile_id=profile_id,
        platform=platform,
        device=device,
        dtype="float16" if device is not VisualDeviceKind.CPU else "float32",
        timeout_seconds=30,
        max_media_items=4,
        max_output_bytes=65_536,
        peak_vram_mb_limit=16_384 if device is not VisualDeviceKind.CPU else 1,
        peak_ram_mb_limit=32_768,
        supports_determinism=True,
        supports_cancellation_cleanup=True,
    )


def _default_metrics() -> tuple[VisualMetricThreshold, ...]:
    metrics: list[VisualMetricThreshold] = []
    for capability in VisualCapability:
        required = capability is not VisualCapability.IDENTITY
        metrics.extend(
            (
                VisualMetricThreshold(
                    metric_id=f"accuracy.{capability.value}",
                    metric=VisualMetricKind.ACCURACY,
                    unit=VisualMetricUnit.BASIS_POINTS,
                    capability=capability,
                    required=required,
                    minimum=7_000,
                    evaluation_set="visual.core.v1",
                    description="minimum held-out accuracy in basis points",
                ),
                VisualMetricThreshold(
                    metric_id=f"calibration.{capability.value}",
                    metric=VisualMetricKind.CALIBRATION,
                    unit=VisualMetricUnit.BASIS_POINTS,
                    capability=capability,
                    required=required,
                    maximum=1_500,
                    evaluation_set="visual.core.v1",
                    description="maximum expected calibration error in basis points",
                ),
            )
        )
    metrics.extend(
        (
            VisualMetricThreshold(
                metric_id="resource.latency",
                metric=VisualMetricKind.LATENCY,
                unit=VisualMetricUnit.MILLISECONDS,
                capability=None,
                required=True,
                maximum=30_000,
                description="maximum case latency in milliseconds",
            ),
            VisualMetricThreshold(
                metric_id="resource.vram",
                metric=VisualMetricKind.VRAM,
                unit=VisualMetricUnit.MEBIBYTES,
                capability=None,
                required=True,
                maximum=16_384,
                description="maximum peak VRAM in MiB",
            ),
            VisualMetricThreshold(
                metric_id="resource.ram",
                metric=VisualMetricKind.RAM,
                unit=VisualMetricUnit.MEBIBYTES,
                capability=None,
                required=True,
                maximum=32_768,
                description="maximum peak RAM in MiB",
            ),
            VisualMetricThreshold(
                metric_id="behavior.determinism",
                metric=VisualMetricKind.DETERMINISM,
                unit=VisualMetricUnit.BOOLEAN,
                capability=None,
                required=True,
                minimum=1,
                description="deterministic repeatability is required",
            ),
            VisualMetricThreshold(
                metric_id="platform.support",
                metric=VisualMetricKind.PLATFORM_SUPPORT,
                unit=VisualMetricUnit.BOOLEAN,
                capability=None,
                required=True,
                minimum=1,
                description="declared platform profile must be supported",
            ),
        )
    )
    return tuple(metrics)


def build_default_visual_benchmark_plan() -> VisualBenchmarkPlan:
    """Build the frozen M11-01 offline plan used by tests and the research fixture."""

    fixtures = (
        _default_fixture(
            "visual.synthetic.global-ocr",
            VisualModality.IMAGE,
            (
                VisualCapability.GLOBAL_DESCRIPTION,
                VisualCapability.OCR,
                VisualCapability.SUBJECTS_OBJECTS,
                VisualCapability.STYLE,
                VisualCapability.CONFIDENCE,
            ),
            timestamp_mode=VisualTimestampMode.NOT_APPLICABLE,
            tags=("global", "multilingual", "rotated-text"),
            rotation_degrees=90,
        ),
        _default_fixture(
            "visual.synthetic.temporal-vfr",
            VisualModality.VIDEO,
            (
                VisualCapability.SHOTS,
                VisualCapability.ACTIONS,
                VisualCapability.CAMERA,
                VisualCapability.MOTION,
                VisualCapability.EDITS,
                VisualCapability.TIMESTAMPS,
            ),
            timestamp_mode=VisualTimestampMode.VFR,
            tags=("hard-cut", "fade", "vfr", "silent"),
        ),
        _default_fixture(
            "visual.synthetic.identity-ambiguity",
            VisualModality.IMAGE,
            (VisualCapability.IDENTITY,),
            timestamp_mode=VisualTimestampMode.NOT_APPLICABLE,
            tags=("look-alike", "ambiguous", "privacy-opt-in"),
        ),
        _default_fixture(
            "visual.synthetic.corrupt",
            VisualModality.VIDEO,
            (VisualCapability.CORRUPTION, VisualCapability.TIMESTAMPS),
            timestamp_mode=VisualTimestampMode.CFR,
            tags=("truncated", "decode-error"),
            corrupt_media=True,
        ),
        _default_fixture(
            "visual.synthetic.adversarial-text",
            VisualModality.IMAGE,
            (VisualCapability.ADVERSARIAL_TEXT, VisualCapability.OCR, VisualCapability.CONFIDENCE),
            timestamp_mode=VisualTimestampMode.NOT_APPLICABLE,
            tags=("prompt-injection", "occluded", "exact-text"),
            adversarial_text=True,
        ),
    )
    native = VisualCandidateProfile(
        candidate_id="native.comfyui.clip-textgenerate",
        family=VisualCandidateFamily.COMFYUI_NATIVE,
        adapter_id="comfyui_native_clip_textgenerate",
        adapter_version="1.0.0",
        model_id="host-owned-clip-textgenerate",
        license="host_declared",
        capabilities=(
            VisualCapability.GLOBAL_DESCRIPTION,
            VisualCapability.OCR,
            VisualCapability.SUBJECTS_OBJECTS,
        ),
        device_profiles=(_default_device("native.cpu.fixture", VisualDeviceKind.CPU, "linux"),),
        privacy_mode="local_host_owned",
        requires_network=False,
        supports_determinism=True,
        supports_cancellation_cleanup=False,
        disposition=VisualDisposition.UNAVAILABLE,
        disposition_reason="live ComfyUI host, model weights, and media lane were not admitted",
        evidence_level=EvidenceLevel.EXPERIMENTAL,
        source_refs=("R8", "R9", "M10-08"),
    )
    ollama = VisualCandidateProfile(
        candidate_id="fallback.ollama.loopback-vlm",
        family=VisualCandidateFamily.OLLAMA,
        adapter_id="ollama_native_api",
        adapter_version="1.0.0",
        model_id="explicitly-selected-loopback-vlm",
        license="user_selected",
        capabilities=(
            VisualCapability.GLOBAL_DESCRIPTION,
            VisualCapability.OCR,
            VisualCapability.SUBJECTS_OBJECTS,
        ),
        device_profiles=(_default_device("ollama.cpu.fixture", VisualDeviceKind.CPU, "linux"),),
        privacy_mode="explicit_loopback_only",
        requires_network=True,
        supports_determinism=False,
        supports_cancellation_cleanup=False,
        disposition=VisualDisposition.UNAVAILABLE,
        disposition_reason="loopback Ollama server and explicitly selected model were not started",
        evidence_level=EvidenceLevel.EXPERIMENTAL,
        source_refs=("R8", "R9"),
    )
    specialist = VisualCandidateProfile(
        candidate_id="specialist.visual.adapter-placeholder",
        family=VisualCandidateFamily.SPECIALIST,
        adapter_id="specialist_not_selected",
        adapter_version="1.0.0",
        model_id="explicit-selection-required",
        license="not_selected",
        capabilities=(VisualCapability.SHOTS, VisualCapability.ACTIONS, VisualCapability.MOTION),
        device_profiles=(_default_device("specialist.cpu.fixture", VisualDeviceKind.CPU, "linux"),),
        privacy_mode="explicit_local_selection",
        requires_network=False,
        supports_determinism=True,
        supports_cancellation_cleanup=True,
        disposition=VisualDisposition.UNSUPPORTED,
        disposition_reason="no specialist profile is selected in the M11-01 native-first lane",
        evidence_level=EvidenceLevel.EXPERIMENTAL,
        source_refs=("P3", "P8", "P9"),
    )
    rejected = VisualCandidateProfile(
        candidate_id="native.conditioning-only.h3-qwen3vl",
        family=VisualCandidateFamily.COMFYUI_NATIVE,
        adapter_id="conditioning_only_rejected",
        adapter_version="1.0.0",
        model_id="h3-qwen3-vl-conditioning",
        license="host_declared",
        capabilities=(VisualCapability.GLOBAL_DESCRIPTION,),
        device_profiles=(
            _default_device("conditioning.cpu.fixture", VisualDeviceKind.CPU, "linux"),
        ),
        privacy_mode="local_host_owned",
        requires_network=False,
        supports_determinism=True,
        supports_cancellation_cleanup=True,
        disposition=VisualDisposition.REJECTED,
        disposition_reason="conditioning-only model path cannot execute TextGenerate perception",
        evidence_level=EvidenceLevel.FRAMEWORK_REFERENCE,
        source_refs=("R8", "R9", "M10-03"),
    )
    return VisualBenchmarkPlan(
        plan_id="m11-01.visual-benchmark",
        plan_version="1.0.0",
        fixtures=fixtures,
        metrics=_default_metrics(),
        candidates=(native, ollama, specialist, rejected),
        limits=VisualBenchmarkLimits(
            max_candidates=8,
            max_fixtures=16,
            max_wall_time_seconds=30,
            max_total_compute_seconds=120,
            max_peak_vram_mb=16_384,
            max_peak_ram_mb=32_768,
            max_media_items_per_case=4,
            max_output_bytes=65_536,
            max_concurrency=1,
            network_allowed=False,
            media_upload_allowed=False,
        ),
        stop_rules=VisualStopRules(
            max_schema_failures=0,
            max_timeout_failures=2,
            max_resource_failures=1,
            max_adversarial_unsafe_outputs=0,
            abort_on_privacy_violation=True,
            abort_on_hidden_fallback=True,
            reject_on_missing_required_metric=True,
            reject_on_unsupported_media=True,
        ),
        capability_policy=VisualCapabilityPolicy(
            mandatory=tuple(
                capability
                for capability in VisualCapability
                if capability is not VisualCapability.IDENTITY
            ),
            optional=(VisualCapability.IDENTITY,),
            consequence=(
                "Unqualified mandatory capabilities are removed from executable visual claims; "
                "optional identity remains opt-in and unsupported without a privacy-qualified "
                "profile."
            ),
        ),
        routing=VisualRoutingPolicy(
            preference_order=(
                VisualCandidateFamily.COMFYUI_NATIVE,
                VisualCandidateFamily.OLLAMA,
                VisualCandidateFamily.SPECIALIST,
            ),
            automatic_fallback=False,
            explicit_selection_required=True,
            fidelity_winner_preselected=False,
            disclosure=(
                "Product preference is native host-owned CLIP/TextGenerate, then explicitly "
                "selected "
                "loopback Ollama, then explicitly selected specialist adapters; this is a routing "
                "preference, not a fidelity result."
            ),
        ),
        source_refs=("R3", "R4", "R5", "R8", "R9", "P1-P10", "E1-E3", "M10-08"),
    )


__all__ = [
    "VISUAL_BENCHMARK_SCHEMA",
    "MAX_VISUAL_FIXTURES",
    "MAX_VISUAL_CANDIDATES",
    "MAX_VISUAL_METRICS",
    "VisualCapability",
    "VisualModality",
    "VisualPrivacyClass",
    "VisualTimestampMode",
    "VisualCandidateFamily",
    "VisualDisposition",
    "VisualDeviceKind",
    "VisualMetricKind",
    "VisualMetricUnit",
    "VisualFixture",
    "VisualMetricThreshold",
    "VisualDeviceProfile",
    "VisualCapabilityMeasurement",
    "VisualCandidateProfile",
    "VisualBenchmarkLimits",
    "VisualStopRules",
    "VisualCapabilityPolicy",
    "VisualRoutingPolicy",
    "VisualBenchmarkPlan",
    "build_default_visual_benchmark_plan",
]
