"""Process-local derivative retention with fenced, single-owner media leases."""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..core.authoring_media import (
    DERIVATIVE_PROFILE_ID,
    LEASE_LIFETIME_SECONDS,
    LEASE_TTL_SECONDS,
    MAX_CACHE_BYTES,
    MAX_INTEGER,
    MAX_LEASES,
    MAX_WORKSPACE_DECORATION_LEASES,
    MAX_WORKSPACE_LEASES,
    MAX_WORKSPACE_VIDEO_LEASES,
    RELEASED_SCHEMA,
    RUNTIME_PROFILE_FINGERPRINT,
    SUCCESS_SCHEMA,
    CreateLeaseRequest,
    LeaseCommand,
    LeaseScope,
    MediaLeaseError,
    VerifiedDerivative,
    derivative_byte_limit,
    derivative_media_type,
)
from ..core.canonical import canonical_fingerprint

_LOGGER = logging.getLogger(__name__)
_CAPABILITY = re.compile(r"[0-9a-f]{64}\Z")
_IDLE_REAP_SECONDS = 1.0
_READING_REAP_SECONDS = 0.25
# The expiry thread reports its first failed pass and then one in this many. A fault that
# persists fails every pass, one a second while nothing streams: a line every ten minutes.
_REAPER_FAULT_REPORT_EVERY = 600


class _BuildCancellation:
    def __init__(self, stopped: threading.Event, caller: object | None) -> None:
        self._stopped = stopped
        self._caller = caller

    def is_cancelled(self) -> bool:
        checker = getattr(self._caller, "is_cancelled", None)
        return self._stopped.is_set() or (callable(checker) and bool(checker()))


class DerivativeClaim(Protocol):
    """A claim may also offer `retention_probe() -> Callable[[], bool] | None`: the liveness of
    the source its body derives from. A claim without it is never retained past its leases."""

    @property
    def cache_key(self) -> str: ...
    def current(self) -> bool: ...
    def confirm(self, deadline: float) -> None: ...
    def generate(
        self, deadline: float, cancellation: object | None
    ) -> tuple[bytearray, VerifiedDerivative]: ...


@dataclass(slots=True, repr=False)
class _Cached:
    body: bytearray
    facts: VerifiedDerivative
    references: int = 0
    # Source liveness offered by the claim that last took a lease on this body; None means the
    # body ends with its last lease. `idle_since` is set while no lease holds the body.
    retainer: Callable[[], bool] | None = None
    idle_since: float | None = None


@dataclass(slots=True, repr=False)
class _Entry:
    request: CreateLeaseRequest
    claim: DerivativeClaim
    cache_key: str
    lease_id: str
    capability: str
    born: float
    expires: float
    owner_id: str
    runtime_epoch: int
    revision: int = 1
    opened: bool = False
    read: LeaseRead | None = None


class LeaseRead:
    """A revocable cache borrow; only bounded chunk copies escape the cache owner."""

    def __init__(
        self, body: bytearray, current: Callable[[], bool], finished: Callable[[], None]
    ) -> None:
        self._body: bytearray | None = body
        self._offset = 0
        self._current = current
        self._finished = finished
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "<LeaseRead opaque>"

    def read_chunk(self, maximum: int = 65536) -> bytes:
        if type(maximum) is not int or not 1 <= maximum <= 65536:
            raise MediaLeaseError()
        if not self._current():
            self.discard()
            raise MediaLeaseError("lease_gone")
        with self._lock:
            if self._body is None:
                raise MediaLeaseError("lease_gone")
            end = min(len(self._body), self._offset + maximum)
            result = bytes(self._body[self._offset : end])
            self._offset = end
            return result

    def discard(self) -> None:
        with self._lock:
            body, self._body = self._body, None
            # CRITICAL: this is a borrow. Clearing here would corrupt another lease sharing
            # the same derivative; the cache owner clears only after revoking every reader.
        if body is not None:
            self._finished()


