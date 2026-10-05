"""M12-07 audio-perception acceptance and disposition contracts.

The gate evaluates the frozen audio benchmark and explicitly supplied redacted profile evidence.
It never discovers a provider, starts a process, opens media, contacts a server, or changes an
unavailable route into an executable profile.  The current offline lane is expected to close as
``unsupported_research``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum

from .audio_perception_benchmark import (
    AUDIO_PERCEPTION_BENCHMARK_SCHEMA,
    AudioBenchmarkPlan,
    AudioCandidateFamily,
    AudioCapability,
    AudioDisposition,
)
from .canonical import canonical_fingerprint
from .errors import AudioAcceptanceError

AUDIO_ACCEPTANCE_SCHEMA = "h3.audio.acceptance.v1"
MAX_AUDIO_ACCEPTANCE_PROFILES = 32
MAX_AUDIO_ACCEPTANCE_SKIPS = 128
MAX_AUDIO_ACCEPTANCE_LIMITATIONS = 64
MAX_AUDIO_ACCEPTANCE_OUTPUT_BYTES = 65_536

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
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


class AudioAcceptanceStatus(str, Enum):
    """Overall disposition of the frozen audio-perception acceptance gate."""

    QUALIFIED = "qualified"
    REJECTED = "rejected"
    UNSUPPORTED_RESEARCH = "unsupported_research"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AudioAcceptanceError(f"{field_name} must be a bounded identifier")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise AudioAcceptanceError(f"{field_name} contains sensitive or locator material")
    return value


def _code(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value.casefold()) is None:
        raise AudioAcceptanceError(f"{field_name} must be a lower-case bounded code")
    return value.casefold()


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise AudioAcceptanceError(f"{field_name} must be a SHA-256 fingerprint")
    return value


def _text(value: object, field_name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AudioAcceptanceError(f"{field_name} must be bounded non-empty text")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise AudioAcceptanceError(f"{field_name} contains sensitive or locator material")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise AudioAcceptanceError(f"{field_name} contains a control character")
    return value


def _tuple_strings(values: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise AudioAcceptanceError(f"{field_name} must be a bounded tuple")
    result = tuple(_identifier(value, f"{field_name} item") for value in values)
    if len(result) != len(set(result)):
        raise AudioAcceptanceError(f"{field_name} must not contain duplicates")
    return result


def _tuple_text(values: object, field_name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise AudioAcceptanceError(f"{field_name} must be a bounded tuple")
    return tuple(_text(value, f"{field_name} item") for value in values)


def _non_negative(value: object, field_name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AudioAcceptanceError(f"{field_name} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise AudioAcceptanceError(f"{field_name} exceeds the finite limit")
    return value


def _basis_points(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        raise AudioAcceptanceError(f"{field_name} must be between 0 and 10000 basis points")
    return value


def _bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise AudioAcceptanceError(f"{field_name} must be boolean")
    return value


def _enum(value: object, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise AudioAcceptanceError(f"{field_name} must be a {expected.__name__}")


@dataclass(frozen=True, slots=True)
class AudioCapabilityMeasurement:
    """One bounded, redacted metric for one declared audio capability."""

    capability: AudioCapability
    score_basis_points: int
    schema: str = AUDIO_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.capability, AudioCapability, "measurement capability")
        _basis_points(self.score_basis_points, "measurement score_basis_points")
        if self.schema != AUDIO_ACCEPTANCE_SCHEMA:
            raise AudioAcceptanceError("unsupported audio acceptance schema")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "capability": self.capability.value,
            "score_basis_points": self.score_basis_points,
        }


@dataclass(frozen=True, slots=True)
class AudioProfileEvidence:
    """Explicit redacted resource, ownership, safety, and metric evidence for one profile."""

    candidate_id: str
    latency_ms: int | None
    peak_vram_mb: int | None
    peak_ram_mb: int | None
    deterministic: bool | None
    platform_supported: bool | None
    cancellation_cleanup_verified: bool
    exact_dialogue_ownership_preserved: bool
    no_speech_and_degraded_bounded: bool
    measurements: tuple[AudioCapabilityMeasurement, ...]
    bounded_skips: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    schema: str = AUDIO_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "profile evidence candidate_id")
        for value, field_name in (
            (self.latency_ms, "profile evidence latency_ms"),
            (self.peak_vram_mb, "profile evidence peak_vram_mb"),
            (self.peak_ram_mb, "profile evidence peak_ram_mb"),
        ):
            if value is not None:
                _non_negative(value, field_name)
        for value, field_name in (
            (self.deterministic, "profile evidence deterministic"),
            (self.platform_supported, "profile evidence platform_supported"),
        ):
            if not isinstance(value, (bool, type(None))):
                raise AudioAcceptanceError(f"{field_name} must be boolean or None")
        _bool(self.cancellation_cleanup_verified, "profile evidence cancellation cleanup")
        _bool(
            self.exact_dialogue_ownership_preserved,
            "profile evidence exact dialogue ownership",
        )
        _bool(
            self.no_speech_and_degraded_bounded,
            "profile evidence no-speech/degraded bound",
        )
        if not isinstance(self.measurements, tuple) or not self.measurements:
            raise AudioAcceptanceError("profile evidence measurements must be non-empty")
        if not all(isinstance(item, AudioCapabilityMeasurement) for item in self.measurements):
            raise AudioAcceptanceError("profile evidence measurements contain an invalid value")
        capabilities = tuple(item.capability for item in self.measurements)
        if len(capabilities) != len(set(capabilities)):
            raise AudioAcceptanceError("profile evidence measurement capabilities must be unique")
        _tuple_strings(
            self.bounded_skips, "profile evidence bounded_skips", MAX_AUDIO_ACCEPTANCE_SKIPS
        )
        _tuple_text(
            self.limitations,
            "profile evidence limitations",
            MAX_AUDIO_ACCEPTANCE_LIMITATIONS,
        )
        if self.schema != AUDIO_ACCEPTANCE_SCHEMA:
            raise AudioAcceptanceError("unsupported audio acceptance schema")

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
            "exact_dialogue_ownership_preserved": self.exact_dialogue_ownership_preserved,
            "no_speech_and_degraded_bounded": self.no_speech_and_degraded_bounded,
            "measurements": [item.to_wire() for item in self.measurements],
            "bounded_skips": list(self.bounded_skips),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class AudioAcceptanceRuntime:
    """Declarative runtime state for the offline acceptance lane."""

    profile_evidence: tuple[AudioProfileEvidence, ...] = ()
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
    schema: str = AUDIO_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile_evidence, tuple)
            or len(self.profile_evidence) > MAX_AUDIO_ACCEPTANCE_PROFILES
        ):
            raise AudioAcceptanceError("runtime profile_evidence must be bounded")
        if not all(isinstance(item, AudioProfileEvidence) for item in self.profile_evidence):
            raise AudioAcceptanceError("runtime profile_evidence contains an invalid value")
        candidate_ids = tuple(item.candidate_id for item in self.profile_evidence)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise AudioAcceptanceError("runtime profile evidence IDs must be unique")
        for flag_value, field_name in (
            (self.fallback_used, "runtime fallback_used"),
            (self.network_contacted, "runtime network_contacted"),
            (self.media_started, "runtime media_started"),
            (self.host_started, "runtime host_started"),
            (self.optional_dependencies_absent, "runtime optional_dependencies_absent"),
        ):
            _bool(flag_value, field_name)
        for state_value, field_name in (
            (self.native_live_execution, "runtime native_live_execution"),
            (self.ollama_live_execution, "runtime ollama_live_execution"),
            (self.specialist_live_execution, "runtime specialist_live_execution"),
        ):
            _code(state_value, field_name)
            if state_value not in _RUNTIME_STATES:
                raise AudioAcceptanceError(f"{field_name} has an unsupported state")
        _tuple_strings(self.bounded_skips, "runtime bounded_skips", MAX_AUDIO_ACCEPTANCE_SKIPS)
        _tuple_text(self.limitations, "runtime limitations", MAX_AUDIO_ACCEPTANCE_LIMITATIONS)
        if self.schema != AUDIO_ACCEPTANCE_SCHEMA:
            raise AudioAcceptanceError("unsupported audio acceptance schema")

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
class AudioAcceptanceCandidateReceipt:
    """Declared and effective disposition for one candidate profile."""

    candidate_id: str
    family: AudioCandidateFamily
    declared_disposition: AudioDisposition
    effective_disposition: AudioDisposition
    capabilities: tuple[AudioCapability, ...]
    missing_required_metrics: tuple[str, ...] = ()
    failed_checks: tuple[str, ...] = ()
    bounded_skips: tuple[str, ...] = ()
    limitation: str = ""
    schema: str = AUDIO_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate receipt candidate_id")
        _enum(self.family, AudioCandidateFamily, "candidate receipt family")
        _enum(self.declared_disposition, AudioDisposition, "candidate receipt declared disposition")
        _enum(
            self.effective_disposition, AudioDisposition, "candidate receipt effective disposition"
        )
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise AudioAcceptanceError("candidate receipt capabilities must be non-empty")
        if not all(isinstance(item, AudioCapability) for item in self.capabilities):
            raise AudioAcceptanceError("candidate receipt capabilities contain an invalid value")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise AudioAcceptanceError("candidate receipt capabilities must be unique")
        _tuple_strings(
            self.missing_required_metrics,
            "candidate receipt missing_required_metrics",
            MAX_AUDIO_ACCEPTANCE_SKIPS,
        )
        _tuple_strings(
            self.failed_checks, "candidate receipt failed_checks", MAX_AUDIO_ACCEPTANCE_SKIPS
        )
        _tuple_strings(
            self.bounded_skips, "candidate receipt bounded_skips", MAX_AUDIO_ACCEPTANCE_SKIPS
        )
        if self.limitation:
            _text(self.limitation, "candidate receipt limitation")
        if self.schema != AUDIO_ACCEPTANCE_SCHEMA:
            raise AudioAcceptanceError("unsupported audio acceptance schema")

    @property
    def executable(self) -> bool:
        return self.effective_disposition is AudioDisposition.QUALIFIED

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
class AudioAcceptanceFamilySummary:
    """Route-separated public disposition summary."""

    family: AudioCandidateFamily
    candidate_ids: tuple[str, ...]
    executable_candidate_ids: tuple[str, ...]
    dispositions: tuple[tuple[str, AudioDisposition], ...]
    schema: str = AUDIO_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        _enum(self.family, AudioCandidateFamily, "family summary family")
        _tuple_strings(
            self.candidate_ids, "family summary candidate_ids", MAX_AUDIO_ACCEPTANCE_PROFILES
        )
        _tuple_strings(
            self.executable_candidate_ids,
            "family summary executable_candidate_ids",
            MAX_AUDIO_ACCEPTANCE_PROFILES,
        )
        if (
            not isinstance(self.dispositions, tuple)
            or len(self.dispositions) > MAX_AUDIO_ACCEPTANCE_PROFILES
        ):
            raise AudioAcceptanceError("family summary dispositions must be bounded")
        for candidate_id, disposition in self.dispositions:
            _identifier(candidate_id, "family summary disposition candidate_id")
            _enum(disposition, AudioDisposition, "family summary disposition")
        if self.schema != AUDIO_ACCEPTANCE_SCHEMA:
            raise AudioAcceptanceError("unsupported audio acceptance schema")

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
class AudioAcceptanceReport:
    """Redacted, deterministic acceptance result."""

    plan_id: str
    plan_fingerprint: str
    status: AudioAcceptanceStatus
    candidate_receipts: tuple[AudioAcceptanceCandidateReceipt, ...]
    family_summaries: tuple[AudioAcceptanceFamilySummary, ...]
    unsupported_capabilities: tuple[AudioCapability, ...]
    executable_candidate_ids: tuple[str, ...]
    fallback_used: bool
    optional_dependencies_absent: bool
    bounded_skips: tuple[str, ...]
    limitations: tuple[str, ...]
    schema: str = AUDIO_ACCEPTANCE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.plan_id, "acceptance plan_id")
        _fingerprint(self.plan_fingerprint, "acceptance plan_fingerprint")
        _enum(self.status, AudioAcceptanceStatus, "acceptance status")
        if not isinstance(self.candidate_receipts, tuple) or not self.candidate_receipts:
            raise AudioAcceptanceError("acceptance candidate_receipts must be non-empty")
        if not all(
            isinstance(item, AudioAcceptanceCandidateReceipt) for item in self.candidate_receipts
        ):
            raise AudioAcceptanceError("acceptance candidate_receipts contain an invalid value")
        candidate_ids = tuple(item.candidate_id for item in self.candidate_receipts)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise AudioAcceptanceError("acceptance candidate IDs must be unique")
        if not isinstance(self.family_summaries, tuple) or len(self.family_summaries) != len(
            AudioCandidateFamily
        ):
            raise AudioAcceptanceError("acceptance family summaries must cover every family")
        families = tuple(item.family for item in self.family_summaries)
        if len(families) != len(set(families)):
            raise AudioAcceptanceError("acceptance family summaries must be unique")
        if not isinstance(self.unsupported_capabilities, tuple) or not all(
            isinstance(item, AudioCapability) for item in self.unsupported_capabilities
        ):
            raise AudioAcceptanceError("unsupported capabilities contain an invalid value")
        _tuple_strings(
            self.executable_candidate_ids,
            "acceptance executable_candidate_ids",
            MAX_AUDIO_ACCEPTANCE_PROFILES,
        )
        _bool(self.fallback_used, "acceptance fallback_used")
        _bool(self.optional_dependencies_absent, "acceptance optional_dependencies_absent")
        _tuple_strings(self.bounded_skips, "acceptance bounded_skips", MAX_AUDIO_ACCEPTANCE_SKIPS)
        _tuple_text(self.limitations, "acceptance limitations", MAX_AUDIO_ACCEPTANCE_LIMITATIONS)
        if self.schema != AUDIO_ACCEPTANCE_SCHEMA:
            raise AudioAcceptanceError("unsupported audio acceptance schema")
        if len(self.to_wire_bytes()) > MAX_AUDIO_ACCEPTANCE_OUTPUT_BYTES:
            raise AudioAcceptanceError("acceptance report exceeds the portable output limit")

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


def _evaluate_candidate(
    plan: AudioBenchmarkPlan,
    candidate_id: str,
    family: AudioCandidateFamily,
    capabilities: tuple[AudioCapability, ...],
    disposition: AudioDisposition,
    disposition_reason: str,
    evidence: AudioProfileEvidence | None,
) -> AudioAcceptanceCandidateReceipt:
    missing: list[str] = []
    failed: list[str] = []
    if disposition is AudioDisposition.QUALIFIED:
        if evidence is None:
            failed.append("runtime_evidence_missing")
        else:
            if evidence.cancellation_cleanup_verified is not True:
                failed.append("cancellation_cleanup_missing")
            if evidence.deterministic is not True:
                failed.append("determinism_missing_or_false")
            if evidence.platform_supported is not True:
                failed.append("platform_support_missing_or_false")
            if evidence.exact_dialogue_ownership_preserved is not True:
                failed.append("exact_dialogue_ownership_failed")
            if evidence.no_speech_and_degraded_bounded is not True:
                failed.append("degraded_outcome_bound_missing")
            if evidence.latency_ms is None:
                missing.append("latency_ms")
            elif evidence.latency_ms > plan.limits.max_wall_time_seconds * 1000:
                failed.append("latency_ms.maximum")
            if evidence.peak_vram_mb is None:
                missing.append("peak_vram_mb")
            elif evidence.peak_vram_mb > plan.limits.max_peak_vram_mb:
                failed.append("peak_vram_mb.maximum")
            if evidence.peak_ram_mb is None:
                missing.append("peak_ram_mb")
            elif evidence.peak_ram_mb > plan.limits.max_peak_ram_mb:
                failed.append("peak_ram_mb.maximum")
            measurements = {item.capability: item for item in evidence.measurements}
            thresholds = {item.capability: item for item in plan.thresholds}
            for capability in capabilities:
                measurement = measurements.get(capability)
                threshold = thresholds.get(capability)
                if measurement is None or threshold is None:
                    missing.append(f"structural.{capability.value}")
                elif measurement.score_basis_points < threshold.minimum:
                    failed.append(f"structural.{capability.value}.minimum")
            unknown = set(measurements) - set(capabilities)
            if unknown:
                failed.append("measurement_capability_not_declared")
    effective = (
        disposition if disposition is not AudioDisposition.QUALIFIED else AudioDisposition.QUALIFIED
    )
    if disposition is AudioDisposition.QUALIFIED and (failed or missing):
        effective = AudioDisposition.REJECTED
    limitation = disposition_reason
    if evidence is not None and evidence.limitations:
        limitation = "; ".join(evidence.limitations)
    return AudioAcceptanceCandidateReceipt(
        candidate_id=candidate_id,
        family=family,
        declared_disposition=disposition,
        effective_disposition=effective,
        capabilities=capabilities,
        missing_required_metrics=tuple(sorted(set(missing))),
        failed_checks=tuple(sorted(set(failed))),
        bounded_skips=evidence.bounded_skips if evidence is not None else (),
        limitation=limitation,
    )


def evaluate_audio_acceptance(
    plan: AudioBenchmarkPlan,
    *,
    runtime: AudioAcceptanceRuntime | None = None,
) -> AudioAcceptanceReport:
    """Evaluate one frozen audio plan without selecting or contacting a provider."""

    if not isinstance(plan, AudioBenchmarkPlan):
        raise AudioAcceptanceError("acceptance requires AudioBenchmarkPlan")
    if plan.schema != AUDIO_PERCEPTION_BENCHMARK_SCHEMA:
        raise AudioAcceptanceError("unsupported audio benchmark plan schema")
    runtime_value = AudioAcceptanceRuntime() if runtime is None else runtime
    if not isinstance(runtime_value, AudioAcceptanceRuntime):
        raise AudioAcceptanceError("runtime must be AudioAcceptanceRuntime")
    if runtime_value.fallback_used:
        raise AudioAcceptanceError("hidden provider/profile fallback is forbidden")
    if runtime_value.network_contacted or runtime_value.media_started or runtime_value.host_started:
        raise AudioAcceptanceError("offline acceptance cannot start host/media or contact network")
    if not runtime_value.optional_dependencies_absent:
        raise AudioAcceptanceError("optional dependency absence is required for this gate")
    evidence_by_id = {item.candidate_id: item for item in runtime_value.profile_evidence}
    plan_candidate_ids = {item.candidate_id for item in plan.candidates}
    if set(evidence_by_id) - plan_candidate_ids:
        raise AudioAcceptanceError("profile evidence references an unknown candidate")
    receipts = tuple(
        _evaluate_candidate(
            plan,
            candidate.candidate_id,
            candidate.family,
            candidate.capabilities,
            candidate.disposition,
            candidate.disposition_reason,
            evidence_by_id.get(candidate.candidate_id),
        )
        for candidate in plan.candidates
    )
    executable = tuple(item.candidate_id for item in receipts if item.executable)
    qualified_capabilities = {
        capability for item in receipts if item.executable for capability in item.capabilities
    }
    unsupported = tuple(
        capability for capability in AudioCapability if capability not in qualified_capabilities
    )
    if executable:
        status = AudioAcceptanceStatus.QUALIFIED
    elif any(item.declared_disposition is AudioDisposition.QUALIFIED for item in receipts):
        status = AudioAcceptanceStatus.REJECTED
    else:
        status = AudioAcceptanceStatus.UNSUPPORTED_RESEARCH
    family_summaries = tuple(
        AudioAcceptanceFamilySummary(
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
        for family in AudioCandidateFamily
    )
    limitations = tuple(
        sorted(
            set(runtime_value.limitations)
            | {item.limitation for item in receipts if item.limitation and not item.executable}
        )
    )
    return AudioAcceptanceReport(
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
    "AUDIO_ACCEPTANCE_SCHEMA",
    "MAX_AUDIO_ACCEPTANCE_LIMITATIONS",
    "MAX_AUDIO_ACCEPTANCE_OUTPUT_BYTES",
    "MAX_AUDIO_ACCEPTANCE_PROFILES",
    "MAX_AUDIO_ACCEPTANCE_SKIPS",
    "AudioAcceptanceCandidateReceipt",
    "AudioAcceptanceError",
    "AudioAcceptanceFamilySummary",
    "AudioAcceptanceReport",
    "AudioAcceptanceRuntime",
    "AudioAcceptanceStatus",
    "AudioCapabilityMeasurement",
    "AudioProfileEvidence",
    "evaluate_audio_acceptance",
]
