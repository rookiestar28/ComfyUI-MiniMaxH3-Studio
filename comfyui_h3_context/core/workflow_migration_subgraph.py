"""Whether a subgraph-form workflow is one of the canonical migrations.

Builds on the shape checks below it and adds the question they deliberately leave open: is the
wiring the canonical wiring, port by port and link by link, against the frozen expectations.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping

from .workflow_migration_expectations import (
    _ALLOWED_API_NODE_TYPES,
    _CANONICAL_SUBGRAPH_FINGERPRINTS,
    _DYNAMIC_NATIVE_TYPES,
    _EXPECTED_REFERENCE_SUBGRAPH_MEDIA,
    _EXPECTED_SUBGRAPH_LINKS,
    _EXPECTED_SUBGRAPH_NODE_TYPES,
    _FINGERPRINT,
    _NATIVE_TYPES,
)
from .workflow_migration_primitives import (
    WorkflowMigrationAudit,
    WorkflowMigrationError,
    _accepted,
    _mapping,
    _member_error,
    _rejected,
    _strict_int,
    _strict_projection_pair,
)
from .workflow_migration_subgraph_shape import (
    _validate_subgraph_container,
    _validate_subgraph_manifest_shape,
    _validate_subgraph_node_ports,
)


def _subgraph_nodes(
    workflow: Mapping[str, object],
) -> tuple[Mapping[str, object], list[Mapping[str, object]], list[Mapping[str, object]]]:
    definitions = _mapping(workflow.get("definitions"), "workflow.definitions")
    member_error = _member_error(definitions, frozenset({"subgraphs"}))
    if member_error:
        raise WorkflowMigrationError(member_error)
    raw_defs = definitions.get("subgraphs")
    if not isinstance(raw_defs, list) or len(raw_defs) != 1:
        raise WorkflowMigrationError("workflow.definitions.subgraphs must contain one definition")
    definition = _mapping(raw_defs[0], "subgraph[0]")
    allowed_definition = frozenset(
        {
            "id",
            "version",
            "state",
            "revision",
            "config",
            "name",
            "inputNode",
            "outputNode",
            "inputs",
            "outputs",
            "widgets",
            "nodes",
            "groups",
            "links",
            "extra",
            "category",
            "description",
            "h3_context",
        }
    )
    member_error = _member_error(definition, allowed_definition)
    if member_error:
        raise WorkflowMigrationError(member_error)
    raw_nodes = definition.get("nodes", [])
    raw_links = definition.get("links", [])
    if not isinstance(raw_nodes, list) or not isinstance(raw_links, list):
        raise WorkflowMigrationError("subgraph nodes and links must be arrays")
    for field in ("inputs", "outputs"):
        external = definition.get(field, [])
        if not isinstance(external, list):
            raise WorkflowMigrationError(f"subgraph definition.{field} must be an array")
        for item in external:
            if not isinstance(item, Mapping):
                raise WorkflowMigrationError(f"subgraph definition.{field} member is invalid")
            member_error = _member_error(
                item, frozenset({"id", "name", "type", "linkIds", "localized_name", "label", "pos"})
            )
            if member_error:
                raise WorkflowMigrationError(member_error)
            link_ids = item.get("linkIds")
            if not isinstance(link_ids, list) or any(
                not _strict_int(link_id) or link_id < 0 for link_id in link_ids
            ):
                raise WorkflowMigrationError("subgraph external linkIds are invalid")
    all_nodes = [_mapping(node, "subgraph[0].nodes") for node in raw_nodes]
    all_links = [_mapping(link, "subgraph[0].links") for link in raw_links]
    return definition, all_nodes, all_links


def _port_type(
    nodes_by_id: Mapping[int, Mapping[str, object]],
    definition: Mapping[str, object],
    node_id: int,
    slot: int,
    *,
    origin: bool,
    link_id: int | None = None,
) -> str | None:
    if node_id == -10:
        entries = definition.get("inputs", [])
        if not isinstance(entries, list) or slot >= len(entries):
            return None
        item = entries[slot]
        return (
            item.get("type")
            if isinstance(item, Mapping) and isinstance(item.get("type"), str)
            else None
        )
    if node_id == -20:
        entries = definition.get("outputs", [])
        if not isinstance(entries, list) or slot >= len(entries):
            return None
        item = entries[slot]
        return (
            item.get("type")
            if isinstance(item, Mapping) and isinstance(item.get("type"), str)
            else None
        )
    node = nodes_by_id.get(node_id)
    if node is None:
        return None
    entries = node.get("outputs" if origin else "inputs", [])
    if not origin and link_id is not None and isinstance(entries, list):
        for item in entries:
            if isinstance(item, Mapping) and item.get("link") == link_id:
                return item.get("type") if isinstance(item.get("type"), str) else None
    if not isinstance(entries, list) or slot >= len(entries):
        return None
    item = entries[slot]
    return (
        item.get("type")
        if isinstance(item, Mapping) and isinstance(item.get("type"), str)
        else None
    )


def _expected_subgraph_target_slot(
    node: Mapping[str, object], input_index: int, input_name: str
) -> int:
    """ComfyUI native nodes keep legacy slot numbers for autogrow inputs."""

    if node.get("type") == "MiniMaxH3ReferenceToVideo":
        native_slots = {
            "prompt": 0,
            "width": 1,
            "height": 2,
            "length": 3,
            "ref_image_size": 4,
            "ref_images.image0": 8,
            "ref_images.image1": 9,
            "ref_videos.video0": 10,
            "ref_audios.audio0": 12,
        }
        return native_slots.get(input_name, input_index)
    return input_index


def _validate_subgraph(workflow: Mapping[str, object]) -> WorkflowMigrationAudit:
    kind = "subgraph"
    top_allowed = frozenset(
        {
            "revision",
            "last_node_id",
            "last_link_id",
            "nodes",
            "links",
            "version",
            "definitions",
            "extra",
            "h3_context_fixture",
            "h3_context",
        }
    )
    member_error = _member_error(workflow, top_allowed)
    if member_error:
        return _rejected(kind, "versioned_subgraph", member_error, "open_versioned_workflow")
    manifest = workflow.get("h3_context_fixture")
    if not isinstance(manifest, Mapping):
        return _rejected(
            kind, "versioned_subgraph", "unsupported_schema", "open_versioned_workflow"
        )
    schema = manifest.get("schema")
    policy = str(manifest.get("migration_policy", "versioned_subgraph"))
    try:
        definition, nodes, links = _subgraph_nodes(workflow)
    except WorkflowMigrationError as exc:
        reason = (
            str(exc)
            if str(exc) in {"unknown_workflow_member", "private_workflow_field"}
            else "invalid_link"
        )
        return _rejected(kind, policy, reason, "open_versioned_workflow")

    by_id: dict[int, Mapping[str, object]] = {}
    node_allowed = frozenset(
        {
            "id",
            "type",
            "pos",
            "size",
            "flags",
            "order",
            "mode",
            "inputs",
            "outputs",
            "properties",
            "widgets_values",
            "title",
        }
    )
    for node in nodes:
        member_error = _member_error(node, node_allowed)
        if member_error:
            return _rejected(kind, policy, member_error, "open_versioned_workflow")
        node_id = node.get("id")
        node_type = node.get("type")
        if (
            type(node_id) is not int
            or node_id < 0
            or node_id in by_id
            or not isinstance(node_type, str)
        ):
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        if node_type not in _ALLOWED_API_NODE_TYPES:
            return _rejected(kind, policy, "unknown_node_type", "open_versioned_workflow")
        by_id[node_id] = node
        raw_inputs = node.get("inputs", [])
        raw_outputs = node.get("outputs", [])
        if not isinstance(raw_inputs, list) or not isinstance(raw_outputs, list):
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        seen_names: set[str] = set()
        for item in raw_inputs:
            if not isinstance(item, Mapping):
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
            error = _member_error(item, frozenset({"label", "name", "type", "link", "widget"}))
            if (
                error
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("type"), str)
                or item["name"] in seen_names
            ):
                return _rejected(
                    kind, policy, error or "invalid_graph_binding", "rebuild_canonical_graph"
                )
            seen_names.add(item["name"])
            link = item.get("link")
            widget = item.get("widget")
            if widget is not None:
                if not isinstance(widget, Mapping) or _member_error(widget, frozenset({"name"})):
                    return _rejected(
                        kind, policy, "unknown_workflow_member", "open_versioned_workflow"
                    )
                if not isinstance(widget.get("name"), str) or widget["name"] != item["name"]:
                    return _rejected(
                        kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                    )
            if link is not None and (not _strict_int(link) or link < 0):
                return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        for item in raw_outputs:
            if not isinstance(item, Mapping):
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
            error = _member_error(item, frozenset({"name", "type", "links"}))
            if (
                error
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("type"), str)
                or not isinstance(item.get("links"), list)
            ):
                return _rejected(
                    kind, policy, error or "invalid_graph_binding", "rebuild_canonical_graph"
                )
            if any(not _strict_int(link_id) or link_id < 0 for link_id in item["links"]):
                return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")

    # A schema-1 ProductShell wrapper is deliberately manual-only.  A dynamic native node in
    # that wrapper is never a qualified migration graph, even if its inputs look complete.
    if schema == "h3-context-product-shell-subgraph/1" and any(
        node.get("type") in _NATIVE_TYPES for node in by_id.values()
    ):
        return _rejected(kind, policy, "unsupported_dynamic_subgraph", "open_versioned_workflow")
    for link in links:
        raw_origin_id = link.get("origin_id")
        raw_target_id = link.get("target_id")
        source = by_id.get(raw_origin_id) if isinstance(raw_origin_id, int) else None
        target = by_id.get(raw_target_id) if isinstance(raw_target_id, int) else None
        if (
            link.get("type") == "H3_PROMPT_STRING"
            and target is not None
            and target.get("type") in _NATIVE_TYPES
            and (
                source is None or source.get("type") != "comfyui_h3_context.H3Context.ProductShell"
            )
        ):
            return _rejected(
                kind,
                policy,
                "legacy_prompt_socket_to_native",
                "use_versioned_product_shell_workflow",
            )
    if schema != "h3-context-subgraph-fixture/2" and any(
        node.get("type") in _DYNAMIC_NATIVE_TYPES for node in by_id.values()
    ):
        return _rejected(kind, policy, "unsupported_dynamic_subgraph", "open_versioned_workflow")
    for node in nodes:
        for field in ("flags", "properties"):
            value = node.get(field)
            if not isinstance(value, Mapping):
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
            error = _member_error(value, frozenset())
            if error:
                return _rejected(kind, policy, error, "open_versioned_workflow")
    if schema == "h3-context-product-shell-subgraph/1":
        manifest_allowed = frozenset(
            {
                "schema",
                "name",
                "product_scope",
                "standard_prompt_socket",
                "dynamic_reference_boundary",
                "rollback",
            }
        )
    elif schema == "h3-context-subgraph-fixture/2":
        manifest_allowed = frozenset(
            {
                "schema",
                "name",
                "expanded_fixture",
                "ux_schema",
                "high_value_inputs",
                "execution_disclosure",
                "input_projection",
                "output_fingerprint_projection",
                "expanded_node_types",
                "output_projection",
                "direct_media_links",
                "graph_fingerprint_parts",
                "media_inputs",
                "asset_projection",
                "directive_disclosure",
                "limitations",
                "canonical_projections",
            }
        )
    else:
        manifest_allowed = frozenset()
    member_error = _member_error(manifest, manifest_allowed)
    if member_error:
        return _rejected(kind, policy, member_error, "open_versioned_workflow")
    if schema == "h3-context-subgraph-fixture/2":
        disclosure = manifest.get("execution_disclosure")
        disclosure_error = (
            None
            if not isinstance(disclosure, Mapping)
            else _member_error(
                disclosure,
                frozenset(
                    {"execution_route", "privacy_mode", "network", "media_transfer", "credentials"}
                ),
            )
        )
        if not isinstance(disclosure, Mapping) or disclosure_error:
            return _rejected(
                kind,
                policy,
                disclosure_error or "unknown_workflow_member",
                "open_versioned_workflow",
            )
        directive = manifest.get("directive_disclosure")
        directive_error = (
            None
            if directive is None or not isinstance(directive, Mapping)
            else _member_error(
                directive, frozenset({"schema", "supported_actions", "execution", "owner"})
            )
        )
        if directive is not None and (not isinstance(directive, Mapping) or directive_error):
            return _rejected(
                kind,
                policy,
                directive_error or "unknown_workflow_member",
                "open_versioned_workflow",
            )

    link_by_id: dict[int, Mapping[str, object]] = {}
    link_allowed = frozenset({"id", "origin_id", "origin_slot", "target_id", "target_slot", "type"})
    for link in links:
        error = _member_error(link, link_allowed)
        if error:
            return _rejected(kind, policy, error, "open_versioned_workflow")
        link_id = link.get("id")
        if type(link_id) is not int or link_id < 0 or link_id in link_by_id:
            return _rejected(
                kind,
                policy,
                "duplicate_link" if link_id in link_by_id else "invalid_link",
                "rebuild_canonical_graph",
            )
        origin_id, target_id = link.get("origin_id"), link.get("target_id")
        origin_slot, target_slot = link.get("origin_slot"), link.get("target_slot")
        if (
            type(origin_id) is not int
            or type(target_id) is not int
            or type(origin_slot) is not int
            or type(target_slot) is not int
            or origin_slot < 0
            or target_slot < 0
        ):
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        if (
            origin_id not in by_id
            and origin_id != -10
            or target_id not in by_id
            and target_id != -20
        ):
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        if not isinstance(link.get("type"), str):
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        source = by_id.get(origin_id)
        target = by_id.get(target_id)
        if (
            link["type"] == "H3_PROMPT_STRING"
            and target is not None
            and target.get("type") in _NATIVE_TYPES
            and (
                source is None or source.get("type") != "comfyui_h3_context.H3Context.ProductShell"
            )
        ):
            return _rejected(
                kind,
                policy,
                "legacy_prompt_socket_to_native",
                "use_versioned_product_shell_workflow",
            )
        origin_type = _port_type(by_id, definition, origin_id, origin_slot, origin=True)
        target_type = _port_type(
            by_id, definition, target_id, target_slot, origin=False, link_id=link_id
        )
        if origin_type is None or target_type is None:
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        compatible_boundary = (
            target_id == -20
            and origin_type == "STRING"
            and target_type == "H3_PROMPT_STRING"
            and link["type"] == "H3_PROMPT_STRING"
        )
        if not compatible_boundary and not (origin_type == target_type == link["type"]):
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        link_by_id[link_id] = link

    # Every node/link back-reference must agree with the canonical edge table.  Both sides of
    # every edge are required exactly once; a stale link table must never be silently accepted.
    target_refs: Counter[int] = Counter()
    origin_refs: Counter[int] = Counter()
    for node_id, node in by_id.items():
        raw_inputs = node.get("inputs", [])
        raw_outputs = node.get("outputs", [])
        if not isinstance(raw_inputs, list) or not isinstance(raw_outputs, list):
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        for slot, item in enumerate(raw_inputs):
            link_id = item.get("link") if isinstance(item, Mapping) else None
            if link_id is None:
                continue
            link = link_by_id.get(link_id)
            if (
                link is None
                or link["target_id"] != node_id
                or link["target_slot"]
                != _expected_subgraph_target_slot(node, slot, str(item.get("name", "")))
            ):
                return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
            target_refs[link_id] += 1
        for slot, item in enumerate(raw_outputs):
            if not isinstance(item, Mapping) or not isinstance(item.get("links"), list):
                return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
            output_links = item["links"]
            if len(output_links) != len(set(output_links)) or any(
                not _strict_int(link_id) or link_id < 0 for link_id in output_links
            ):
                return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
            for link_id in output_links:
                link = link_by_id.get(link_id)
                if link is None or link["origin_id"] != node_id or link["origin_slot"] != slot:
                    return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
                origin_refs[link_id] += 1
    for field, external_id in (("inputs", -10), ("outputs", -20)):
        entries = definition.get(field, [])
        if not isinstance(entries, list):
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        for slot, item in enumerate(entries):
            if not isinstance(item, Mapping) or not isinstance(item.get("linkIds"), list):
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
            link_ids = item["linkIds"]
            if len(link_ids) != len(set(link_ids)) or any(
                not _strict_int(link_id) or link_id < 0 for link_id in link_ids
            ):
                return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
            for link_id in link_ids:
                link = link_by_id.get(link_id)
                expected_side = "origin_id" if external_id == -10 else "target_id"
                if (
                    link is None
                    or link[expected_side] != external_id
                    or link["origin_slot" if external_id == -10 else "target_slot"] != slot
                ):
                    return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
                if external_id == -10:
                    origin_refs[link_id] += 1
                else:
                    target_refs[link_id] += 1
    link_ids = set(link_by_id)
    if (
        set(origin_refs) != link_ids
        or set(target_refs) != link_ids
        or any(origin_refs[link_id] != 1 or target_refs[link_id] != 1 for link_id in link_ids)
    ):
        return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
    if schema == "h3-context-subgraph-fixture/2":
        expected_links = _EXPECTED_SUBGRAPH_LINKS.get(str(manifest.get("expanded_fixture")))
        actual_links = [
            (
                link["id"],
                link["origin_id"],
                link["origin_slot"],
                link["target_id"],
                link["target_slot"],
                link["type"],
            )
            for link in links
        ]
        if expected_links is None or actual_links != expected_links:
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")

    product_shells = [
        node_id
        for node_id, node in by_id.items()
        if node.get("type") == "comfyui_h3_context.H3Context.ProductShell"
    ]
    if len(product_shells) != 1:
        return _rejected(
            kind, policy, "missing_product_shell_boundary", "restore_versioned_fixture"
        )
    dynamic_nodes = [
        node_id for node_id, node in by_id.items() if node.get("type") in _DYNAMIC_NATIVE_TYPES
    ]
    if any(
        node.get("type") in _NATIVE_TYPES
        for node in by_id.values()
        if node.get("type") not in _DYNAMIC_NATIVE_TYPES
    ):
        return _rejected(kind, policy, "unsupported_dynamic_subgraph", "open_versioned_workflow")
    shell = by_id[product_shells[0]]
    shell_inputs = shell.get("inputs")
    shell_outputs = shell.get("outputs")
    if not isinstance(shell_inputs, list) or not isinstance(shell_outputs, list):
        return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
    if [(item.get("name"), item.get("type")) for item in shell_inputs] != [
        ("report", "H3_CONTEXT_REPORT"),
        ("native_h3_wiring", "H3_NATIVE_H3_WIRING"),
    ] or [(item.get("name"), item.get("type")) for item in shell_outputs] != [
        ("prompt", "STRING"),
        ("product_shell", "H3_PRODUCT_SHELL"),
    ]:
        return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
    if schema == "h3-context-product-shell-subgraph/1":
        if dynamic_nodes or len(by_id) != 1:
            return _rejected(
                kind, policy, "unsupported_dynamic_subgraph", "open_versioned_workflow"
            )
        expected_edges = {
            (-10, 0, 1, 0, "H3_CONTEXT_REPORT"),
            (-10, 1, 1, 1, "H3_NATIVE_H3_WIRING"),
            (1, 0, -20, 0, "STRING"),
            (1, 1, -20, 1, "H3_PRODUCT_SHELL"),
        }
        actual_edges = {
            (
                link["origin_id"],
                link["origin_slot"],
                link["target_id"],
                link["target_slot"],
                link["type"],
            )
            for link in links
        }
        if actual_edges != expected_edges or len(links) != 4:
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        manifest_shape_audit = _validate_subgraph_manifest_shape(manifest, schema, kind, policy)
        if manifest_shape_audit is not None:
            return manifest_shape_audit
        container_audit = _validate_subgraph_container(
            workflow, definition, schema, manifest, nodes, kind, policy
        )
        if container_audit is not None:
            return container_audit
        node_port_audit = _validate_subgraph_node_ports(
            nodes, "workflows/m15_03_product_shell_base.json", kind, policy
        )
        if node_port_audit is not None:
            return node_port_audit
        return _accepted(kind, policy)
    if schema != "h3-context-subgraph-fixture/2" or len(dynamic_nodes) != 1:
        reason = (
            "unsupported_dynamic_subgraph"
            if dynamic_nodes
            and isinstance(schema, str)
            and schema.startswith("h3-context-subgraph-fixture/")
            else "unsupported_schema"
        )
        return _rejected(kind, policy, reason, "open_versioned_workflow")

    manifest_shape_audit = _validate_subgraph_manifest_shape(manifest, schema, kind, policy)
    if manifest_shape_audit is not None:
        return manifest_shape_audit
    container_audit = _validate_subgraph_container(
        workflow, definition, schema, manifest, nodes, kind, policy
    )
    if container_audit is not None:
        return container_audit

    native_type = by_id[dynamic_nodes[0]].get("type")
    expanded = manifest.get("expanded_fixture")
    expected_expanded = (
        "workflows/m15_09_assistant_reference.json"
        if native_type == "MiniMaxH3ReferenceToVideo"
        else "workflows/m15_09_assistant_base.json"
    )
    if expanded != expected_expanded:
        return _rejected(kind, policy, "unsupported_dynamic_subgraph", "open_versioned_workflow")
    expected_node_types = _EXPECTED_SUBGRAPH_NODE_TYPES.get(expected_expanded)
    if expected_node_types is None or [node.get("type") for node in nodes] != expected_node_types:
        return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
    fingerprint_parts = manifest.get("graph_fingerprint_parts")
    expected_fingerprint = _CANONICAL_SUBGRAPH_FINGERPRINTS.get(expected_expanded)
    if (
        expected_fingerprint is None
        or not isinstance(fingerprint_parts, list)
        or not all(isinstance(item, str) for item in fingerprint_parts)
        or "".join(fingerprint_parts) != expected_fingerprint
    ):
        return _rejected(kind, policy, "graph_fingerprint_mismatch", "restore_versioned_fixture")
    native = by_id[dynamic_nodes[0]]
    native_inputs = native.get("inputs")
    if not isinstance(native_inputs, list):
        return _rejected(kind, policy, "missing_native_input", "restore_versioned_fixture")
    native_input_names = [item.get("name") for item in native_inputs if isinstance(item, Mapping)]
    if native_type == "MiniMaxH3ImageToVideo":
        expected_names = ["prompt", "width", "height", "length"]
        expected_types = {"prompt": "STRING", "width": "INT", "height": "INT", "length": "INT"}
    else:
        expected_names = [
            "prompt",
            "width",
            "height",
            "length",
            "ref_image_size",
            "ref_images.image0",
            "ref_images.image1",
            "ref_videos.video0",
            "ref_audios.audio0",
        ]
        expected_types = {
            "prompt": "STRING",
            "width": "INT",
            "height": "INT",
            "length": "INT",
            "ref_image_size": "COMBO",
            "ref_images.image0": "IMAGE",
            "ref_images.image1": "IMAGE",
            "ref_videos.video0": "IMAGE",
            "ref_audios.audio0": "AUDIO",
        }
    if native_input_names != expected_names:
        return _rejected(kind, policy, "missing_native_input", "restore_versioned_fixture")
    for item in native_inputs:
        name = item.get("name") if isinstance(item, Mapping) else None
        if (
            not isinstance(item, Mapping)
            or not isinstance(name, str)
            or item.get("type") != expected_types.get(name)
        ):
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        link = item.get("link")
        if name == "prompt":
            if not _strict_int(link):
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        elif name in {"width", "height", "length", "ref_image_size"}:
            if link is not None or not isinstance(item.get("widget"), Mapping):
                return _rejected(kind, policy, "missing_native_input", "restore_versioned_fixture")
            widget = item["widget"]
            if widget.get("name") != name:
                return _rejected(kind, policy, "invalid_native_enum", "restore_versioned_fixture")
        else:
            if not _strict_int(link):
                return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
    widgets_values = native.get("widgets_values")
    if native_type == "MiniMaxH3ImageToVideo":
        if (
            not isinstance(widgets_values, list)
            or len(widgets_values) != 3
            or not all(_strict_int(value) for value in widgets_values)
        ):
            return _rejected(kind, policy, "missing_native_input", "restore_versioned_fixture")
    elif (
        not isinstance(widgets_values, list)
        or len(widgets_values) != 4
        or not all(_strict_int(value) for value in widgets_values[:3])
        or widgets_values[3] not in {"match", "max"}
    ):
        return _rejected(kind, policy, "invalid_native_enum", "restore_versioned_fixture")
    node_port_audit = _validate_subgraph_node_ports(nodes, str(expanded), kind, policy)
    if node_port_audit is not None:
        return node_port_audit
    projection = manifest.get("output_projection")
    validator_id = next(
        (
            node_id
            for node_id, node in by_id.items()
            if node.get("type") == "comfyui_h3_context.H3Context.Validator"
        ),
        None,
    )
    adapter_id = next(
        (
            node_id
            for node_id, node in by_id.items()
            if node.get("type") == "comfyui_h3_context.H3Context.NativeH3Adapter"
        ),
        None,
    )
    shell_id = product_shells[0]
    if (
        not isinstance(projection, Mapping)
        or set(projection)
        != {
            "prompt",
            "report",
            "native_prompt",
            "native_report",
            "product_shell",
            "native_node",
            "terminal_node",
        }
        or not _strict_projection_pair(projection.get("prompt"), str(shell_id), 0)
        or not _strict_projection_pair(projection.get("report"), str(validator_id), 1)
        or not _strict_projection_pair(
            projection.get("native_prompt"), str(dynamic_nodes[0]), "prompt"
        )
        or not _strict_projection_pair(projection.get("native_report"), str(adapter_id), 1)
        or not _strict_projection_pair(projection.get("product_shell"), str(shell_id), 1)
        or type(projection.get("native_node")) is not str
        or projection.get("native_node") != str(dynamic_nodes[0])
        or type(projection.get("terminal_node")) is not str
        or projection.get("terminal_node") != str(shell_id)
    ):
        return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
    expanded_types = manifest.get("expanded_node_types")
    if expanded_types != [node.get("type") for node in nodes]:
        return _rejected(kind, policy, "invalid_graph_binding", "restore_versioned_fixture")
    fingerprint_parts = manifest.get("graph_fingerprint_parts")
    if (
        not isinstance(fingerprint_parts, list)
        or not fingerprint_parts
        or not all(isinstance(item, str) for item in fingerprint_parts)
        or "".join(fingerprint_parts) != _CANONICAL_SUBGRAPH_FINGERPRINTS.get(str(expanded), "")
        or not _FINGERPRINT.fullmatch("".join(fingerprint_parts))
    ):
        return _rejected(kind, policy, "graph_fingerprint_mismatch", "restore_versioned_fixture")
    direct = manifest.get("direct_media_links")
    if not isinstance(direct, list):
        return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
    if native_type == "MiniMaxH3ImageToVideo":
        if direct:
            return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
    else:
        if direct != _EXPECTED_REFERENCE_SUBGRAPH_MEDIA:
            return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
        if len(direct) != 4:
            return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
        seen_paths: set[str] = set()
        for item in direct:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("target_input"), str)
                or not _strict_int(item.get("link"))
            ):
                return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
            path = item["target_input"]
            if (
                not re.fullmatch(
                    r"(?:ref_images\.image(?:0|[1-9][0-9]*)|ref_videos\.video(?:0|[1-9][0-9]*)|ref_audios\.audio(?:0|[1-9][0-9]*))",
                    path,
                )
                or path in seen_paths
                or item.get("target") != str(dynamic_nodes[0])
            ):
                return _rejected(
                    kind, policy, "non_canonical_autogrow", "restore_versioned_fixture"
                )
            seen_paths.add(path)
            link = link_by_id.get(item["link"])
            expected_link_type = "AUDIO" if path.startswith("ref_audios.") else "IMAGE"
            native_input = next(
                (entry for entry in native_inputs if entry.get("name") == path), None
            )
            expected_slot = (
                _expected_subgraph_target_slot(native, native_inputs.index(native_input), path)
                if native_input is not None
                else None
            )
            native_link_matches = (
                native_input is not None and native_input.get("link") == item["link"]
            )
            if (
                link is None
                or link["target_id"] != dynamic_nodes[0]
                or link["target_slot"] != expected_slot
                or not native_link_matches
                or link["type"] != expected_link_type
            ):
                return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
        if seen_paths != {
            "ref_images.image0",
            "ref_images.image1",
            "ref_videos.video0",
            "ref_audios.audio0",
        }:
            return _rejected(kind, policy, "non_canonical_autogrow", "restore_versioned_fixture")
    return _accepted(kind, policy)
