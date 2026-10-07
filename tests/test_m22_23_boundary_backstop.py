"""M22-23. No exception of unknown content leaves the provider boundary.

Both `request` methods wrapped their whole network body in one `try` and handled a fixed list of
families -- five for the remote transport, four for the loopback. Anything else escaped as a
traceback into the host, where every other failure there is a typed outcome carrying a remediation.
M22-22 closed one instance, a `UnicodeEncodeError` out of `http.client.putheader`, by preventing
that particular exception from being constructible. It did not close the shape, and the shape has
two more instances: the loopback twin, and the unguarded `finally: connection.close()` in both,
where a raising close does not merely escape but *replaces* the typed outcome already in flight.

Two properties here are easy to state and easy to get subtly wrong, so both are asserted directly.

The backstop must not swallow `PromptModelContractError`. It derives from `ValueError` and signals
one of this repository's own invariants breaking; reporting that as a transport failure would point
the user at their network for a bug in here, and would hide it behind a remediation.

And the original exception must be genuinely unreachable from the one that leaves, not merely
hidden. `raise ... from None` sets `__suppress_context__`, which stops the default traceback printer
from rendering the chain -- it drops no reference, and `__context__` still holds the original. At
this boundary the original may be holding a credential, which is exactly what M22-22's instance did.
"""

from __future__ import annotations

import http.client
import ssl
import unittest
from collections.abc import Mapping
from typing import Any

from test_prompt_model_session import FakeExchange, ollama_tags, session_request
from test_remote_prompt_model import ENDPOINT, POLICY, SECRET

from comfyui_h3_context.adapters.prompt_model_transport import (
    LoopbackJsonExchange,
    PromptModelTransportError,
    RemoteHttpsExchange,
    _timeout,
    run_prompt_model_session,
)
from comfyui_h3_context.core.prompt_model_provider import (
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcomeId,
    admit_egress_destination,
)
from comfyui_h3_context.core.remote_prompt_model import RuntimeCredential

#: A plain ASCII marker. M22-22's review turned on exactly this detail: the probe there asked
#: whether a secret was a substring of a `repr`, and the secret ended in a character `repr` escapes,
#: so it could never match however leaky the repr was. A marker that survives escaping is the only
#: kind that can answer the question being asked.
MARKER = "CREDENTIALMARKERAAA"

# Taken from the policy rather than picked out of REMOTE_FAMILIES: the exchange refuses a
# destination whose family disagrees with its policy, and ENDPOINT comes from the same profile.
REMOTE_FAMILY = POLICY.family
OLLAMA_ENDPOINT = "http://127.0.0.1:11434"


class Unanticipated(Exception):
    """A family none of the clauses names, and none of them ever will."""


def carrying_marker() -> UnicodeEncodeError:
    """The M22-22 shape: an exception whose payload is the whole Authorization value."""

    return UnicodeEncodeError("latin-1", "Bearer sk-" + MARKER, 10, 11, "not encodable")


#: Each is outside every clause the two methods name, and each is a different reason to be.
UNANTICIPATED = (
    (Unanticipated("boom"), "a class this build has never seen"),
    (carrying_marker(), "UnicodeEncodeError, the M22-22 instance"),
    (AttributeError("nope"), "an ordinary programming error"),
    (KeyError("absent"), "a lookup failure"),
    (RecursionError("deep"), "RuntimeError-derived, so adjacent to the transport error itself"),
)


def reachable_text(error: BaseException) -> str:
    """Everything a reporter could render by walking this exception, however it walks it."""

    seen: list[str] = []
    stack: list[BaseException | None] = [error]
    visited: set[int] = set()
    while stack:
        current = stack.pop()
        if current is None or id(current) in visited:
            continue
        visited.add(id(current))
        seen.append(repr(current))
        seen.append(str(current))
        seen.append(repr(current.args))
        stack.append(current.__cause__)
        stack.append(current.__context__)
    return "\n".join(seen)


def frames_holding(error: BaseException, needle: str) -> list[tuple[str, str]]:
    """Which traceback frame locals, if any, still render the needle.

    `f_locals` on a function frame is materialized from the fast locals when it is read, so a name
    unbound before the exception propagated is simply absent here -- which is what makes clearing
    the binding an effective fix rather than a cosmetic one.
    """

    found: list[tuple[str, str]] = []
    frame = error.__traceback__
    while frame is not None:
        for name, value in frame.tb_frame.f_locals.items():
            if needle in repr(value):
                found.append((frame.tb_frame.f_code.co_name, name))
        frame = frame.tb_next
    return found


