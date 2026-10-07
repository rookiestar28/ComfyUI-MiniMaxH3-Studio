"""M4-04 bounded official Context-IR lifecycle and error taxonomy tests."""

from __future__ import annotations

import unittest

from comfyui_h3_context.core import (
    OfficialAspectRatio,
    OfficialContextIRAdapter,
    OfficialContextIRLifecyclePolicy,
    OfficialContextIRRequest,
    OfficialContextIRTaskStatus,
    OfficialContextIRTransportResponse,
    ProviderExecutionPolicy,
    ProviderIdentity,
    ProviderOutcome,
    ProviderPrivacyMode,
    RawContextRequest,
    ResolvedCredential,
    TaskMode,
    build_official_context_ir_request,
    build_reference_registry,
    normalize_request,
)
from comfyui_h3_context.core.errors import OfficialContextIRError


class Resolver:
    def resolve(self, reference: str) -> ResolvedCredential:
        return ResolvedCredential(reference=reference, value="runtime-only-secret")


class LifecycleTransport:
    def __init__(self, responses: list[OfficialContextIRTransportResponse | Exception]) -> None:
        self.responses = responses
        self.create_count = 0
        self.query_count = 0

    def create(
        self,
        payload: dict[str, object],
        credential: ResolvedCredential,
    ) -> OfficialContextIRTransportResponse:
        del payload, credential
        self.create_count += 1
        return OfficialContextIRTransportResponse(202, {"task_id": "task-1"})

    def query(
        self,
        task_id: str,
        credential: ResolvedCredential,
    ) -> OfficialContextIRTransportResponse:
        del task_id, credential
        self.query_count += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Probe:
    def __init__(self, values: list[bool]) -> None:
        self.values = values

    def is_cancelled(self) -> bool:
        return self.values.pop(0) if self.values else self.values[-1] if self.values else False


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def request() -> OfficialContextIRRequest:
    registry = build_reference_registry(())
    result = normalize_request(
        RawContextRequest(
            mode=TaskMode.T2VA,
            user_intent="A bounded lifecycle test prompt.",
            duration_seconds=5,
            assets=(),
            reference_registry=registry,
        )
    )
    assert result.request is not None, result.diagnostics
    return build_official_context_ir_request(
        result.request,
        ratio=OfficialAspectRatio.RATIO_16_9,
    )


def policy() -> ProviderExecutionPolicy:
    return ProviderExecutionPolicy(
        provider=ProviderIdentity.OFFICIAL_MINIMAX,
        privacy_mode=ProviderPrivacyMode.EXPLICIT_REMOTE,
        offline=False,
        network_allowed=True,
        upload_consent=True,
        credential_reference="env.official_minimax",
    )


def success(prompt: str = "official prompt") -> OfficialContextIRTransportResponse:
    return OfficialContextIRTransportResponse(
        200,
        {
            "task": {
                "id": "task-1",
                "status": OfficialContextIRTaskStatus.SUCCEEDED.value,
                "task_type": "h3_context_ir",
                "content": {"prompt": prompt},
            }
        },
    )


def state(status: OfficialContextIRTaskStatus) -> OfficialContextIRTransportResponse:
    return OfficialContextIRTransportResponse(
        200,
        {
            "task": {
                "id": "task-1",
                "status": status.value,
                "task_type": "h3_context_ir",
                "content": {},
            }
        },
    )


def unknown_state() -> OfficialContextIRTransportResponse:
    return OfficialContextIRTransportResponse(
        200,
        {
            "task": {
                "id": "task-1",
                "status": "unknown",
                "task_type": "h3_context_ir",
                "content": {},
            }
        },
    )


