"""Pure bounded browser projections for one exact semantic-proposal review.

The initial handle is content-free. Proposal summaries are constructed only for an admitted read
from process-local report/workspace/transaction authorities and are never portable authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import NoReturn, cast

from .canonical import fingerprint_context_report
from .context_reporting import ContextReport
from .segment_workspace import MultiSegmentWorkspace
from .semantic_proposal_transaction import (
    SemanticProposalTransaction,
    SemanticProposalTransactionState,
)
from .ui_projection import ExecutionCorrelation

SEMANTIC_PROPOSAL_REVIEW_HANDLE_SCHEMA = "h3.context.semantic_proposal_review_handle.v1"
SEMANTIC_PROPOSAL_REVIEW_SCHEMA = "h3.context.semantic_proposal_review.v1"
SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA = "h3.context.semantic_proposal.action_result.v1"
MAX_SEMANTIC_REVIEW_BYTES = 65_536
MAX_SEMANTIC_REVIEW_ITEMS = 64
MAX_SEMANTIC_REVIEW_TEXT = 4_096

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")
_REVIEW_ID = re.compile(r"review_[A-Za-z0-9_-]{32,96}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SENSITIVE = re.compile(
    r"(?i)(?:authorization|bearer\s|api[_-]?key|password|secret|token\s*=|sig\s*=|x-amz-)"
)
_PATH_OR_LOCATOR = re.compile(r"(?ix)(?:[\\/]|(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9+.-]*:[^\s])")
_CONSTRAINT_LABEL = re.compile(
    r"(?:subject|scene|style|action|camera|audio|event|layer):"
    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z"
)
_COLLECTION_ID_FIELDS = {
    "subjects": "subject_id",
    "scenes": "scene_id",
    "actions": "action_id",
    "cameras": "camera_id",
    "styles": "style_id",
    "audios": "audio_id",
}
_SUMMARY_FIELDS = (
    "description",
    "summary",
    "text",
    "name",
    "label",
    "movement",
    "visual_language",
    "content",
)
_RELATION_FIELDS = (
    ("subject_ids", "subject"),
    ("scene_id", "scene"),
    ("style_id", "style"),
    ("action_ids", "action"),
    ("camera_id", "camera"),
    ("audio_ids", "audio"),
    ("event_ids", "event"),
    ("layer", "layer"),
)


class SemanticProposalReviewError(ValueError):
    """Raised when a review projection cannot remain closed, bounded and content-safe."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise SemanticProposalReviewError(code)


