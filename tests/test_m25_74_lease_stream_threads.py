"""Which thread decides whether a lease is still current.

The route streams a body on the server's event loop and answers a release there. A claim's
currentness touches the filesystem, so neither may reach a claim: the reaper thread and the
worker operations decide, and a reader only observes what they decided.

A lease's own expiry needs no claim: the reader and the lookups read the lease's clock themselves
and do not wait for a reaper pass to learn of it.

Whoever asks a claim or a probe does so without the authority's lock. The event loop takes that
lock for every chunk and every count, so a pass or a worker operation that held it while it
waited for the filesystem would stop every stream for as long as the wait lasts. The tests of
that hold a question open on one thread and require that another thread's work finishes.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, TypeVar, cast

import pytest
from test_m25_45_lease_cross_boundary import (
    SizedClaim,
    lease_request,
    open_command,
    release_command,
)
from test_m25_media_derivative_contract import create_wire
from test_m25_media_derivative_routes import same_origin
from test_m25_media_source_leases import command

import comfyui_h3_context.adapters.authoring_media_leases as leases_module
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.adapters.comfyui_authoring_media_leases import (
    CAPABILITY_HEADER,
    LEASE_ROUTE,
    MediaLeaseRouteService,
)
from comfyui_h3_context.core.authoring_media import (
    LEASE_TTL_SECONDS,
    MAX_WORKSPACE_VIDEO_LEASES,
    REQUEST_SCHEMA,
    CreateLeaseRequest,
    MediaLeaseError,
    VerifiedDerivative,
)

web = pytest.importorskip("aiohttp.web")
http_test = pytest.importorskip("aiohttp.test_utils")
TestClient, TestServer = http_test.TestClient, http_test.TestServer

BODY_BYTES = 5 * 65536 + 17


class RecordingClaim(SizedClaim):
    """Remembers which thread asked each of its questions."""

    def __init__(self) -> None:
        super().__init__("video_proxy", BODY_BYTES)
        self.asked: list[tuple[str, int]] = []

    def _record(self, question: str) -> None:
        self.asked.append((question, threading.get_ident()))

    def current(self) -> bool:
        self._record("current")
        return super().current()

    def confirm(self, deadline: float) -> None:
        self._record("confirm")
        super().confirm(deadline)

    def generate(
        self, deadline: float, cancellation: object
    ) -> tuple[bytearray, VerifiedDerivative]:
        self._record("generate")
        return super().generate(deadline, cancellation)

    def retention_probe(self) -> Callable[[], bool] | None:
        self._record("retention_probe")
        return self._source_alive

    def _source_alive(self) -> bool:
        self._record("probe")
        return True


def test_no_claim_is_evaluated_on_the_event_loop_thread() -> None:
    claim = RecordingClaim()

    async def run() -> int:
        loop_thread = threading.get_ident()
        # The product composition: a retaining authority with its own reaper thread.
        authority = MediaLeaseAuthority(lambda _request: claim, retention_seconds=900)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        app.router.add_post(LEASE_ROUTE + "/open", service.open)
        try:
            async with TestClient(TestServer(app)) as client:
                headers = same_origin(client)
                created = await client.post(LEASE_ROUTE, json=create_wire(), headers=headers)
                assert created.status == 200
                receipt = await created.json()
                private = dict(headers, **{CAPABILITY_HEADER: created.headers[CAPABILITY_HEADER]})

                def command(operation: str) -> dict[str, object]:
                    return {
                        "schema": REQUEST_SCHEMA,
                        "operation": operation,
                        "requestId": "threads-" + operation,
                        **{
                            key: receipt[key]
                            for key in ("leaseId", "revision", "ownerId", "runtimeEpoch")
                        },
                    }

                opened = await client.post(
                    LEASE_ROUTE + "/open", json=command("open"), headers=private
                )
                assert opened.status == 200
                assert len(await opened.read()) == BODY_BYTES
                renewed = await client.post(LEASE_ROUTE, json=command("renew"), headers=private)
                assert renewed.status == 200
                receipt = await renewed.json()
                released = await client.post(LEASE_ROUTE, json=command("release"), headers=private)
                assert released.status == 200
                assert authority.resources()["leases"] == 0
                assert authority.retained() == {"entries": 1, "bytes": BODY_BYTES}
        finally:
            service.close()
        return loop_thread

    loop_thread = asyncio.run(run())
    questions = {question for question, _ in claim.asked}
    assert {"current", "confirm", "generate", "retention_probe"} <= questions
    assert [entry for entry in claim.asked if entry[1] == loop_thread] == []


def test_a_reader_observes_and_never_evaluates() -> None:
    claim = RecordingClaim()
    authority = MediaLeaseAuthority(lambda _request: claim, start_reaper=False)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        del claim.asked[:]
        assert len(read.read_chunk(65536)) == 65536

        # The claim stops being current. Nothing has decided that yet, so the reader, which asks
        # nobody, keeps reading ...
        claim.live = False
        assert len(read.read_chunk(65536)) == 65536
        assert claim.asked == []
        # ... until the reaper decides, which the reader then observes.
        authority.reap()
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            read.read_chunk(65536)
        assert {question for question, _ in claim.asked} == {"current"}
    finally:
        authority.close()


def test_revocation_reaches_an_open_stream_through_the_reaper() -> None:
    claim = RecordingClaim()
    authority = MediaLeaseAuthority(lambda _request: claim)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        assert read.read_chunk(65536)
        del claim.asked[:]
        reader = threading.get_ident()

        claim.live = False
        deadline = time.monotonic() + 10
        revoked = False
        while time.monotonic() < deadline and not revoked:
            try:
                read.read_chunk(1)
            except MediaLeaseError as error:
                assert error.reason == "lease_gone"
                revoked = True
            else:
                time.sleep(0.01)
        assert revoked
        assert claim.asked and all(thread != reader for _, thread in claim.asked)
        assert authority.resources() == {
            "leases": 0,
            "cacheEntries": 0,
            "cacheBytes": 0,
            "activeReads": 0,
        }
    finally:
        authority.close()


class _OneWait:
    """Stands in for the reaper's wake event: records one wait, then lets the loop end."""

    def __init__(self, authority: MediaLeaseAuthority) -> None:
        self._authority = authority
        self.timeouts: list[float] = []
        self.calls: list[str] = []

    def wait(self, timeout: float) -> bool:
        self.calls.append("wait")
        self.timeouts.append(timeout)
        self._authority._stopped.set()
        return False

    def clear(self) -> None:
        self.calls.append("clear")

    def set(self) -> None:
        pass


