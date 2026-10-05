"""Semantic graph comparison: the aggregation surface every consumer imports.

This was one 2307-line module holding the shared vocabulary, the graph model, graph comparison, the
frozen M14-01 evaluation and the untrusted-wire readers. Those are five separate concerns and they
now have five owners, layered one way:

    primitives <- model <- diff <- evaluation <- wire

The public surface is unchanged: every name importable from here before still is, and
`tests/test_frontend_host_layering.py`'s sibling `tests/test_core_module_layering.py` asserts the
direction rather than leaving it to hold by construction.
"""

from __future__ import annotations

from .semantic_graph_diff import (
    SemanticComparison,
    SemanticDiffFinding,
    compare_semantic_graphs,
    compare_semantic_prompts,
)
from .semantic_graph_evaluation import (
    ComparatorEvaluationCase,
    ComparatorEvaluationReport,
    FrozenEvaluationDesign,
    GuideGolden,
    OfficialBehaviorProfile,
    SemanticEvaluationBundle,
    build_comparator_evaluation_case,
    build_semantic_evaluation_bundle,
    wilson_interval,
)
from .semantic_graph_model import (
    SemanticNode,
    SemanticPromptGraph,
    SemanticProvenance,
    SemanticRelation,
    SemanticSpanAnchor,
    SemanticUncertainty,
    parse_legacy_semantic_prompt_v1,
    parse_semantic_prompt,
    render_semantic_prompt,
)
from .semantic_graph_primitives import (
    FROZEN_EVALUATION_BUNDLE_FINGERPRINT,
    FROZEN_GUIDE_CLUSTER_COUNT,
    FROZEN_GUIDE_SUPPORT,
    FROZEN_INTERVAL_METHOD,
    FROZEN_INTERVAL_Z,
    FROZEN_NON_P0_CLUSTER_COUNT,
    FROZEN_NON_P0_NEGATIVE_SUPPORT,
    FROZEN_NON_P0_POSITIVE_SUPPORT,
    FROZEN_NON_P0_SUPPORT,
    FROZEN_P0_CLUSTER_COUNT,
    FROZEN_P0_SUPPORT,
    FROZEN_POINT_TARGET,
    LEGACY_SEMANTIC_GRAPH_SCHEMA,
    M14_01_TERMINAL_DISPOSITION_SHA256,
    MAX_EVALUATION_JSON_BYTES,
    MAX_SEMANTIC_ITEMS,
    MAX_SEMANTIC_TEXT,
    OFFICIAL_BEHAVIOR_PROFILE_SCHEMA,
    SEMANTIC_EVALUATION_SCHEMA,
    SEMANTIC_GRAPH_SCHEMA,
    BehaviorProfileDisposition,
    EvaluationPartition,
    SemanticDiffOutcome,
    SemanticGraphError,
    SemanticNodeKind,
    SemanticRelationKind,
    SemanticSourceKind,
)
from .semantic_graph_wire import (
    decode_semantic_evaluation_json,
    validate_current_semantic_graph_wire,
    validate_semantic_evaluation_wire,
    validate_semantic_graph_wire,
)

__all__ = [
    "BehaviorProfileDisposition",
    "ComparatorEvaluationCase",
    "ComparatorEvaluationReport",
    "EvaluationPartition",
    "FROZEN_EVALUATION_BUNDLE_FINGERPRINT",
    "FROZEN_GUIDE_CLUSTER_COUNT",
    "FROZEN_GUIDE_SUPPORT",
    "FROZEN_INTERVAL_METHOD",
    "FROZEN_INTERVAL_Z",
    "FROZEN_NON_P0_CLUSTER_COUNT",
    "FROZEN_NON_P0_NEGATIVE_SUPPORT",
    "FROZEN_NON_P0_POSITIVE_SUPPORT",
    "FROZEN_NON_P0_SUPPORT",
    "FROZEN_P0_CLUSTER_COUNT",
    "FROZEN_P0_SUPPORT",
    "FROZEN_POINT_TARGET",
    "FrozenEvaluationDesign",
    "GuideGolden",
    "M14_01_TERMINAL_DISPOSITION_SHA256",
    "MAX_EVALUATION_JSON_BYTES",
    "MAX_SEMANTIC_ITEMS",
    "MAX_SEMANTIC_TEXT",
    "OFFICIAL_BEHAVIOR_PROFILE_SCHEMA",
    "OfficialBehaviorProfile",
    "SEMANTIC_EVALUATION_SCHEMA",
    "SEMANTIC_GRAPH_SCHEMA",
    "LEGACY_SEMANTIC_GRAPH_SCHEMA",
    "SemanticComparison",
    "SemanticDiffFinding",
    "SemanticDiffOutcome",
    "SemanticEvaluationBundle",
    "SemanticGraphError",
    "SemanticNode",
    "SemanticNodeKind",
    "SemanticPromptGraph",
    "SemanticProvenance",
    "SemanticRelation",
    "SemanticRelationKind",
    "SemanticSourceKind",
    "SemanticSpanAnchor",
    "SemanticUncertainty",
    "build_comparator_evaluation_case",
    "build_semantic_evaluation_bundle",
    "compare_semantic_graphs",
    "compare_semantic_prompts",
    "decode_semantic_evaluation_json",
    "parse_semantic_prompt",
    "parse_legacy_semantic_prompt_v1",
    "render_semantic_prompt",
    "validate_semantic_evaluation_wire",
    "validate_semantic_graph_wire",
    "validate_current_semantic_graph_wire",
    "wilson_interval",
]