BODY = b'{"choices":[{"message":{"content":"hi"}}]}'


class FailingResponse:
    """A response that can fail inside `read`, which is its own point inside the `try`."""

    def __init__(self, *, fail_read: BaseException | None) -> None:
        self.status = 200
        self._fail_read = fail_read

    def read(self, limit: int) -> bytes:
        if self._fail_read is not None:
            raise self._fail_read
        return BODY[:limit]


class FailingConnection:
    """A connection that fails at exactly one named step, and counts its closes."""

    def __init__(self, *, at: str, error: BaseException, close_error: BaseException | None = None):
        self._at = at
        self._error = error
        self._close_error = close_error
        self.closes = 0

    def _maybe(self, step: str) -> None:
        if step == self._at:
            raise self._error

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._maybe("request")

    def getresponse(self) -> FailingResponse:
        self._maybe("getresponse")
        return FailingResponse(fail_read=self._error if self._at == "read" else None)

    def close(self) -> None:
        self.closes += 1
        if self._close_error is not None:
            raise self._close_error


def remote_failing(**kwargs: Any) -> tuple[RemoteHttpsExchange, list[FailingConnection]]:
    made: list[FailingConnection] = []

    def factory(host: str, port: int, timeout: float) -> FailingConnection:
        if kwargs.get("at") == "connect":
            # `_connect` is inside the `try`, and failing here also means `connection` stays None,
            # which is the path where the guarded close must not fire at all.
            raise kwargs["error"]
        connection = FailingConnection(**kwargs)
        made.append(connection)
        return connection

    exchange = RemoteHttpsExchange(
        admit_egress_destination(REMOTE_FAMILY, ENDPOINT),
        RuntimeCredential(SECRET),
        policy=POLICY,
        connection_factory=factory,
    )
    return exchange, made


def send(exchange: RemoteHttpsExchange) -> Mapping[str, object]:
    return exchange.request("POST", POLICY.chat_route, {"model": "m", "messages": []})


def loopback_failing(error: BaseException) -> LoopbackJsonExchange:
    """The loopback has no connection factory; its resolver is called inside the same `try`."""

    def resolver(host: str) -> str:
        raise error

    return LoopbackJsonExchange(
        admit_egress_destination(PromptModelFamily.OLLAMA, OLLAMA_ENDPOINT),
        address_resolver=resolver,
    )


class NothingUnknownLeavesTheBoundaryTests(unittest.TestCase):
    def test_every_unanticipated_failure_becomes_one_typed_outcome(self) -> None:
        # Every distinct point inside the `try` that a stub can reach: establishing the connection,
        # issuing the request, taking the response, and reading its body. The clause is uniform, so
        # this is about proving the `try` really spans what it is claimed to span.
        for error, label in UNANTICIPATED:
            for step in ("connect", "request", "getresponse", "read"):
                with self.subTest(error=label, step=step):
                    exchange, _ = remote_failing(at=step, error=error)
                    with self.assertRaises(PromptModelTransportError) as caught:
                        send(exchange)
                    raised = caught.exception
                    self.assertIs(raised.outcome_id, PromptModelOutcomeId.TRANSPORT)
                    self.assertEqual("unexpected", raised.detail)
                    self.assertEqual(type(error).__name__, raised.unexpected_type)

    def test_the_loopback_twin_answers_the_same_question_the_same_way(self) -> None:
        # Fixing only the remote transport would repeat M22-20's error of treating one visible
        # instance as the whole defect. This family is also the one an ordinary user reaches first.
        for error, label in UNANTICIPATED:
            with self.subTest(error=label):
                with self.assertRaises(PromptModelTransportError) as caught:
                    loopback_failing(error).request("GET", "/api/tags")
                raised = caught.exception
                self.assertIs(raised.outcome_id, PromptModelOutcomeId.TRANSPORT)
                self.assertEqual("unexpected", raised.detail)
                self.assertEqual(type(error).__name__, raised.unexpected_type)

    def test_the_detail_stays_a_closed_code_and_never_names_the_class(self) -> None:
        # `detail` is not a developer channel: `_failure` wraps it with
        # `UntrustedProviderDetail.from_provider_text` and attaches it to the outcome as
        # `provider_detail`, so whatever goes here is shown to the user as words from the provider.
        # Naming a Python class there would attribute this build's own failure to the service.
        exchange, _ = remote_failing(at="request", error=Unanticipated("boom"))
        with self.assertRaises(PromptModelTransportError) as caught:
            send(exchange)
        self.assertEqual("unexpected", caught.exception.detail)
        self.assertNotIn("Unanticipated", caught.exception.detail)