def _one_reaper_iteration(authority: MediaLeaseAuthority) -> _OneWait:
    waits = _OneWait(authority)
    wake: Any = authority._wake
    authority._wake = waits  # type: ignore[assignment]
    try:
        authority._reaper()
    finally:
        authority._stopped.clear()
        authority._wake = wake
    return waits


def _reaper_wait(authority: MediaLeaseAuthority) -> float:
    (timeout,) = _one_reaper_iteration(authority).timeouts
    return timeout


def test_the_reaper_runs_four_times_as_often_while_a_body_is_streaming() -> None:
    claim = RecordingClaim()
    authority = MediaLeaseAuthority(lambda _request: claim, start_reaper=False)
    try:
        assert _reaper_wait(authority) == 1.0
        receipt, capability = authority.create(lease_request("video_proxy"))
        assert _reaper_wait(authority) == 1.0
        read = authority.open(open_command(receipt), capability)
        # Opening wakes the reaper, so the shorter period starts with the stream.
        assert authority._wake.is_set()
        assert _reaper_wait(authority) == 0.25
        read.discard()
        assert _reaper_wait(authority) == 1.0
    finally:
        authority.close()


def test_the_reaper_consumes_the_wake_up_an_open_left(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = RecordingClaim()
    authority = MediaLeaseAuthority(lambda _request: claim, start_reaper=False)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        assert authority._wake.is_set()
        woken: list[bool] = []

        def one_pass() -> None:
            # What the event looks like when the reaper gets to work; then let the loop end.
            woken.append(authority._wake.is_set())
            authority._stopped.set()

        monkeypatch.setattr(authority, "reap", one_pass)
        try:
            authority._reaper()
        finally:
            authority._stopped.clear()
        # One pass, and the wake-up is spent: the next wait lasts its whole period. An event left
        # set would turn this thread into a loop that evaluates every claim without a pause.
        assert woken == [False]
        assert not authority._wake.is_set()
        read.discard()
    finally:
        authority.close()


def test_the_reaper_consumes_a_wake_up_only_after_it_has_waited() -> None:
    authority = MediaLeaseAuthority(lambda _request: RecordingClaim(), start_reaper=False)
    try:
        # Cleared before the wait, a wake-up that an `open` set just after the reaper looked for
        # readers would be lost, and that stream's first pass would come after the idle period.
        assert _one_reaper_iteration(authority).calls == ["wait", "clear"]
    finally:
        authority.close()


class _WatchedWake(threading.Event):
    """The reaper's wake event, telling a test that the reaper waits and how each wait ended."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = threading.Event()
        self.woken: list[bool] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waiting.set()
        woken = super().wait(timeout)
        self.woken.append(woken)
        return woken


def test_close_wakes_the_reaper_which_ends_without_another_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A period no close can outwait: a close that only set the stop flag fails here instead of
    # passing slowly.
    monkeypatch.setattr(leases_module, "_IDLE_REAP_SECONDS", 30.0)
    authority = MediaLeaseAuthority(lambda _request: RecordingClaim(), start_reaper=False)
    wake = _WatchedWake()
    authority._wake = wake
    passes: list[int] = []
    monkeypatch.setattr(authority, "reap", lambda: passes.append(1))
    reaper = threading.Thread(target=authority._reaper, daemon=True)
    authority._thread = reaper
    reaper.start()
    try:
        assert wake.waiting.wait(5), "the reaper never waited"
    finally:
        authority.close()
    assert not reaper.is_alive()
    assert wake.woken == [True]
    # Woken to stop, it stops: it does not ask every claim once more while the authority closes.
    assert passes == []


def _clocked(claim: RecordingClaim) -> tuple[MediaLeaseAuthority, list[float]]:
    now = [100.0]
    authority = MediaLeaseAuthority(
        lambda _request: claim, clock=lambda: now[0], start_reaper=False
    )
    return authority, now


def test_a_reader_sees_its_lease_expire_before_any_reaper_pass() -> None:
    claim = RecordingClaim()
    authority, now = _clocked(claim)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        assert len(read.read_chunk(65536)) == 65536
        del claim.asked[:]

        now[0] += LEASE_TTL_SECONDS
        # No pass has run and no claim is asked: the reader reads the lease's own clock.
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            read.read_chunk(65536)
        assert claim.asked == []
        assert authority.resources()["activeReads"] == 0
    finally:
        authority.close()


def test_a_lookup_ends_an_expired_lease_without_asking_its_claim() -> None:
    claim = RecordingClaim()
    authority, now = _clocked(claim)
    try:
        request = lease_request("video_proxy")
        receipt, capability = authority.create(request)
        command = open_command(receipt)
        assert authority.lease_scope(command, capability) == request.scope
        del claim.asked[:]

        now[0] += LEASE_TTL_SECONDS
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            authority.lease_scope(command, capability)
        assert authority.resources()["leases"] == 0
        assert claim.asked == []
    finally:
        authority.close()


def test_a_release_ends_an_expired_lease_no_pass_has_removed() -> None:
    claim = RecordingClaim()
    authority, now = _clocked(claim)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        del claim.asked[:]

        now[0] += LEASE_TTL_SECONDS
        # The answer a client got when a reap had already removed the lease: the release succeeds.
        released = authority.release(release_command(receipt), capability)
        assert released["operation"] == "release"
        assert released["leaseId"] == receipt["leaseId"]
        assert authority.resources()["leases"] == 0
        # The same release again is answered the same way, as for any ended lease.
        assert authority.release(release_command(receipt), capability) == released
        assert claim.asked == []
    finally:
        authority.close()


def test_a_release_repeated_after_its_tombstone_expired_finds_no_lease() -> None:
    claim = RecordingClaim()
    authority, now = _clocked(claim)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        released = authority.release(release_command(receipt), capability)
        assert authority.release(release_command(receipt), capability) == released
        del claim.asked[:]

        now[0] += LEASE_TTL_SECONDS
        # No pass has purged the tombstone. It has expired all the same, and the release is
        # answered as it was when a reap purged the tombstones before every lookup.
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            authority.release(release_command(receipt), capability)
        assert claim.asked == []
    finally:
        authority.close()


def _worker_operation(
    authority: MediaLeaseAuthority, operation: str, receipt: dict[str, object], capability: str
) -> object:
    if operation == "open":
        return authority.open(open_command(receipt), capability)
    if operation == "renew":
        return authority.renew(command(receipt, "renew"), capability)
    return authority.transfer(
        command(receipt, "transfer", next_owner_id="clip-9", next_runtime_epoch=2), capability
    )


@pytest.mark.parametrize("operation", ["open", "renew", "transfer"])
def test_a_worker_operation_finds_no_lease_whose_claim_went_stale(operation: str) -> None:
    claim = RecordingClaim()
    authority = MediaLeaseAuthority(lambda _request: claim, start_reaper=False)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        claim.live = False
        # No pass has run. The operation reaps before it looks the lease up, as the lookup itself
        # did while it still evaluated claims, so its answer does not depend on whether the
        # reaper got there first.
        with pytest.raises(MediaLeaseError) as raised:
            _worker_operation(authority, operation, receipt, capability)
        assert raised.value.reason == "lease_gone"
        assert authority.resources()["leases"] == 0
    finally:
        authority.close()


@pytest.mark.parametrize("ended", ["expired", "stale"])
def test_a_create_is_admitted_against_the_leases_that_are_still_live(ended: str) -> None:
    owners = [f"clip-{number}" for number in range(1, MAX_WORKSPACE_VIDEO_LEASES + 2)]
    claims = {owner: SizedClaim("video_proxy", 64, owner) for owner in owners}
    now = [100.0]
    authority = MediaLeaseAuthority(
        lambda request: claims[request.owner_id], clock=lambda: now[0], start_reaper=False
    )
    try:
        for owner in owners[:-1]:
            authority.create(lease_request("video_proxy", owner))
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            authority.create(lease_request("video_proxy", owners[-1]))
        if ended == "expired":
            now[0] += LEASE_TTL_SECONDS
        else:
            for owner in owners[:-1]:
                claims[owner].live = False
        # No pass has run. The create reaps before it counts the workspace's leases, so a lease
        # that has ended does not take the place of one that can still be granted.
        authority.create(lease_request("video_proxy", owners[-1]))
        assert authority.resources()["leases"] == 1
    finally:
        authority.close()


class _ConfirmedSlowly(RecordingClaim):
    """A claim during whose confirmation a test can let something happen."""

    def __init__(self) -> None:
        super().__init__()
        self.during_confirm: Callable[[], None] | None = None

    def confirm(self, deadline: float) -> None:
        super().confirm(deadline)
        if self.during_confirm is not None:
            self.during_confirm()


@pytest.mark.parametrize("ended", ["expired", "stale"])
def test_a_replayed_create_finds_no_lease_that_ended_while_it_was_confirmed(ended: str) -> None:
    claim = _ConfirmedSlowly()
    authority, now = _clocked(claim)
    try:
        request = lease_request("video_proxy")
        authority.create(request)
        now[0] += LEASE_TTL_SECONDS - 10

        def end() -> None:
            if ended == "expired":
                now[0] += 10
            else:
                claim.live = False

        claim.during_confirm = end
        # A replay hands the lease's capability out again, so the lease is confirmed first. The
        # create reaps once more after that confirmation and answers for the lease as it is then.
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            authority.create(request)
        assert authority.resources()["leases"] == 0
    finally:
        authority.close()


@pytest.mark.parametrize("operation", ["open", "renew", "transfer"])
@pytest.mark.parametrize("ended", ["released", "expired"])
def test_a_worker_operation_finds_no_lease_that_ended_while_it_was_confirmed(
    operation: str, ended: str
) -> None:
    claim = _ConfirmedSlowly()
    authority, now = _clocked(claim)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        now[0] += LEASE_TTL_SECONDS - 10

        def end() -> None:
            if ended == "expired":
                now[0] += 10
            else:
                authority.release(release_command(receipt), capability)

        claim.during_confirm = end
        # The confirmation runs without the lock, so the lease can end during it. The operation
        # looks the lease up again afterwards and answers for it as it is then: it neither opens
        # a body nor renews an owner for a lease that is no longer there.
        with pytest.raises(MediaLeaseError) as raised:
            _worker_operation(authority, operation, receipt, capability)
        assert raised.value.reason == "lease_gone"
        assert authority.resources() == {
            "leases": 0,
            "cacheEntries": 0,
            "cacheBytes": 0,
            "activeReads": 0,
        }
    finally:
        authority.close()


def test_an_open_finds_its_lease_opened_while_it_was_confirmed() -> None:
    claim = _ConfirmedSlowly()
    authority = MediaLeaseAuthority(lambda _request: claim, start_reaper=False)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        first: list[object] = []

        def open_again() -> None:
            claim.during_confirm = None
            first.append(authority.open(open_command(receipt), capability))

        claim.during_confirm = open_again
        # Two opens passed the first look at the lease before either confirmed it. The one that
        # finishes second must not hand out a second reader over the first one's.
        with pytest.raises(MediaLeaseError) as raised:
            authority.open(open_command(receipt), capability)
        assert raised.value.reason == "lease_gone"
        (read,) = first
        assert len(cast(Any, read).read_chunk(65536)) == 65536
        assert authority.resources()["activeReads"] == 1
    finally:
        authority.close()


def test_a_create_refused_after_its_claim_was_confirmed_asks_nothing_more() -> None:
    claim = _ConfirmedSlowly()
    now = [100.0]
    authority = MediaLeaseAuthority(
        lambda _request: claim, clock=lambda: now[0], start_reaper=False, retention_seconds=900
    )
    try:

        def run_out() -> None:
            now[0] += 43.0

        claim.during_confirm = run_out
        # The operation's budget ran out while the claim was confirmed. The create is refused
        # there: it does not go on to ask for a probe or to generate a body nobody will get.
        with pytest.raises(MediaLeaseError) as raised:
            authority.create(lease_request("video_proxy"))
        assert raised.value.reason == "timeout"
        assert [question for question, _ in claim.asked] == ["confirm"]
        assert authority.resources()["cacheEntries"] == 0
    finally:
        authority.close()


class _Thread:
    """Runs one call on its own thread and keeps what it returned or raised."""

    def __init__(self, work: Callable[[], object]) -> None:
        self.results: list[object] = []
        self.failures: list[BaseException] = []
        self._thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self._thread.start()

    def _run(self, work: Callable[[], object]) -> None:
        try:
            self.results.append(work())
        except BaseException as error:
            self.failures.append(error)

    def finished(self, timeout: float = 5.0) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


class _Hold:
    """Stops one named question on every thread but the test's own until the test lets it go."""

    def __init__(self) -> None:
        self._test_thread = threading.get_ident()
        self.question: str | None = None
        self.skip = 0
        self.entered = threading.Event()
        self.go = threading.Event()

    def at(self, question: str) -> None:
        if question != self.question or threading.get_ident() == self._test_thread:
            return
        if self.skip:
            self.skip -= 1
            return
        self.entered.set()
        # Bounded, so that a failing test ends instead of hanging the run.
        self.go.wait(30)


class HeldClaim(RecordingClaim):
    """A claim any one question of which a test can hold open while other threads work."""

    def __init__(self) -> None:
        super().__init__()
        self.hold = _Hold()
        self.source_alive = True

    def current(self) -> bool:
        self.hold.at("current")
        return super().current()

    def confirm(self, deadline: float) -> None:
        self.hold.at("confirm")
        super().confirm(deadline)

    def generate(
        self, deadline: float, cancellation: object
    ) -> tuple[bytearray, VerifiedDerivative]:
        self.hold.at("generate")
        return super().generate(deadline, cancellation)

    def retention_probe(self) -> Callable[[], bool] | None:
        self.hold.at("retention_probe")
        return super().retention_probe()

    def _source_alive(self) -> bool:
        self.hold.at("probe")
        self._record("probe")
        return self.source_alive


Result = TypeVar("Result")


def _event_loop_can(work: Callable[[], Result]) -> Result:
    """Run what the server's event loop does between two chunks and require that it finishes.

    The loop takes the authority's lock for every chunk and every count. Run on a thread of its
    own, so that a lock held behind a waiting question fails the test instead of hanging it.
    """

    loop = _Thread(work)
    assert loop.finished(), "the event loop waited behind a question that was still open"
    assert loop.failures == []
    (result,) = loop.results
    return cast(Result, result)


def test_a_pass_that_waits_for_a_claim_does_not_hold_the_lock() -> None:
    claim = HeldClaim()
    authority = MediaLeaseAuthority(lambda _request: claim, start_reaper=False)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        claim.hold.question = "current"
        one_pass = _Thread(authority.reap)
        try:
            assert claim.hold.entered.wait(5), "the pass never asked the claim"
            assert _event_loop_can(lambda: len(read.read_chunk(65536))) == 65536
            assert _event_loop_can(authority.resources)["activeReads"] == 1
        finally:
            claim.hold.go.set()
            assert one_pass.finished()
        assert one_pass.failures == []
    finally:
        authority.close()


def test_a_pass_that_waits_for_the_probe_of_an_idle_body_does_not_hold_the_lock() -> None:
    claim = HeldClaim()
    authority = MediaLeaseAuthority(
        lambda _request: claim, start_reaper=False, retention_seconds=900
    )
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        authority.release(release_command(receipt), capability)
        assert authority.retained() == {"entries": 1, "bytes": BODY_BYTES}
        claim.hold.question = "probe"
        one_pass = _Thread(authority.reap)
        try:
            assert claim.hold.entered.wait(5), "the pass never asked the probe"
            assert _event_loop_can(authority.resources)["cacheEntries"] == 1
        finally:
            claim.hold.go.set()
            assert one_pass.finished()
        assert one_pass.failures == []
        assert authority.retained() == {"entries": 1, "bytes": BODY_BYTES}
    finally:
        authority.close()


def test_a_pass_that_waits_for_the_probe_of_a_body_it_just_freed_does_not_hold_the_lock() -> None:
    claim = HeldClaim()
    authority = MediaLeaseAuthority(
        lambda _request: claim, start_reaper=False, retention_seconds=900
    )
    try:
        authority.create(lease_request("video_proxy"))
        # The claim stops being current: the pass ends the lease, which frees the body, and then
        # asks the source about the body it just freed.
        claim.live = False
        claim.hold.question = "probe"
        one_pass = _Thread(authority.reap)
        try:
            assert claim.hold.entered.wait(5), "the pass never asked the probe"
            assert _event_loop_can(authority.resources) == {
                "leases": 0,
                "cacheEntries": 1,
                "cacheBytes": BODY_BYTES,
                "activeReads": 0,
            }
        finally:
            claim.hold.go.set()
            assert one_pass.finished()
        assert one_pass.failures == []
    finally:
        authority.close()


@pytest.mark.parametrize(
    ("operation", "asked_before"),
    [
        pytest.param("create", 0, id="create-before-admission"),
        pytest.param("create", 1, id="create-before-registration"),
        pytest.param("replay", 1, id="create-again-after-confirming-the-replayed-lease"),
        pytest.param("open", 0, id="open"),
        pytest.param("renew", 0, id="renew"),
        pytest.param("transfer", 0, id="transfer"),
    ],
)
def test_a_worker_operation_does_not_hold_the_lock_while_it_reaps(
    operation: str, asked_before: int
) -> None:
    streaming = HeldClaim()
    other = SizedClaim("video_proxy", 64, "other")
    authority = MediaLeaseAuthority(
        lambda request: streaming if request.owner_id == "clip-1" else other, start_reaper=False
    )
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        request = lease_request("video_proxy", "clip-2")
        existing = None if operation == "create" else authority.create(request)

        def work() -> object:
            if existing is None or operation == "replay":
                return authority.create(request)
            return _worker_operation(authority, operation, *existing)

        # Every reap of the operation asks the streaming lease's claim; the one under test is the
        # first that is not skipped.
        streaming.hold.question = "current"
        streaming.hold.skip = asked_before
        worker = _Thread(work)
        try:
            assert streaming.hold.entered.wait(5), "the operation never asked the other claim"
            assert _event_loop_can(lambda: len(read.read_chunk(65536))) == 65536
            assert _event_loop_can(authority.resources)["activeReads"] == 1
        finally:
            streaming.hold.go.set()
            assert worker.finished()
        assert worker.failures == []
    finally:
        authority.close()


@pytest.mark.parametrize(
    ("operation", "question", "asked_before"),
    [
        pytest.param("create", "claim", 0, id="create-asks-for-its-claim"),
        pytest.param("create", "confirm", 0, id="create-confirms-before-it-looks-for-a-body"),
        pytest.param("create", "retention_probe", 0, id="create-asks-for-the-probe"),
        pytest.param("create", "generate", 0, id="create-generates"),
        pytest.param("create", "confirm", 1, id="create-confirms-before-it-registers"),
        pytest.param("replay", "confirm", 0, id="create-again-confirms-the-replayed-lease"),
        pytest.param("open", "confirm", 0, id="open-confirms"),
        pytest.param("renew", "confirm", 0, id="renew-confirms"),
        pytest.param("transfer", "confirm", 0, id="transfer-confirms"),
    ],
)
def test_a_worker_operation_does_not_hold_the_lock_while_it_asks_its_own_claim(
    operation: str, question: str, asked_before: int
) -> None:
    streaming = SizedClaim("video_proxy", BODY_BYTES, "streaming")
    working = HeldClaim()

    def claim(request: CreateLeaseRequest) -> SizedClaim:
        if request.owner_id == "clip-1":
            return streaming
        working.hold.at("claim")
        return working

    authority = MediaLeaseAuthority(claim, start_reaper=False, retention_seconds=900)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        request = lease_request("video_proxy", "clip-2")
        existing = None if operation == "create" else authority.create(request)

        def work() -> object:
            if existing is None or operation == "replay":
                return authority.create(request)
            return _worker_operation(authority, operation, *existing)

        # The factory, the confirmation, the generation and the probe's supplier all reach the
        # workspace or the filesystem. Each is asked between two locked sections, never in one.
        working.hold.question = question
        working.hold.skip = asked_before
        worker = _Thread(work)
        try:
            assert working.hold.entered.wait(5), f"the operation never asked: {question}"
            assert _event_loop_can(lambda: len(read.read_chunk(65536))) == 65536
            assert _event_loop_can(authority.resources)["activeReads"] == 1
        finally:
            working.hold.go.set()
            assert worker.finished()
        assert worker.failures == []
    finally:
        authority.close()


class _Refusing(HeldClaim):
    """A claim whose confirmation fails after it was asked, so that the failure is handled."""

    def confirm(self, deadline: float) -> None:
        super().confirm(deadline)
        raise MediaLeaseError("stale")


def test_a_failed_confirmation_does_not_hold_the_lock_while_it_asks_for_the_probe() -> None:
    streaming = SizedClaim("video_proxy", BODY_BYTES, "streaming")
    working = _Refusing()
    authority = MediaLeaseAuthority(
        lambda request: streaming if request.owner_id == "clip-1" else working,
        start_reaper=False,
        retention_seconds=900,
    )
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        # A confirmation that fails asks the claim whether it still offers a probe. The question
        # reaches the workspace like the confirmation itself, and it stands in the condition of
        # an `if`, where no statement of its own shows it.
        working.hold.question = "retention_probe"
        worker = _Thread(lambda: authority.create(lease_request("video_proxy", "clip-2")))
        try:
            assert working.hold.entered.wait(5), "the failed confirmation never asked for the probe"
            assert _event_loop_can(lambda: len(read.read_chunk(65536))) == 65536
            assert _event_loop_can(authority.resources)["activeReads"] == 1
        finally:
            working.hold.go.set()
            assert worker.finished()
        assert [type(error) for error in worker.failures] == [MediaLeaseError]
    finally:
        authority.close()


class _HeldDigest:
    """`hashlib` as the authority's module sees it, with the digest of one body held open."""

    def __init__(self) -> None:
        self.armed = False
        self.entered = threading.Event()
        self.go = threading.Event()

    def sha256(self, data: Any = b"") -> Any:
        # Only a body is a bytearray; whatever else the authority digests is bytes.
        if self.armed and type(data) is bytearray and not self.entered.is_set():
            self.entered.set()
            # Bounded, so that a failing test ends instead of hanging the run.
            self.go.wait(30)
        return hashlib.sha256(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(hashlib, name)


def test_the_digest_of_a_generated_body_is_not_taken_with_the_lock_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streaming = SizedClaim("video_proxy", BODY_BYTES, "streaming")
    generating = SizedClaim("video_proxy", BODY_BYTES, "generating")
    digest = _HeldDigest()
    monkeypatch.setattr(leases_module, "hashlib", digest)
    authority = MediaLeaseAuthority(
        lambda request: streaming if request.owner_id == "clip-1" else generating,
        start_reaper=False,
    )
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        # A generated body is checked against the digest its generator reported: one pass over as
        # many bytes as a derivative may have, in the condition of an `if` between two locked
        # sections.
        digest.armed = True
        worker = _Thread(lambda: authority.create(lease_request("video_proxy", "clip-2")))
        try:
            assert digest.entered.wait(5), "the create never took the digest of its body"
            assert _event_loop_can(lambda: len(read.read_chunk(65536))) == 65536
            assert _event_loop_can(authority.resources)["activeReads"] == 1
        finally:
            digest.go.set()
            assert worker.finished()
        assert worker.failures == []
    finally:
        authority.close()


def test_a_stale_stream_is_revoked_while_another_create_is_still_generating() -> None:
    streaming = SizedClaim("video_proxy", BODY_BYTES, "streaming")
    working = HeldClaim()
    # The authority's own thread, as in the product.
    authority = MediaLeaseAuthority(
        lambda request: streaming if request.owner_id == "clip-1" else working
    )
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        # An edit makes the stream's lease stale and starts the next create, which keeps the
        # authority's single-flight lock for as long as it generates. The reaper's pass does not
        # wait for that lock: the stale stream ends within the reaper's period all the same.
        working.hold.question = "generate"
        building = _Thread(lambda: authority.create(lease_request("video_proxy", "clip-2")))
        try:
            assert working.hold.entered.wait(5), "the create never reached its generation"
            streaming.live = False
            deadline = time.monotonic() + 10
            revoked = False
            while time.monotonic() < deadline and not revoked:
                try:
                    read.read_chunk(1)
                except MediaLeaseError as error:
                    assert error.reason == "lease_gone"
                    revoked = True
                else:
                    time.sleep(0.01)
            assert revoked, "the stale stream outlived a create that was still generating"
        finally:
            working.hold.go.set()
            assert building.finished()
        assert building.failures == []
    finally:
        authority.close()


def test_the_reaper_threads_own_pass_does_not_hold_the_lock() -> None:
    claim = HeldClaim()
    # The authority's own thread, as in the product: nothing in this test calls `reap`.
    authority = MediaLeaseAuthority(lambda _request: claim)
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        read = authority.open(open_command(receipt), capability)
        claim.hold.question = "current"
        try:
            assert claim.hold.entered.wait(5), "the reaper never asked the claim"
            assert _event_loop_can(lambda: len(read.read_chunk(65536))) == 65536
            assert _event_loop_can(authority.resources)["activeReads"] == 1
        finally:
            claim.hold.go.set()
    finally:
        authority.close()


def test_a_pass_that_outlives_close_ends_quietly() -> None:
    claim = HeldClaim()
    claim.source_alive = False
    authority = MediaLeaseAuthority(
        lambda _request: claim, start_reaper=False, retention_seconds=900
    )
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        authority.release(release_command(receipt), capability)
        claim.hold.question = "probe"
        one_pass = _Thread(authority.reap)
        try:
            assert claim.hold.entered.wait(5), "the pass never asked the probe"
            # The pass is about to learn that the source has ended. The authority closes first
            # and drops every body itself.
            authority.close()
            assert authority.resources()["cacheEntries"] == 0
        finally:
            claim.hold.go.set()
            assert one_pass.finished()
        # The body the pass was told to drop is gone already, and that is not an error.
        assert one_pass.failures == []
    finally:
        authority.close()


def test_a_pass_does_not_end_the_body_that_replaced_the_one_it_listed() -> None:
    claim = HeldClaim()
    now = [100.0]
    authority = MediaLeaseAuthority(
        lambda _request: claim, clock=lambda: now[0], start_reaper=False, retention_seconds=900
    )
    try:
        receipt, capability = authority.create(lease_request("video_proxy"))
        authority.release(release_command(receipt), capability)
        now[0] += 900
        claim.hold.question = "probe"
        one_pass = _Thread(authority.reap)
        try:
            assert claim.hold.entered.wait(5), "the pass never asked the probe"
            # While that pass waits, a create reaps as well: the body has reached its idle bound
            # and goes, and the create builds it again and leases it.
            authority.create(lease_request("video_proxy", "clip-2"))
            assert [question for question, _ in claim.asked].count("generate") == 2
        finally:
            claim.hold.go.set()
            assert one_pass.finished()
        assert one_pass.failures == []
        # What the pass listed is not what sits under the key now: the leased body is untouched.
        assert authority.resources() == {
            "leases": 1,
            "cacheEntries": 1,
            "cacheBytes": BODY_BYTES,
            "activeReads": 0,
        }
    finally:
        authority.close()


def test_close_stops_the_authority_before_it_wakes_the_reaper() -> None:
    authority = MediaLeaseAuthority(lambda _request: RecordingClaim(), start_reaper=False)
    stopped_when_woken: list[bool] = []

    class _Wake(threading.Event):
        def set(self) -> None:
            stopped_when_woken.append(authority._stopped.is_set())
            super().set()

    authority._wake = _Wake()
    authority.close()
    # Woken first, the reaper could find the stop flag still clear and ask every claim once more.
    assert stopped_when_woken == [True]


class _JoinedThread:
    """Stands in for the authority's reaper thread and tells a test that `close` waits for it."""

    def __init__(self, thread: threading.Thread) -> None:
        self._thread = thread
        self.joining = threading.Event()

    def join(self, timeout: float | None = None) -> None:
        self.joining.set()
        self._thread.join(timeout)


def test_close_does_not_hold_the_lock_while_it_waits_for_the_reaper() -> None:
    claim = HeldClaim()
    authority = MediaLeaseAuthority(lambda _request: claim)
    reaper = authority._thread
    assert reaper is not None
    waited_for = _JoinedThread(reaper)
    authority._thread = waited_for  # type: ignore[assignment]
    closing: _Thread | None = None
    try:
        authority.create(lease_request("video_proxy"))
        claim.hold.question = "current"
        assert claim.hold.entered.wait(5), "the reaper never asked the claim"
        # The pass is in the middle of its question when the authority closes. The answer makes
        # the lease stale, so the pass needs the lock once more before the reaper can end.
        closing = _Thread(authority.close)
        assert waited_for.joining.wait(5), "close never waited for the reaper"
        claim.live = False
    finally:
        claim.hold.go.set()
    # A close that held the lock while it waited would wait out its whole timeout here, with
    # every reader behind it.
    assert closing is not None and closing.finished(3)
    assert closing.failures == []
    assert not reaper.is_alive()
    assert authority.resources() == {
        "leases": 0,
        "cacheEntries": 0,
        "cacheBytes": 0,
        "activeReads": 0,
    }


class _OwnedLock:
    """Stands in for the authority's lock and knows whether the calling thread holds it."""

    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self._holder: int | None = None
        self._depth = 0

    def __enter__(self) -> None:
        self._lock.acquire()
        self._holder = threading.get_ident()
        self._depth += 1

    def __exit__(self, *_exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._holder = None
        self._lock.release()

    def held(self) -> bool:
        return self._holder == threading.get_ident()


class _Watched:
    """One of the authority's maps, noting every use made of it without the authority's lock."""

    def __init__(self, name: str, target: Any, lock: _OwnedLock, misuse: list[str]) -> None:
        self._name = name
        self._target = target
        self._lock = lock
        self._misuse = misuse

    def _use(self, how: str) -> Any:
        if not self._lock.held():
            self._misuse.append(f"{self._name} {how} without the lock")
        return self._target

    def __getitem__(self, key: object) -> Any:
        return self._use("read")[key]

    def __setitem__(self, key: object, value: object) -> None:
        self._use("written")[key] = value

    def __delitem__(self, key: object) -> None:
        del self._use("written")[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._use("read"))

    def __len__(self) -> int:
        return len(self._use("read"))

    def get(self, key: object, default: object = None) -> Any:
        return self._use("read").get(key, default)

    def pop(self, key: object, *default: object) -> Any:
        return self._use("written").pop(key, *default)

    def popitem(self, last: bool = True) -> Any:
        return self._use("written").popitem(last=last)

    def values(self) -> Any:
        return self._use("read").values()

    def items(self) -> Any:
        return self._use("read").items()

    def clear(self) -> None:
        self._use("written").clear()


class _Turned(RecordingClaim):
    """A claim a test turns: its confirmation can fail, its probe can be withdrawn or end."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.cache_key += key
        self.confirm_error: Exception | None = None
        self.offers_probe = True
        self.source_alive = True

    def confirm(self, deadline: float) -> None:
        super().confirm(deadline)
        if self.confirm_error is not None:
            raise self.confirm_error

    def retention_probe(self) -> Callable[[], bool] | None:
        return super().retention_probe() if self.offers_probe else None

    def _source_alive(self) -> bool:
        return self.source_alive


def test_every_use_of_the_authoritys_maps_is_made_with_its_lock_held() -> None:
    claims = {f"clip-{number}": _Turned(str(number)) for number in range(1, 5)}
    now = [100.0]
    authority = MediaLeaseAuthority(
        lambda request: claims[request.owner_id],
        clock=lambda: now[0],
        start_reaper=False,
        retention_seconds=900,
    )
    # A lock that is not taken is a race no single-threaded test can lose. What can be seen is
    # the state the lock guards: every use of the four maps is noted with whether the lock was
    # held by the thread that made it.
    lock = _OwnedLock(authority._lock)
    misuse: list[str] = []
    authority._lock = lock  # type: ignore[assignment]
    for name in ("_entries", "_cache", "_tombstones", "_requests"):
        setattr(authority, name, _Watched(name, getattr(authority, name), lock, misuse))

    def lease(owner: str) -> tuple[dict[str, object], str]:
        return authority.create(lease_request("video_proxy", owner))

    try:
        # One lease from its creation to its release, by way of every operation.
        receipt, capability = lease("clip-1")
        assert lease("clip-1")[0]["leaseId"] == receipt["leaseId"]
        scope = lease_request("video_proxy", "clip-1").scope
        assert authority.lease_scope(open_command(receipt), capability) == scope
        read = authority.open(open_command(receipt), capability)
        assert len(read.read_chunk(65536)) == 65536
        assert (
            authority.metadata(open_command(receipt), capability)["leaseId"] == receipt["leaseId"]
        )
        _one_reaper_iteration(authority)
        read.discard()
        receipt = authority.renew(command(receipt, "renew"), capability)
        receipt, capability = authority.transfer(
            command(receipt, "transfer", next_owner_id="clip-9", next_runtime_epoch=2), capability
        )
        authority.release(release_command(receipt), capability)
        assert authority.retained() == {"entries": 1, "bytes": BODY_BYTES}

        # A pass that ends a lease and, in its last section, the body that lease leaves behind.
        lease("clip-2")
        claims["clip-2"].live = False
        claims["clip-2"].source_alive = False
        authority.reap()
        assert authority.resources()["cacheEntries"] == 1

        # A confirmation that fails ends its lease: once as the claim's own refusal, with the
        # body withdrawn because the claim stopped offering a probe, and once as an error.
        refused = lease("clip-3")
        claims["clip-3"].confirm_error = MediaLeaseError("stale")
        claims["clip-3"].offers_probe = False
        with pytest.raises(MediaLeaseError, match="stale"):
            authority.renew(command(refused[0], "renew"), refused[1])
        failed = lease("clip-4")
        claims["clip-4"].confirm_error = OSError("unreadable")
        with pytest.raises(MediaLeaseError, match="stale"):
            authority.renew(command(failed[0], "renew"), failed[1])
        assert authority.resources()["leases"] == 0

        # A pass over what has expired by now: the tombstones and the replayable requests.
        now[0] += LEASE_TTL_SECONDS
        authority.reap()
    finally:
        authority.close()
    assert misuse == []
