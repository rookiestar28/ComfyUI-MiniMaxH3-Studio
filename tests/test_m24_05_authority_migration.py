"""An old positive planning identity cannot materialize a changed guide contract."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from test_directive_semantics import copy_event, request
from test_full_rendering import full_plan
from test_m26_03_context_materializer import _proposal, _source
from test_task_mode_retention_classifier import (
    _asset,
    _mode,
    _registry,
    _retention,
    _task,
)

from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.adapters.production_context_materializer import (
    CanonicalProductionMaterializer,
)
from comfyui_h3_context.core import (
    OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
    OFFICIAL_H3_GUIDE_REVISION,
    AssetRole,
    AudioRetentionMarker,
    FullReferenceTaskType,
    MediaKind,
    RetentionDomain,
    TaskMode,
    TaskModeRetentionRequest,
    VisualRetentionMarker,
    build_official_source_profile_binding,
    build_task_mode_retention_report,
    render_full_reference_prompt,
    render_profiled_prompt,
    resolve_reference_directives,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.production_import import ProductionImportError
from comfyui_h3_context.core.semantic_graph_comparator import (
    SemanticGraphError,
    SemanticSourceKind,
    parse_legacy_semantic_prompt_v1,
    parse_semantic_prompt,
    validate_current_semantic_graph_wire,
    validate_semantic_graph_wire,
)
from comfyui_h3_context.core.source_profiled_prompt import (
    validate_source_profiled_prompt_wire,
)

ROOT = Path(__file__).resolve().parents[1]


def _v2_schema(name: str) -> dict[str, object]:
    value = json.loads((ROOT / "governance" / "contracts" / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    Draft202012Validator.check_schema(value)
    return value


def _assert_schema_accepts(name: str, wire: dict[str, object]) -> None:
    errors = list(Draft202012Validator(_v2_schema(name)).iter_errors(wire))
    assert errors == []


def test_pre_scope_planning_identity_is_refused_before_scratch_materialization() -> None:
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    source = _source(sidebar)
    context, proposal = _proposal(source, parts=(8,))
    old_graph = source.report.plan.intent_graph.to_wire()
    old_graph.pop("schema")
    segments = old_graph["segments"]
    assert isinstance(segments, list)
    for segment in segments:
        segment.pop("development")
    relations = old_graph["retention"]
    assert isinstance(relations, list)
    for relation in relations:
        relation.pop("scope")
    old_context = replace(context, source_intent_graph_fingerprint=canonical_fingerprint(old_graph))
    before = set(sidebar._entries)
    materialize = CanonicalProductionMaterializer(source, workspace_registry=sidebar)
    with pytest.raises(ProductionImportError, match="segment_context_proposal_mismatch"):
        materialize(old_context, proposal, proposal.segments[0])
    assert set(sidebar._entries) == before
    claim = materialize(context, proposal, proposal.segments[0])
    assert claim.report.plan.intent_graph.to_wire()["schema"] == "h3.context.intent_graph.v2"
    claim.release()


def test_current_typed_wires_match_published_v2_schemas() -> None:
    plan = full_plan()
    prompt = render_full_reference_prompt(plan).text
    semantic_graph = parse_semantic_prompt(
        prompt,
        plan.request.profile,
        TaskMode.REF2VA,
        SemanticSourceKind.LOCAL,
        "local.m24.schema-fixture",
    )
    semantic_wire = semantic_graph.to_wire()
    _assert_schema_accepts("semantic_prompt_graph_v2.schema.json", semantic_wire)
    assert validate_current_semantic_graph_wire(semantic_wire) == semantic_graph

    binding = build_official_source_profile_binding(
        plan.request.profile,
        observed_revision=OFFICIAL_H3_GUIDE_REVISION,
        observed_digest=OFFICIAL_H3_FULL_REFERENCE_GUIDE_DIGEST,
    )
    source_profiled_wire = render_profiled_prompt(plan, binding).to_wire()
    _assert_schema_accepts("source_profiled_prompt_v2.schema.json", source_profiled_wire)
    validate_source_profiled_prompt_wire(source_profiled_wire)

    directive_wire = resolve_reference_directives(request(copy_event())).to_wire()
    _assert_schema_accepts("reference_directives_v2.schema.json", directive_wire)

    registry = _registry(
        _asset("picture", MediaKind.IMAGE, AssetRole.REFERENCE, 1),
        _asset("audio", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 2),
    )
    classifier_wire = build_task_mode_retention_report(
        TaskModeRetentionRequest(
            TaskMode.REF2VA,
            registry,
            mode_evidence=(_mode(TaskMode.REF2VA),),
            task_type_evidence=(
                _task(
                    FullReferenceTaskType.REFERENCE_GENERATION,
                    source_asset_ids=("picture",),
                ),
            ),
            retention_evidence=(
                _retention(
                    RetentionDomain.VISUAL,
                    VisualRetentionMarker.FULLY_PRESERVED,
                    ("picture",),
                    "subject-1",
                ),
                _retention(
                    RetentionDomain.AUDIO,
                    AudioRetentionMarker.REFERENCE,
                    ("audio",),
                    "audio-1",
                ),
            ),
        )
    ).to_wire()
    _assert_schema_accepts("task_mode_retention_classifier_v2.schema.json", classifier_wire)


def test_legacy_semantic_wire_cannot_be_retagged_as_current_or_drop_scope() -> None:
    plan = full_plan()
    prompt = render_full_reference_prompt(plan).text
    legacy = parse_legacy_semantic_prompt_v1(
        prompt,
        plan.request.profile,
        TaskMode.REF2VA,
        SemanticSourceKind.LOCAL,
        "local.m24.legacy-schema-fixture",
    )
    legacy_wire = legacy.to_wire()
    schema = Draft202012Validator(_v2_schema("semantic_prompt_graph_v2.schema.json"))

    assert list(schema.iter_errors(legacy_wire))
    with pytest.raises(SemanticGraphError, match="legacy semantic graph"):
        validate_current_semantic_graph_wire(legacy_wire)

    retagged = copy.deepcopy(legacy_wire)
    retagged["schema"] = "h3.prompt.semantic_graph.v2"
    assert list(schema.iter_errors(retagged))
    with pytest.raises(SemanticGraphError, match="not fully source-derived"):
        validate_semantic_graph_wire(retagged)

    current_wire = parse_semantic_prompt(
        prompt,
        plan.request.profile,
        TaskMode.REF2VA,
        SemanticSourceKind.LOCAL,
        "local.m24.current-schema-fixture",
    ).to_wire()
    relations = current_wire["relations"]
    assert isinstance(relations, list)
    assert relations
    first_relation = relations[0]
    assert isinstance(first_relation, dict)
    first_relation.pop("retention_scope")
    assert list(schema.iter_errors(current_wire))
