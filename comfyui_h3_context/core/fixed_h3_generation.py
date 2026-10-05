"""Fail-closed terminal contract for the unavailable fixed-H3 generation study.

This module is deliberately model-free. It records why no fixed H3-Base arm was admitted and
prevents missing generation, score, and comparison evidence from becoming a product claim.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .local_qualification import (
    ProductScopeDisposition,
    build_default_local_qualification_plan,
    evaluate_local_qualification,
)
from .native_h3 import (
    NATIVE_H3_IMAGE_NODE_ID,
    NATIVE_H3_REFERENCE_NODE_ID,
    NATIVE_H3_WIRING_SCHEMA,
)
from .semantic_graph_comparator import (
    FROZEN_EVALUATION_BUNDLE_FINGERPRINT,
    M14_01_TERMINAL_DISPOSITION_SHA256,
)

FIXED_H3_GENERATION_SCHEMA = "h3.fixed_generation.terminal.v1"
FIXED_H3_RECORD_DATE = "2026-08-09"
MAX_FIXED_H3_WIRE_BYTES = 32_768
MAX_FIXED_H3_DEPTH = 8
MAX_FIXED_H3_CONTAINER_ITEMS = 64
MAX_FIXED_H3_STRING_LENGTH = 256

M14_03_BEHAVIOR_PROFILE_FINGERPRINT = (
    "sha256:1b79c85e50320367dfbff1d8ecc09be797e0b9f651705309edbabe321611d30d"
)
M14_04_LOCAL_QUALIFICATION_FINGERPRINT = (
    "sha256:693ae2289aa4b184dbf9300abd110f1ba82ffee7689446a90b5c33becff274c4"
)
M14_04_LOCAL_QUALIFICATION_PLAN_FINGERPRINT = (
    "sha256:c01f76acf3a78af69d40cbce5a1fc80d68cc3ceec068fd8b40dfd17fe7dfbd8d"
)
# IMPORTANT: this terminal receipt is historical M14 evidence; never substitute
# the current host pin.
M14_05_NATIVE_H3_HOST_VERSION = "0.30.0"
M14_05_NATIVE_H3_HOST_REVISION = "c44dea18809e3ca0e12e25cbe6938c2c45a29c9d"
M14_05_NATIVE_H3_SOURCE_BLOB = "22bc91cd4570a071c664ec4848a5be748909e1e7"
FROZEN_FIXED_H3_TERMINAL_FINGERPRINT = (
    "sha256:9a11ebfd5e2df401e6e892fa9ef3fc6fecc53bc0a28a4c4e84d003a7de4f52b0"
)

_SAFE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_FORBIDDEN_VALUE_MARKERS = (
    "http://",
    "https://",
    "token=",
    "password=",
    "authorization:",
    "bearer ",
    "/mnt/",
    "\\\\",
    ":\\",
)
_SENSITIVE_KEYS = frozenset(
    {"token", "password", "secret", "credential", "cookie", "api_key", "private_key"}
)


class FixedH3Error(ValueError):
    """Raised when terminal fixed-H3 evidence is malformed or contradictory."""


class FixedH3Disposition(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"


class FixedH3ClaimCap(str, Enum):
    NO_FIXED_H3_GENERATION_EVIDENCE = "NO_FIXED_H3_GENERATION_EVIDENCE"


class FixedH3EvidenceStatus(str, Enum):
    MISSING = "MISSING"


class FixedH3PromptFamily(str, Enum):
    RAW_MANUAL = "raw_manual"
    OFFICIAL = "official"
    LOCAL = "local"
    ABLATED = "ablated"


class FixedH3Metric(str, Enum):
    INSTRUCTION = "instruction"
    IDENTITY = "identity"
    TRANSITION = "transition"
    KEYFRAME = "keyframe"
    CAMERA = "camera"
    TEXT = "text"
    DIALOGUE = "dialogue"
    LIP_SYNC = "lip_sync"
    SOUND = "sound"
    MUSIC = "music"
    INTENT_DRIFT = "intent_drift"


_FAMILY_REASONS = {
    FixedH3PromptFamily.RAW_MANUAL: "execution_envelope_unavailable",
    FixedH3PromptFamily.OFFICIAL: "official_prompt_unavailable",
    FixedH3PromptFamily.LOCAL: "assisted_profile_unqualified",
    FixedH3PromptFamily.ABLATED: "ablation_prompts_unavailable",
}


def _require_exact_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if type(value) is not enum_type:
        raise FixedH3Error(f"{field_name} must be an exact enum member")


def _require_exact_value(value: object, expected: object, field_name: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise FixedH3Error(f"{field_name} is not the admitted terminal value")


def _require_exact_string_tuple(value: object, expected: tuple[str, ...], field_name: str) -> None:
    if type(value) is not tuple or len(value) != len(expected):
        raise FixedH3Error(f"{field_name} is not the admitted terminal inventory")
    for actual, admitted in zip(value, expected, strict=True):
        if type(actual) is not str or actual != admitted:
            raise FixedH3Error(f"{field_name} is not the admitted terminal inventory")


@dataclass(frozen=True, slots=True)
class FixedH3FamilyReceipt:
    """One missing prompt-family receipt with evaluator-derived state."""

    family: FixedH3PromptFamily
    status: FixedH3EvidenceStatus = field(default=FixedH3EvidenceStatus.MISSING, init=False)
    reason: str = field(init=False)
    prompt_count: int = field(default=0, init=False)
    prompt_fingerprints: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        _require_exact_enum(self.family, FixedH3PromptFamily, "prompt family")
        object.__setattr__(self, "reason", _FAMILY_REASONS[self.family])
        self.require_admitted()

    def require_admitted(self) -> FixedH3FamilyReceipt:
        if type(self) is not FixedH3FamilyReceipt:
            raise FixedH3Error("prompt-family receipt must be an exact admitted value")
        _require_exact_enum(self.family, FixedH3PromptFamily, "prompt family")
        _require_exact_value(self.status, FixedH3EvidenceStatus.MISSING, "prompt status")
        _require_exact_value(self.reason, _FAMILY_REASONS[self.family], "prompt reason")
        _require_exact_value(self.prompt_count, 0, "prompt count")
        _require_exact_string_tuple(self.prompt_fingerprints, (), "prompt fingerprints")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "family": self.family.value,
            "status": self.status.value,
            "reason": self.reason,
            "prompt_count": self.prompt_count,
            "prompt_fingerprints": list(self.prompt_fingerprints),
        }


@dataclass(frozen=True, slots=True)
class FixedH3MetricReceipt:
    """One unmeasured metric; no direction, scale, or margin is invented."""

    metric: FixedH3Metric
    status: FixedH3EvidenceStatus = field(default=FixedH3EvidenceStatus.MISSING, init=False)
    direction: str = field(default="MISSING", init=False)
    scale: str = field(default="MISSING", init=False)
    noninferiority_margin: None = field(default=None, init=False)
    observation_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        _require_exact_enum(self.metric, FixedH3Metric, "fixed-H3 metric")
        self.require_admitted()

    def require_admitted(self) -> FixedH3MetricReceipt:
        if type(self) is not FixedH3MetricReceipt:
            raise FixedH3Error("metric receipt must be an exact admitted value")
        _require_exact_enum(self.metric, FixedH3Metric, "fixed-H3 metric")
        _require_exact_value(self.status, FixedH3EvidenceStatus.MISSING, "metric status")
        _require_exact_value(self.direction, "MISSING", "metric direction")
        _require_exact_value(self.scale, "MISSING", "metric scale")
        _require_exact_value(self.noninferiority_margin, None, "metric margin")
        _require_exact_value(self.observation_count, 0, "metric observation count")
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "metric": self.metric.value,
            "status": self.status.value,
            "direction": self.direction,
            "scale": self.scale,
            "noninferiority_margin": self.noninferiority_margin,
            "observation_count": self.observation_count,
        }


@dataclass(frozen=True, slots=True)
class FixedH3NativePrerequisite:
    """Pinned requirements for a future study, explicitly not runtime evidence."""

    status: FixedH3EvidenceStatus = field(default=FixedH3EvidenceStatus.MISSING, init=False)
    host_version: str = field(default=M14_05_NATIVE_H3_HOST_VERSION, init=False)
    host_revision: str = field(default=M14_05_NATIVE_H3_HOST_REVISION, init=False)
    source_blob: str = field(default=M14_05_NATIVE_H3_SOURCE_BLOB, init=False)
    schema: str = field(default=NATIVE_H3_WIRING_SCHEMA, init=False)
    classes: tuple[str, ...] = field(
        default=(
            "EmptyMiniMaxH3LatentAV",
            NATIVE_H3_IMAGE_NODE_ID,
            NATIVE_H3_REFERENCE_NODE_ID,
            "MiniMaxH3SigmaShift",
        ),
        init=False,
    )
    prompt_socket: str = field(default="STRING", init=False)
    media_socket: str = field(default="V3_MEDIA", init=False)
    required_model_inputs: tuple[str, ...] = field(
        default=("audio_vae", "clip", "model", "vae"), init=False
    )
    task_modes: tuple[str, ...] = field(default=("t2va", "i2va", "fl2va", "ref2va"), init=False)
    executable_workflow_admitted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> FixedH3NativePrerequisite:
        if type(self) is not FixedH3NativePrerequisite:
            raise FixedH3Error("native prerequisite must be an exact admitted value")
        for value, expected, field_name in (
            (self.status, FixedH3EvidenceStatus.MISSING, "native status"),
            (self.host_version, M14_05_NATIVE_H3_HOST_VERSION, "native host version"),
            (self.host_revision, M14_05_NATIVE_H3_HOST_REVISION, "native host revision"),
            (self.source_blob, M14_05_NATIVE_H3_SOURCE_BLOB, "native source blob"),
            (self.schema, NATIVE_H3_WIRING_SCHEMA, "native schema"),
            (self.prompt_socket, "STRING", "native prompt socket"),
            (self.media_socket, "V3_MEDIA", "native media socket"),
            (self.executable_workflow_admitted, False, "native workflow admission"),
        ):
            _require_exact_value(value, expected, field_name)
        _require_exact_string_tuple(
            self.classes,
            (
                "EmptyMiniMaxH3LatentAV",
                NATIVE_H3_IMAGE_NODE_ID,
                NATIVE_H3_REFERENCE_NODE_ID,
                "MiniMaxH3SigmaShift",
            ),
            "native classes",
        )
        _require_exact_string_tuple(
            self.required_model_inputs,
            ("audio_vae", "clip", "model", "vae"),
            "native model inputs",
        )
        _require_exact_string_tuple(
            self.task_modes, ("t2va", "i2va", "fl2va", "ref2va"), "native task modes"
        )
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "status": self.status.value,
            "host_version": self.host_version,
            "host_revision": self.host_revision,
            "source_blob": self.source_blob,
            "schema": self.schema,
            "classes": list(self.classes),
            "prompt_socket": self.prompt_socket,
            "media_socket": self.media_socket,
            "required_model_inputs": list(self.required_model_inputs),
            "task_modes": list(self.task_modes),
            "executable_workflow_admitted": self.executable_workflow_admitted,
        }


def _new_prompt_families() -> tuple[FixedH3FamilyReceipt, ...]:
    return tuple(FixedH3FamilyReceipt(family) for family in FixedH3PromptFamily)


def _new_metrics() -> tuple[FixedH3MetricReceipt, ...]:
    return tuple(FixedH3MetricReceipt(metric) for metric in FixedH3Metric)


def _new_native_prerequisites() -> FixedH3NativePrerequisite:
    return FixedH3NativePrerequisite()


_LIMITATIONS = (
    "fixed_h3_execution_not_authorized",
    "fixed_h3_model_unavailable",
    "fixed_h3_study_media_unavailable",
    "fixed_h3_workflow_unavailable",
    "official_prompt_unavailable",
    "assisted_profile_unqualified",
    "ablation_prompts_unavailable",
    "no_generation_quality_claim",
    "no_official_noninferiority_claim",
)


@dataclass(frozen=True, slots=True)
class FixedH3TerminalRecord:
    """Exact zero-execution terminal record; every field is constructor-owned."""

    schema: str = field(default=FIXED_H3_GENERATION_SCHEMA, init=False)
    record_date: str = field(default=FIXED_H3_RECORD_DATE, init=False)
    disposition: FixedH3Disposition = field(default=FixedH3Disposition.UNAVAILABLE, init=False)
    claim_cap: FixedH3ClaimCap = field(
        default=FixedH3ClaimCap.NO_FIXED_H3_GENERATION_EVIDENCE, init=False
    )
    official_terminal_disposition_sha256: str = field(
        default=M14_01_TERMINAL_DISPOSITION_SHA256, init=False
    )
    official_behavior_profile_fingerprint: str = field(
        default=M14_03_BEHAVIOR_PROFILE_FINGERPRINT, init=False
    )
    official_behavior_profile_disposition: str = field(default="UNAVAILABLE", init=False)
    local_qualification_fingerprint: str = field(
        default=M14_04_LOCAL_QUALIFICATION_FINGERPRINT, init=False
    )
    local_qualification_plan_fingerprint: str = field(
        default=M14_04_LOCAL_QUALIFICATION_PLAN_FINGERPRINT, init=False
    )
    local_product_scope: str = field(default="MANUAL_ONLY_SCOPED", init=False)
    prompt_families: tuple[FixedH3FamilyReceipt, ...] = field(
        default_factory=_new_prompt_families, init=False
    )
    metrics: tuple[FixedH3MetricReceipt, ...] = field(default_factory=_new_metrics, init=False)
    native_prerequisites: FixedH3NativePrerequisite = field(
        default_factory=_new_native_prerequisites, init=False
    )
    authorization_granted: bool = field(default=False, init=False)
    host_started: bool = field(default=False, init=False)
    model_loaded: bool = field(default=False, init=False)
    media_opened: bool = field(default=False, init=False)
    network_contacted: bool = field(default=False, init=False)
    provider_selected: bool = field(default=False, init=False)
    gpu_used: bool = field(default=False, init=False)
    output_artifact_created: bool = field(default=False, init=False)
    reference_code_executed: bool = field(default=False, init=False)
    execution_status: FixedH3EvidenceStatus = field(
        default=FixedH3EvidenceStatus.MISSING, init=False
    )
    model_evidence_status: FixedH3EvidenceStatus = field(
        default=FixedH3EvidenceStatus.MISSING, init=False
    )
    media_evidence_status: FixedH3EvidenceStatus = field(
        default=FixedH3EvidenceStatus.MISSING, init=False
    )
    workflow_evidence_status: FixedH3EvidenceStatus = field(
        default=FixedH3EvidenceStatus.MISSING, init=False
    )
    generation_attempt_count: int = field(default=0, init=False)
    generation_failure_count: int = field(default=0, init=False)
    retained_output_count: int = field(default=0, init=False)
    scored_output_count: int = field(default=0, init=False)
    published_output_count: int = field(default=0, init=False)
    run_receipts: tuple[str, ...] = field(default=(), init=False)
    output_receipts: tuple[str, ...] = field(default=(), init=False)
    score_receipts: tuple[str, ...] = field(default=(), init=False)
    locators: tuple[str, ...] = field(default=(), init=False)
    primary_margin: None = field(default=None, init=False)
    confidence_interval: None = field(default=None, init=False)
    aggregate_score: None = field(default=None, init=False)
    official_noninferiority: None = field(default=None, init=False)
    promotion_authorized: bool = field(default=False, init=False)
    limitations: tuple[str, ...] = field(default=_LIMITATIONS, init=False)

    def __post_init__(self) -> None:
        self.require_admitted()

    def require_admitted(self) -> FixedH3TerminalRecord:
        if type(self) is not FixedH3TerminalRecord:
            raise FixedH3Error("terminal record must be an exact admitted value")
        _verify_predecessor_authorities()
        for value, expected, field_name in (
            (self.schema, FIXED_H3_GENERATION_SCHEMA, "terminal schema"),
            (self.record_date, FIXED_H3_RECORD_DATE, "terminal record date"),
            (self.disposition, FixedH3Disposition.UNAVAILABLE, "terminal disposition"),
            (
                self.claim_cap,
                FixedH3ClaimCap.NO_FIXED_H3_GENERATION_EVIDENCE,
                "terminal claim cap",
            ),
            (
                self.official_terminal_disposition_sha256,
                M14_01_TERMINAL_DISPOSITION_SHA256,
                "official terminal authority",
            ),
            (
                self.official_behavior_profile_fingerprint,
                M14_03_BEHAVIOR_PROFILE_FINGERPRINT,
                "official behavior authority",
            ),
            (
                self.official_behavior_profile_disposition,
                "UNAVAILABLE",
                "official behavior disposition",
            ),
            (
                self.local_qualification_fingerprint,
                M14_04_LOCAL_QUALIFICATION_FINGERPRINT,
                "local qualification authority",
            ),
            (
                self.local_qualification_plan_fingerprint,
                M14_04_LOCAL_QUALIFICATION_PLAN_FINGERPRINT,
                "local qualification plan authority",
            ),
            (self.local_product_scope, "MANUAL_ONLY_SCOPED", "local product scope"),
            (self.authorization_granted, False, "authorization"),
            (self.host_started, False, "host start"),
            (self.model_loaded, False, "model load"),
            (self.media_opened, False, "media open"),
            (self.network_contacted, False, "network contact"),
            (self.provider_selected, False, "provider selection"),
            (self.gpu_used, False, "GPU use"),
            (self.output_artifact_created, False, "output creation"),
            (self.reference_code_executed, False, "reference execution"),
            (self.execution_status, FixedH3EvidenceStatus.MISSING, "execution status"),
            (self.model_evidence_status, FixedH3EvidenceStatus.MISSING, "model status"),
            (self.media_evidence_status, FixedH3EvidenceStatus.MISSING, "media status"),
            (self.workflow_evidence_status, FixedH3EvidenceStatus.MISSING, "workflow status"),
            (self.generation_attempt_count, 0, "generation attempt count"),
            (self.generation_failure_count, 0, "generation failure count"),
            (self.retained_output_count, 0, "retained output count"),
            (self.scored_output_count, 0, "scored output count"),
            (self.published_output_count, 0, "published output count"),
            (self.primary_margin, None, "primary margin"),
            (self.confidence_interval, None, "confidence interval"),
            (self.aggregate_score, None, "aggregate score"),
            (self.official_noninferiority, None, "official noninferiority"),
            (self.promotion_authorized, False, "promotion authorization"),
        ):
            _require_exact_value(value, expected, field_name)
        for receipt_inventory, inventory_name in (
            (self.run_receipts, "run receipts"),
            (self.output_receipts, "output receipts"),
            (self.score_receipts, "score receipts"),
            (self.locators, "locators"),
        ):
            _require_exact_string_tuple(receipt_inventory, (), inventory_name)
        _require_exact_string_tuple(self.limitations, _LIMITATIONS, "limitations")
        if type(self.prompt_families) is not tuple or len(self.prompt_families) != len(
            FixedH3PromptFamily
        ):
            raise FixedH3Error("prompt-family inventory is not admitted")
        for family_receipt, family in zip(self.prompt_families, FixedH3PromptFamily, strict=True):
            if type(family_receipt) is not FixedH3FamilyReceipt:
                raise FixedH3Error("prompt-family inventory contains a non-receipt")
            family_receipt.require_admitted()
            if family_receipt.family is not family:
                raise FixedH3Error("prompt-family inventory order drifted")
        if type(self.metrics) is not tuple or len(self.metrics) != len(FixedH3Metric):
            raise FixedH3Error("metric inventory is not admitted")
        for metric_receipt, metric in zip(self.metrics, FixedH3Metric, strict=True):
            if type(metric_receipt) is not FixedH3MetricReceipt:
                raise FixedH3Error("metric inventory contains a non-receipt")
            metric_receipt.require_admitted()
            if metric_receipt.metric is not metric:
                raise FixedH3Error("metric inventory order drifted")
        if type(self.native_prerequisites) is not FixedH3NativePrerequisite:
            raise FixedH3Error("native prerequisites are not admitted")
        self.native_prerequisites.require_admitted()
        return self

    def to_wire(self) -> dict[str, object]:
        self.require_admitted()
        return {
            "schema": self.schema,
            "record_date": self.record_date,
            "disposition": self.disposition.value,
            "claim_cap": self.claim_cap.value,
            "official_terminal_disposition_sha256": self.official_terminal_disposition_sha256,
            "official_behavior_profile_fingerprint": self.official_behavior_profile_fingerprint,
            "official_behavior_profile_disposition": self.official_behavior_profile_disposition,
            "local_qualification_fingerprint": self.local_qualification_fingerprint,
            "local_qualification_plan_fingerprint": self.local_qualification_plan_fingerprint,
            "local_product_scope": self.local_product_scope,
            "prompt_families": [receipt.to_wire() for receipt in self.prompt_families],
            "metrics": [receipt.to_wire() for receipt in self.metrics],
            "native_prerequisites": self.native_prerequisites.to_wire(),
            "authorization_granted": self.authorization_granted,
            "host_started": self.host_started,
            "model_loaded": self.model_loaded,
            "media_opened": self.media_opened,
            "network_contacted": self.network_contacted,
            "provider_selected": self.provider_selected,
            "gpu_used": self.gpu_used,
            "output_artifact_created": self.output_artifact_created,
            "reference_code_executed": self.reference_code_executed,
            "execution_status": self.execution_status.value,
            "model_evidence_status": self.model_evidence_status.value,
            "media_evidence_status": self.media_evidence_status.value,
            "workflow_evidence_status": self.workflow_evidence_status.value,
            "generation_attempt_count": self.generation_attempt_count,
            "generation_failure_count": self.generation_failure_count,
            "retained_output_count": self.retained_output_count,
            "scored_output_count": self.scored_output_count,
            "published_output_count": self.published_output_count,
            "run_receipts": list(self.run_receipts),
            "output_receipts": list(self.output_receipts),
            "score_receipts": list(self.score_receipts),
            "locators": list(self.locators),
            "primary_margin": self.primary_margin,
            "confidence_interval": self.confidence_interval,
            "aggregate_score": self.aggregate_score,
            "official_noninferiority": self.official_noninferiority,
            "promotion_authorized": self.promotion_authorized,
            "limitations": list(self.limitations),
        }

    def to_wire_bytes(self) -> bytes:
        return json.dumps(
            self.to_wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())


def _verify_predecessor_authorities() -> None:
    if FROZEN_EVALUATION_BUNDLE_FINGERPRINT != (
        "sha256:a86a67781151dc7fb46a8739964a4efe4d6cc3ca11173377826bbd3b4056cf7d"
    ):
        raise FixedH3Error("semantic evaluation authority drifted")
    report = evaluate_local_qualification(build_default_local_qualification_plan())
    if (
        report.product_scope is not ProductScopeDisposition.MANUAL_ONLY_SCOPED
        or report.fingerprint != M14_04_LOCAL_QUALIFICATION_FINGERPRINT
        or report.plan_fingerprint != M14_04_LOCAL_QUALIFICATION_PLAN_FINGERPRINT
    ):
        raise FixedH3Error("local qualification authority drifted")


def build_fixed_h3_terminal_record() -> FixedH3TerminalRecord:
    """Build the only admitted record; this performs no host, model, media, or network work."""

    record = FixedH3TerminalRecord()
    if len(record.to_wire_bytes()) > MAX_FIXED_H3_WIRE_BYTES:
        raise FixedH3Error("terminal fixed-H3 record exceeds its portable bound")
    if record.fingerprint != FROZEN_FIXED_H3_TERMINAL_FINGERPRINT:
        raise FixedH3Error("terminal fixed-H3 record fingerprint drifted")
    return record


def _assert_exact_json_member_types(value: object, *, depth: int = 0) -> None:
    """Reject Python equality/member tricks before any semantic comparison."""

    if depth > MAX_FIXED_H3_DEPTH:
        raise FixedH3Error("terminal fixed-H3 wire exceeds its depth bound")
    value_type = type(value)
    if value_type is dict:
        mapping = cast(dict[object, object], value)
        if len(mapping) > MAX_FIXED_H3_CONTAINER_ITEMS:
            raise FixedH3Error("terminal fixed-H3 object exceeds its item bound")
        for key, member in mapping.items():
            if type(key) is not str:
                raise FixedH3Error("terminal fixed-H3 member names must be exact strings")
            if len(key) > 64 or key.casefold() in _SENSITIVE_KEYS:
                raise FixedH3Error("terminal fixed-H3 member name is unsafe")
            _assert_exact_json_member_types(member, depth=depth + 1)
        return
    if value_type is list:
        sequence = cast(list[object], value)
        if len(sequence) > MAX_FIXED_H3_CONTAINER_ITEMS:
            raise FixedH3Error("terminal fixed-H3 list exceeds its item bound")
        for member in sequence:
            _assert_exact_json_member_types(member, depth=depth + 1)
        return
    if value_type is str:
        text = cast(str, value)
        if len(text) > MAX_FIXED_H3_STRING_LENGTH:
            raise FixedH3Error("terminal fixed-H3 string exceeds its bound")
        if any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in text):
            raise FixedH3Error("terminal fixed-H3 string contains an unsafe code point")
        lowered = text.casefold()
        if any(marker in lowered for marker in _FORBIDDEN_VALUE_MARKERS):
            raise FixedH3Error("terminal fixed-H3 string contains an unsafe locator")
        return
    if value_type in {bool, int, type(None)}:
        if value_type is int and not -1_000_000 <= cast(int, value) <= 1_000_000:
            raise FixedH3Error("terminal fixed-H3 integer exceeds its bound")
        return
    raise FixedH3Error("terminal fixed-H3 wire contains a non-JSON member type")


def validate_fixed_h3_terminal_wire(value: object) -> FixedH3TerminalRecord:
    """Validate a Python mapping by exact regeneration from accepted authorities."""

    # CRITICAL: exact JSON-domain validation must precede set/dict equality; otherwise hostile
    # Python subclasses can invoke caller-controlled equality inside this evidence boundary.
    _assert_exact_json_member_types(value)
    if type(value) is not dict:
        raise FixedH3Error("terminal fixed-H3 wire must be a closed object")
    mapping = cast(dict[str, object], value)
    try:
        encoded = json.dumps(
            mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FixedH3Error("terminal fixed-H3 wire is not canonical JSON") from exc
    if len(encoded) > MAX_FIXED_H3_WIRE_BYTES:
        raise FixedH3Error("terminal fixed-H3 wire exceeds its portable bound")
    expected = build_fixed_h3_terminal_record()
    if mapping != expected.to_wire():
        raise FixedH3Error("terminal fixed-H3 wire is not evaluator-derived")
    return expected


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FixedH3Error("terminal fixed-H3 JSON contains duplicate members")
        result[key] = value
    return result


def _parse_bounded_integer(token: str) -> int:
    if len(token) > 16:
        raise FixedH3Error("terminal fixed-H3 JSON integer exceeds its bound")
    return int(token)


def _reject_nonfinite(_token: str) -> object:
    raise FixedH3Error("terminal fixed-H3 JSON contains a non-finite value")


def decode_fixed_h3_terminal_json(payload: str | bytes | bytearray) -> FixedH3TerminalRecord:
    """Decode bounded duplicate-aware JSON and validate its exact terminal semantics."""

    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise FixedH3Error("terminal fixed-H3 JSON contains an unsafe code point") from exc
        text = payload
    elif type(payload) is bytes:
        encoded = payload
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise FixedH3Error("terminal fixed-H3 JSON is not strict UTF-8") from exc
    elif type(payload) is bytearray:
        encoded = bytes(payload)
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise FixedH3Error("terminal fixed-H3 JSON is not strict UTF-8") from exc
    else:
        raise FixedH3Error("terminal fixed-H3 JSON must be text or bytes")
    if len(encoded) > MAX_FIXED_H3_WIRE_BYTES:
        raise FixedH3Error("terminal fixed-H3 JSON exceeds its portable bound")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_int=_parse_bounded_integer,
            parse_constant=_reject_nonfinite,
        )
    except FixedH3Error:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise FixedH3Error("terminal fixed-H3 JSON is invalid") from exc
    return validate_fixed_h3_terminal_wire(value)


__all__ = [
    "FIXED_H3_GENERATION_SCHEMA",
    "FIXED_H3_RECORD_DATE",
    "MAX_FIXED_H3_WIRE_BYTES",
    "M14_03_BEHAVIOR_PROFILE_FINGERPRINT",
    "M14_01_TERMINAL_DISPOSITION_SHA256",
    "M14_04_LOCAL_QUALIFICATION_FINGERPRINT",
    "M14_04_LOCAL_QUALIFICATION_PLAN_FINGERPRINT",
    "FROZEN_FIXED_H3_TERMINAL_FINGERPRINT",
    "FixedH3Error",
    "FixedH3Disposition",
    "FixedH3ClaimCap",
    "FixedH3EvidenceStatus",
    "FixedH3PromptFamily",
    "FixedH3Metric",
    "FixedH3FamilyReceipt",
    "FixedH3MetricReceipt",
    "FixedH3NativePrerequisite",
    "FixedH3TerminalRecord",
    "build_fixed_h3_terminal_record",
    "validate_fixed_h3_terminal_wire",
    "decode_fixed_h3_terminal_json",
]
