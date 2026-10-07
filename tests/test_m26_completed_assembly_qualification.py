"""A retained pre-execution hold must not revoke completed original generation."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from test_production_import import (
    _authorities,
    _m26_ready_sequence,
    _Materializer,
    _production,
    _request,
)

from comfyui_h3_context.core.generation_sequence import (
    GenerationSequenceProjection,
    build_generation_sequence_projection,
    create_generation_sequence_state,
)
from comfyui_h3_context.core.m26_assembly import (
    M26AssemblyAuthorizationV1,
    M26AssemblyError,
    build_production_assembly_capability,
    mint_m26_assembly_authorization,
)
from comfyui_h3_context.core.production_import import ProductionAutomaticPlanAuthorityV2
from comfyui_h3_context.core.production_storyboard import (
    ManagedExecutionQualificationV1,
    ProposalBlockerCodeV1,
)
from comfyui_h3_context.core.segment_artifacts import SegmentArtifactReceipt


def _subject() -> tuple[
    ProductionAutomaticPlanAuthorityV2,
    GenerationSequenceProjection,
    tuple[SegmentArtifactReceipt, ...],
]:
    context, _, proposal = _authorities(
        parts=(15, 15), qualification=ManagedExecutionQualificationV1.PENDING
    )
    _, registry, created = _production(context)
    imported = registry.import_automatic_plan(
        _request(created, context, proposal),
        planning_context=context,
        proposal=proposal,
        materialize=_Materializer(context),
    ).projection
    authority = cast(
        ProductionAutomaticPlanAuthorityV2,
        registry.claim_automatic_plan_authority(
            created.workspace_handle,
            expected_workspace_revision=imported.workspace_revision,
            expected_workspace_fingerprint=imported.workspace_fingerprint,
            expected_plan_fingerprint=imported.plan_fingerprint,
        ),
    )
    sequence, receipts = _m26_ready_sequence(authority)
    return authority, sequence, receipts


def _authorize(
    authority: ProductionAutomaticPlanAuthorityV2,
    sequence: GenerationSequenceProjection,
    receipts: tuple[SegmentArtifactReceipt, ...],
) -> M26AssemblyAuthorizationV1:
    return mint_m26_assembly_authorization(
        authorization_id="assembly.completed.qualification",
        automatic_plan=authority,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        cut_boundary_receipts=authority.cut_boundary_receipts,
        capability=build_production_assembly_capability(
            store_identity_fingerprint="sha256:" + "a" * 64
        ),
        issued_at_ms=20_000,
        expires_at_ms=60_000,
    )


def test_completed_originals_discharge_only_the_historical_execution_hold() -> None:
    authority, sequence, receipts = _subject()
    before = authority.private_wire_bytes()
    assert not authority.startable
    assert authority.start_hold_codes == (
        ProposalBlockerCodeV1.MANAGED_EXECUTION_QUALIFICATION_PENDING,
    )
    result = _authorize(authority, sequence, receipts)
    assert result.artifact_receipt_fingerprints == tuple(item.fingerprint for item in receipts)
    assert result.generation_state_fingerprint == sequence.state.fingerprint
    assert not authority.startable
    assert authority.private_wire_bytes() == before


@pytest.mark.parametrize("mutation", ["unfinished", "cancelled", "missing", "changed", "clean"])
def test_hold_is_not_satisfied_by_incomplete_or_mismatched_outputs(mutation: str) -> None:
    authority, sequence, receipts = _subject()
    if mutation == "unfinished":
        sequence = build_generation_sequence_projection(
            create_generation_sequence_state(sequence.state.plan), sequence.correlation
        )
    elif mutation == "cancelled":
        sequence = build_generation_sequence_projection(
            replace(sequence.state, cancellation_requested=True, state_fingerprint=None),
            sequence.correlation,
        )
    elif mutation == "missing":
        receipts = receipts[:1]
    elif mutation == "clean":
        plan = replace(
            sequence.state.plan,
            jobs=sequence.state.plan.jobs[1:],
            clean_segment_ids=(authority.reconstruction_order[0],),
            sequence_fingerprint=None,
        )
        sequence = build_generation_sequence_projection(
            replace(
                sequence.state,
                plan=plan,
                runtimes=sequence.state.runtimes[1:],
                state_fingerprint=None,
            ),
            sequence.correlation,
        )
    else:
        receipts = (replace(receipts[0], byte_length=1000, receipt_fingerprint=None), receipts[1])
    with pytest.raises(M26AssemblyError, match="assembly_authorization"):
        _authorize(authority, sequence, receipts)


@pytest.mark.parametrize(
    "hold",
    [
        ProposalBlockerCodeV1.REQUIRED_ASSET_MISSING,
        ProposalBlockerCodeV1.MANAGED_EXECUTION_UNSUPPORTED,
        ProposalBlockerCodeV1.LOCAL_REFERENCE_UNAVAILABLE,
    ],
)
def test_completed_outputs_never_discharge_other_holds(hold: ProposalBlockerCodeV1) -> None:
    authority, sequence, receipts = _subject()
    # Deliberate defensive-boundary corruption: completion cannot erase unrelated holds.
    object.__setattr__(authority, "start_hold_codes", (*authority.start_hold_codes, hold))
    with pytest.raises(M26AssemblyError, match="assembly_authorization"):
        _authorize(authority, sequence, receipts)
