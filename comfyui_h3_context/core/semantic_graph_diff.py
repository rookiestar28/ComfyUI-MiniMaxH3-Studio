"""Comparing two semantic graphs, and saying exactly how they differ.

Owns the outcome of a comparison -- `SemanticComparison` and the findings inside it -- and the two
entry points that produce one, from graphs or from prompt text.

The outcome priority lives in the primitives layer rather than here because the evaluation layer
ranks by it too, and one ranking is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import (
    ProfileIdentity,
    TaskMode,
)
from .errors import PromptParseError
from .semantic_graph_model import (
    SemanticNode,
    SemanticPromptGraph,
    SemanticRelation,
    parse_semantic_prompt,
)
from .semantic_graph_primitives import (
    _OUTCOME_PRIORITY,
    MAX_SEMANTIC_ITEMS,
    MAX_SEMANTIC_TEXT,
    SemanticDiffOutcome,
    SemanticGraphError,
    SemanticNodeKind,
    SemanticRelationKind,
    SemanticSourceKind,
    _enum,
    _fingerprint,
    _identifier,
    _text,
)


@dataclass(frozen=True, slots=True)
class SemanticDiffFinding:
    finding_id: str
    outcome: SemanticDiffOutcome
    semantic_key: str
    local_value: str | None
    reference_value: str | None

    def __post_init__(self) -> None:
        _identifier(self.finding_id, "semantic finding_id")
        _enum(self.outcome, SemanticDiffOutcome, "semantic diff outcome")
        _text(self.semantic_key, "semantic diff key", 256)
        for value, field in (
            (self.local_value, "semantic diff local_value"),
            (self.reference_value, "semantic diff reference_value"),
        ):
            if value is not None:
                _text(value, field, MAX_SEMANTIC_TEXT)

    def to_wire(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "outcome": self.outcome.value,
            "semantic_key": self.semantic_key,
            "local_value": self.local_value,
            "reference_value": self.reference_value,
        }


@dataclass(frozen=True, slots=True, init=False)
class SemanticComparison:
    primary_outcome: SemanticDiffOutcome
    findings: tuple[SemanticDiffFinding, ...]
    local_graph_fingerprint: str | None
    reference_graph_fingerprint: str | None
    comparator_version: str = "1.0.0"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        # CRITICAL: comparison results must stay comparator-owned; public init can forge authority.
        raise SemanticGraphError("semantic comparison construction is comparator-owned")

    def __post_init__(self) -> None:
        _enum(self.primary_outcome, SemanticDiffOutcome, "semantic primary outcome")
        if type(self.findings) is not tuple or not self.findings:
            raise SemanticGraphError("semantic comparison requires bounded findings")
        if len(self.findings) > MAX_SEMANTIC_ITEMS or not all(
            type(item) is SemanticDiffFinding for item in self.findings
        ):
            raise SemanticGraphError("semantic comparison findings are invalid")
        expected = max(
            (finding.outcome for finding in self.findings),
            key=_OUTCOME_PRIORITY.__getitem__,
        )
        if self.primary_outcome is not expected:
            raise SemanticGraphError("semantic primary outcome differs from P0 precedence")
        for value in (self.local_graph_fingerprint, self.reference_graph_fingerprint):
            if value is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                raise SemanticGraphError("semantic comparison graph fingerprint is invalid")
        if self.comparator_version != "1.0.0":
            raise SemanticGraphError("unsupported semantic comparator version")

    @property
    def material_difference(self) -> bool:
        return self.primary_outcome not in {
            SemanticDiffOutcome.EXACT,
            SemanticDiffOutcome.COMPATIBLE,
        }

    def to_wire(self) -> dict[str, object]:
        return {
            "comparator_version": self.comparator_version,
            "primary_outcome": self.primary_outcome.value,
            "material_difference": self.material_difference,
            "findings": [finding.to_wire() for finding in self.findings],
            "local_graph_fingerprint": self.local_graph_fingerprint,
            "reference_graph_fingerprint": self.reference_graph_fingerprint,
        }


def _derive_semantic_comparison(
    primary_outcome: SemanticDiffOutcome,
    findings: tuple[SemanticDiffFinding, ...],
    local_graph_fingerprint: str | None,
    reference_graph_fingerprint: str | None,
) -> SemanticComparison:
    comparison = object.__new__(SemanticComparison)
    object.__setattr__(comparison, "primary_outcome", primary_outcome)
    object.__setattr__(comparison, "findings", findings)
    object.__setattr__(comparison, "local_graph_fingerprint", local_graph_fingerprint)
    object.__setattr__(comparison, "reference_graph_fingerprint", reference_graph_fingerprint)
    object.__setattr__(comparison, "comparator_version", "1.0.0")
    comparison.__post_init__()
    return comparison


def _finding(
    findings: list[SemanticDiffFinding],
    outcome: SemanticDiffOutcome,
    semantic_key: str,
    local_value: str | None,
    reference_value: str | None,
) -> None:
    findings.append(
        SemanticDiffFinding(
            f"finding.{len(findings) + 1}",
            outcome,
            semantic_key,
            local_value,
            reference_value,
        )
    )


def _relations(
    graph: SemanticPromptGraph, kind: SemanticRelationKind
) -> dict[str, SemanticRelation]:
    values: dict[str, SemanticRelation] = {}
    for relation in graph.relations:
        if relation.kind is kind:
            values[f"{relation.source_id}->{relation.target_id}"] = relation
    return values


def _node_values(graph: SemanticPromptGraph, kind: SemanticNodeKind) -> dict[str, SemanticNode]:
    return {node.node_id: node for node in graph.nodes if node.kind is kind}


def _ordered_ids(graph: SemanticPromptGraph, kind: SemanticNodeKind) -> tuple[str, ...]:
    return tuple(
        node.node_id
        for node in sorted(
            (node for node in graph.nodes if node.kind is kind),
            key=lambda item: (item.anchor.start, item.node_id),
        )
    )


def _normalized_words(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))


def _comparison(
    findings: list[SemanticDiffFinding],
    local_fingerprint: str | None,
    reference_fingerprint: str | None,
) -> SemanticComparison:
    primary = max(
        (finding.outcome for finding in findings),
        key=_OUTCOME_PRIORITY.__getitem__,
    )
    return _derive_semantic_comparison(
        primary,
        tuple(findings),
        local_fingerprint,
        reference_fingerprint,
    )


def compare_semantic_graphs(
    local: SemanticPromptGraph,
    reference: SemanticPromptGraph,
) -> SemanticComparison:
    """Compare two regenerated graphs with explicit P0-first deterministic rules."""

    if type(local) is not SemanticPromptGraph or type(reference) is not SemanticPromptGraph:
        raise SemanticGraphError("semantic comparison requires exact SemanticPromptGraph values")
    # GUARD: a legacy comparison cannot qualify the changed current grammar by matching text.
    # Compare it for inspection only with its own version; cross-version admission is unscorable.
    if local.schema != reference.schema:
        return _comparison(
            [
                SemanticDiffFinding(
                    "finding.1",
                    SemanticDiffOutcome.UNSCORABLE,
                    "graph.schema",
                    local.schema,
                    reference.schema,
                )
            ],
            local.graph_fingerprint,
            reference.graph_fingerprint,
        )
    if (
        local.provenance.profile != reference.provenance.profile
        or local.provenance.task_mode is not reference.provenance.task_mode
    ):
        return _comparison(
            [
                SemanticDiffFinding(
                    "finding.1",
                    SemanticDiffOutcome.UNSCORABLE,
                    "graph.profile_or_mode",
                    local.provenance.task_mode.value,
                    reference.provenance.task_mode.value,
                )
            ],
            local.graph_fingerprint,
            reference.graph_fingerprint,
        )

    findings: list[SemanticDiffFinding] = []

    local_exact = _node_values(local, SemanticNodeKind.EXACT_TEXT)
    reference_exact = _node_values(reference, SemanticNodeKind.EXACT_TEXT)
    for key in sorted(set(local_exact) | set(reference_exact)):
        local_exact_node = local_exact.get(key)
        reference_exact_node = reference_exact.get(key)
        if (
            local_exact_node is None
            or reference_exact_node is None
            or local_exact_node.value != reference_exact_node.value
        ):
            _finding(
                findings,
                SemanticDiffOutcome.HARD_MUTATION,
                key,
                None if local_exact_node is None else local_exact_node.value,
                None if reference_exact_node is None else reference_exact_node.value,
            )

    for relation_kind, outcome in (
        (SemanticRelationKind.ROLE, SemanticDiffOutcome.ROLE),
        (SemanticRelationKind.OWNERSHIP, SemanticDiffOutcome.OWNERSHIP),
    ):
        left_by_source = {
            relation.source_id: relation
            for relation in local.relations
            if relation.kind is relation_kind
        }
        right_by_source = {
            relation.source_id: relation
            for relation in reference.relations
            if relation.kind is relation_kind
        }
        for key in sorted(set(left_by_source) & set(right_by_source)):
            local_relation = left_by_source[key]
            reference_relation = right_by_source[key]
            if (local_relation.target_id, local_relation.value) != (
                reference_relation.target_id,
                reference_relation.value,
            ):
                _finding(
                    findings,
                    outcome,
                    f"relation.{relation_kind.value}.{key}",
                    f"{local_relation.target_id}:{local_relation.value}",
                    f"{reference_relation.target_id}:{reference_relation.value}",
                )

    for node_kind in (SemanticNodeKind.ASSET, SemanticNodeKind.ENTITY, SemanticNodeKind.EVENT):
        local_order = _ordered_ids(local, node_kind)
        reference_order = _ordered_ids(reference, node_kind)
        if set(local_order) == set(reference_order) and local_order != reference_order:
            _finding(
                findings,
                SemanticDiffOutcome.ORDER,
                f"order.{node_kind.value}",
                ",".join(local_order),
                ",".join(reference_order),
            )

    local_timing = _node_values(local, SemanticNodeKind.TIMING)
    reference_timing = _node_values(reference, SemanticNodeKind.TIMING)
    for key in sorted(set(local_timing) & set(reference_timing)):
        if local_timing[key].value != reference_timing[key].value:
            _finding(
                findings,
                SemanticDiffOutcome.TEMPORAL,
                key,
                local_timing[key].value,
                reference_timing[key].value,
            )

    for copy_kind in (SemanticRelationKind.COPY, SemanticRelationKind.RETAIN):
        left_relations = _relations(local, copy_kind)
        right_relations = _relations(reference, copy_kind)
        for key in sorted(set(left_relations) | set(right_relations)):
            local_copy = left_relations.get(key)
            reference_copy = right_relations.get(key)
            if (
                local_copy is None
                or reference_copy is None
                or local_copy.value != reference_copy.value
            ):
                _finding(
                    findings,
                    SemanticDiffOutcome.HARD_MUTATION,
                    f"relation.{copy_kind.value}.{key}",
                    None if local_copy is None else local_copy.value,
                    None if reference_copy is None else reference_copy.value,
                )

    structured_kinds = {
        SemanticNodeKind.ASSET,
        SemanticNodeKind.ENTITY,
        SemanticNodeKind.EVENT,
        SemanticNodeKind.TIMING,
        SemanticNodeKind.EXACT_TEXT,
        SemanticNodeKind.AUDIO_FACT,
        SemanticNodeKind.AV_FACT,
    }
    local_nodes = {node.node_id: node for node in local.nodes if node.kind in structured_kinds}
    reference_nodes = {
        node.node_id: node for node in reference.nodes if node.kind in structured_kinds
    }
    for key in sorted(set(local_nodes) - set(reference_nodes)):
        _finding(
            findings,
            SemanticDiffOutcome.LOCAL_ONLY,
            key,
            local_nodes[key].value,
            None,
        )
    for key in sorted(set(reference_nodes) - set(local_nodes)):
        _finding(
            findings,
            SemanticDiffOutcome.OFFICIAL_ONLY,
            key,
            None,
            reference_nodes[key].value,
        )

    if not findings:
        local_sections = _node_values(local, SemanticNodeKind.SECTION)
        reference_sections = _node_values(reference, SemanticNodeKind.SECTION)
        section_changed = False
        section_compatible = True
        for key in sorted(set(local_sections) | set(reference_sections)):
            left = local_sections.get(key)
            right = reference_sections.get(key)
            if left is None or right is None:
                section_changed = True
                section_compatible = False
                break
            if left.value != right.value:
                section_changed = True
                if _normalized_words(left.value) != _normalized_words(right.value):
                    section_compatible = False
        if section_changed and not section_compatible:
            _finding(
                findings,
                SemanticDiffOutcome.CONTRADICTION,
                "sections.content",
                _fingerprint([node.value for node in local_sections.values()]),
                _fingerprint([node.value for node in reference_sections.values()]),
            )
        elif local.source_text == reference.source_text:
            _finding(findings, SemanticDiffOutcome.EXACT, "graph", "exact", "exact")
        else:
            _finding(
                findings,
                SemanticDiffOutcome.COMPATIBLE,
                "graph",
                "compatible",
                "compatible",
            )

    return _comparison(findings, local.graph_fingerprint, reference.graph_fingerprint)


def compare_semantic_prompts(
    local_text: str,
    reference_text: str,
    profile: ProfileIdentity,
    task_mode: TaskMode,
    local_kind: SemanticSourceKind,
    reference_kind: SemanticSourceKind,
    local_source_id: str,
    reference_source_id: str,
    *,
    local_authority_revision: str | None = None,
    local_authority_digest: str | None = None,
    reference_authority_revision: str | None = None,
    reference_authority_digest: str | None = None,
) -> SemanticComparison:
    """Parse then compare; parse failures become content-free typed unscorable results."""

    try:
        local = parse_semantic_prompt(
            local_text,
            profile,
            task_mode,
            local_kind,
            local_source_id,
            local_authority_revision,
            local_authority_digest,
        )
        reference = parse_semantic_prompt(
            reference_text,
            profile,
            task_mode,
            reference_kind,
            reference_source_id,
            reference_authority_revision,
            reference_authority_digest,
        )
    except (PromptParseError, SemanticGraphError):
        return _comparison(
            [
                SemanticDiffFinding(
                    "finding.1",
                    SemanticDiffOutcome.UNSCORABLE,
                    "graph.parse",
                    "unscorable",
                    "unscorable",
                )
            ],
            None,
            None,
        )
    return compare_semantic_graphs(local, reference)
