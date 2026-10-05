"""Node contracts: the aggregation surface every consumer imports.

This was one 2170-line module in which 1768 lines of registry data sat on top of the roughly 370
that define what the data means. Those are two questions -- what a node contract is, and which ones
exist -- and they now have two owners:

    node_contracts_types <- node_contracts_registry

The public surface is unchanged: the same eleven names import from here as before, and
`tests/test_core_module_layering.py` asserts the direction rather than leaving it to hold by
construction.
"""

from __future__ import annotations

from .errors import NodeContractError
from .node_contracts_registry import default_node_contract_registry
from .node_contracts_types import (
    NODE_CONTRACT_SCHEMA,
    NODE_NAMESPACE,
    HostApiFamily,
    HostCapabilities,
    HostVersion,
    NodeContract,
    NodeContractRegistry,
    NodeSocket,
    NodeSocketType,
)

__all__ = [
    "HostApiFamily",
    "HostCapabilities",
    "HostVersion",
    "NODE_CONTRACT_SCHEMA",
    "NODE_NAMESPACE",
    "NodeContract",
    "NodeContractError",
    "NodeContractRegistry",
    "NodeSocket",
    "NodeSocketType",
    "default_node_contract_registry",
]
