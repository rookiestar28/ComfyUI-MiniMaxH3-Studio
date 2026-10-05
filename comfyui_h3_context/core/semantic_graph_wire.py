"""Reading the wire forms, and refusing everything that is not exactly one of them.

Every function here treats its input as untrusted: closed member sets, exact JSON member types, no
duplicate object keys, no non-finite numbers, and a byte ceiling before parsing rather than after.

This is the outermost layer and the only one an untrusted payload reaches first.
"""

from __future__ import annotations

import json
import re
from math import isfinite
from typing import cast

from .contracts import (
    ProfileIdentity,
    PromptProfile,
    SchemaVersion,
    TaskMode,
)
from .semantic_graph_evaluation import (
    ComparatorEvaluationCase,
    FrozenEvaluationDesign,
    GuideGolden,
    SemanticEvaluationBundle,
    build_comparator_evaluation_case,
    build_semantic_evaluation_bundle,
)
from .semantic_graph_model import (
    SemanticPromptGraph,
    parse_legacy_semantic_prompt_v1,
    parse_semantic_prompt,
)
from .semantic_graph_primitives import (
    FROZEN_GUIDE_SUPPORT,
    FROZEN_NON_P0_SUPPORT,
    FROZEN_P0_SUPPORT,
    LEGACY_SEMANTIC_GRAPH_SCHEMA,
    MAX_EVALUATION_JSON_BYTES,
    MAX_EVALUATION_SOURCE_TEXT,
    SEMANTIC_EVALUATION_SCHEMA,
    SEMANTIC_GRAPH_SCHEMA,
    EvaluationPartition,
    SemanticDiffOutcome,
    SemanticGraphError,
    SemanticSourceKind,
    _text,
)


