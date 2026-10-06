"""Qualified semantic-proposal source and provider authority.

This module owns the closed M17-16 catalog and deterministic source derivation.  Provider I/O and
the process-local one-time review authority are intentionally added in the same bounded module so
the content-bearing values never need a portable or browser-visible representation.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, NoReturn, cast

from .canonical import canonical_fingerprint
from .constrained_semantic_planning import (
    CONSTRAINED_SEMANTIC_PLANNING_SCHEMA,
    TYPED_SEMANTIC_PLANNING_SCHEMA,
    ExactTextSnapshot,
    SemanticPlanningBudget,
    SemanticPlanningPolicy,
    SemanticPlanningProfile,
    SemanticPlanningRequest,
    SemanticPlanningResult,
    SemanticPlanningStatus,
    SemanticTargetKind,
    build_semantic_generation_request,
    parse_semantic_proposal,
)
from .constraints import TimePoint
from .context_reporting import ContextReport
from .contracts import EvidenceLevel, ProviderIdentity
from .evidence import (
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSource,
    EvidenceSourceKind,
    Provenance,
    SupportStatus,
)
from .feasible_av_timeline_planner import (
    FeasibleAVTimelineRequest,
    FrameGridPolicy,
    ReferenceAnchor,
    ShotDraft,
    TimelinePlannerStatus,
    build_feasible_av_timeline,
)
from .hierarchical_evidence_reduction import (
    HierarchicalEvidenceReductionRequest,
    ReductionBudget,
    ReductionEvidence,
    ReductionLevel,
    ReductionModality,
    ReductionStatus,
    ReductionTarget,
    build_hierarchical_evidence_reduction,
)
from .local_adapters import LocalDeviceKind, LocalDeviceSpec, LocalResourceBudget
from .model_manifest import (
    MAX_MODEL_JSON_KEYS,
    ModelBackendFamily,
    ModelCapability,
    ModelCapabilityState,
    ModelGenerationRequest,
    ModelGenerationResult,
    ModelManifest,
    ModelRuntimeProfile,
)
from .native_h3 import NativeH3Wiring, assert_native_h3_wiring_authority
from .prompt_model_provider import ModelMetadata
from .provider_setup import ProviderSetup
from .segment_workspace import (
    AcceptedIntentAuthority,
    MultiSegmentWorkspace,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    create_workspace,
)
from .semantic_enrichment import (
    EnrichmentTargetKind,
    SemanticEnrichmentResult,
    SemanticEnrichmentStatus,
    SemanticProposal,
    apply_semantic_enrichment,
)
from .semantic_intents import (
    SemanticIntentError,
    semantic_dialogue_catalog,
    validate_semantic_dialogue_bindings,
)
from .semantic_proposal_transaction import (
    SemanticProposalAuthorization,
    SemanticProposalTransaction,
    SemanticProposalTransactionState,
    prepare_semantic_proposal_transaction,
)
from .task_mode_retention_classifier import (
    TaskModeRetentionRequest,
    build_task_mode_retention_report,
)
from .unified_evidence_graph import build_unified_evidence_graph

SEMANTIC_PROVIDER_CATALOG_SCHEMA = "h3.semantic.provider_profiles.v1"
SEMANTIC_CONNECTION_CATALOG_SCHEMA = "h3.semantic.provider_profiles.v2"
SEMANTIC_MODEL_MAX_BYTES = 96 * 1024 * 1024 * 1024
FIXED_OLLAMA_ENDPOINT = "http://127.0.0.1:11434/api"
_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "contracts" / "semantic_provider_profiles_v1.json"
)
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_NATIVE_DIGEST = re.compile(r"[0-9a-f]{64}")
_MODEL_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ROOT_KEYS = frozenset({"schema", "profiles"})
_PROFILE_KEYS = frozenset(
    {
        "profile_id",
        "endpoint",
        "model_id",
        "model_digest",
        "server_version",
        "model_family",
        "model_format",
        "parameter_size",
        "quantization_level",
        "model_size_bytes",
        "context_length",
        "embedding_length",
        "capabilities",
        "adapter_version",
        "parser_version",
        "license_id",
        "license_source",
        "license_text_sha256",
        "show_identity_sha256",
        "raw_media_capability",
    }
)


class SemanticProposalProducerError(ValueError):
    """Closed, content-free M17-16 failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise SemanticProposalProducerError(code)