class OfficialContextIRLifecycleTests(unittest.TestCase):
    def test_lifecycle_policy_is_bounded_and_descriptor_claims_cancellation(self) -> None:
        with self.assertRaises(ValueError):
            OfficialContextIRLifecyclePolicy(max_polls=0)
        with self.assertRaises(ValueError):
            OfficialContextIRLifecyclePolicy(max_wall_time_seconds=0)
        with self.assertRaises(ValueError):
            OfficialContextIRLifecyclePolicy(poll_interval_seconds=0)
        self.assertTrue(OfficialContextIRAdapter().descriptor.capabilities.supports_cancellation)

    def test_immediate_success_and_queued_running_poll_sequence(self) -> None:
        immediate_transport = LifecycleTransport([success()])
        immediate = OfficialContextIRAdapter().execute(
            request(), policy=policy(), resolver=Resolver(), transport=immediate_transport
        )
        self.assertEqual(immediate.prompt, "official prompt")
        self.assertEqual(immediate_transport.query_count, 1)

        clock = FakeClock()
        sleeps: list[float] = []

        def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds)

        transport = LifecycleTransport(
            [
                state(OfficialContextIRTaskStatus.QUEUED),
                state(OfficialContextIRTaskStatus.RUNNING),
                success(),
            ]
        )
        result = OfficialContextIRAdapter().execute(
            request(),
            policy=policy(),
            resolver=Resolver(),
            transport=transport,
            lifecycle=OfficialContextIRLifecyclePolicy(max_polls=4, poll_interval_seconds=2),
            clock=clock.now,
            sleep=record_sleep,
        )
        self.assertEqual(result.prompt, "official prompt")
        self.assertEqual(transport.query_count, 3)
        self.assertEqual(sleeps, [2.0, 2.0])

    def test_poll_count_and_wall_time_timeout_are_distinct_bounded_failures(self) -> None:
        transport = LifecycleTransport([state(OfficialContextIRTaskStatus.RUNNING)] * 2)
        with self.assertRaises(OfficialContextIRError) as failure:
            OfficialContextIRAdapter().execute(
                request(),
                policy=policy(),
                resolver=Resolver(),
                transport=transport,
                lifecycle=OfficialContextIRLifecyclePolicy(max_polls=2, poll_interval_seconds=1),
                sleep=lambda _: None,
            )
        self.assertEqual(failure.exception.category, "timeout")
        self.assertIsNotNone(failure.exception.receipt)
        assert failure.exception.receipt is not None
        self.assertEqual(failure.exception.receipt.outcome, ProviderOutcome.TIMEOUT)
        self.assertEqual(transport.query_count, 2)

        clock = FakeClock()
        wall_transport = LifecycleTransport([state(OfficialContextIRTaskStatus.QUEUED)])
        with self.assertRaises(OfficialContextIRError) as wall_failure:
            OfficialContextIRAdapter().execute(
                request(),
                policy=policy(),
                resolver=Resolver(),
                transport=wall_transport,
                lifecycle=OfficialContextIRLifecyclePolicy(
                    max_polls=10, max_wall_time_seconds=1, poll_interval_seconds=2
                ),
                clock=clock.now,
                sleep=clock.advance,
            )
        self.assertEqual(wall_failure.exception.category, "timeout")
        self.assertEqual(wall_transport.query_count, 1)

    def test_cancellation_before_create_and_between_polls_stops_without_success(self) -> None:
        before = LifecycleTransport([success()])
        with self.assertRaises(OfficialContextIRError) as before_failure:
            OfficialContextIRAdapter().execute(
                request(),
                policy=policy(),
                resolver=Resolver(),
                transport=before,
                cancellation_probe=Probe([True]),
            )
        self.assertEqual(before_failure.exception.category, "cancelled")
        self.assertEqual(before.create_count, 0)

        after_create = LifecycleTransport([success()])
        with self.assertRaises(OfficialContextIRError) as after_failure:
            OfficialContextIRAdapter().execute(
                request(),
                policy=policy(),
                resolver=Resolver(),
                transport=after_create,
                cancellation_probe=Probe([False, True]),
            )
        self.assertEqual(after_failure.exception.category, "cancelled")
        self.assertEqual(after_create.create_count, 1)
        self.assertEqual(after_create.query_count, 0)

        between = LifecycleTransport([state(OfficialContextIRTaskStatus.QUEUED), success()])
        with self.assertRaises(OfficialContextIRError) as between_failure:
            OfficialContextIRAdapter().execute(
                request(),
                policy=policy(),
                resolver=Resolver(),
                transport=between,
                cancellation_probe=Probe([False, False, False, True]),
                sleep=lambda _: None,
            )
        self.assertEqual(between_failure.exception.category, "cancelled")
        self.assertEqual(between.query_count, 1)

    def test_terminal_statuses_and_api_categories_are_preserved(self) -> None:
        for status, category, outcome in (
            (OfficialContextIRTaskStatus.FAILED, "failed", ProviderOutcome.FAILED),
            (OfficialContextIRTaskStatus.CANCELLED, "cancelled", ProviderOutcome.CANCELLED),
        ):
            transport = LifecycleTransport([state(status)])
            with self.subTest(status=status), self.assertRaises(OfficialContextIRError) as failure:
                OfficialContextIRAdapter().execute(
                    request(), policy=policy(), resolver=Resolver(), transport=transport
                )
            self.assertEqual(failure.exception.category, category)
            assert failure.exception.receipt is not None
            self.assertEqual(failure.exception.receipt.outcome, outcome)

        for status_code, category, outcome in (
            (400, "bad_request", ProviderOutcome.FAILED),
            (401, "authentication", ProviderOutcome.AUTHENTICATION),
            (402, "quota", ProviderOutcome.QUOTA),
            (422, "moderated", ProviderOutcome.MODERATED),
            (429, "rate_limit", ProviderOutcome.RETRYABLE_TRANSPORT),
            (500, "server_error", ProviderOutcome.FAILED),
        ):
            response = OfficialContextIRTransportResponse(
                status_code, {"error": {"message": "secret"}}
            )
            transport = LifecycleTransport([response])
            with (
                self.subTest(status_code=status_code),
                self.assertRaises(OfficialContextIRError) as failure,
            ):
                OfficialContextIRAdapter().execute(
                    request(), policy=policy(), resolver=Resolver(), transport=transport
                )
            self.assertEqual(failure.exception.category, category)
            self.assertNotIn("secret", str(failure.exception))
            assert failure.exception.receipt is not None
            self.assertEqual(failure.exception.receipt.outcome, outcome)

    def test_unsupported_media_and_transport_failures_are_distinct_and_redacted(self) -> None:
        unsupported = OfficialContextIRTransportResponse(
            400,
            {"error": {"type": "unsupported_media_error", "message": "private media"}},
        )
        transport = LifecycleTransport([unsupported])
        with self.assertRaises(OfficialContextIRError) as unsupported_failure:
            OfficialContextIRAdapter().execute(
                request(), policy=policy(), resolver=Resolver(), transport=transport
            )
        self.assertEqual(unsupported_failure.exception.category, "unsupported_media")
        assert unsupported_failure.exception.receipt is not None
        self.assertEqual(
            unsupported_failure.exception.receipt.outcome, ProviderOutcome.UNSUPPORTED_MEDIA
        )

        transport_failure = LifecycleTransport([RuntimeError("credential=do-not-leak")])
        with self.assertRaises(OfficialContextIRError) as failure:
            OfficialContextIRAdapter().execute(
                request(), policy=policy(), resolver=Resolver(), transport=transport_failure
            )
        self.assertEqual(failure.exception.category, "transport")
        self.assertNotIn("do-not-leak", str(failure.exception))
        assert failure.exception.receipt is not None
        self.assertEqual(failure.exception.receipt.outcome, ProviderOutcome.RETRYABLE_TRANSPORT)

    def test_unknown_status_is_fail_closed_and_never_polled_forever(self) -> None:
        transport = LifecycleTransport([unknown_state()])
        with self.assertRaises(OfficialContextIRError) as failure:
            OfficialContextIRAdapter().execute(
                request(), policy=policy(), resolver=Resolver(), transport=transport
            )
        self.assertEqual(failure.exception.category, "malformed_response")
        self.assertEqual(transport.query_count, 1)


if __name__ == "__main__":
    unittest.main()
