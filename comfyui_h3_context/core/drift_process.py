"""Strict offline source-drift classification and smallest-route projection.

This module composes the immutable source checkpoint.  It never fetches a source, executes a
regression, promotes a baseline, contacts a host/provider, or publishes a report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .source_drift_checkpoint import build_default_source_drift_checkpoint

DRIFT_EVENT_SCHEMA = "h3.drift.event.v1"
DRIFT_PROCESS_REPORT_SCHEMA = "h3.drift.process.report.v1"
MAX_DRIFT_EVENT_BYTES = 65_536
MAX_DRIFT_REPORT_BYTES = 131_072
MAX_DRIFT_SOURCES = 40
MAX_DRIFT_ROUTES = 64
MAX_DRIFT_ITEMS = 64
MAX_DRIFT_DEPTH = 8
MAX_DRIFT_STRING_LENGTH = 512

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}\Z")
_CHECK_ID = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}\Z")
_IDENTITY = re.compile(r"(?:git|gitblob):[0-9a-f]{40}|sha256:[0-9a-f]{64}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DATE = re.compile(r"20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])\Z")


class DriftEventError(ValueError):
    """Raised when an event or report is open, unsafe, inconsistent, or unbounded."""


class DriftKind(str, Enum):
    NO_DRIFT = "NO_DRIFT"
    UNCHANGED_SEAM_AT_NEW_REPO_REVISION = "UNCHANGED_SEAM_AT_NEW_REPO_REVISION"
    ADDITIVE_NON_SUBSTITUTING = "ADDITIVE_NON_SUBSTITUTING"
    PRESENTATION_OR_BUILD_DRIFT = "PRESENTATION_OR_BUILD_DRIFT"
    COMPATIBLE_CONTRACT_DRIFT = "COMPATIBLE_CONTRACT_DRIFT"
    ROUTE_BLOCKING_CONTRACT_DRIFT = "ROUTE_BLOCKING_CONTRACT_DRIFT"
    SECURITY_OR_LICENSE_BLOCKER = "SECURITY_OR_LICENSE_BLOCKER"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    UNRESOLVED = "UNRESOLVED"


class ChangeSignal(str, Enum):
    NONE = "NONE"
    ADDITIVE = "ADDITIVE"
    PRESENTATION_BUILD = "PRESENTATION_BUILD"
    COMPATIBLE_CONTRACT = "COMPATIBLE_CONTRACT"
    CONTRACT_BREAKING = "CONTRACT_BREAKING"
    SECURITY_LICENSE = "SECURITY_LICENSE"
    INTEGRITY_MISMATCH = "INTEGRITY_MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"
    UNRESOLVED = "UNRESOLVED"


class SourceAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class PublicationState(str, Enum):
    DO_NOT_PUBLISH = "DO_NOT_PUBLISH"
    APPROVED_UNPUBLISHED = "APPROVED_UNPUBLISHED"
    PUBLISHED_EXACT = "PUBLISHED_EXACT"


class PublicationMonitoringState(str, Enum):
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    INACTIVE_NO_PUBLISHED_HASH = "INACTIVE_NO_PUBLISHED_HASH"
    ACTIVE_EXACT_HASH = "ACTIVE_EXACT_HASH"


class DriftMateriality(str, Enum):
    NONE = "NONE"
    NON_BLOCKING = "NON_BLOCKING"
    BLOCKING = "BLOCKING"


class DriftProcessDisposition(str, Enum):
    PASS = "PASS"  # noqa: S105 - process disposition, never a credential
    RUN_AFFECTED_CHECKS = "RUN_AFFECTED_CHECKS"
    BLOCK_OWNED_ROUTE = "BLOCK_OWNED_ROUTE"
    BLOCK_PUBLICATION = "BLOCK_PUBLICATION"


class ReviewState(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUIRED = "REQUIRED"


class ClosureState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"


class ClaimCeiling(str, Enum):
    EXISTING_CLAIMS_ONLY = "EXISTING_CLAIMS_ONLY"
    OWNED_ROUTE_ONLY = "OWNED_ROUTE_ONLY"
    NO_PUBLICATION = "NO_PUBLICATION"


class TaskModeDimension(str, Enum):
    NONE = "NONE"
    MANUAL = "MANUAL"
    ASSISTED = "ASSISTED"


class ProductSurfaceDimension(str, Enum):
    PROMPT_CONTRACT = "PROMPT_CONTRACT"
    MEDIA_CONTRACT = "MEDIA_CONTRACT"
    NATIVE_HOST = "NATIVE_HOST"
    SIDEBAR = "SIDEBAR"
    SUBGRAPH = "SUBGRAPH"
    CONTAINER = "CONTAINER"
    PROVIDER = "PROVIDER"
    SUPPLY_CHAIN = "SUPPLY_CHAIN"
    EVALUATION = "EVALUATION"


class HostProfileDimension(str, Enum):
    NONE = "NONE"
    SUPPORTED = "SUPPORTED"
    LATEST = "LATEST"


class ProviderProfileDimension(str, Enum):
    NONE = "NONE"
    OLLAMA_OPTIONAL = "OLLAMA_OPTIONAL"


class EvaluationLayerDimension(str, Enum):
    NONE = "NONE"
    STRUCTURAL = "STRUCTURAL"
    COMPATIBILITY = "COMPATIBILITY"
    RELEASE = "RELEASE"


class SemanticSeam(str, Enum):
    PROMPT_MEDIA_TYPING = "prompt_media_typing"
    NATIVE_INPUTS_ENUMS = "native_inputs_enums"
    V3_DYNAMIC_PATHS = "v3_dynamic_paths"
    SUBGRAPH_INPUTS = "subgraph_inputs"
    SIGMA_SHIFT = "sigma_shift"
    OPEN_BOX_SIDEBAR_STATE = "open_box_sidebar_state"
    NATIVE_MANUAL_REVERSIBILITY = "native_manual_reversibility"
    CLASSIFIED_ERRORS = "classified_errors"
    CONTAINER_GEOMETRY = "container_geometry"
    FALLBACK_ABSENCE = "fallback_absence"
    DEPENDENCY_LICENSE_ADVISORY = "dependency_license_advisory"
    ARTIFACT_SBOM_HASH = "artifact_sbom_hash"


class OwnerRole(str, Enum):
    CORE_MAINTAINER = "CORE_MAINTAINER"
    HOST_INTEGRATION_REVIEWER = "HOST_INTEGRATION_REVIEWER"
    PROVIDER_REVIEWER = "PROVIDER_REVIEWER"
    RELEASE_REVIEWER = "RELEASE_REVIEWER"
    EVALUATION_REVIEWER = "EVALUATION_REVIEWER"


class DriftCadence(str, Enum):
    ON_SOURCE_CHANGE = "ON_SOURCE_CHANGE"
    PRE_RELEASE = "PRE_RELEASE"
    QUARTERLY = "QUARTERLY"


class UnavailableDisposition(str, Enum):
    BLOCK_OWNED_ROUTE = "BLOCK_OWNED_ROUTE"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise DriftEventError(f"{field} is not a bounded identifier")
    return value


def _identity(value: object, field: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str or _IDENTITY.fullmatch(value) is None:
        raise DriftEventError(f"{field} is not a public identity")
    return value


def _fingerprint(value: object, field: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise DriftEventError(f"{field} is not a semantic fingerprint")
    return value


def _closed_enum(enum_type: type[Enum], value: object, field: str) -> Enum:
    if type(value) is not str:
        raise DriftEventError(f"{field} must be a closed string enum")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise DriftEventError(f"{field} is not a supported enum value") from exc


def _unique_tuple(values: tuple[str, ...], field: str, maximum: int = MAX_DRIFT_ITEMS) -> None:
    if type(values) is not tuple or not values or len(values) > maximum:
        raise DriftEventError(f"{field} is not a bounded non-empty tuple")
    if len(values) != len(set(values)):
        raise DriftEventError(f"{field} contains duplicates")
    for value in values:
        if type(value) is not str or _CHECK_ID.fullmatch(value) is None:
            raise DriftEventError(f"{field} contains an invalid identifier")


@dataclass(frozen=True, slots=True)
class DriftResourceBudget:
    max_observation_bytes: int = MAX_DRIFT_EVENT_BYTES
    max_routes: int = MAX_DRIFT_ROUTES

    def __post_init__(self) -> None:
        if type(self.max_observation_bytes) is not int or self.max_observation_bytes != 65_536:
            raise DriftEventError("resource budget bytes must remain frozen")
        if type(self.max_routes) is not int or self.max_routes != 64:
            raise DriftEventError("resource budget routes must remain frozen")


@dataclass(frozen=True, slots=True)
class DriftRoute:
    route_id: str
    task_mode: TaskModeDimension
    product_surface: ProductSurfaceDimension
    host_profile: HostProfileDimension
    provider_profile: ProviderProfileDimension
    evaluation_layer: EvaluationLayerDimension

    def __post_init__(self) -> None:
        _identifier(self.route_id, "route_id")
        for value, enum_type, field in (
            (self.task_mode, TaskModeDimension, "task_mode"),
            (self.product_surface, ProductSurfaceDimension, "product_surface"),
            (self.host_profile, HostProfileDimension, "host_profile"),
            (self.provider_profile, ProviderProfileDimension, "provider_profile"),
            (self.evaluation_layer, EvaluationLayerDimension, "evaluation_layer"),
        ):
            if type(value) is not enum_type:
                raise DriftEventError(f"{field} must be an exact enum")

    def to_wire(self) -> dict[str, str]:
        return {
            "route_id": self.route_id,
            "task_mode": self.task_mode.value,
            "product_surface": self.product_surface.value,
            "host_profile": self.host_profile.value,
            "provider_profile": self.provider_profile.value,
            "evaluation_layer": self.evaluation_layer.value,
        }


_ROUTES = {
    "manual.prompt.structural": DriftRoute(
        "manual.prompt.structural",
        TaskModeDimension.MANUAL,
        ProductSurfaceDimension.PROMPT_CONTRACT,
        HostProfileDimension.NONE,
        ProviderProfileDimension.NONE,
        EvaluationLayerDimension.STRUCTURAL,
    ),
    "manual.media.structural": DriftRoute(
        "manual.media.structural",
        TaskModeDimension.MANUAL,
        ProductSurfaceDimension.MEDIA_CONTRACT,
        HostProfileDimension.NONE,
        ProviderProfileDimension.NONE,
        EvaluationLayerDimension.STRUCTURAL,
    ),
    "supported.native.compatibility": DriftRoute(
        "supported.native.compatibility",
        TaskModeDimension.MANUAL,
        ProductSurfaceDimension.NATIVE_HOST,
        HostProfileDimension.SUPPORTED,
        ProviderProfileDimension.NONE,
        EvaluationLayerDimension.COMPATIBILITY,
    ),
    "latest.native.compatibility": DriftRoute(
        "latest.native.compatibility",
        TaskModeDimension.MANUAL,
        ProductSurfaceDimension.NATIVE_HOST,
        HostProfileDimension.LATEST,
        ProviderProfileDimension.NONE,
        EvaluationLayerDimension.COMPATIBILITY,
    ),
    "latest.sidebar.compatibility": DriftRoute(
        "latest.sidebar.compatibility",
        TaskModeDimension.MANUAL,
        ProductSurfaceDimension.SIDEBAR,
        HostProfileDimension.LATEST,
        ProviderProfileDimension.NONE,
        EvaluationLayerDimension.COMPATIBILITY,
    ),
    "latest.subgraph.compatibility": DriftRoute(
        "latest.subgraph.compatibility",
        TaskModeDimension.MANUAL,
        ProductSurfaceDimension.SUBGRAPH,
        HostProfileDimension.LATEST,
        ProviderProfileDimension.NONE,
        EvaluationLayerDimension.COMPATIBILITY,
    ),
    "assisted.container.compatibility": DriftRoute(
        "assisted.container.compatibility",
        TaskModeDimension.ASSISTED,
        ProductSurfaceDimension.CONTAINER,
        HostProfileDimension.NONE,
        ProviderProfileDimension.NONE,
        EvaluationLayerDimension.COMPATIBILITY,
    ),
    "assisted.ollama.compatibility": DriftRoute(
        "assisted.ollama.compatibility",
        TaskModeDimension.ASSISTED,
        ProductSurfaceDimension.PROVIDER,
        HostProfileDimension.NONE,
        ProviderProfileDimension.OLLAMA_OPTIONAL,
        EvaluationLayerDimension.COMPATIBILITY,
    ),
    "release.supply.integrity": DriftRoute(
        "release.supply.integrity",
        TaskModeDimension.NONE,
        ProductSurfaceDimension.SUPPLY_CHAIN,
        HostProfileDimension.NONE,
        ProviderProfileDimension.NONE,
        EvaluationLayerDimension.RELEASE,
    ),
    "evaluation.structural": DriftRoute(
        "evaluation.structural",
        TaskModeDimension.NONE,
        ProductSurfaceDimension.EVALUATION,
        HostProfileDimension.NONE,
        ProviderProfileDimension.NONE,
        EvaluationLayerDimension.STRUCTURAL,
    ),
}


@dataclass(frozen=True, slots=True)
class DriftSourceDefinition:
    source_id: str
    baseline_identity: str
    semantic_extractor_id: str
    owner_role: OwnerRole
    cadence: DriftCadence
    resource_budget: DriftResourceBudget
    unavailable_disposition: UnavailableDisposition
    semantic_seam: SemanticSeam
    routes: tuple[DriftRoute, ...]
    required_check_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _identity(self.baseline_identity, "baseline_identity")
        _identifier(self.semantic_extractor_id, "semantic_extractor_id")
        if type(self.owner_role) is not OwnerRole:
            raise DriftEventError("owner_role must be an exact enum")
        if type(self.cadence) is not DriftCadence:
            raise DriftEventError("cadence must be an exact enum")
        if type(self.resource_budget) is not DriftResourceBudget:
            raise DriftEventError("resource_budget must be exact")
        if type(self.unavailable_disposition) is not UnavailableDisposition:
            raise DriftEventError("unavailable_disposition must be exact")
        if type(self.semantic_seam) is not SemanticSeam:
            raise DriftEventError("semantic_seam must be an exact enum")
        if type(self.routes) is not tuple or not self.routes or len(self.routes) > MAX_DRIFT_ROUTES:
            raise DriftEventError("routes must be a bounded non-empty tuple")
        if not all(type(route) is DriftRoute for route in self.routes):
            raise DriftEventError("routes contain a non-exact route")
        if len({route.route_id for route in self.routes}) != len(self.routes):
            raise DriftEventError("routes contain duplicates")
        _unique_tuple(self.required_check_ids, "required_check_ids")


_PROCESS_METADATA: dict[str, tuple[SemanticSeam, OwnerRole, DriftCadence, str, tuple[str, ...]]] = {
    "minimax.repository": (
        SemanticSeam.PROMPT_MEDIA_TYPING,
        OwnerRole.CORE_MAINTAINER,
        DriftCadence.ON_SOURCE_CHANGE,
        "manual.prompt.structural",
        ("core.prompt_contract",),
    ),
    "minimax.readme": (
        SemanticSeam.PROMPT_MEDIA_TYPING,
        OwnerRole.CORE_MAINTAINER,
        DriftCadence.ON_SOURCE_CHANGE,
        "manual.media.structural",
        ("core.prompt_profile",),
    ),
    "minimax.guide.base": (
        SemanticSeam.PROMPT_MEDIA_TYPING,
        OwnerRole.CORE_MAINTAINER,
        DriftCadence.QUARTERLY,
        "manual.prompt.structural",
        ("core.base_prompt",),
    ),
    "minimax.guide.reference": (
        SemanticSeam.NATIVE_MANUAL_REVERSIBILITY,
        OwnerRole.CORE_MAINTAINER,
        DriftCadence.QUARTERLY,
        "manual.prompt.structural",
        ("core.reference_prompt",),
    ),
    "minimax.model.revision": (
        SemanticSeam.CONTAINER_GEOMETRY,
        OwnerRole.EVALUATION_REVIEWER,
        DriftCadence.PRE_RELEASE,
        "assisted.container.compatibility",
        ("model.container_contract",),
    ),
    "minimax.model.license": (
        SemanticSeam.DEPENDENCY_LICENSE_ADVISORY,
        OwnerRole.RELEASE_REVIEWER,
        DriftCadence.PRE_RELEASE,
        "release.supply.integrity",
        ("release.license", "release.advisory"),
    ),
    "minimax.context_ir.examples": (
        SemanticSeam.CLASSIFIED_ERRORS,
        OwnerRole.CORE_MAINTAINER,
        DriftCadence.ON_SOURCE_CHANGE,
        "manual.prompt.structural",
        ("core.context_ir_boundary",),
    ),
    "comfyui.supported.core": (
        SemanticSeam.FALLBACK_ABSENCE,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.PRE_RELEASE,
        "supported.native.compatibility",
        ("host.supported_core",),
    ),
    "comfyui.supported.text_generate": (
        SemanticSeam.CLASSIFIED_ERRORS,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.PRE_RELEASE,
        "supported.native.compatibility",
        ("host.text_generate_schema",),
    ),
    "comfyui.supported.clip_loader": (
        SemanticSeam.CONTAINER_GEOMETRY,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.PRE_RELEASE,
        "supported.native.compatibility",
        ("host.clip_loader_schema",),
    ),
    "comfyui.supported.native_h3": (
        SemanticSeam.NATIVE_INPUTS_ENUMS,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.PRE_RELEASE,
        "supported.native.compatibility",
        ("host.native_h3_schema",),
    ),
    "comfyui.supported.frontend": (
        SemanticSeam.NATIVE_MANUAL_REVERSIBILITY,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.PRE_RELEASE,
        "latest.sidebar.compatibility",
        ("frontend.supported_contract",),
    ),
    "comfyui.docs.v3": (
        SemanticSeam.V3_DYNAMIC_PATHS,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.QUARTERLY,
        "latest.native.compatibility",
        ("host.v3_dynamic_paths",),
    ),
    "comfyui.docs.sidebar": (
        SemanticSeam.OPEN_BOX_SIDEBAR_STATE,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.QUARTERLY,
        "latest.sidebar.compatibility",
        ("frontend.sidebar_lifecycle",),
    ),
    "comfyui.latest.core": (
        SemanticSeam.SUBGRAPH_INPUTS,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.ON_SOURCE_CHANGE,
        "latest.subgraph.compatibility",
        ("host.subgraph_inputs",),
    ),
    "comfyui.latest.native_h3": (
        SemanticSeam.SIGMA_SHIFT,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.ON_SOURCE_CHANGE,
        "latest.native.compatibility",
        ("host.sigma_shift", "host.latest_native_workflow"),
    ),
    "comfyui.latest.frontend": (
        SemanticSeam.OPEN_BOX_SIDEBAR_STATE,
        OwnerRole.HOST_INTEGRATION_REVIEWER,
        DriftCadence.ON_SOURCE_CHANGE,
        "latest.sidebar.compatibility",
        ("frontend.latest_contract",),
    ),
    "ollama.docs.introduction": (
        SemanticSeam.FALLBACK_ABSENCE,
        OwnerRole.PROVIDER_REVIEWER,
        DriftCadence.QUARTERLY,
        "assisted.ollama.compatibility",
        ("provider.boundary",),
    ),
    "ollama.docs.chat": (
        SemanticSeam.PROMPT_MEDIA_TYPING,
        OwnerRole.PROVIDER_REVIEWER,
        DriftCadence.QUARTERLY,
        "assisted.ollama.compatibility",
        ("provider.chat_schema",),
    ),
    "ollama.docs.structured_outputs": (
        SemanticSeam.CLASSIFIED_ERRORS,
        OwnerRole.PROVIDER_REVIEWER,
        DriftCadence.QUARTERLY,
        "assisted.ollama.compatibility",
        ("provider.structured_output",),
    ),
    "ollama.docs.tags": (
        SemanticSeam.FALLBACK_ABSENCE,
        OwnerRole.PROVIDER_REVIEWER,
        DriftCadence.QUARTERLY,
        "assisted.ollama.compatibility",
        ("provider.model_inventory",),
    ),
    "ollama.docs.show": (
        SemanticSeam.CONTAINER_GEOMETRY,
        OwnerRole.PROVIDER_REVIEWER,
        DriftCadence.QUARTERLY,
        "assisted.ollama.compatibility",
        ("provider.model_metadata",),
    ),
    "ollama.docs.ps": (
        SemanticSeam.FALLBACK_ABSENCE,
        OwnerRole.PROVIDER_REVIEWER,
        DriftCadence.QUARTERLY,
        "assisted.ollama.compatibility",
        ("provider.runtime_inventory",),
    ),
    "ollama.latest.server": (
        SemanticSeam.FALLBACK_ABSENCE,
        OwnerRole.PROVIDER_REVIEWER,
        DriftCadence.ON_SOURCE_CHANGE,
        "assisted.ollama.compatibility",
        ("provider.server_api", "provider.runtime_qualification"),
    ),
    "ollama.python_client": (
        SemanticSeam.CLASSIFIED_ERRORS,
        OwnerRole.PROVIDER_REVIEWER,
        DriftCadence.PRE_RELEASE,
        "assisted.ollama.compatibility",
        ("provider.python_client",),
    ),
    "m14.fidelity_scorecard": (
        SemanticSeam.ARTIFACT_SBOM_HASH,
        OwnerRole.EVALUATION_REVIEWER,
        DriftCadence.PRE_RELEASE,
        "release.supply.integrity",
        ("evaluation.fidelity_fingerprint", "release.supply_manifest"),
    ),
}


def _verify_process_metadata() -> None:
    """Keep every required semantic trigger represented without inventing a new source."""

    required_seams = set(SemanticSeam)
    actual_seams = {metadata[0] for metadata in _PROCESS_METADATA.values()}
    if actual_seams != required_seams:
        raise DriftEventError("process metadata is missing a required semantic seam")


_verify_process_metadata()


def build_default_drift_source_registry() -> tuple[DriftSourceDefinition, ...]:
    """Compose process metadata with every immutable checkpoint surface."""

    checkpoint = build_default_source_drift_checkpoint()
    source_ids = tuple(surface.surface_id for surface in checkpoint.surfaces)
    if len(checkpoint.surfaces) > MAX_DRIFT_SOURCES or set(source_ids) != set(_PROCESS_METADATA):
        raise DriftEventError("process registry is not complete against the frozen checkpoint")
    definitions: list[DriftSourceDefinition] = []
    for surface in checkpoint.surfaces:
        seam, owner, cadence, route_id, check_ids = _PROCESS_METADATA[surface.surface_id]
        definitions.append(
            DriftSourceDefinition(
                source_id=surface.surface_id,
                baseline_identity=surface.expected_identity,
                semantic_extractor_id=f"extractor.{seam.value}.v1",
                owner_role=owner,
                cadence=cadence,
                resource_budget=DriftResourceBudget(),
                unavailable_disposition=UnavailableDisposition.BLOCK_OWNED_ROUTE,
                semantic_seam=seam,
                routes=(_ROUTES[route_id],),
                required_check_ids=check_ids,
            )
        )
    return tuple(definitions)


def _registry_map(
    registry: tuple[DriftSourceDefinition, ...],
) -> dict[str, DriftSourceDefinition]:
    if type(registry) is not tuple or len(registry) > MAX_DRIFT_SOURCES:
        raise DriftEventError("registry must be an exact bounded tuple")
    if not all(type(item) is DriftSourceDefinition for item in registry):
        raise DriftEventError("registry contains a non-exact definition")
    result = {item.source_id: item for item in registry}
    if len(result) != len(registry):
        raise DriftEventError("registry contains duplicate source IDs")
    expected = build_default_drift_source_registry()
    if registry != expected:
        raise DriftEventError("registry differs from the closed default process registry")
    return result


@dataclass(frozen=True, slots=True)
class DriftEvent:
    source_id: str
    baseline_identity: str
    observed_identity: str | None
    baseline_semantic_fingerprint: str
    observed_semantic_fingerprint: str | None
    availability: SourceAvailability
    change_signal: ChangeSignal
    observed_on: str
    publication_state: PublicationState
    published_artifact_hash: str | None
    schema: str = DRIFT_EVENT_SCHEMA

    def __post_init__(self) -> None:
        if type(self) is not DriftEvent:
            raise DriftEventError("event must be an exact contract value")
        if self.schema != DRIFT_EVENT_SCHEMA:
            raise DriftEventError("unsupported event schema")
        _identifier(self.source_id, "source_id")
        _identity(self.baseline_identity, "baseline_identity")
        _identity(self.observed_identity, "observed_identity", nullable=True)
        _fingerprint(self.baseline_semantic_fingerprint, "baseline_semantic_fingerprint")
        _fingerprint(
            self.observed_semantic_fingerprint,
            "observed_semantic_fingerprint",
            nullable=True,
        )
        if type(self.availability) is not SourceAvailability:
            raise DriftEventError("availability must be an exact enum")
        if type(self.change_signal) is not ChangeSignal:
            raise DriftEventError("change_signal must be an exact enum")
        if type(self.observed_on) is not str or _DATE.fullmatch(self.observed_on) is None:
            raise DriftEventError("observed_on is not a bounded calendar date")
        try:
            date.fromisoformat(self.observed_on)
        except ValueError as exc:
            raise DriftEventError("observed_on is not a real calendar date") from exc
        if type(self.publication_state) is not PublicationState:
            raise DriftEventError("publication_state must be an exact enum")
        _fingerprint(self.published_artifact_hash, "published_artifact_hash", nullable=True)
        if self.publication_state is PublicationState.PUBLISHED_EXACT:
            if self.published_artifact_hash is None:
                raise DriftEventError("published exact state requires a published artifact hash")
        elif self.published_artifact_hash is not None:
            raise DriftEventError("unpublished state must not carry a published artifact hash")
        if self.availability is SourceAvailability.UNAVAILABLE:
            if (
                self.observed_identity is not None
                or self.observed_semantic_fingerprint is not None
                or self.change_signal is not ChangeSignal.UNAVAILABLE
            ):
                raise DriftEventError("unavailable event must not invent observed source facts")
        else:
            if self.observed_identity is None:
                raise DriftEventError("available event requires an observed source identity")
            if self.observed_semantic_fingerprint is None:
                if self.change_signal is not ChangeSignal.UNRESOLVED:
                    raise DriftEventError(
                        "missing semantic observation requires the UNRESOLVED change signal"
                    )
                return
            semantic_changed = (
                self.baseline_semantic_fingerprint != self.observed_semantic_fingerprint
            )
            if semantic_changed and self.change_signal in {
                ChangeSignal.NONE,
                ChangeSignal.UNAVAILABLE,
            }:
                raise DriftEventError("semantic drift requires a closed non-empty change signal")
            if not semantic_changed and self.change_signal is not ChangeSignal.NONE:
                raise DriftEventError("unchanged semantics require the NONE change signal")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "source_id": self.source_id,
            "baseline_identity": self.baseline_identity,
            "observed_identity": self.observed_identity,
            "baseline_semantic_fingerprint": self.baseline_semantic_fingerprint,
            "observed_semantic_fingerprint": self.observed_semantic_fingerprint,
            "availability": self.availability.value,
            "change_signal": self.change_signal.value,
            "observed_on": self.observed_on,
            "publication_state": self.publication_state.value,
            "published_artifact_hash": self.published_artifact_hash,
        }


@dataclass(frozen=True, slots=True)
class DriftTemplateProjection:
    template_kind: str
    source_id: str
    baseline_identity: str
    observed_identity: str | None
    drift_kind: DriftKind
    disposition: DriftProcessDisposition
    route_ids: tuple[str, ...]
    check_ids: tuple[str, ...]
    owner_roles: tuple[OwnerRole, ...]
    publication_state: PublicationState

    def __post_init__(self) -> None:
        if self.template_kind not in {"ISSUE", "ADVISORY"}:
            raise DriftEventError("template_kind is not closed")
        _identifier(self.source_id, "template source_id")
        _identity(self.baseline_identity, "template baseline_identity")
        _identity(self.observed_identity, "template observed_identity", nullable=True)
        if (
            type(self.drift_kind) is not DriftKind
            or type(self.disposition) is not DriftProcessDisposition
        ):
            raise DriftEventError("template outcome enums are invalid")
        _unique_tuple(self.route_ids, "template route_ids")
        _unique_tuple(self.check_ids, "template check_ids")
        if (
            type(self.owner_roles) is not tuple
            or not self.owner_roles
            or len(self.owner_roles) > MAX_DRIFT_ITEMS
            or not all(type(role) is OwnerRole for role in self.owner_roles)
            or len(set(self.owner_roles)) != len(self.owner_roles)
        ):
            raise DriftEventError("template owner_roles are invalid")
        if type(self.publication_state) is not PublicationState:
            raise DriftEventError("template publication_state is invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "template_kind": self.template_kind,
            "source_id": self.source_id,
            "baseline_identity": self.baseline_identity,
            "observed_identity": self.observed_identity,
            "drift_kind": self.drift_kind.value,
            "disposition": self.disposition.value,
            "route_ids": list(self.route_ids),
            "check_ids": list(self.check_ids),
            "owner_roles": [role.value for role in self.owner_roles],
            "publication_state": self.publication_state.value,
        }


@dataclass(frozen=True, slots=True)
class DriftProcessReport:
    event_fingerprint: str
    source_id: str
    baseline_identity: str
    observed_identity: str | None
    baseline_semantic_fingerprint: str
    observed_semantic_fingerprint: str | None
    availability: SourceAvailability
    change_signal: ChangeSignal
    observed_on: str
    drift_kind: DriftKind
    materiality: DriftMateriality
    affected_routes: tuple[DriftRoute, ...]
    claim_ceiling: ClaimCeiling
    required_check_ids: tuple[str, ...]
    disposition: DriftProcessDisposition
    review_state: ReviewState
    closure_state: ClosureState
    publication_state: PublicationState
    published_artifact_hash: str | None
    publication_monitoring_state: PublicationMonitoringState
    issue_template: DriftTemplateProjection
    advisory_template: DriftTemplateProjection
    schema: str = DRIFT_PROCESS_REPORT_SCHEMA

    def __post_init__(self) -> None:
        if type(self) is not DriftProcessReport or self.schema != DRIFT_PROCESS_REPORT_SCHEMA:
            raise DriftEventError("report must be an exact supported contract")
        _fingerprint(self.event_fingerprint, "event_fingerprint")
        definition = {item.source_id: item for item in build_default_drift_source_registry()}.get(
            self.source_id
        )
        if definition is None:
            raise DriftEventError("report source_id is not registered")
        _identity(self.baseline_identity, "report baseline_identity")
        _identity(self.observed_identity, "report observed_identity", nullable=True)
        _fingerprint(self.baseline_semantic_fingerprint, "report baseline_semantic_fingerprint")
        _fingerprint(
            self.observed_semantic_fingerprint,
            "report observed_semantic_fingerprint",
            nullable=True,
        )
        for value, enum_type, field in (
            (self.availability, SourceAvailability, "availability"),
            (self.change_signal, ChangeSignal, "change_signal"),
            (self.drift_kind, DriftKind, "drift_kind"),
            (self.materiality, DriftMateriality, "materiality"),
            (self.claim_ceiling, ClaimCeiling, "claim_ceiling"),
            (self.disposition, DriftProcessDisposition, "disposition"),
            (self.review_state, ReviewState, "review_state"),
            (self.closure_state, ClosureState, "closure_state"),
            (self.publication_state, PublicationState, "publication_state"),
            (
                self.publication_monitoring_state,
                PublicationMonitoringState,
                "publication_monitoring_state",
            ),
        ):
            if type(value) is not enum_type:
                raise DriftEventError(f"report {field} must be an exact enum")
        if self.baseline_identity != definition.baseline_identity:
            raise DriftEventError("report baseline identity differs from checkpoint authority")
        if self.affected_routes != definition.routes:
            raise DriftEventError("report routes differ from registry authority")
        if self.required_check_ids != definition.required_check_ids:
            raise DriftEventError("report checks differ from registry authority")
        # CRITICAL: decoded reports cannot claim a drift kind outside the source-owned seam.
        _require_drift_kind_admitted(definition.semantic_seam, self.drift_kind)
        source_event = DriftEvent(
            source_id=self.source_id,
            baseline_identity=self.baseline_identity,
            observed_identity=self.observed_identity,
            baseline_semantic_fingerprint=self.baseline_semantic_fingerprint,
            observed_semantic_fingerprint=self.observed_semantic_fingerprint,
            availability=self.availability,
            change_signal=self.change_signal,
            observed_on=self.observed_on,
            publication_state=self.publication_state,
            published_artifact_hash=self.published_artifact_hash,
        )
        if self.event_fingerprint != source_event.fingerprint:
            raise DriftEventError("report event fingerprint differs from retained event facts")
        if self.drift_kind is not _derive_drift_kind(source_event, definition):
            raise DriftEventError("report drift kind differs from retained event facts")
        expected_outcome = _outcome(self.drift_kind)
        actual_outcome = (
            self.materiality,
            self.claim_ceiling,
            self.disposition,
            self.review_state,
            self.closure_state,
        )
        if actual_outcome != expected_outcome:
            raise DriftEventError("report outcome differs from deterministic drift policy")
        semantic_equal = (
            self.observed_semantic_fingerprint is not None
            and self.baseline_semantic_fingerprint == self.observed_semantic_fingerprint
        )
        identity_equal = self.observed_identity == self.baseline_identity
        if self.drift_kind is DriftKind.SOURCE_UNAVAILABLE:
            if (
                self.availability is not SourceAvailability.UNAVAILABLE
                or self.observed_identity is not None
                or self.observed_semantic_fingerprint is not None
            ):
                raise DriftEventError("source-unavailable report contains observed source facts")
        elif self.availability is not SourceAvailability.AVAILABLE:
            raise DriftEventError("available drift kind requires source availability")
        elif self.drift_kind is DriftKind.NO_DRIFT and not (semantic_equal and identity_equal):
            raise DriftEventError("no-drift report contains changed facts")
        elif self.drift_kind is DriftKind.UNCHANGED_SEAM_AT_NEW_REPO_REVISION and not (
            semantic_equal and not identity_equal
        ):
            raise DriftEventError("unchanged-seam report does not contain repository-only drift")
        elif self.drift_kind is DriftKind.UNRESOLVED:
            if semantic_equal:
                raise DriftEventError("unresolved report cannot contain unchanged semantic facts")
        elif self.drift_kind not in {
            DriftKind.NO_DRIFT,
            DriftKind.UNCHANGED_SEAM_AT_NEW_REPO_REVISION,
            DriftKind.SOURCE_UNAVAILABLE,
            DriftKind.UNRESOLVED,
        } and (semantic_equal or self.observed_semantic_fingerprint is None):
            raise DriftEventError("semantic-drift report does not contain changed semantic facts")
        _fingerprint(self.published_artifact_hash, "published_artifact_hash", nullable=True)
        expected_monitoring = _publication_monitoring(
            self.publication_state, self.published_artifact_hash
        )
        if self.publication_monitoring_state is not expected_monitoring:
            raise DriftEventError("report publication monitoring state is inconsistent")
        if type(self.issue_template) is not DriftTemplateProjection:
            raise DriftEventError("report issue template is invalid")
        if type(self.advisory_template) is not DriftTemplateProjection:
            raise DriftEventError("report advisory template is invalid")
        expected_base = (
            self.source_id,
            self.baseline_identity,
            self.observed_identity,
            self.drift_kind,
            self.disposition,
            tuple(route.route_id for route in self.affected_routes),
            self.required_check_ids,
            (definition.owner_role,),
            self.publication_state,
        )
        for template, kind in (
            (self.issue_template, "ISSUE"),
            (self.advisory_template, "ADVISORY"),
        ):
            actual = (
                template.source_id,
                template.baseline_identity,
                template.observed_identity,
                template.drift_kind,
                template.disposition,
                template.route_ids,
                template.check_ids,
                template.owner_roles,
                template.publication_state,
            )
            if template.template_kind != kind or actual != expected_base:
                raise DriftEventError("report template differs from closed report fields")

    @property
    def blocking(self) -> bool:
        return self.materiality is DriftMateriality.BLOCKING

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "event_fingerprint": self.event_fingerprint,
            "source_id": self.source_id,
            "baseline_identity": self.baseline_identity,
            "observed_identity": self.observed_identity,
            "baseline_semantic_fingerprint": self.baseline_semantic_fingerprint,
            "observed_semantic_fingerprint": self.observed_semantic_fingerprint,
            "availability": self.availability.value,
            "change_signal": self.change_signal.value,
            "observed_on": self.observed_on,
            "drift_kind": self.drift_kind.value,
            "materiality": self.materiality.value,
            "affected_routes": [route.to_wire() for route in self.affected_routes],
            "claim_ceiling": self.claim_ceiling.value,
            "required_check_ids": list(self.required_check_ids),
            "disposition": self.disposition.value,
            "review_state": self.review_state.value,
            "closure_state": self.closure_state.value,
            "publication_state": self.publication_state.value,
            "published_artifact_hash": self.published_artifact_hash,
            "publication_monitoring_state": self.publication_monitoring_state.value,
            "issue_template": self.issue_template.to_wire(),
            "advisory_template": self.advisory_template.to_wire(),
        }


_SIGNAL_TO_KIND = {
    ChangeSignal.ADDITIVE: DriftKind.ADDITIVE_NON_SUBSTITUTING,
    ChangeSignal.PRESENTATION_BUILD: DriftKind.PRESENTATION_OR_BUILD_DRIFT,
    ChangeSignal.COMPATIBLE_CONTRACT: DriftKind.COMPATIBLE_CONTRACT_DRIFT,
    ChangeSignal.CONTRACT_BREAKING: DriftKind.ROUTE_BLOCKING_CONTRACT_DRIFT,
    ChangeSignal.SECURITY_LICENSE: DriftKind.SECURITY_OR_LICENSE_BLOCKER,
    ChangeSignal.INTEGRITY_MISMATCH: DriftKind.SECURITY_OR_LICENSE_BLOCKER,
    ChangeSignal.UNRESOLVED: DriftKind.UNRESOLVED,
}

_CONTRACT_SIGNALS = frozenset(
    {
        ChangeSignal.ADDITIVE,
        ChangeSignal.COMPATIBLE_CONTRACT,
        ChangeSignal.CONTRACT_BREAKING,
    }
)
_SEAM_SIGNAL_POLICY: dict[SemanticSeam, frozenset[ChangeSignal]] = {
    SemanticSeam.PROMPT_MEDIA_TYPING: _CONTRACT_SIGNALS,
    SemanticSeam.NATIVE_INPUTS_ENUMS: _CONTRACT_SIGNALS,
    SemanticSeam.V3_DYNAMIC_PATHS: _CONTRACT_SIGNALS,
    SemanticSeam.SUBGRAPH_INPUTS: _CONTRACT_SIGNALS,
    SemanticSeam.SIGMA_SHIFT: _CONTRACT_SIGNALS,
    SemanticSeam.OPEN_BOX_SIDEBAR_STATE: frozenset(
        {*_CONTRACT_SIGNALS, ChangeSignal.PRESENTATION_BUILD}
    ),
    SemanticSeam.NATIVE_MANUAL_REVERSIBILITY: _CONTRACT_SIGNALS,
    SemanticSeam.CLASSIFIED_ERRORS: _CONTRACT_SIGNALS,
    SemanticSeam.CONTAINER_GEOMETRY: _CONTRACT_SIGNALS,
    SemanticSeam.FALLBACK_ABSENCE: _CONTRACT_SIGNALS,
    SemanticSeam.DEPENDENCY_LICENSE_ADVISORY: frozenset(
        {
            ChangeSignal.ADDITIVE,
            ChangeSignal.COMPATIBLE_CONTRACT,
            ChangeSignal.SECURITY_LICENSE,
        }
    ),
    SemanticSeam.ARTIFACT_SBOM_HASH: frozenset({ChangeSignal.INTEGRITY_MISMATCH}),
}
_GLOBAL_FACT_DRIFT_KINDS = frozenset(
    {
        DriftKind.NO_DRIFT,
        DriftKind.UNCHANGED_SEAM_AT_NEW_REPO_REVISION,
        DriftKind.SOURCE_UNAVAILABLE,
        DriftKind.UNRESOLVED,
    }
)
_SEAM_DRIFT_KIND_POLICY: dict[SemanticSeam, frozenset[DriftKind]] = {
    seam: frozenset(_SIGNAL_TO_KIND[signal] for signal in signals) | _GLOBAL_FACT_DRIFT_KINDS
    for seam, signals in _SEAM_SIGNAL_POLICY.items()
}


def _verify_signal_policy() -> None:
    if set(_SEAM_SIGNAL_POLICY) != set(SemanticSeam):
        raise DriftEventError("signal policy is incomplete for the closed semantic seam registry")
    security_owners = {
        seam
        for seam, signals in _SEAM_SIGNAL_POLICY.items()
        if ChangeSignal.SECURITY_LICENSE in signals
    }
    if security_owners != {SemanticSeam.DEPENDENCY_LICENSE_ADVISORY}:
        raise DriftEventError("security/license signal escaped its owned seam")
    integrity_owners = {
        seam
        for seam, signals in _SEAM_SIGNAL_POLICY.items()
        if ChangeSignal.INTEGRITY_MISMATCH in signals
    }
    if integrity_owners != {SemanticSeam.ARTIFACT_SBOM_HASH}:
        raise DriftEventError("integrity signal escaped its owned seam")


_verify_signal_policy()


def _require_drift_kind_admitted(seam: SemanticSeam, kind: DriftKind) -> None:
    if kind not in _SEAM_DRIFT_KIND_POLICY[seam]:
        raise DriftEventError("drift kind is not admitted by the registered semantic seam")


def _publication_monitoring(
    state: PublicationState, published_hash: str | None
) -> PublicationMonitoringState:
    if state is PublicationState.DO_NOT_PUBLISH:
        if published_hash is not None:
            raise DriftEventError("do-not-publish state cannot carry a published hash")
        return PublicationMonitoringState.BLOCKED_BY_POLICY
    if state is PublicationState.APPROVED_UNPUBLISHED:
        if published_hash is not None:
            raise DriftEventError("approved unpublished state cannot carry a published hash")
        return PublicationMonitoringState.INACTIVE_NO_PUBLISHED_HASH
    if published_hash is None:
        raise DriftEventError("published exact state requires a published hash")
    return PublicationMonitoringState.ACTIVE_EXACT_HASH


def _outcome(
    kind: DriftKind,
) -> tuple[
    DriftMateriality,
    ClaimCeiling,
    DriftProcessDisposition,
    ReviewState,
    ClosureState,
]:
    if kind in {DriftKind.NO_DRIFT, DriftKind.UNCHANGED_SEAM_AT_NEW_REPO_REVISION}:
        return (
            DriftMateriality.NONE,
            ClaimCeiling.EXISTING_CLAIMS_ONLY,
            DriftProcessDisposition.PASS,
            ReviewState.NOT_REQUIRED,
            ClosureState.CLOSED,
        )
    if kind in {
        DriftKind.ADDITIVE_NON_SUBSTITUTING,
        DriftKind.PRESENTATION_OR_BUILD_DRIFT,
        DriftKind.COMPATIBLE_CONTRACT_DRIFT,
    }:
        return (
            DriftMateriality.NON_BLOCKING,
            ClaimCeiling.OWNED_ROUTE_ONLY,
            DriftProcessDisposition.RUN_AFFECTED_CHECKS,
            ReviewState.REQUIRED,
            ClosureState.OPEN,
        )
    if kind is DriftKind.SECURITY_OR_LICENSE_BLOCKER:
        return (
            DriftMateriality.BLOCKING,
            ClaimCeiling.NO_PUBLICATION,
            DriftProcessDisposition.BLOCK_PUBLICATION,
            ReviewState.REQUIRED,
            ClosureState.OPEN,
        )
    return (
        DriftMateriality.BLOCKING,
        ClaimCeiling.OWNED_ROUTE_ONLY,
        DriftProcessDisposition.BLOCK_OWNED_ROUTE,
        ReviewState.REQUIRED,
        ClosureState.OPEN,
    )


def _derive_drift_kind(event: DriftEvent, definition: DriftSourceDefinition) -> DriftKind:
    if event.baseline_identity != definition.baseline_identity:
        raise DriftEventError("event baseline identity differs from checkpoint authority")
    if event.availability is SourceAvailability.UNAVAILABLE:
        return DriftKind.SOURCE_UNAVAILABLE
    if event.change_signal is ChangeSignal.UNRESOLVED:
        return DriftKind.UNRESOLVED
    if event.baseline_semantic_fingerprint == event.observed_semantic_fingerprint:
        return (
            DriftKind.NO_DRIFT
            if event.baseline_identity == event.observed_identity
            else DriftKind.UNCHANGED_SEAM_AT_NEW_REPO_REVISION
        )
    # CRITICAL: caller signals cannot escalate beyond the registered source-owned seam.
    if event.change_signal not in _SEAM_SIGNAL_POLICY[definition.semantic_seam]:
        raise DriftEventError("change signal is not admitted by the registered semantic seam")
    try:
        return _SIGNAL_TO_KIND[event.change_signal]
    except KeyError as exc:
        raise DriftEventError("change signal cannot classify semantic drift") from exc


def classify_drift_event(
    event: DriftEvent,
    registry: tuple[DriftSourceDefinition, ...] | None = None,
) -> DriftProcessReport:
    """Classify explicit content-free facts; never execute or remediate the selected route."""

    if type(event) is not DriftEvent:
        raise DriftEventError("event must be an exact DriftEvent")
    active_registry = build_default_drift_source_registry() if registry is None else registry
    definition = _registry_map(active_registry).get(event.source_id)
    if definition is None:
        raise DriftEventError("event source_id is not registered")
    kind = _derive_drift_kind(event, definition)
    materiality, claim, disposition, review, closure = _outcome(kind)
    monitoring = _publication_monitoring(event.publication_state, event.published_artifact_hash)
    route_ids = tuple(route.route_id for route in definition.routes)

    def template(template_kind: str) -> DriftTemplateProjection:
        return DriftTemplateProjection(
            template_kind=template_kind,
            source_id=event.source_id,
            baseline_identity=event.baseline_identity,
            observed_identity=event.observed_identity,
            drift_kind=kind,
            disposition=disposition,
            route_ids=route_ids,
            check_ids=definition.required_check_ids,
            owner_roles=(definition.owner_role,),
            publication_state=event.publication_state,
        )

    return DriftProcessReport(
        event_fingerprint=event.fingerprint,
        source_id=event.source_id,
        baseline_identity=event.baseline_identity,
        observed_identity=event.observed_identity,
        baseline_semantic_fingerprint=event.baseline_semantic_fingerprint,
        observed_semantic_fingerprint=event.observed_semantic_fingerprint,
        availability=event.availability,
        change_signal=event.change_signal,
        observed_on=event.observed_on,
        drift_kind=kind,
        materiality=materiality,
        affected_routes=definition.routes,
        claim_ceiling=claim,
        required_check_ids=definition.required_check_ids,
        disposition=disposition,
        review_state=review,
        closure_state=closure,
        publication_state=event.publication_state,
        published_artifact_hash=event.published_artifact_hash,
        publication_monitoring_state=monitoring,
        issue_template=template("ISSUE"),
        advisory_template=template("ADVISORY"),
    )


_EVENT_FIELDS = {
    "schema",
    "source_id",
    "baseline_identity",
    "observed_identity",
    "baseline_semantic_fingerprint",
    "observed_semantic_fingerprint",
    "availability",
    "change_signal",
    "observed_on",
    "publication_state",
    "published_artifact_hash",
}


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DriftEventError(f"JSON contains duplicate member {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise DriftEventError(f"JSON contains non-finite constant {value!r}")


def _reject_resources(value: object, *, depth: int = 0) -> None:
    if depth > MAX_DRIFT_DEPTH:
        raise DriftEventError("JSON exceeds the depth limit")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        if len(value) > MAX_DRIFT_STRING_LENGTH or any(ord(char) < 0x20 for char in value):
            raise DriftEventError("JSON contains an unsafe or oversized string")
        return
    if type(value) is list:
        if len(value) > MAX_DRIFT_ITEMS:
            raise DriftEventError("JSON list exceeds the item limit")
        for item in value:
            _reject_resources(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > MAX_DRIFT_ITEMS:
            raise DriftEventError("JSON object exceeds the item limit")
        for key, item in value.items():
            if type(key) is not str:
                raise DriftEventError("JSON member name must be a string")
            _reject_resources(item, depth=depth + 1)
        return
    raise DriftEventError("JSON contains a non-exact JSON type")


def _decode_json(payload: str | bytes | bytearray, maximum: int) -> object:
    # CRITICAL: exact built-in types prevent hostile subclasses from spoofing byte limits.
    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise DriftEventError("JSON is not strict UTF-8") from exc
        text = payload
    elif type(payload) in {bytes, bytearray}:
        encoded = bytes(cast(bytes | bytearray, payload))
        if encoded.startswith(b"\xef\xbb\xbf"):
            raise DriftEventError("JSON must not contain a UTF-8 BOM")
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise DriftEventError("JSON is not strict UTF-8") from exc
    else:
        raise DriftEventError("JSON must be text or bytes")
    if len(encoded) > maximum:
        raise DriftEventError("JSON exceeds the byte limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except DriftEventError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DriftEventError("JSON is malformed or too deep") from exc
    _reject_resources(value)
    return value


def _object(value: object, fields: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise DriftEventError(f"{name} must be an exact object")
    result = cast(dict[str, object], value)
    unknown = set(result) - fields
    missing = fields - set(result)
    if unknown:
        raise DriftEventError(f"{name} contains unknown fields")
    if missing:
        raise DriftEventError(f"{name} is missing required fields")
    return result


def decode_drift_event_json(payload: str | bytes | bytearray) -> DriftEvent:
    """Strictly decode one content-free local drift observation."""

    value = _object(_decode_json(payload, MAX_DRIFT_EVENT_BYTES), _EVENT_FIELDS, "event")
    event = DriftEvent(
        schema=cast(str, value["schema"]),
        source_id=cast(str, value["source_id"]),
        baseline_identity=cast(str, value["baseline_identity"]),
        observed_identity=cast(str | None, value["observed_identity"]),
        baseline_semantic_fingerprint=cast(str, value["baseline_semantic_fingerprint"]),
        observed_semantic_fingerprint=cast(str | None, value["observed_semantic_fingerprint"]),
        availability=cast(
            SourceAvailability,
            _closed_enum(SourceAvailability, value["availability"], "availability"),
        ),
        change_signal=cast(
            ChangeSignal,
            _closed_enum(ChangeSignal, value["change_signal"], "change_signal"),
        ),
        observed_on=cast(str, value["observed_on"]),
        publication_state=cast(
            PublicationState,
            _closed_enum(PublicationState, value["publication_state"], "publication_state"),
        ),
        published_artifact_hash=cast(str | None, value["published_artifact_hash"]),
    )
    definition = {item.source_id: item for item in build_default_drift_source_registry()}.get(
        event.source_id
    )
    if definition is None:
        raise DriftEventError("event source_id is not registered")
    if event.baseline_identity != definition.baseline_identity:
        raise DriftEventError("event baseline identity differs from checkpoint authority")
    return event


_REPORT_FIELDS = {
    "schema",
    "event_fingerprint",
    "source_id",
    "baseline_identity",
    "observed_identity",
    "baseline_semantic_fingerprint",
    "observed_semantic_fingerprint",
    "availability",
    "change_signal",
    "observed_on",
    "drift_kind",
    "materiality",
    "affected_routes",
    "claim_ceiling",
    "required_check_ids",
    "disposition",
    "review_state",
    "closure_state",
    "publication_state",
    "published_artifact_hash",
    "publication_monitoring_state",
    "issue_template",
    "advisory_template",
}
_ROUTE_FIELDS = {
    "route_id",
    "task_mode",
    "product_surface",
    "host_profile",
    "provider_profile",
    "evaluation_layer",
}
_TEMPLATE_FIELDS = {
    "template_kind",
    "source_id",
    "baseline_identity",
    "observed_identity",
    "drift_kind",
    "disposition",
    "route_ids",
    "check_ids",
    "owner_roles",
    "publication_state",
}


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise DriftEventError(f"{field} must be a list")
    result = tuple(cast(list[object], value))
    if not all(type(item) is str for item in result):
        raise DriftEventError(f"{field} must contain strings")
    strings = cast(tuple[str, ...], result)
    _unique_tuple(strings, field)
    return strings


def _route_from_wire(value: object) -> DriftRoute:
    route = _object(value, _ROUTE_FIELDS, "route")
    return DriftRoute(
        route_id=cast(str, route["route_id"]),
        task_mode=cast(
            TaskModeDimension,
            _closed_enum(TaskModeDimension, route["task_mode"], "route.task_mode"),
        ),
        product_surface=cast(
            ProductSurfaceDimension,
            _closed_enum(
                ProductSurfaceDimension, route["product_surface"], "route.product_surface"
            ),
        ),
        host_profile=cast(
            HostProfileDimension,
            _closed_enum(HostProfileDimension, route["host_profile"], "route.host_profile"),
        ),
        provider_profile=cast(
            ProviderProfileDimension,
            _closed_enum(
                ProviderProfileDimension, route["provider_profile"], "route.provider_profile"
            ),
        ),
        evaluation_layer=cast(
            EvaluationLayerDimension,
            _closed_enum(
                EvaluationLayerDimension, route["evaluation_layer"], "route.evaluation_layer"
            ),
        ),
    )


def _template_from_wire(value: object) -> DriftTemplateProjection:
    template = _object(value, _TEMPLATE_FIELDS, "template")
    owners_value = template["owner_roles"]
    if type(owners_value) is not list:
        raise DriftEventError("template.owner_roles must be a list")
    owners = tuple(cast(list[object], owners_value))
    if (
        not owners
        or len(owners) > MAX_DRIFT_ITEMS
        or len(set(owners)) != len(owners)
        or not all(type(owner) is str for owner in owners)
    ):
        raise DriftEventError("template.owner_roles must contain bounded unique strings")
    return DriftTemplateProjection(
        template_kind=cast(str, template["template_kind"]),
        source_id=cast(str, template["source_id"]),
        baseline_identity=cast(str, template["baseline_identity"]),
        observed_identity=cast(str | None, template["observed_identity"]),
        drift_kind=cast(
            DriftKind,
            _closed_enum(DriftKind, template["drift_kind"], "template.drift_kind"),
        ),
        disposition=cast(
            DriftProcessDisposition,
            _closed_enum(DriftProcessDisposition, template["disposition"], "template.disposition"),
        ),
        route_ids=_string_tuple(template["route_ids"], "template.route_ids"),
        check_ids=_string_tuple(template["check_ids"], "template.check_ids"),
        owner_roles=tuple(
            cast(OwnerRole, _closed_enum(OwnerRole, owner, "template.owner_role"))
            for owner in cast(tuple[str, ...], owners)
        ),
        publication_state=cast(
            PublicationState,
            _closed_enum(
                PublicationState,
                template["publication_state"],
                "template.publication_state",
            ),
        ),
    )


def decode_drift_process_report_json(payload: str | bytes | bytearray) -> DriftProcessReport:
    """Strictly decode and cross-check one report projection."""

    value = _object(_decode_json(payload, MAX_DRIFT_REPORT_BYTES), _REPORT_FIELDS, "report")
    routes_value = value["affected_routes"]
    if type(routes_value) is not list or not routes_value or len(routes_value) > MAX_DRIFT_ROUTES:
        raise DriftEventError("report routes must be a bounded non-empty list")
    routes = tuple(_route_from_wire(item) for item in cast(list[object], routes_value))
    return DriftProcessReport(
        schema=cast(str, value["schema"]),
        event_fingerprint=cast(str, value["event_fingerprint"]),
        source_id=cast(str, value["source_id"]),
        baseline_identity=cast(str, value["baseline_identity"]),
        observed_identity=cast(str | None, value["observed_identity"]),
        baseline_semantic_fingerprint=cast(str, value["baseline_semantic_fingerprint"]),
        observed_semantic_fingerprint=cast(str | None, value["observed_semantic_fingerprint"]),
        availability=cast(
            SourceAvailability,
            _closed_enum(SourceAvailability, value["availability"], "availability"),
        ),
        change_signal=cast(
            ChangeSignal,
            _closed_enum(ChangeSignal, value["change_signal"], "change_signal"),
        ),
        observed_on=cast(str, value["observed_on"]),
        drift_kind=cast(DriftKind, _closed_enum(DriftKind, value["drift_kind"], "drift_kind")),
        materiality=cast(
            DriftMateriality,
            _closed_enum(DriftMateriality, value["materiality"], "materiality"),
        ),
        affected_routes=routes,
        claim_ceiling=cast(
            ClaimCeiling,
            _closed_enum(ClaimCeiling, value["claim_ceiling"], "claim_ceiling"),
        ),
        required_check_ids=_string_tuple(value["required_check_ids"], "required_check_ids"),
        disposition=cast(
            DriftProcessDisposition,
            _closed_enum(DriftProcessDisposition, value["disposition"], "disposition"),
        ),
        review_state=cast(
            ReviewState,
            _closed_enum(ReviewState, value["review_state"], "review_state"),
        ),
        closure_state=cast(
            ClosureState,
            _closed_enum(ClosureState, value["closure_state"], "closure_state"),
        ),
        publication_state=cast(
            PublicationState,
            _closed_enum(PublicationState, value["publication_state"], "publication_state"),
        ),
        published_artifact_hash=cast(str | None, value["published_artifact_hash"]),
        publication_monitoring_state=cast(
            PublicationMonitoringState,
            _closed_enum(
                PublicationMonitoringState,
                value["publication_monitoring_state"],
                "publication_monitoring_state",
            ),
        ),
        issue_template=_template_from_wire(value["issue_template"]),
        advisory_template=_template_from_wire(value["advisory_template"]),
    )


__all__ = [
    "DRIFT_EVENT_SCHEMA",
    "DRIFT_PROCESS_REPORT_SCHEMA",
    "ChangeSignal",
    "ClaimCeiling",
    "ClosureState",
    "DriftCadence",
    "DriftProcessDisposition",
    "DriftEvent",
    "DriftEventError",
    "DriftKind",
    "DriftMateriality",
    "DriftProcessReport",
    "DriftResourceBudget",
    "DriftRoute",
    "DriftSourceDefinition",
    "DriftTemplateProjection",
    "EvaluationLayerDimension",
    "HostProfileDimension",
    "OwnerRole",
    "ProductSurfaceDimension",
    "ProviderProfileDimension",
    "PublicationMonitoringState",
    "PublicationState",
    "ReviewState",
    "SemanticSeam",
    "SourceAvailability",
    "TaskModeDimension",
    "UnavailableDisposition",
    "build_default_drift_source_registry",
    "classify_drift_event",
    "decode_drift_event_json",
    "decode_drift_process_report_json",
]
