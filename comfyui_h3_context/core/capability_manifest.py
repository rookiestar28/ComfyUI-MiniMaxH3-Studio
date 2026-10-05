"""M10-01 capability maturity and backend-owned binding manifest.

This module is a pure facade over the declarative node contracts. It never imports ComfyUI or a
frontend runtime, mutates a host registry, selects a provider, or executes a model. The manifest is
the one portable mapping from public node ports to capability and projection metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .contracts import EvidenceLevel
from .errors import CapabilityManifestError, HostCanaryError
from .node_contracts import (
    NodeContract,
    NodeContractRegistry,
    NodeSocket,
    NodeSocketType,
    default_node_contract_registry,
)

CAPABILITY_REGISTRY_SCHEMA = "h3.context.capability.registry.v1"
CAPABILITY_MANIFEST_SCHEMA = "h3.context.binding.manifest.v1"
HOST_CANARY_SCHEMA = "h3.host.canary.v1"
MAX_CAPABILITIES = 128
MAX_BINDINGS = 512
MAX_CANARY_FINDINGS = 16
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FIELD_ID = re.compile(r"[a-z][a-z0-9_.-]{0,191}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_SENSITIVE_MARKERS = (
    "http://",
    "https://",
    "authorization",
    "bearer ",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token=",
    "sig=",
    "x-amz-",
)


class CapabilityMaturity(str, Enum):
    """Closed implementation/evidence maturity values required by M10."""

    DETERMINISTIC = "deterministic"
    CONCRETE_EXECUTABLE = "concrete_executable"
    INJECTED_ONLY = "injected_only"
    FIXTURE_ONLY = "fixture_only"
    OPTIONAL_LIVE = "optional_live"
    UNSUPPORTED = "unsupported"


class BindingKind(str, Enum):
    """Projection kind for a field in a public node contract."""

    INPUT = "input"
    OUTPUT = "output"
    WIDGET = "widget"


class BindingCardinality(str, Enum):
    SINGLE = "single"
    OPTIONAL_SINGLE = "optional_single"
    LIST = "list"
    OPTIONAL_LIST = "optional_list"


class BindingPriority(str, Enum):
    """Small closed priority scale used by a future projection, not a UI layout."""

    CORE = "core"
    SUPPORTING = "supporting"
    ADVANCED = "advanced"


class BindingVisibility(str, Enum):
    PUBLIC = "public"
    ADVANCED = "advanced"
    INTERNAL = "internal"
    HIDDEN = "hidden"


class BindingSyncDirection(str, Enum):
    TO_CORE = "to_core"
    FROM_CORE = "from_core"
    BIDIRECTIONAL = "bidirectional"


class BindingSensitivity(str, Enum):
    PUBLIC = "public"
    CANONICAL = "canonical"
    MEDIA_RUNTIME = "media_runtime"
    CREDENTIAL_REFERENCE = "credential_reference"


class BindingConsent(str, Enum):
    NONE = "none"
    REMOTE_MEDIA = "remote_media"
    CREDENTIAL = "credential"


class HostCanarySeam(str, Enum):
    SIDEBAR_REGISTRATION = "sidebar_registration"
    GRAPH_CHANGE = "graph_change"
    GRAPH_MUTATION_UNDO = "graph_mutation_undo"
    SUBGRAPH_TRAVERSAL = "subgraph_traversal"
    DESTROY_RELOAD = "destroy_reload"


class HostCanaryOutcome(str, Enum):
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    NOT_RUN = "not_run"


ScalarValue = str | int | float | bool | None


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise CapabilityManifestError(f"{field} must be a bounded identifier")
    return value


def _code(value: object, field: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value) is None:
        raise CapabilityManifestError(f"{field} must be a lower-case bounded code")
    return value


def _text(value: object, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CapabilityManifestError(f"{field} must be a bounded non-empty string")
    lowered = value.casefold()
    if (
        any(marker in lowered for marker in _SENSITIVE_MARKERS)
        or value.startswith("/")
        or "\\" in value
    ):
        raise CapabilityManifestError(f"{field} contains sensitive or remote metadata")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise CapabilityManifestError(f"{field} contains a control character")
    return value


def _enum(value: object, expected: type[Enum], field: str) -> Enum:
    if not isinstance(value, expected):
        raise CapabilityManifestError(f"{field} must be a {expected.__name__}")
    return value


def _unique_strings(values: object, field: str, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > maximum:
        raise CapabilityManifestError(f"{field} must be a bounded tuple")
    result = tuple(_identifier(value, f"{field} item") for value in values)
    if len(result) != len(set(result)):
        raise CapabilityManifestError(f"{field} must not contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration:
    """One explicit capability maturity/disposition declaration."""

    capability_id: str
    maturity: CapabilityMaturity
    owner_stage: str
    description: str
    evidence_level: EvidenceLevel
    provider_family: str = "none"
    supported_modes: tuple[str, ...] = ()
    requires_network: bool = False
    requires_consent: bool = False
    notes: str = "explicitly bounded"

    def __post_init__(self) -> None:
        _code(self.capability_id, "capability_id")
        _enum(self.maturity, CapabilityMaturity, "capability maturity")
        if re.fullmatch(r"M[0-9]+-[0-9]{2}", self.owner_stage) is None:
            raise CapabilityManifestError("capability owner_stage must use Mx-yy")
        _text(self.description, "capability description")
        _enum(self.evidence_level, EvidenceLevel, "capability evidence_level")
        _code(self.provider_family, "capability provider_family")
        _unique_strings(self.supported_modes, "capability supported_modes")
        if not isinstance(self.requires_network, bool) or not isinstance(
            self.requires_consent, bool
        ):
            raise CapabilityManifestError("capability network/consent flags must be booleans")
        _text(self.notes, "capability notes", 1024)
        if self.maturity is CapabilityMaturity.OPTIONAL_LIVE and not self.requires_consent:
            raise CapabilityManifestError("optional_live capabilities require explicit consent")

    def to_wire(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "maturity": self.maturity.value,
            "owner_stage": self.owner_stage,
            "description": self.description,
            "evidence_level": self.evidence_level.value,
            "provider_family": self.provider_family,
            "supported_modes": list(self.supported_modes),
            "requires_network": self.requires_network,
            "requires_consent": self.requires_consent,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class CapabilityRegistry:
    """Immutable closed set of capability declarations."""

    declarations: tuple[CapabilityDeclaration, ...]
    schema: str = CAPABILITY_REGISTRY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CAPABILITY_REGISTRY_SCHEMA:
            raise CapabilityManifestError("unsupported capability registry schema")
        if not isinstance(self.declarations, tuple) or len(self.declarations) > MAX_CAPABILITIES:
            raise CapabilityManifestError("capability declarations exceed the finite bound")
        if not all(isinstance(item, CapabilityDeclaration) for item in self.declarations):
            raise CapabilityManifestError("capability declarations contain an invalid value")
        ids = tuple(item.capability_id for item in self.declarations)
        if len(ids) != len(set(ids)):
            raise CapabilityManifestError("capability IDs must be unique")

    def get(self, capability_id: str) -> CapabilityDeclaration:
        _code(capability_id, "capability_id")
        for item in self.declarations:
            if item.capability_id == capability_id:
                return item
        raise CapabilityManifestError(f"unknown capability: {capability_id!r}")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "capabilities": [item.to_wire() for item in self.declarations],
        }


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not slug or not slug[0].isalpha():
        raise CapabilityManifestError("cannot derive a stable lower-case node slug")
    return slug


def _capability_for_node(node_id: str) -> str:
    return "node." + _slug(node_id.rsplit(".", 1)[-1])


_NODE_MATURITY: dict[str, CapabilityMaturity] = {
    "Request": CapabilityMaturity.DETERMINISTIC,
    "ReferenceRegistry": CapabilityMaturity.DETERMINISTIC,
    "Plan": CapabilityMaturity.DETERMINISTIC,
    "Compiler": CapabilityMaturity.DETERMINISTIC,
    "FullReference": CapabilityMaturity.DETERMINISTIC,
    "Validator": CapabilityMaturity.DETERMINISTIC,
    "Preview": CapabilityMaturity.DETERMINISTIC,
    "AuditOverride": CapabilityMaturity.DETERMINISTIC,
    "ProviderTransparency": CapabilityMaturity.DETERMINISTIC,
    "Reliability": CapabilityMaturity.DETERMINISTIC,
    "NativeH3Adapter": CapabilityMaturity.DETERMINISTIC,
    "OfficialContextIR": CapabilityMaturity.INJECTED_ONLY,
    "MediaAdmissionProducer": CapabilityMaturity.DETERMINISTIC,
    "VisualPerceptionProducer": CapabilityMaturity.UNSUPPORTED,
    "AudioPerceptionProducer": CapabilityMaturity.UNSUPPORTED,
    "SourceProfiledRenderer": CapabilityMaturity.DETERMINISTIC,
    "LocalReconstruction": CapabilityMaturity.DETERMINISTIC,
    "FeasibleAVTimeline": CapabilityMaturity.DETERMINISTIC,
    "HierarchicalEvidenceReduction": CapabilityMaturity.DETERMINISTIC,
    "ConstrainedSemanticPlanning": CapabilityMaturity.DETERMINISTIC,
    "ProductShell": CapabilityMaturity.DETERMINISTIC,
    "SemanticProposalProducer": CapabilityMaturity.CONCRETE_EXECUTABLE,
}


def _node_declaration(node: NodeContract) -> CapabilityDeclaration:
    short_name = node.node_id.rsplit(".", 1)[-1]
    maturity = _NODE_MATURITY.get(short_name, CapabilityMaturity.UNSUPPORTED)
    return CapabilityDeclaration(
        _capability_for_node(node.node_id),
        maturity,
        node.roadmap_stage,
        f"{node.display_name} public node contract",
        EvidenceLevel.FRAMEWORK_REFERENCE,
        provider_family=(
            "official_minimax"
            if short_name == "OfficialContextIR"
            else "ollama"
            if short_name == "SemanticProposalProducer"
            else "none"
        ),
        requires_network=short_name in {"OfficialContextIR", "SemanticProposalProducer"},
        requires_consent=short_name == "OfficialContextIR",
        notes=(
            "injected transport remains required"
            if short_name == "OfficialContextIR"
            else "fixed-loopback qualified local execution only"
            if short_name == "SemanticProposalProducer"
            else "node contract is deterministic and provider-free"
        ),
    )


def default_capability_registry() -> CapabilityRegistry:
    """Return the reviewed capability classification for current public and planned seams."""

    node_declarations = tuple(
        _node_declaration(item) for item in default_node_contract_registry().definitions
    )
    additional = (
        CapabilityDeclaration(
            "perception.image_observation",
            CapabilityMaturity.INJECTED_ONLY,
            "M5-01",
            "Image/VLM observation boundary with explicit injected adapter",
            EvidenceLevel.EXPERIMENTAL,
            notes="no decoder or VLM runtime ships in the package",
        ),
        CapabilityDeclaration(
            "perception.video_analysis",
            CapabilityMaturity.INJECTED_ONLY,
            "M6-01",
            "Video sampling and temporal observation boundary",
            EvidenceLevel.EXPERIMENTAL,
            notes="source timestamps and adapter output are caller supplied",
        ),
        CapabilityDeclaration(
            "perception.audio_analysis",
            CapabilityMaturity.INJECTED_ONLY,
            "M6-02",
            "Audio analysis and speech evidence boundary",
            EvidenceLevel.EXPERIMENTAL,
            notes="audio decoder and ASR runtime are not bundled",
        ),
        CapabilityDeclaration(
            "perception.cross_reference",
            CapabilityMaturity.INJECTED_ONLY,
            "M6-03",
            "Cross-modal entity/reference resolution boundary",
            EvidenceLevel.EXPERIMENTAL,
            notes="identity claims remain evidence until qualified",
        ),
        CapabilityDeclaration(
            "perception.semantic_enrichment",
            CapabilityMaturity.INJECTED_ONLY,
            "M6-06",
            "Constrained semantic proposal boundary",
            EvidenceLevel.EXPERIMENTAL,
            notes="proposals never mutate hard constraints automatically",
        ),
        CapabilityDeclaration(
            "workflow.frozen_fixture",
            CapabilityMaturity.FIXTURE_ONLY,
            "M9-03",
            "Static metadata/workflow fixture evidence",
            EvidenceLevel.FRAMEWORK_REFERENCE,
            notes="fixtures are not normal-user runtime capability",
        ),
        CapabilityDeclaration(
            "provider.official_oracle",
            CapabilityMaturity.UNSUPPORTED,
            "M9-04",
            "Official oracle live execution route",
            EvidenceLevel.FRAMEWORK_REFERENCE,
            provider_family="official_minimax",
            requires_network=True,
            requires_consent=True,
            notes="current governance decision prohibits live execution",
        ),
        CapabilityDeclaration(
            "authoring.assisted_draft",
            CapabilityMaturity.DETERMINISTIC,
            "M22-05",
            "Deterministic draft audit and one bounded repair decision",
            EvidenceLevel.FRAMEWORK_REFERENCE,
            notes=(
                "model output is injected; adoption preserves references and user text and "
                "requires the deterministic reaudit to pass"
            ),
        ),
        CapabilityDeclaration(
            "provider.prompt_model_in_process_gguf",
            CapabilityMaturity.UNSUPPORTED,
            "M22-03",
            "In-process GGUF prompt-model execution family",
            EvidenceLevel.EXPERIMENTAL,
            provider_family="in_process_gguf",
            notes="declared contract only; no executable runtime or install profile ships",
        ),
        CapabilityDeclaration(
            "provider.prompt_model_loopback_server",
            CapabilityMaturity.INJECTED_ONLY,
            "M22-03",
            "Explicit OpenAI-compatible loopback prompt-model transport",
            EvidenceLevel.EXPERIMENTAL,
            provider_family="loopback_server",
            requires_network=True,
            notes="hermetic loopback stub evidence only; no real server or model is qualified",
        ),
        CapabilityDeclaration(
            "provider.prompt_model_ollama",
            CapabilityMaturity.INJECTED_ONLY,
            "M22-03",
            "Explicit loopback Ollama prompt-model transport",
            EvidenceLevel.EXPERIMENTAL,
            provider_family="ollama",
            requires_network=True,
            notes="hermetic loopback stub evidence only; no real daemon or model is qualified",
        ),
        CapabilityDeclaration(
            "provider.prompt_model_remote_openai_compatible",
            CapabilityMaturity.INJECTED_ONLY,
            "M22-04",
            "Consented remote OpenAI-compatible prompt-model transport",
            EvidenceLevel.EXPERIMENTAL,
            provider_family="remote_openai_compatible",
            requires_network=True,
            requires_consent=True,
            notes=(
                "curated profiles and hermetic transport evidence only; no third-party service "
                "is qualified"
            ),
        ),
        CapabilityDeclaration(
            "provider.prompt_model_remote_anthropic",
            CapabilityMaturity.INJECTED_ONLY,
            "M22-15",
            "Consented native Anthropic Models and Messages prompt-model transport",
            EvidenceLevel.EXPERIMENTAL,
            provider_family="remote_anthropic",
            requires_network=True,
            requires_consent=True,
            notes=(
                "curated catalog-only profile and hermetic native-dialect evidence; no live "
                "Anthropic service is qualified"
            ),
        ),
        CapabilityDeclaration(
            "provider.prompt_model_settings",
            CapabilityMaturity.DETERMINISTIC,
            "M22-06",
            "Provider selection disclosure consent and diagnostic state",
            EvidenceLevel.FRAMEWORK_REFERENCE,
            notes="session-only state; no profile is selected by default",
        ),
        CapabilityDeclaration(
            "provider.prompt_model_readiness",
            CapabilityMaturity.INJECTED_ONLY,
            "M22-08",
            "Explicit consent-gated remote prompt-model readiness observation",
            EvidenceLevel.EXPERIMENTAL,
            provider_family="remote_openai_compatible",
            requires_network=True,
            requires_consent=True,
            notes=(
                "one user-requested model-list observation through an injected transport; no "
                "live provider availability claim"
            ),
        ),
        CapabilityDeclaration(
            "adapter.comfyui_native_generation",
            CapabilityMaturity.INJECTED_ONLY,
            "M10-03",
            "Host-owned ComfyUI native TextGenerate generation path",
            EvidenceLevel.EXPERIMENTAL,
            provider_family="comfyui_native",
            notes=(
                "injected host CLIP and pinned call-chain seam; "
                "supported-host/model qualification remains missing"
            ),
        ),
        CapabilityDeclaration(
            "adapter.ollama_fallback",
            CapabilityMaturity.INJECTED_ONLY,
            "M10-03",
            "Explicit loopback Ollama fallback adapter",
            EvidenceLevel.EXPERIMENTAL,
            provider_family="ollama",
            requires_network=True,
            notes=(
                "explicit loopback transport/preflight seam; live server qualification "
                "and automatic fallback are absent"
            ),
        ),
        CapabilityDeclaration(
            "runtime.media_decode",
            CapabilityMaturity.INJECTED_ONLY,
            "M10-02",
            "Bounded canonical media admission and decode",
            EvidenceLevel.EXPERIMENTAL,
            notes=(
                "bounded admission/PTS and optional process seam; "
                "live decoder qualification remains missing"
            ),
        ),
        CapabilityDeclaration(
            "runtime.coordinator",
            CapabilityMaturity.INJECTED_ONLY,
            "M10-04",
            "Model/provider execution coordinator",
            EvidenceLevel.EXPERIMENTAL,
            notes=(
                "pure coordinator enforces explicit profiles, resource budgets, ownership, "
                "cache scope, and injected lifecycle cleanup; live host/provider qualification "
                "remains missing"
            ),
        ),
        CapabilityDeclaration(
            "host.sidebar_projection",
            CapabilityMaturity.UNSUPPORTED,
            "M10-01",
            "Optional sidebar projection over the visible graph",
            EvidenceLevel.FRAMEWORK_REFERENCE,
            notes="M10 defines the contract; M15 owns a future frontend bundle",
        ),
    )
    return CapabilityRegistry(node_declarations + additional)


def _cardinality(socket: NodeSocket) -> BindingCardinality:
    if socket.max_items > 1:
        return BindingCardinality.LIST if socket.required else BindingCardinality.OPTIONAL_LIST
    return BindingCardinality.SINGLE if socket.required else BindingCardinality.OPTIONAL_SINGLE


def _binding_sensitivity(
    socket: NodeSocket, node_id: str
) -> tuple[BindingSensitivity, BindingConsent, bool]:
    name = socket.name.casefold()
    if "credential" in name:
        return BindingSensitivity.CREDENTIAL_REFERENCE, BindingConsent.CREDENTIAL, True
    if socket.socket_type in {
        NodeSocketType.IMAGE,
        NodeSocketType.VIDEO,
        NodeSocketType.AUDIO,
        NodeSocketType.H3_CONTEXT_IR_MEDIA,
        NodeSocketType.H3_MEDIA_PRODUCER_RESULT,
        NodeSocketType.H3_VISUAL_PRODUCER_RESULT,
        NodeSocketType.H3_AUDIO_PRODUCER_RESULT,
    }:
        consent = (
            BindingConsent.REMOTE_MEDIA if "OfficialContextIR" in node_id else BindingConsent.NONE
        )
        return BindingSensitivity.MEDIA_RUNTIME, consent, True
    return BindingSensitivity.CANONICAL, BindingConsent.NONE, False


def _field_id(node_id: str, kind: BindingKind, name: str) -> str:
    value = f"h3.{_slug(node_id)}.{kind.value}.{name}"
    if _FIELD_ID.fullmatch(value) is None:
        raise CapabilityManifestError("derived binding field_id is invalid")
    return value


@dataclass(frozen=True, slots=True)
class PortBinding:
    """One stable node field/port projection entry."""

    field_id: str
    pipeline_role: str
    node_type: str
    node_id: str
    binding_kind: BindingKind
    port_name: str
    widget_name: str | None
    socket_type: str
    codec: str
    cardinality: BindingCardinality
    default: ScalarValue
    choices: tuple[str, ...]
    priority: BindingPriority
    visibility: BindingVisibility
    maturity: CapabilityMaturity
    sync_direction: BindingSyncDirection
    sensitivity: BindingSensitivity
    consent: BindingConsent
    runtime_only: bool
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        if _FIELD_ID.fullmatch(self.field_id) is None:
            raise CapabilityManifestError("binding field_id must be stable lower-case metadata")
        _code(self.pipeline_role, "binding pipeline_role")
        _identifier(self.node_type, "binding node_type")
        _identifier(self.node_id, "binding node_id")
        _enum(self.binding_kind, BindingKind, "binding kind")
        _code(self.port_name, "binding port_name")
        if self.widget_name is not None:
            _code(self.widget_name, "binding widget_name")
        _code(self.socket_type.casefold(), "binding socket_type")
        _code(self.codec.replace(".", "_"), "binding codec")
        _enum(self.cardinality, BindingCardinality, "binding cardinality")
        if not isinstance(self.choices, tuple) or len(self.choices) > 32:
            raise CapabilityManifestError("binding choices exceed the finite bound")
        if len(self.choices) != len(set(self.choices)):
            raise CapabilityManifestError("binding choices must be unique")
        for choice in self.choices:
            _text(choice, "binding choice", 256)
        _enum(self.priority, BindingPriority, "binding priority")
        _enum(self.visibility, BindingVisibility, "binding visibility")
        _enum(self.maturity, CapabilityMaturity, "binding maturity")
        _enum(self.sync_direction, BindingSyncDirection, "binding sync_direction")
        _enum(self.sensitivity, BindingSensitivity, "binding sensitivity")
        _enum(self.consent, BindingConsent, "binding consent")
        if not isinstance(self.runtime_only, bool):
            raise CapabilityManifestError("binding runtime_only must be a boolean")
        if self.schema_version != "1.0":
            raise CapabilityManifestError("unsupported binding schema version")
        if self.sensitivity is BindingSensitivity.CREDENTIAL_REFERENCE and not self.runtime_only:
            raise CapabilityManifestError("credential reference bindings must be runtime-only")

    def to_wire(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "pipeline_role": self.pipeline_role,
            "node_type": self.node_type,
            "node_id": self.node_id,
            "binding_kind": self.binding_kind.value,
            "port_name": self.port_name,
            "widget_name": self.widget_name,
            "socket_type": self.socket_type,
            "codec": self.codec,
            "cardinality": self.cardinality.value,
            "default": self.default,
            "choices": list(self.choices),
            "priority": self.priority.value,
            "visibility": self.visibility.value,
            "maturity": self.maturity.value,
            "sync_direction": self.sync_direction.value,
            "sensitivity": self.sensitivity.value,
            "consent": self.consent.value,
            "runtime_only": self.runtime_only,
            "schema_version": self.schema_version,
        }


def _binding_for(node: NodeContract, socket: NodeSocket, kind: BindingKind) -> PortBinding:
    short_name = node.node_id.rsplit(".", 1)[-1]
    maturity = _NODE_MATURITY.get(short_name, CapabilityMaturity.UNSUPPORTED)
    sensitivity, consent, runtime_only = _binding_sensitivity(socket, node.node_id)
    if kind is BindingKind.INPUT:
        sync = BindingSyncDirection.TO_CORE
        priority = BindingPriority.CORE if socket.required else BindingPriority.SUPPORTING
        visibility = (
            BindingVisibility.ADVANCED
            if sensitivity is not BindingSensitivity.CANONICAL
            else BindingVisibility.PUBLIC
        )
        widget_name = socket.name
    else:
        sync = BindingSyncDirection.FROM_CORE
        priority = BindingPriority.CORE
        visibility = BindingVisibility.PUBLIC
        widget_name = None
    return PortBinding(
        field_id=_field_id(node.node_id, kind, socket.name),
        pipeline_role=node.category.rsplit("/", 1)[-1],
        node_type=node.node_id,
        node_id=node.node_id,
        binding_kind=kind,
        port_name=socket.name,
        widget_name=widget_name,
        socket_type=socket.socket_type.value,
        codec=f"comfyui.v1.{socket.socket_type.value.casefold()}",
        cardinality=_cardinality(socket),
        default=socket.default,
        choices=socket.choices,
        priority=priority,
        visibility=visibility,
        maturity=maturity,
        sync_direction=sync,
        sensitivity=sensitivity,
        consent=consent,
        runtime_only=runtime_only,
    )


@dataclass(frozen=True, slots=True)
class BindingManifest:
    """Deterministic backend-owned mapping for all public registrations and ports."""

    node_ids: tuple[str, ...]
    display_names: tuple[tuple[str, str], ...]
    capabilities: CapabilityRegistry
    bindings: tuple[PortBinding, ...]
    schema: str = CAPABILITY_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CAPABILITY_MANIFEST_SCHEMA:
            raise CapabilityManifestError("unsupported binding manifest schema")
        _unique_strings(self.node_ids, "manifest node_ids", 64)
        if not isinstance(self.display_names, tuple) or len(self.display_names) != len(
            self.node_ids
        ):
            raise CapabilityManifestError("manifest display_names must cover every node")
        display_ids: list[str] = []
        for node_id, display_name in self.display_names:
            _identifier(node_id, "manifest display node_id")
            _text(display_name, "manifest display_name", 256)
            display_ids.append(node_id)
        if set(display_ids) != set(self.node_ids):
            raise CapabilityManifestError("manifest display names drift from node IDs")
        if not isinstance(self.capabilities, CapabilityRegistry):
            raise CapabilityManifestError("manifest capabilities must be a CapabilityRegistry")
        if not isinstance(self.bindings, tuple) or len(self.bindings) > MAX_BINDINGS:
            raise CapabilityManifestError("manifest bindings exceed the finite bound")
        if not all(isinstance(item, PortBinding) for item in self.bindings):
            raise CapabilityManifestError("manifest bindings contain an invalid value")
        field_ids = tuple(item.field_id for item in self.bindings)
        if len(field_ids) != len(set(field_ids)):
            raise CapabilityManifestError("manifest field IDs must be unique")
        if not all(item.node_id in self.node_ids for item in self.bindings):
            raise CapabilityManifestError("manifest binding references an unknown node")
        expected_caps = {_capability_for_node(node_id) for node_id in self.node_ids}
        actual_caps = {item.capability_id for item in self.capabilities.declarations}
        if not expected_caps.issubset(actual_caps):
            raise CapabilityManifestError("manifest omits a public node capability")

    @property
    def fingerprint(self) -> str:
        payload = self.to_wire(include_fingerprint=False)
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": self.schema,
            "node_ids": list(self.node_ids),
            "display_names": [list(item) for item in self.display_names],
            "capabilities": self.capabilities.to_wire(),
            "bindings": [item.to_wire() for item in self.bindings],
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload


PUBLIC_NODE_IDS = (
    "comfyui_h3_context.H3Context.Request",
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    "comfyui_h3_context.H3Context.Plan",
    "comfyui_h3_context.H3Context.Compiler",
    "comfyui_h3_context.H3Context.FullReference",
    "comfyui_h3_context.H3Context.Validator",
    "comfyui_h3_context.H3Context.Preview",
    "comfyui_h3_context.H3Context.AuditOverride",
    "comfyui_h3_context.H3Context.ProviderTransparency",
    "comfyui_h3_context.H3Context.Reliability",
    "comfyui_h3_context.H3Context.NativeH3Adapter",
    "comfyui_h3_context.H3Context.ProductShell",
    "comfyui_h3_context.H3Context.SemanticProposalProducer",
    "comfyui_h3_context.H3Context.OfficialContextIR",
    "comfyui_h3_context.H3Context.MediaAdmissionProducer",
    "comfyui_h3_context.H3Context.VisualPerceptionProducer",
    "comfyui_h3_context.H3Context.AudioPerceptionProducer",
    "comfyui_h3_context.H3Context.HardConstraintProducer",
    "comfyui_h3_context.H3Context.IntentGraphProducer",
    "comfyui_h3_context.H3Context.EvidenceFusionProducer",
    "comfyui_h3_context.H3Context.CrossReferenceProducer",
    "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
    "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
    "comfyui_h3_context.H3Context.FeasibleAVTimeline",
    "comfyui_h3_context.H3Context.HierarchicalEvidenceReduction",
    "comfyui_h3_context.H3Context.ConstrainedSemanticPlanning",
    "comfyui_h3_context.H3Context.SourceProfiledRenderer",
    "comfyui_h3_context.H3Context.LocalReconstruction",
)
PUBLIC_NODE_DISPLAY_NAMES = (
    "H3 Context Request",
    "H3 Reference Registry",
    "H3 Context Plan",
    "H3 Context Compiler",
    "H3 Full-Reference Plan",
    "H3 Context Validator",
    "H3 Context Preview",
    "H3 Context Audit Override",
    "H3 Provider Transparency",
    "H3 Reliability Status",
    "H3 Native MiniMax H3 Adapter",
    "H3 Product Shell Boundary",
    "H3 Semantic Proposal Producer",
    "H3 Official Context-IR",
    "H3 Media Admission Producer",
    "H3 Visual Perception Producer",
    "H3 Audio Perception Producer",
    "H3 Hard Constraint Producer",
    "H3 Intent Graph Producer",
    "H3 Evidence Fusion Producer",
    "H3 Cross Reference Producer",
    "H3 Directive Authority Producer",
    "H3 Full Reference Timeline Producer",
    "H3 Feasible AV Timeline",
    "H3 Hierarchical Evidence Reduction",
    "H3 Constrained Semantic Planning",
    "H3 Source-Profiled Renderer",
    "H3 Local Reconstruction Acceptance",
)


def build_default_binding_manifest(
    *,
    registry: NodeContractRegistry | None = None,
    registration_ids: tuple[str, ...] = PUBLIC_NODE_IDS,
    display_names: Mapping[str, str] | None = None,
) -> BindingManifest:
    """Build the one manifest and reject registry/registration drift before returning it."""

    selected_registry = default_node_contract_registry() if registry is None else registry
    if not isinstance(selected_registry, NodeContractRegistry):
        raise CapabilityManifestError("registry must be a NodeContractRegistry")
    ids = _unique_strings(registration_ids, "registration_ids", 64)
    contract_ids = tuple(item.node_id for item in selected_registry.definitions)
    if set(ids) != set(contract_ids) or len(ids) != len(contract_ids):
        raise CapabilityManifestError("registration IDs and declarative contracts are out of sync")
    names = (
        dict(zip(PUBLIC_NODE_IDS, PUBLIC_NODE_DISPLAY_NAMES, strict=True))
        if display_names is None
        else dict(display_names)
    )
    if set(names) != set(ids):
        raise CapabilityManifestError("display names do not cover the public registrations")
    ordered_nodes = tuple(item.node_id for item in selected_registry.definitions)
    bindings = tuple(
        binding
        for node in selected_registry.definitions
        for binding in tuple(
            _binding_for(node, socket, BindingKind.INPUT) for socket in node.inputs
        )
        + tuple(_binding_for(node, socket, BindingKind.OUTPUT) for socket in node.outputs)
    )
    capabilities = default_capability_registry()
    return BindingManifest(
        node_ids=ordered_nodes,
        display_names=tuple((node_id, names[node_id]) for node_id in ordered_nodes),
        capabilities=capabilities,
        bindings=bindings,
    )


@dataclass(frozen=True, slots=True)
class HostCanaryFinding:
    seam: HostCanarySeam
    outcome: HostCanaryOutcome
    evidence_code: str
    detail: str

    def __post_init__(self) -> None:
        _enum(self.seam, HostCanarySeam, "canary seam")
        _enum(self.outcome, HostCanaryOutcome, "canary outcome")
        _code(self.evidence_code, "canary evidence_code")
        _text(self.detail, "canary detail", 512)

    def to_wire(self) -> dict[str, str]:
        return {
            "seam": self.seam.value,
            "outcome": self.outcome.value,
            "evidence_code": self.evidence_code,
            "detail": self.detail,
        }


def _overall_canary_outcome(findings: tuple[HostCanaryFinding, ...]) -> HostCanaryOutcome:
    outcomes = {item.outcome for item in findings}
    if HostCanaryOutcome.BLOCKED in outcomes:
        return HostCanaryOutcome.BLOCKED
    if HostCanaryOutcome.NOT_RUN in outcomes:
        return HostCanaryOutcome.NOT_RUN
    if HostCanaryOutcome.UNSUPPORTED in outcomes:
        return HostCanaryOutcome.UNSUPPORTED
    if HostCanaryOutcome.DEGRADED in outcomes:
        return HostCanaryOutcome.DEGRADED
    return HostCanaryOutcome.SUPPORTED


@dataclass(frozen=True, slots=True)
class HostCanaryReport:
    host_profile: str
    status: HostCanaryOutcome
    findings: tuple[HostCanaryFinding, ...]
    schema: str = HOST_CANARY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != HOST_CANARY_SCHEMA:
            raise HostCanaryError("unsupported host canary schema")
        _text(self.host_profile, "host_profile", 256)
        _enum(self.status, HostCanaryOutcome, "canary report status")
        if not isinstance(self.findings, tuple) or len(self.findings) != len(tuple(HostCanarySeam)):
            raise HostCanaryError("canary report must contain one finding per seam")
        if len(self.findings) > MAX_CANARY_FINDINGS:
            raise HostCanaryError("canary findings exceed the finite bound")
        seams = tuple(item.seam for item in self.findings)
        if len(set(seams)) != len(seams) or set(seams) != set(HostCanarySeam):
            raise HostCanaryError("canary findings must cover each seam exactly once")
        if self.status is not _overall_canary_outcome(self.findings):
            raise HostCanaryError("canary report status does not match findings")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_wire(include_fingerprint=False), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": self.schema,
            "host_profile": self.host_profile,
            "status": self.status.value,
            "findings": [item.to_wire() for item in self.findings],
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload


def build_host_canary_report(
    host_profile: str,
    findings: Mapping[HostCanarySeam, tuple[HostCanaryOutcome, str]],
) -> HostCanaryReport:
    """Build an explicit canary report from injected public-seam observations."""

    if set(findings) != set(HostCanarySeam):
        raise HostCanaryError("canary findings must cover all documented seams")
    values = []
    for seam in HostCanarySeam:
        outcome, detail = findings[seam]
        values.append(HostCanaryFinding(seam, outcome, "host_seam_observation", detail))
    typed = tuple(values)
    return HostCanaryReport(host_profile, _overall_canary_outcome(typed), typed)


__all__ = [
    "CAPABILITY_REGISTRY_SCHEMA",
    "CAPABILITY_MANIFEST_SCHEMA",
    "HOST_CANARY_SCHEMA",
    "CapabilityMaturity",
    "BindingKind",
    "BindingCardinality",
    "BindingPriority",
    "BindingVisibility",
    "BindingSyncDirection",
    "BindingSensitivity",
    "BindingConsent",
    "HostCanarySeam",
    "HostCanaryOutcome",
    "CapabilityDeclaration",
    "CapabilityRegistry",
    "default_capability_registry",
    "PortBinding",
    "BindingManifest",
    "PUBLIC_NODE_IDS",
    "PUBLIC_NODE_DISPLAY_NAMES",
    "build_default_binding_manifest",
    "HostCanaryFinding",
    "HostCanaryReport",
    "build_host_canary_report",
]
