"""Whether an API-form workflow is one of the canonical migrations.

The API form and the subgraph form are two different documents describing the same intent, and this
module owns only the first. Its link checking is separate from the subgraph's because an API link is
a metadata reference while a subgraph link is a wire.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from .canonical import canonical_fingerprint
from .workflow_migration_expectations import (
    _ALLOWED_API_NODE_TYPES,
    _API_NODE_KEYS,
    _API_ROOT_KEYS,
    _API_SCHEMAS,
    _CANONICAL_API_FINGERPRINTS,
    _CANONICAL_AUTOGROW,
    _CANONICAL_MIGRATION,
    _CORE_API_TYPES,
    _DYNAMIC_NATIVE_TYPES,
    _EXPECTED_KEYS,
    _FINGERPRINT,
    _HOST_KEYS,
    _NATIVE_TYPES,
    _REQUIRED_INPUTS,
    _TASK_MODES,
)
from .workflow_migration_primitives import (
    _PRIVATE_KEY,
    WorkflowMigrationAudit,
    _accepted,
    _mapping,
    _member_error,
    _rejected,
    _strict_int,
    _strict_projection_pair,
)


def _node_inputs(node: Mapping[str, object], label: str) -> Mapping[str, object]:
    value = node.get("inputs", {})
    return _mapping(value, f"{label}.inputs")


def _link_tuples(value: object) -> tuple[list[tuple[str, int]], bool]:
    """Parse only ComfyUI's [node-id, output-slot] links; malformed mappings never disappear."""

    if not isinstance(value, list):
        return [], isinstance(value, Mapping)
    if len(value) == 2 and isinstance(value[0], (str, int)) and type(value[1]) is int:
        if value[1] < 0:
            return [], True
        return [(str(value[0]), value[1])], False
    if not value:
        return [], True
    links: list[tuple[str, int]] = []
    for item in value:
        child, malformed = _link_tuples(item)
        if malformed or not child:
            return [], True
        links.extend(child)
    return links, False


def _required_present(inputs: Mapping[str, object], name: str) -> bool:
    # Grouped/autogrow names are a separate serialized contract and must never satisfy a native
    # node's required direct input.  The migration audit accepts only the pinned direct shape.
    return name in inputs and inputs[name] is not None


def _expected_link_source(node_type: str, input_name: str) -> tuple[frozenset[str], int] | None:
    common: dict[tuple[str, str], tuple[frozenset[str], int]] = {
        ("comfyui_h3_context.H3Context.Plan", "request"): (
            frozenset({"comfyui_h3_context.H3Context.Request"}),
            0,
        ),
        ("comfyui_h3_context.H3Context.Plan", "reference_registry"): (
            frozenset({"comfyui_h3_context.H3Context.ReferenceRegistry"}),
            0,
        ),
        ("comfyui_h3_context.H3Context.Compiler", "plan"): (
            frozenset({"comfyui_h3_context.H3Context.Plan"}),
            0,
        ),
        ("comfyui_h3_context.H3Context.Validator", "plan"): (
            frozenset({"comfyui_h3_context.H3Context.Plan"}),
            0,
        ),
        ("comfyui_h3_context.H3Context.Validator", "prompt_document"): (
            frozenset({"comfyui_h3_context.H3Context.Compiler"}),
            2,
        ),
        ("comfyui_h3_context.H3Context.NativeH3Adapter", "report"): (
            frozenset({"comfyui_h3_context.H3Context.Validator"}),
            1,
        ),
        ("comfyui_h3_context.H3Context.ProductShell", "report"): (
            frozenset({"comfyui_h3_context.H3Context.Validator"}),
            1,
        ),
        ("comfyui_h3_context.H3Context.ProductShell", "native_h3_wiring"): (
            frozenset({"comfyui_h3_context.H3Context.NativeH3Adapter"}),
            1,
        ),
        ("comfyui_h3_context.H3Context.Preview", "report"): (
            frozenset({"comfyui_h3_context.H3Context.Validator"}),
            1,
        ),
        ("MiniMaxH3ImageToVideo", "prompt"): (
            frozenset({"comfyui_h3_context.H3Context.ProductShell"}),
            0,
        ),
        ("MiniMaxH3ReferenceToVideo", "prompt"): (
            frozenset({"comfyui_h3_context.H3Context.ProductShell"}),
            0,
        ),
        ("comfyui_h3_context.H3Context.ReferenceRegistry", "images"): (
            frozenset({"LoadImage"}),
            0,
        ),
        ("MiniMaxH3ReferenceToVideo", "ref_images"): (frozenset({"LoadImage"}), 0),
    }
    return common.get((node_type, input_name))


