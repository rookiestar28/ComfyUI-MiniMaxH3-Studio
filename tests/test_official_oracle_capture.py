"""M9-05 offline/mocked official-oracle capture lifecycle tests."""

from __future__ import annotations

import json
import unittest
from typing import cast

from comfyui_h3_context.core import (
    OfficialAspectRatio,
    OfficialContextIRRequest,
    OfficialContextIRTaskStatus,
    OfficialContextIRTransportResponse,
    OfficialOracleAbortCondition,
    OfficialOracleBudgetPolicy,
    OfficialOracleCaptureError,
    OfficialOracleCaptureRunner,
    OfficialOracleCaptureStatus,
    OfficialOracleClaimCeiling,
    OfficialOracleDecision,
    OfficialOracleExecutionMode,
    OfficialOracleExecutionScope,
    OfficialOracleGovernancePolicy,
    OfficialOracleLaneDisposition,
    OfficialOracleLaneStatus,
    OfficialOracleMediaPrivacy,
    OfficialOracleRetentionPolicy,
    OfficialOracleReviewStatus,
    OfficialOracleSourceApplicability,
    OfficialOracleSourceCategory,
    OfficialOracleSourceLedger,
    OfficialOracleSourceRecord,
    OfficialOracleTermsDecision,
    ProviderExecutionPolicy,
    ProviderIdentity,
    ProviderPrivacyMode,
    RawContextRequest,
    TaskMode,
    build_official_context_ir_request,
    build_reference_registry,
    normalize_request,
)
from comfyui_h3_context.core.official_oracle_governance import OfficialOracleExecutionGate


def request() -> OfficialContextIRRequest:
    normalized = normalize_request(
        RawContextRequest(
            mode=TaskMode.T2VA,
            user_intent="Offline mocked capture test.",
            duration_seconds=5,
            assets=(),
            reference_registry=build_reference_registry(()),
        )
    )
    assert normalized.request is not None, normalized.diagnostics
    return build_official_context_ir_request(
        normalized.request,
        ratio=OfficialAspectRatio.RATIO_16_9,
    )


def ledger() -> OfficialOracleSourceLedger:
    rows = (
        ("general", OfficialOracleSourceCategory.GENERAL_TERMS),
        ("product", OfficialOracleSourceCategory.PRODUCT_TERMS),
        ("privacy", OfficialOracleSourceCategory.PRIVACY_POLICY),
        ("api", OfficialOracleSourceCategory.API_LIFECYCLE),
        ("deletion", OfficialOracleSourceCategory.DELETION),
        ("pricing", OfficialOracleSourceCategory.PRICING),
        ("account", OfficialOracleSourceCategory.ACCOUNT_ORDER),
        ("supplemental", OfficialOracleSourceCategory.SUPPLEMENTAL),
    )
    return OfficialOracleSourceLedger(
        ledger_id="capture_ledger",
        ledger_revision="ledger.rev_1",
        reviewer_reference="reviewer.capture",
        approver_reference="approver.capture",
        jurisdiction="GLOBAL",
        sources=tuple(
            OfficialOracleSourceRecord(
                source_id=f"source.{source_id}",
                category=category,
                url=f"https://official.example.test/{source_id}",
                revision="retrieved-2026-08-07",
                effective_date="2026-08-07",
                retrieved_at="2026-08-07",
                sha256="b" * 64,
                authority="official",
                jurisdiction="GLOBAL",
                applicability=OfficialOracleSourceApplicability.NOT_APPLICABLE,
            )
            for source_id, category in rows
        ),
    )


