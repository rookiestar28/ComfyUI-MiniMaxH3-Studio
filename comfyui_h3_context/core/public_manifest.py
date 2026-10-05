"""Versioned, dependency-free aggregate for the public ComfyUI surface.

The aggregate is the reconciliation boundary between declarative contracts, capability maturity,
reachability, runtime object-info, and model-free fixture references.  It contains metadata only;
it never imports ComfyUI, a provider, a model runtime, or a filesystem fixture.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .capability_manifest import (
    PUBLIC_NODE_IDS,
    BindingManifest,
    build_default_binding_manifest,
)
from .contracts import TaskMode
from .errors import PublicManifestError
from .node_contracts import NodeContract, NodeContractRegistry, default_node_contract_registry
from .reachability import (
    ReachabilityManifest,
    build_default_reachability_manifest,
    validate_binding_coverage,
)

PUBLIC_MANIFEST_SCHEMA = "h3.context.public.manifest.v1"
MAX_PUBLIC_OBJECT_INFO = 64
MAX_WORKFLOW_FIXTURES = 32
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_SOCKET_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_RELATIVE_FIXTURE = re.compile(r"[A-Za-z0-9_. -]+(?:/[A-Za-z0-9_. -]+)*\.json\Z")
_SAFE_MARKERS = (
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
)


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PublicManifestError(f"{field} must be a bounded identifier")
    return value


def _socket_name(value: object, field: str) -> str:
    if not isinstance(value, str) or _SOCKET_NAME.fullmatch(value) is None:
        raise PublicManifestError(f"{field} must be a bounded socket name")
    return value


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PublicManifestError(f"{field} must be a bounded non-empty string")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SAFE_MARKERS):
        raise PublicManifestError(f"{field} contains sensitive or remote metadata")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise PublicManifestError(f"{field} contains a control character")
    return value


def _names(values: object, field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > 32:
        raise PublicManifestError(f"{field} must be a bounded tuple")
    result = tuple(_socket_name(value, field) for value in values)
    if len(result) != len(set(result)):
        raise PublicManifestError(f"{field} must not contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class NodeObjectInfo:
    """Safe projection of the metadata a ComfyUI object-info response exposes."""

    node_id: str
    display_name: str
    category: str
    required_inputs: tuple[str, ...]
    optional_inputs: tuple[str, ...]
    output_types: tuple[str, ...]
    function: str
    output_node: bool = False

    def __post_init__(self) -> None:
        _identifier(self.node_id, "object-info node_id")
        _text(self.display_name, "object-info display_name", 256)
        _text(self.category, "object-info category", 256)
        required = _names(self.required_inputs, "object-info required_inputs")
        optional = _names(self.optional_inputs, "object-info optional_inputs")
        if set(required).intersection(optional):
            raise PublicManifestError("object-info required and optional inputs overlap")
        output_types = _names(self.output_types, "object-info output_types")
        _text(self.function, "object-info function", 128)
        if not isinstance(self.output_node, bool):
            raise PublicManifestError("object-info output_node must be a boolean")
        object.__setattr__(self, "required_inputs", required)
        object.__setattr__(self, "optional_inputs", optional)
        object.__setattr__(self, "output_types", output_types)

    @classmethod
    def from_contract(cls, contract: NodeContract) -> NodeObjectInfo:
        """Build a static object-info projection without importing a host node class."""

        if not isinstance(contract, NodeContract):
            raise PublicManifestError("object-info construction requires a NodeContract")
        return cls(
            node_id=contract.node_id,
            display_name=contract.display_name,
            category=contract.category,
            required_inputs=tuple(item.name for item in contract.inputs if item.required),
            optional_inputs=tuple(item.name for item in contract.inputs if not item.required),
            output_types=tuple(item.socket_type.value for item in contract.outputs),
            function="execute",
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "category": self.category,
            "required_inputs": list(self.required_inputs),
            "optional_inputs": list(self.optional_inputs),
            "output_types": list(self.output_types),
            "function": self.function,
            "output_node": self.output_node,
        }


@dataclass(frozen=True, slots=True)
class WorkflowFixtureRef:
    """Bounded identity/reference for a static workflow or transparent Subgraph fixture."""

    fixture_id: str
    path: str
    kind: str
    task_modes: tuple[TaskMode, ...]
    node_ids: tuple[str, ...]
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
        if not all(isinstance(value, TaskMode) for value in self.task_modes):
            raise PublicManifestError("fixture task_modes contain an invalid value")
        if not isinstance(self.node_ids, tuple) or not self.node_ids:
            raise PublicManifestError("fixture node_ids must be a non-empty tuple")
        for node_id in self.node_ids:
            _identifier(node_id, "fixture node_id")
        if len(self.node_ids) != len(set(self.node_ids)):
            raise PublicManifestError("fixture node_ids must not contain duplicates")
        _text(self.schema, "fixture schema", 128)

    def to_wire(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "path": self.path,
            "kind": self.kind,
            "task_modes": [value.value for value in self.task_modes],
            "node_ids": list(self.node_ids),
            "schema": self.schema,
        }


_PIPELINE_NODES = (
    "comfyui_h3_context.H3Context.Request",
    "comfyui_h3_context.H3Context.Plan",
    "comfyui_h3_context.H3Context.Compiler",
    "comfyui_h3_context.H3Context.Validator",
    "comfyui_h3_context.H3Context.NativeH3Adapter",
    "comfyui_h3_context.H3Context.Preview",
)
_REFERENCE_NODES = ("comfyui_h3_context.H3Context.ReferenceRegistry", *_PIPELINE_NODES)
_PRODUCT_SHELL_BASE_NODES = (
    *_PIPELINE_NODES,
    "comfyui_h3_context.H3Context.ProductShell",
)
_PRODUCT_SHELL_REFERENCE_NODES = (
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    *_PRODUCT_SHELL_BASE_NODES,
)
_ASSISTANT_BASE_NODES = (
    *_PIPELINE_NODES[:5],
    "comfyui_h3_context.H3Context.ProductShell",
    "comfyui_h3_context.H3Context.Preview",
)
_ASSISTANT_REFERENCE_NODES = (
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    *_PIPELINE_NODES[:5],
    "comfyui_h3_context.H3Context.ProductShell",
    "comfyui_h3_context.H3Context.Preview",
)
_PERCEPTION_PRODUCER_NODES = (
    "comfyui_h3_context.H3Context.MediaAdmissionProducer",
    "comfyui_h3_context.H3Context.VisualPerceptionProducer",
    "comfyui_h3_context.H3Context.AudioPerceptionProducer",
)
_DOWNSTREAM_PRODUCER_NODES = (
    "comfyui_h3_context.H3Context.HardConstraintProducer",
    "comfyui_h3_context.H3Context.IntentGraphProducer",
    "comfyui_h3_context.H3Context.EvidenceFusionProducer",
    "comfyui_h3_context.H3Context.CrossReferenceProducer",
    "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
    "comfyui_h3_context.H3Context.FullReferenceTimelineProducer",
)
_M15_02_WORKFLOW_NODES = (
    "comfyui_h3_context.H3Context.Request",
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    "comfyui_h3_context.H3Context.MediaAdmissionProducer",
    *_DOWNSTREAM_PRODUCER_NODES,
)
_M13_10_WORKFLOW_NODES = (
    "comfyui_h3_context.H3Context.Request",
    "comfyui_h3_context.H3Context.ReferenceRegistry",
    "comfyui_h3_context.H3Context.MediaAdmissionProducer",
    "comfyui_h3_context.H3Context.HardConstraintProducer",
    "comfyui_h3_context.H3Context.IntentGraphProducer",
    "comfyui_h3_context.H3Context.EvidenceFusionProducer",
    "comfyui_h3_context.H3Context.CrossReferenceProducer",
    "comfyui_h3_context.H3Context.DirectiveAuthorityProducer",
    "comfyui_h3_context.H3Context.Plan",
    "comfyui_h3_context.H3Context.FeasibleAVTimeline",
    "comfyui_h3_context.H3Context.HierarchicalEvidenceReduction",
    "comfyui_h3_context.H3Context.ConstrainedSemanticPlanning",
    "comfyui_h3_context.H3Context.SourceProfiledRenderer",
    "comfyui_h3_context.H3Context.Validator",
    "comfyui_h3_context.H3Context.NativeH3Adapter",
    "comfyui_h3_context.H3Context.LocalReconstruction",
)
DEFAULT_WORKFLOW_FIXTURES = (
    WorkflowFixtureRef(
        "workflow.m3_07.base",
        "workflows/m3_07_h3_context_base.json",
        "workflow",
        (TaskMode.T2VA,),
        _PIPELINE_NODES,
    ),
    WorkflowFixtureRef(
        "workflow.m3_07.reference",
        "workflows/m3_07_h3_context_reference.json",
        "workflow",
        (TaskMode.REF2VA,),
        _REFERENCE_NODES,
    ),
    WorkflowFixtureRef(
        "workflow.m6_07.full_reference",
        "workflows/m6_07_h3_context_full_reference.json",
        "workflow",
        (TaskMode.REF2VA,),
        _REFERENCE_NODES,
    ),
    WorkflowFixtureRef(
        "workflow.m7_03.audit_override",
        "workflows/m7_03_h3_context_audit_override.json",
        "workflow",
        (TaskMode.T2VA,),
        (*_PIPELINE_NODES, "comfyui_h3_context.H3Context.AuditOverride"),
    ),
    WorkflowFixtureRef(
        "workflow.m7_04.provider_transparency",
        "workflows/m7_04_h3_context_provider_transparency.json",
        "workflow",
        (TaskMode.T2VA,),
        (*_PIPELINE_NODES, "comfyui_h3_context.H3Context.ProviderTransparency"),
    ),
    WorkflowFixtureRef(
        "workflow.m7_05.reliability",
        "workflows/m7_05_h3_context_reliability.json",
        "workflow",
        (TaskMode.T2VA,),
        (*_PIPELINE_NODES, "comfyui_h3_context.H3Context.Reliability"),
    ),
    WorkflowFixtureRef(
        "workflow.m15_01.perception_producers",
        "workflows/m15_01_perception_producers.json",
        "workflow",
        (TaskMode.I2VA, TaskMode.REF2VA),
        _PERCEPTION_PRODUCER_NODES,
    ),
    WorkflowFixtureRef(
        "workflow.m15_02.downstream_producers",
        "workflows/m15_02_downstream_producers.json",
        "workflow",
        (TaskMode.T2VA, TaskMode.REF2VA),
        _M15_02_WORKFLOW_NODES,
    ),
    *(
        WorkflowFixtureRef(
            f"workflow.m15_02.downstream_{scenario}",
            f"workflows/m15_02_downstream_{scenario}.json",
            "workflow",
            (TaskMode.REF2VA,),
            _M15_02_WORKFLOW_NODES,
        )
        for scenario in ("minimal", "complex", "ambiguous", "adversarial")
    ),
    WorkflowFixtureRef(
        "workflow.m13_10.local_reconstruction",
        "workflows/m13_10_local_reconstruction.json",
        "workflow",
        (TaskMode.REF2VA,),
        _M13_10_WORKFLOW_NODES,
    ),
    WorkflowFixtureRef(
        "workflow.m15_03.product_shell_base",
        "workflows/m15_03_product_shell_base.json",
        "workflow",
        (TaskMode.T2VA,),
        _PRODUCT_SHELL_BASE_NODES,
    ),
    WorkflowFixtureRef(
        "workflow.m15_03.product_shell_reference",
        "workflows/m15_03_product_shell_reference.json",
        "workflow",
        (TaskMode.REF2VA,),
        _PRODUCT_SHELL_REFERENCE_NODES,
    ),
    WorkflowFixtureRef(
        "workflow.m15_09.assistant_base",
        "workflows/m15_09_assistant_base.json",
        "workflow",
        (TaskMode.T2VA,),
        _ASSISTANT_BASE_NODES,
        "h3-context-workflow-fixture/2",
    ),
    WorkflowFixtureRef(
        "workflow.m15_09.assistant_reference",
        "workflows/m15_09_assistant_reference.json",
        "workflow",
        (TaskMode.REF2VA,),
        _ASSISTANT_REFERENCE_NODES,
        "h3-context-workflow-fixture/2",
    ),
    WorkflowFixtureRef(
        "subgraph.m15_03.product_shell_boundary",
        "subgraphs/H3 Product Shell Boundary.json",
        "subgraph",
        (TaskMode.T2VA, TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA, TaskMode.REF2VA),
        ("comfyui_h3_context.H3Context.ProductShell",),
        "h3-context-product-shell-subgraph/1",
    ),
    WorkflowFixtureRef(
        "subgraph.base_assistant",
        "subgraphs/H3 Context Assistant - Base.json",
        "subgraph",
        (TaskMode.T2VA,),
        _ASSISTANT_BASE_NODES,
        "h3-context-subgraph-fixture/2",
    ),
    WorkflowFixtureRef(
        "subgraph.reference_assistant",
        "subgraphs/H3 Context Assistant - Reference.json",
        "subgraph",
        (TaskMode.REF2VA,),
        _ASSISTANT_REFERENCE_NODES,
        "h3-context-subgraph-fixture/2",
    ),
)


@dataclass(frozen=True, slots=True)
class PublicManifest:
    """One immutable source for registration, contracts, bindings, reachability, and fixtures."""

    contracts: NodeContractRegistry
    bindings: BindingManifest
    reachability: ReachabilityManifest
    object_info: tuple[NodeObjectInfo, ...]
    workflow_fixtures: tuple[WorkflowFixtureRef, ...]
    schema: str = PUBLIC_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PUBLIC_MANIFEST_SCHEMA:
            raise PublicManifestError("unsupported public manifest schema")
        if not isinstance(self.contracts, NodeContractRegistry):
            raise PublicManifestError("public manifest contracts are invalid")
        if not isinstance(self.bindings, BindingManifest):
            raise PublicManifestError("public manifest bindings are invalid")
        if not isinstance(self.reachability, ReachabilityManifest):
            raise PublicManifestError("public manifest reachability is invalid")
        validate_binding_coverage(self.bindings, self.reachability)
        contract_ids = tuple(item.node_id for item in self.contracts.definitions)
        if contract_ids != self.bindings.node_ids:
            raise PublicManifestError("contract and binding registration order drift")
        if set(contract_ids) != set(PUBLIC_NODE_IDS):
            raise PublicManifestError("public registration IDs drift from the reviewed namespace")
        if not isinstance(self.object_info, tuple) or not self.object_info:
            raise PublicManifestError("object_info must be a non-empty tuple")
        if len(self.object_info) > MAX_PUBLIC_OBJECT_INFO or not all(
            isinstance(item, NodeObjectInfo) for item in self.object_info
        ):
            raise PublicManifestError("object_info exceeds the bounded public surface")
        object_ids = tuple(item.node_id for item in self.object_info)
        if object_ids != contract_ids:
            raise PublicManifestError("runtime object-info IDs/order drift from contracts")
        for contract, info in zip(self.contracts.definitions, self.object_info, strict=True):
            self._validate_object_info(contract, info)
        if not isinstance(self.workflow_fixtures, tuple) or not self.workflow_fixtures:
            raise PublicManifestError("workflow_fixtures must be a non-empty tuple")
        if len(self.workflow_fixtures) > MAX_WORKFLOW_FIXTURES or not all(
            isinstance(item, WorkflowFixtureRef) for item in self.workflow_fixtures
        ):
            raise PublicManifestError("workflow_fixtures exceed the bounded public surface")
        fixture_ids = tuple(item.fixture_id for item in self.workflow_fixtures)
        if len(fixture_ids) != len(set(fixture_ids)):
            raise PublicManifestError("fixture IDs must be unique")
        known_ids = set(contract_ids)
        for fixture in self.workflow_fixtures:
            if not set(fixture.node_ids).issubset(known_ids):
                raise PublicManifestError(
                    f"fixture {fixture.fixture_id!r} contains an unknown node"
                )

    @staticmethod
    def _validate_object_info(contract: NodeContract, info: NodeObjectInfo) -> None:
        if info.node_id != contract.node_id or info.display_name != contract.display_name:
            raise PublicManifestError(f"object-info identity drift for {contract.node_id!r}")
        if info.category != contract.category:
            raise PublicManifestError(f"object-info category drift for {contract.node_id!r}")
        expected_required = tuple(item.name for item in contract.inputs if item.required)
        expected_optional = tuple(item.name for item in contract.inputs if not item.required)
        expected_outputs = tuple(item.socket_type.value for item in contract.outputs)
        if info.required_inputs != expected_required or info.optional_inputs != expected_optional:
            raise PublicManifestError(f"object-info input/widget drift for {contract.node_id!r}")
        if info.output_types != expected_outputs:
            raise PublicManifestError(f"object-info output drift for {contract.node_id!r}")

    @property
    def node_ids(self) -> tuple[str, ...]:
        return self.bindings.node_ids

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
            "bindings": self.bindings.to_wire(include_fingerprint=False),
            "reachability": self.reachability.to_wire(include_fingerprint=False),
            "object_info": [item.to_wire() for item in self.object_info],
            "workflow_fixtures": [item.to_wire() for item in self.workflow_fixtures],
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value


def build_public_manifest(
    *,
    contracts: NodeContractRegistry | None = None,
    bindings: BindingManifest | None = None,
    reachability: ReachabilityManifest | None = None,
    object_info: tuple[NodeObjectInfo, ...] | None = None,
    workflow_fixtures: tuple[WorkflowFixtureRef, ...] = DEFAULT_WORKFLOW_FIXTURES,
) -> PublicManifest:
    """Build the pure aggregate, with optional runtime object-info supplied by the adapter."""

    selected_contracts = default_node_contract_registry() if contracts is None else contracts
    if not isinstance(selected_contracts, NodeContractRegistry):
        raise PublicManifestError("contracts must be a NodeContractRegistry")
    selected_bindings = (
        build_default_binding_manifest(registry=selected_contracts)
        if bindings is None
        else bindings
    )
    if not isinstance(selected_bindings, BindingManifest):
        raise PublicManifestError("bindings must be a BindingManifest")
    selected_reachability = (
        build_default_reachability_manifest(selected_bindings)
        if reachability is None
        else reachability
    )
    selected_object_info = (
        tuple(NodeObjectInfo.from_contract(item) for item in selected_contracts.definitions)
        if object_info is None
        else object_info
    )
    return PublicManifest(
        contracts=selected_contracts,
        bindings=selected_bindings,
        reachability=selected_reachability,
        object_info=selected_object_info,
        workflow_fixtures=workflow_fixtures,
    )


__all__ = [
    "DEFAULT_WORKFLOW_FIXTURES",
    "MAX_PUBLIC_OBJECT_INFO",
    "MAX_WORKFLOW_FIXTURES",
    "PUBLIC_MANIFEST_SCHEMA",
    "NodeObjectInfo",
    "PublicManifest",
    "WorkflowFixtureRef",
    "build_public_manifest",
]
