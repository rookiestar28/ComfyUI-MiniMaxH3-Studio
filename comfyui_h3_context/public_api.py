"""Runtime-facing public-surface facade for ComfyUI registration and UI consumers.

This adapter is intentionally small: it reads class metadata from the canonical node mapping and
hands the result to the dependency-free ``core.public_api`` builder.  It does not import a host,
provider, model, media, or network runtime and does not create a second registration map.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from .core.errors import PublicManifestError
from .core.node_contracts import default_node_contract_registry
from .core.public_api import (
    DEFAULT_WORKFLOW_FIXTURES,
    PUBLIC_MANIFEST_SCHEMA,
    NodeObjectInfo,
    PublicManifest,
    WorkflowFixtureRef,
    build_public_manifest,
)
from .core.public_manifest_v2 import (
    PUBLIC_MANIFEST_V2_SCHEMA,
    PublicManifestV2,
    RuntimeNodeFacts,
    build_public_manifest_v2,
    project_public_manifest_v1,
)
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS


def _metadata_names(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        raise PublicManifestError(f"{field} must be a metadata mapping")
    names = tuple(value.keys())
    if not all(isinstance(name, str) and name for name in names):
        raise PublicManifestError(f"{field} contains an invalid input name")
    if len(names) != len(set(names)):
        raise PublicManifestError(f"{field} contains duplicate input names")
    return names


def collect_node_object_info(
    node_mapping: Mapping[str, type[object]],
    display_names: Mapping[str, str],
) -> tuple[NodeObjectInfo, ...]:
    """Project only bounded class metadata into the pure object-info value."""

    result: list[NodeObjectInfo] = []
    for node_id, node_class in node_mapping.items():
        if not isinstance(node_id, str) or not isinstance(node_class, type):
            raise PublicManifestError("node mapping contains an invalid registration")
        if node_id not in display_names or not isinstance(display_names[node_id], str):
            raise PublicManifestError(f"display name missing for {node_id!r}")
        raw_input_types = getattr(node_class, "INPUT_TYPES", None)
        if not callable(raw_input_types):
            raise PublicManifestError(f"INPUT_TYPES missing for {node_id!r}")
        raw_metadata = raw_input_types()
        if not isinstance(raw_metadata, Mapping):
            raise PublicManifestError(f"INPUT_TYPES for {node_id!r} is not a mapping")
        required = _metadata_names(raw_metadata.get("required", {}), "required inputs")
        optional = _metadata_names(raw_metadata.get("optional", {}), "optional inputs")
        raw_return_types = getattr(node_class, "RETURN_TYPES", ())
        if not isinstance(raw_return_types, tuple) or not all(
            isinstance(value, str) and value for value in raw_return_types
        ):
            raise PublicManifestError(f"RETURN_TYPES for {node_id!r} is invalid")
        raw_function = getattr(node_class, "FUNCTION", "execute")
        if not isinstance(raw_function, str) or not raw_function:
            raise PublicManifestError(f"FUNCTION for {node_id!r} is invalid")
        raw_category = getattr(node_class, "CATEGORY", "")
        if not isinstance(raw_category, str) or not raw_category:
            raise PublicManifestError(f"CATEGORY for {node_id!r} is invalid")
        raw_output_node = getattr(node_class, "OUTPUT_NODE", False)
        if not isinstance(raw_output_node, bool):
            raise PublicManifestError(f"OUTPUT_NODE for {node_id!r} is invalid")
        result.append(
            NodeObjectInfo(
                node_id=node_id,
                display_name=display_names[node_id],
                category=raw_category,
                required_inputs=required,
                optional_inputs=optional,
                output_types=cast(tuple[str, ...], raw_return_types),
                function=raw_function,
                output_node=raw_output_node,
            )
        )
    return tuple(result)


def build_runtime_public_manifest(
    *,
    node_mapping: Mapping[str, type[object]] = NODE_CLASS_MAPPINGS,
    display_names: Mapping[str, str] = NODE_DISPLAY_NAME_MAPPINGS,
    workflow_fixtures: tuple[WorkflowFixtureRef, ...] = DEFAULT_WORKFLOW_FIXTURES,
) -> PublicManifest:
    """Build and reconcile the canonical runtime registration/object-info manifest."""

    contracts = default_node_contract_registry()
    collected_object_info = collect_node_object_info(node_mapping, display_names)
    object_info_by_id = {item.node_id: item for item in collected_object_info}
    expected_ids = {contract.node_id for contract in contracts.definitions}
    if set(object_info_by_id) != expected_ids:
        raise PublicManifestError("runtime registration IDs drift from declarative contracts")
    object_info = tuple(object_info_by_id[contract.node_id] for contract in contracts.definitions)
    from .core.capability_manifest import build_default_binding_manifest

    bindings = build_default_binding_manifest(
        registry=contracts,
        registration_ids=tuple(node_mapping),
        display_names=display_names,
    )
    return build_public_manifest(
        contracts=contracts,
        bindings=bindings,
        object_info=object_info,
        workflow_fixtures=workflow_fixtures,
    )


def build_runtime_public_manifest_v2(
    *,
    node_mapping: Mapping[str, type[object]] = NODE_CLASS_MAPPINGS,
    display_names: Mapping[str, str] = NODE_DISPLAY_NAME_MAPPINGS,
) -> PublicManifestV2:
    """Build the compact declared surface: the registry, plus the two facts only a class knows."""

    contracts = default_node_contract_registry()
    collected = {
        item.node_id: item for item in collect_node_object_info(node_mapping, display_names)
    }
    expected_ids = {contract.node_id for contract in contracts.definitions}
    if set(collected) != expected_ids:
        raise PublicManifestError("runtime registration IDs drift from declarative contracts")
    # v1 catches a display-name or category divergence indirectly: it carries the *registered*
    # values into object-info, and `PublicManifest._validate_object_info` then compares them with
    # the contract.  v2 derives those fields from the contract, so that comparison would have
    # nothing left to disagree with -- the divergence would stop being detected rather than stop
    # existing.  The check is therefore made here, where the registered values are still in hand.
    for contract in contracts.definitions:
        observed = collected[contract.node_id]
        if observed.display_name != contract.display_name:
            raise PublicManifestError(f"display name drift for {contract.node_id!r}")
        if observed.category != contract.category:
            raise PublicManifestError(f"category drift for {contract.node_id!r}")
        expected_required = tuple(item.name for item in contract.inputs if item.required)
        expected_optional = tuple(item.name for item in contract.inputs if not item.required)
        if (
            observed.required_inputs != expected_required
            or observed.optional_inputs != expected_optional
        ):
            raise PublicManifestError(f"input/widget drift for {contract.node_id!r}")
        if observed.output_types != tuple(item.socket_type.value for item in contract.outputs):
            raise PublicManifestError(f"output drift for {contract.node_id!r}")
    return build_public_manifest_v2(
        contracts=contracts,
        runtime_nodes=tuple(
            RuntimeNodeFacts(
                node_id=contract.node_id,
                function=collected[contract.node_id].function,
                output_node=collected[contract.node_id].output_node,
            )
            for contract in contracts.definitions
        ),
    )


def get_public_manifest() -> PublicManifest:
    """Return a fresh immutable verified manifest for the current canonical node classes."""

    return build_runtime_public_manifest()


def get_public_manifest_v2() -> PublicManifestV2:
    """Return the compact declared surface; `project_public_manifest_v1` rebuilds v1 from it."""

    return build_runtime_public_manifest_v2()


def get_node_mappings() -> tuple[Mapping[str, type[object]], Mapping[str, str]]:
    """Return the canonical mappings used by registration and object-info projection."""

    return NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS


__all__ = [
    "DEFAULT_WORKFLOW_FIXTURES",
    "PUBLIC_MANIFEST_SCHEMA",
    "PUBLIC_MANIFEST_V2_SCHEMA",
    "NodeObjectInfo",
    "PublicManifest",
    "PublicManifestV2",
    "RuntimeNodeFacts",
    "WorkflowFixtureRef",
    "build_runtime_public_manifest",
    "build_runtime_public_manifest_v2",
    "collect_node_object_info",
    "get_node_mappings",
    "get_public_manifest",
    "get_public_manifest_v2",
    "project_public_manifest_v1",
]
