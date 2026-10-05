"""Compact public manifest: what is declared, separated from what is computed.

The v1 manifest is 308 056 bytes for 28 nodes, and 76% of that is `bindings` and `reachability` --
two sections nobody writes.  ``build_default_binding_manifest`` computes every one of a binding's
twenty fields from the node contract and the socket, and ``build_default_reachability_manifest``
computes every reachability entry from those bindings.  Serializing them next to the registry does
not record a second fact; it records the same fact a second time, in a form that can disagree.

So v2 carries only what is actually declared:

* the node contract registry, which is the one canonical identity for a public node;
* the capability registry, whose maturity, milestone and evidence level are reviewed judgements
  rather than derivations;
* the two object-info fields that come from the runtime class rather than the contract --
  ``function`` and ``output_node``;
* the workflow fixtures, **without** their node lists, because a fixture's node inventory is a
  property of the fixture file and is extracted from it;
* the host-core node types a fixture may legitimately contain, declared once.

``project_public_manifest_v1`` rebuilds the full v1 aggregate from that, byte for byte.  v1 is not
modified by this module and its fingerprint does not move: v1 becomes a projection of v2 rather than
a separate authority, which is the whole point.

This module reads no file and imports no host.  The fixture extractor takes a parsed document,
never a path, so verification decides what to read and this stays pure.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .capability_manifest import (
    BindingManifest,
    CapabilityRegistry,
    build_default_binding_manifest,
    default_capability_registry,
)
from .contracts import TaskMode
from .errors import PublicManifestError
from .node_contracts import NodeContractRegistry, default_node_contract_registry
from .public_manifest import (
    DEFAULT_WORKFLOW_FIXTURES,
    MAX_PUBLIC_OBJECT_INFO,
    MAX_WORKFLOW_FIXTURES,
    NodeObjectInfo,
    PublicManifest,
    WorkflowFixtureRef,
)
from .reachability import ReachabilityManifest, build_default_reachability_manifest

PUBLIC_MANIFEST_V2_SCHEMA = "h3.context.public.manifest.v2"

#: Node types a packaged fixture may contain that this repository does not own.  Two are the pinned
#: native H3 anchors M18-01 inventories as `host_core`; the other four are the ComfyUI-core media
#: loaders the M17-20 native templates bring with them.  The list is deliberately closed: a fixture
#: node type in neither this set nor the canonical registry is unknown, and unknown fails.
HOST_CORE_NODE_TYPES: tuple[str, ...] = (
    "GetVideoComponents",
    "LoadAudio",
    "LoadImage",
    "LoadVideo",
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
)

MAX_FIXTURE_NODES = 64
MAX_FIXTURE_TASK_MODES = 8
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
#: v1 bounds `function` but does not constrain its case, so neither does this.  Being stricter
#: here would mean a node class with a CamelCase `FUNCTION` builds under v1 and raises under v2,
#: which is a divergence in the direction that is hardest to notice: v2 alone would be wrong.
_FUNCTION = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_RELATIVE_FIXTURE = re.compile(r"[A-Za-z0-9_. -]+(?:/[A-Za-z0-9_. -]+)*\.json\Z")
_API_PROMPT_KEY = "prompt"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PublicManifestError(f"{field} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeNodeFacts:
    """The part of object-info that the contract does not decide.

    Everything else a `NodeObjectInfo` carries -- display name, category, input names, output types
    -- is already in the contract, and `PublicManifest._validate_object_info` refuses object-info
    that disagrees with it.  These two fields come from the registered class instead, so they are
    the only ones v2 has to record.
    """

    node_id: str
    function: str
    output_node: bool

    def __post_init__(self) -> None:
        _identifier(self.node_id, "runtime node_id")
        if not isinstance(self.function, str) or _FUNCTION.fullmatch(self.function) is None:
            raise PublicManifestError("runtime function must be a bounded identifier name")
        if not isinstance(self.output_node, bool):
            raise PublicManifestError("runtime output_node must be a boolean")

    def to_wire(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "function": self.function,
            "output_node": self.output_node,
        }


@dataclass(frozen=True, slots=True)
class WorkflowFixtureDescriptor:
    """A fixture's identity, without the node inventory that the file itself decides."""

    fixture_id: str
    path: str
    kind: str
    task_modes: tuple[TaskMode, ...]
    schema: str = "h3-context-workflow-fixture/1"

    def __post_init__(self) -> None:
        _identifier(self.fixture_id, "fixture_id")
        if not isinstance(self.path, str) or _RELATIVE_FIXTURE.fullmatch(self.path) is None:
            raise PublicManifestError("fixture path must be a relative JSON path")
        if ".." in self.path.split("/"):
            raise PublicManifestError("fixture path cannot traverse parent directories")
        if self.kind not in {"workflow", "subgraph"}:
            raise PublicManifestError("fixture kind must be workflow or subgraph")
        if not isinstance(self.task_modes, tuple) or not self.task_modes:
            raise PublicManifestError("fixture task_modes must be a non-empty tuple")
        # v1 leaves this unbounded.  The ceiling is set above the number of modes that exist, so it
        # closes the field without ever refusing a fixture v1 would have accepted.  Duplicates are
        # deliberately still tolerated, because rejecting them would be a real divergence.
        if len(self.task_modes) > MAX_FIXTURE_TASK_MODES:
            raise PublicManifestError("fixture task_modes exceed the bounded surface")
        if not all(isinstance(value, TaskMode) for value in self.task_modes):
            raise PublicManifestError("fixture task_modes contain an invalid value")
        if not isinstance(self.schema, str) or not self.schema or len(self.schema) > 128:
            raise PublicManifestError("fixture schema must be bounded non-empty text")

    def to_wire(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "path": self.path,
            "kind": self.kind,
            "task_modes": [value.value for value in self.task_modes],
            "schema": self.schema,
        }


