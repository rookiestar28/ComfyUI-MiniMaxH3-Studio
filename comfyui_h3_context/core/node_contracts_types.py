"""What a node contract is: the types, the bounds and the refusals that define one.

Sockets, contracts, the registry that holds them, and the host capability declaration they are
checked against. Every constructor validates on the way in -- a socket name that does not match the
pattern, a duplicate identifier, a choice list past its ceiling or metadata carrying an unsafe
marker is refused at construction rather than stored and checked later.

This layer knows nothing about which contracts this package actually declares. That is the next
one, and keeping the two apart is the point of the split: extending the vocabulary is a change to
370 lines, not to 2170.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from .errors import NodeContractError

NODE_CONTRACT_SCHEMA = "h3-node-contract/1"


NODE_NAMESPACE = "comfyui_h3_context."


MAX_NODE_DEFINITIONS = 64


MAX_NODE_SOCKETS = 64


MAX_SOCKET_CHOICES = 32


_NODE_ID_PATTERN = re.compile(r"comfyui_h3_context\.[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")


_SOCKET_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


_CATEGORY_PATTERN = re.compile(r"[a-z][a-z0-9_.-]*(?:/[a-z][a-z0-9_.-]*){0,3}\Z")


_STAGE_PATTERN = re.compile(r"M[0-9]+-[0-9]{2}\Z")


_SAFE_METADATA_MARKERS = (
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


ScalarDefault = str | int | float | bool | None


class NodeSocketType(str, Enum):
    """Host-neutral socket vocabulary mapped by a later ComfyUI adapter."""

    STRING = "STRING"
    INT = "INT"
    FLOAT = "FLOAT"
    BOOLEAN = "BOOLEAN"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    H3_CONTEXT_REQUEST = "H3_CONTEXT_REQUEST"
    H3_HARD_CONSTRAINTS = "H3_HARD_CONSTRAINTS"
    H3_REFERENCE_REGISTRY = "H3_REFERENCE_REGISTRY"
    H3_INTENT_GRAPH = "H3_INTENT_GRAPH"
    H3_FULL_REFERENCE_TIMELINE = "H3_FULL_REFERENCE_TIMELINE"
    H3_CONTEXT_PLAN = "H3_CONTEXT_PLAN"
    H3_PROMPT_DOCUMENT = "H3_PROMPT_DOCUMENT"
    H3_PROMPT_STRING = "H3_PROMPT_STRING"
    H3_CONTEXT_REPORT = "H3_CONTEXT_REPORT"
    H3_CONTEXT_PREVIEW = "H3_CONTEXT_PREVIEW"
    H3_AUDIT_OVERRIDE = "H3_AUDIT_OVERRIDE"
    H3_PROVIDER_TRANSPARENCY = "H3_PROVIDER_TRANSPARENCY"
    H3_PROVIDER_CONSENT = "H3_PROVIDER_CONSENT"
    H3_PROVIDER_SETUP = "H3_PROVIDER_SETUP"
    H3_PROVIDER_RECEIPT = "H3_PROVIDER_RECEIPT"
    H3_CONTEXT_IR_MEDIA = "H3_CONTEXT_IR_MEDIA"
    H3_EXECUTION_STATUS = "H3_EXECUTION_STATUS"
    H3_PROGRESS_EVENT = "H3_PROGRESS_EVENT"
    H3_RECOVERY_DECISION = "H3_RECOVERY_DECISION"
    H3_VALIDATION_RESULT = "H3_VALIDATION_RESULT"
    H3_NATIVE_H3_WIRING = "H3_NATIVE_H3_WIRING"
    H3_MEDIA_PRODUCER_RESULT = "H3_MEDIA_PRODUCER_RESULT"
    H3_VISUAL_PRODUCER_RESULT = "H3_VISUAL_PRODUCER_RESULT"
    H3_AUDIO_PRODUCER_RESULT = "H3_AUDIO_PRODUCER_RESULT"
    H3_UNIFIED_EVIDENCE_GRAPH = "H3_UNIFIED_EVIDENCE_GRAPH"
    H3_CROSS_REFERENCE_GRAPH = "H3_CROSS_REFERENCE_GRAPH"
    H3_DIRECTIVE_AUTHORITY = "H3_DIRECTIVE_AUTHORITY"
    H3_DOWNSTREAM_PRODUCER_REPORT = "H3_DOWNSTREAM_PRODUCER_REPORT"
    H3_SOURCE_PROFILED_PROMPT = "H3_SOURCE_PROFILED_PROMPT"
    H3_LOCAL_RECONSTRUCTION = "H3_LOCAL_RECONSTRUCTION"
    H3_FEASIBLE_AV_TIMELINE = "H3_FEASIBLE_AV_TIMELINE"
    H3_HIERARCHICAL_EVIDENCE_REDUCTION = "H3_HIERARCHICAL_EVIDENCE_REDUCTION"
    H3_CONSTRAINED_SEMANTIC_PLANNING = "H3_CONSTRAINED_SEMANTIC_PLANNING"
    H3_PRODUCT_SHELL = "H3_PRODUCT_SHELL"
    H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY = "H3_SEMANTIC_PROPOSAL_REVIEW_AUTHORITY"
    H3_RECOMPUTE_PLAN = "H3_RECOMPUTE_PLAN"
    H3_PIPELINE_TRANSACTION = "H3_PIPELINE_TRANSACTION"
    H3_GENERATION_SEQUENCE_STATE = "H3_GENERATION_SEQUENCE_STATE"


class HostApiFamily(str, Enum):
    """ComfyUI host API families recognized by this declarative contract."""

    V1 = "v1"
    V3 = "v3"


def _require_metadata(value: object, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise NodeContractError(f"{field} must be a bounded non-empty string")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SAFE_METADATA_MARKERS):
        raise NodeContractError(f"{field} contains sensitive or remote metadata")
    if "\\" in value or "\x00" in value:
        raise NodeContractError(f"{field} contains an unsafe character")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise NodeContractError(f"{field} contains a control character")
    return value


def _require_unique(values: tuple[object, ...], field: str) -> None:
    if len(values) != len(set(values)):
        raise NodeContractError(f"{field} must not contain duplicates")


@dataclass(frozen=True, slots=True, order=True)
class HostVersion:
    """Comparable semantic host version used only for explicit compatibility checks."""

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.major, "host major"),
            (self.minor, "host minor"),
            (self.patch, "host patch"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999:
                raise NodeContractError(f"{field} must be between 0 and 999")

    @classmethod
    def from_wire(cls, value: str) -> HostVersion:
        if not isinstance(value, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\Z", value) is None:
            raise NodeContractError("host version must use major.minor.patch")
        parts = tuple(int(part) for part in value.split("."))
        return cls(*parts)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True)
class HostCapabilities:
    """Explicit host facts supplied by an adapter; no host probing occurs here."""

    api_family: HostApiFamily
    version: HostVersion
    socket_types: tuple[NodeSocketType, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.api_family, HostApiFamily):
            raise NodeContractError("host api_family must be a HostApiFamily")
        if not isinstance(self.version, HostVersion):
            raise NodeContractError("host version must be a HostVersion")
        if not isinstance(self.socket_types, tuple) or len(self.socket_types) > MAX_NODE_SOCKETS:
            raise NodeContractError("host socket_types exceed the bounded limit")
        if not all(isinstance(value, NodeSocketType) for value in self.socket_types):
            raise NodeContractError("host socket_types contain an invalid value")
        _require_unique(self.socket_types, "host socket_types")


@dataclass(frozen=True, slots=True)
class NodeSocket:
    """One typed node input/output declaration with explicit optionality and cardinality."""

    name: str
    socket_type: NodeSocketType
    required: bool
    default: ScalarDefault = None
    min_items: int = 1
    max_items: int = 1
    choices: tuple[str, ...] = ()
    description: str = "socket"

    def __post_init__(self) -> None:
        if _SOCKET_NAME_PATTERN.fullmatch(self.name) is None:
            raise NodeContractError("socket name must be a lower-case identifier")
        if not isinstance(self.socket_type, NodeSocketType):
            raise NodeContractError("socket_type must be a NodeSocketType")
        if not isinstance(self.required, bool):
            raise NodeContractError("socket required must be a bool")
        for value, field in (
            (self.min_items, "socket min_items"),
            (self.max_items, "socket max_items"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 256:
                raise NodeContractError(f"{field} must be between 0 and 256")
        if self.max_items < self.min_items:
            raise NodeContractError("socket max_items must not precede min_items")
        if self.required and self.min_items < 1:
            raise NodeContractError("required sockets must have min_items >= 1")
        if not self.required and self.min_items != 0:
            raise NodeContractError("optional sockets must have min_items == 0")
        if not isinstance(self.choices, tuple) or len(self.choices) > MAX_SOCKET_CHOICES:
            raise NodeContractError("socket choices exceed the bounded limit")
        _require_unique(self.choices, "socket choices")
        for choice in self.choices:
            _require_metadata(choice, "socket choice", 256)
        _require_metadata(self.description, "socket description")
        self._validate_default()

    def _validate_default(self) -> None:
        if self.default is None:
            return
        if self.socket_type is NodeSocketType.BOOLEAN and not isinstance(self.default, bool):
            raise NodeContractError("BOOLEAN socket defaults must be bool")
        if self.socket_type is NodeSocketType.INT and (
            isinstance(self.default, bool) or not isinstance(self.default, int)
        ):
            raise NodeContractError("INT socket defaults must be int")
        if self.socket_type is NodeSocketType.FLOAT and (
            isinstance(self.default, bool) or not isinstance(self.default, (int, float))
        ):
            raise NodeContractError("FLOAT socket defaults must be numeric")
        if self.socket_type is NodeSocketType.STRING and not isinstance(self.default, str):
            raise NodeContractError("STRING socket defaults must be str")
        if self.socket_type not in {
            NodeSocketType.STRING,
            NodeSocketType.INT,
            NodeSocketType.FLOAT,
            NodeSocketType.BOOLEAN,
        }:
            raise NodeContractError("typed object/media sockets cannot declare scalar defaults")
        if isinstance(self.default, str):
            _require_metadata(self.default, "socket default", 256)
        if self.choices and self.default not in self.choices:
            raise NodeContractError("socket default must be one of its choices")

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "socket_type": self.socket_type.value,
            "required": self.required,
            "default": self.default,
            "min_items": self.min_items,
            "max_items": self.max_items,
            "choices": list(self.choices),
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class NodeContract:
    """Declarative node identity and socket surface; it has no executable implementation."""

    node_id: str
    display_name: str
    category: str
    inputs: tuple[NodeSocket, ...]
    outputs: tuple[NodeSocket, ...]
    host_api: HostApiFamily
    minimum_host_version: HostVersion
    migration_note: str
    roadmap_stage: str
    executable: bool = False

    def __post_init__(self) -> None:
        if _NODE_ID_PATTERN.fullmatch(self.node_id) is None:
            raise NodeContractError("node_id must use the project namespace")
        _require_metadata(self.display_name, "node display_name", 256)
        if _CATEGORY_PATTERN.fullmatch(self.category) is None:
            raise NodeContractError("node category must use a bounded category path")
        if not isinstance(self.inputs, tuple) or len(self.inputs) > MAX_NODE_SOCKETS:
            raise NodeContractError("node inputs exceed the bounded limit")
        if not isinstance(self.outputs, tuple) or len(self.outputs) > MAX_NODE_SOCKETS:
            raise NodeContractError("node outputs exceed the bounded limit")
        if not all(isinstance(value, NodeSocket) for value in self.inputs + self.outputs):
            raise NodeContractError("node sockets contain an invalid value")
        names = tuple(value.name for value in self.inputs + self.outputs)
        _require_unique(names, "node socket names")
        if not isinstance(self.host_api, HostApiFamily):
            raise NodeContractError("node host_api must be a HostApiFamily")
        if not isinstance(self.minimum_host_version, HostVersion):
            raise NodeContractError("node minimum_host_version must be a HostVersion")
        _require_metadata(self.migration_note, "node migration_note")
        if _STAGE_PATTERN.fullmatch(self.roadmap_stage) is None:
            raise NodeContractError("node roadmap_stage must use the form Mx-yy")
        if not isinstance(self.executable, bool):
            raise NodeContractError("node executable must be a bool")

    @property
    def required_socket_types(self) -> frozenset[NodeSocketType]:
        return frozenset(value.socket_type for value in self.inputs + self.outputs)

    def to_wire(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "category": self.category,
            "inputs": [value.to_wire() for value in self.inputs],
            "outputs": [value.to_wire() for value in self.outputs],
            "host_api": self.host_api.value,
            "minimum_host_version": str(self.minimum_host_version),
            "migration_note": self.migration_note,
            "roadmap_stage": self.roadmap_stage,
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class NodeContractRegistry:
    """Immutable node contract set with explicit lookup/collision/host checks."""

    definitions: tuple[NodeContract, ...]
    schema: str = NODE_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != NODE_CONTRACT_SCHEMA:
            raise NodeContractError("unsupported node contract schema")
        if not isinstance(self.definitions, tuple) or len(self.definitions) > MAX_NODE_DEFINITIONS:
            raise NodeContractError("node definitions exceed the bounded limit")
        if not all(isinstance(value, NodeContract) for value in self.definitions):
            raise NodeContractError("node definitions contain an invalid value")
        _require_unique(tuple(value.node_id for value in self.definitions), "node IDs")

    def get(self, node_id: str) -> NodeContract:
        for definition in self.definitions:
            if definition.node_id == node_id:
                return definition
        raise NodeContractError(f"unknown node contract: {node_id!r}")

    def with_definition(self, definition: NodeContract) -> NodeContractRegistry:
        if not isinstance(definition, NodeContract):
            raise NodeContractError("registered value must be a NodeContract")
        if any(value.node_id == definition.node_id for value in self.definitions):
            raise NodeContractError(f"duplicate node ID: {definition.node_id!r}")
        return NodeContractRegistry(self.definitions + (definition,), self.schema)

    def check_foreign_collisions(self, existing_ids: Iterable[str]) -> None:
        try:
            foreign = frozenset(existing_ids)
        except TypeError as exc:
            raise NodeContractError("existing node IDs must be iterable") from exc
        if not all(isinstance(value, str) for value in foreign):
            raise NodeContractError("existing node IDs must be strings")
        collisions = sorted(foreign.intersection(value.node_id for value in self.definitions))
        if collisions:
            raise NodeContractError(f"foreign node ID collision: {collisions[0]!r}")

    def assert_host_compatible(self, capabilities: HostCapabilities) -> None:
        if not isinstance(capabilities, HostCapabilities):
            raise NodeContractError("host compatibility requires HostCapabilities")
        for definition in self.definitions:
            if capabilities.api_family is not definition.host_api:
                raise NodeContractError(
                    f"node {definition.node_id!r} requires host API {definition.host_api.value}"
                )
            if capabilities.version < definition.minimum_host_version:
                raise NodeContractError(
                    f"node {definition.node_id!r} requires host version "
                    f"{definition.minimum_host_version}"
                )
            missing = definition.required_socket_types.difference(capabilities.socket_types)
            if missing:
                missing_name = sorted(value.value for value in missing)[0]
                raise NodeContractError(
                    f"host does not provide required socket type {missing_name!r}"
                )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "nodes": [value.to_wire() for value in self.definitions],
        }
