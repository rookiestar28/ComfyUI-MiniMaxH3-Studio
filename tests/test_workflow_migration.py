"""M15-10 versioned workflow migration audit regressions."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.core import (
    MigrationDisposition,
    WorkflowMigrationError,
    audit_workflow_migration,
    decode_workflow_json,
)
from scripts.release_matrix import _audit_workflow_migrations

ROOT = Path(__file__).resolve().parents[1]


def _load(relative: str) -> dict[str, Any]:
    with (ROOT / relative).open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


def _legacy_prompt_bridge() -> dict[str, Any]:
    return {
        "version": 0.4,
        "nodes": [{"id": 1, "type": "legacy_assistant"}],
        "definitions": {
            "subgraphs": [
                {
                    "id": "legacy_assistant",
                    "version": 1,
                    "inputs": [],
                    "outputs": [],
                    "nodes": [
                        {
                            "id": 1,
                            "type": "comfyui_h3_context.H3Context.Compiler",
                            "inputs": [],
                            "outputs": [
                                {
                                    "name": "prompt",
                                    "type": "H3_PROMPT_STRING",
                                    "links": [7],
                                }
                            ],
                        },
                        {
                            "id": 2,
                            "type": "MiniMaxH3ImageToVideo",
                            "inputs": [{"name": "prompt", "type": "STRING", "link": 7}],
                            "outputs": [],
                        },
                    ],
                    "links": [
                        {
                            "id": 7,
                            "origin_id": 1,
                            "origin_slot": 0,
                            "target_id": 2,
                            "target_slot": 0,
                            "type": "H3_PROMPT_STRING",
                        }
                    ],
                }
            ]
        },
        "h3_context_fixture": {"schema": "h3-context-subgraph-fixture/1"},
    }


def test_current_versioned_workflows_and_assistant_subgraphs_are_accepted() -> None:
    paths = (
        "workflows/m15_09_assistant_base.json",
        "workflows/m15_09_assistant_reference.json",
        "workflows/m15_03_product_shell_base.json",
        "workflows/m15_03_product_shell_reference.json",
        "subgraphs/H3 Context Assistant - Base.json",
        "subgraphs/H3 Context Assistant - Reference.json",
        "subgraphs/H3 Product Shell Boundary.json",
    )
    for path in paths:
        result = audit_workflow_migration(_load(path))
        assert result.disposition is MigrationDisposition.ACCEPTED, path
        assert result.reason_code is None, path


def test_legacy_prompt_socket_bridge_is_rejected_without_mutation() -> None:
    workflow = _legacy_prompt_bridge()
    before = copy.deepcopy(workflow)

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "legacy_prompt_socket_to_native"
    assert result.next_action == "use_versioned_product_shell_workflow"
    assert workflow == before

    api_workflow = _load("workflows/m15_09_assistant_base.json")
    api_workflow["links"] = [
        {
            "type": "H3_PROMPT_STRING",
            "target_type": "STRING",
        }
    ]
    api_result = audit_workflow_migration(api_workflow)
    assert api_result.reason_code == "legacy_prompt_socket_to_native"


def test_grouped_or_one_based_autogrow_is_rejected() -> None:
    workflow = _load("workflows/m15_09_assistant_reference.json")
    links = workflow["expected"]["direct_media_links"]
    assert isinstance(links, list)
    links[0]["target_path"] = "ref_images.image1"

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "non_canonical_autogrow"


def test_invalid_native_enum_and_missing_required_input_are_distinct() -> None:
    invalid_enum = _load("workflows/m15_09_assistant_reference.json")
    prompt = invalid_enum["prompt"]
    assert isinstance(prompt, dict)
    prompt["9"]["inputs"]["ref_image_size"] = "legacy_first"
    enum_result = audit_workflow_migration(invalid_enum)
    assert enum_result.reason_code == "invalid_native_enum"

    missing_input = _load("workflows/m15_09_assistant_reference.json")
    prompt = missing_input["prompt"]
    assert isinstance(prompt, dict)
    del prompt["9"]["inputs"]["length"]
    missing_result = audit_workflow_migration(missing_input)
    assert missing_result.reason_code == "missing_native_input"


def test_unqualified_dynamic_native_subgraph_is_rejected() -> None:
    workflow = _load("subgraphs/H3 Context Assistant - Reference.json")
    manifest = workflow["h3_context_fixture"]
    assert isinstance(manifest, dict)
    manifest["schema"] = "h3-context-subgraph-fixture/1"

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "unsupported_dynamic_subgraph"


def test_decoder_rejects_duplicate_members_and_non_finite_numbers() -> None:
    with pytest.raises(WorkflowMigrationError, match="duplicate JSON member"):
        decode_workflow_json('{"prompt": {}, "prompt": {}}')
    with pytest.raises(WorkflowMigrationError, match="non-finite"):
        decode_workflow_json('{"prompt": {"1": {"inputs": {"x": NaN}}}}')


def test_release_matrix_binds_the_same_audit_to_current_fixtures() -> None:
    evidence = _audit_workflow_migrations()
    assert set(evidence) == {
        "product_shell_base",
        "product_shell_reference",
        "assistant_base",
        "assistant_reference",
    }
    for item in evidence.values():
        assert isinstance(item, dict)
        assert item["disposition"] == "accepted"


def test_schema_one_native_only_graph_cannot_bypass_product_shell_boundary() -> None:
    workflow = {
        "schema": "h3-context-workflow-fixture/1",
        "fixture_id": "unqualified-native-only",
        "fixture_status": "static_model_free_contract",
        "workflow_format": "comfyui_api_prompt_v1",
        "host": {"migration_policy": "versioned_product_shell"},
        "prompt": {
            "1": {
                "class_type": "MiniMaxH3ImageToVideo",
                "inputs": {"prompt": "native", "width": 512, "height": 512, "length": 16},
            }
        },
    }

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code in {"missing_product_shell_boundary", "invalid_fixture_contract"}


@pytest.mark.parametrize("member", ["token", "url", "local_path", "credentials"])
def test_unknown_or_private_top_level_members_are_rejected(member: str) -> None:
    workflow = _load("workflows/m15_09_assistant_base.json")
    workflow[member] = "not allowed"

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code in {"unknown_workflow_member", "private_workflow_field"}


def test_migration_binding_and_graph_fingerprint_are_pinned() -> None:
    missing_binding = _load("workflows/m15_09_assistant_base.json")
    expected = missing_binding["expected"]
    assert isinstance(expected, dict)
    del expected["migration_source"]
    del expected["rollback_fixture"]
    binding_result = audit_workflow_migration(missing_binding)
    assert binding_result.disposition is MigrationDisposition.REJECTED
    assert binding_result.reason_code == "missing_migration_binding"

    wrong_fingerprint = _load("workflows/m15_09_assistant_base.json")
    expected = wrong_fingerprint["expected"]
    assert isinstance(expected, dict)
    expected["graph_fingerprint_parts"] = ["sha256:" + "0" * 64]
    fingerprint_result = audit_workflow_migration(wrong_fingerprint)
    assert fingerprint_result.disposition is MigrationDisposition.REJECTED
    assert fingerprint_result.reason_code == "graph_fingerprint_mismatch"


def test_malformed_link_mapping_is_not_silently_ignored() -> None:
    workflow = _load("workflows/m15_09_assistant_base.json")
    prompt = workflow["prompt"]
    assert isinstance(prompt, dict)
    node = prompt["6"]
    assert isinstance(node, dict)
    inputs = node["inputs"]
    assert isinstance(inputs, dict)
    inputs["width"] = {"link": {"origin": "bad"}}

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_link"


def test_product_shell_and_native_prompt_edges_are_typed_and_bound() -> None:
    workflow = _load("workflows/m15_09_assistant_base.json")
    prompt = workflow["prompt"]
    assert isinstance(prompt, dict)
    native = prompt["6"]
    assert isinstance(native, dict)
    native_inputs = native["inputs"]
    assert isinstance(native_inputs, dict)
    native_inputs["prompt"] = ["4", 0]

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_graph_binding"


def test_api_link_metadata_rejects_dangling_and_foreign_edges() -> None:
    workflow = _load("workflows/m15_09_assistant_base.json")
    workflow["links"] = [
        {
            "id": 1,
            "origin_id": "missing",
            "origin_slot": 0,
            "target_id": "8",
            "target_slot": 0,
            "type": "STRING",
        }
    ]

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_link"

    typed = _load("workflows/m15_09_assistant_base.json")
    typed["links"] = [
        {
            "id": 1,
            "origin_id": "8",
            "origin_slot": 0,
            "target_id": "6",
            "target_slot": 0,
            "type": "H3_PROMPT_STRING",
        }
    ]
    typed_result = audit_workflow_migration(typed)
    assert typed_result.disposition is MigrationDisposition.REJECTED
    assert typed_result.reason_code == "invalid_graph_binding"


def test_subgraph_links_are_unique_typed_and_endpoint_bound() -> None:
    duplicate = _load("subgraphs/H3 Context Assistant - Base.json")
    definition = duplicate["definitions"]["subgraphs"][0]
    assert isinstance(definition, dict)
    links = definition["links"]
    assert isinstance(links, list)
    links.append(copy.deepcopy(links[0]))
    duplicate_result = audit_workflow_migration(duplicate)
    assert duplicate_result.disposition is MigrationDisposition.REJECTED
    assert duplicate_result.reason_code == "duplicate_link"

    dangling = _load("subgraphs/H3 Context Assistant - Base.json")
    definition = dangling["definitions"]["subgraphs"][0]
    assert isinstance(definition, dict)
    links = definition["links"]
    assert isinstance(links, list)
    links[0]["target_id"] = 999
    dangling_result = audit_workflow_migration(dangling)
    assert dangling_result.disposition is MigrationDisposition.REJECTED
    assert dangling_result.reason_code == "invalid_link"

    wrong_port = _load("subgraphs/H3 Context Assistant - Reference.json")
    definition = wrong_port["definitions"]["subgraphs"][0]
    assert isinstance(definition, dict)
    links = definition["links"]
    assert isinstance(links, list)
    next(link for link in links if link["id"] == 19)["target_slot"] = 99
    wrong_port_result = audit_workflow_migration(wrong_port)
    assert wrong_port_result.disposition is MigrationDisposition.REJECTED
    assert wrong_port_result.reason_code == "invalid_link"


def test_product_shell_wrapper_requires_a_typed_string_boundary() -> None:
    workflow = _load("subgraphs/H3 Product Shell Boundary.json")
    definition = workflow["definitions"]["subgraphs"][0]
    assert isinstance(definition, dict)
    links = definition["links"]
    assert isinstance(links, list)
    links[2]["type"] = "H3_PROMPT_STRING"

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_graph_binding"


def test_schema_one_product_shell_with_dynamic_native_is_rejected() -> None:
    workflow = _load("subgraphs/H3 Product Shell Boundary.json")
    definition = workflow["definitions"]["subgraphs"][0]
    assert isinstance(definition, dict)
    nodes = definition["nodes"]
    assert isinstance(nodes, list)
    nodes.append(
        {
            "id": 2,
            "type": "MiniMaxH3SigmaShift",
            "inputs": [
                {"name": "model", "type": "MODEL", "link": None},
                {"name": "shift_video", "type": "FLOAT", "link": None},
                {"name": "shift_audio", "type": "FLOAT", "link": None},
            ],
            "outputs": [],
            "widgets_values": [],
        }
    )

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "unsupported_dynamic_subgraph"


def test_nested_subgraph_manifest_members_are_closed() -> None:
    workflow = _load("subgraphs/H3 Context Assistant - Base.json")
    manifest = workflow["h3_context_fixture"]
    assert isinstance(manifest, dict)
    disclosure = manifest["execution_disclosure"]
    assert isinstance(disclosure, dict)
    disclosure.update({"token": "forbidden"})

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "private_workflow_field"


def test_subgraph_fingerprint_is_bound_to_the_expanded_fixture() -> None:
    workflow = _load("subgraphs/H3 Context Assistant - Base.json")
    manifest = workflow["h3_context_fixture"]
    assert isinstance(manifest, dict)
    manifest["graph_fingerprint_parts"] = ["sha256:" + "0" * 64]

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "graph_fingerprint_mismatch"


def test_product_shell_wrapper_manifest_is_closed_and_exact() -> None:
    workflow = _load("subgraphs/H3 Product Shell Boundary.json")
    manifest = workflow["h3_context_fixture"]
    assert isinstance(manifest, dict)
    manifest.pop("product_scope")

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_fixture_contract"


@pytest.mark.parametrize("field", ["token", "url", "private_path"])
def test_product_shell_definition_metadata_is_closed_and_pinned(field: str) -> None:
    workflow = _load("subgraphs/H3 Product Shell Boundary.json")
    definition = workflow["definitions"]["subgraphs"][0]
    assert isinstance(definition, dict)
    context = definition["h3_context"]
    assert isinstance(context, dict)
    context[field] = "forbidden"

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code in {"unknown_workflow_member", "private_workflow_field"}


def test_subgraph_root_wrapper_and_port_names_are_bound() -> None:
    workflow = _load("subgraphs/H3 Context Assistant - Base.json")
    root = workflow["nodes"][0]
    assert isinstance(root, dict)
    root["type"] = "foreign.subgraph"
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_graph_binding"

    workflow = _load("subgraphs/H3 Product Shell Boundary.json")
    definition = workflow["definitions"]["subgraphs"][0]
    assert isinstance(definition, dict)
    definition["inputs"][0]["name"] = "evil"
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_graph_binding"


@pytest.mark.parametrize("side", ["input", "output", "external", "duplicate"])
def test_subgraph_link_backreferences_are_complete(side: str) -> None:
    workflow = _load("subgraphs/H3 Context Assistant - Base.json")
    definition = workflow["definitions"]["subgraphs"][0]
    assert isinstance(definition, dict)
    if side == "input":
        node = next(
            node
            for node in definition["nodes"]
            if any(item.get("link") is not None for item in node.get("inputs", []))
        )
        next(item for item in node["inputs"] if item.get("link") is not None)["link"] = None
    elif side == "output":
        node = next(
            node
            for node in definition["nodes"]
            if any(item.get("links") for item in node.get("outputs", []))
        )
        next(item for item in node["outputs"] if item.get("links"))["links"] = []
    elif side == "external":
        definition["inputs"][0]["linkIds"] = []
    else:
        node = next(
            node
            for node in definition["nodes"]
            if any(item.get("links") for item in node.get("outputs", []))
        )
        links = next(item for item in node["outputs"] if item.get("links"))["links"]
        links.append(links[0])

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_link"


@pytest.mark.parametrize("mutation", ["input", "output", "title", "label"])
def test_subgraph_internal_stage_ports_and_metadata_are_closed(mutation: str) -> None:
    workflow = _load("subgraphs/H3 Context Assistant - Base.json")
    definition = workflow["definitions"]["subgraphs"][0]
    assert isinstance(definition, dict)
    request = next(
        node
        for node in definition["nodes"]
        if node["type"] == "comfyui_h3_context.H3Context.Request"
    )
    if mutation == "input":
        request["inputs"].append({"name": "private_media", "type": "IMAGE", "link": None})
    elif mutation == "output":
        request["outputs"].append({"name": "secret_output", "type": "STRING", "links": []})
    elif mutation == "title":
        request["title"] = "https://private.example"
    else:
        request["inputs"][0]["label"] = "https://private.example"

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code in {
        "invalid_fixture_contract",
        "invalid_graph_binding",
        "unknown_workflow_member",
    }


def test_subgraph_request_widget_values_remain_user_editable_but_structural() -> None:
    workflow = _load("subgraphs/H3 Context Assistant - Base.json")
    request = next(
        node
        for node in workflow["definitions"]["subgraphs"][0]["nodes"]
        if node["type"] == "comfyui_h3_context.H3Context.Request"
    )
    request["widgets_values"][1] = "A user-declared https://example.invalid reference"
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.ACCEPTED


def test_subgraph_native_shape_and_reference_widget_are_validated() -> None:
    base = _load("subgraphs/H3 Context Assistant - Base.json")
    definition = base["definitions"]["subgraphs"][0]
    native = next(node for node in definition["nodes"] if node["type"] == "MiniMaxH3ImageToVideo")
    native["inputs"] = [item for item in native["inputs"] if item["name"] != "length"]
    base_result = audit_workflow_migration(base)
    assert base_result.disposition is MigrationDisposition.REJECTED
    assert base_result.reason_code == "missing_native_input"

    reference = _load("subgraphs/H3 Context Assistant - Reference.json")
    definition = reference["definitions"]["subgraphs"][0]
    native = next(
        node for node in definition["nodes"] if node["type"] == "MiniMaxH3ReferenceToVideo"
    )
    native["widgets_values"][3] = "unsupported_enum"
    reference_result = audit_workflow_migration(reference)
    assert reference_result.disposition is MigrationDisposition.REJECTED
    assert reference_result.reason_code == "invalid_native_enum"


@pytest.mark.parametrize(
    ("field", "value"),
    (("link", 11), ("source", "evil")),
)
def test_subgraph_direct_media_metadata_is_bound_to_the_serialized_edge(
    field: str, value: object
) -> None:
    workflow = _load("subgraphs/H3 Context Assistant - Reference.json")
    manifest = workflow["h3_context_fixture"]
    assert isinstance(manifest, dict)
    direct = manifest["direct_media_links"]
    assert isinstance(direct, list)
    direct[0][field] = value

    result = audit_workflow_migration(workflow)

    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code in {"malformed_media_binding", "invalid_graph_binding"}


def test_subgraph_nested_projection_members_are_closed() -> None:
    workflow = _load("subgraphs/H3 Context Assistant - Base.json")
    manifest = workflow["h3_context_fixture"]
    assert isinstance(manifest, dict)
    projection = manifest["input_projection"]
    assert isinstance(projection, list)
    projection[0]["private_path"] = "forbidden"
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code in {"unknown_workflow_member", "private_workflow_field"}

    workflow = _load("subgraphs/H3 Context Assistant - Reference.json")
    manifest = workflow["h3_context_fixture"]
    assert isinstance(manifest, dict)
    direct = manifest["direct_media_links"]
    assert isinstance(direct, list)
    direct[0]["url"] = "https://forbidden"
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "private_workflow_field"


def test_subgraph_duplicate_stage_and_malformed_external_link_ids_fail_closed() -> None:
    duplicate = _load("subgraphs/H3 Context Assistant - Base.json")
    definition = duplicate["definitions"]["subgraphs"][0]
    request = next(
        node
        for node in definition["nodes"]
        if node["type"] == "comfyui_h3_context.H3Context.Request"
    )
    definition["nodes"].append(
        {
            "id": 99,
            "type": request["type"],
            "inputs": [],
            "outputs": [],
        }
    )
    manifest = duplicate["h3_context_fixture"]
    assert isinstance(manifest, dict)
    manifest["expanded_node_types"] = [node["type"] for node in definition["nodes"]]
    duplicate_result = audit_workflow_migration(duplicate)
    assert duplicate_result.disposition is MigrationDisposition.REJECTED
    assert duplicate_result.reason_code == "invalid_graph_binding"

    malformed = _load("subgraphs/H3 Context Assistant - Base.json")
    definition = malformed["definitions"]["subgraphs"][0]
    definition["inputs"][0]["linkIds"] = [[10]]
    malformed_result = audit_workflow_migration(malformed)
    assert malformed_result.disposition is MigrationDisposition.REJECTED
    assert malformed_result.reason_code == "invalid_link"


def test_api_optional_edges_and_pipeline_bindings_are_strict() -> None:
    workflow = _load("workflows/m15_09_assistant_base.json")
    workflow["links"] = [
        {
            "id": 1,
            "origin_id": "1",
            "origin_slot": 0,
            "target_id": "2",
            "target_slot": 0,
            "type": "STRING",
        }
    ]
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_graph_binding"

    workflow = _load("workflows/m15_09_assistant_base.json")
    workflow["links"] = [
        {
            "id": 1,
            "origin_id": 4,
            "origin_slot": 1,
            "target_id": "8",
            "target_slot": 0,
            "type": "H3_CONTEXT_REPORT",
        }
    ]
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_link"

    workflow = _load("workflows/m15_09_assistant_base.json")
    expected = workflow["expected"]
    assert isinstance(expected, dict)
    expected["pipeline_node_ids"] = [[]]
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_graph_binding"

    workflow = _load("workflows/m15_09_assistant_base.json")
    expected = workflow["expected"]
    assert isinstance(expected, dict)
    pipeline_node_ids = expected["pipeline_node_ids"]
    assert isinstance(pipeline_node_ids, list)
    expected["pipeline_node_ids"] = list(reversed(pipeline_node_ids))
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_graph_binding"

    workflow = _load("workflows/m15_09_assistant_base.json")
    workflow["native_bindings"] = [{"path_basis": "fully_qualified_zero_based", "token": "bad"}]
    result = audit_workflow_migration(workflow)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "private_workflow_field"


def test_api_projection_and_reference_ordinals_reject_bool_coercion() -> None:
    projection = _load("workflows/m15_09_assistant_base.json")
    expected = projection["expected"]
    assert isinstance(expected, dict)
    output = expected["output_projection"]
    assert isinstance(output, dict)
    output["report"][1] = False
    result = audit_workflow_migration(projection)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "invalid_graph_binding"

    reference = _load("workflows/m15_09_assistant_reference.json")
    expected = reference["expected"]
    assert isinstance(expected, dict)
    direct = expected["direct_media_links"]
    assert isinstance(direct, list)
    direct[0]["presentation_ordinal"] = True
    result = audit_workflow_migration(reference)
    assert result.disposition is MigrationDisposition.REJECTED
    assert result.reason_code == "malformed_media_binding"
