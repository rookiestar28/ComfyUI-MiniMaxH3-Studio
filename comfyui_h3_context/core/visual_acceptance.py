"""M11-08 visual-perception acceptance and disposition contracts.

The gate consumes the frozen M11-01 benchmark metadata and optional, explicitly supplied profile
measurements.  It never discovers a provider, starts a process, opens media, contacts a server, or
turns an unavailable route into a qualified profile.  With the current offline plan the expected
terminal result is ``unsupported_research``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_fingerprint
from .errors import VisualAcceptanceError
from .visual_benchmark import (
    VISUAL_BENCHMARK_SCHEMA,
    VisualBenchmarkPlan,
    VisualCandidateFamily,
    VisualCandidateProfile,
    VisualCapability,
    VisualCapabilityMeasurement,
    VisualDisposition,
    VisualMetricKind,
    VisualMetricThreshold,
)

VISUAL_ACCEPTANCE_SCHEMA = "h3.visual.acceptance.v1"
MAX_ACCEPTANCE_PROFILES = 32
MAX_ACCEPTANCE_SKIPS = 128
MAX_ACCEPTANCE_LIMITATIONS = 64
MAX_ACCEPTANCE_OUTPUT_BYTES = 65_536

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_SENSITIVE_MARKERS = (
    "http://",
    "https://",
    "file://",
    "/mnt/",
    "\\\\",
    "api_key",
    "authorization",
    "bearer ",
    "password",
    "secret",
    "token=",
    "signed",
)
_RUNTIME_STATES = frozenset({"not_started", "not_qualified", "qualified"})


class AcceptanceStatus(str, Enum):
    """Overall disposition of the frozen visual-perception acceptance gate."""

    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNSUPPORTED_RESEARCH = "unsupported_research"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise VisualAcceptanceError(f"{field_name} must be a bounded identifier")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise VisualAcceptanceError(f"{field_name} contains sensitive or locator material")
    return value


def _code(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise VisualAcceptanceError(f"{field_name} must be a lower-case bounded code")
    return value.casefold()


def _text(value: object, field_name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise VisualAcceptanceError(f"{field_name} must be bounded non-empty text")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise VisualAcceptanceError(f"{field_name} contains sensitive or locator material")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise VisualAcceptanceError(f"{field_name} contains a control character")
    return value


def _tuple_strings(values: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise VisualAcceptanceError(f"{field_name} must be a bounded tuple")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise VisualAcceptanceError(f"{field_name} must not contain duplicates")
    return result


def _tuple_text(values: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise VisualAcceptanceError(f"{field_name} must be a bounded tuple")
    return tuple(_text(value, f"{field_name} item") for value in values)


def _non_negative(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VisualAcceptanceError(f"{field_name} must be a non-negative integer")
    return value


def _bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise VisualAcceptanceError(f"{field_name} must be boolean")
    return value


def _enum(value: object, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise VisualAcceptanceError(f"{field_name} must be a {expected.__name__}")


@dataclass(frozen=True, slots=True)
class VisualProfileEvidence:
    """Explicit, redacted resource and lifecycle evidence for one candidate profile."""

    candidate_id: str
    latency_ms: int | None
    peak_vram_mb: int | None
    peak_ram_mb: int | None
    deterministic: bool | None
    platform_supported: bool | None
    cancellation_cleanup_verified: bool
    bounded_skips: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    schema: str = VISUAL_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "profile evidence candidate_id")
        for value, field_name in (
            (self.latency_ms, "profile evidence latency_ms"),
            (self.peak_vram_mb, "profile evidence peak_vram_mb"),
            (self.peak_ram_mb, "profile evidence peak_ram_mb"),
        ):
            if value is not None:
                _non_negative(value, field_name)
        if not isinstance(self.deterministic, (bool, type(None))) or not isinstance(
            self.platform_supported, (bool, type(None))
        ):
            raise VisualAcceptanceError("profile evidence boolean metrics must be bool or None")
        _bool(self.cancellation_cleanup_verified, "profile evidence cancellation cleanup")
        _tuple_strings(self.bounded_skips, "profile evidence bounded_skips", MAX_ACCEPTANCE_SKIPS)
        _tuple_text(self.limitations, "profile evidence limitations", MAX_ACCEPTANCE_LIMITATIONS)
        if self.schema != VISUAL_ACCEPTANCE_SCHEMA:
            raise VisualAcceptanceError("unsupported visual acceptance schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "latency_ms": self.latency_ms,
            "peak_vram_mb": self.peak_vram_mb,
            "peak_ram_mb": self.peak_ram_mb,
            "deterministic": self.deterministic,
            "platform_supported": self.platform_supported,
            "cancellation_cleanup_verified": self.cancellation_cleanup_verified,
            "bounded_skips": list(self.bounded_skips),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class VisualAcceptanceRuntime:
    """Execution-boundary receipt; defaults describe the offline/no-contact fixture."""

    profile_evidence: tuple[VisualProfileEvidence, ...] = ()
    fallback_used: bool = False
    network_contacted: bool = False
    media_started: bool = False
    host_started: bool = False
    optional_dependencies_absent: bool = True
    native_live_execution: str = "not_started"
    ollama_live_execution: str = "not_started"
    specialist_live_execution: str = "not_started"
    bounded_skips: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    schema: str = VISUAL_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile_evidence, tuple)
            or len(self.profile_evidence) > MAX_ACCEPTANCE_PROFILES
        ):
            raise VisualAcceptanceError("runtime profile_evidence must be a bounded tuple")
        if not all(isinstance(item, VisualProfileEvidence) for item in self.profile_evidence):
            raise VisualAcceptanceError("runtime profile_evidence contains an invalid value")
        profile_ids = tuple(item.candidate_id for item in self.profile_evidence)
        if len(profile_ids) != len(set(profile_ids)):
            raise VisualAcceptanceError("runtime profile evidence IDs must be unique")
        for bool_value, bool_field_name in (
            (self.fallback_used, "runtime fallback_used"),
            (self.network_contacted, "runtime network_contacted"),
            (self.media_started, "runtime media_started"),
            (self.host_started, "runtime host_started"),
            (self.optional_dependencies_absent, "runtime optional_dependencies_absent"),
        ):
            _bool(bool_value, bool_field_name)
        for state_value, state_field_name in (
            (self.native_live_execution, "runtime native_live_execution"),
            (self.ollama_live_execution, "runtime ollama_live_execution"),
            (self.specialist_live_execution, "runtime specialist_live_execution"),
        ):
            _code(state_value, state_field_name)
            if state_value not in _RUNTIME_STATES:
                raise VisualAcceptanceError(f"{state_field_name} has an unsupported state")
        _tuple_strings(self.bounded_skips, "runtime bounded_skips", MAX_ACCEPTANCE_SKIPS)
        _tuple_text(self.limitations, "runtime limitations", MAX_ACCEPTANCE_LIMITATIONS)
        if self.schema != VISUAL_ACCEPTANCE_SCHEMA:
            raise VisualAcceptanceError("unsupported visual acceptance schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_evidence": [item.to_wire() for item in self.profile_evidence],
            "fallback_used": self.fallback_used,
            "network_contacted": self.network_contacted,
            "media_started": self.media_started,
            "host_started": self.host_started,
            "optional_dependencies_absent": self.optional_dependencies_absent,
            "native_live_execution": self.native_live_execution,
            "ollama_live_execution": self.ollama_live_execution,
            "specialist_live_execution": self.specialist_live_execution,
            "bounded_skips": list(self.bounded_skips),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class AcceptanceCandidateReceipt:
    """Declared and effective disposition for one candidate profile."""

    candidate_id: str
    family: VisualCandidateFamily
    declared_disposition: VisualDisposition
    effective_disposition: VisualDisposition
    capabilities: tuple[VisualCapability, ...]
    missing_required_metrics: tuple[str, ...] = ()
    failed_checks: tuple[str, ...] = ()
    bounded_skips: tuple[str, ...] = ()
    limitation: str = ""
    schema: str = VISUAL_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate receipt candidate_id")
        _enum(self.family, VisualCandidateFamily, "candidate receipt family")
        _enum(
            self.declared_disposition, VisualDisposition, "candidate receipt declared disposition"
        )
        _enum(
            self.effective_disposition, VisualDisposition, "candidate receipt effective disposition"
        )
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise VisualAcceptanceError("candidate receipt capabilities must be non-empty")
        if not all(isinstance(item, VisualCapability) for item in self.capabilities):
            raise VisualAcceptanceError("candidate receipt capabilities contain an invalid value")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise VisualAcceptanceError("candidate receipt capabilities must be unique")
        _tuple_strings(
            self.missing_required_metrics,
            "candidate receipt missing_required_metrics",
            MAX_ACCEPTANCE_SKIPS,
        )
        _tuple_strings(self.failed_checks, "candidate receipt failed_checks", MAX_ACCEPTANCE_SKIPS)
        _tuple_strings(self.bounded_skips, "candidate receipt bounded_skips", MAX_ACCEPTANCE_SKIPS)
        if self.limitation:
            _text(self.limitation, "candidate receipt limitation")
        if self.schema != VISUAL_ACCEPTANCE_SCHEMA:
            raise VisualAcceptanceError("unsupported visual acceptance schema")

    @property
    def executable(self) -> bool:
        return self.effective_disposition is VisualDisposition.QUALIFIED

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "family": self.family.value,
            "declared_disposition": self.declared_disposition.value,
            "effective_disposition": self.effective_disposition.value,
            "capabilities": [item.value for item in self.capabilities],
            "missing_required_metrics": list(self.missing_required_metrics),
            "failed_checks": list(self.failed_checks),
            "bounded_skips": list(self.bounded_skips),
            "limitation": self.limitation,
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class VisualFamilySummary:
    """Route-separated public disposition summary."""

    family: VisualCandidateFamily
    candidate_ids: tuple[str, ...]
    executable_candidate_ids: tuple[str, ...]
    dispositions: tuple[tuple[str, VisualDisposition], ...]
    schema: str = VISUAL_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.family, VisualCandidateFamily, "family summary family")
        _tuple_strings(self.candidate_ids, "family summary candidate_ids", MAX_ACCEPTANCE_PROFILES)
        _tuple_strings(
            self.executable_candidate_ids,
            "family summary executable_candidate_ids",
            MAX_ACCEPTANCE_PROFILES,
        )
        if (
            not isinstance(self.dispositions, tuple)
            or len(self.dispositions) > MAX_ACCEPTANCE_PROFILES
        ):
            raise VisualAcceptanceError("family summary dispositions must be bounded")
        for candidate_id, disposition in self.dispositions:
            _identifier(candidate_id, "family summary disposition candidate_id")
            _enum(disposition, VisualDisposition, "family summary disposition")
        if self.schema != VISUAL_ACCEPTANCE_SCHEMA:
            raise VisualAcceptanceError("unsupported visual acceptance schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "family": self.family.value,
            "candidate_ids": list(self.candidate_ids),
            "executable_candidate_ids": list(self.executable_candidate_ids),
            "dispositions": {
                candidate_id: disposition.value for candidate_id, disposition in self.dispositions
            },
        }


@dataclass(frozen=True, slots=True)
class VisualAcceptanceReport:
    """Redacted, deterministic acceptance result."""

    plan_id: str
    plan_fingerprint: str
    status: AcceptanceStatus
    candidate_receipts: tuple[AcceptanceCandidateReceipt, ...]
    family_summaries: tuple[VisualFamilySummary, ...]
    unsupported_capabilities: tuple[VisualCapability, ...]
    executable_candidate_ids: tuple[str, ...]
    fallback_used: bool
    optional_dependencies_absent: bool
    bounded_skips: tuple[str, ...]
    limitations: tuple[str, ...]
    schema: str = VISUAL_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.plan_id, "acceptance plan_id")
        if not isinstance(self.plan_fingerprint, str) or not self.plan_fingerprint.startswith(
            "sha256:"
        ):
            raise VisualAcceptanceError("acceptance plan_fingerprint must be a SHA-256 fingerprint")
        _enum(self.status, AcceptanceStatus, "acceptance status")
        if not isinstance(self.candidate_receipts, tuple) or not self.candidate_receipts:
            raise VisualAcceptanceError("acceptance candidate_receipts must be non-empty")
        if not all(
            isinstance(item, AcceptanceCandidateReceipt) for item in self.candidate_receipts
        ):
            raise VisualAcceptanceError("acceptance candidate_receipts contain an invalid value")
        candidate_ids = tuple(item.candidate_id for item in self.candidate_receipts)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise VisualAcceptanceError("acceptance candidate IDs must be unique")
        if not isinstance(self.family_summaries, tuple) or len(self.family_summaries) != len(
            VisualCandidateFamily
        ):
            raise VisualAcceptanceError("acceptance family summaries must cover every family")
        families = tuple(item.family for item in self.family_summaries)
        if len(families) != len(set(families)):
            raise VisualAcceptanceError("acceptance family summaries must be unique")
        if not isinstance(self.unsupported_capabilities, tuple):
            raise VisualAcceptanceError("unsupported capabilities must be a tuple")
        if not all(isinstance(item, VisualCapability) for item in self.unsupported_capabilities):
            raise VisualAcceptanceError("unsupported capabilities contain an invalid value")
        _tuple_strings(
            self.executable_candidate_ids,
            "acceptance executable_candidate_ids",
            MAX_ACCEPTANCE_PROFILES,
        )
        _bool(self.fallback_used, "acceptance fallback_used")
        _bool(self.optional_dependencies_absent, "acceptance optional_dependencies_absent")
        _tuple_strings(self.bounded_skips, "acceptance bounded_skips", MAX_ACCEPTANCE_SKIPS)
        _tuple_text(self.limitations, "acceptance limitations", MAX_ACCEPTANCE_LIMITATIONS)
        if self.schema != VISUAL_ACCEPTANCE_SCHEMA:
            raise VisualAcceptanceError("unsupported visual acceptance schema")
        if len(self.to_wire_bytes()) > MAX_ACCEPTANCE_OUTPUT_BYTES:
            raise VisualAcceptanceError("acceptance report exceeds the portable output limit")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "status": self.status.value,
            "candidate_receipts": [item.to_wire() for item in self.candidate_receipts],
            "family_summaries": [item.to_wire() for item in self.family_summaries],
            "unsupported_capabilities": [item.value for item in self.unsupported_capabilities],
            "executable_candidate_ids": list(self.executable_candidate_ids),
            "fallback_used": self.fallback_used,
            "optional_dependencies_absent": self.optional_dependencies_absent,
            "bounded_skips": list(self.bounded_skips),
            "limitations": list(self.limitations),
        }

    def to_wire_bytes(self) -> bytes:
        import json

        return json.dumps(
            self.to_wire(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_public_dict(self) -> dict[str, object]:
        result = self.to_wire()
        result["fingerprint"] = self.fingerprint
        return result


def _check_threshold(
    threshold: VisualMetricThreshold,
    value: int | None,
    failed_checks: list[str],
    missing: list[str],
) -> None:
    metric_id = threshold.metric_id
    required = threshold.required
    minimum = threshold.minimum
    maximum = threshold.maximum
    if value is None:
        if required:
            missing.append(metric_id)
        return
    if minimum is not None and value < minimum:
        failed_checks.append(f"{metric_id}.minimum")
    if maximum is not None and value > maximum:
        failed_checks.append(f"{metric_id}.maximum")


def _measurement_value(
    measurement: VisualCapabilityMeasurement, metric: VisualMetricKind
) -> int | None:
    if metric is VisualMetricKind.ACCURACY:
        return measurement.accuracy_basis_points
    if metric is VisualMetricKind.CALIBRATION:
        return measurement.calibration_ece_basis_points
    return None


def _evaluate_candidate(
    plan: VisualBenchmarkPlan,
    candidate: VisualCandidateProfile,
    evidence: VisualProfileEvidence | None,
) -> AcceptanceCandidateReceipt:
    missing: list[str] = []
    failed: list[str] = []
    if candidate.disposition is VisualDisposition.QUALIFIED:
        if evidence is None:
            failed.append("runtime_evidence_missing")
        else:
            if not evidence.cancellation_cleanup_verified:
                failed.append("cancellation_cleanup_missing")
            if evidence.deterministic is not True:
                failed.append("determinism_missing_or_false")
            if evidence.platform_supported is not True:
                failed.append("platform_support_missing_or_false")
            global_values: dict[VisualMetricKind, int | None] = {
                VisualMetricKind.LATENCY: evidence.latency_ms,
                VisualMetricKind.VRAM: evidence.peak_vram_mb,
                VisualMetricKind.RAM: evidence.peak_ram_mb,
                VisualMetricKind.DETERMINISM: int(evidence.deterministic)
                if evidence.deterministic is not None
                else None,
                VisualMetricKind.PLATFORM_SUPPORT: int(evidence.platform_supported)
                if evidence.platform_supported is not None
                else None,
            }
            for threshold in plan.metrics:
                if threshold.capability is None:
                    _check_threshold(
                        threshold,
                        global_values[threshold.metric],
                        failed,
                        missing,
                    )
            measurements = {item.capability: item for item in candidate.measurements}
            for capability in candidate.capabilities:
                measurement = measurements.get(capability)
                for threshold in plan.metrics:
                    if threshold.capability is capability:
                        value = (
                            _measurement_value(measurement, threshold.metric)
                            if measurement
                            else None
                        )
                        _check_threshold(threshold, value, failed, missing)
    effective = VisualDisposition.QUALIFIED
    if candidate.disposition is not VisualDisposition.QUALIFIED:
        effective = candidate.disposition
    elif failed or missing:
        effective = VisualDisposition.REJECTED
    limitation = candidate.disposition_reason
    if evidence is not None and evidence.limitations:
        limitation = "; ".join(evidence.limitations)
    return AcceptanceCandidateReceipt(
        candidate_id=candidate.candidate_id,
        family=candidate.family,
        declared_disposition=candidate.disposition,
        effective_disposition=effective,
        capabilities=candidate.capabilities,
        missing_required_metrics=tuple(sorted(set(missing))),
        failed_checks=tuple(sorted(set(failed))),
        bounded_skips=evidence.bounded_skips if evidence is not None else (),
        limitation=limitation,
    )


def evaluate_visual_acceptance(
    plan: VisualBenchmarkPlan,
    *,
    runtime: VisualAcceptanceRuntime | None = None,
) -> VisualAcceptanceReport:
    """Evaluate one frozen plan without selecting or contacting a provider."""

    if not isinstance(plan, VisualBenchmarkPlan):
        raise VisualAcceptanceError("acceptance requires VisualBenchmarkPlan")
    if plan.schema != VISUAL_BENCHMARK_SCHEMA:
        raise VisualAcceptanceError("unsupported benchmark plan schema")
    runtime_value = VisualAcceptanceRuntime() if runtime is None else runtime
    if not isinstance(runtime_value, VisualAcceptanceRuntime):
        raise VisualAcceptanceError("runtime must be VisualAcceptanceRuntime")
    if runtime_value.fallback_used:
        raise VisualAcceptanceError("hidden provider/profile fallback is forbidden")
    if runtime_value.network_contacted or runtime_value.media_started:
        raise VisualAcceptanceError("offline acceptance cannot contact network or start media")
    if not runtime_value.optional_dependencies_absent:
        raise VisualAcceptanceError("optional dependency absence is required for this gate")
    evidence_by_id = {item.candidate_id: item for item in runtime_value.profile_evidence}
    plan_candidate_ids = {item.candidate_id for item in plan.candidates}
    unknown = set(evidence_by_id) - plan_candidate_ids
    if unknown:
        raise VisualAcceptanceError("profile evidence references an unknown candidate")

    receipts = tuple(
        _evaluate_candidate(plan, candidate, evidence_by_id.get(candidate.candidate_id))
        for candidate in plan.candidates
    )
    executable = tuple(item.candidate_id for item in receipts if item.executable)
    qualified_capabilities = {
        capability for item in receipts if item.executable for capability in item.capabilities
    }
    unsupported = tuple(
        capability for capability in VisualCapability if capability not in qualified_capabilities
    )
    if executable:
        status = AcceptanceStatus.QUALIFIED
    elif any(item.declared_disposition is VisualDisposition.QUALIFIED for item in receipts):
        status = AcceptanceStatus.REJECTED
    else:
        status = AcceptanceStatus.UNSUPPORTED_RESEARCH
    family_summaries = tuple(
        VisualFamilySummary(
            family=family,
            candidate_ids=tuple(item.candidate_id for item in receipts if item.family is family),
            executable_candidate_ids=tuple(
                item.candidate_id for item in receipts if item.family is family and item.executable
            ),
            dispositions=tuple(
                (item.candidate_id, item.effective_disposition)
                for item in receipts
                if item.family is family
            ),
        )
        for family in VisualCandidateFamily
    )
    limitations = tuple(
        sorted(
            set(runtime_value.limitations)
            | {item.limitation for item in receipts if item.limitation and not item.executable}
        )
    )
    return VisualAcceptanceReport(
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        status=status,
        candidate_receipts=receipts,
        family_summaries=family_summaries,
        unsupported_capabilities=unsupported,
        executable_candidate_ids=executable,
        fallback_used=runtime_value.fallback_used,
        optional_dependencies_absent=runtime_value.optional_dependencies_absent,
        bounded_skips=tuple(sorted(set(runtime_value.bounded_skips))),
        limitations=limitations,
    )


__all__ = [
    "VISUAL_ACCEPTANCE_SCHEMA",
    "AcceptanceCandidateReceipt",
    "AcceptanceStatus",
    "VisualAcceptanceReport",
    "VisualAcceptanceRuntime",
    "VisualFamilySummary",
    "VisualProfileEvidence",
    "evaluate_visual_acceptance",
]