class MediaLeaseAuthority:
    """One bounded cache; callers inject the workspace application's opaque claim factory.

    `retention_seconds` is how long a body may stay resident after its last lease ends. Zero, the
    default, ends every body with its last lease.

    The reaper thread is what ends a stale lease while its body is streaming. `start_reaper=False`
    is for a caller that drives `reap()` itself: with neither, a stream ends only when its lease
    expires or when the next `create`, `open`, `renew` or `transfer` reaps.
    """

    def __init__(
        self,
        claim: Callable[[CreateLeaseRequest], DerivativeClaim],
        *,
        clock: Callable[[], float] = time.monotonic,
        start_reaper: bool = True,
        retention_seconds: float = 0.0,
    ) -> None:
        if (
            type(retention_seconds) not in (int, float)
            or not math.isfinite(retention_seconds)
            or retention_seconds < 0
        ):
            raise ValueError("retention_seconds is invalid")
        self._claim = claim
        self._clock = clock
        self._retention = float(retention_seconds)
        self._lock = threading.RLock()
        self._build = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._cache: dict[str, _Cached] = {}
        self._tombstones: OrderedDict[tuple[str, str, int, int, str], float] = OrderedDict()
        self._requests: OrderedDict[tuple[str, str], tuple[str, str, float]] = OrderedDict()
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._reaper_faults = 0
        self._thread: threading.Thread | None = None
        if start_reaper:
            self._thread = threading.Thread(
                target=self._reaper, name="h3-media-lease-expiry", daemon=True
            )
            self._thread.start()

    def __repr__(self) -> str:
        return "<MediaLeaseAuthority opaque>"

    @property
    def retention_seconds(self) -> float:
        return self._retention

    def _reaper(self) -> None:
        while not self._stopped.is_set():
            with self._lock:
                reading = any(entry.read is not None for entry in self._entries.values())
            # A reader does not evaluate its own claim, so while a body is streaming this thread
            # is what revokes it; `open` wakes it so that the shorter period starts at once.
            self._wake.wait(_READING_REAP_SECONDS if reading else _IDLE_REAP_SECONDS)
            # CRITICAL: consume the wake-up. An event left set makes every later wait return at
            # once, and this thread then evaluates every live claim in a tight loop for as long
            # as the process runs.
            self._wake.clear()
            if not self._stopped.is_set():
                try:
                    self.reap()
                except Exception as fault:
                    # IMPORTANT: this thread alone ends idle bodies and revokes a streaming one
                    # while no route operation arrives. An unhandled fault would end it for the
                    # life of the process and leave those bounds unenforced, so one failed pass
                    # is counted and the next pass runs. `create`, `open`, `renew` and `transfer`
                    # call `reap()` unshielded, but only when one of them arrives: in the periods
                    # this thread exists for, a fault that persists would be seen by nobody, so
                    # it is reported here.
                    self._reaper_faults += 1
                    self._report_reaper_fault(fault)

    def _report_reaper_fault(self, fault: Exception) -> None:
        """One line for the first failed pass and for one in every 600 after it.

        IMPORTANT: called without the authority's lock, and it takes none. A log write can wait
        for as long as its console does, and every request takes that lock: under it, a waiting
        write would stop them all.
        """

        if self._reaper_faults % _REAPER_FAULT_REPORT_EVERY != 1:
            return
        try:
            # IMPORTANT: the line names the exception's type and the count and nothing else. An
            # exception's message can name a file, and a traceback carries the message.
            _LOGGER.warning(
                "H3 Context media lease expiry pass failed: kind=%s count=%d",
                type(fault).__name__,
                self._reaper_faults,
            )
        except Exception:  # noqa: S110 -- the report is not worth the thread it runs on
            # A host's logging filter can raise. Letting that out would end the expiry thread,
            # which is what the shield around the pass exists to prevent.
            pass

    def _guard(self, deadline: float, cancellation: object | None = None) -> None:
        if self._stopped.is_set():
            raise MediaLeaseError("lease_gone")
        checker = getattr(cancellation, "is_cancelled", None)
        if callable(checker) and checker():
            raise MediaLeaseError("cancelled")
        if self._clock() >= deadline:
            raise MediaLeaseError("timeout")

    @staticmethod
    def _current(claim: DerivativeClaim) -> bool:
        try:
            return claim.current() is True
        except Exception:
            return False

    def _retainer(self, claim: DerivativeClaim) -> Callable[[], bool] | None:
        if self._retention <= 0:
            return None
        supplier = getattr(claim, "retention_probe", None)
        if not callable(supplier):
            return None
        try:
            probe = supplier()
        except Exception:
            return None
        return probe if callable(probe) else None

    def _confirm_claim(self, claim: DerivativeClaim, deadline: float) -> None:
        try:
            claim.confirm(deadline)
        except BaseException:
            # A claim that stops offering a probe has disproved its source's content. The body
            # derived from that content then ends with its leases, and at once if it has none.
            if self._retention > 0 and self._retainer(claim) is None:
                with self._lock:
                    cached = self._cache.get(claim.cache_key)
                    if cached is not None:
                        cached.retainer = None
                        if cached.references == 0:
                            self._drop(claim.cache_key)
            raise

    def _confirm(self, entry: _Entry, deadline: float, cancellation: object | None) -> None:
        self._guard(deadline, cancellation)
        try:
            self._confirm_claim(entry.claim, deadline)
        except MediaLeaseError as exc:
            if exc.reason in {"stale", "authority_mismatch", "lease_gone"}:
                with self._lock:
                    self._remove(entry)
            raise
        except Exception:
            with self._lock:
                self._remove(entry)
            raise MediaLeaseError("stale") from None
        self._guard(deadline, cancellation)

    def _drop(self, cache_key: str) -> None:
        self._cache.pop(cache_key).body.clear()

    def _remove(self, entry: _Entry) -> None:
        if self._entries.pop(entry.lease_id, None) is not entry:
            return
        read, entry.read = entry.read, None
        if read is not None:
            read.discard()
        cached = self._cache[entry.cache_key]
        cached.references -= 1
        if cached.references == 0:
            # CRITICAL: a body kept here has no lease and no claim. It stays reachable only
            # through `create`, which confirms a fresh claim (a full re-hash of the source under
            # the current workspace state) before it looks the body up and again before it
            # registers a lease. Never serve an idle body on any path that skips that confirm.
            if cached.retainer is None or self._stopped.is_set():
                self._drop(entry.cache_key)
            else:
                cached.idle_since = self._clock()
        key = (
            entry.lease_id,
            entry.owner_id,
            entry.runtime_epoch,
            entry.revision,
            hashlib.sha256(entry.capability.encode("ascii")).hexdigest(),
        )
        self._tombstones[key] = self._clock() + LEASE_TTL_SECONDS
        while len(self._tombstones) > 256:
            self._tombstones.popitem(last=False)

    def reap(self) -> None:
        """End expired and stale leases and idle bodies. Never call this with the lock held.

        CRITICAL: a claim's currentness and a body's probe touch the filesystem. They are
        evaluated between the locked sections, never inside one, so that a body reader -- which
        runs on the server's event loop and takes this lock for every chunk -- never waits
        behind them.
        """

        with self._lock:
            now = self._clock()
            live: list[_Entry] = []
            for entry in tuple(self._entries.values()):
                if now >= entry.expires:
                    self._remove(entry)
                else:
                    live.append(entry)
            idle = [
                (cache_key, cached, cached.retainer, cached.idle_since)
                for cache_key, cached in self._cache.items()
                if cached.references == 0
            ]
            for tombstone, expiry in tuple(self._tombstones.items()):
                if now >= expiry:
                    del self._tombstones[tombstone]
            for request_key, (_, _, expiry) in tuple(self._requests.items()):
                if now >= expiry:
                    del self._requests[request_key]
        stale = [entry for entry in live if not self._current(entry.claim)]
        ended = {id(cached) for _, cached, retainer, _ in idle if not self._alive(retainer)}
        if not stale and not idle:
            return
        with self._lock:
            already_idle = {id(cached) for cached in self._cache.values() if cached.references == 0}
            for entry in stale:
                self._remove(entry)
            self._drop_idle(idle, ended)
            freed = [
                (cache_key, cached, cached.retainer, cached.idle_since)
                for cache_key, cached in self._cache.items()
                if cached.references == 0 and id(cached) not in already_idle
            ]
        if not freed:
            return
        # A body whose last lease this pass just ended. When its source has ended as well -- a
        # released workspace is exactly that -- it goes now instead of waiting one more period.
        ended = {id(cached) for _, cached, retainer, _ in freed if not self._alive(retainer)}
        if ended:
            with self._lock:
                self._drop_idle(freed, ended)

    def _drop_idle(
        self,
        candidates: list[tuple[str, _Cached, Callable[[], bool] | None, float | None]],
        ended: set[int],
    ) -> None:
        """Drop the idle bodies a probe evaluation outside the lock judged. Lock held."""

        now = self._clock()
        for cache_key, cached, retainer, since in candidates:
            # Leased again, or leased and released again, since it was listed: the verdict was
            # about an earlier idle period and no longer applies.
            if (
                self._cache.get(cache_key) is not cached
                or cached.references != 0
                or cached.retainer is not retainer
                or cached.idle_since != since
            ):
                continue
            if id(cached) in ended or since is None or now - since >= self._retention:
                self._drop(cache_key)

    @staticmethod
    def _alive(retainer: Callable[[], bool] | None) -> bool:
        try:
            return retainer is not None and retainer() is True
        except Exception:
            return False

    def _find(self, command: LeaseCommand, capability: str, *, expired_ok: bool = False) -> _Entry:
        """The live lease this command names. Lock held; evaluates no claim.

        Staleness is the reaper's and the worker operations' business: `open`, `renew` and
        `transfer` reap before they look a lease up and then confirm it. `release` and
        `lease_scope` run on the event loop and must not reach a claim at all.
        """

        if type(capability) is not str or _CAPABILITY.fullmatch(capability) is None:
            raise MediaLeaseError("lease_gone")
        entry = self._entries.get(command.lease_id)
        if (
            entry is None
            or not hmac.compare_digest(entry.capability, capability)
            or (entry.owner_id, entry.runtime_epoch, entry.revision)
            != (command.owner_id, command.runtime_epoch, command.revision)
        ):
            raise MediaLeaseError("lease_gone")
        if not expired_ok and self._clock() >= entry.expires:
            self._remove(entry)
            raise MediaLeaseError("lease_gone")
        return entry

    def lease_scope(self, command: LeaseCommand, capability: str) -> LeaseScope:
        """Return the admitted scope without exposing private lease state."""

        with self._lock:
            return self._find(command, capability).request.scope

    def _receipt(self, entry: _Entry, request_id: str, operation: str) -> dict[str, object]:
        facts = self._cache[entry.cache_key].facts
        return {
            "schema": SUCCESS_SCHEMA,
            "requestId": request_id,
            "operation": operation,
            "leaseId": entry.lease_id,
            "revision": entry.revision,
            "ownerId": entry.owner_id,
            "runtimeEpoch": entry.runtime_epoch,
            "ttlMs": max(0, min(60_000, int((entry.expires - self._clock()) * 1000))),
            "derivativeKind": facts.kind,
            "mediaType": derivative_media_type(facts.kind),
            "byteCount": facts.byte_count,
            "derivativeFingerprint": facts.derivative.derivative_fingerprint,
            "profileFingerprint": RUNTIME_PROFILE_FINGERPRINT,
            "audioDisposition": facts.audio_disposition,
            "assetFingerprint": facts.asset_fingerprint,
            "derivativeProfileId": DERIVATIVE_PROFILE_ID,
        }

    def _capacity(self, request: CreateLeaseRequest) -> None:
        entries = tuple(self._entries.values())
        workspace = [
            entry for entry in entries if entry.request.workspace_handle == request.workspace_handle
        ]
        decorations = sum(entry.request.scope == "asset" for entry in workspace)
        # CRITICAL: asset scope is the decoration class. Keep it in the one eight-lease pool and
        # preserve two free workspace slots for clip playback; a kind-based or second-pool count
        # can let thumbnails starve playback after a mixed acquisition race.
        if (
            len(entries) >= MAX_LEASES
            or len(workspace) >= MAX_WORKSPACE_LEASES
            or (
                request.scope == "asset"
                and (
                    decorations >= MAX_WORKSPACE_DECORATION_LEASES
                    or len(workspace) >= MAX_WORKSPACE_LEASES - 2
                )
            )
            or (
                request.derivative_kind == "video_proxy"
                and sum(entry.request.derivative_kind == "video_proxy" for entry in workspace)
                >= MAX_WORKSPACE_VIDEO_LEASES
            )
        ):
            raise MediaLeaseError("resource_limit")

    def _make_room(self, incoming: int) -> None:
        # Idle bodies are a convenience and share the one byte budget with leased ones: the
        # longest-idle go first, so a create that fits without them never fails because of them.
        total = sum(len(value.body) for value in self._cache.values())
        idle = sorted(
            (
                (cached.idle_since or 0.0, key)
                for key, cached in self._cache.items()
                if cached.references == 0
            ),
        )
        # IMPORTANT: a body that cannot fit beside the leased bytes is refused by the caller
        # whatever is evicted. Evicting for it would discard every idle body, each of which a
        # later lease would have to generate again, and still end in the same refusal.
        leased = total - sum(len(self._cache[key].body) for _, key in idle)
        if leased + incoming > MAX_CACHE_BYTES:
            return
        for _, key in idle:
            if total + incoming <= MAX_CACHE_BYTES:
                return
            total -= len(self._cache[key].body)
            self._drop(key)

    def create(
        self, request: CreateLeaseRequest, *, cancellation: object | None = None
    ) -> tuple[dict[str, object], str]:
        if type(request) is not CreateLeaseRequest:
            raise MediaLeaseError()
        deadline = self._clock() + 43.0
        self._guard(deadline, cancellation)
        if not self._build.acquire(blocking=False):
            raise MediaLeaseError("busy")
        body: bytearray | None = None
        try:
            key = (request.workspace_handle, request.request_id)
            fingerprint = canonical_fingerprint(request.to_wire())
            replay: _Entry | None = None
            self.reap()
            with self._lock:
                previous = self._requests.get(key)
                if previous is not None:
                    prior_fingerprint, lease_id, _ = previous
                    entry = self._entries.get(lease_id)
                    if prior_fingerprint != fingerprint:
                        raise MediaLeaseError()
                    if entry is None or (entry.owner_id, entry.runtime_epoch) != (
                        request.owner_id,
                        request.runtime_epoch,
                    ):
                        raise MediaLeaseError("lease_gone")
                    replay = entry
                else:
                    self._capacity(request)
            if replay is not None:
                # CRITICAL: replay still returns authority. Stat identity alone misses replaced
                # bytes with restored timestamps; rehash outside the lock before returning it.
                self._confirm(replay, deadline, cancellation)
                self.reap()
                with self._lock:
                    if self._entries.get(replay.lease_id) is not replay or (
                        replay.owner_id,
                        replay.runtime_epoch,
                    ) != (request.owner_id, request.runtime_epoch):
                        raise MediaLeaseError("lease_gone")
                    return self._receipt(replay, request.request_id, "create"), replay.capability
            claim = self._claim(request)
            self._confirm_claim(claim, deadline)
            self._guard(deadline, cancellation)
            retainer = self._retainer(claim)
            with self._lock:
                cached = self._cache.get(claim.cache_key)
            if cached is None:
                body, facts = claim.generate(
                    deadline, _BuildCancellation(self._stopped, cancellation)
                )
                if type(body) is not bytearray or type(facts) is not VerifiedDerivative:
                    raise MediaLeaseError("generation_failed")
                # IMPORTANT (M25-45): a body that simply does not fit is a resource limit, not a
                # generation failure. The browser's own body admission already answers
                # `resource_limit` for the same physical condition, and a client that sees two
                # different reasons for one fact cannot tell "this source is too big for the
                # profile" from "the encoder went wrong". Keep every other mismatch below --
                # wrong kind, wrong asset, wrong length field, wrong digest -- as
                # `generation_failed`: those are the generator contradicting itself.
                if not 1 <= len(body) <= derivative_byte_limit(request.derivative_kind):
                    raise MediaLeaseError("resource_limit")
                if (
                    facts.kind != request.derivative_kind
                    or facts.source.asset_id != request.asset_id
                    or len(body) != facts.byte_count
                    or "sha256:" + hashlib.sha256(body).hexdigest()
                    != facts.derivative.derivative_fingerprint
                ):
                    raise MediaLeaseError("generation_failed")
            # CRITICAL: generation and hashing run outside the store lock. A semantic edit or
            # release during I/O must never publish a body under the earlier snapshot's lease.
            self._confirm_claim(claim, deadline)
            self._guard(deadline, cancellation)
            self.reap()
            with self._lock:
                self._capacity(request)
                self._guard(deadline, cancellation)
                if not self._current(claim):
                    raise MediaLeaseError("stale")
                live_cache = self._cache.get(claim.cache_key)
                if live_cache is None:
                    if body is None:
                        # Last concurrent owner released the cached bytes during currentness I/O.
                        raise MediaLeaseError("stale")
                    self._make_room(len(body))
                    if (
                        sum(len(value.body) for value in self._cache.values()) + len(body)
                        > MAX_CACHE_BYTES
                    ):
                        raise MediaLeaseError("resource_limit")
                    live_cache = _Cached(body, facts)
                    self._cache[claim.cache_key] = live_cache
                    body = None
                live_cache.references += 1
                live_cache.retainer = retainer
                live_cache.idle_since = None
                now = self._clock()
                entry = _Entry(
                    request,
                    claim,
                    claim.cache_key,
                    "lease-" + secrets.token_hex(16),
                    secrets.token_hex(32),
                    now,
                    now + LEASE_TTL_SECONDS,
                    request.owner_id,
                    request.runtime_epoch,
                )
                self._entries[entry.lease_id] = entry
                self._requests[key] = (fingerprint, entry.lease_id, now + LEASE_LIFETIME_SECONDS)
                while len(self._requests) > 256:
                    self._requests.popitem(last=False)
                return self._receipt(entry, request.request_id, "create"), entry.capability
        except MediaLeaseError:
            raise
        except Exception:
            raise MediaLeaseError("generation_failed") from None
        finally:
            if body is not None:
                body.clear()
            self._build.release()

    def open(
        self, command: LeaseCommand, capability: str, *, cancellation: object | None = None
    ) -> LeaseRead:
        if command.operation != "open":
            raise MediaLeaseError()
        deadline = self._clock() + 43.0
        self.reap()
        with self._lock:
            entry = self._find(command, capability)
            if entry.read is not None:
                raise MediaLeaseError("busy")
            if entry.opened:
                raise MediaLeaseError("lease_gone")
        self._confirm(entry, deadline, cancellation)
        with self._lock:
            if self._find(command, capability) is not entry or entry.opened:
                raise MediaLeaseError("lease_gone")

            def current() -> bool:
                # CRITICAL: a reader only observes its own lease. The route calls this for every
                # chunk on the server's event loop; evaluating a claim (or reaping) here puts
                # filesystem work for every live lease between two chunks of every body and
                # stalls every other request. Revocation reaches a stream through `_remove`,
                # which the reaper and the worker operations call and which discards this reader.
                with self._lock:
                    return (
                        self._entries.get(entry.lease_id) is entry
                        and entry.revision == command.revision
                        and self._clock() < entry.expires
                    )

            def finished() -> None:
                with self._lock:
                    entry.read = None

            read = LeaseRead(self._cache[entry.cache_key].body, current, finished)
            entry.read = read
            entry.opened = True
            self._wake.set()
            return read

    def metadata(self, command: LeaseCommand, capability: str) -> dict[str, object]:
        if command.operation != "open":
            raise MediaLeaseError()
        with self._lock:
            entry = self._find(command, capability)
            if not entry.opened or entry.read is None:
                raise MediaLeaseError("lease_gone")
            result = self._receipt(entry, command.request_id, command.operation)
            geometry = self._cache[entry.cache_key].facts.geometry
            result["geometry"] = None if geometry is None else geometry.to_wire()
            return result

    def renew(
        self, command: LeaseCommand, capability: str, *, cancellation: object | None = None
    ) -> dict[str, object]:
        if command.operation != "renew":
            raise MediaLeaseError()
        return self._change_owner(command, capability, cancellation)[0]

    def transfer(
        self, command: LeaseCommand, capability: str, *, cancellation: object | None = None
    ) -> tuple[dict[str, object], str]:
        if command.operation != "transfer":
            raise MediaLeaseError()
        return self._change_owner(command, capability, cancellation)

    def _change_owner(
        self, command: LeaseCommand, capability: str, cancellation: object | None
    ) -> tuple[dict[str, object], str]:
        self.reap()
        with self._lock:
            entry = self._find(command, capability)
            if entry.read is not None:
                raise MediaLeaseError("busy")
        self._confirm(entry, self._clock() + 43.0, cancellation)
        with self._lock:
            if self._find(command, capability) is not entry:
                raise MediaLeaseError("lease_gone")
            if entry.read is not None:
                raise MediaLeaseError("busy")
            if entry.revision >= MAX_INTEGER:
                self._remove(entry)
                raise MediaLeaseError("lease_gone")
            if command.operation == "transfer":
                if command.next_owner_id is None or command.next_runtime_epoch is None:
                    raise MediaLeaseError()
                entry.owner_id = command.next_owner_id
                entry.runtime_epoch = command.next_runtime_epoch
                entry.capability = secrets.token_hex(32)
                entry.opened = False
            entry.revision += 1
            entry.expires = min(
                self._clock() + LEASE_TTL_SECONDS, entry.born + LEASE_LIFETIME_SECONDS
            )
            return self._receipt(entry, command.request_id, command.operation), entry.capability

    def release(self, command: LeaseCommand, capability: str) -> dict[str, object]:
        if (
            command.operation != "release"
            or type(capability) is not str
            or _CAPABILITY.fullmatch(capability) is None
        ):
            raise MediaLeaseError()
        # The route answers a release inline on the event loop, so this reaches no claim: an
        # expired or stale lease that the reaper has not removed yet is simply removed here.
        with self._lock:
            key = (
                command.lease_id,
                command.owner_id,
                command.runtime_epoch,
                command.revision,
                hashlib.sha256(capability.encode("ascii")).hexdigest(),
            )
            ended = self._tombstones.get(key)
            if ended is None or self._clock() >= ended:
                self._remove(self._find(command, capability, expired_ok=True))
        return {
            "schema": RELEASED_SCHEMA,
            "requestId": command.request_id,
            "operation": "release",
            "leaseId": command.lease_id,
        }

    def resources(self) -> dict[str, int]:
        with self._lock:
            return {
                "leases": len(self._entries),
                "cacheEntries": len(self._cache),
                "cacheBytes": sum(len(value.body) for value in self._cache.values()),
                "activeReads": sum(entry.read is not None for entry in self._entries.values()),
            }

    def retained(self) -> dict[str, int]:
        """The bodies no lease holds, which `resources()` counts among all cached bodies."""

        with self._lock:
            idle = [value for value in self._cache.values() if value.references == 0]
            return {"entries": len(idle), "bytes": sum(len(value.body) for value in idle)}

    def close(self) -> None:
        self._stopped.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        with self._lock:
            for entry in tuple(self._entries.values()):
                self._remove(entry)
            for cache_key in tuple(self._cache):
                self._drop(cache_key)
            self._requests.clear()
            self._tombstones.clear()
