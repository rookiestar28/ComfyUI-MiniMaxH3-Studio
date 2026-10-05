"""Deterministic producer invalidation closure over M17 segment manifests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_fingerprint
from .segment_workspace import (
    MAX_WORKSPACE_ANCESTRY_DEPTH,
    MAX_WORKSPACE_SEGMENTS,
    SegmentContextManifest,
    SegmentRelationKind,
)


class RecomputePlanningError(ValueError):
    """Raised when caller input cannot be bounded or identified safely."""


class RecomputeDisposition(str, Enum):
    """Ordered per-segment producer disposition."""

    CLEAN = "clean"
    DIRTY_SELF = "dirty_self"
    DIRTY_UPSTREAM = "dirty_upstream"
    BLOCKED_MISSING_PREDECESSOR = "blocked_missing_predecessor"
    REQUIRES_FULL_RECOMPUTE = "requires_full_recompute"


@dataclass(frozen=True, slots=True)
class SegmentRecomputeDecision:
    segment_id: str
    disposition: RecomputeDisposition
    reason_codes: tuple[str, ...]
    triggering_segment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.segment_id) is not str or not self.segment_id:
            raise RecomputePlanningError("decision_segment_id")
        if type(self.disposition) is not RecomputeDisposition:
            raise RecomputePlanningError("decision_disposition")
        if type(self.reason_codes) is not tuple or not self.reason_codes:
            raise RecomputePlanningError("decision_reason_codes")
        if type(self.triggering_segment_ids) is not tuple:
            raise RecomputePlanningError("decision_triggering_segment_ids")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "disposition": self.disposition.value,
            "reason_codes": list(self.reason_codes),
            "triggering_segment_ids": list(self.triggering_segment_ids),
        }


@dataclass(frozen=True, slots=True)
class RecomputePlan:
    decisions: tuple[SegmentRecomputeDecision, ...]
    mandatory_segment_ids: tuple[str, ...]
    requested_segment_ids: tuple[str, ...] | None
    missing_required_segment_ids: tuple[str, ...]
    selection_safe: bool
    requires_full_recompute: bool

    def __post_init__(self) -> None:
        if type(self.decisions) is not tuple or not self.decisions:
            raise RecomputePlanningError("plan_decisions")
        if not all(type(item) is SegmentRecomputeDecision for item in self.decisions):
            raise RecomputePlanningError("plan_decision_type")
        for values, field in (
            (self.mandatory_segment_ids, "mandatory_segment_ids"),
            (self.missing_required_segment_ids, "missing_required_segment_ids"),
        ):
            if type(values) is not tuple or len(values) > MAX_WORKSPACE_SEGMENTS:
                raise RecomputePlanningError(field)
        if self.requested_segment_ids is not None and (
            type(self.requested_segment_ids) is not tuple
            or len(self.requested_segment_ids) > MAX_WORKSPACE_SEGMENTS
        ):
            raise RecomputePlanningError("requested_segment_ids")
        if type(self.selection_safe) is not bool:
            raise RecomputePlanningError("selection_safe")
        if type(self.requires_full_recompute) is not bool:
            raise RecomputePlanningError("requires_full_recompute")

    @property
    def required_enlargement_segment_ids(self) -> tuple[str, ...]:
        return self.missing_required_segment_ids

    def to_public_dict(self) -> dict[str, object]:
        return {
            "decisions": [item.to_public_dict() for item in self.decisions],
            "mandatory_segment_ids": list(self.mandatory_segment_ids),
            "requested_segment_ids": (
                None if self.requested_segment_ids is None else list(self.requested_segment_ids)
            ),
            "missing_required_segment_ids": list(self.missing_required_segment_ids),
            "selection_safe": self.selection_safe,
            "requires_full_recompute": self.requires_full_recompute,
        }


@dataclass(frozen=True, slots=True)
class _GraphInspection:
    order: tuple[str, ...]
    by_id: dict[str, SegmentContextManifest]
    downstream: dict[str, tuple[str, ...]]
    missing_predecessor_ids: tuple[str, ...]
    structural_reason: str | None


def plan_recompute(
    previous_manifests: tuple[SegmentContextManifest, ...],
    current_manifests: tuple[SegmentContextManifest, ...],
    *,
    requested_segment_ids: tuple[str, ...] | None = None,
) -> RecomputePlan:
    """Compare exact producer identities and return the minimal declared closure."""

    previous = _inspect_manifests(previous_manifests, "previous")
    current = _inspect_manifests(current_manifests, "current")
    requested = _validate_requested_selection(requested_segment_ids, current.order)

    full_reason = _full_recompute_reason(previous, current)
    if full_reason is not None:
        return _full_recompute_plan(current, requested, full_reason)

    missing_origins = set(current.missing_predecessor_ids)
    blocked_triggers = _propagate(current.downstream, missing_origins)
    blocked = set(blocked_triggers)

    changed = {
        segment_id
        for segment_id in current.order
        if previous.by_id[segment_id].producer_fingerprint
        != current.by_id[segment_id].producer_fingerprint
    }
    dirty_triggers = _propagate(current.downstream, changed)
    dirty = set(dirty_triggers)

    decisions: list[SegmentRecomputeDecision] = []
    mandatory: list[str] = []
    for segment_id in current.order:
        if segment_id in blocked:
            disposition = RecomputeDisposition.BLOCKED_MISSING_PREDECESSOR
            reason = (
                "missing_predecessor"
                if segment_id in missing_origins
                else "upstream_predecessor_blocked"
            )
            triggers = tuple(item for item in current.order if item in blocked_triggers[segment_id])
        elif segment_id in changed:
            disposition = RecomputeDisposition.DIRTY_SELF
            reason = "producer_fingerprint_changed"
            triggers = (segment_id,)
        elif segment_id in dirty:
            disposition = RecomputeDisposition.DIRTY_UPSTREAM
            reason = "upstream_producer_changed"
            triggers = tuple(item for item in current.order if item in dirty_triggers[segment_id])
        else:
            disposition = RecomputeDisposition.CLEAN
            reason = "producer_unchanged"
            triggers = ()
        if disposition is not RecomputeDisposition.CLEAN:
            mandatory.append(segment_id)
        decisions.append(
            SegmentRecomputeDecision(
                segment_id=segment_id,
                disposition=disposition,
                reason_codes=(reason,),
                triggering_segment_ids=triggers,
            )
        )
    return _build_plan(tuple(decisions), tuple(mandatory), requested, False)


def extend_recompute_plan(
    current_manifests: tuple[SegmentContextManifest, ...],
    base_plan: RecomputePlan,
    *,
    forced_dirty_segment_ids: tuple[str, ...] = (),
    forced_reason_codes: tuple[tuple[str, tuple[str, ...]], ...] = (),
    requested_segment_ids: tuple[str, ...] | None = None,
) -> RecomputePlan:
    """Add bounded dirty origins while keeping M17-02 graph propagation authoritative.

    M17-09 and later composition layers may discover an external invalidation (for example an
    unusable artifact receipt). They must not reimplement dependency traversal. This helper
    reuses the same inspected downstream graph and preserves any structural or missing-predecessor
    block already present in ``base_plan``.
    """

    if type(base_plan) is not RecomputePlan:
        raise RecomputePlanningError("base_plan_type")
    current = _inspect_manifests(current_manifests, "current")
    expected_ids = tuple(item.segment_id for item in base_plan.decisions)
    if expected_ids != current.order:
        raise RecomputePlanningError("base_plan_manifest_mismatch")
    forced = _validate_requested_selection(forced_dirty_segment_ids, current.order)
    if forced is None:  # pragma: no cover - forced origins are always explicit tuples
        forced = ()
    forced_set = set(forced)
    reason_by_id: dict[str, tuple[str, ...]] = {item: ("forced_rerun",) for item in forced}
    seen_reason_ids: set[str] = set()
    for index, pair in enumerate(forced_reason_codes):
        if type(pair) is not tuple or len(pair) != 2:
            raise RecomputePlanningError(f"forced_reason_codes[{index}]")
        segment_id, reasons = pair
        if segment_id in seen_reason_ids:
            raise RecomputePlanningError("duplicate_forced_reason_segment")
        seen_reason_ids.add(segment_id)
        if segment_id not in forced_set:
            raise RecomputePlanningError("unknown_forced_reason_segment")
        if (
            type(reasons) is not tuple
            or not reasons
            or not all(
                type(reason) is str and reason and "|" not in reason and "\n" not in reason
                for reason in reasons
            )
        ):
            raise RecomputePlanningError(f"forced_reason_codes[{index}].reasons")
        if len(reasons) != len(set(reasons)):
            raise RecomputePlanningError("duplicate_forced_reason_code")
        reason_by_id[segment_id] = reasons

    if current.structural_reason is not None and not base_plan.requires_full_recompute:
        raise RecomputePlanningError("base_plan_structural_mismatch")
    forced_triggers = (
        _propagate(current.downstream, forced_set) if current.structural_reason is None else {}
    )
    base_by_id = {item.segment_id: item for item in base_plan.decisions}
    decisions: list[SegmentRecomputeDecision] = []
    mandatory: list[str] = []
    for segment_id in current.order:
        base = base_by_id[segment_id]
        triggered = forced_triggers.get(segment_id, set())
        trigger_ids = set(base.triggering_segment_ids)
        trigger_ids.update(triggered)
        ordered_triggers = tuple(item for item in current.order if item in trigger_ids)
        disposition: RecomputeDisposition = base.disposition
        reason_codes: tuple[str, ...] = base.reason_codes
        if base.disposition in {
            RecomputeDisposition.BLOCKED_MISSING_PREDECESSOR,
            RecomputeDisposition.REQUIRES_FULL_RECOMPUTE,
        }:
            disposition = base.disposition
            reason_codes = base.reason_codes
            ordered_triggers = base.triggering_segment_ids
        elif base.disposition is RecomputeDisposition.CLEAN and triggered:
            if segment_id in forced_set:
                disposition = RecomputeDisposition.DIRTY_SELF
                reason_codes = reason_by_id[segment_id]
            else:
                disposition = RecomputeDisposition.DIRTY_UPSTREAM
                reason_codes = ("upstream_forced_rerun",)
        else:
            disposition = base.disposition
            reason_list = list(base.reason_codes)
            if segment_id in forced_set:
                reason_list.extend(reason_by_id[segment_id])
            elif triggered:
                reason_list.append("upstream_forced_rerun")
            reason_codes = tuple(dict.fromkeys(reason_list))
        if disposition is not RecomputeDisposition.CLEAN:
            mandatory.append(segment_id)
        decisions.append(
            SegmentRecomputeDecision(
                segment_id=segment_id,
                disposition=disposition,
                reason_codes=reason_codes,
                triggering_segment_ids=ordered_triggers,
            )
        )
    requested = _validate_requested_selection(requested_segment_ids, current.order)
    requires_full = base_plan.requires_full_recompute or any(
        item.disposition is RecomputeDisposition.REQUIRES_FULL_RECOMPUTE for item in decisions
    )
    return _build_plan(tuple(decisions), tuple(mandatory), requested, requires_full)


def _inspect_manifests(
    manifests: tuple[SegmentContextManifest, ...], label: str
) -> _GraphInspection:
    if type(manifests) is not tuple or not manifests:
        raise RecomputePlanningError(f"{label}_manifest_type")
    if len(manifests) > MAX_WORKSPACE_SEGMENTS:
        raise RecomputePlanningError("manifest_limit")
    if not all(type(item) is SegmentContextManifest for item in manifests):
        raise RecomputePlanningError(f"{label}_manifest_type")

    order = tuple(item.segment_id for item in manifests)
    if len(set(order)) != len(order):
        return _invalid_inspection(order, manifests, "invalid_dependency_graph")
    by_id = dict(zip(order, manifests, strict=True))
    if any(_manifest_integrity_reason(item) is not None for item in manifests):
        return _invalid_inspection(order, manifests, "tampered_manifest")
    first = manifests[0]
    if any(
        item.workspace_id != first.workspace_id
        or item.workspace_revision != first.workspace_revision
        or item.workspace_fingerprint != first.workspace_fingerprint
        or item.ordinal != ordinal
        for ordinal, item in enumerate(manifests, start=1)
    ):
        return _invalid_inspection(order, manifests, "incoherent_manifest_set")

    all_ids = set(order)
    seen: set[str] = set()
    ancestry: dict[str, tuple[str, int]] = {}
    downstream_lists: dict[str, list[str]] = {item: [] for item in order}
    missing: list[str] = []
    for item in manifests:
        dependencies = item.dependency_segment_ids
        if item.relation in {
            SegmentRelationKind.INDEPENDENT,
            SegmentRelationKind.CUT,
            SegmentRelationKind.RESET,
        }:
            if dependencies or item.ancestry_root_segment_id != item.segment_id:
                return _invalid_inspection(order, manifests, "invalid_dependency_graph")
            if item.ancestry_depth != 0:
                return _invalid_inspection(order, manifests, "invalid_dependency_graph")
        elif len(dependencies) != 1:
            return _invalid_inspection(order, manifests, "invalid_dependency_graph")
        else:
            predecessor = dependencies[0]
            if predecessor not in all_ids:
                missing.append(item.segment_id)
            elif predecessor not in seen:
                return _invalid_inspection(order, manifests, "invalid_dependency_graph")
            else:
                root, depth = ancestry[predecessor]
                if (
                    item.ancestry_root_segment_id != root
                    or item.ancestry_depth != depth + 1
                    or item.ancestry_depth > MAX_WORKSPACE_ANCESTRY_DEPTH
                ):
                    return _invalid_inspection(order, manifests, "invalid_dependency_graph")
                downstream_lists[predecessor].append(item.segment_id)
        expected_reset = item.relation in {
            SegmentRelationKind.CUT,
            SegmentRelationKind.RESET,
        }
        if item.reset_boundary is not expected_reset:
            return _invalid_inspection(order, manifests, "invalid_dependency_graph")
        ancestry[item.segment_id] = (
            item.ancestry_root_segment_id,
            item.ancestry_depth,
        )
        seen.add(item.segment_id)
    downstream = {key: tuple(value) for key, value in downstream_lists.items()}
    return _GraphInspection(order, by_id, downstream, tuple(missing), None)


def _manifest_integrity_reason(manifest: SegmentContextManifest) -> str | None:
    try:
        wire = manifest.to_wire()
        supplied_manifest_fingerprint = wire.pop("manifest_fingerprint")
        if supplied_manifest_fingerprint != canonical_fingerprint(wire):
            return "manifest_fingerprint_mismatch"
        predecessor = (
            manifest.dependency_segment_ids[0]
            if len(manifest.dependency_segment_ids) == 1
            else None
        )
        producer_wire = {
            "segment_id": manifest.segment_id,
            "task_mode": manifest.task_mode.value,
            "source_id": manifest.source_id,
            "reference_ids": list(manifest.reference_ids),
            "duration": manifest.duration.to_wire(),
            "relation": manifest.relation.value,
            "predecessor_segment_id": predecessor,
            "accepted_intent_fingerprint": manifest.accepted_intent_fingerprint,
            "semantic_receipt_fingerprint": manifest.semantic_receipt_fingerprint,
            "profile_fingerprint": manifest.profile_fingerprint,
            "reference_registry_fingerprint": manifest.reference_registry_fingerprint,
            "native_binding_fingerprint": manifest.native_binding_fingerprint,
            "producer_settings_fingerprint": manifest.producer_settings_fingerprint,
        }
        if manifest.producer_fingerprint != canonical_fingerprint(producer_wire):
            return "producer_fingerprint_mismatch"
    except (AttributeError, IndexError, TypeError, ValueError):
        return "manifest_shape_invalid"
    return None


def _invalid_inspection(
    order: tuple[str, ...],
    manifests: tuple[SegmentContextManifest, ...],
    reason: str,
) -> _GraphInspection:
    return _GraphInspection(order, dict(zip(order, manifests, strict=True)), {}, (), reason)


def _full_recompute_reason(previous: _GraphInspection, current: _GraphInspection) -> str | None:
    if previous.structural_reason is not None:
        return previous.structural_reason
    if current.structural_reason is not None:
        return current.structural_reason
    previous_first = previous.by_id[previous.order[0]]
    current_first = current.by_id[current.order[0]]
    if previous_first.workspace_id != current_first.workspace_id:
        return "workspace_identity_changed"
    if current_first.workspace_revision < previous_first.workspace_revision:
        return "stale_current_manifest"
    if previous.order != current.order:
        return "segment_set_changed"
    return None


def _full_recompute_plan(
    current: _GraphInspection,
    requested: tuple[str, ...] | None,
    reason: str,
) -> RecomputePlan:
    decisions = tuple(
        SegmentRecomputeDecision(
            segment_id=segment_id,
            disposition=RecomputeDisposition.REQUIRES_FULL_RECOMPUTE,
            reason_codes=(reason,),
        )
        for segment_id in current.order
    )
    return _build_plan(decisions, current.order, requested, True)


def _propagate(downstream: dict[str, tuple[str, ...]], origins: set[str]) -> dict[str, set[str]]:
    triggers: dict[str, set[str]] = {item: {item} for item in origins}
    queue = [item for item in downstream if item in origins]
    cursor = 0
    while cursor < len(queue):
        source = queue[cursor]
        cursor += 1
        for target in downstream.get(source, ()):
            existing = triggers.setdefault(target, set())
            before = len(existing)
            existing.update(triggers[source])
            if len(existing) != before:
                queue.append(target)
    return triggers


def _validate_requested_selection(
    requested: tuple[str, ...] | None, current_order: tuple[str, ...]
) -> tuple[str, ...] | None:
    if requested is None:
        return None
    if type(requested) is not tuple or len(requested) > MAX_WORKSPACE_SEGMENTS:
        raise RecomputePlanningError("requested_segment_ids")
    if not all(type(item) is str for item in requested) or len(set(requested)) != len(requested):
        raise RecomputePlanningError("requested_segment_ids")
    known = set(current_order)
    if any(item not in known for item in requested):
        raise RecomputePlanningError("unknown_requested_segment")
    return requested


def _build_plan(
    decisions: tuple[SegmentRecomputeDecision, ...],
    mandatory: tuple[str, ...],
    requested: tuple[str, ...] | None,
    requires_full: bool,
) -> RecomputePlan:
    requested_set = set(mandatory) if requested is None else set(requested)
    missing = tuple(item for item in mandatory if item not in requested_set)
    return RecomputePlan(
        decisions=decisions,
        mandatory_segment_ids=mandatory,
        requested_segment_ids=requested,
        missing_required_segment_ids=missing,
        selection_safe=not missing,
        requires_full_recompute=requires_full,
    )
