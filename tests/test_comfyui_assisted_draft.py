"""M22-12 hermetic real budget/identity/session binding tests."""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Mapping

from historical_prompt_model_fixtures import load_historical_catalog as load_prompt_model_catalog

from comfyui_h3_context.adapters.assisted_draft_execution import (
    _CountingExchange,
    build_assisted_draft_binding,
    build_assisted_instruction,
)
from comfyui_h3_context.adapters.comfyui_provider_settings import (
    ProviderSettingsSessionRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SIDEBAR_ACTION_SCHEMA,
    SidebarWorkspaceRegistry,
    decode_sidebar_action_json,
    dispatch_assisted_sidebar_action,
)
from comfyui_h3_context.adapters.prompt_model_transport import (
    PromptModelTransportError,
    RemoteExchangeMetrics,
)
from comfyui_h3_context.core import (
    ContextReport,
    ExecutionCorrelation,
    SidebarWorkspaceError,
    TaskMode,
    build_native_h3_wiring,
    canonical_fingerprint,
)
from comfyui_h3_context.core.assisted_draft import DraftGuarantee
from comfyui_h3_context.core.assisted_draft_orchestration import (
    AssistedDraftExecutionResult,
    run_assisted_draft_orchestration,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelOutcomeId,
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
from comfyui_h3_context.core.remote_provider_policy import policy_for_profile
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)

SESSION_ID = "ps_" + "c" * 32
MODEL_ID = load_prompt_model_catalog().profiles[0].model_id
#: The shipped row is qualified, so its pre-chat identity check is the strict one: a complete
#: tags row in the wire spelling, and a completed answer attributed to the exact model.
_QUALIFIED = load_prompt_model_catalog().profiles[0]
_MAYBE_EVIDENCE = _QUALIFIED.qualification_evidence
assert isinstance(_MAYBE_EVIDENCE, PromptModelQualificationEvidence)
#: Rebound to a non-optional name so every use below reads as the fact it is, rather than
#: repeating a narrowing assertion the module already made once.
_EVIDENCE = _MAYBE_EVIDENCE
WIRE_DIGEST = _EVIDENCE.model_digest.removeprefix("sha256:")


def _report() -> ContextReport:
    request = H3ContextRequestNode().build_request(
        TaskMode.T2VA,
        "A paper kite crosses a quiet sky.",
        duration_seconds=6.0,
    )[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    return H3ContextValidatorNode().validate(plan, document)[1]


def _state() -> ProviderSettingsState:
    # M22-13: the shipped local row arrives qualified, carrying the evidence that justifies it.
    # Re-stating `qualified` here would strip that evidence and the profile would refuse to build,
    # which is the point: the claim and its proof are one value.
    profile = load_prompt_model_catalog().profiles[0]
    assert profile.qualification_state is PromptModelQualificationState.QUALIFIED
    assert profile.qualification_evidence is not None
    state = ProviderSettingsState(profiles=(profile,))
    assert state.apply(
        ProviderSettingsIntent.SELECT_PROFILE, {"profile_id": profile.profile_id}
    ).accepted
    state.record_candidates((DiscoveryCandidate(profile.model_id, DiscoveryRejection.ADMITTED),))
    assert state.apply(ProviderSettingsIntent.SELECT_MODEL, {"model_id": profile.model_id}).accepted
    state.recheck(
        lambda _profile, _credential: ReadinessObservation(
            reachable=True,
            candidates=(DiscoveryCandidate(profile.model_id, DiscoveryRejection.ADMITTED),),
            # A ready qualified session is one whose exact weights a probe actually saw. Leaving
            # this out would make the fixture claim an authority the product would refuse.
            identity=QualifiedIdentityObservation(
                profile_id=profile.profile_id,
                model_id=profile.model_id,
                model_digest=_EVIDENCE.model_digest,
                qualification_sha256=_EVIDENCE.show_identity_sha256,
                endpoint_sha256=compute_endpoint_fingerprint(profile.endpoint),
                observed_at=1.0,
            ),
        )
    )
    return state


class Exchange:
    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, object]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        del timeout_seconds
        self.calls.append((method, path, payload))
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": MODEL_ID,
                        "model": MODEL_ID,
                        "digest": WIRE_DIGEST,
                        "size": _EVIDENCE.model_size_bytes,
                        "details": {
                            "format": _EVIDENCE.model_format,
                            "family": _EVIDENCE.model_family,
                        },
                    }
                ]
            }
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return {
            "model": MODEL_ID,
            "done": True,
            "message": {
                "content": json.dumps(
                    {
                        "schema": "h3.prompt_model.draft_json.v1",
                        "prompt_text": answer,
                    }
                )
            },
        }


class RealSessionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = _report()
        self.registry = ProviderSettingsSessionRegistry(state_factory=_state)
        decision = self.registry.begin_assisted_execution(SESSION_ID)
        assert decision.lease is not None
        self.lease = decision.lease

    def _run(self, exchange: object) -> AssistedDraftExecutionResult:
        guarantee = DraftGuarantee(allowed_labels=(), required_labels=())
        return run_assisted_draft_orchestration(
            profile=self.lease.snapshot.profile,
            model_choice=self.lease.snapshot.model,
            plan=self.report.plan,
            template=self.report.prompt_document,
            guarantee=guarantee,
            instruction=build_assisted_instruction(self.report, guarantee),
            evidence_fingerprint=canonical_fingerprint(self.report.evidence.to_wire()),
            provider_revision=self.lease.snapshot.provider_revision,
            session_generation=self.lease.session_generation,
            authority_epoch=self.lease.snapshot.authority_epoch,
            model_factory=lambda: build_assisted_draft_binding(
                self.lease,
                self.report,
                exchange_factory=lambda _snapshot: exchange,
            ),
            cancellation=self.lease.cancelled,
        )

    def test_a_qualified_fixture_traverses_real_budget_identity_and_chat(self) -> None:
        exchange = Exchange(self.report.prompt_document.text)
        result = self._run(exchange)
        self.assertTrue(result.usable)
        self.assertEqual([call[1] for call in exchange.calls], ["/api/tags", "/api/chat"])
        chat = exchange.calls[1][2]
        assert isinstance(chat, Mapping)
        options = chat["options"]
        assert isinstance(options, Mapping)
        self.assertEqual(chat["model"], MODEL_ID)
        self.assertFalse(chat["stream"])
        self.assertLessEqual(options["num_ctx"], 32_768)
        self.assertLessEqual(options["num_predict"], 4_096)
        assert result.receipt is not None
        self.assertEqual(result.receipt.requests, 2)

    def test_a_typed_repair_transport_failure_is_not_collapsed(self) -> None:
        dirty = self.report.prompt_document.text + " contact sheet"
        exchange = Exchange(
            dirty,
            PromptModelTransportError(PromptModelOutcomeId.AUTHENTICATION, "private upstream"),
        )
        result = self._run(exchange)
        self.assertFalse(result.usable)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.AUTHENTICATION)
        self.assertEqual(
            [call[1] for call in exchange.calls],
            ["/api/tags", "/api/chat", "/api/chat"],
        )
        self.assertNotIn("private upstream", json.dumps(result.to_wire()))

    def test_remote_repair_fits_the_transport_cap_and_checks_identity_once(self) -> None:
        from test_provider_connection import MODEL, choose, connect, connection_state, ownership

        from comfyui_h3_context.adapters.provider_readiness import probe_provider_readiness

        state = connection_state(qualified=True)
        connect(state)
        choose(state)
        profile = next(row for row in state.profiles if row.profile_id == state.selected_profile_id)
        model_id = MODEL
        policy = policy_for_profile(profile)
        state.apply(
            ProviderSettingsIntent.RECHECK_READINESS,
            ownership(state),
            readiness_probe=lambda selected, credential: probe_provider_readiness(
                selected,
                credential,
                model_choice=state.model_choice,
                cached_candidates=state.candidates,
            ),
        )
        registry = ProviderSettingsSessionRegistry(state_factory=lambda: state)
        decision = registry.begin_assisted_execution(SESSION_ID)
        assert decision.lease is not None
        self.lease = decision.lease
        report_text = self.report.prompt_document.text

        class CappedRemote:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, object]] = []
                self.answers = [report_text + " contact sheet", report_text]

            def request(
                self,
                method: str,
                path: str,
                payload: Mapping[str, object] | None = None,
                *,
                timeout_seconds: float | None = None,
            ) -> Mapping[str, object]:
                del timeout_seconds
                if len(self.calls) >= policy.max_transmissions:
                    raise PromptModelTransportError(
                        PromptModelOutcomeId.REQUEST_TOO_LARGE, "call_limit"
                    )
                self.calls.append((method, path, payload))
                if method == "GET":
                    return {"data": [{"id": model_id}]}
                return {
                    "model": model_id,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "schema": "h3.prompt_model.draft_json.v1",
                                        "prompt_text": self.answers.pop(0),
                                    }
                                ),
                            },
                        }
                    ],
                }

        exchange = CappedRemote()
        result = self._run(exchange)
        self.assertTrue(result.usable, result.outcome.to_wire())
        self.assertEqual([call[0] for call in exchange.calls], ["GET", "POST", "POST"])
        assert result.receipt is not None
        self.assertEqual(result.receipt.requests, 3)

    def test_a_typed_exchange_setup_failure_is_not_collapsed(self) -> None:
        guarantee = DraftGuarantee(allowed_labels=(), required_labels=())
        result = run_assisted_draft_orchestration(
            profile=self.lease.snapshot.profile,
            plan=self.report.plan,
            template=self.report.prompt_document,
            guarantee=guarantee,
            instruction=build_assisted_instruction(self.report, guarantee),
            evidence_fingerprint=canonical_fingerprint(self.report.evidence.to_wire()),
            provider_revision=self.lease.snapshot.provider_revision,
            session_generation=self.lease.session_generation,
            authority_epoch=self.lease.snapshot.authority_epoch,
            model_factory=lambda: build_assisted_draft_binding(
                self.lease,
                self.report,
                exchange_factory=lambda _snapshot: (_ for _ in ()).throw(
                    PromptModelTransportError(
                        PromptModelOutcomeId.AUTHENTICATION, "private setup detail"
                    )
                ),
            ),
            cancellation=self.lease.cancelled,
        )
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.AUTHENTICATION)
        self.assertNotIn("private setup detail", json.dumps(result.to_wire()))

    def test_exchange_wrapper_checks_cancellation_immediately_before_transmission(self) -> None:
        inner = Exchange(self.report.prompt_document.text)
        exchange = _CountingExchange(inner, cancelled=lambda: True)
        with self.assertRaises(PromptModelTransportError) as raised:
            exchange.request("GET", "/api/tags")
        self.assertIs(raised.exception.outcome_id, PromptModelOutcomeId.CANCELLED)
        self.assertEqual(inner.calls, [])

    def test_failed_send_metrics_count_once_and_are_not_reused_by_a_pre_send_failure(self) -> None:
        class FailedExchange:
            metrics: RemoteExchangeMetrics | None = None

            def request(self, *_args: object, **_kwargs: object) -> Mapping[str, object]:
                if self.metrics is None:
                    self.metrics = RemoteExchangeMetrics(400, 100, 73, 11, 2, True)
                raise PromptModelTransportError(PromptModelOutcomeId.PROVIDER_ERROR, "")

        exchange = _CountingExchange(FailedExchange(), cancelled=lambda: False)
        for attempt in range(2):
            with self.assertRaises(PromptModelTransportError):
                exchange.request("POST", "/v1/messages", {"model": "fixture"})
            self.assertEqual(exchange.requests, attempt + 1)
            self.assertEqual(exchange.response_bytes, 73)
            self.assertEqual(exchange.prompt_tokens, 11)
            self.assertEqual(exchange.completion_tokens, 2)

    def test_malformed_draft_json_is_a_closed_failure(self) -> None:
        class Malformed(Exchange):
            def request(
                self,
                method: str,
                path: str,
                payload: Mapping[str, object] | None = None,
                *,
                timeout_seconds: float | None = None,
            ) -> Mapping[str, object]:
                if path == "/api/tags":
                    return super().request(method, path, payload, timeout_seconds=timeout_seconds)
                self.calls.append((method, path, payload))
                return {"message": {"content": '{"prompt_text":"x","extra":true}'}}

        result = self._run(Malformed())
        self.assertFalse(result.usable)
        self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)

    def test_the_closed_sidebar_contract_admits_only_typed_assisted_actions(self) -> None:
        root = {
            "schema": SIDEBAR_ACTION_SCHEMA,
            "workspace_id": "ws_0123456789abcdefghijklmnopqrstuv",
            "expected_revision": 0,
            "expected_report_fingerprint": "sha256:" + "a" * 64,
            "action": "optimize_prompt",
            "payload": {},
        }
        decoded = decode_sidebar_action_json(json.dumps(root).encode())
        self.assertEqual(decoded["action"], "optimize_prompt")
        for action, payload in (
            (
                "edit_assisted_proposal",
                {
                    "proposal_id": "assist_0123456789abcdefghijklmnopqrstuv",
                    "expected_proposal_revision": 1,
                    "prompt_text": "bounded prompt",
                },
            ),
            (
                "accept_assisted_proposal",
                {
                    "proposal_id": "assist_0123456789abcdefghijklmnopqrstuv",
                    "expected_proposal_revision": 1,
                },
            ),
            (
                "reject_assisted_proposal",
                {
                    "proposal_id": "assist_0123456789abcdefghijklmnopqrstuv",
                    "expected_proposal_revision": 1,
                },
            ),
            ("cancel_assisted_execution", {}),
        ):
            with self.subTest(action=action):
                decoded = decode_sidebar_action_json(
                    json.dumps({**root, "action": action, "payload": payload}).encode()
                )
                self.assertEqual(decoded["action"], action)

    def test_explicit_optimize_publishes_one_review_proposal(self) -> None:
        self.assertTrue(self.registry.finish_assisted_execution(self.lease))
        workspaces = SidebarWorkspaceRegistry(max_entries=2, ttl_seconds=60)
        workspace = workspaces.publish(
            self.report,
            build_native_h3_wiring(self.report),
            ExecutionCorrelation("prompt-route", "51"),
        )
        action = {
            "schema": SIDEBAR_ACTION_SCHEMA,
            "workspace_id": workspace.workspace_id,
            "expected_revision": workspace.report_revision,
            "expected_report_fingerprint": workspace.report_fingerprint,
            "action": "optimize_prompt",
            "payload": {},
        }
        exchange = Exchange(self.report.prompt_document.text)
        result = asyncio.run(
            dispatch_assisted_sidebar_action(
                action,
                SESSION_ID,
                workspace_registry=workspaces,
                provider_registry=self.registry,
                exchange_factory=lambda _snapshot: exchange,
            )
        )
        assert isinstance(result, dict)
        self.assertEqual(result["state"], "proposal")
        self.assertIsNotNone(result["proposal"])
        self.assertEqual(
            workspaces.get(workspace.workspace_id).report_fingerprint, workspace.report_fingerprint
        )

    def test_refine_is_closed_measured_data_and_invalid_input_never_acquires_a_lease(self) -> None:
        from unittest.mock import patch

        self.assertTrue(self.registry.finish_assisted_execution(self.lease))
        workspaces = SidebarWorkspaceRegistry(max_entries=2, ttl_seconds=60)
        workspace = workspaces.publish(
            self.report, build_native_h3_wiring(self.report), ExecutionCorrelation("refine", "53")
        )
        action = {
            "schema": SIDEBAR_ACTION_SCHEMA,
            "workspace_id": workspace.workspace_id,
            "expected_revision": workspace.report_revision,
            "expected_report_fingerprint": workspace.report_fingerprint,
            "action": "refine_prompt",
            "payload": {"instruction": "  Emphasize the lighting. 中文 😀  "},
        }
        exchange = Exchange(self.report.prompt_document.text)
        result = asyncio.run(
            dispatch_assisted_sidebar_action(
                action,
                SESSION_ID,
                workspace_registry=workspaces,
                provider_registry=self.registry,
                exchange_factory=lambda _snapshot: exchange,
            )
        )
        assert isinstance(result, dict)
        self.assertEqual(result["state"], "proposal")
        chat = exchange.calls[-1][2]
        assert isinstance(chat, Mapping)
        messages = chat["messages"]
        assert isinstance(messages, list)
        data = json.loads(messages[1]["content"].split("\n", 1)[1])
        self.assertEqual(data["schema"], "h3.context.assisted_draft.request.v2")
        payload = action["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(data["revision_instruction"], payload["instruction"])
        self.assertEqual(data["current_prompt"], self.report.prompt_document.text)
        self.assertNotIn(payload["instruction"], messages[0]["content"])
        for payload in (
            {},
            {"instruction": ""},
            {"instruction": " \n "},
            {"instruction": "x" * 2049},
            {"instruction": "\x00"},
            {"instruction": "\ud800"},
            {"instruction": 1},
            {"instruction": "valid", "system": "forged"},
        ):
            with self.subTest(payload_keys=list(payload)):
                with patch.object(self.registry, "begin_assisted_execution") as begin:
                    with self.assertRaises(ValueError) as caught:
                        asyncio.run(
                            dispatch_assisted_sidebar_action(
                                {**action, "payload": payload},
                                SESSION_ID,
                                workspace_registry=workspaces,
                                provider_registry=self.registry,
                                exchange_factory=lambda _snapshot: self.fail("invalid input sent"),
                            )
                        )
                    begin.assert_not_called()
                    self.assertNotIn("forged", str(caught.exception))
        with self.assertRaises(SidebarWorkspaceError):
            asyncio.run(
                dispatch_assisted_sidebar_action(
                    action,
                    SESSION_ID,
                    workspace_registry=workspaces,
                    provider_registry=self.registry,
                    exchange_factory=lambda _snapshot: self.fail("unresolved proposal sent"),
                )
            )

    def test_cancel_requires_the_exact_current_workspace_root_before_signalling(self) -> None:
        workspaces = SidebarWorkspaceRegistry(max_entries=2, ttl_seconds=60)
        workspace = workspaces.publish(
            self.report,
            build_native_h3_wiring(self.report),
            ExecutionCorrelation("prompt-cancel", "52"),
        )
        stale = {
            "schema": SIDEBAR_ACTION_SCHEMA,
            "workspace_id": workspace.workspace_id,
            "expected_revision": workspace.report_revision,
            "expected_report_fingerprint": "sha256:" + "0" * 64,
            "action": "cancel_assisted_execution",
            "payload": {},
        }
        with self.assertRaises(SidebarWorkspaceError):
            asyncio.run(
                dispatch_assisted_sidebar_action(
                    stale,
                    SESSION_ID,
                    workspace_registry=workspaces,
                    provider_registry=self.registry,
                )
            )
        self.assertFalse(self.lease.cancelled())

        exact = {**stale, "expected_report_fingerprint": workspace.report_fingerprint}
        result = asyncio.run(
            dispatch_assisted_sidebar_action(
                exact,
                SESSION_ID,
                workspace_registry=workspaces,
                provider_registry=self.registry,
            )
        )
        assert isinstance(result, dict)
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(self.lease.cancelled())


if __name__ == "__main__":
    unittest.main()