def _exact_text(value: object, field: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail(f"catalog_{field}")
    return value


def _exact_keys(value: object, expected: frozenset[str], field: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        _fail(f"catalog_{field}_members")
    return value


def _reject_constant(_: str) -> NoReturn:
    _fail("catalog_non_finite")


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("catalog_duplicate_member")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class OllamaNativeDigest:
    """A digest in Ollama's native wire domain (bare lowercase hexadecimal)."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or _NATIVE_DIGEST.fullmatch(self.value) is None:
            _fail("native_digest_invalid")


def parse_ollama_native_digest(value: object) -> OllamaNativeDigest:
    if type(value) is not str or _NATIVE_DIGEST.fullmatch(value) is None:
        _fail("native_digest_invalid")
    return OllamaNativeDigest(value)


def ollama_native_digest_to_model_digest(value: object) -> str:
    if type(value) is not OllamaNativeDigest:
        _fail("native_digest_type")
    return f"sha256:{value.value}"


@dataclass(frozen=True, slots=True)
class SemanticProviderProfile:
    profile_id: str
    endpoint: str
    model_id: str
    model_digest: str
    server_version: str
    model_family: str
    model_format: str
    parameter_size: str
    quantization_level: str
    model_size_bytes: int
    context_length: int
    embedding_length: int
    capabilities: tuple[str, ...]
    adapter_version: str
    parser_version: str
    license_id: str
    license_source: str
    license_text_sha256: str
    show_identity_sha256: str
    raw_media_capability: bool

    def __post_init__(self) -> None:
        if _PROFILE_ID.fullmatch(self.profile_id) is None:
            _fail("catalog_profile_id")
        if self.endpoint != FIXED_OLLAMA_ENDPOINT:
            _fail("catalog_endpoint")
        for value, field_name in (
            (self.model_id, "model_id"),
            (self.model_family, "model_family"),
            (self.model_format, "model_format"),
            (self.parameter_size, "parameter_size"),
            (self.quantization_level, "quantization_level"),
            (self.adapter_version, "adapter_version"),
            (self.parser_version, "parser_version"),
            (self.license_id, "license_id"),
            (self.license_source, "license_source"),
        ):
            _exact_text(value, field_name)
        model_id_folded = self.model_id.casefold()
        if model_id_folded.endswith(":cloud") or model_id_folded.endswith("-cloud"):
            _fail("catalog_cloud_model")
        if _MODEL_DIGEST.fullmatch(self.model_digest) is None:
            _fail("catalog_model_digest")
        if _SAFE_VERSION.fullmatch(self.server_version) is None:
            _fail("catalog_server_version")
        if type(self.model_size_bytes) is not int or self.model_size_bytes <= 0:
            _fail("catalog_model_size")
        if type(self.context_length) is not int or not 0 < self.context_length <= 16_777_216:
            _fail("catalog_context_length")
        if type(self.embedding_length) is not int or not 0 < self.embedding_length <= 1_048_576:
            _fail("catalog_embedding_length")
        if type(self.capabilities) is not tuple or self.capabilities != tuple(
            sorted(set(self.capabilities))
        ):
            _fail("catalog_capabilities")
        if self.capabilities != ("completion", "structured_output"):
            _fail("catalog_capabilities")
        if _MODEL_DIGEST.fullmatch(self.license_text_sha256) is None:
            _fail("catalog_license_digest")
        if _MODEL_DIGEST.fullmatch(self.show_identity_sha256) is None:
            _fail("catalog_show_identity")
        if self.raw_media_capability is not False:
            _fail("catalog_raw_media")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @property
    def planning_profile(self) -> SemanticPlanningProfile:
        return SemanticPlanningProfile(
            profile_id=self.profile_id,
            backend_family=ModelBackendFamily.OLLAMA,
            model_id=self.model_id,
            model_digest=self.model_digest,
            seed=0,
            raw_media_capability=False,
        )

    @property
    def model_manifest(self) -> ModelManifest:
        return ModelManifest(
            manifest_id="h3.semantic.qwen3_8.27b_bf16",
            backend_family=ModelBackendFamily.OLLAMA,
            adapter_id="ollama.semantic",
            adapter_version=self.adapter_version,
            model_id=self.model_id,
            model_digest=self.model_digest,
            checkpoint_fingerprint=self.model_digest,
            detected_family=self.model_family,
            clip_type=None,
            tokenizer_processor="ollama",
            generation_weights_complete=True,
            capabilities=frozenset(
                {ModelCapability.TEXT_GENERATION, ModelCapability.STRUCTURED_OUTPUT}
            ),
            structured_output_schema="h3.semantic.proposal_json.v1",
            parser_path="h3.semantic.proposal_json.v1",
            runtime=ModelRuntimeProfile(
                device=LocalDeviceSpec(LocalDeviceKind.AUTO),
                dtype="bf16",
                offload="ollama",
                max_context_tokens=self.context_length,
                max_output_tokens=2_048,
                max_media_items=1,
                limits=LocalResourceBudget(
                    max_memory_bytes=64 * 1024 * 1024 * 1024,
                    max_wall_time_seconds=120.0,
                    max_references=0,
                    max_output_bytes=262_144,
                    max_output_items=64,
                    max_concurrency=1,
                ),
            ),
            cancellation=ModelCapabilityState.QUALIFIED,
            license="apache-2.0",
            host_profile="ollama.local",
            evidence_level=EvidenceLevel.FRAMEWORK_REFERENCE,
            approved=True,
            notes="Exact reviewed local semantic-proposal profile; raw media is prohibited.",
            server_version=self.server_version,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "server_version": self.server_version,
            "model_family": self.model_family,
            "model_format": self.model_format,
            "parameter_size": self.parameter_size,
            "quantization_level": self.quantization_level,
            "model_size_bytes": self.model_size_bytes,
            "context_length": self.context_length,
            "embedding_length": self.embedding_length,
            "capabilities": list(self.capabilities),
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "license_id": self.license_id,
            "license_source": self.license_source,
            "license_text_sha256": self.license_text_sha256,
            "show_identity_sha256": self.show_identity_sha256,
            "raw_media_capability": self.raw_media_capability,
        }


@dataclass(frozen=True, slots=True)
class SemanticProviderCatalog:
    schema: str
    profiles: tuple[SemanticProviderProfile, ...]

    def __post_init__(self) -> None:
        if self.schema != SEMANTIC_PROVIDER_CATALOG_SCHEMA:
            _fail("catalog_schema")
        if type(self.profiles) is not tuple or len(self.profiles) != 1:
            _fail("catalog_profile_count")
        if len({profile.profile_id for profile in self.profiles}) != len(self.profiles):
            _fail("catalog_duplicate_profile")

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(profile.profile_id for profile in self.profiles)

    def require(self, profile_id: object) -> SemanticProviderProfile:
        if type(profile_id) is not str:
            _fail("profile_unavailable")
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        _fail("profile_unavailable")


def _decode_catalog(raw: bytes) -> SemanticProviderCatalog:
    if len(raw) > 65_536:
        _fail("catalog_size")
    try:
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except SemanticProposalProducerError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticProposalProducerError("catalog_json") from exc
    root = _exact_keys(decoded, _ROOT_KEYS, "root")
    if root["schema"] != SEMANTIC_PROVIDER_CATALOG_SCHEMA:
        _fail("catalog_schema")
    values = root["profiles"]
    if type(values) is not list or len(values) != 1:
        _fail("catalog_profile_count")
    profile_values = _exact_keys(values[0], _PROFILE_KEYS, "profile")
    capabilities = profile_values["capabilities"]
    if type(capabilities) is not list or not all(type(item) is str for item in capabilities):
        _fail("catalog_capabilities")
    return SemanticProviderCatalog(
        schema=SEMANTIC_PROVIDER_CATALOG_SCHEMA,
        profiles=(
            SemanticProviderProfile(
                profile_id=cast(str, profile_values["profile_id"]),
                endpoint=cast(str, profile_values["endpoint"]),
                model_id=cast(str, profile_values["model_id"]),
                model_digest=cast(str, profile_values["model_digest"]),
                server_version=cast(str, profile_values["server_version"]),
                model_family=cast(str, profile_values["model_family"]),
                model_format=cast(str, profile_values["model_format"]),
                parameter_size=cast(str, profile_values["parameter_size"]),
                quantization_level=cast(str, profile_values["quantization_level"]),
                model_size_bytes=cast(int, profile_values["model_size_bytes"]),
                context_length=cast(int, profile_values["context_length"]),
                embedding_length=cast(int, profile_values["embedding_length"]),
                capabilities=cast(tuple[str, ...], tuple(capabilities)),
                adapter_version=cast(str, profile_values["adapter_version"]),
                parser_version=cast(str, profile_values["parser_version"]),
                license_id=cast(str, profile_values["license_id"]),
                license_source=cast(str, profile_values["license_source"]),
                license_text_sha256=cast(str, profile_values["license_text_sha256"]),
                show_identity_sha256=cast(str, profile_values["show_identity_sha256"]),
                raw_media_capability=cast(bool, profile_values["raw_media_capability"]),
            ),
        ),
    )


def load_semantic_provider_catalog() -> SemanticProviderCatalog:
    try:
        return _decode_catalog(_CATALOG_PATH.read_bytes())
    except OSError:
        raise SemanticProposalProducerError("catalog_unavailable") from None


@dataclass(frozen=True, slots=True)
class SemanticProviderConnection:
    """Package-owned local connection; no model or inferred weight facts."""

    profile_id: str
    endpoint: str
    adapter_version: str
    parser_version: str
    required_capability: str
    structured_format: str
    max_model_bytes: int
    max_action_seconds: int

    def __post_init__(self) -> None:
        if (
            self.profile_id != "ollama.local"
            or self.endpoint != FIXED_OLLAMA_ENDPOINT
            or self.adapter_version != "1.1.0"
            or self.parser_version != "1.0.0"
            or self.required_capability != "completion"
            or self.structured_format != "format"
            or type(self.max_model_bytes) is not int
            or self.max_model_bytes != SEMANTIC_MODEL_MAX_BYTES
            or type(self.max_action_seconds) is not int
            or self.max_action_seconds != 120
        ):
            _fail("connection_authority")

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "endpoint": self.endpoint,
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "required_capability": self.required_capability,
            "structured_format": self.structured_format,
            "max_model_bytes": self.max_model_bytes,
            "max_action_seconds": self.max_action_seconds,
        }


def load_semantic_connection() -> SemanticProviderConnection:
    path = _CATALOG_PATH.with_name("semantic_provider_profiles_v2.json")
    try:
        raw = path.read_bytes()
        if len(raw) > 65_536:
            _fail("catalog_size")
        root = _exact_keys(
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_closed_object,
                parse_constant=_reject_constant,
            ),
            _ROOT_KEYS,
            "root",
        )
        if root["schema"] != SEMANTIC_CONNECTION_CATALOG_SCHEMA:
            _fail("catalog_schema")
        profiles = root["profiles"]
        if type(profiles) is not list or len(profiles) != 1:
            _fail("catalog_profile_count")
        row = _exact_keys(
            profiles[0],
            frozenset(
                {
                    "profile_id",
                    "endpoint",
                    "adapter_version",
                    "parser_version",
                    "required_capability",
                    "structured_format",
                    "max_model_bytes",
                    "max_action_seconds",
                }
            ),
            "connection",
        )
        return SemanticProviderConnection(**cast(Any, row))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise SemanticProposalProducerError("catalog_unavailable") from None


@dataclass(frozen=True, slots=True)
class SemanticChosenModelProfile:
    """One exact observed model, private to a source-bound semantic action."""

    connection: SemanticProviderConnection
    model_id: str
    model_size_bytes: int
    server_version: str
    metadata: ModelMetadata

    def __post_init__(self) -> None:
        if type(self.connection) is not SemanticProviderConnection:
            _fail("connection_authority")
        if (
            type(self.model_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}", self.model_id) is None
            or ".." in self.model_id
            or self.model_id.casefold().endswith((":cloud", "-cloud", "/cloud", "_cloud"))
        ):
            _fail("model_choice_invalid")
        if (
            type(self.model_size_bytes) is not int
            or not 0 < self.model_size_bytes <= self.connection.max_model_bytes
        ):
            _fail("model_resource_limit")
        if (
            type(self.server_version) is not str
            or _SAFE_VERSION.fullmatch(self.server_version) is None
        ):
            _fail("model_version_invalid")
        if type(self.metadata) is not ModelMetadata or self.metadata.locality != "local":
            _fail("model_metadata_invalid")
        if not self.metadata.capabilities or "completion" not in self.metadata.capabilities:
            _fail("capability_mismatch")
        if (
            not self.metadata.model_digest
            or not self.metadata.family
            or not self.metadata.context_length
        ):
            _fail("model_metadata_missing")

    @property
    def profile_id(self) -> str:
        return self.connection.profile_id

    @property
    def endpoint(self) -> str:
        return self.connection.endpoint

    @property
    def adapter_version(self) -> str:
        return self.connection.adapter_version

    @property
    def model_digest(self) -> str:
        return cast(str, self.metadata.model_digest)

    @property
    def model_family(self) -> str:
        return cast(str, self.metadata.family)

    @property
    def context_length(self) -> int:
        return cast(int, self.metadata.context_length)

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @property
    def planning_profile(self) -> SemanticPlanningProfile:
        return SemanticPlanningProfile(
            self.profile_id,
            ModelBackendFamily.OLLAMA,
            self.model_id,
            self.model_digest,
            seed=0,
            raw_media_capability=False,
        )

    @property
    def model_manifest(self) -> ModelManifest:
        # IMPORTANT: native quantization/license are observations, not a bf16/Apache model default.
        return ModelManifest(
            manifest_id="h3.semantic.ollama.chosen",
            backend_family=ModelBackendFamily.OLLAMA,
            adapter_id="ollama.semantic",
            adapter_version=self.adapter_version,
            model_id=self.model_id,
            model_digest=self.model_digest,
            checkpoint_fingerprint=self.model_digest,
            detected_family=self.model_family,
            clip_type=None,
            tokenizer_processor="ollama",
            generation_weights_complete=True,
            capabilities=frozenset(
                {ModelCapability.TEXT_GENERATION, ModelCapability.STRUCTURED_OUTPUT}
            ),
            structured_output_schema="h3.semantic.proposal_json.v1",
            parser_path="h3.semantic.proposal_json.v1",
            runtime=ModelRuntimeProfile(
                device=LocalDeviceSpec(LocalDeviceKind.AUTO),
                dtype="auto",
                offload="ollama",
                max_context_tokens=self.context_length,
                max_output_tokens=2_048,
                max_media_items=1,
                limits=LocalResourceBudget(
                    max_memory_bytes=self.connection.max_model_bytes,
                    max_wall_time_seconds=120.0,
                    max_references=0,
                    max_output_bytes=262_144,
                    max_output_items=64,
                    max_concurrency=1,
                ),
            ),
            cancellation=ModelCapabilityState.QUALIFIED,
            license="native.license.unreported"
            if self.metadata.license_sha256 is None
            else "native.license." + self.metadata.license_sha256.removeprefix("sha256:"),
            notes=(
                "Exact local completion choice; native metadata is not inferred; "
                "raw media is prohibited."
            ),
            host_profile="ollama.local",
            evidence_level=EvidenceLevel.FRAMEWORK_REFERENCE,
            approved=True,
            server_version=self.server_version,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "connection": self.connection.to_wire(),
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "model_size_bytes": self.model_size_bytes,
            "server_version": self.server_version,
            "metadata": self.metadata.to_wire(),
        }


SemanticExecutionProfile = SemanticProviderProfile | SemanticChosenModelProfile


@dataclass(frozen=True, slots=True)
class SemanticSourceTarget:
    target_kind: str
    target_id: str
    claim: str


@dataclass(frozen=True, slots=True)
class SemanticSourceBundle:
    report: ContextReport
    wiring: NativeH3Wiring
    profile: SemanticExecutionProfile
    workspace: MultiSegmentWorkspace
    baseline_plan: Any
    planning_request: SemanticPlanningRequest
    reduction_items: tuple[SemanticSourceTarget, ...]


def _build_semantic_ollama_generation_request(
    source: object,
    *,
    typed_intents: bool = False,
) -> ModelGenerationRequest:
    """Add the M17-16-only provider hint without changing the generic planning API."""

    if type(source) is not SemanticSourceBundle:
        _fail("producer_authority")
    request = source.planning_request
    generic = build_semantic_generation_request(request)
    allowed_sources = sorted(request.allowed_source_ids)
    target_pairs = sorted(
        {
            (target.target_kind, target.target_id)
            for target in source.reduction_items
            if target.target_id not in request.protected_target_ids
        }
    )
    if (
        not target_pairs
        or len(target_pairs) > MAX_MODEL_JSON_KEYS
        or not allowed_sources
        or len(allowed_sources) > MAX_MODEL_JSON_KEYS
    ):
        _fail("provider_schema_capacity")

    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    }
    proposal_schema_id = (
        TYPED_SEMANTIC_PLANNING_SCHEMA if typed_intents else CONSTRAINED_SEMANTIC_PLANNING_SCHEMA
    )
    proposal_properties: dict[str, object] = {
        "schema": {"const": proposal_schema_id},
        "proposal_id": identifier,
        "target_kind": {"enum": sorted({kind for kind, _ in target_pairs})},
        "target_id": {"enum": sorted({target_id for _, target_id in target_pairs})},
        "claim": {
            "type": "string",
            "minLength": 1,
            "maxLength": request.budget.max_claim_chars,
        },
        "evidence_label": {"enum": ["source", "inferred"]},
        "source_ids": {
            "type": "array",
            "items": {"enum": allowed_sources},
            "minItems": 1,
            "maxItems": 64,
            "uniqueItems": True,
        },
        "confidence": {
            "type": "string",
            "pattern": r"^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$",
        },
        "rationale": {
            "anyOf": [
                {"type": "null"},
                {"type": "string", "minLength": 1, "maxLength": 1_024},
            ]
        },
    }
    proposal_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": list(proposal_properties),
        "properties": proposal_properties,
        "allOf": [
            {
                "anyOf": [
                    {
                        "properties": {
                            "target_kind": {"const": kind},
                            "target_id": {"const": target_id},
                        },
                        "required": ["target_kind", "target_id"],
                    }
                    for kind, target_id in target_pairs
                ]
            }
        ],
    }
    if typed_intents:
        dialogue_rows = semantic_dialogue_catalog(source.baseline_plan.hard_constraints.exact_texts)
        dialogue_items: dict[str, object] = (
            {"enum": [row.to_wire() for row in dialogue_rows]}
            if dialogue_rows
            else {"type": "object", "properties": {}, "additionalProperties": False}
        )
        del proposal_properties["claim"]
        proposal_properties["intent"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "description", "dialogue_bindings"],
            "properties": {
                "kind": {"enum": sorted({kind for kind, _ in target_pairs})},
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": request.budget.max_claim_chars,
                    "pattern": r"^[^<>\[\]\x00-\x1f\x7f-\x9f\u2028\u2029]+$",
                },
                "dialogue_bindings": {
                    "type": "array",
                    "items": dialogue_items,
                    "maxItems": len(dialogue_rows),
                },
            },
        }
        proposal_schema["required"] = list(proposal_properties)
    root_properties: dict[str, object] = {
        "schema": {"const": proposal_schema_id},
        "document_id": identifier,
        "policy": {"const": "evidence_bounded"},
        "task_mode": {"const": request.timeline_plan.task_mode.value},
        "effective_duration": {"const": request.timeline_plan.effective_duration.raw},
        "asset_ids": {"const": list(request.timeline_plan.reference_order)},
        "reference_order": {"const": list(request.timeline_plan.reference_order)},
        "timeline_fingerprint": {"const": request.timeline_plan.fingerprint},
        "reduction_fingerprint": {"const": request.reduction_plan.fingerprint},
        "preserved_exact_text": {
            "const": [item.to_wire() for item in request.protected_exact_text]
        },
        "proposals": {
            "type": "array",
            "items": proposal_schema,
            "minItems": 1,
            "maxItems": request.budget.max_proposals,
        },
        "complete": {"const": True},
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": list(root_properties),
        "properties": root_properties,
    }
    return replace(generic, structured_schema=schema)


@dataclass(frozen=True, slots=True)
class SemanticProposalActionUsage:
    generations: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.generations,
            self.request_bytes,
            self.response_bytes,
            self.prompt_tokens,
            self.completion_tokens,
        ):
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                _fail("semantic_usage_invalid")
        if self.generations > 2:
            _fail("semantic_generation_limit")

    def to_public_dict(self) -> dict[str, int]:
        return {
            "generations": self.generations,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass(frozen=True, slots=True)
class SemanticProposalProduct:
    planning_result: SemanticPlanningResult = field(repr=False)
    enrichment_result: SemanticEnrichmentResult = field(repr=False)
    transaction: SemanticProposalTransaction = field(repr=False)
    usage: SemanticProposalActionUsage = SemanticProposalActionUsage()


_ENRICHMENT_TARGETS = {
    SemanticTargetKind.SCENE: EnrichmentTargetKind.SCENE,
    SemanticTargetKind.SUBJECT: EnrichmentTargetKind.SUBJECT,
    SemanticTargetKind.ACTION: EnrichmentTargetKind.ACTION,
    SemanticTargetKind.CAMERA: EnrichmentTargetKind.CAMERA,
    SemanticTargetKind.STYLE: EnrichmentTargetKind.STYLE,
    SemanticTargetKind.AUDIO: EnrichmentTargetKind.AUDIO,
}


_TARGET_COLLECTIONS = (
    ("scene", "scenes", "scene_id"),
    ("subject", "subjects", "subject_id"),
    ("action", "actions", "action_id"),
    ("camera", "cameras", "camera_id"),
    ("style", "styles", "style_id"),
    ("audio", "audios", "audio_id"),
)


def _source_targets(report: ContextReport) -> tuple[SemanticSourceTarget, ...]:
    graph = report.plan.intent_graph
    targets: list[SemanticSourceTarget] = []
    for kind, collection_name, identifier_name in _TARGET_COLLECTIONS:
        for value in getattr(graph, collection_name):
            claim = getattr(value, "description", None)
            if type(claim) is not str or not claim:
                _fail("semantic_target_claim")
            targets.append(SemanticSourceTarget(kind, getattr(value, identifier_name), claim))
    if not targets:
        _fail("semantic_targets_missing")
    return tuple(targets)


def _stable_id(prefix: str, value: object) -> str:
    return prefix + canonical_fingerprint(value).split(":", 1)[1][:32]


def validate_semantic_source_authority(report: object, wiring: object) -> NativeH3Wiring:
    """Validate caller source before selected-model discovery is allowed any I/O."""
    if (
        type(report) is not ContextReport
        or not report.is_successful
        or not report.validation.is_valid
    ):
        _fail("report_authority")
    try:
        exact_wiring = assert_native_h3_wiring_authority(wiring, report)
    except Exception as exc:
        raise SemanticProposalProducerError("wiring_authority") from exc
    if exact_wiring.report_id != report.report_id:
        _fail("wiring_authority")
    _source_targets(report)
    frames = FrameGridPolicy().frame_count(report.plan.intent_graph.effective_duration.seconds)
    nearest = frames.to_integral_value(rounding=ROUND_HALF_UP)
    if (
        abs(frames - nearest) > Decimal("0.000000000001")
        or int(nearest) != report.request.effective_frame_count
    ):
        _fail("source_timeline_incompatible")
    return exact_wiring


def build_semantic_source_bundle(
    report: object,
    wiring: object,
    profile: object,
) -> SemanticSourceBundle:
    """Derive the exact context-scope workspace and evidence-bounded planning request."""

    exact_wiring = validate_semantic_source_authority(report, wiring)
    report = cast(ContextReport, report)
    if type(profile) not in {SemanticProviderProfile, SemanticChosenModelProfile}:
        _fail("profile_authority")
    profile = cast(SemanticExecutionProfile, profile)

    graph = report.plan.intent_graph
    targets = _source_targets(report)
    intent_fingerprint = canonical_fingerprint(graph.to_wire())
    segment_id = _stable_id(
        "context_",
        {
            "report_id": report.report_id,
            "report_revision": report.revision,
            "intent_graph_fingerprint": intent_fingerprint,
        },
    )
    registry_fingerprint = canonical_fingerprint(report.request.reference_registry.to_wire())
    native_fingerprint = canonical_fingerprint(exact_wiring.to_wire())
    settings_fingerprint = canonical_fingerprint(
        {
            "schema": "h3.semantic.producer_settings.v1",
            "policy": "evidence_bounded",
            "seed": 0,
            "raw_media": False,
        }
    )
    declaration = SegmentDeclaration(
        segment_id=segment_id,
        task_mode=report.request.task_mode,
        source_id=report.report_id,
        reference_ids=tuple(asset.asset_id for asset in report.request.reference_registry.assets),
        duration=SegmentDuration.from_frame_count(report.request.effective_frame_count),
        relation=SegmentRelationKind.INDEPENDENT,
        predecessor_segment_id=None,
        accepted_intent_fingerprint=intent_fingerprint,
        semantic_receipt_fingerprint=None,
        profile_fingerprint=canonical_fingerprint(profile.planning_profile.to_wire()),
        reference_registry_fingerprint=registry_fingerprint,
        native_binding_fingerprint=native_fingerprint,
        producer_settings_fingerprint=settings_fingerprint,
    )
    workspace = create_workspace(
        _stable_id(
            "workspace_",
            {
                "report_id": report.report_id,
                "report_revision": report.revision,
                "prompt_fingerprint": exact_wiring.prompt_fingerprint,
                "profile_fingerprint": profile.fingerprint,
            },
        ),
        (declaration,),
        accepted_intent_authorities=(AcceptedIntentAuthority(segment_id, intent_fingerprint),),
        selected_segment_ids=(segment_id,),
    )

    mode_report = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            task_mode=report.request.task_mode,
            reference_registry=report.request.reference_registry,
        )
    )
    # The whole-duration frame count is checked independently of the planner.  Internal IntentGraph
    # cuts remain exact semantic authority and are not copied into the separate feasible shot.
    frame_grid = FrameGridPolicy()
    duration_frames = frame_grid.frame_count(graph.effective_duration.seconds)
    nearest_duration_frame = duration_frames.to_integral_value(rounding=ROUND_HALF_UP)
    if (
        abs(duration_frames - nearest_duration_frame) > Decimal("0.000000000001")
        or int(nearest_duration_frame) != report.request.effective_frame_count
    ):
        _fail("source_timeline_incompatible")
    anchors = tuple(
        ReferenceAnchor(
            _stable_id(
                "anchor_",
                {"role": asset.role.value, "asset_id": asset.asset_id},
            ),
            asset.role.value,
            asset.asset_id,
            (
                TimePoint.from_text("0")
                if asset.role.value == "first_frame"
                else graph.effective_duration
            ),
        )
        for asset in report.request.reference_registry.assets
        if asset.role.value in {"first_frame", "last_frame"}
    )
    timeline_result = build_feasible_av_timeline(
        FeasibleAVTimelineRequest(
            mode_report=mode_report,
            effective_duration=graph.effective_duration,
            frame_grid=frame_grid,
            reference_registry=report.request.reference_registry,
            anchors=anchors,
            shots=(
                ShotDraft(
                    segment_id,
                    TimePoint.from_text("0"),
                    graph.effective_duration,
                ),
            ),
            reference_order=tuple(
                asset.asset_id for asset in report.request.reference_registry.assets
            ),
        )
    )
    if timeline_result.plan is None or timeline_result.status is not TimelinePlannerStatus.COMPLETE:
        _fail("source_timeline_incompatible")

    reduction_evidence = tuple(
        ReductionEvidence(
            item_id=target.target_id,
            level=ReductionLevel.SEGMENT,
            modality=(
                ReductionModality.AUDIO
                if target.target_kind == "audio"
                else ReductionModality.VISUAL
            ),
            claim=target.claim,
            source_ids=(segment_id,),
            token_estimate=max(1, min(4096, len(target.claim.split()))),
            salience=Decimal("1"),
        )
        for target in targets
    )
    evidence_graph = build_unified_evidence_graph()
    reduction_result = build_hierarchical_evidence_reduction(
        HierarchicalEvidenceReductionRequest(
            evidence_graph=evidence_graph,
            timeline_plan=timeline_result.plan,
            items=reduction_evidence,
            target=ReductionTarget(
                target_id="semantic_review",
                focus_source_ids=(segment_id,),
                required_item_ids=tuple(item.item_id for item in reduction_evidence),
            ),
            budget=ReductionBudget(
                max_output_items=64,
                max_output_tokens=65_536,
                min_source_coverage=Decimal("1"),
                min_target_coverage=Decimal("1"),
                min_fidelity=Decimal("1"),
            ),
        )
    )
    if reduction_result.plan is None or reduction_result.status is not ReductionStatus.COMPLETE:
        _fail("reduction_derivation")
    planning_request = SemanticPlanningRequest(
        reduction_plan=reduction_result.plan,
        timeline_plan=timeline_result.plan,
        profile=profile.planning_profile,
        policy=SemanticPlanningPolicy.EVIDENCE_BOUNDED,
        # CRITICAL: protected text must travel in the immutable proposal header. Leaving this
        # empty hides accepted dialogue from the parser and lets a changed header evade its guard.
        protected_exact_text=tuple(
            ExactTextSnapshot(item.constraint_id, item.text)
            for item in report.plan.hard_constraints.exact_texts
        ),
        budget=SemanticPlanningBudget(
            max_proposals=64,
            max_output_tokens=2_048,
            max_claim_chars=4_096,
            max_diff_fields=128,
        ),
        media_fingerprints=(),
    )
    return SemanticSourceBundle(
        report=report,
        wiring=exact_wiring,
        profile=profile,
        workspace=workspace,
        baseline_plan=report.plan,
        planning_request=planning_request,
        reduction_items=targets,
    )


def _build_enrichment_proposals(
    product_source: SemanticSourceBundle,
    planning_result: SemanticPlanningResult,
) -> tuple[SemanticProposal, ...]:
    document = planning_result.document
    if planning_result.status is not SemanticPlanningStatus.COMPLETE or document is None:
        _fail("semantic_planning_incomplete")
    values: list[SemanticProposal] = []
    for proposal in document.proposals:
        kind = _ENRICHMENT_TARGETS.get(cast(SemanticTargetKind, proposal.target_kind))
        if kind is None:
            _fail("semantic_target_protected")
        if proposal.intent is not None:
            try:
                validate_semantic_dialogue_bindings(
                    proposal.intent, product_source.baseline_plan.hard_constraints.exact_texts
                )
            except SemanticIntentError:
                _fail("semantic_dialogue_binding")
        source = EvidenceSource(
            EvidenceSourceKind.PROVIDER_OUTPUT,
            f"semantic.{proposal.proposal_id}",
        )
        record = EvidenceRecord(
            evidence_id=proposal.proposal_id,
            claim=proposal.claim,
            origin=EvidenceOrigin.ASSISTED_PROPOSAL,
            support=SupportStatus.SUPPORTED,
            provenance=Provenance(
                source=source,
                provider=ProviderIdentity.LOCAL,
                evidence_level=EvidenceLevel.FRAMEWORK_REFERENCE,
                provider_version=product_source.profile.adapter_version,
                source_revision=product_source.profile.model_digest,
            ),
            confidence=proposal.confidence,
        )
        values.append(
            SemanticProposal(
                proposal_id=proposal.proposal_id,
                target_kind=kind,
                target_id=proposal.target_id,
                text=proposal.claim,
                evidence=record,
            )
        )
    if not values:
        _fail("semantic_proposals_missing")
    return tuple(values)


def _assemble_semantic_proposal_product(
    source: object,
    model_result: object,
    capability_fingerprint: object,
    *,
    typed_intents: bool = False,
) -> SemanticProposalProduct:
    """Purely join one qualified model result to enrichment and the accepted M17-05 contract."""

    if type(source) is not SemanticSourceBundle or type(model_result) is not ModelGenerationResult:
        _fail("provider_execution_authority")
    if (
        type(capability_fingerprint) is not str
        or _MODEL_DIGEST.fullmatch(capability_fingerprint) is None
    ):
        _fail("provider_execution_authority")
    if (
        model_result.model_id != source.profile.model_id
        or model_result.model_digest != source.profile.model_digest
        or model_result.backend_family is not ModelBackendFamily.OLLAMA
    ):
        _fail("provider_execution_mismatch")
    planning_result = parse_semantic_proposal(
        source.planning_request,
        model_result.text,
        model_result=model_result,
        accepted_exact_text=source.baseline_plan.hard_constraints.exact_texts,
    )
    if planning_result.status is not SemanticPlanningStatus.COMPLETE:
        _fail("provider_output_invalid")
    if typed_intents and (
        planning_result.document is None
        or planning_result.document.schema != TYPED_SEMANTIC_PLANNING_SCHEMA
    ):
        _fail("provider_output_invalid")
    proposals = _build_enrichment_proposals(source, planning_result)
    enrichment = apply_semantic_enrichment(source.baseline_plan, proposals)
    if (
        enrichment.status is not SemanticEnrichmentStatus.APPLIED
        or not enrichment.is_complete
        or enrichment.after_plan is None
        or enrichment.conflicts
    ):
        _fail("semantic_enrichment_rejected")
    before_graph = canonical_fingerprint(source.baseline_plan.intent_graph.to_wire())
    after_graph = canonical_fingerprint(enrichment.after_plan.intent_graph.to_wire())
    if before_graph == after_graph:
        _fail("semantic_enrichment_noop")
    authorization = SemanticProposalAuthorization(
        capability_fingerprint=capability_fingerprint,
        profile_fingerprint=canonical_fingerprint(source.planning_request.profile.to_wire()),
        raw_media_allowed=False,
        consent_fingerprint=None,
    )
    transaction_id = _stable_id(
        "semantic_tx_",
        {
            "workspace": source.workspace.fingerprint,
            "planning_result": canonical_fingerprint(planning_result.to_wire()),
            "authorization": authorization.fingerprint,
        },
    )
    try:
        transaction = prepare_semantic_proposal_transaction(
            workspace=source.workspace,
            segment_id=source.workspace.segments[0].segment_id,
            baseline_plan=source.baseline_plan,
            planning_request=source.planning_request,
            planning_result=planning_result,
            enrichment_result=enrichment,
            authorization=authorization,
            transaction_id=transaction_id,
        )
    except (TypeError, ValueError) as exc:
        raise SemanticProposalProducerError("semantic_transaction_rejected") from exc
    if (
        transaction.attempt != 1
        or transaction.revision != 1
        or transaction.state is not SemanticProposalTransactionState.READY_FOR_REVIEW
    ):
        _fail("semantic_transaction_state")
    return SemanticProposalProduct(planning_result, enrichment, transaction)


_AUTHORITY_SCHEMA = "h3.semantic.proposal_review_authority.v1"
_MAX_LINEAGES = 64
_MAX_OWNER_BYTES = 33_554_432
_MAX_LINEAGE_BYTES = 524_288
_OWNER_OVERHEAD_BYTES = 1_024
_UNCLAIMED_TTL_SECONDS = 300.0
_CORRELATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}")


class SemanticProposalReviewAuthority:
    """Content-free handle whose authority is exact process-local object identity."""

    __slots__ = ("_lineage_id", "_expires_at_ms", "_fingerprint")
    _lineage_id: str
    _expires_at_ms: int
    _fingerprint: str

    def __new__(cls, *_: object, **__: object) -> SemanticProposalReviewAuthority:
        raise TypeError("semantic proposal review authorities are factory-only")

    def __setattr__(self, _: str, __: object) -> NoReturn:
        raise AttributeError("semantic proposal review authority is immutable")

    @property
    def lineage_id(self) -> str:
        return self._lineage_id

    @property
    def expires_at_ms(self) -> int:
        return self._expires_at_ms

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": _AUTHORITY_SCHEMA,
            "lineage_id": self.lineage_id,
            "expires_at_ms": self.expires_at_ms,
            "authority_fingerprint": self.fingerprint,
        }

    def __repr__(self) -> str:
        return "SemanticProposalReviewAuthority(<content-free>)"


class _SemanticProposalReservation:
    __slots__ = ("lineage_id",)
    lineage_id: str

    def __new__(cls, *_: object, **__: object) -> _SemanticProposalReservation:
        raise TypeError("semantic proposal reservations are factory-only")

    def __repr__(self) -> str:
        return "_SemanticProposalReservation(<opaque>)"


@dataclass(slots=True)
class _AuthorityEntry:
    lineage_id: str
    identity_fingerprint: str
    correlation: tuple[str, str]
    state: str
    reservation: _SemanticProposalReservation | None = None
    handle: SemanticProposalReviewAuthority | None = None
    source: SemanticSourceBundle | None = field(default=None, repr=False)
    product: SemanticProposalProduct | None = field(default=None, repr=False)
    reserved_bytes: int = 0
    expires_at: float | None = None
    active_claim: object | None = None
    active_bundle: SemanticProposalReviewBundle | None = None
    content_fingerprint: str | None = None
    handle_expires_at_ms: int | None = None
    handle_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticProposalReviewBundle:
    report: ContextReport = field(repr=False)
    workspace: MultiSegmentWorkspace = field(repr=False)
    transaction: SemanticProposalTransaction = field(repr=False)


class SemanticProposalReviewClaim:
    __slots__ = ("_owner", "_lineage_id", "_identity", "_bundle", "_finished")

    def __init__(
        self,
        owner: _SemanticProposalAuthorityOwner,
        lineage_id: str,
        identity: object,
        bundle: SemanticProposalReviewBundle,
    ) -> None:
        self._owner = owner
        self._lineage_id = lineage_id
        self._identity = identity
        self._bundle = bundle
        self._finished = False

    @property
    def bundle(self) -> SemanticProposalReviewBundle:
        return self._bundle

    def commit(self) -> None:
        if self._finished:
            _fail("review_claim_inactive")
        try:
            self._owner._finish_claim(
                self._lineage_id,
                self._identity,
                self._bundle,
                commit=True,
            )
        finally:
            self._finished = True

    def abort(self) -> None:
        if self._finished:
            _fail("review_claim_inactive")
        try:
            self._owner._finish_claim(
                self._lineage_id,
                self._identity,
                self._bundle,
                commit=False,
            )
        finally:
            self._finished = True

    def __repr__(self) -> str:
        return "SemanticProposalReviewClaim(<private-bundle>)"


class _SemanticProposalAuthorityOwner:
    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, _AuthorityEntry] = {}
        self._correlations: dict[tuple[str, str], str] = {}

    @staticmethod
    def _wire_bytes(wire: object) -> bytes:
        try:
            return json.dumps(
                wire,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise SemanticProposalProducerError("authority_accounting") from exc

    def _retained_state_wires(
        self,
        entry: _AuthorityEntry,
        product: SemanticProposalProduct | None = None,
    ) -> tuple[tuple[str, object], ...]:
        source = entry.source
        retained_product = entry.product if product is None else product
        if source is None or retained_product is None:
            _fail("review_authority_content")
        return (
            (
                "report",
                {
                    "wire": source.report.to_wire(),
                    "revision": source.report.revision,
                },
            ),
            ("wiring", source.wiring.to_wire()),
            ("provider_profile", source.profile.to_wire()),
            ("workspace", source.workspace.to_wire()),
            ("baseline_plan", source.baseline_plan.to_wire()),
            ("planning_request", source.planning_request.to_wire()),
            (
                "reduction_items",
                [
                    {
                        "target_kind": item.target_kind,
                        "target_id": item.target_id,
                        "claim": item.claim,
                    }
                    for item in source.reduction_items
                ],
            ),
            ("planning_result", retained_product.planning_result.to_wire()),
            ("enrichment_result", retained_product.enrichment_result.to_wire()),
            ("transaction", retained_product.transaction.to_public_dict()),
            ("candidate_graph", retained_product.transaction.candidate_graph.to_wire()),
            ("action_usage", retained_product.usage.to_public_dict()),
        )

    def _retained_state_fingerprint(
        self,
        entry: _AuthorityEntry,
        product: SemanticProposalProduct | None = None,
    ) -> str:
        wire_digests = {
            name: "sha256:" + hashlib.sha256(self._wire_bytes(wire)).hexdigest()
            for name, wire in self._retained_state_wires(entry, product)
        }
        return canonical_fingerprint(
            {
                "schema": "h3.semantic.retained_authority.v1",
                "wire_digests": wire_digests,
            }
        )

    def _invalidate(self, entry: _AuthorityEntry) -> None:
        entry.state = "invalid"
        entry.reservation = None
        entry.handle = None
        entry.source = None
        entry.product = None
        entry.reserved_bytes = 0
        entry.expires_at = None
        entry.active_claim = None
        entry.active_bundle = None
        entry.content_fingerprint = None
        entry.handle_expires_at_ms = None
        entry.handle_fingerprint = None

    def _assert_current_content(
        self,
        entry: _AuthorityEntry,
        bundle: SemanticProposalReviewBundle | None = None,
    ) -> None:
        try:
            current = self._retained_state_fingerprint(entry)
            exact_bundle = bundle is None or (
                bundle is entry.active_bundle
                and entry.source is not None
                and entry.product is not None
                and bundle.report is entry.source.report
                and bundle.workspace is entry.source.workspace
                and bundle.transaction is entry.product.transaction
            )
        # CRITICAL: any failure to reserialize current private state invalidates the authority.
        except Exception:
            self._invalidate(entry)
            raise SemanticProposalProducerError("review_authority_content") from None
        if entry.content_fingerprint != current or not exact_bundle:
            self._invalidate(entry)
            _fail("review_authority_content")

    def _expire(self, now: float) -> None:
        for entry in self._entries.values():
            if (
                entry.state == "unclaimed"
                and entry.expires_at is not None
                and now >= entry.expires_at
            ):
                entry.state = "expired"
                entry.source = None
                entry.product = None
                entry.handle = None
                entry.reserved_bytes = 0
                entry.content_fingerprint = None
                entry.active_bundle = None
                entry.handle_expires_at_ms = None
                entry.handle_fingerprint = None

    def _identity(
        self,
        source: SemanticSourceBundle,
        setup: ProviderSetup,
        prompt_id: str,
        execution_node_id: str,
    ) -> str:
        return canonical_fingerprint(
            {
                "schema": "h3.semantic.producer_lineage.v1",
                "prompt_id": prompt_id,
                "execution_node_id": execution_node_id,
                "report_id": source.report.report_id,
                "report_revision": source.report.revision,
                "workspace_fingerprint": source.workspace.fingerprint,
                "wiring_prompt_fingerprint": source.wiring.prompt_fingerprint,
                "setup_fingerprint": setup.setup_fingerprint,
                "endpoint_fingerprint": canonical_fingerprint(
                    {
                        "scheme": "http",
                        "host": "127.0.0.1",
                        "port": 11434,
                        "path": "/api",
                    }
                ),
                "profile_fingerprint": source.profile.fingerprint,
            }
        )

    def begin(
        self,
        source: object,
        setup: object,
        prompt_id: object,
        execution_node_id: object,
    ) -> SemanticProposalReviewAuthority | _SemanticProposalReservation:
        if type(source) is not SemanticSourceBundle or type(setup) is not ProviderSetup:
            _fail("producer_authority")
        if (
            type(prompt_id) is not str
            or _CORRELATION.fullmatch(prompt_id) is None
            or type(execution_node_id) is not str
            or _CORRELATION.fullmatch(execution_node_id) is None
        ):
            _fail("correlation_invalid")
        now = float(self._clock())
        correlation = (prompt_id, execution_node_id)
        identity = self._identity(source, setup, prompt_id, execution_node_id)
        lineage_id = "semantic_lineage_" + identity.split(":", 1)[1][:32]
        with self._lock:
            self._expire(now)
            previous_id = self._correlations.get(correlation)
            if previous_id is not None:
                previous = self._entries[previous_id]
                if previous.identity_fingerprint != identity:
                    _fail("identity_conflict")
                if previous.state == "unclaimed" and previous.handle is not None:
                    self._assert_handle(previous.handle, previous)
                    self._assert_current_content(previous)
                    return previous.handle
                if previous.state == "active":
                    _fail("transaction_in_progress")
                _fail(f"lineage_{previous.state}")
            if len(self._entries) >= _MAX_LINEAGES:
                _fail("lineage_capacity")
            reserved_total = sum(entry.reserved_bytes for entry in self._entries.values())
            if reserved_total + _MAX_LINEAGE_BYTES > _MAX_OWNER_BYTES:
                _fail("owner_byte_capacity")
            reservation = object.__new__(_SemanticProposalReservation)
            object.__setattr__(reservation, "lineage_id", lineage_id)
            entry = _AuthorityEntry(
                lineage_id=lineage_id,
                identity_fingerprint=identity,
                correlation=correlation,
                state="active",
                reservation=reservation,
                source=source,
                reserved_bytes=_MAX_LINEAGE_BYTES,
            )
            self._entries[lineage_id] = entry
            self._correlations[correlation] = lineage_id
            return reservation

    def fail(self, reservation: object) -> None:
        with self._lock:
            entry = self._reservation_entry(reservation)
            entry.state = "failed"
            entry.source = None
            entry.product = None
            entry.reservation = None
            entry.reserved_bytes = 0
            entry.content_fingerprint = None
            entry.expires_at = None
            entry.active_claim = None
            entry.active_bundle = None
            entry.handle_expires_at_ms = None
            entry.handle_fingerprint = None

    def _encoded_size(self, entry: _AuthorityEntry, product: SemanticProposalProduct) -> int:
        return _OWNER_OVERHEAD_BYTES + sum(
            len(self._wire_bytes(wire)) for _, wire in self._retained_state_wires(entry, product)
        )

    def commit(
        self,
        reservation: object,
        product: object,
    ) -> SemanticProposalReviewAuthority:
        if type(product) is not SemanticProposalProduct:
            _fail("proposal_product")
        now = float(self._clock())
        with self._lock:
            entry = self._reservation_entry(reservation)
            usage = self._encoded_size(entry, product)
            content_fingerprint = self._retained_state_fingerprint(entry, product)
            if usage > _MAX_LINEAGE_BYTES:
                self.fail(reservation)
                _fail("authority_byte_limit")
            expires_at = now + _UNCLAIMED_TTL_SECONDS
            wire = {
                "schema": _AUTHORITY_SCHEMA,
                "lineage_id": entry.lineage_id,
                "expires_at_ms": int(expires_at * 1000),
            }
            handle = object.__new__(SemanticProposalReviewAuthority)
            object.__setattr__(handle, "_lineage_id", entry.lineage_id)
            object.__setattr__(handle, "_expires_at_ms", wire["expires_at_ms"])
            object.__setattr__(handle, "_fingerprint", canonical_fingerprint(wire))
            entry.state = "unclaimed"
            entry.product = product
            entry.reservation = None
            entry.handle = handle
            entry.reserved_bytes = usage
            entry.expires_at = expires_at
            entry.content_fingerprint = content_fingerprint
            entry.handle_expires_at_ms = handle.expires_at_ms
            entry.handle_fingerprint = handle.fingerprint
            return handle

    def _reservation_entry(self, reservation: object) -> _AuthorityEntry:
        if type(reservation) is not _SemanticProposalReservation:
            _fail("reservation_authority")
        lineage_id = reservation.lineage_id
        entry = self._entries.get(lineage_id)
        if entry is None or entry.state != "active" or entry.reservation is not reservation:
            _fail("reservation_inactive")
        return entry

    def _assert_handle(
        self,
        handle: SemanticProposalReviewAuthority,
        entry: _AuthorityEntry,
    ) -> None:
        if entry.handle is not handle:
            _fail("review_authority")
        try:
            wire = {
                "schema": _AUTHORITY_SCHEMA,
                "lineage_id": handle.lineage_id,
                "expires_at_ms": handle.expires_at_ms,
            }
            exact = (
                handle.lineage_id == entry.lineage_id
                and handle.expires_at_ms == entry.handle_expires_at_ms
                and handle.fingerprint == entry.handle_fingerprint
                and handle.fingerprint == canonical_fingerprint(wire)
            )
        except Exception:
            exact = False
        if not exact:
            self._invalidate(entry)
            _fail("review_authority")

    @contextmanager
    def claim(self, handle: object) -> Iterator[SemanticProposalReviewClaim]:
        if type(handle) is not SemanticProposalReviewAuthority:
            _fail("review_authority")
        now = float(self._clock())
        with self._lock:
            self._expire(now)
            try:
                entry = self._entries.get(handle.lineage_id)
            except Exception:
                entry = None
            if entry is None:
                entry = next(
                    (
                        candidate
                        for candidate in self._entries.values()
                        if candidate.handle is handle
                    ),
                    None,
                )
            if entry is None or entry.state != "unclaimed":
                _fail("review_authority_inactive")
            self._assert_handle(handle, entry)
            if entry.source is None or entry.product is None:
                self._invalidate(entry)
                _fail("review_authority_content")
            self._assert_current_content(entry)
            identity = object()
            bundle = SemanticProposalReviewBundle(
                entry.source.report,
                entry.source.workspace,
                entry.product.transaction,
            )
            entry.state = "claimed"
            entry.active_claim = identity
            entry.active_bundle = bundle
            claim = SemanticProposalReviewClaim(
                self,
                entry.lineage_id,
                identity,
                bundle,
            )
        try:
            yield claim
        finally:
            if not claim._finished:
                claim.abort()

    def _finish_claim(
        self,
        lineage_id: str,
        identity: object,
        bundle: SemanticProposalReviewBundle,
        *,
        commit: bool,
    ) -> None:
        with self._lock:
            entry = self._entries.get(lineage_id)
            if entry is None or entry.state != "claimed" or entry.active_claim is not identity:
                _fail("review_claim_inactive")
            self._assert_current_content(entry, bundle)
            entry.active_claim = None
            entry.active_bundle = None
            if commit:
                entry.state = "consumed"
                entry.source = None
                entry.product = None
                entry.handle = None
                entry.reserved_bytes = 0
                entry.expires_at = None
                entry.content_fingerprint = None
                entry.handle_expires_at_ms = None
                entry.handle_fingerprint = None
            else:
                entry.state = "unclaimed"


_PROCESS_AUTHORITY_OWNER = _SemanticProposalAuthorityOwner()


def _begin_semantic_proposal_authority(
    source: object,
    setup: object,
    prompt_id: object,
    execution_node_id: object,
) -> SemanticProposalReviewAuthority | _SemanticProposalReservation:
    return _PROCESS_AUTHORITY_OWNER.begin(source, setup, prompt_id, execution_node_id)


def _commit_semantic_proposal_authority(
    reservation: object,
    product: object,
) -> SemanticProposalReviewAuthority:
    return _PROCESS_AUTHORITY_OWNER.commit(reservation, product)


def _fail_semantic_proposal_authority(reservation: object) -> None:
    _PROCESS_AUTHORITY_OWNER.fail(reservation)


def claim_semantic_proposal_review_authority(
    authority: object,
) -> Any:
    """Return the one-time process-local claim context consumed later by M17-06."""

    return _PROCESS_AUTHORITY_OWNER.claim(authority)


__all__ = [
    "FIXED_OLLAMA_ENDPOINT",
    "OllamaNativeDigest",
    "SemanticProposalProducerError",
    "SemanticProposalProduct",
    "SemanticProposalReviewAuthority",
    "SemanticProposalReviewBundle",
    "SemanticProposalReviewClaim",
    "SemanticProviderCatalog",
    "SemanticProviderProfile",
    "SemanticSourceBundle",
    "SemanticSourceTarget",
    "build_semantic_source_bundle",
    "claim_semantic_proposal_review_authority",
    "load_semantic_provider_catalog",
    "ollama_native_digest_to_model_digest",
    "parse_ollama_native_digest",
]