def validate_semantic_graph_wire(value: object) -> SemanticPromptGraph:
    """Regenerate a portable graph and reject every caller-declared derived drift."""

    if type(value) is not dict:
        raise SemanticGraphError("semantic graph wire must be a closed object")
    _assert_exact_json_member_types(value)
    expected_keys = {
        "schema",
        "source_text",
        "provenance",
        "nodes",
        "relations",
        "uncertainties",
        "observed_rules",
        "hypotheses",
        "unknowns",
        "graph_fingerprint",
    }
    if set(value) != expected_keys:
        raise SemanticGraphError("semantic graph wire members are not closed")
    provenance = value.get("provenance")
    if type(provenance) is not dict or set(provenance) != {
        "source_kind",
        "source_id",
        "profile",
        "task_mode",
        "source_fingerprint",
        "authority_revision",
        "authority_digest",
    }:
        raise SemanticGraphError("semantic graph provenance wire members are not closed")
    profile = provenance.get("profile")
    if type(profile) is not dict or set(profile) != {"name", "version"}:
        raise SemanticGraphError("semantic graph profile wire members are not closed")
    try:
        identity = ProfileIdentity(
            PromptProfile(profile["name"]),
            SchemaVersion.from_wire(profile["version"]),
        )
        task_mode = TaskMode(provenance["task_mode"])
        source_kind = SemanticSourceKind(provenance["source_kind"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SemanticGraphError("semantic graph wire has an invalid closed enum") from exc
    source_text = value["source_text"]
    source_id = provenance["source_id"]
    if type(source_text) is not str or type(source_id) is not str:
        raise SemanticGraphError("semantic graph wire source identity is invalid")
    authority_revision = provenance["authority_revision"]
    authority_digest = provenance["authority_digest"]
    if authority_revision is not None and type(authority_revision) is not str:
        raise SemanticGraphError("semantic authority revision is invalid")
    if authority_digest is not None and type(authority_digest) is not str:
        raise SemanticGraphError("semantic authority digest is invalid")
    schema = value["schema"]
    if schema == SEMANTIC_GRAPH_SCHEMA:
        parser = parse_semantic_prompt
    elif schema == LEGACY_SEMANTIC_GRAPH_SCHEMA:
        parser = parse_legacy_semantic_prompt_v1
    else:
        raise SemanticGraphError("unsupported semantic graph schema")
    graph = parser(
        source_text,
        identity,
        task_mode,
        source_kind,
        source_id,
        authority_revision,
        authority_digest,
    )
    if graph.to_wire() != value:
        raise SemanticGraphError("semantic graph wire is not fully source-derived")
    return graph


def validate_current_semantic_graph_wire(value: object) -> SemanticPromptGraph:
    """Read current v2 authority and reject inspectable legacy retention ambiguity."""

    graph = validate_semantic_graph_wire(value)
    if graph.schema != SEMANTIC_GRAPH_SCHEMA:
        raise SemanticGraphError("legacy semantic graph cannot authorize current behavior")
    if any(
        item.code
        in {
            "invalid.retention_marker_domain",
            "legacy.retention_scope_unspecified",
        }
        for item in graph.uncertainties
    ):
        raise SemanticGraphError("semantic graph retention scope is not current authority")
    return graph


def _closed(value: object, keys: set[str], field: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise SemanticGraphError(f"{field} members are not closed")
    return value


def _assert_exact_json_member_types(value: object) -> None:
    # CRITICAL: close the Python mapping boundary before enum conversion or equality checks.
    pending: list[tuple[object, bool]] = [(value, False)]
    active_containers: set[int] = set()
    while pending:
        member, leaving = pending.pop()
        member_type = type(member)
        if member_type is dict or member_type is list:
            identity = id(member)
            if leaving:
                active_containers.remove(identity)
                continue
            if identity in active_containers:
                raise SemanticGraphError("semantic wire values require exact JSON member types")
            active_containers.add(identity)
            pending.append((member, True))
            if member_type is dict:
                for key, nested in cast(dict[object, object], member).items():
                    if type(key) is not str:
                        raise SemanticGraphError(
                            "semantic wire values require exact JSON member types"
                        )
                    pending.append((nested, False))
            else:
                pending.extend((nested, False) for nested in cast(list[object], member))
            continue
        if member is None or member_type is str or member_type is bool or member_type is int:
            continue
        if member_type is float and isfinite(cast(float, member)):
            continue
        raise SemanticGraphError("semantic wire values require exact JSON member types")


def _parse_guide(value: object) -> GuideGolden:
    wire = _closed(value, {"case_id", "source_cluster", "graph", "round_trip"}, "guide")
    if wire["round_trip"] is not True:
        raise SemanticGraphError("guide round_trip must be true")
    case_id = wire["case_id"]
    source_cluster = wire["source_cluster"]
    if type(case_id) is not str or type(source_cluster) is not str:
        raise SemanticGraphError("guide identity is invalid")
    return GuideGolden(
        case_id,
        source_cluster,
        validate_semantic_graph_wire(wire["graph"]),
    )


def _parse_evaluation_case(value: object) -> ComparatorEvaluationCase:
    wire = _closed(
        value,
        {
            "case_id",
            "source_cluster",
            "seed",
            "partition",
            "expected_outcome",
            "expected_material_difference",
            "local_graph",
            "reference_graph",
            "comparison",
        },
        "evaluation case",
    )
    try:
        partition = EvaluationPartition(wire["partition"])
        expected = SemanticDiffOutcome(wire["expected_outcome"])
    except (TypeError, ValueError) as exc:
        raise SemanticGraphError("evaluation case has an invalid closed enum") from exc
    case_id = wire["case_id"]
    source_cluster = wire["source_cluster"]
    seed = wire["seed"]
    material = wire["expected_material_difference"]
    if (
        type(case_id) is not str
        or type(source_cluster) is not str
        or type(seed) is not int
        or type(material) is not bool
    ):
        raise SemanticGraphError("evaluation case primitive fields are invalid")
    case = build_comparator_evaluation_case(
        case_id,
        source_cluster,
        seed,
        partition,
        expected,
        material,
        validate_semantic_graph_wire(wire["local_graph"]),
        validate_semantic_graph_wire(wire["reference_graph"]),
    )
    if case.comparison.to_wire() != wire["comparison"]:
        raise SemanticGraphError("evaluation comparison wire is not comparator-derived")
    return case


def _assert_public_fixture_source(value: object) -> None:
    if type(value) is not str or not value or len(value) > MAX_EVALUATION_SOURCE_TEXT:
        raise SemanticGraphError("evaluation contains unsafe public fixture source")
    folded = value.casefold()
    sensitive = re.search(
        r"\b(?:api[_-]?key|credential|password|bearer|authorization|cookie|signed[_-]?url)\b",
        folded,
    )
    locator = (
        re.search(r"(?:https?|file)://", folded)
        or re.search(r"[a-z]:\\", folded)
        or re.search(r"(?:^|[\s\"'])(?:/home/|/users/|/tmp/|\\\\)", folded)
    )
    if sensitive is not None or locator is not None:
        raise SemanticGraphError("evaluation contains unsafe public fixture source")


def _precheck_evaluation_sources(wire: dict[str, object]) -> None:
    guides = wire["guide_goldens"]
    p0_cases = wire["p0_cases"]
    non_p0_cases = wire["non_p0_cases"]
    if type(guides) is not list or type(p0_cases) is not list or type(non_p0_cases) is not list:
        raise SemanticGraphError("evaluation support arrays are invalid")
    for item in guides:
        if type(item) is not dict or type(item.get("graph")) is not dict:
            raise SemanticGraphError("guide graph wire is invalid")
        _assert_public_fixture_source(item["graph"].get("source_text"))
    for item in p0_cases + non_p0_cases:
        if type(item) is not dict:
            raise SemanticGraphError("evaluation case wire is invalid")
        for key in ("local_graph", "reference_graph"):
            graph = item.get(key)
            if type(graph) is not dict:
                raise SemanticGraphError("evaluation graph wire is invalid")
            _assert_public_fixture_source(graph.get("source_text"))


def validate_semantic_evaluation_wire(value: object) -> SemanticEvaluationBundle:
    if type(value) is not dict:
        raise SemanticGraphError("evaluation bundle members are not closed")
    _assert_exact_json_member_types(value)
    wire = _closed(
        value,
        {
            "schema",
            "design",
            "guide_goldens",
            "p0_cases",
            "non_p0_cases",
            "report",
            "official_profile",
            "bundle_fingerprint",
        },
        "evaluation bundle",
    )
    if wire["schema"] != SEMANTIC_EVALUATION_SCHEMA:
        raise SemanticGraphError("unsupported semantic evaluation schema")
    if wire["design"] != FrozenEvaluationDesign().to_wire():
        raise SemanticGraphError("evaluation design differs from preregistration")
    for key, count in (
        ("guide_goldens", FROZEN_GUIDE_SUPPORT),
        ("p0_cases", FROZEN_P0_SUPPORT),
        ("non_p0_cases", FROZEN_NON_P0_SUPPORT),
    ):
        support = wire[key]
        if type(support) is not list or len(support) != count:
            raise SemanticGraphError(f"{key} support differs from preregistration")
    _precheck_evaluation_sources(wire)
    guide_wires = cast(list[object], wire["guide_goldens"])
    p0_wires = cast(list[object], wire["p0_cases"])
    non_p0_wires = cast(list[object], wire["non_p0_cases"])
    guides = tuple(_parse_guide(item) for item in guide_wires)
    p0_cases = tuple(_parse_evaluation_case(item) for item in p0_wires)
    non_p0_cases = tuple(_parse_evaluation_case(item) for item in non_p0_wires)
    bundle = build_semantic_evaluation_bundle(guides, p0_cases, non_p0_cases)
    if bundle.to_wire() != wire:
        raise SemanticGraphError("evaluation bundle wire is not fully support-derived")
    return bundle


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticGraphError("semantic evaluation JSON contains a duplicate member")
        result[key] = value
    return result


class _NonFiniteJSONError(Exception):
    pass


def _reject_non_finite_json(value: str) -> object:
    raise _NonFiniteJSONError(f"non-finite JSON number is forbidden: {value}")


def decode_semantic_evaluation_json(text: str) -> SemanticEvaluationBundle:
    source = _text(text, "semantic evaluation JSON", MAX_EVALUATION_JSON_BYTES)
    if len(source.encode("utf-8")) > MAX_EVALUATION_JSON_BYTES:
        raise SemanticGraphError("semantic evaluation JSON exceeds the byte limit")
    try:
        value = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite_json,
        )
    # CRITICAL: preserve duplicate-member rejection while normalizing parser resource failures.
    except SemanticGraphError:
        raise
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
        RecursionError,
        _NonFiniteJSONError,
    ) as exc:
        raise SemanticGraphError("semantic evaluation JSON is invalid") from exc
    return validate_semantic_evaluation_wire(value)
