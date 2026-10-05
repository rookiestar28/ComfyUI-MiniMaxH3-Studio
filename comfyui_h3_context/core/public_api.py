"""Stable dependency-free facade for the public contract/manifest boundary.

Consumers that need the complete pure public surface should import this module rather than
depending on the broad historical ``core`` export list.  The broad list remains available for
backward compatibility and is intentionally not removed in this milestone.
"""

from __future__ import annotations

from .capability_manifest import BindingManifest, build_default_binding_manifest
from .node_contracts import NodeContractRegistry, default_node_contract_registry
from .public_manifest import (
    DEFAULT_WORKFLOW_FIXTURES,
    PUBLIC_MANIFEST_SCHEMA,
    NodeObjectInfo,
    PublicManifest,
    WorkflowFixtureRef,
    build_public_manifest,
)
from .public_manifest_v2 import (
    HOST_CORE_NODE_TYPES,
    PUBLIC_MANIFEST_V2_SCHEMA,
    FixtureNodeInventory,
    PublicManifestV2,
    RuntimeNodeFacts,
    WorkflowFixtureDescriptor,
    build_public_manifest_v2,
    extract_fixture_node_types,
    project_public_manifest_v1,
)
from .reachability import ReachabilityManifest, build_default_reachability_manifest

__all__ = [
    "DEFAULT_WORKFLOW_FIXTURES",
    "HOST_CORE_NODE_TYPES",
    "PUBLIC_MANIFEST_SCHEMA",
    "PUBLIC_MANIFEST_V2_SCHEMA",
    "BindingManifest",
    "FixtureNodeInventory",
    "NodeContractRegistry",
    "NodeObjectInfo",
    "PublicManifest",
    "PublicManifestV2",
    "ReachabilityManifest",
    "RuntimeNodeFacts",
    "WorkflowFixtureDescriptor",
    "WorkflowFixtureRef",
    "build_default_binding_manifest",
    "build_default_reachability_manifest",
    "build_public_manifest",
    "build_public_manifest_v2",
    "default_node_contract_registry",
    "extract_fixture_node_types",
    "project_public_manifest_v1",
]