def _identifier(value: object, code: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(code)
    return value


def _fingerprint(value: object, code: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        _fail(code)
    return value


def _text(value: object, code: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_SEMANTIC_REVIEW_TEXT
        or any(ord(char) < 0x20 and char not in "\t\n\r" for char in value)
        or _SENSITIVE.search(value) is not None
        # CRITICAL: browser-publication prose cannot carry any path or locator authority.
        or _PATH_OR_LOCATOR.search(value) is not None
    ):
        _fail(code)
    return value


def _bounded_wire(value: dict[str, object], schema: str) -> None:
    if value.get("schema") != schema:
        _fail("review_schema")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SemanticProposalReviewError("review_wire") from exc
    if len(encoded) > MAX_SEMANTIC_REVIEW_BYTES:
        _fail("review_wire_limit")


@dataclass(frozen=True, slots=True)
class SemanticProposalReviewHandle:
    review_id: str
    transaction_fingerprint: str
    workspace_fingerprint: str
    report_fingerprint: str
    correlation: ExecutionCorrelation
    available: bool = True
    reason: str = "review_available"
    schema: str = SEMANTIC_PROPOSAL_REVIEW_HANDLE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SEMANTIC_PROPOSAL_REVIEW_HANDLE_SCHEMA:
            _fail("review_handle_schema")
        if type(self.review_id) is not str or _REVIEW_ID.fullmatch(self.review_id) is None:
            _fail("review_id")
        _fingerprint(self.transaction_fingerprint, "review_transaction_fingerprint")
        _fingerprint(self.workspace_fingerprint, "review_workspace_fingerprint")
        _fingerprint(self.report_fingerprint, "review_report_fingerprint")
        if type(self.correlation) is not ExecutionCorrelation:
            _fail("review_correlation")
        if self.available is not True or self.reason != "review_available":
            _fail("review_availability")
        _bounded_wire(self.to_wire(), self.schema)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "review_id": self.review_id,
            "transaction_fingerprint": self.transaction_fingerprint,
            "workspace_fingerprint": self.workspace_fingerprint,
            "report_fingerprint": self.report_fingerprint,
            "correlation": self.correlation.to_wire(),
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SemanticProposalReviewItem:
    target_id: str
    summary: str
    change_kind: str = "modified"
    reference_labels: tuple[str, ...] = ()
    constraint_labels: tuple[str, ...] = ()
    uncertainty_codes: tuple[str, ...] = ()
    reason_code: str = "candidate_modified"

    def __post_init__(self) -> None:
        _identifier(self.target_id, "review_target_id")
        _text(self.summary, "review_summary")
        if self.change_kind not in {"added", "modified", "removed"}:
            _fail("review_change_kind")
        for field_name, values in (
            ("reference_label", self.reference_labels),
            ("constraint_label", self.constraint_labels),
            ("uncertainty_code", self.uncertainty_codes),
        ):
            if (
                type(values) is not tuple
                or len(values) > MAX_SEMANTIC_REVIEW_ITEMS
                or len(set(values)) != len(values)
            ):
                _fail(f"review_{field_name}s")
        for value in self.reference_labels:
            _text(value, "review_reference_label")
        for value in self.constraint_labels:
            if type(value) is not str or _CONSTRAINT_LABEL.fullmatch(value) is None:
                _fail("review_constraint_label")
        for value in self.uncertainty_codes:
            _identifier(value, "review_uncertainty_code")
        _identifier(self.reason_code, "review_reason_code")

    def to_wire(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "summary": self.summary,
            "change_kind": self.change_kind,
            "reference_labels": list(self.reference_labels),
            "constraint_labels": list(self.constraint_labels),
            "uncertainty_codes": list(self.uncertainty_codes),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class SemanticProposalClarification:
    clarification_id: str
    label: str
    reason_code: str = "resolution_required"

    def __post_init__(self) -> None:
        _identifier(self.clarification_id, "review_clarification_id")
        _text(self.label, "review_clarification_label")
        if self.reason_code != "resolution_required":
            _fail("review_clarification_reason")

    def to_wire(self) -> dict[str, str]:
        return {
            "clarification_id": self.clarification_id,
            "label": self.label,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class SemanticProposalReviewGroup:
    collection: str
    items: tuple[SemanticProposalReviewItem, ...]

    def __post_init__(self) -> None:
        if self.collection not in _COLLECTION_ID_FIELDS:
            _fail("review_collection")
        if (
            type(self.items) is not tuple
            or not self.items
            or len(self.items) > MAX_SEMANTIC_REVIEW_ITEMS
            or any(type(item) is not SemanticProposalReviewItem for item in self.items)
            or len({item.target_id for item in self.items}) != len(self.items)
        ):
            _fail("review_group_items")

    def to_wire(self) -> dict[str, object]:
        return {"collection": self.collection, "items": [item.to_wire() for item in self.items]}


@dataclass(frozen=True, slots=True)
class SemanticProposalReviewProjection:
    review_id: str
    transaction_fingerprint: str
    workspace_id: str
    workspace_revision: int
    workspace_fingerprint: str
    report_fingerprint: str
    attempt: int
    revision: int
    state: str
    segment_id: str
    correlation: ExecutionCorrelation
    changed_collections: tuple[str, ...]
    groups: tuple[SemanticProposalReviewGroup, ...]
    clarifications: tuple[SemanticProposalClarification, ...]
    uncertainty_codes: tuple[str, ...]
    reason_code: str
    actions: dict[str, bool]
    action_reasons: dict[str, str]
    terminal: str | None
    schema: str = SEMANTIC_PROPOSAL_REVIEW_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SEMANTIC_PROPOSAL_REVIEW_SCHEMA:
            _fail("review_schema")
        if type(self.review_id) is not str or _REVIEW_ID.fullmatch(self.review_id) is None:
            _fail("review_id")
        _fingerprint(self.transaction_fingerprint, "review_transaction_fingerprint")
        _identifier(self.workspace_id, "review_workspace_id")
        if (
            type(self.workspace_revision) is not int
            or not 1 <= self.workspace_revision <= 1_000_000
        ):
            _fail("review_workspace_revision")
        _fingerprint(self.workspace_fingerprint, "review_workspace_fingerprint")
        _fingerprint(self.report_fingerprint, "review_report_fingerprint")
        if type(self.attempt) is not int or not 1 <= self.attempt <= 8:
            _fail("review_attempt")
        if type(self.revision) is not int or not 1 <= self.revision <= 32:
            _fail("review_revision")
        if self.state not in {value.value for value in SemanticProposalTransactionState}:
            _fail("review_state")
        _identifier(self.segment_id, "review_segment_id")
        if type(self.correlation) is not ExecutionCorrelation:
            _fail("review_correlation")
        if (
            type(self.changed_collections) is not tuple
            or len(self.changed_collections) > MAX_SEMANTIC_REVIEW_ITEMS
            or tuple(group.collection for group in self.groups) != self.changed_collections
            or len(set(self.changed_collections)) != len(self.changed_collections)
        ):
            _fail("review_changed_collections")
        if (
            type(self.groups) is not tuple
            or any(type(group) is not SemanticProposalReviewGroup for group in self.groups)
            or sum(len(group.items) for group in self.groups) > MAX_SEMANTIC_REVIEW_ITEMS
        ):
            _fail("review_groups")
        if (
            type(self.clarifications) is not tuple
            or len(self.clarifications) > 32
            or any(
                type(value) is not SemanticProposalClarification for value in self.clarifications
            )
            or len({value.clarification_id for value in self.clarifications})
            != len(self.clarifications)
        ):
            _fail("review_clarifications")
        if (
            type(self.uncertainty_codes) is not tuple
            or len(self.uncertainty_codes) > 32
            or len(set(self.uncertainty_codes)) != len(self.uncertainty_codes)
        ):
            _fail("review_uncertainty_codes")
        for value in self.uncertainty_codes:
            _identifier(value, "review_uncertainty_code")
        _identifier(self.reason_code, "review_reason_code")
        expected_action_keys = {
            "proposal_read",
            "proposal_resolve",
            "proposal_accept",
            "proposal_reject",
            "proposal_cancel",
            "edit",
            "regenerate",
        }
        if (
            type(self.actions) is not dict
            or set(self.actions) != expected_action_keys
            or any(type(value) is not bool for value in self.actions.values())
            or self.actions["proposal_read"] is not True
            or self.actions["edit"] is not False
            or self.actions["regenerate"] is not False
        ):
            _fail("review_actions")
        if self.action_reasons != {
            "edit": "source_owner_unavailable",
            "regenerate": "source_owner_unavailable",
        }:
            _fail("review_action_reasons")
        if self.terminal not in {None, "accepted", "rejected", "cancelled", "failed"}:
            _fail("review_terminal")
        _bounded_wire(self.to_wire(), self.schema)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "review_id": self.review_id,
            "transaction_fingerprint": self.transaction_fingerprint,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "report_fingerprint": self.report_fingerprint,
            "attempt": self.attempt,
            "revision": self.revision,
            "state": self.state,
            "segment_id": self.segment_id,
            "correlation": self.correlation.to_wire(),
            "changed_collections": list(self.changed_collections),
            "groups": [group.to_wire() for group in self.groups],
            "clarifications": [value.to_wire() for value in self.clarifications],
            "uncertainty_codes": list(self.uncertainty_codes),
            "reason_code": self.reason_code,
            "actions": dict(self.actions),
            "action_reasons": dict(self.action_reasons),
            "terminal": self.terminal,
        }


def _summary(value: dict[str, object]) -> str:
    for field_name in _SUMMARY_FIELDS:
        candidate = value.get(field_name)
        if type(candidate) is str and candidate:
            return _text(candidate, "review_summary")
    _fail("review_summary_missing")


def _reference_labels(graph: dict[str, object], raw: dict[str, object]) -> tuple[str, ...]:
    registry = graph.get("registry")
    if type(registry) is not dict:
        _fail("review_registry_shape")
    labels = registry.get("labels")
    if type(labels) is not list:
        _fail("review_registry_shape")
    by_asset: dict[str, str] = {}
    for value in labels:
        if type(value) is not dict:
            _fail("review_registry_shape")
        asset_id = value.get("asset_id")
        label = value.get("label")
        if type(asset_id) is not str or type(label) is not str:
            _fail("review_registry_shape")
        by_asset[_identifier(asset_id, "review_reference_asset")] = _text(
            label, "review_reference_label"
        )
    source_ids = raw.get("source_asset_ids", [])
    if type(source_ids) is not list:
        _fail("review_reference_shape")
    result: list[str] = []
    for value in source_ids:
        asset_id = _identifier(value, "review_reference_asset")
        label = by_asset.get(asset_id)
        if label is None:
            _fail("review_reference_label_missing")
        result.append(label)
    return tuple(result)


def _constraint_labels(raw: dict[str, object]) -> tuple[str, ...]:
    result: list[str] = []
    for field_name, prefix in _RELATION_FIELDS:
        value = raw.get(field_name)
        values = value if type(value) is list else [] if value is None else [value]
        for item in values:
            identifier = _identifier(item, "review_constraint_id")
            label = f"{prefix}:{identifier}"
            if _CONSTRAINT_LABEL.fullmatch(label) is None:
                _fail("review_constraint_label")
            result.append(label)
    if len(result) > MAX_SEMANTIC_REVIEW_ITEMS or len(set(result)) != len(result):
        _fail("review_constraint_labels")
    return tuple(result)


def _groups(
    report: ContextReport,
    transaction: SemanticProposalTransaction,
) -> tuple[SemanticProposalReviewGroup, ...]:
    graph = transaction.candidate_graph.to_wire()
    baseline = report.plan.intent_graph.to_wire()
    groups: list[SemanticProposalReviewGroup] = []
    count = 0
    for collection in transaction.graph_diff.changed_collections:
        id_field = _COLLECTION_ID_FIELDS.get(collection)
        values = graph.get(collection)
        baseline_values = baseline.get(collection)
        if id_field is None or type(values) is not list or type(baseline_values) is not list:
            _fail("review_collection_shape")
        collection_values = cast(list[object], values)
        baseline_collection_values = cast(list[object], baseline_values)
        current_by_id: dict[str, dict[str, object]] = {}
        baseline_by_id: dict[str, dict[str, object]] = {}
        for raw_values, destination in (
            (collection_values, current_by_id),
            (baseline_collection_values, baseline_by_id),
        ):
            for raw in raw_values:
                if type(raw) is not dict:
                    _fail("review_item_shape")
                target_id = _identifier(raw.get(id_field), "review_target_id")
                if target_id in destination:
                    _fail("review_target_duplicate")
                destination[target_id] = raw
        items: list[SemanticProposalReviewItem] = []
        for target_id in sorted(set(current_by_id) | set(baseline_by_id)):
            current = current_by_id.get(target_id)
            previous = baseline_by_id.get(target_id)
            if current == previous:
                continue
            raw = current if current is not None else previous
            if raw is None:  # pragma: no cover - set union guarantees one side
                _fail("review_item_shape")
            change_kind = (
                "added" if previous is None else "removed" if current is None else "modified"
            )
            selected_graph = graph if current is not None else baseline
            uncertainty_codes = (
                ("clarification_required",)
                if transaction.state is SemanticProposalTransactionState.CLARIFICATION_REQUIRED
                else ()
            )
            items.append(
                SemanticProposalReviewItem(
                    target_id,
                    _summary(raw),
                    change_kind,
                    _reference_labels(selected_graph, raw),
                    _constraint_labels(raw),
                    uncertainty_codes,
                    f"candidate_{change_kind}",
                )
            )
            count += 1
            if count > MAX_SEMANTIC_REVIEW_ITEMS:
                _fail("review_item_limit")
        if not items:
            _fail("review_collection_no_changes")
        groups.append(SemanticProposalReviewGroup(collection, tuple(items)))
    return tuple(groups)


def build_semantic_proposal_review_handle(
    review_id: str,
    report: ContextReport,
    workspace: MultiSegmentWorkspace,
    transaction: SemanticProposalTransaction,
    correlation: ExecutionCorrelation,
) -> SemanticProposalReviewHandle:
    if (
        type(report) is not ContextReport
        or type(workspace) is not MultiSegmentWorkspace
        or type(transaction) is not SemanticProposalTransaction
    ):
        _fail("review_authority")
    if (
        transaction.workspace_id != workspace.workspace_id
        or transaction.workspace_revision != workspace.revision
        or transaction.workspace_fingerprint != workspace.fingerprint
    ):
        _fail("review_workspace_authority")
    return SemanticProposalReviewHandle(
        review_id,
        transaction.fingerprint,
        workspace.fingerprint,
        fingerprint_context_report(report),
        correlation,
    )


def build_semantic_proposal_review_projection(
    review_id: str,
    report: ContextReport,
    workspace: MultiSegmentWorkspace,
    transaction: SemanticProposalTransaction,
    correlation: ExecutionCorrelation,
) -> SemanticProposalReviewProjection:
    if (
        type(report) is not ContextReport
        or type(workspace) is not MultiSegmentWorkspace
        or type(transaction) is not SemanticProposalTransaction
        or type(correlation) is not ExecutionCorrelation
        or type(review_id) is not str
        or _REVIEW_ID.fullmatch(review_id) is None
        or transaction.workspace_id != workspace.workspace_id
    ):
        _fail("review_authority")
    if transaction.state is SemanticProposalTransactionState.ACCEPTED:
        if transaction.applied_workspace_fingerprint != workspace.fingerprint:
            _fail("review_workspace_authority")
    elif (
        transaction.workspace_revision != workspace.revision
        or transaction.workspace_fingerprint != workspace.fingerprint
    ):
        _fail("review_workspace_authority")
    report_fingerprint = fingerprint_context_report(report)
    state = transaction.state
    terminal = (
        state.value
        if state
        in {
            SemanticProposalTransactionState.ACCEPTED,
            SemanticProposalTransactionState.REJECTED,
            SemanticProposalTransactionState.CANCELLED,
            SemanticProposalTransactionState.FAILED,
        }
        else None
    )
    active = terminal is None
    return SemanticProposalReviewProjection(
        review_id=review_id,
        transaction_fingerprint=transaction.fingerprint,
        workspace_id=workspace.workspace_id,
        workspace_revision=workspace.revision,
        workspace_fingerprint=workspace.fingerprint,
        report_fingerprint=report_fingerprint,
        attempt=transaction.attempt,
        revision=transaction.revision,
        state=state.value,
        segment_id=transaction.segment_id,
        correlation=correlation,
        changed_collections=transaction.graph_diff.changed_collections,
        groups=_groups(report, transaction),
        clarifications=tuple(
            SemanticProposalClarification(value, value) for value in transaction.clarification_ids
        ),
        uncertainty_codes=(
            ("clarification_required",)
            if state is SemanticProposalTransactionState.CLARIFICATION_REQUIRED
            else ()
        ),
        reason_code={
            SemanticProposalTransactionState.CLARIFICATION_REQUIRED: "clarification_required",
            SemanticProposalTransactionState.READY_FOR_REVIEW: "proposal_ready",
            SemanticProposalTransactionState.ACCEPTED: "proposal_accepted",
            SemanticProposalTransactionState.REJECTED: "proposal_rejected",
            SemanticProposalTransactionState.CANCELLED: "proposal_cancelled",
            SemanticProposalTransactionState.FAILED: "proposal_failed",
        }[state],
        actions={
            "proposal_read": True,
            "proposal_resolve": state is SemanticProposalTransactionState.CLARIFICATION_REQUIRED,
            "proposal_accept": state is SemanticProposalTransactionState.READY_FOR_REVIEW,
            "proposal_reject": active,
            "proposal_cancel": active,
            "edit": False,
            "regenerate": False,
        },
        action_reasons={
            "edit": "source_owner_unavailable",
            "regenerate": "source_owner_unavailable",
        },
        terminal=terminal,
    )


def build_semantic_proposal_action_result(
    outcome: str,
    reason: str,
    review: SemanticProposalReviewProjection,
) -> dict[str, object]:
    if outcome not in {"read", "resolved", "accepted", "rejected", "cancelled"}:
        _fail("review_outcome")
    _identifier(reason, "review_reason")
    if type(review) is not SemanticProposalReviewProjection:
        _fail("review_projection")
    value: dict[str, object] = {
        "schema": SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA,
        "outcome": outcome,
        "reason": reason,
        "review": review.to_wire(),
    }
    _bounded_wire(value, SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA)
    return value


__all__ = [
    "MAX_SEMANTIC_REVIEW_BYTES",
    "MAX_SEMANTIC_REVIEW_ITEMS",
    "MAX_SEMANTIC_REVIEW_TEXT",
    "SEMANTIC_PROPOSAL_ACTION_RESULT_SCHEMA",
    "SEMANTIC_PROPOSAL_REVIEW_HANDLE_SCHEMA",
    "SEMANTIC_PROPOSAL_REVIEW_SCHEMA",
    "SemanticProposalReviewError",
    "SemanticProposalClarification",
    "SemanticProposalReviewGroup",
    "SemanticProposalReviewHandle",
    "SemanticProposalReviewItem",
    "SemanticProposalReviewProjection",
    "build_semantic_proposal_action_result",
    "build_semantic_proposal_review_handle",
    "build_semantic_proposal_review_projection",
]