def terms() -> OfficialOracleTermsDecision:
    lane = (
        OfficialOracleLaneDisposition(
            "official_live_oracle",
            OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
            OfficialOracleExecutionScope.OFFLINE_ONLY,
            OfficialOracleClaimCeiling.NO_CLAIM,
            OfficialOracleRetentionPolicy.NONE,
            "review.live-unavailable",
        ),
        OfficialOracleLaneDisposition(
            "official_mocked_contract",
            OfficialOracleLaneStatus.LIMITED,
            OfficialOracleExecutionScope.MOCKED_ONLY,
            OfficialOracleClaimCeiling.STRUCTURAL_ONLY,
            OfficialOracleRetentionPolicy.HASH_ONLY,
            "review.mocked-only",
        ),
        *tuple(
            OfficialOracleLaneDisposition(
                lane_id,
                OfficialOracleLaneStatus.UNAVAILABLE_PROHIBITED,
                OfficialOracleExecutionScope.OFFLINE_ONLY,
                OfficialOracleClaimCeiling.NO_CLAIM,
                OfficialOracleRetentionPolicy.NONE,
                f"review.{lane_id}",
            )
            for lane_id in (
                "recorded_oracle_fixture",
                "benchmark",
                "publication",
                "training_distillation",
            )
        ),
    )
    return OfficialOracleTermsDecision(
        review_id="capture-review",
        source_ledger_id="capture_ledger",
        terms_revision="terms.rev_1",
        reviewed_at="2026-08-07T00:00:00Z",
        valid_until="2026-08-08T00:00:00Z",
        reviewer_reference="reviewer.capture",
        status=OfficialOracleReviewStatus.APPROVED_CONTROLLED_RESEARCH,
        source_references=tuple(
            f"source.{name}"
            for name in (
                "general",
                "product",
                "privacy",
                "api",
                "deletion",
                "pricing",
                "account",
                "supplemental",
            )
        ),
        lane_dispositions=lane,
        api_use=OfficialOracleDecision.ALLOW,
        output_retention=OfficialOracleDecision.ALLOW,
        redistribution=OfficialOracleDecision.DENY,
        benchmarking=OfficialOracleDecision.DENY,
        publication=OfficialOracleDecision.DENY,
        automated_probing=OfficialOracleDecision.DENY,
        training_distillation=OfficialOracleDecision.DENY,
    )


def gate() -> OfficialOracleExecutionGate:
    policy = OfficialOracleGovernancePolicy(
        policy_revision="policy.rev_1",
        operator_consent=True,
        consent_reference="consent.capture",
        consent_terms_revision="terms.rev_1",
        consent_policy_revision="policy.rev_1",
        media_privacy=OfficialOracleMediaPrivacy.NO_MEDIA,
        retention_policy=OfficialOracleRetentionPolicy.HASH_ONLY,
        deletion_reference="delete.capture",
        abort_on=frozenset({OfficialOracleAbortCondition.CANCELLATION}),
        budget=OfficialOracleBudgetPolicy(
            max_calls=8,
            max_requests_per_window=8,
            window_seconds=60.0,
            max_spend_minor_units=0,
            currency="usd",
            max_output_tokens=100,
        ),
    )
    provider = ProviderExecutionPolicy(
        provider=ProviderIdentity.OFFICIAL_MINIMAX,
        privacy_mode=ProviderPrivacyMode.EXPLICIT_REMOTE,
        offline=False,
        network_allowed=True,
        upload_consent=False,
        credential_reference="env.capture",
    )
    return OfficialOracleExecutionGate(terms(), policy, ledger(), provider)


class FakeTransport:
    offline = True

    def __init__(self, queries: list[OfficialContextIRTransportResponse | Exception]) -> None:
        self.queries = queries
        self.created = 0
        self.queried = 0
        self.cancelled = 0
        self.listed = 0
        self.payload: dict[str, object] | None = None

    def is_offline(self) -> bool:
        return self.offline

    def create(self, payload: dict[str, object]) -> OfficialContextIRTransportResponse:
        self.created += 1
        self.payload = payload
        return OfficialContextIRTransportResponse(202, {"task_id": "capture-task"})

    def query(self, task_id: str) -> OfficialContextIRTransportResponse:
        self.queried += 1
        del task_id
        response = self.queries.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def list(self, cursor: str | None = None) -> OfficialContextIRTransportResponse:
        self.listed += 1
        del cursor
        return OfficialContextIRTransportResponse(
            200,
            {"items": [{"task_id": "capture-task", "status": "succeeded"}]},
        )

    def cancel(self, task_id: str) -> OfficialContextIRTransportResponse:
        self.cancelled += 1
        del task_id
        return OfficialContextIRTransportResponse(200, {"status": "cancelled"})


def task(
    status: OfficialContextIRTaskStatus, **extra: object
) -> OfficialContextIRTransportResponse:
    body: dict[str, object] = {
        "task": {
            "id": "capture-task",
            "status": status.value,
            "task_type": "h3_context_ir",
        }
    }
    task_value = cast(dict[str, object], body["task"])
    task_value.update(extra)
    return OfficialContextIRTransportResponse(200, body)