@dataclass(frozen=True, slots=True)
class FixtureNodeInventory:
    """What one fixture file actually contains, split by who owns each node type."""

    fixture_id: str
    repository_nodes: tuple[str, ...]
    host_core_nodes: tuple[str, ...]
    #: Node types owned by neither side.  Non-empty is a failure, never a tolerated third class:
    #: an unclassified node is exactly the case where "we did not recognise it" must not read as
    #: "it is fine".
    unknown_nodes: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.fixture_id, "fixture_id")
        for group, field in (
            (self.repository_nodes, "repository_nodes"),
            (self.host_core_nodes, "host_core_nodes"),
            (self.unknown_nodes, "unknown_nodes"),
        ):
            if not isinstance(group, tuple) or len(group) > MAX_FIXTURE_NODES:
                raise PublicManifestError(f"{field} exceed the bounded fixture surface")
            if list(group) != sorted(set(group)):
                raise PublicManifestError(f"{field} must be sorted and unique")

    def require_known(self) -> FixtureNodeInventory:
        if self.unknown_nodes:
            raise PublicManifestError(
                f"fixture {self.fixture_id!r} contains an unknown node type: "
                f"{self.unknown_nodes[0]}"
            )
        return self


def extract_fixture_node_types(
    document: object,
    fixture_id: str,
    *,
    repository_node_ids: frozenset[str],
    host_core_node_types: tuple[str, ...] = HOST_CORE_NODE_TYPES,
) -> FixtureNodeInventory:
    """Classify every node type in one parsed fixture document.

    Two document shapes exist and both are read explicitly:

    * ``comfyui_api_prompt_v1`` -- ``prompt`` maps an id to ``{"class_type": ..., "inputs": ...}``;
    * litegraph -- ``nodes[].type``, plus ``definitions.subgraphs[].nodes[].type``, where a node
      whose ``type`` is a subgraph definition id is an *instance* rather than a node type.

    A generic "collect every ``type`` string" walk is wrong and is not what this does: litegraph
    spells socket types ``type`` as well, so such a walk reports ``STRING`` and ``IMAGE`` as nodes.
    """

    if not isinstance(document, dict):
        raise PublicManifestError(f"fixture {fixture_id!r} is not a JSON object")
    found: set[str] = set()
    prompt = document.get(_API_PROMPT_KEY)
    if isinstance(prompt, dict):
        # A document carrying both shapes is one this function does not understand, and the two
        # branches below are exclusive, so the litegraph half would be read by nobody -- not
        # classified as repository, not as host-core, not even as unknown.  That is the one way a
        # node type could pass `require_known()` without ever being looked at, so an ambiguous
        # document is refused rather than half-read.  No shipped fixture has this shape.
        if isinstance(document.get("nodes"), list):
            raise PublicManifestError(
                f"fixture {fixture_id!r} carries both an API prompt and a litegraph node list"
            )
        for node in prompt.values():
            if isinstance(node, dict) and isinstance(node.get("class_type"), str):
                found.add(node["class_type"])
    else:
        definitions = document.get("definitions")
        subgraphs = definitions.get("subgraphs", []) if isinstance(definitions, dict) else []
        instance_ids = {
            item.get("id")
            for item in subgraphs
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        groups = [document, *(item for item in subgraphs if isinstance(item, dict))]
        for group in groups:
            nodes = group.get("nodes")
            if not isinstance(nodes, list):
                continue
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                kind = node.get("type")
                if isinstance(kind, str) and kind not in instance_ids:
                    found.add(kind)
    host_core = frozenset(host_core_node_types)
    return FixtureNodeInventory(
        fixture_id=fixture_id,
        repository_nodes=tuple(sorted(name for name in found if name in repository_node_ids)),
        host_core_nodes=tuple(sorted(name for name in found if name in host_core)),
        unknown_nodes=tuple(
            sorted(
                name for name in found if name not in repository_node_ids and name not in host_core
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class PublicManifestV2:
    """The declared public surface. Everything else about it is computed from these members."""

    contracts: NodeContractRegistry
    capabilities: CapabilityRegistry
    runtime_nodes: tuple[RuntimeNodeFacts, ...]
    workflow_fixtures: tuple[WorkflowFixtureDescriptor, ...]
    host_core_node_types: tuple[str, ...] = HOST_CORE_NODE_TYPES
    schema: str = PUBLIC_MANIFEST_V2_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PUBLIC_MANIFEST_V2_SCHEMA:
            raise PublicManifestError("unsupported public manifest v2 schema")
        if not isinstance(self.contracts, NodeContractRegistry):
            raise PublicManifestError("public manifest v2 contracts are invalid")
        if not isinstance(self.capabilities, CapabilityRegistry):
            raise PublicManifestError("public manifest v2 capabilities are invalid")
        contract_ids = tuple(item.node_id for item in self.contracts.definitions)
        if not isinstance(self.runtime_nodes, tuple) or not self.runtime_nodes:
            raise PublicManifestError("runtime_nodes must be a non-empty tuple")
        if len(self.runtime_nodes) > MAX_PUBLIC_OBJECT_INFO or not all(
            isinstance(item, RuntimeNodeFacts) for item in self.runtime_nodes
        ):
            raise PublicManifestError("runtime_nodes exceed the bounded public surface")
        runtime_ids = tuple(item.node_id for item in self.runtime_nodes)
        if runtime_ids != contract_ids:
            raise PublicManifestError("runtime node facts drift from the contract order")
        if not isinstance(self.workflow_fixtures, tuple) or not self.workflow_fixtures:
            raise PublicManifestError("workflow_fixtures must be a non-empty tuple")
        if len(self.workflow_fixtures) > MAX_WORKFLOW_FIXTURES or not all(
            isinstance(item, WorkflowFixtureDescriptor) for item in self.workflow_fixtures
        ):
            raise PublicManifestError("workflow_fixtures exceed the bounded public surface")
        fixture_ids = tuple(item.fixture_id for item in self.workflow_fixtures)
        if len(fixture_ids) != len(set(fixture_ids)):
            raise PublicManifestError("fixture IDs must be unique")
        if not isinstance(self.host_core_node_types, tuple) or not self.host_core_node_types:
            raise PublicManifestError("host_core_node_types must be a non-empty tuple")
        if list(self.host_core_node_types) != sorted(set(self.host_core_node_types)):
            raise PublicManifestError("host_core_node_types must be sorted and unique")
        for node_type in self.host_core_node_types:
            _identifier(node_type, "host core node type")
        if set(self.host_core_node_types) & set(contract_ids):
            raise PublicManifestError("a host-core node type cannot also be a repository node")

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(item.node_id for item in self.contracts.definitions)

    @property
    def repository_node_ids(self) -> frozenset[str]:
        return frozenset(self.node_ids)

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_wire(include_fingerprint=False),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_wire(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "contracts": self.contracts.to_wire(),
            "capabilities": self.capabilities.to_wire(),
            "runtime_nodes": [item.to_wire() for item in self.runtime_nodes],
            "workflow_fixtures": [item.to_wire() for item in self.workflow_fixtures],
            "host_core_node_types": list(self.host_core_node_types),
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value


#: The v1 fixture node lists, keyed by fixture.  v1 declared them inline; v2 does not carry them at
#: all, so the projection needs them from somewhere.  They are taken from the accepted v1 constant
#: rather than re-derived, and a test re-extracts all twenty from the fixture files and asserts the
#: two agree -- which is the check V1 section 2 asks for, run at verification time where it belongs
#: rather than at import time where it would make registration read the filesystem.
DEFAULT_FIXTURE_NODE_INVENTORY: dict[str, tuple[str, ...]] = {
    item.fixture_id: item.node_ids for item in DEFAULT_WORKFLOW_FIXTURES
}


def build_public_manifest_v2(
    *,
    contracts: NodeContractRegistry | None = None,
    capabilities: CapabilityRegistry | None = None,
    runtime_nodes: tuple[RuntimeNodeFacts, ...] | None = None,
    workflow_fixtures: tuple[WorkflowFixtureDescriptor, ...] | None = None,
    host_core_node_types: tuple[str, ...] = HOST_CORE_NODE_TYPES,
) -> PublicManifestV2:
    """Build the declared surface, defaulting every member to its canonical source."""

    selected_contracts = default_node_contract_registry() if contracts is None else contracts
    if not isinstance(selected_contracts, NodeContractRegistry):
        raise PublicManifestError("contracts must be a NodeContractRegistry")
    selected_capabilities = default_capability_registry() if capabilities is None else capabilities
    if runtime_nodes is None:
        raise PublicManifestError(
            "runtime_nodes must be supplied by the adapter that reads the registered classes"
        )
    selected_fixtures = (
        tuple(
            WorkflowFixtureDescriptor(
                fixture_id=item.fixture_id,
                path=item.path,
                kind=item.kind,
                task_modes=item.task_modes,
                schema=item.schema,
            )
            for item in DEFAULT_WORKFLOW_FIXTURES
        )
        if workflow_fixtures is None
        else workflow_fixtures
    )
    return PublicManifestV2(
        contracts=selected_contracts,
        capabilities=selected_capabilities,
        runtime_nodes=runtime_nodes,
        workflow_fixtures=selected_fixtures,
        host_core_node_types=host_core_node_types,
    )


def derive_object_info(manifest: PublicManifestV2) -> tuple[NodeObjectInfo, ...]:
    """Rebuild object-info: contract facts from the contract, class facts from `runtime_nodes`."""

    facts = {item.node_id: item for item in manifest.runtime_nodes}
    result: list[NodeObjectInfo] = []
    for contract in manifest.contracts.definitions:
        fact = facts[contract.node_id]
        result.append(
            NodeObjectInfo(
                node_id=contract.node_id,
                display_name=contract.display_name,
                category=contract.category,
                required_inputs=tuple(item.name for item in contract.inputs if item.required),
                optional_inputs=tuple(item.name for item in contract.inputs if not item.required),
                output_types=tuple(item.socket_type.value for item in contract.outputs),
                function=fact.function,
                output_node=fact.output_node,
            )
        )
    return tuple(result)


def derive_binding_manifest(manifest: PublicManifestV2) -> BindingManifest:
    """Recompute the binding view. It was never authored; it is a function of the registry."""

    return build_default_binding_manifest(
        registry=manifest.contracts,
        registration_ids=manifest.node_ids,
        display_names={item.node_id: item.display_name for item in manifest.contracts.definitions},
    )


def derive_reachability_manifest(bindings: BindingManifest) -> ReachabilityManifest:
    """Recompute the reachability view from the bindings, exactly as v1 does."""

    return build_default_reachability_manifest(bindings)


def project_public_manifest_v1(
    manifest: PublicManifestV2,
    *,
    fixture_node_ids: dict[str, tuple[str, ...]] | None = None,
) -> PublicManifest:
    """Rebuild the complete v1 aggregate from the declared surface.

    The result is byte-identical to what `build_public_manifest` produces for the same inputs, and
    carries the same fingerprint.  v1 readers therefore see no change at all; that is the point of
    projecting rather than replacing.
    """

    if not isinstance(manifest, PublicManifestV2):
        raise PublicManifestError("project_public_manifest_v1 requires a PublicManifestV2")
    inventory = DEFAULT_FIXTURE_NODE_INVENTORY if fixture_node_ids is None else fixture_node_ids
    bindings = derive_binding_manifest(manifest)
    fixtures: list[WorkflowFixtureRef] = []
    for descriptor in manifest.workflow_fixtures:
        node_ids = inventory.get(descriptor.fixture_id)
        if not node_ids:
            raise PublicManifestError(
                f"no node inventory is available for fixture {descriptor.fixture_id!r}"
            )
        fixtures.append(
            WorkflowFixtureRef(
                fixture_id=descriptor.fixture_id,
                path=descriptor.path,
                kind=descriptor.kind,
                task_modes=descriptor.task_modes,
                node_ids=node_ids,
                schema=descriptor.schema,
            )
        )
    return PublicManifest(
        contracts=manifest.contracts,
        bindings=bindings,
        reachability=derive_reachability_manifest(bindings),
        object_info=derive_object_info(manifest),
        workflow_fixtures=tuple(fixtures),
    )


__all__ = [
    "DEFAULT_FIXTURE_NODE_INVENTORY",
    "HOST_CORE_NODE_TYPES",
    "MAX_FIXTURE_NODES",
    "MAX_FIXTURE_TASK_MODES",
    "PUBLIC_MANIFEST_V2_SCHEMA",
    "FixtureNodeInventory",
    "PublicManifestV2",
    "RuntimeNodeFacts",
    "WorkflowFixtureDescriptor",
    "build_public_manifest_v2",
    "derive_binding_manifest",
    "derive_object_info",
    "derive_reachability_manifest",
    "extract_fixture_node_types",
    "project_public_manifest_v1",
]
