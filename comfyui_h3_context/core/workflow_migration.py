"""Workflow migration auditing: the aggregation surface every consumer imports.

This was one 2569-line module holding the bounded reader, the frozen expectations, the subgraph
shape checks, the API validator and the subgraph validator alongside the one entry point that picks
between them. Those are five separate questions and they now have five owners, layered one way:

    primitives, expectations <- subgraph_shape <- api, subgraph <- this entry point

The public surface is unchanged: the same six names import from here as before, and
`tests/test_core_module_layering.py` asserts the direction rather than leaving it to hold by
construction.
"""

from __future__ import annotations

from collections.abc import Mapping

from .workflow_migration_api import _validate_api
from .workflow_migration_primitives import (
    MIGRATION_AUDIT_SCHEMA,
    MigrationDisposition,
    WorkflowMigrationAudit,
    WorkflowMigrationError,
    _rejected,
    _walk_json,
    decode_workflow_json,
)
from .workflow_migration_subgraph import _validate_subgraph


def audit_workflow_migration(workflow: Mapping[str, object]) -> WorkflowMigrationAudit:
    """Return an immutable accepted/rejected migration disposition without mutating input."""

    _walk_json(workflow)
    if not isinstance(workflow, Mapping):
        raise WorkflowMigrationError("workflow root must be an object")
    if "prompt" in workflow:
        return _validate_api(workflow)
    if "definitions" in workflow and "h3_context_fixture" in workflow:
        return _validate_subgraph(workflow)
    return _rejected("unknown", "unspecified", "unknown_workflow_shape", "open_versioned_workflow")


__all__ = [
    "MIGRATION_AUDIT_SCHEMA",
    "MigrationDisposition",
    "WorkflowMigrationAudit",
    "WorkflowMigrationError",
    "audit_workflow_migration",
    "decode_workflow_json",
]