class TheOriginalIsUnreachableNotMerelyHiddenTests(unittest.TestCase):
    def test_neither_link_survives(self) -> None:
        # Both, deliberately. Asserting only `__cause__` would pass for `raise ... from None`, which
        # is the form this item had to reject: it sets `__suppress_context__` and drops nothing.
        for error, label in UNANTICIPATED:
            with self.subTest(error=label):
                exchange, _ = remote_failing(at="request", error=error)
                with self.assertRaises(PromptModelTransportError) as caught:
                    send(exchange)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    def test_no_fragment_of_the_payload_is_reachable_by_any_walk(self) -> None:
        exchange, _ = remote_failing(at="request", error=carrying_marker())
        with self.assertRaises(PromptModelTransportError) as caught:
            send(exchange)
        rendered = reachable_text(caught.exception)
        self.assertIn("transport", rendered.lower())
        self.assertNotIn(MARKER, rendered)
        self.assertNotIn(SECRET, rendered)

    def test_the_marker_probe_can_actually_fail(self) -> None:
        # A negative assertion is worth only as much as the probe behind it. This pins that
        # `reachable_text` does find the payload when the link is present, so the assertion above
        # is evidence rather than a tautology.
        original = carrying_marker()
        try:
            try:
                raise original
            except UnicodeEncodeError as exc:
                raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "x") from exc
        except PromptModelTransportError as chained:
            self.assertIn(MARKER, reachable_text(chained))

    def test_from_none_alone_would_not_have_been_enough(self) -> None:
        # The measurement that changed this item's design, kept as a test so the reasoning cannot
        # quietly rot: suppression is not severance.
        try:
            try:
                raise carrying_marker()
            except UnicodeEncodeError:
                raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "x") from None
        except PromptModelTransportError as suppressed:
            self.assertIsNone(suppressed.__cause__)
            self.assertTrue(suppressed.__suppress_context__)
            self.assertIsNotNone(suppressed.__context__)
            self.assertIn(MARKER, reachable_text(suppressed))


class TheBackstopShadowsNothingTests(unittest.TestCase):
    def test_a_contract_error_still_propagates_unretyped(self) -> None:
        # It is `ValueError`-derived, so a blanket `except Exception` would capture it and report a
        # broken internal invariant as a network problem.
        def through_remote() -> object:
            exchange, _ = remote_failing(
                at="request", error=PromptModelContractError("upstream_status")
            )
            return send(exchange)

        def through_loopback() -> object:
            exchange = loopback_failing(PromptModelContractError("upstream_status"))
            return exchange.request("GET", "/api/tags")

        for name, raiser in (("remote", through_remote), ("loopback", through_loopback)):
            with self.subTest(exchange=name):
                with self.assertRaises(PromptModelContractError) as caught:
                    raiser()
                self.assertEqual("upstream_status", caught.exception.code)

    def test_every_named_family_still_produces_its_own_outcome(self) -> None:
        # If the backstop were ordered ahead of these, each would silently become "unexpected".
        for error, expected in (
            (TimeoutError("slow"), PromptModelOutcomeId.TIMEOUT),
            (ssl.SSLError("bad cert"), PromptModelOutcomeId.EGRESS_REFUSED),
            (ConnectionResetError("reset"), PromptModelOutcomeId.TRANSPORT),
            (OSError("socket"), PromptModelOutcomeId.TRANSPORT),
            (http.client.HTTPException("protocol"), PromptModelOutcomeId.TRANSPORT),
        ):
            with self.subTest(error=type(error).__name__):
                exchange, _ = remote_failing(at="request", error=error)
                with self.assertRaises(PromptModelTransportError) as caught:
                    send(exchange)
                self.assertIs(caught.exception.outcome_id, expected)
                self.assertNotEqual("unexpected", caught.exception.detail)
                self.assertIsNone(caught.exception.unexpected_type)

    def test_a_transport_error_raised_inside_still_passes_straight_through(self) -> None:
        marker = PromptModelTransportError(PromptModelOutcomeId.MODEL_MISSING, "mine")
        exchange, _ = remote_failing(at="request", error=marker)
        with self.assertRaises(PromptModelTransportError) as caught:
            send(exchange)
        self.assertIs(marker, caught.exception)


