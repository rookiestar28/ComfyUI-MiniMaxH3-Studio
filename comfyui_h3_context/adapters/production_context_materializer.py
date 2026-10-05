"""Canonical short-lived Context materialization for automatic Production segments."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from hashlib import sha256

from ..core.audit_override import MAX_OVERRIDE_REVISION, stage_audit_override
from ..core.canonical import canonical_fingerprint, fingerprint_context_report

# CRITICAL: keep this adapter on the shared core pipeline. Importing node/UI wrappers here
# recreates the forbidden layer inversion and permits canonical behavior to diverge.
from ..core.canonical_context_pipeline import (
    build_canonical_context_plan,
    build_canonical_context_request,
    compile_canonical_context_plan,
    validate_canonical_context_document,
)
from ..core.constraints import (
    ExactTextConstraint,
    ExactTextKind,
    ForbiddenContent,
    HardConstraintSet,
    KeepChangeDirective,
    RequiredContent,
    TimePoint,
    TimingConstraint,
)
from ..core.context_reporting import ContextPlan, ContextReport
from ..core.contracts import CURRENT_PROFILE_VERSION, ProfileIdentity, PromptProfile, TaskMode
from ..core.dialogue_speakers import render_dialogue_line
from ..core.evidence import EvidenceSet
from ..core.intent_graph import build_intent_graph
from ..core.native_h3 import (
    NativeH3Wiring,
    assert_native_h3_wiring_authority,
    build_native_h3_wiring,
)
from ..core.normalization import RawContextRequest
from ..core.production_import import (
    ProductionImportError,
    SegmentContextMaterializationClaim,
)
from ..core.production_semantics import (
    ProductionSemanticSliceV1,
    derive_production_semantic_authority,
)
from ..core.production_storyboard import (
    ProductionPlanningContextV2,
    ProposalSegmentV1,
    SegmentationProposalV2,
    fingerprint_prompt_text,
    validate_current_segmentation_proposal,
)
from ..core.registry import ReferenceAsset, ReferenceRegistry, build_reference_registry
from ..core.sidebar_workspace import SidebarWorkspaceProjection
from ..core.ui_projection import ExecutionCorrelation
from .comfyui_sidebar_workspace import (
    SidebarProductionSeed,
    SidebarWorkspaceRegistry,
    claim_sidebar_production_materialization_seed,
    publish_production_materialization,
    release_production_materialization,
)
from .production_planning_source import (
    ProductionPlanningSourceSnapshot,
    fingerprint_production_source_request,
)

PRODUCTION_CANONICAL_LOWERING_SCHEMA = "h3.context.production_canonical_lowering.v1"
PRODUCTION_CANONICAL_LOWERING_REASON = "Materialize approved Production segment prompt"
MAX_PRODUCTION_CANONICAL_LOWERING_PROMPT_LENGTH = 4_096
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class ProductionCanonicalLoweringV1:
    """Content-free recipe for the exact reproducible frontend T2VA graph subset."""

    base_report_fingerprint: str
    base_report_revision: int
    override_revision: int
    reason: str = PRODUCTION_CANONICAL_LOWERING_REASON
    schema: str = PRODUCTION_CANONICAL_LOWERING_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCTION_CANONICAL_LOWERING_SCHEMA:
            raise ProductionImportError("canonical_lowering_schema")
        if self.reason != PRODUCTION_CANONICAL_LOWERING_REASON:
            raise ProductionImportError("canonical_lowering_reason")
        if (
            type(self.base_report_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.base_report_fingerprint) is None
        ):
            raise ProductionImportError("canonical_lowering_fingerprint")
        if (
            type(self.base_report_revision) is not int
            or not 0 <= self.base_report_revision < MAX_OVERRIDE_REVISION
            or type(self.override_revision) is not int
            or self.override_revision != self.base_report_revision + 1
            or not 1 <= self.override_revision <= MAX_OVERRIDE_REVISION
        ):
            raise ProductionImportError("canonical_lowering_revision")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "base_report_fingerprint": self.base_report_fingerprint,
            "base_report_revision": self.base_report_revision,
            "override_revision": self.override_revision,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CanonicalProductionMaterializationSnapshot:
    """Internal immutable join for one live canonical scratch Context."""

    context_workspace_handle: str
    report: ContextReport = field(repr=False)
    wiring: NativeH3Wiring = field(repr=False)
    report_revision: int
    report_fingerprint: str
    seed: SidebarProductionSeed
    canonical_lowering: ProductionCanonicalLoweringV1 | None
    _claim_current: Callable[[], SidebarProductionSeed] = field(repr=False, compare=False)

    def assert_current(self) -> None:
        try:
            current = self._claim_current()
        except Exception as exc:
            raise ProductionImportError("segment_context_materialization_stale") from exc
        if type(current) is not SidebarProductionSeed or current != self.seed:
            raise ProductionImportError("segment_context_materialization_stale")


_SNAPSHOT_LOCK = threading.RLock()
_LIVE_SNAPSHOTS: dict[str, CanonicalProductionMaterializationSnapshot] = {}


def claim_canonical_production_materialization(
    claim: SegmentContextMaterializationClaim,
) -> CanonicalProductionMaterializationSnapshot:
    """Resolve only the exact still-live claim minted by this canonical adapter."""

    if type(claim) is not SegmentContextMaterializationClaim or claim.released:
        raise ProductionImportError("segment_context_materialization_authority")
    with _SNAPSHOT_LOCK:
        snapshot = _LIVE_SNAPSHOTS.get(claim.context_workspace_handle)
        if (
            snapshot is None
            or snapshot.report is not claim.report
            or snapshot.wiring is not claim.wiring
        ):
            raise ProductionImportError("segment_context_materialization_authority")
        snapshot.assert_current()
        return snapshot


def _derive_canonical_lowering(
    request: RawContextRequest,
    plan: ContextPlan,
    compiled_report: ContextReport,
    segment: ProposalSegmentV1,
) -> ProductionCanonicalLoweringV1 | None:
    """Certify only a graph recipe the public Request node can reproduce exactly."""

    default_profile = ProfileIdentity(PromptProfile.BASE, CURRENT_PROFILE_VERSION)
    if (
        request.mode is not TaskMode.T2VA
        or request.assets != ()
        or request.reference_registry != ReferenceRegistry.empty()
        or request.hard_constraints != HardConstraintSet.empty()
        or request.evidence != EvidenceSet.empty()
        or plan.request.profile != default_profile
        or not segment.local_prompt
        or len(segment.local_prompt) > MAX_PRODUCTION_CANONICAL_LOWERING_PROMPT_LENGTH
    ):
        return None
    duration_milliseconds = segment.duration.requested_seconds * 1_000
    try:
        rebuilt_request = build_canonical_context_request(
            TaskMode.T2VA,
            segment.local_prompt,
            duration_milliseconds / 1_000,
        )
        rebuilt_plan = build_canonical_context_plan(rebuilt_request)[0]
        # CRITICAL: prompt equality alone misses absent forbidden constraints and evidence. The
        # whole typed plan and Compiler report identity must match before exposing this recipe.
        if rebuilt_plan != plan:
            return None
        _prompt, rebuilt_report, _document = compile_canonical_context_plan(rebuilt_plan)
        compiled_fingerprint = fingerprint_context_report(compiled_report)
        if fingerprint_context_report(rebuilt_report) != compiled_fingerprint:
            return None
        return ProductionCanonicalLoweringV1(
            base_report_fingerprint=compiled_fingerprint,
            base_report_revision=compiled_report.revision,
            override_revision=compiled_report.revision + 1,
        )
    except Exception:
        return None


def _segment_registry(source: ReferenceRegistry, segment: ProposalSegmentV1) -> ReferenceRegistry:
    by_id = {asset.asset_id: asset for asset in source.assets}
    if len(by_id) != len(source.assets) or any(
        asset_id not in by_id for asset_id in segment.asset_ids
    ):
        raise ProductionImportError("segment_context_reference_authority")
    selected: list[ReferenceAsset] = []
    for ordinal, asset_id in enumerate(segment.asset_ids, start=1):
        asset = by_id[asset_id]
        if asset.paired_video_id is not None and asset.paired_video_id not in segment.asset_ids:
            raise ProductionImportError("segment_context_reference_authority")
        selected.append(replace(asset, connection_order=ordinal))
    return build_reference_registry(tuple(selected))


def _project_dialogue_bindings(source: ContextPlan, local: ContextPlan, prompt: str) -> ContextPlan:
    lines = tuple(
        row
        for row in local.hard_constraints.exact_texts
        if row.kind in (ExactTextKind.DIALOGUE, ExactTextKind.LYRICS)
    )
    if not lines:
        return local
    source_lines = {row.constraint_id: row for row in source.hard_constraints.exact_texts}
    subject_ids: set[str] = set()
    projected: list[
        ExactTextConstraint
        | TimingConstraint
        | RequiredContent
        | ForbiddenContent
        | KeepChangeDirective
    ] = []
    for row in local.hard_constraints.constraints:
        if isinstance(row, ExactTextConstraint) and row in lines:
            # CRITICAL: the approved segment keeps the source's global IDs. Checking only words
            # permits a changed speaker/tag; the scratch graph's local IDs are not that authority.
            original = source_lines[row.constraint_id]
            if render_dialogue_line(source, original) not in prompt:
                raise ProductionImportError("segment_context_dialogue_preservation")
            subject_ids.update(
                speaker.subject_id for speaker in row.speakers if speaker.subject_id is not None
            )
            row = replace(
                row,
                segment_id=local.intent_graph.segments[0].segment_id
                if row.segment_id is not None
                else None,
            )
        projected.append(row)
    # Global subject definitions and their ordinal labels survive in the approved local prompt.
    # Preserve their order as well as the speaking subset; dropping earlier subjects renames them.
    subjects = source.intent_graph.subjects
    if not subject_ids.issubset({row.subject_id for row in subjects}):
        raise ProductionImportError("segment_context_dialogue_binding")
    result = build_intent_graph(
        effective_duration=local.intent_graph.effective_duration,
        registry=local.intent_graph.registry,
        subjects=subjects,
        segments=local.intent_graph.segments,
    )
    if result.graph is None:
        raise ProductionImportError("segment_context_dialogue_binding")
    constraints = HardConstraintSet(tuple(projected), local.hard_constraints.transformations)
    return build_canonical_context_plan(
        RawContextRequest(
            mode=local.request.task_mode,
            user_intent=local.request.user_intent,
            duration_seconds=local.request.requested_duration_seconds,
            assets=local.request.assets,
            hard_constraints=constraints,
            reference_registry=local.request.reference_registry,
            evidence=local.evidence,
        ),
        local.request.reference_registry,
        result.graph,
    )[0]


def _slice_text(semantic_slice: ProductionSemanticSliceV1) -> str:
    return "\n".join(
        (
            *(row.text for row in semantic_slice.global_fields),
            *(row.text for row in semantic_slice.timed_spans),
        )
    )


def _text_constraint_owners(
    proposal: SegmentationProposalV2,
    text: str,
) -> tuple[str, ...]:
    owners = tuple(
        segment.segment_id
        for segment, semantic_slice in zip(proposal.segments, proposal.semantic_slices, strict=True)
        if text in segment.local_prompt or text in _slice_text(semantic_slice)
    )
    if not owners:
        raise ProductionImportError("segment_context_constraint_unaccounted")
    return owners


def _milliseconds(value: Decimal) -> int:
    milliseconds = value * 1000
    if milliseconds != milliseconds.to_integral_value():
        raise ProductionImportError("segment_context_timing_precision")
    return int(milliseconds)


def _time_from_milliseconds(value: int) -> TimePoint:
    return TimePoint.from_text(format(Decimal(value) / 1000, "f"))


def _segment_constraints(
    source: HardConstraintSet,
    proposal: SegmentationProposalV2,
    segment: ProposalSegmentV1,
    *,
    source_plan: ContextPlan | None = None,
) -> HardConstraintSet:
    source_asset_ids = {row.asset_id for row in proposal.semantic_authority.reference_definitions}
    retained: list[
        ExactTextConstraint
        | TimingConstraint
        | RequiredContent
        | ForbiddenContent
        | KeepChangeDirective
    ] = []
    rebased_timing_ids: set[str] = set()
    for constraint in source.constraints:
        if isinstance(constraint, ExactTextConstraint):
            # IMPORTANT: repeated words can belong to different speakers. Production ownership
            # must join the source full span, or one segment accidentally retains both lines.
            span = (
                render_dialogue_line(source_plan, constraint)
                if source_plan is not None and constraint.kind is not ExactTextKind.VISIBLE_TEXT
                else constraint.text
            )
            if segment.segment_id in _text_constraint_owners(proposal, span):
                retained.append(constraint)
            continue
        if isinstance(constraint, TimingConstraint):
            if constraint.end is None or constraint.end.seconds == constraint.start.seconds:
                raise ProductionImportError("segment_context_timing_point_unsupported")
            start = _milliseconds(constraint.start.seconds)
            end = _milliseconds(constraint.end.seconds)
            timing_owners = tuple(
                row
                for row in proposal.segments
                if row.global_start_milliseconds <= start and end <= row.global_end_milliseconds
            )
            if len(timing_owners) != 1:
                raise ProductionImportError("segment_context_timing_ownership")
            if timing_owners[0].segment_id == segment.segment_id:
                local_start = start - segment.global_start_milliseconds
                local_end = end - segment.global_start_milliseconds
                local = replace(
                    constraint,
                    start=_time_from_milliseconds(local_start),
                    end=_time_from_milliseconds(local_end),
                )
                if local != constraint:
                    rebased_timing_ids.add(constraint.constraint_id)
                retained.append(local)
            continue
        if isinstance(constraint, RequiredContent):
            if constraint.asset_id is not None and constraint.asset_id not in source_asset_ids:
                raise ProductionImportError("segment_context_constraint_asset")
            text_owners = _text_constraint_owners(proposal, constraint.content)
            if segment.segment_id in text_owners and (
                constraint.asset_id is None or constraint.asset_id in segment.asset_ids
            ):
                retained.append(constraint)
            continue
        if isinstance(constraint, ForbiddenContent):
            if constraint.asset_id is not None and constraint.asset_id not in source_asset_ids:
                raise ProductionImportError("segment_context_constraint_asset")
            if constraint.asset_id is None or constraint.asset_id in segment.asset_ids:
                retained.append(constraint)
            continue
        if isinstance(constraint, KeepChangeDirective):
            retained.append(constraint)
            continue
        raise ProductionImportError("segment_context_constraint_type")

    retained_ids = {row.constraint_id for row in retained}
    if any(row.constraint_id in rebased_timing_ids for row in source.transformations):
        # A coordinate rebase is not a user-authorized semantic transformation. The existing
        # history contract cannot represent both without rewriting its value-preserving chain.
        raise ProductionImportError("segment_context_timing_transformation_unsupported")
    transformations = tuple(
        row for row in source.transformations if row.constraint_id in retained_ids
    )
    try:
        return HardConstraintSet(tuple(retained), transformations)
    except Exception as exc:
        raise ProductionImportError("segment_context_constraint_projection") from exc


def _assert_source_join(
    source: ProductionPlanningSourceSnapshot,
    planning_context: ProductionPlanningContextV2,
) -> None:
    report = source.report
    registry = report.request.reference_registry
    expected_assets = tuple((row.asset_id, row.role) for row in registry.assets)
    try:
        expected_semantics = derive_production_semantic_authority(
            report.plan, report.prompt_document
        )
    except Exception as exc:
        raise ProductionImportError("segment_context_source_mismatch") from exc
    if (
        planning_context.optimized_candidate_id != report.report_id
        or planning_context.optimized_candidate_text_fingerprint
        != fingerprint_prompt_text(report.prompt_document.text)
        or planning_context.source_request_fingerprint
        != fingerprint_production_source_request(report.request)
        or planning_context.source_intent_graph_fingerprint
        != canonical_fingerprint(report.plan.intent_graph.to_wire())
        or planning_context.source_profile_fingerprint
        != canonical_fingerprint(report.request.profile.to_wire())
        or planning_context.reference_registry_fingerprint
        != canonical_fingerprint(registry.to_wire())
        or planning_context.global_task_mode is not report.request.task_mode
        or tuple((row.asset_id, row.role) for row in planning_context.assets) != expected_assets
        or planning_context.reference_ids != tuple(row.asset_id for row in registry.assets)
        or planning_context.subject_ids
        != tuple(row.subject_id for row in report.plan.intent_graph.subjects)
        or planning_context.semantic_authority != expected_semantics
    ):
        raise ProductionImportError("segment_context_source_mismatch")


class CanonicalProductionMaterializer:
    """Rebuild one proposal segment through the canonical Context pipeline.

    The source snapshot is an immutable content authority.  No source workspace handle is
    consulted after construction, so a committed automatic plan remains rematerializable after
    its authoring Sidebar workspace reaches its fixed expiry.
    """

    __slots__ = ("_source", "_registry", "_lease")

    def __init__(
        self,
        source: ProductionPlanningSourceSnapshot,
        *,
        workspace_registry: SidebarWorkspaceRegistry | None = None,
    ) -> None:
        if type(source) is not ProductionPlanningSourceSnapshot:
            raise ProductionImportError("segment_context_source_authority")
        report = source.report
        try:
            assert_native_h3_wiring_authority(source.wiring, report)
        except Exception as exc:
            raise ProductionImportError("segment_context_source_authority") from exc
        if not report.is_successful or not report.validation.is_valid:
            raise ProductionImportError("segment_context_source_authority")
        self._source = source
        self._registry = workspace_registry
        self._lease = threading.Lock()

    def _publish(
        self,
        report: ContextReport,
        wiring: NativeH3Wiring,
        correlation: ExecutionCorrelation,
    ) -> SidebarWorkspaceProjection:
        if self._registry is None:
            return publish_production_materialization(report, wiring, correlation)
        return self._registry.publish_production_materialization(report, wiring, correlation)

    def _release(
        self,
        workspace_id: str,
        *,
        expected_report_revision: int,
        expected_report_fingerprint: str,
    ) -> None:
        if self._registry is None:
            release_production_materialization(
                workspace_id,
                expected_report_revision=expected_report_revision,
                expected_report_fingerprint=expected_report_fingerprint,
            )
            return
        self._registry.release_production_materialization(
            workspace_id,
            expected_report_revision=expected_report_revision,
            expected_report_fingerprint=expected_report_fingerprint,
        )

    def _claim_materialization_seed(
        self,
        workspace_id: str,
        *,
        expected_report_revision: int,
        expected_report_fingerprint: str,
    ) -> SidebarProductionSeed:
        if self._registry is None:
            return claim_sidebar_production_materialization_seed(
                workspace_id,
                expected_report_revision=expected_report_revision,
                expected_report_fingerprint=expected_report_fingerprint,
            )
        return self._registry.claim_production_materialization_seed(
            workspace_id,
            expected_report_revision=expected_report_revision,
            expected_report_fingerprint=expected_report_fingerprint,
        )

    def __call__(
        self,
        planning_context: ProductionPlanningContextV2,
        proposal: SegmentationProposalV2,
        segment: ProposalSegmentV1,
    ) -> SegmentContextMaterializationClaim:
        if (
            type(planning_context) is not ProductionPlanningContextV2
            or type(proposal) is not SegmentationProposalV2
            or type(segment) is not ProposalSegmentV1
        ):
            raise ProductionImportError("segment_context_materialization_authority")
        try:
            validate_current_segmentation_proposal(planning_context, proposal)
        except Exception as exc:
            raise ProductionImportError("segment_context_proposal_mismatch") from exc
        if not any(row is segment or row == segment for row in proposal.segments):
            raise ProductionImportError("segment_context_proposal_mismatch")
        _assert_source_join(self._source, planning_context)

        # CRITICAL: one materializer may be retained by several replay/eligibility callers. Keep
        # exactly one ephemeral Sidebar Context alive; overlapping claims create ambiguous seed
        # ownership and can evict an unrelated workspace under the bounded registry.
        if not self._lease.acquire(blocking=False):
            raise ProductionImportError("segment_context_materialization_active")

        published: SidebarWorkspaceProjection | None = None
        snapshot: CanonicalProductionMaterializationSnapshot | None = None
        try:
            registry = _segment_registry(self._source.report.request.reference_registry, segment)
            constraints = _segment_constraints(
                self._source.report.request.hard_constraints,
                proposal,
                segment,
                source_plan=self._source.report.plan,
            )
            request = RawContextRequest(
                mode=segment.task_mode,
                user_intent=segment.local_prompt,
                duration_seconds=segment.duration.requested_seconds,
                assets=registry.to_asset_descriptors(),
                hard_constraints=constraints,
                reference_registry=registry,
                evidence=self._source.report.evidence,
            )
            plan = build_canonical_context_plan(request, registry)[0]
            plan = _project_dialogue_bindings(self._source.report.plan, plan, segment.local_prompt)
            if plan.request.profile != self._source.report.request.profile:
                raise ProductionImportError("segment_context_profile_mismatch")
            _prompt, compiled_report, _document = compile_canonical_context_plan(plan)
            canonical_lowering = _derive_canonical_lowering(
                request,
                plan,
                compiled_report,
                segment,
            )
            # CRITICAL: local_prompt is already the reviewed canonical prompt. Feeding it back as
            # free-form intent makes the compiler wrap its sections a second time. Use the existing
            # staged-prompt seam, then run the real validator over the exact approved text.
            staged = stage_audit_override(
                compiled_report,
                expected_revision=compiled_report.revision,
                expected_report_fingerprint=fingerprint_context_report(compiled_report),
                reason=PRODUCTION_CANONICAL_LOWERING_REASON,
                prompt_text=segment.local_prompt,
            )
            _validation, report = validate_canonical_context_document(plan, staged.prompt_document)
            if report.prompt_document.text != segment.local_prompt:
                raise ProductionImportError("segment_context_prompt_mismatch")
            wiring = build_native_h3_wiring(report)
            if wiring.prompt != segment.local_prompt:
                raise ProductionImportError("segment_context_prompt_mismatch")
            digest = sha256(
                f"{proposal.fingerprint}:{segment.segment_id}".encode("ascii")
            ).hexdigest()
            publication = self._publish(
                report,
                wiring,
                ExecutionCorrelation(
                    prompt_id=f"production.materialization.{digest[:40]}",
                    execution_node_id="production.context.materializer",
                ),
            )
            if type(publication) is not SidebarWorkspaceProjection:
                raise ProductionImportError("segment_context_publication")
            published = publication
            workspace_id = published.workspace_id
            expected_revision = published.report_revision
            expected_fingerprint = published.report_fingerprint
            seed = self._claim_materialization_seed(
                workspace_id,
                expected_report_revision=expected_revision,
                expected_report_fingerprint=expected_fingerprint,
            )
            snapshot = CanonicalProductionMaterializationSnapshot(
                context_workspace_handle=workspace_id,
                report=report,
                wiring=wiring,
                report_revision=expected_revision,
                report_fingerprint=expected_fingerprint,
                seed=seed,
                canonical_lowering=canonical_lowering,
                _claim_current=lambda: self._claim_materialization_seed(
                    workspace_id,
                    expected_report_revision=expected_revision,
                    expected_report_fingerprint=expected_fingerprint,
                ),
            )
            with _SNAPSHOT_LOCK:
                if workspace_id in _LIVE_SNAPSHOTS:
                    raise ProductionImportError("segment_context_materialization_collision")
                _LIVE_SNAPSHOTS[workspace_id] = snapshot
            released = False

            def release() -> None:
                nonlocal released
                if released:
                    raise ProductionImportError("segment_context_already_released")
                released = True
                try:
                    with _SNAPSHOT_LOCK:
                        if _LIVE_SNAPSHOTS.get(workspace_id) is snapshot:
                            del _LIVE_SNAPSHOTS[workspace_id]
                    try:
                        self._release(
                            workspace_id,
                            expected_report_revision=expected_revision,
                            expected_report_fingerprint=expected_fingerprint,
                        )
                    except KeyError:
                        # The fixed-lifetime Sidebar owner may already have pruned this exact
                        # scratch. Absence is terminal cleanup; stale current content still raises.
                        pass
                finally:
                    self._lease.release()

            return SegmentContextMaterializationClaim(
                context_workspace_handle=workspace_id,
                report=report,
                wiring=wiring,
                release=release,
            )
        except Exception:
            cleanup_error: Exception | None = None
            if published is not None:
                with _SNAPSHOT_LOCK:
                    if (
                        snapshot is not None
                        and _LIVE_SNAPSHOTS.get(published.workspace_id) is snapshot
                    ):
                        del _LIVE_SNAPSHOTS[published.workspace_id]
                # CRITICAL: every fault after publication must destroy the exact scratch before
                # releasing the single-claim lease. Otherwise the next attempt runs beside an
                # unaddressable Context. Already-pruned absence is the one terminal success case.
                try:
                    self._release(
                        published.workspace_id,
                        expected_report_revision=published.report_revision,
                        expected_report_fingerprint=published.report_fingerprint,
                    )
                except KeyError:
                    pass
                except Exception as exc:
                    cleanup_error = exc
            if self._lease.locked():
                self._lease.release()
            if cleanup_error is not None:
                raise ProductionImportError(
                    "segment_context_materialization_compensation_failed"
                ) from cleanup_error
            raise


__all__ = [
    "CanonicalProductionMaterializationSnapshot",
    "CanonicalProductionMaterializer",
    "MAX_PRODUCTION_CANONICAL_LOWERING_PROMPT_LENGTH",
    "PRODUCTION_CANONICAL_LOWERING_REASON",
    "PRODUCTION_CANONICAL_LOWERING_SCHEMA",
    "ProductionCanonicalLoweringV1",
    "claim_canonical_production_materialization",
]