def _validate_api_metadata_links(
    raw_links: object,
    nodes: Mapping[str, Mapping[str, object]],
    kind: str,
    policy: str,
) -> WorkflowMigrationAudit | None:
    if raw_links is None:
        return None
    if not isinstance(raw_links, list):
        return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
    seen: set[int] = set()
    allowed = frozenset(
        {
            "id",
            "origin_id",
            "origin_slot",
            "target_id",
            "target_slot",
            "type",
            "target_type",
            "target_class",
        }
    )
    for raw_link in raw_links:
        if not isinstance(raw_link, Mapping):
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        member_error = _member_error(raw_link, allowed)
        if member_error:
            return _rejected(kind, policy, member_error, "open_versioned_workflow")
        link_id = raw_link.get("id")
        if type(link_id) is not int or link_id < 0 or link_id in seen:
            return _rejected(
                kind,
                policy,
                "duplicate_link" if link_id in seen else "invalid_link",
                "rebuild_canonical_graph",
            )
        seen.add(link_id)
        origin_id = raw_link.get("origin_id")
        target_id = raw_link.get("target_id")
        if type(origin_id) is not str or type(target_id) is not str:
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        if origin_id not in nodes or target_id not in nodes:
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        if not _strict_int(raw_link.get("origin_slot")) or not _strict_int(
            raw_link.get("target_slot")
        ):
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        if (
            raw_link["origin_slot"] < 0
            or raw_link["target_slot"] < 0
            or not isinstance(raw_link.get("type"), str)
        ):
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        source_type = nodes[origin_id].get("class_type")
        target_type = nodes[target_id].get("class_type")
        advertised_target = raw_link.get("target_type", raw_link.get("target_class"))
        if (
            raw_link.get("target_type") is not None
            and raw_link.get("target_class") is not None
            and raw_link.get("target_type") != raw_link.get("target_class")
        ):
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        if advertised_target is not None and advertised_target != target_type:
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        if target_type in _DYNAMIC_NATIVE_TYPES:
            if (
                source_type != "comfyui_h3_context.H3Context.ProductShell"
                or raw_link["origin_slot"] != 0
                or raw_link["target_slot"] != 0
                or raw_link["type"] != "STRING"
            ):
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        elif target_type == "comfyui_h3_context.H3Context.ProductShell":
            valid_shell_edges = {
                ("comfyui_h3_context.H3Context.Validator", 1, 0, "H3_CONTEXT_REPORT"),
                ("comfyui_h3_context.H3Context.NativeH3Adapter", 1, 1, "H3_NATIVE_H3_WIRING"),
            }
            if (
                source_type,
                raw_link["origin_slot"],
                raw_link["target_slot"],
                raw_link["type"],
            ) not in valid_shell_edges:
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        else:
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
    return None


