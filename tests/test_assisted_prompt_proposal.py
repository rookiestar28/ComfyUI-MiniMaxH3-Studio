"""M22-12 process-memory assisted proposal and canonical CAS tests."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import replace
from typing import TypeVar

from historical_prompt_model_fixtures import load_historical_catalog as load_prompt_model_catalog

from comfyui_h3_context.adapters.comfyui_provider_settings import (
    ProviderAuthorityClaim,
    ProviderSettingsSessionError,
    ProviderSettingsSessionRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    AssistedPromptProposalProjection,
    SidebarWorkspaceRegistry,
)
from comfyui_h3_context.core import (
    ContextReport,
    ExecutionCorrelation,
    SidebarWorkspaceError,
    SidebarWorkspaceProjection,
    TaskMode,
    build_native_h3_wiring,
    canonical_fingerprint,
)
from comfyui_h3_context.core.assisted_draft import DraftCandidate, DraftGuarantee, RepairShape
from comfyui_h3_context.core.assisted_draft_orchestration import (
    AssistedDraftExecutionResult,
    AssistedDraftModelBinding,
    AssistedDraftUsage,
    run_assisted_draft_orchestration,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelQualificationEvidence,
    PromptModelQualificationState,
    QualifiedIdentityObservation,
    compute_endpoint_fingerprint,
)
from comfyui_h3_context.core.prompt_model_session import (
    DiscoveryCandidate,
    DiscoveryRejection,
)
from comfyui_h3_context.core.provider_settings import (
    ProviderSettingsIntent,
    ProviderSettingsState,
    ReadinessObservation,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)

SESSION_ID = "ps_" + "b" * 32
_AuthorityResult = TypeVar("_AuthorityResult")


def _report() -> ContextReport:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "A safe operator-owned prompt.",
        duration_seconds=5.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


def _provider_state() -> ProviderSettingsState:
    # The shipped local row already arrives qualified with its evidence attached (M22-13); the
    # `replace` is kept only so this fixture keeps working if the shipped default ever changes.
    profile = replace(
        load_prompt_model_catalog().profiles[0],
        qualification_state=PromptModelQualificationState.QUALIFIED,
    )
    evidence = profile.qualification_evidence
    assert isinstance(evidence, PromptModelQualificationEvidence)
    assert profile.model_digest is not None
    state = ProviderSettingsState(profiles=(profile,))
    assert state.apply(
        ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": profile.profile_id}
    ).accepted
    state.record_candidates(
        (
            DiscoveryCandidate(
                identifier=profile.model_id,
                reason=DiscoveryRejection.ADMITTED,
            ),
        )
    )
    assert state.apply(ProviderSettingsIntent.SELECT_MODEL, {"model_id": profile.model_id}).accepted
    state.recheck(
        lambda _profile, _credential: ReadinessObservation(
            reachable=True,
            candidates=(
                DiscoveryCandidate(
                    identifier=profile.model_id,
                    reason=DiscoveryRejection.ADMITTED,
                ),
            ),
            identity=QualifiedIdentityObservation(
                profile_id=profile.profile_id,
                model_id=profile.model_id,
                model_digest=profile.model_digest,
                qualification_sha256=evidence.show_identity_sha256,
                endpoint_sha256=compute_endpoint_fingerprint(profile.endpoint),
                observed_at=1.0,
            ),
        )
    )
    return state


def _stage_action(workspace: SidebarWorkspaceProjection, text: str) -> dict[str, object]:
    return {
        "schema": "h3.context.sidebar.action.v2",
        "workspace_id": workspace.workspace_id,
        "expected_revision": workspace.report_revision,
        "expected_report_fingerprint": workspace.report_fingerprint,
        "action": "stage_prompt",
        "payload": {"reason": "Manual competing revision", "prompt_text": text},
    }


class AssistedPromptProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = _report()
        self.workspaces = SidebarWorkspaceRegistry(max_entries=2, ttl_seconds=60)
        self.workspace = self.workspaces.publish(
            self.report,
            build_native_h3_wiring(self.report),
            ExecutionCorrelation("prompt-assisted", "41"),
        )
        self.providers = ProviderSettingsSessionRegistry(state_factory=_provider_state)
        decision = self.providers.begin_assisted_execution(SESSION_ID)
        assert decision.lease is not None
        self.lease = decision.lease
        self.guarantee = DraftGuarantee(allowed_labels=(), required_labels=())

    def _execution(self, candidate_text: str) -> AssistedDraftExecutionResult:
        class Model:
            def __call__(
                self,
                instruction: str,
                *,
                shape: RepairShape | None,
            ) -> DraftCandidate:
                del instruction, shape
                return DraftCandidate(candidate_text)

        return run_assisted_draft_orchestration(
            profile=self.lease.snapshot.profile,
            plan=self.report.plan,
            template=self.report.prompt_document,
            guarantee=self.guarantee,
            instruction="Improve clarity without changing exact constraints.",
            evidence_fingerprint=canonical_fingerprint(self.report.evidence.to_wire()),
            provider_revision=self.lease.snapshot.provider_revision,
            session_generation=self.lease.session_generation,
            authority_epoch=self.lease.snapshot.authority_epoch,
            model_factory=lambda: AssistedDraftModelBinding(
                model=Model(),
                usage=lambda: AssistedDraftUsage(requests=1),
            ),
            cancellation=self.lease.cancelled,
        )

    def _publish(self, candidate_text: str) -> AssistedPromptProposalProjection:
        execution = self._execution(candidate_text)
        self.assertTrue(execution.usable)
        proposal = self.providers.run_under_assisted_authority(
            self.lease.session_id,
            self.lease.session_generation,
            self.lease.snapshot.authority_epoch,
            lambda authority: self.workspaces.publish_assisted_proposal(
                workspace_id=self.workspace.workspace_id,
                expected_revision=self.workspace.report_revision,
                expected_report_fingerprint=self.workspace.report_fingerprint,
                execution=execution,
                guarantee=self.guarantee,
                lease=self.lease,
                authority=authority,
            ),
            lease=self.lease,
        )
        self.assertTrue(self.providers.finish_assisted_execution(self.lease))
        return proposal

    def _under_authority(
        self,
        operation: Callable[[ProviderAuthorityClaim], _AuthorityResult],
    ) -> _AuthorityResult:
        try:
            return self.providers.run_under_assisted_authority(
                self.lease.session_id,
                self.lease.session_generation,
                self.lease.snapshot.authority_epoch,
                operation,
            )
        except ProviderSettingsSessionError:
            raise SidebarWorkspaceError("assisted_proposal_authority", "provider changed") from None

    def test_edit_and_reject_never_mutate_the_canonical_workspace(self) -> None:
        proposal = self._publish(self.workspace.prompt_text)
        before = self.workspaces.get(self.workspace.workspace_id)
        edited_text = before.prompt_text.replace("quietly", "carefully")
        edited = self._under_authority(
            lambda authority: self.workspaces.edit_assisted_proposal(
                proposal.proposal_id,
                expected_workspace_id=self.workspace.workspace_id,
                expected_proposal_revision=proposal.proposal_revision,
                prompt_text=edited_text,
                authority=authority,
            )
        )
        self.assertEqual(edited.proposal_revision, proposal.proposal_revision + 1)
        self.assertEqual(
            self.workspaces.get(self.workspace.workspace_id).report_fingerprint,
            before.report_fingerprint,
        )

        rejected = self._under_authority(
            lambda authority: self.workspaces.reject_assisted_proposal(
                proposal.proposal_id,
                expected_workspace_id=self.workspace.workspace_id,
                expected_proposal_revision=edited.proposal_revision,
                authority=authority,
            )
        )
        self.assertEqual(rejected.state, "rejected")
        self.assertEqual(
            self.workspaces.get(self.workspace.workspace_id).report_fingerprint,
            before.report_fingerprint,
        )

    def test_accept_uses_the_current_report_cas_and_publishes_exactly_once(self) -> None:
        # M24-05: the manual skeleton now renders the user's own sentence instead of the
        # former generic fallback, so the simulated edit anchors on that sentence.
        candidate = self.workspace.prompt_text.replace(
            "A safe operator-owned prompt.",
            "A safe operator-owned prompt, held in a slow pan.",
        )
        proposal = self._publish(candidate)
        accepted = self._under_authority(
            lambda authority: self.workspaces.accept_assisted_proposal(
                proposal.proposal_id,
                expected_workspace_id=self.workspace.workspace_id,
                expected_proposal_revision=proposal.proposal_revision,
                authority=authority,
            )
        )
        self.assertEqual(accepted.prompt_text, candidate)
        self.assertEqual(accepted.report_revision, self.workspace.report_revision + 1)
        with self.assertRaises(SidebarWorkspaceError):
            self._under_authority(
                lambda authority: self.workspaces.accept_assisted_proposal(
                    proposal.proposal_id,
                    expected_workspace_id=self.workspace.workspace_id,
                    expected_proposal_revision=proposal.proposal_revision,
                    authority=authority,
                )
            )
        self.assertEqual(
            self.workspaces.get(self.workspace.workspace_id).report_revision,
            accepted.report_revision,
        )

    def test_workspace_or_provider_drift_refuses_accept_without_overwrite(self) -> None:
        proposal = self._publish(self.workspace.prompt_text)
        manual_text = self.workspace.prompt_text.replace("quietly", "manually")
        manual = self.workspaces.dispatch(_stage_action(self.workspace, manual_text))
        assert isinstance(manual, SidebarWorkspaceProjection)
        with self.assertRaises(SidebarWorkspaceError):
            self._under_authority(
                lambda authority: self.workspaces.accept_assisted_proposal(
                    proposal.proposal_id,
                    expected_workspace_id=self.workspace.workspace_id,
                    expected_proposal_revision=proposal.proposal_revision,
                    authority=authority,
                )
            )
        self.assertEqual(
            self.workspaces.get(self.workspace.workspace_id).report_fingerprint,
            manual.report_fingerprint,
        )

        fresh_workspace = self.workspaces.publish(
            self.report,
            build_native_h3_wiring(self.report),
            ExecutionCorrelation("prompt-assisted-2", "42"),
        )
        new_decision = self.providers.begin_assisted_execution(SESSION_ID)
        assert new_decision.lease is not None
        self.lease = new_decision.lease
        self.workspace = fresh_workspace
        proposal = self._publish(fresh_workspace.prompt_text)
        dispatch = self.providers.state_for(SESSION_ID).apply(ProviderSettingsIntent.CLEAR_MODEL)
        self.assertTrue(dispatch.accepted)
        with self.assertRaises(SidebarWorkspaceError):
            self._under_authority(
                lambda authority: self.workspaces.accept_assisted_proposal(
                    proposal.proposal_id,
                    expected_workspace_id=fresh_workspace.workspace_id,
                    expected_proposal_revision=proposal.proposal_revision,
                    authority=authority,
                )
            )
        self.assertEqual(
            self.workspaces.get(fresh_workspace.workspace_id).report_fingerprint,
            fresh_workspace.report_fingerprint,
        )

    def test_proposal_cannot_be_adopted_through_an_unrelated_workspace_root(self) -> None:
        proposal = self._publish(self.workspace.prompt_text)
        other = self.workspaces.publish(
            self.report,
            build_native_h3_wiring(self.report),
            ExecutionCorrelation("prompt-assisted-other", "43"),
        )
        with self.assertRaises(SidebarWorkspaceError) as raised:
            self._under_authority(
                lambda authority: self.workspaces.edit_assisted_proposal(
                    proposal.proposal_id,
                    expected_workspace_id=other.workspace_id,
                    expected_proposal_revision=proposal.proposal_revision,
                    prompt_text=proposal.candidate_text,
                    authority=authority,
                )
            )
        self.assertEqual(raised.exception.code, "assisted_proposal_identity")

    def test_publication_refuses_a_receipt_not_bound_to_the_exact_lease_and_report(self) -> None:
        execution = self._execution(self.workspace.prompt_text)
        assert execution.receipt is not None
        mismatches = (
            replace(
                execution,
                receipt=replace(
                    execution.receipt,
                    evidence_fingerprint="sha256:" + "0" * 64,
                ),
            ),
            replace(
                execution,
                receipt=replace(
                    execution.receipt,
                    provider_revision=execution.receipt.provider_revision + 1,
                ),
            ),
            replace(
                execution,
                receipt=replace(execution.receipt, model_id="wrong-model"),
            ),
        )
        for mismatch in mismatches:
            with self.subTest(receipt=mismatch.receipt):

                def publish_mismatch(
                    authority: ProviderAuthorityClaim,
                    candidate: AssistedDraftExecutionResult = mismatch,
                ) -> AssistedPromptProposalProjection:
                    return self.workspaces.publish_assisted_proposal(
                        workspace_id=self.workspace.workspace_id,
                        expected_revision=self.workspace.report_revision,
                        expected_report_fingerprint=self.workspace.report_fingerprint,
                        execution=candidate,
                        guarantee=self.guarantee,
                        lease=self.lease,
                        authority=authority,
                    )

                with self.assertRaises(SidebarWorkspaceError) as raised:
                    self.providers.run_under_assisted_authority(
                        self.lease.session_id,
                        self.lease.session_generation,
                        self.lease.snapshot.authority_epoch,
                        publish_mismatch,
                        lease=self.lease,
                    )
                self.assertEqual(raised.exception.code, "assisted_proposal_receipt")


if __name__ == "__main__":
    unittest.main()