class ASessionHandlesAContractErrorFromEitherLegTests(unittest.TestCase):
    """The transport lets `PromptModelContractError` through, so its caller has to catch it.

    `run_prompt_model_session` calls `request` twice. The identity leg has always been wrapped for
    both error types; the chat leg was wrapped for the transport error only, so a contract error
    there would have left the session function uncaught -- the same untyped escape one level up
    from the one this item closes. A review found it, and it survived the first mutation round
    because the fix had been written without a case that could see it.
    """

    def test_neither_leg_lets_a_contract_error_escape_the_session(self) -> None:
        for leg, exchange in (
            ("identity", FakeExchange(tags=PromptModelContractError("tags_row"))),
            (
                "chat",
                FakeExchange(
                    tags=ollama_tags(), raises=PromptModelContractError("upstream_status")
                ),
            ),
        ):
            with self.subTest(leg=leg):
                result = run_prompt_model_session(session_request(), exchange)
                self.assertIsNone(result.answer)
                self.assertIs(result.outcome.outcome_id, PromptModelOutcomeId.MALFORMED_RESPONSE)


class TheLoopbackNamedFamiliesAreNotShadowedEitherTests(unittest.TestCase):
    def test_each_keeps_its_own_outcome(self) -> None:
        # Written because the remote-only version of this assertion would not have caught it: the
        # loopback maps `ConnectionError` to BACKEND_ABSENT, a mapping the remote twin does not
        # have, so an ordering mistake there would have been invisible.
        for error, expected in (
            (TimeoutError("slow"), PromptModelOutcomeId.TIMEOUT),
            (ConnectionRefusedError("no server"), PromptModelOutcomeId.BACKEND_ABSENT),
            (OSError("socket"), PromptModelOutcomeId.TRANSPORT),
            (http.client.HTTPException("protocol"), PromptModelOutcomeId.TRANSPORT),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(PromptModelTransportError) as caught:
                    loopback_failing(error).request("GET", "/api/tags")
                self.assertIs(caught.exception.outcome_id, expected)
                self.assertNotEqual("unexpected", caught.exception.detail)
                self.assertIsNone(caught.exception.unexpected_type)


class NothingEscapesFromBeforeTheTryEitherTests(unittest.TestCase):
    """The two routes a review found, both of which made AC-01 false as first written.

    Both live *before* the `try` statement, which is why the backstop could never have caught them
    and why "add a backstop" was not on its own the whole fix. Both are pre-existing rather than
    introduced here, and both are closed now because the acceptance criterion this item wrote for
    itself says no `Exception` leaves these methods untyped.
    """

    def test_a_timeout_too_large_to_be_a_float_is_refused_not_a_crash(self) -> None:
        # `isinstance(value, (int, float))` admits an arbitrarily large int, and `float(value)` is
        # the first thing that can discover it cannot be one. OverflowError is neither a
        # TypeError nor a ValueError and left the boundary raw.
        self.assertTrue(issubclass(OverflowError, Exception))
        with self.assertRaises(PromptModelTransportError) as caught:
            _timeout(10**400, 30.0)
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.TRANSPORT)
        self.assertEqual("timeout", caught.exception.detail)

    def test_both_transports_refuse_that_timeout_rather_than_crashing(self) -> None:
        exchange, _ = remote_failing(at="none", error=Unanticipated("unused"))
        with self.assertRaises(PromptModelTransportError) as remote:
            exchange.request("POST", POLICY.chat_route, {"model": "m"}, timeout_seconds=10**400)
        self.assertEqual("timeout", remote.exception.detail)

        loopback = LoopbackJsonExchange(
            admit_egress_destination(PromptModelFamily.OLLAMA, OLLAMA_ENDPOINT)
        )
        with self.assertRaises(PromptModelTransportError) as local:
            loopback.request("GET", "/api/tags", timeout_seconds=10**400)
        self.assertEqual("timeout", local.exception.detail)

    def test_a_payload_too_deep_to_serialize_is_refused_not_a_crash(self) -> None:
        # `json.dumps` recurses, so RecursionError -- a RuntimeError, matching neither of the two
        # families the serialization block caught. The decode direction already had
        # `_refuse_deep_nesting` for exactly this; the encode direction had nothing.
        self.assertFalse(issubclass(RecursionError, (TypeError, ValueError)))
        deep: dict[str, object] = {}
        cursor = deep
        for _ in range(6000):
            child: dict[str, object] = {}
            cursor["n"] = child
            cursor = child

        exchange, _ = remote_failing(at="none", error=Unanticipated("unused"))
        with self.assertRaises(PromptModelTransportError) as remote:
            exchange.request("POST", POLICY.chat_route, deep)
        self.assertEqual("payload", remote.exception.detail)

        loopback = LoopbackJsonExchange(
            admit_egress_destination(PromptModelFamily.OLLAMA, OLLAMA_ENDPOINT)
        )
        with self.assertRaises(PromptModelTransportError) as local:
            loopback.request("POST", "/api/chat", deep)
        self.assertEqual("payload", local.exception.detail)