def _validate_api(workflow: Mapping[str, object]) -> WorkflowMigrationAudit:
    kind = "api_prompt"
    host = workflow.get("host")
    if not isinstance(host, Mapping):
        return _rejected(kind, "unspecified", "unknown_workflow_member", "open_versioned_workflow")
    policy = str(host.get("migration_policy", ""))
    member_error = _member_error(workflow, _API_ROOT_KEYS)
    if member_error:
        return _rejected(kind, policy, member_error, "open_versioned_workflow")
    member_error = _member_error(host, _HOST_KEYS)
    if member_error:
        return _rejected(kind, policy, member_error, "open_versioned_workflow")
    schema = workflow.get("schema")
    if schema not in _API_SCHEMAS:
        return _rejected(kind, policy, "unsupported_schema", "open_versioned_workflow")
    if (
        workflow.get("fixture_status") != "static_model_free_contract"
        or workflow.get("workflow_format") != "comfyui_api_prompt_v1"
    ):
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    fixture_id = workflow.get("fixture_id")
    if not isinstance(fixture_id, str) or fixture_id not in _CANONICAL_API_FINGERPRINTS:
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    if (
        not policy
        or not isinstance(host.get("version"), str)
        or not isinstance(host.get("native_source"), str)
    ):
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    for field in ("revision_parts", "native_source_blob_parts"):
        parts = host.get(field)
        if (
            not isinstance(parts, list)
            or not parts
            or not all(isinstance(item, str) and item for item in parts)
        ):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    if ".." in host["native_source"] or "://" in host["native_source"]:
        return _rejected(kind, policy, "private_workflow_field", "open_versioned_workflow")

    # Keep the old bridge reason stable for callers that explicitly submit the legacy link shape.
    raw_links = workflow.get("links")
    if isinstance(raw_links, list):
        for raw_link in raw_links:
            if (
                isinstance(raw_link, Mapping)
                and raw_link.get("type") == "H3_PROMPT_STRING"
                and (
                    raw_link.get("target_type") == "STRING"
                    or raw_link.get("target_class") in _NATIVE_TYPES
                )
            ):
                return _rejected(
                    kind,
                    policy,
                    "legacy_prompt_socket_to_native",
                    "use_versioned_product_shell_workflow",
                )

    prompt = workflow.get("prompt")
    if not isinstance(prompt, Mapping) or not prompt:
        return _rejected(kind, policy, "missing_product_shell_boundary", "rebuild_canonical_graph")
    nodes: dict[str, Mapping[str, object]] = {}
    for raw_id, raw_node in prompt.items():
        if not isinstance(raw_id, str) or not raw_id or raw_id in nodes:
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        if not isinstance(raw_node, Mapping):
            return _rejected(kind, policy, "unknown_node_type", "open_versioned_workflow")
        member_error = _member_error(raw_node, _API_NODE_KEYS)
        if member_error:
            return _rejected(kind, policy, member_error, "open_versioned_workflow")
        node_type = raw_node.get("class_type")
        if node_type not in _ALLOWED_API_NODE_TYPES:
            return _rejected(kind, policy, "unknown_node_type", "open_versioned_workflow")
        nodes[raw_id] = raw_node

    type_counts = Counter(str(node.get("class_type")) for node in nodes.values())
    for required in _CORE_API_TYPES:
        if type_counts[required] != 1:
            return _rejected(
                kind,
                policy,
                "missing_product_shell_boundary"
                if required == "comfyui_h3_context.H3Context.ProductShell"
                else "missing_canonical_stage",
                "rebuild_canonical_graph",
            )
    dynamic_nodes = [
        node_id
        for node_id, node in nodes.items()
        if node.get("class_type") in _DYNAMIC_NATIVE_TYPES
    ]
    if len(dynamic_nodes) != 1:
        return _rejected(kind, policy, "missing_product_shell_boundary", "rebuild_canonical_graph")
    if any(
        node.get("class_type") not in _DYNAMIC_NATIVE_TYPES
        for node in nodes.values()
        if node.get("class_type") in _NATIVE_TYPES
    ):
        return _rejected(kind, policy, "unsupported_dynamic_native", "open_versioned_workflow")
    task_nodes = [
        node
        for node in nodes.values()
        if node.get("class_type") == "comfyui_h3_context.H3Context.Request"
    ]
    request_inputs = _node_inputs(task_nodes[0], "request")
    task_mode = request_inputs.get("task_mode")
    if task_mode not in _TASK_MODES:
        return _rejected(kind, policy, "invalid_task_mode", "restore_versioned_fixture")
    if task_mode == "ref2va":
        if (
            type_counts["comfyui_h3_context.H3Context.ReferenceRegistry"] != 1
            or type_counts["LoadImage"] != 2
        ):
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
    elif any(
        type_counts[item]
        for item in (
            "comfyui_h3_context.H3Context.ReferenceRegistry",
            "LoadImage",
            "LoadVideo",
            "LoadAudio",
            "GetVideoComponents",
        )
    ):
        return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")

    # Validate node input shape, required native fields, and every serialized link.
    for _node_id, node in nodes.items():
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
        for input_name, value in inputs.items():
            if not isinstance(input_name, str):
                return _rejected(kind, policy, "unknown_workflow_member", "open_versioned_workflow")
            if _PRIVATE_KEY.search(input_name):
                return _rejected(kind, policy, "private_workflow_field", "open_versioned_workflow")
            links, malformed = _link_tuples(value)
            if malformed:
                return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
            source_spec = _expected_link_source(str(node["class_type"]), input_name)
            if links and source_spec is None:
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
            if source_spec is not None:
                expected_classes, expected_slot = source_spec
                for source_id, source_slot in links:
                    source = nodes.get(source_id)
                    if (
                        source is None
                        or source.get("class_type") not in expected_classes
                        or source_slot != expected_slot
                    ):
                        return _rejected(
                            kind, policy, "invalid_graph_binding", "rebuild_canonical_graph"
                        )
        node_type = str(node["class_type"])
        if node_type in _REQUIRED_INPUTS:
            missing = [
                name for name in _REQUIRED_INPUTS[node_type] if not _required_present(inputs, name)
            ]
            if missing:
                return _rejected(kind, policy, "missing_native_input", "restore_versioned_fixture")
            if node_type == "MiniMaxH3ReferenceToVideo":
                if not isinstance(inputs.get("ref_images"), list) or not inputs["ref_images"]:
                    return _rejected(
                        kind, policy, "missing_native_input", "restore_versioned_fixture"
                    )
                if inputs.get("ref_image_size") not in {"match", "max"}:
                    return _rejected(
                        kind, policy, "invalid_native_enum", "restore_versioned_fixture"
                    )

    expected = workflow.get("expected")
    if not isinstance(expected, Mapping):
        return _rejected(kind, policy, "missing_migration_binding", "restore_versioned_fixture")
    member_error = _member_error(expected, _EXPECTED_KEYS)
    if member_error:
        return _rejected(kind, policy, member_error, "open_versioned_workflow")
    migration_source = _CANONICAL_MIGRATION[str(task_mode)]
    if (
        expected.get("migration_source") != migration_source
        or expected.get("rollback_fixture") != migration_source
    ):
        return _rejected(kind, policy, "missing_migration_binding", "restore_versioned_fixture")
    if (
        expected.get("task_mode") != task_mode
        or expected.get("prompt_report_semantics") != "byte_identical_to_migration_source"
    ):
        return _rejected(kind, policy, "invalid_fixture_contract", "restore_versioned_workflow")
    fingerprint_parts = expected.get("graph_fingerprint_parts")
    if (
        not isinstance(fingerprint_parts, list)
        or not fingerprint_parts
        or not all(isinstance(item, str) for item in fingerprint_parts)
    ):
        return _rejected(kind, policy, "graph_fingerprint_mismatch", "restore_versioned_fixture")
    try:
        actual_fingerprint = canonical_fingerprint(prompt)
    except Exception:
        actual_fingerprint = ""
    expected_canonical_fingerprint = _CANONICAL_API_FINGERPRINTS[fixture_id]
    if (
        "".join(fingerprint_parts) != actual_fingerprint
        or "".join(fingerprint_parts) != expected_canonical_fingerprint
        or not _FINGERPRINT.fullmatch("".join(fingerprint_parts))
    ):
        return _rejected(kind, policy, "graph_fingerprint_mismatch", "restore_versioned_fixture")

    shell_id = next(
        node_id
        for node_id, node in nodes.items()
        if node.get("class_type") == "comfyui_h3_context.H3Context.ProductShell"
    )
    native_id = dynamic_nodes[0]
    validator_id = next(
        node_id
        for node_id, node in nodes.items()
        if node.get("class_type") == "comfyui_h3_context.H3Context.Validator"
    )
    adapter_id = next(
        node_id
        for node_id, node in nodes.items()
        if node.get("class_type") == "comfyui_h3_context.H3Context.NativeH3Adapter"
    )
    projection = expected.get("output_projection")
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
        or not _strict_projection_pair(projection.get("prompt"), shell_id, 0)
        or not _strict_projection_pair(projection.get("report"), validator_id, 1)
        or not _strict_projection_pair(projection.get("native_prompt"), native_id, "prompt")
        or not _strict_projection_pair(projection.get("native_report"), adapter_id, 1)
        or not _strict_projection_pair(projection.get("product_shell"), shell_id, 1)
        or type(projection.get("native_node")) is not str
        or projection.get("native_node") != native_id
        or type(projection.get("terminal_node")) is not str
        or projection.get("terminal_node") != shell_id
    ):
        return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
    stage_ids = expected.get("pipeline_node_ids")
    pipeline_types = [
        "comfyui_h3_context.H3Context.Request",
    ]
    if task_mode == "ref2va":
        pipeline_types.append("comfyui_h3_context.H3Context.ReferenceRegistry")
    pipeline_types.extend(
        [
            "comfyui_h3_context.H3Context.Plan",
            "comfyui_h3_context.H3Context.Compiler",
            "comfyui_h3_context.H3Context.Validator",
            "comfyui_h3_context.H3Context.NativeH3Adapter",
            "comfyui_h3_context.H3Context.ProductShell",
        ]
    )
    if isinstance(fixture_id, str) and fixture_id.startswith("m15-09-"):
        pipeline_types.append(str(nodes[native_id]["class_type"]))
    pipeline_types.append("comfyui_h3_context.H3Context.Preview")
    expected_stage_ids = [
        node_id
        for node_type in pipeline_types
        for node_id, node in nodes.items()
        if node.get("class_type") == node_type
    ]
    if (
        not isinstance(stage_ids, list)
        or not all(isinstance(item, str) for item in stage_ids)
        or len(stage_ids) != len(set(stage_ids))
        or stage_ids != expected_stage_ids
    ):
        return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")

    shell = nodes[shell_id]
    shell_inputs = _node_inputs(shell, "product_shell")
    if not _strict_projection_pair(shell_inputs.get("report"), validator_id, 1) or not (
        _strict_projection_pair(shell_inputs.get("native_h3_wiring"), adapter_id, 1)
    ):
        return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
    native_inputs = _node_inputs(nodes[native_id], "native")
    if not _strict_projection_pair(native_inputs.get("prompt"), shell_id, 0):
        return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")

    direct = expected.get("direct_media_links")
    if not isinstance(direct, list):
        return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
    if task_mode != "ref2va":
        if direct:
            return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
        if "dynamic_reference_parent_visible" in expected:
            return _rejected(kind, policy, "invalid_fixture_contract", "restore_versioned_fixture")
    else:
        if len(direct) != 2:
            return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
        native_refs, malformed = _link_tuples(native_inputs.get("ref_images"))
        if malformed or len(native_refs) != len(direct):
            return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
        for ordinal, item in enumerate(direct, start=1):
            if not isinstance(item, Mapping):
                return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
            required_keys = frozenset(
                {
                    "source",
                    "source_output",
                    "target",
                    "target_input",
                    "target_path",
                    "asset_id",
                    "presentation_label",
                    "presentation_ordinal",
                }
            )
            member_error = _member_error(item, required_keys)
            if member_error or set(item) != set(required_keys):
                return _rejected(
                    kind,
                    policy,
                    member_error or "malformed_media_binding",
                    "rebuild_canonical_graph",
                )
            path = item.get("target_path")
            if (
                not isinstance(path, str)
                or _CANONICAL_AUTOGROW.fullmatch(path) is None
                or path != f"ref_images.ref_image_{ordinal - 1}"
            ):
                return _rejected(
                    kind, policy, "non_canonical_autogrow", "restore_versioned_fixture"
                )
            if (
                item.get("target") != native_id
                or item.get("target_input") != "ref_images"
                or type(item.get("source_output")) is not int
                or item.get("source_output") != 0
                or type(item.get("presentation_ordinal")) is not int
                or item.get("presentation_ordinal") != ordinal
                or item.get("asset_id") != f"image_{ordinal}"
                or item.get("presentation_label") != f"<Picture {ordinal}>"
            ):
                return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
            source_id, source_slot = native_refs[ordinal - 1]
            if (
                item.get("source") != source_id
                or source_slot != 0
                or nodes[source_id].get("class_type") != "LoadImage"
            ):
                return _rejected(kind, policy, "malformed_media_binding", "rebuild_canonical_graph")
        if expected.get("dynamic_reference_parent_visible") is not True:
            return _rejected(kind, policy, "invalid_fixture_contract", "restore_versioned_fixture")

    metadata_audit = _validate_api_metadata_links(raw_links, nodes, kind, policy)
    if metadata_audit is not None:
        return metadata_audit
    bindings = workflow.get("native_bindings")
    if bindings is not None:
        if not isinstance(bindings, list):
            return _rejected(kind, policy, "non_canonical_autogrow", "restore_versioned_fixture")
        for binding in bindings:
            if not isinstance(binding, Mapping):
                return _rejected(
                    kind, policy, "non_canonical_autogrow", "restore_versioned_fixture"
                )
            binding_error = _member_error(binding, frozenset({"path_basis"}))
            if binding_error:
                return _rejected(kind, policy, binding_error, "open_versioned_workflow")
            if binding.get("path_basis") != "fully_qualified_zero_based":
                return _rejected(
                    kind, policy, "non_canonical_autogrow", "restore_versioned_fixture"
                )
    return _accepted(kind, policy)