class OfficialOracleCaptureTests(unittest.TestCase):
    def test_mock_submit_poll_success_emits_redacted_manifest(self) -> None:
        transport = FakeTransport(
            [
                task(OfficialContextIRTaskStatus.RUNNING),
                task(
                    OfficialContextIRTaskStatus.SUCCEEDED,
                    content={"prompt": "raw oracle prompt must not be retained"},
                    usage={"input_tokens": 11, "output_tokens": 7},
                ),
            ]
        )
        runner = OfficialOracleCaptureRunner(gate())
        handle = runner.submit(request(), transport, now="2026-08-07T01:00:00Z", rate_clock=0.0)
        self.assertEqual(handle.manifest.status, OfficialOracleCaptureStatus.SUBMITTED)
        running = runner.poll(handle, transport, now="2026-08-07T01:00:01Z", rate_clock=1.0)
        result = runner.poll(running, transport, now="2026-08-07T01:00:02Z", rate_clock=2.0)
        self.assertTrue(result.terminal)
        self.assertEqual(result.manifest.status, OfficialOracleCaptureStatus.SUCCEEDED)
        self.assertEqual(result.manifest.input_tokens, 11)
        self.assertEqual(result.manifest.output_tokens, 7)
        wire = json.dumps(result.to_public_dict(), sort_keys=True)
        self.assertNotIn("raw oracle prompt", wire)
        self.assertNotIn("https://", wire)
        self.assertNotIn("env.capture", wire)

    def test_list_and_cancel_are_explicit_bounded_operations(self) -> None:
        transport = FakeTransport([])
        runner = OfficialOracleCaptureRunner(gate())
        listing = runner.list(transport, now="2026-08-07T01:00:00Z", rate_clock=0.0)
        self.assertEqual(listing.items[0].task_id, "capture-task")
        handle = runner.with_gate(listing.next_gate).submit(
            request(), transport, now="2026-08-07T01:00:01Z", rate_clock=1.0
        )
        cancelled = runner.cancel(handle, transport, now="2026-08-07T01:00:02Z", rate_clock=2.0)
        self.assertTrue(cancelled.terminal)
        self.assertEqual(cancelled.manifest.status, OfficialOracleCaptureStatus.CANCELLED)
        self.assertEqual(transport.cancelled, 1)

    def test_live_mode_and_non_offline_transport_are_rejected_before_call(self) -> None:
        transport = FakeTransport([])
        with self.assertRaises(OfficialOracleCaptureError) as live:
            OfficialOracleCaptureRunner(gate()).submit(
                request(),
                transport,
                execution_mode=OfficialOracleExecutionMode.LIVE,
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        self.assertEqual(live.exception.code, "live_transport_unavailable")
        self.assertEqual(transport.created, 0)

        class NetworkTransport(FakeTransport):
            def is_offline(self) -> bool:
                return False

        with self.assertRaises(OfficialOracleCaptureError) as network:
            OfficialOracleCaptureRunner(gate()).submit(
                request(),
                NetworkTransport([]),
                now="2026-08-07T01:00:00Z",
                rate_clock=0.0,
            )
        self.assertEqual(network.exception.code, "offline_transport_required")

    def test_malformed_and_transport_failures_remain_distinct(self) -> None:
        malformed = FakeTransport(
            [OfficialContextIRTransportResponse(200, {"task": {"status": "unknown"}})]
        )
        handle = OfficialOracleCaptureRunner(gate()).submit(
            request(), malformed, now="2026-08-07T01:00:00Z", rate_clock=0.0
        )
        with self.assertRaises(OfficialOracleCaptureError) as malformed_failure:
            OfficialOracleCaptureRunner(handle.gate).poll(
                handle, malformed, now="2026-08-07T01:00:01Z", rate_clock=1.0
            )
        self.assertEqual(malformed_failure.exception.code, "malformed_response")

        transport_error = FakeTransport([RuntimeError("credential=do-not-leak")])
        handle = OfficialOracleCaptureRunner(gate()).submit(
            request(), transport_error, now="2026-08-07T01:00:00Z", rate_clock=0.0
        )
        with self.assertRaises(OfficialOracleCaptureError) as transport_failure:
            OfficialOracleCaptureRunner(handle.gate).poll(
                handle, transport_error, now="2026-08-07T01:00:01Z", rate_clock=1.0
            )
        self.assertEqual(transport_failure.exception.code, "transport")
        self.assertNotIn("do-not-leak", str(transport_failure.exception))

    def test_cancellation_probe_and_timeout_are_terminal(self) -> None:
        transport = FakeTransport([task(OfficialContextIRTaskStatus.RUNNING)])
        handle = OfficialOracleCaptureRunner(gate()).submit(
            request(), transport, now="2026-08-07T01:00:00Z", rate_clock=0.0
        )
        with self.assertRaises(OfficialOracleCaptureError) as timeout:
            OfficialOracleCaptureRunner(handle.gate).poll(
                handle,
                transport,
                now="2026-08-07T01:00:01Z",
                rate_clock=1.0,
                elapsed_seconds=601.0,
            )
        self.assertEqual(timeout.exception.code, "timeout")

    def test_task_failure_categories_and_http_categories_remain_distinct(self) -> None:
        for marker, expected in (
            ("unsupported_media_error", "unsupported_media"),
            ("moderation_error", "moderated"),
            ("authentication_error", "authentication"),
            ("quota_error", "quota"),
        ):
            transport = FakeTransport(
                [
                    task(
                        OfficialContextIRTaskStatus.FAILED,
                        error={"type": marker, "message": "private detail"},
                    )
                ]
            )
            handle = OfficialOracleCaptureRunner(gate()).submit(
                request(), transport, now="2026-08-07T01:00:00Z", rate_clock=0.0
            )
            with (
                self.subTest(marker=marker),
                self.assertRaises(OfficialOracleCaptureError) as failure,
            ):
                OfficialOracleCaptureRunner(handle.gate).poll(
                    handle, transport, now="2026-08-07T01:00:01Z", rate_clock=1.0
                )
            self.assertEqual(failure.exception.code, expected)
            self.assertNotIn("private detail", str(failure.exception))

        for status_code, expected in (
            (400, "bad_request"),
            (401, "authentication"),
            (402, "quota"),
            (422, "moderated"),
            (429, "rate_limit"),
            (500, "server_error"),
        ):

            class HttpTransport(FakeTransport):
                def create(
                    self, payload: dict[str, object], code: int = status_code
                ) -> OfficialContextIRTransportResponse:
                    del payload
                    return OfficialContextIRTransportResponse(
                        code, {"error": {"message": "private detail"}}
                    )

            with (
                self.subTest(status_code=status_code),
                self.assertRaises(OfficialOracleCaptureError) as failure,
            ):
                OfficialOracleCaptureRunner(gate()).submit(
                    request(), HttpTransport([]), now="2026-08-07T01:00:00Z", rate_clock=0.0
                )
            self.assertEqual(failure.exception.code, expected)
            self.assertNotIn("private detail", str(failure.exception))

    def test_cancel_failure_and_poll_until_terminal_cancellation_are_not_success(self) -> None:
        class CancelFailureTransport(FakeTransport):
            def cancel(self, task_id: str) -> OfficialContextIRTransportResponse:
                del task_id
                return OfficialContextIRTransportResponse(500, {"error": {"message": "private"}})

        failed_cancel_transport = CancelFailureTransport([])
        handle = OfficialOracleCaptureRunner(gate()).submit(
            request(), failed_cancel_transport, now="2026-08-07T01:00:00Z", rate_clock=0.0
        )
        with self.assertRaises(OfficialOracleCaptureError) as cancel_failure:
            OfficialOracleCaptureRunner(handle.gate).cancel(
                handle,
                failed_cancel_transport,
                now="2026-08-07T01:00:01Z",
                rate_clock=1.0,
            )
        self.assertEqual(cancel_failure.exception.code, "server_error")
        self.assertFalse(handle.terminal)

        transport = FakeTransport([task(OfficialContextIRTaskStatus.RUNNING)])
        runner = OfficialOracleCaptureRunner(gate())
        handle = runner.submit(request(), transport, now="2026-08-07T01:00:00Z", rate_clock=0.0)
        clock_values = iter((0.0, 0.0, 1.0))
        cancelled = runner.poll_until_terminal(
            handle,
            transport,
            now=lambda: "2026-08-07T01:00:01Z",
            rate_clock=lambda: 1.0,
            clock=lambda: next(clock_values),
            sleep=lambda _: None,
            cancellation_probe=lambda: True,
        )
        self.assertTrue(cancelled.terminal)
        self.assertEqual(cancelled.manifest.status, OfficialOracleCaptureStatus.CANCELLED)
        self.assertEqual(transport.queried, 0)

    def test_handle_transitions_are_immutable_and_list_rejects_malformed_items(self) -> None:
        transport = FakeTransport([task(OfficialContextIRTaskStatus.RUNNING)])
        runner = OfficialOracleCaptureRunner(gate())
        handle = runner.submit(request(), transport, now="2026-08-07T01:00:00Z", rate_clock=0.0)
        running = runner.poll(handle, transport, now="2026-08-07T01:00:01Z", rate_clock=1.0)
        self.assertFalse(handle.terminal)
        self.assertEqual(handle.poll_count, 0)
        self.assertEqual(running.poll_count, 1)
        self.assertEqual(handle.gate.usage.calls, 1)
        self.assertEqual(running.gate.usage.calls, 2)

        class BadListTransport(FakeTransport):
            def list(self, cursor: str | None = None) -> OfficialContextIRTransportResponse:
                del cursor
                return OfficialContextIRTransportResponse(
                    200, {"items": [{"task_id": "bad-token"}]}
                )

        with self.assertRaises(OfficialOracleCaptureError) as malformed:
            runner.list(BadListTransport([]), now="2026-08-07T01:00:00Z", rate_clock=0.0)
        self.assertEqual(malformed.exception.code, "malformed_response")


if __name__ == "__main__":
    unittest.main()