class TheCredentialIsNotLeftInTheFrameTests(unittest.TestCase):
    """A third route, distinct from `__cause__` and `__context__`.

    `authorization_header()` and `api_key_header()` return plain strings, so while the method runs
    the raw credential sits in the `headers` local, and a frame stays alive on the `__traceback__`
    of anything raised there -- where `f_locals` hands it to any reporter that captures frame
    locals. `RuntimeCredential` redacts itself, but that protects the wrapper, not a header value
    already formatted out of it.

    These cases must not use `assertRaises`. It stores `exc_value.with_traceback(None)` and calls
    `traceback.clear_frames` first, so the exception it hands back has no traceback and no frames
    at all -- an assertion that nothing is reachable through them would hold no matter how leaky
    the code was. The first draft of these tests did use it, and the positive control below is what
    exposed that they were vacuous.
    """

    def test_no_traceback_frame_still_holds_the_header_value(self) -> None:
        for step in ("request", "getresponse", "read"):
            with self.subTest(step=step):
                exchange, _ = remote_failing(at=step, error=Unanticipated("boom"))
                try:
                    send(exchange)
                except PromptModelTransportError as raised:
                    self.assertIsNotNone(raised.__traceback__)
                    self.assertEqual([], frames_holding(raised, SECRET))
                else:  # pragma: no cover - the call above must fail
                    self.fail("the request was expected to fail")

    def test_the_frame_probe_can_actually_fail(self) -> None:
        # The positive control, for the same reason the marker walk has one: a negative assertion
        # is worth exactly what its probe is worth. This one earned its place immediately.
        def holds_it() -> None:
            headers = {"Authorization": "Bearer " + SECRET}  # noqa: F841 - the local is the point
            raise Unanticipated("boom")

        try:
            holds_it()
        except Unanticipated as raised:
            self.assertEqual([("holds_it", "headers")], frames_holding(raised, SECRET))
        else:  # pragma: no cover - the call above must fail
            self.fail("the call was expected to fail")


class InterruptionIsNotATransportOutcomeTests(unittest.TestCase):
    def test_base_exceptions_still_unwind(self) -> None:
        # `except Exception` rather than `except BaseException` is the reason. A user cancelling is
        # not a provider failure, and converting an interpreter shutdown into one would be a hang.
        for error in (KeyboardInterrupt(), SystemExit(1), GeneratorExit()):
            with self.subTest(error=type(error).__name__):
                exchange, _ = remote_failing(at="request", error=error)
                with self.assertRaises(type(error)):
                    send(exchange)


class TheCloseCannotReplaceTheOutcomeTests(unittest.TestCase):
    def test_a_raising_close_does_not_overwrite_the_error_in_flight(self) -> None:
        # Before this item an exception from `close()` replaced whatever the body had raised, so the
        # caller received the cleanup failure and never saw the real one.
        exchange, made = remote_failing(
            at="request",
            error=TimeoutError("slow"),
            close_error=OSError("close failed"),
        )
        with self.assertRaises(PromptModelTransportError) as caught:
            send(exchange)
        self.assertIs(caught.exception.outcome_id, PromptModelOutcomeId.TIMEOUT)
        self.assertEqual(1, made[0].closes)

    def test_a_raising_close_does_not_escape_a_successful_call(self) -> None:
        # `at="none"` never matches a step, so the body succeeds and only the close fails.
        exchange, made = remote_failing(
            at="none", error=Unanticipated("unused"), close_error=OSError("close failed")
        )
        answer = send(exchange)
        self.assertIsInstance(answer, Mapping)
        self.assertEqual(1, made[0].closes)

    def test_the_connection_is_closed_exactly_once_on_the_unexpected_path(self) -> None:
        exchange, made = remote_failing(at="getresponse", error=Unanticipated("boom"))
        with self.assertRaises(PromptModelTransportError):
            send(exchange)
        self.assertEqual(1, made[0].closes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
