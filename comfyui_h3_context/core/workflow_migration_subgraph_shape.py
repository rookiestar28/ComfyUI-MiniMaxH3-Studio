"""Whether a serialized subgraph has the shape a subgraph is allowed to have.

Manifest, container and node ports -- three questions about structure, asked before anything looks
at how the subgraph is wired. Separating them from the wiring check keeps each one answerable on
its own: a manifest can be well formed and the links still wrong, and the reverse.
"""

from __future__ import annotations

from collections.abc import Mapping

from .workflow_migration_expectations import (
    _EXPECTED_SUBGRAPH_ASSET_PROJECTION,
    _EXPECTED_SUBGRAPH_CANONICAL_PROJECTIONS,
    _EXPECTED_SUBGRAPH_INPUT_PROJECTIONS,
    _EXPECTED_SUBGRAPH_OUTPUT_FINGERPRINTS,
)
from .workflow_migration_primitives import (
    WorkflowMigrationAudit,
    _member_error,
    _rejected,
    _strict_int,
)


def _validate_subgraph_manifest_shape(
    manifest: Mapping[str, object],
    schema: object,
    kind: str,
    policy: str,
) -> WorkflowMigrationAudit | None:
    if schema == "h3-context-product-shell-subgraph/1":
        expected = {
            "schema": schema,
            "name": "H3 Product Shell Boundary",
            "product_scope": "MANUAL_ONLY_SCOPED",
            "standard_prompt_socket": "STRING",
            "dynamic_reference_boundary": "parent_visible",
            "rollback": "remove_this_boundary_and_reconnect_native_adapter_prompt",
        }
        if set(manifest) != set(expected) or any(
            manifest.get(key) != value for key, value in expected.items()
        ):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
        return None
    if schema != "h3-context-subgraph-fixture/2":
        return _rejected(kind, policy, "unsupported_schema", "open_versioned_workflow")

    common = frozenset(
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
    error = _member_error(manifest, common)
    if error:
        return _rejected(kind, policy, error, "open_versioned_workflow")
    required = {
        "schema",
        "name",
        "expanded_fixture",
        "ux_schema",
        "high_value_inputs",
        "execution_disclosure",
        "input_projection",
        "expanded_node_types",
        "output_projection",
        "direct_media_links",
        "graph_fingerprint_parts",
    }
    if not required.issubset(manifest):
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")

    expanded_fixture = manifest.get("expanded_fixture")
    expected_name_ux = {
        "workflows/m15_09_assistant_base.json": (
            "H3 Context Assistant - Base",
            "h3-context-base-assistant/1",
        ),
        "workflows/m15_09_assistant_reference.json": (
            "H3 Context Assistant - Reference",
            "h3-context-reference-assistant/1",
        ),
    }
    if expanded_fixture not in expected_name_ux:
        return _rejected(kind, policy, "unsupported_dynamic_subgraph", "open_versioned_workflow")
    expected_name, expected_ux_schema = expected_name_ux[expanded_fixture]
    if manifest.get("name") != expected_name or manifest.get("ux_schema") != expected_ux_schema:
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    expected_high_value_inputs = ["task_mode", "user_intent", "duration_seconds"]
    if manifest.get("high_value_inputs") != expected_high_value_inputs:
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    if expanded_fixture.endswith("reference.json"):
        if manifest.get("media_inputs") != ["image_1", "image_2", "video_1", "audio_1"]:
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
        expected_limitations = [
            "No observations or directives are inferred from media, filenames, or metadata.",
            "Media transfer remains disabled; no provider or hosted route is selected.",
            "Weight-backed native H3 execution remains an explicit downstream host operation.",
        ]
        if manifest.get("limitations") != expected_limitations:
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    elif "media_inputs" in manifest or "limitations" in manifest:
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")

    disclosure = manifest.get("execution_disclosure")
    expected_disclosure = {
        "execution_route": "manual",
        "privacy_mode": "local_only",
        "network": "disabled",
        "media_transfer": "disabled",
        "credentials": "none",
    }
    if not isinstance(disclosure, Mapping) or dict(disclosure) != expected_disclosure:
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")

    list_specs: tuple[tuple[str, bool], ...] = (
        ("high_value_inputs", True),
        ("media_inputs", False),
        ("limitations", False),
    )
    for field, required_field in list_specs:
        value = manifest.get(field)
        if value is None and not required_field:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")

    input_projection = manifest.get("input_projection")
    if not isinstance(input_projection, list):
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    for item in input_projection:
        if not isinstance(item, Mapping):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
        member_error = _member_error(item, frozenset({"external", "internal", "link"}))
        if member_error:
            return _rejected(kind, policy, member_error, "open_versioned_workflow")
        if (
            not isinstance(item.get("external"), str)
            or not isinstance(item.get("internal"), list)
            or not all(isinstance(part, str) for part in item["internal"])
            or not _strict_int(item.get("link"))
            or item["link"] < 0
        ):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    if input_projection != _EXPECTED_SUBGRAPH_INPUT_PROJECTIONS[expanded_fixture]:
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")

    output_fingerprints = manifest.get("output_fingerprint_projection")
    if output_fingerprints is not None and (
        not isinstance(output_fingerprints, Mapping)
        or set(output_fingerprints) != {"prompt", "report"}
        or not all(
            isinstance(value, list) and all(isinstance(part, str) for part in value)
            for value in output_fingerprints.values()
        )
    ):
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    if expanded_fixture.endswith("base.json"):
        if output_fingerprints != _EXPECTED_SUBGRAPH_OUTPUT_FINGERPRINTS:
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    elif output_fingerprints is not None:
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")

    assets = manifest.get("asset_projection")
    if assets is not None:
        if not isinstance(assets, list):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
        for item in assets:
            if not isinstance(item, Mapping):
                return _rejected(
                    kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                )
            member_error = _member_error(
                item, frozenset({"asset_id", "role", "connection_order", "label"})
            )
            if member_error:
                return _rejected(kind, policy, member_error, "open_versioned_workflow")
            if (
                not all(
                    isinstance(item.get(key), str) and item.get(key)
                    for key in ("asset_id", "role", "label")
                )
                or not _strict_int(item.get("connection_order"))
                or item["connection_order"] < 1
            ):
                return _rejected(
                    kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                )
        if assets != _EXPECTED_SUBGRAPH_ASSET_PROJECTION:
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    elif expanded_fixture.endswith("reference.json"):
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")

    direct = manifest.get("direct_media_links")
    if not isinstance(direct, list):
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    for item in direct:
        if not isinstance(item, Mapping):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
        member_error = _member_error(item, frozenset({"source", "target", "target_input", "link"}))
        if member_error:
            return _rejected(kind, policy, member_error, "open_versioned_workflow")
        if (
            not all(
                isinstance(item.get(key), str) and item.get(key)
                for key in ("source", "target", "target_input")
            )
            or not _strict_int(item.get("link"))
            or item["link"] < 0
        ):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")

    canonical = manifest.get("canonical_projections")
    if canonical is not None:
        if not isinstance(canonical, Mapping) or set(canonical) != {"minimal", "complex"}:
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
        for projection in canonical.values():
            if not isinstance(projection, Mapping):
                return _rejected(
                    kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                )
            member_error = _member_error(
                projection,
                frozenset(
                    {
                        "fixture",
                        "graph_fingerprint_parts",
                        "output_projection",
                        "direct_media_links",
                    }
                ),
            )
            if member_error:
                return _rejected(kind, policy, member_error, "open_versioned_workflow")
            if not isinstance(projection.get("fixture"), str) or not isinstance(
                projection.get("graph_fingerprint_parts"), list
            ):
                return _rejected(
                    kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                )
            nested_direct = projection.get("direct_media_links")
            if not isinstance(nested_direct, list):
                return _rejected(
                    kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                )
            for nested in nested_direct:
                if not isinstance(nested, Mapping):
                    return _rejected(
                        kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                    )
                nested_error = _member_error(
                    nested, frozenset({"source", "source_output", "target", "target_input"})
                )
                if nested_error:
                    return _rejected(kind, policy, nested_error, "open_versioned_workflow")
        if canonical != _EXPECTED_SUBGRAPH_CANONICAL_PROJECTIONS:
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    elif expanded_fixture.endswith("reference.json"):
        return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")

    directive = manifest.get("directive_disclosure")
    if directive is not None:
        if not isinstance(directive, Mapping):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
        directive_error = _member_error(
            directive, frozenset({"schema", "supported_actions", "execution", "owner"})
        )
        if directive_error:
            return _rejected(kind, policy, directive_error, "open_versioned_workflow")
        if (
            directive.get("schema") != "h3.reference.directives.v1"
            or directive.get("supported_actions") != ["copy_event", "retain", "adapt", "exclude"]
            or directive.get("execution") != "not_inferred_or_executed"
            or not isinstance(directive.get("owner"), str)
        ):
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    return None


def _validate_subgraph_container(
    workflow: Mapping[str, object],
    definition: Mapping[str, object],
    schema: object,
    manifest: Mapping[str, object],
    nodes: list[Mapping[str, object]],
    kind: str,
    policy: str,
) -> WorkflowMigrationAudit | None:
    """Bind the host wrapper and definition metadata before auditing graph edges."""

    def reject(reason: str = "invalid_graph_binding") -> WorkflowMigrationAudit:
        return _rejected(kind, policy, reason, "rebuild_canonical_graph")

    top_extra = workflow.get("extra")
    if top_extra is not None:
        if not isinstance(top_extra, Mapping):
            return reject("invalid_fixture_contract")
        error = _member_error(top_extra, frozenset())
        if error:
            return _rejected(kind, policy, error, "open_versioned_workflow")

    top_context = workflow.get("h3_context")
    if top_context is not None:
        if not isinstance(top_context, Mapping):
            return reject("invalid_fixture_contract")
        error = _member_error(top_context, frozenset())
        if error:
            return _rejected(kind, policy, error, "open_versioned_workflow")

    if (
        type(definition.get("id")) is not str
        or not definition["id"]
        or type(definition.get("version")) is not int
        or definition["version"] != 1
        or type(definition.get("revision")) is not int
        or definition["revision"] != 0
    ):
        return reject("invalid_fixture_contract")
    state = definition.get("state")
    if not isinstance(state, Mapping):
        return reject("invalid_fixture_contract")
    state_error = _member_error(
        state, frozenset({"lastGroupId", "lastNodeId", "lastLinkId", "lastRerouteId"})
    )
    if state_error:
        return _rejected(kind, policy, state_error, "open_versioned_workflow")
    if any(
        not _strict_int(state.get(key)) or state[key] < 0
        for key in ("lastGroupId", "lastNodeId", "lastLinkId", "lastRerouteId")
    ):
        return reject("invalid_fixture_contract")
    config = definition.get("config")
    if not isinstance(config, Mapping):
        return reject("invalid_fixture_contract")
    config_error = _member_error(config, frozenset())
    if config_error:
        return _rejected(kind, policy, config_error, "open_versioned_workflow")
    definition_extra = definition.get("extra")
    if not isinstance(definition_extra, Mapping):
        return reject("invalid_fixture_contract")
    expected_extra: Mapping[str, object] = (
        {}
        if schema == "h3-context-product-shell-subgraph/1"
        else {"workflowRendererVersion": "M15-09-assistant-v2"}
    )
    if dict(definition_extra) != dict(expected_extra):
        error = _member_error(definition_extra, frozenset(expected_extra))
        return _rejected(
            kind, policy, error or "invalid_fixture_contract", "open_versioned_workflow"
        )

    definition_context = definition.get("h3_context")
    if schema == "h3-context-product-shell-subgraph/1":
        expected_context = {
            "schema": "h3-context-product-shell-subgraph/1",
            "product_scope": "MANUAL_ONLY_SCOPED",
            "fixed_cardinality": "report:1,native_h3_wiring:1",
            "dynamic_reference_boundary": "parent_visible",
            "rollback": "remove_this_boundary_and_reconnect_native_adapter_prompt",
        }
        if not isinstance(definition_context, Mapping):
            return reject("invalid_fixture_contract")
        context_error = _member_error(definition_context, frozenset(expected_context))
        if context_error:
            return _rejected(kind, policy, context_error, "open_versioned_workflow")
        if dict(definition_context) != expected_context:
            return reject("invalid_fixture_contract")
    elif definition_context is not None:
        if not isinstance(definition_context, Mapping):
            return reject("invalid_fixture_contract")
        context_error = _member_error(definition_context, frozenset())
        return _rejected(
            kind,
            policy,
            context_error or "unknown_workflow_member",
            "open_versioned_workflow",
        )

    for field, expected_id in (("inputNode", -10), ("outputNode", -20)):
        boundary = definition.get(field)
        if not isinstance(boundary, Mapping):
            return reject("invalid_fixture_contract")
        boundary_error = _member_error(boundary, frozenset({"id", "bounding"}))
        if boundary_error:
            return _rejected(kind, policy, boundary_error, "open_versioned_workflow")
        bounding = boundary.get("bounding")
        if (
            type(boundary.get("id")) is not int
            or boundary["id"] != expected_id
            or not isinstance(bounding, list)
            or len(bounding) != 4
            or not all(_strict_int(value) for value in bounding)
        ):
            return reject("invalid_fixture_contract")
    for field in ("widgets", "groups"):
        value = definition.get(field)
        if not isinstance(value, list) or value:
            return reject("invalid_fixture_contract")
    if any(
        field in definition and not isinstance(definition[field], str)
        for field in ("category", "description")
    ):
        return reject("invalid_fixture_contract")
    if definition.get("name") != manifest.get("name"):
        return reject("invalid_fixture_contract")

    reference = any(node.get("type") == "MiniMaxH3ReferenceToVideo" for node in nodes)
    if schema == "h3-context-product-shell-subgraph/1":
        expected_ports = {
            "inputs": [
                ("report", "H3_CONTEXT_REPORT"),
                ("native_h3_wiring", "H3_NATIVE_H3_WIRING"),
            ],
            "outputs": [("prompt", "STRING"), ("product_shell", "H3_PRODUCT_SHELL")],
        }
    elif reference:
        expected_ports = {
            "inputs": [
                ("task_mode", "COMBO"),
                ("user_intent", "STRING"),
                ("duration_seconds", "FLOAT"),
                ("image_1", "IMAGE"),
                ("image_2", "IMAGE"),
                ("video_1", "VIDEO"),
                ("audio_1", "AUDIO"),
            ],
            "outputs": [
                ("prompt", "H3_PROMPT_STRING"),
                ("report", "H3_CONTEXT_REPORT"),
                ("native_h3_wiring", "H3_NATIVE_H3_WIRING"),
                ("preview", "H3_CONTEXT_PREVIEW"),
            ],
        }
    else:
        expected_ports = {
            "inputs": [
                ("task_mode", "COMBO"),
                ("user_intent", "STRING"),
                ("duration_seconds", "FLOAT"),
            ],
            "outputs": [
                ("prompt", "H3_PROMPT_STRING"),
                ("report", "H3_CONTEXT_REPORT"),
                ("native_h3_wiring", "H3_NATIVE_H3_WIRING"),
                ("preview", "H3_CONTEXT_PREVIEW"),
            ],
        }
    for field, expected in expected_ports.items():
        entries = definition.get(field)
        if not isinstance(entries, list) or len(entries) != len(expected):
            return reject("invalid_graph_binding")
        for item, (expected_name, expected_type) in zip(entries, expected, strict=True):
            if not isinstance(item, Mapping):
                return reject("invalid_graph_binding")
            error = _member_error(
                item, frozenset({"id", "name", "type", "linkIds", "localized_name", "label", "pos"})
            )
            if error:
                return _rejected(kind, policy, error, "open_versioned_workflow")
            link_ids = item.get("linkIds")
            if (
                type(item.get("id")) is not str
                or not item["id"]
                or item.get("name") != expected_name
                or item.get("type") != expected_type
                or not isinstance(link_ids, list)
                or len(link_ids) != len(set(link_ids))
                or any(not _strict_int(link_id) or link_id < 0 for link_id in link_ids)
                or not isinstance(item.get("localized_name"), str)
                or not isinstance(item.get("label"), str)
                or not isinstance(item.get("pos"), list)
                or len(item["pos"]) != 2
                or not all(_strict_int(value) for value in item["pos"])
            ):
                return reject("invalid_graph_binding")

    root_nodes = workflow.get("nodes")
    root_links = workflow.get("links")
    if not isinstance(root_nodes, list) or len(root_nodes) != 1 or not isinstance(root_links, list):
        return reject("invalid_graph_binding")
    if root_links:
        return reject("invalid_link")
    root = root_nodes[0]
    if not isinstance(root, Mapping):
        return reject("invalid_graph_binding")
    root_allowed = frozenset(
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
    root_error = _member_error(root, root_allowed)
    if root_error:
        return _rejected(kind, policy, root_error, "open_versioned_workflow")
    if type(root.get("id")) is not int or root["id"] < 0 or root.get("type") != definition["id"]:
        return reject("invalid_graph_binding")
    for field in ("flags", "properties"):
        value = root.get(field)
        if not isinstance(value, Mapping):
            return reject("invalid_graph_binding")
        error = _member_error(value, frozenset())
        if error:
            return _rejected(kind, policy, error, "open_versioned_workflow")
    root_inputs = root.get("inputs")
    root_outputs = root.get("outputs")
    if not isinstance(root_inputs, list) or not isinstance(root_outputs, list):
        return reject("invalid_graph_binding")
    for field, entries, expected in (
        ("inputs", root_inputs, expected_ports["inputs"]),
        ("outputs", root_outputs, expected_ports["outputs"]),
    ):
        if len(entries) != len(expected):
            return reject("invalid_graph_binding")
        for item, (expected_name, expected_type) in zip(entries, expected, strict=True):
            if not isinstance(item, Mapping):
                return reject("invalid_graph_binding")
            error = _member_error(
                item,
                frozenset(
                    {"label", "name", "type", "widget", "link" if field == "inputs" else "links"}
                ),
            )
            if error:
                return _rejected(kind, policy, error, "open_versioned_workflow")
            if item.get("name") != expected_name or item.get("type") != expected_type:
                return reject("invalid_graph_binding")
            if field == "inputs":
                widget = item.get("widget")
                if item.get("link") is not None:
                    return reject("invalid_graph_binding")
                if widget is not None:
                    if not isinstance(widget, Mapping):
                        return reject("invalid_graph_binding")
                    widget_error = _member_error(widget, frozenset({"name"}))
                    if widget_error:
                        return _rejected(kind, policy, widget_error, "open_versioned_workflow")
                    if widget.get("name") != expected_name:
                        return reject("invalid_graph_binding")
            elif item.get("links") != []:
                return reject("invalid_link")
    if not isinstance(root.get("widgets_values"), list) or not isinstance(root.get("title"), str):
        return reject("invalid_fixture_contract")
    return None


def _validate_subgraph_node_ports(
    nodes: list[Mapping[str, object]],
    expanded_fixture: str,
    kind: str,
    policy: str,
) -> WorkflowMigrationAudit | None:
    """Reject hidden, unbound ports and widget payloads in canonical Subgraph stages."""

    reference = expanded_fixture.endswith("reference.json")
    expected: dict[str, tuple[list[tuple[str, str, bool]], list[tuple[str, str]]]] = {
        "comfyui_h3_context.H3Context.Request": (
            [
                ("task_mode", "COMBO", True),
                ("user_intent", "STRING", True),
                ("duration_seconds", "FLOAT", True),
            ],
            [("request", "H3_CONTEXT_REQUEST")],
        ),
        "comfyui_h3_context.H3Context.Plan": (
            [("request", "H3_CONTEXT_REQUEST", False)]
            + ([("reference_registry", "H3_REFERENCE_REGISTRY", False)] if reference else []),
            [("plan", "H3_CONTEXT_PLAN"), ("report", "H3_CONTEXT_REPORT")],
        ),
        "comfyui_h3_context.H3Context.Compiler": (
            [("plan", "H3_CONTEXT_PLAN", False)],
            [
                ("prompt", "H3_PROMPT_STRING"),
                ("report", "H3_CONTEXT_REPORT"),
                ("prompt_document", "H3_PROMPT_DOCUMENT"),
            ],
        ),
        "comfyui_h3_context.H3Context.Validator": (
            [("plan", "H3_CONTEXT_PLAN", False), ("prompt_document", "H3_PROMPT_DOCUMENT", False)],
            [("validation", "H3_VALIDATION_RESULT"), ("validated_report", "H3_CONTEXT_REPORT")],
        ),
        "comfyui_h3_context.H3Context.NativeH3Adapter": (
            [("report", "H3_CONTEXT_REPORT", False)],
            [("prompt", "H3_PROMPT_STRING"), ("native_h3_wiring", "H3_NATIVE_H3_WIRING")],
        ),
        "comfyui_h3_context.H3Context.ProductShell": (
            [
                ("report", "H3_CONTEXT_REPORT", False),
                ("native_h3_wiring", "H3_NATIVE_H3_WIRING", False),
            ],
            [("prompt", "STRING"), ("product_shell", "H3_PRODUCT_SHELL")],
        ),
        "comfyui_h3_context.H3Context.Preview": (
            [("report", "H3_CONTEXT_REPORT", False)],
            [("prompt", "H3_PROMPT_STRING"), ("preview", "H3_CONTEXT_PREVIEW")],
        ),
        "comfyui_h3_context.H3Context.ReferenceRegistry": (
            [
                ("images.image0", "IMAGE", False),
                ("images.image1", "IMAGE", False),
                ("videos.video0", "VIDEO", False),
                ("audios.audio0", "AUDIO", False),
            ],
            [("reference_registry", "H3_REFERENCE_REGISTRY")],
        ),
        "GetVideoComponents": (
            [("video", "VIDEO", False)],
            [("images", "IMAGE"), ("audio", "AUDIO")],
        ),
        "MiniMaxH3ImageToVideo": (
            [
                ("prompt", "STRING", False),
                ("width", "INT", True),
                ("height", "INT", True),
                ("length", "INT", True),
            ],
            [],
        ),
        "MiniMaxH3ReferenceToVideo": (
            [
                ("prompt", "STRING", False),
                ("width", "INT", True),
                ("height", "INT", True),
                ("length", "INT", True),
                ("ref_image_size", "COMBO", True),
                ("ref_images.image0", "IMAGE", False),
                ("ref_images.image1", "IMAGE", False),
                ("ref_videos.video0", "IMAGE", False),
                ("ref_audios.audio0", "AUDIO", False),
            ],
            [],
        ),
    }
    for node in nodes:
        node_type = node.get("type")
        if not isinstance(node_type, str) or node_type not in expected:
            return _rejected(kind, policy, "unknown_node_type", "open_versioned_workflow")
        if "title" in node:
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
        inputs_expected, outputs_expected = expected[node_type]
        inputs = node.get("inputs")
        outputs = node.get("outputs")
        if not isinstance(inputs, list) or not isinstance(outputs, list):
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        if [(item.get("name"), item.get("type")) for item in inputs] != [
            (name, type_name) for name, type_name, _ in inputs_expected
        ] or [(item.get("name"), item.get("type")) for item in outputs] != outputs_expected:
            return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
        for item, (_name, _type_name, has_widget) in zip(inputs, inputs_expected, strict=True):
            if not isinstance(item, Mapping):
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
            allowed = {"name", "type", "link"}
            if has_widget:
                allowed.add("widget")
            error = _member_error(item, frozenset(allowed))
            if error:
                return _rejected(kind, policy, error, "open_versioned_workflow")
            if "link" not in item:
                return _rejected(kind, policy, "invalid_link", "rebuild_canonical_graph")
            widget = item.get("widget")
            if has_widget:
                if not isinstance(widget, Mapping):
                    return _rejected(
                        kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                    )
                widget_error = _member_error(widget, frozenset({"name"}))
                if widget_error:
                    return _rejected(kind, policy, widget_error, "open_versioned_workflow")
                if widget.get("name") != item.get("name"):
                    return _rejected(
                        kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                    )
            elif widget is not None:
                return _rejected(
                    kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                )
        for item, (_name, _type_name) in zip(outputs, outputs_expected, strict=True):
            if not isinstance(item, Mapping):
                return _rejected(kind, policy, "invalid_graph_binding", "rebuild_canonical_graph")
            error = _member_error(item, frozenset({"name", "type", "links"}))
            if error:
                return _rejected(kind, policy, error, "open_versioned_workflow")
        widgets = node.get("widgets_values")
        if node_type == "comfyui_h3_context.H3Context.Request":
            expected_mode = "ref2va" if reference else "t2va"
            if (
                not isinstance(widgets, list)
                or len(widgets) != 3
                or widgets[0] != expected_mode
                or not isinstance(widgets[1], str)
                or type(widgets[2]) is not int
            ):
                return _rejected(
                    kind, policy, "invalid_fixture_contract", "open_versioned_workflow"
                )
        elif node_type == "MiniMaxH3ImageToVideo":
            if (
                not isinstance(widgets, list)
                or len(widgets) != 3
                or not all(type(value) is int for value in widgets)
            ):
                return _rejected(kind, policy, "missing_native_input", "restore_versioned_fixture")
        elif node_type == "MiniMaxH3ReferenceToVideo":
            if (
                not isinstance(widgets, list)
                or len(widgets) != 4
                or not all(type(value) is int for value in widgets[:3])
                or widgets[3] not in {"match", "max"}
            ):
                return _rejected(kind, policy, "invalid_native_enum", "restore_versioned_fixture")
        elif widgets != []:
            return _rejected(kind, policy, "invalid_fixture_contract", "open_versioned_workflow")
    return None
