"""Pure manifest-to-visible-graph binding and explicit role-anchor validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from .contracts import TaskMode
from .errors import GraphBindingError
from .node_contracts import NodeContract
from .public_manifest import PublicManifest, WorkflowFixtureRef

GRAPH_BINDING_SCHEMA = "h3.context.graph.binding.v1"
MAX_GRAPH_NODES = 128
MAX_GRAPH_EDGES = 256
MAX_GRAPH_ANCHORS = 32
MAX_GRAPH_DEPTH = 4
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_PORT = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class GraphAnchorRole(str, Enum):
    """Explicit role classes that a graph may expose to a controller/sidebar."""

    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    PAIRED_VIDEO_AUDIO = "paired_video_audio"
    REFERENCE = "reference"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise GraphBindingError(f"{field} must be a bounded identifier")
    return value


def _port(value: object, field: str) -> str:
    if not isinstance(value, str) or _PORT.fullmatch(value) is None:
        raise GraphBindingError(f"{field} must be a lower-case socket name")
    return value


def _text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise GraphBindingError(f"{field} must be a bounded non-empty string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise GraphBindingError(f"{field} contains a control character")
    return value


@dataclass(frozen=True, slots=True)
class GraphAnchor:
    """One explicit role producer in a visible graph."""

    role: GraphAnchorRole
    node_instance_id: str
    socket_name: str
    connection_order: int

    def __post_init__(self) -> None:
        if not isinstance(self.role, GraphAnchorRole):
            raise GraphBindingError("anchor role must be a GraphAnchorRole")
        _identifier(self.node_instance_id, "anchor node_instance_id")
        _port(self.socket_name, "anchor socket_name")
        if isinstance(self.connection_order, bool) or not isinstance(self.connection_order, int):
            raise GraphBindingError("anchor connection_order must be an integer")
        if not 1 <= self.connection_order <= MAX_GRAPH_NODES:
            raise GraphBindingError("anchor connection_order exceeds the bounded graph limit")

    def to_wire(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "node_instance_id": self.node_instance_id,
            "socket_name": self.socket_name,
            "connection_order": self.connection_order,
        }


@dataclass(frozen=True, slots=True)
class GraphNode:
    """Safe node-instance projection retaining only contract ports and stable IDs."""

    node_instance_id: str
    node_id: str
    input_ports: tuple[str, ...]
    output_ports: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.node_instance_id, "node_instance_id")
        _identifier(self.node_id, "node_id")
        for field, values in (
            ("input_ports", self.input_ports),
            ("output_ports", self.output_ports),
        ):
            if not isinstance(values, tuple) or len(values) > 32:
                raise GraphBindingError(f"{field} must be a bounded tuple")
            if not all(
                isinstance(value, str) and _PORT.fullmatch(value) is not None for value in values
            ):
                raise GraphBindingError(f"{field} contain an invalid socket name")
            if len(values) != len(set(values)):
                raise GraphBindingError(f"{field} must not contain duplicates")

    @classmethod
    def from_contract(cls, instance_id: str, contract: NodeContract) -> GraphNode:
        if not isinstance(contract, NodeContract):
            raise GraphBindingError("graph node construction requires a NodeContract")
        return cls(
            node_instance_id=instance_id,
            node_id=contract.node_id,
            input_ports=tuple(item.name for item in contract.inputs),
            output_ports=tuple(item.name for item in contract.outputs),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "node_instance_id": self.node_instance_id,
            "node_id": self.node_id,
            "input_ports": list(self.input_ports),
            "output_ports": list(self.output_ports),
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """Typed visible connection between two graph node instances."""

    source_instance_id: str
    source_port: str
    target_instance_id: str
    target_port: str

    def __post_init__(self) -> None:
        _identifier(self.source_instance_id, "edge source_instance_id")
        _port(self.source_port, "edge source_port")
        _identifier(self.target_instance_id, "edge target_instance_id")
        _port(self.target_port, "edge target_port")

    def to_wire(self) -> dict[str, str]:
        return {
            "source_instance_id": self.source_instance_id,
            "source_port": self.source_port,
            "target_instance_id": self.target_instance_id,
            "target_port": self.target_port,
        }


@dataclass(frozen=True, slots=True)
class VisibleGraph:
    """Immutable visible graph, optionally containing transparent child Subgraphs."""

    fixture_id: str
    task_modes: tuple[TaskMode, ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...] = ()
    anchors: tuple[GraphAnchor, ...] = ()
    children: tuple[VisibleGraph, ...] = ()
    transparent: bool = False
    schema: str = GRAPH_BINDING_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != GRAPH_BINDING_SCHEMA:
            raise GraphBindingError("unsupported graph binding schema")
        _identifier(self.fixture_id, "graph fixture_id")
        if not isinstance(self.task_modes, tuple) or not self.task_modes:
            raise GraphBindingError("graph task_modes must be a non-empty tuple")
        if not all(isinstance(value, TaskMode) for value in self.task_modes):
            raise GraphBindingError("graph task_modes contain an invalid value")
        if not isinstance(self.nodes, tuple) or not self.nodes:
            raise GraphBindingError("graph nodes must be a non-empty tuple")
        if len(self.nodes) > MAX_GRAPH_NODES or not all(
            isinstance(value, GraphNode) for value in self.nodes
        ):
            raise GraphBindingError("graph nodes exceed the bounded limit")
        node_instances = tuple(value.node_instance_id for value in self.nodes)
        if len(node_instances) != len(set(node_instances)):
            raise GraphBindingError("graph node instance IDs must be unique")
        if (
            not isinstance(self.edges, tuple)
            or len(self.edges) > MAX_GRAPH_EDGES
            or not all(isinstance(value, GraphEdge) for value in self.edges)
        ):
            raise GraphBindingError("graph edges exceed the bounded limit")
        known_instances = set(node_instances)
        for edge in self.edges:
            if (
                edge.source_instance_id not in known_instances
                or edge.target_instance_id not in known_instances
            ):
                raise GraphBindingError("graph edge references an unknown node instance")
            source = next(
                item for item in self.nodes if item.node_instance_id == edge.source_instance_id
            )
            target = next(
                item for item in self.nodes if item.node_instance_id == edge.target_instance_id
            )
            if (
                edge.source_port not in source.output_ports
                or edge.target_port not in target.input_ports
            ):
                raise GraphBindingError("graph edge references an undeclared port")
        if (
            not isinstance(self.anchors, tuple)
            or len(self.anchors) > MAX_GRAPH_ANCHORS
            or not all(isinstance(value, GraphAnchor) for value in self.anchors)
        ):
            raise GraphBindingError("graph anchors exceed the bounded limit")
        for anchor in self.anchors:
            if anchor.node_instance_id not in known_instances:
                raise GraphBindingError("graph anchor references an unknown node instance")
            node = next(
                item for item in self.nodes if item.node_instance_id == anchor.node_instance_id
            )
            if (
                anchor.socket_name not in node.input_ports
                and anchor.socket_name not in node.output_ports
            ):
                raise GraphBindingError("graph anchor references an undeclared port")
        if (
            not isinstance(self.children, tuple)
            or len(self.children) > 16
            or not all(isinstance(value, VisibleGraph) for value in self.children)
        ):
            raise GraphBindingError("graph children exceed the bounded limit")
        if self.children and not self.transparent:
            raise GraphBindingError("non-transparent graphs cannot contain child Subgraphs")
        if not isinstance(self.transparent, bool):
            raise GraphBindingError("graph transparent must be a boolean")
        if self.depth > MAX_GRAPH_DEPTH:
            raise GraphBindingError("graph nesting exceeds the bounded depth")
        flattened = self.flatten_nodes()
        flattened_ids = tuple(item.node_instance_id for item in flattened)
        if len(flattened_ids) != len(set(flattened_ids)):
            raise GraphBindingError("transparent graph expansion contains duplicate node instances")

    @property
    def depth(self) -> int:
        return 1 if not self.children else 1 + max(value.depth for value in self.children)

    def flatten_nodes(self) -> tuple[GraphNode, ...]:
        values = list(self.nodes)
        for child in self.children:
            values.extend(child.flatten_nodes())
        return tuple(values)

    def resolve_anchors(
        self, required_roles: tuple[GraphAnchorRole, ...] = ()
    ) -> tuple[GraphAnchor, ...]:
        """Return one producer per requested role or fail closed on missing/duplicate/ambiguous."""

        available = self.anchors
        for child in self.children:
            available += child.anchors
        result: list[GraphAnchor] = []
        for role in required_roles:
            if not isinstance(role, GraphAnchorRole):
                raise GraphBindingError("required anchor role is invalid")
            matches = tuple(item for item in available if item.role is role)
            if not matches:
                raise GraphBindingError(f"missing_anchor:{role.value}")
            producers = {(item.node_instance_id, item.socket_name) for item in matches}
            if len(producers) > 1:
                raise GraphBindingError(f"ambiguous_anchor:{role.value}")
            if len(matches) > 1:
                raise GraphBindingError(f"duplicate_anchor:{role.value}")
            result.append(matches[0])
        return tuple(result)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "fixture_id": self.fixture_id,
            "task_modes": [value.value for value in self.task_modes],
            "nodes": [value.to_wire() for value in self.nodes],
            "edges": [value.to_wire() for value in self.edges],
            "anchors": [value.to_wire() for value in self.anchors],
            "children": [value.to_wire() for value in self.children],
            "transparent": self.transparent,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.to_wire(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _fixture(manifest: PublicManifest, fixture_id: str) -> WorkflowFixtureRef:
    matches = tuple(item for item in manifest.workflow_fixtures if item.fixture_id == fixture_id)
    if len(matches) != 1:
        raise GraphBindingError(f"unknown or duplicate fixture: {fixture_id!r}")
    return matches[0]


def manifest_to_graph(
    manifest: PublicManifest,
    fixture_id: str,
    *,
    node_ids: tuple[str, ...] | None = None,
    anchors: tuple[GraphAnchor, ...] = (),
    edges: tuple[GraphEdge, ...] = (),
    children: tuple[VisibleGraph, ...] = (),
    transparent: bool = False,
    instance_prefix: str | None = None,
) -> VisibleGraph:
    """Project one explicit fixture reference into a visible graph value."""

    if not isinstance(manifest, PublicManifest):
        raise GraphBindingError("manifest_to_graph requires a PublicManifest")
    fixture = _fixture(manifest, fixture_id)
    selected_ids = fixture.node_ids if node_ids is None else node_ids
    if not isinstance(selected_ids, tuple) or not selected_ids:
        raise GraphBindingError("graph node_ids must be a non-empty tuple")
    if len(selected_ids) != len(set(selected_ids)) or not set(selected_ids).issubset(
        set(fixture.node_ids)
    ):
        raise GraphBindingError("graph node_ids drift from the fixture")
    prefix = (
        fixture.fixture_id
        if instance_prefix is None
        else _identifier(instance_prefix, "instance_prefix")
    )
    nodes: list[GraphNode] = []
    for index, node_id in enumerate(selected_ids, start=1):
        contract = manifest.contracts.get(node_id)
        nodes.append(GraphNode.from_contract(f"{prefix}.n{index}", contract))
    return VisibleGraph(
        fixture_id=fixture.fixture_id,
        task_modes=fixture.task_modes,
        nodes=tuple(nodes),
        edges=edges,
        anchors=anchors,
        children=children,
        transparent=transparent,
    )


def graph_to_manifest(
    graph: VisibleGraph,
    manifest: PublicManifest,
    *,
    required_anchor_roles: tuple[GraphAnchorRole, ...] = (),
) -> str:
    """Verify a visible graph against one fixture and return its stable fixture ID."""

    if not isinstance(graph, VisibleGraph) or not isinstance(manifest, PublicManifest):
        raise GraphBindingError("graph_to_manifest requires typed graph and manifest values")
    fixture = _fixture(manifest, graph.fixture_id)
    if graph.task_modes != fixture.task_modes:
        raise GraphBindingError("graph task modes drift from the fixture")
    flattened = graph.flatten_nodes()
    node_ids = tuple(item.node_id for item in flattened)
    if len(node_ids) != len(set(node_ids)):
        raise GraphBindingError("graph contains duplicate node IDs after transparent expansion")
    if set(node_ids) != set(fixture.node_ids):
        raise GraphBindingError("graph node IDs do not round-trip to the fixture")
    graph.resolve_anchors(required_anchor_roles)
    return fixture.fixture_id


__all__ = [
    "GRAPH_BINDING_SCHEMA",
    "MAX_GRAPH_ANCHORS",
    "MAX_GRAPH_DEPTH",
    "MAX_GRAPH_EDGES",
    "MAX_GRAPH_NODES",
    "GraphAnchor",
    "GraphAnchorRole",
    "GraphEdge",
    "GraphNode",
    "VisibleGraph",
    "graph_to_manifest",
    "manifest_to_graph",
]
